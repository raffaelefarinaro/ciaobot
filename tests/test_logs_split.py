"""One-time update migration: each registry-resolved Logs/Chats/chat-* moves into
its workspace's Logs/Chats (ciao/logs_split.py). Every fixture is built from
tmp_path; nothing here touches a real install.
"""

from __future__ import annotations

import errno
import inspect
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import logs_split, main
from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.logs_split import migrate_if_needed, read_receipt, receipt_path
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.workspace_reroot import mark_born_per_root
from tests.conftest import attach_stub_mcp


@pytest.fixture(autouse=True)
def _clean_reroot_cache() -> Iterator[None]:
    reset_reroot_cache()
    yield
    reset_reroot_cache()


def _install(tmp_path: Path, *, rerooted: bool = True) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if rerooted:
        mark_born_per_root(tmp_path, runtime, ["personal", "work"])
    reset_reroot_cache()
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )


def _registry(runtime: Path, chats: dict[str, str]) -> None:
    """Write `web_projects.json`: one project per workspace, chats keyed to a project.

    `chats` maps a chat id to the project id it belongs to. The project ids are
    `p-personal` and `p-work`, and `p-gone` names a workspace that is not registered.
    """
    projects = {
        "p-personal": {"name": "Personal", "workspace": "personal"},
        "p-work": {"name": "Work", "workspace": "work"},
        "p-gone": {"name": "Gone", "workspace": "gone"},
    }
    data = {
        "version": 1,
        "revision": 0,
        "projects": projects,
        "chats": {cid: {"project_id": pid} for cid, pid in chats.items()},
    }
    (runtime / "web_projects.json").write_text(json.dumps(data), encoding="utf-8")


def _archive(root: Path, cid: str, name: str = "s.md", body: str = "x") -> Path:
    path = root / cid / "claude" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _shared(config: CiaoConfig) -> Path:
    return config.logs_root / "Chats"


def test_moves_every_resolved_chat_with_no_cap(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    chats = {
        f"chat-{i:04d}": "p-work" if i % 2 == 0 else "p-personal" for i in range(250)
    }
    _registry(runtime, chats)
    for cid in chats:
        _archive(_shared(config), cid)

    result = migrate_if_needed(config)

    assert result["moved"] == 250
    assert result["status"] == "migrated"
    for i in range(250):
        cid = f"chat-{i:04d}"
        ws = "work" if i % 2 == 0 else "personal"
        assert (tmp_path / ws / "Logs" / "Chats" / cid / "claude" / "s.md").is_file()
    assert [p for p in _shared(config).iterdir() if p.name.startswith("chat-")] == []


def test_unresolvable_items_stay_in_the_shared_root(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {})
    ghost = _archive(_shared(config), "chat-ghost")
    loose = _shared(config) / "summary.md"
    loose.write_text("s", encoding="utf-8")
    cli = tmp_path / "Logs" / "CLI" / "x.md"
    telegram = tmp_path / "Logs" / "Telegram" / "y.md"
    meetings = tmp_path / "Logs" / "Meetings" / "z.md"
    for path in (cli, telegram, meetings):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    result = migrate_if_needed(config)

    assert result["unresolved"] == 1
    assert result["status"] == "migrated"
    for path in (ghost, loose, cli, telegram, meetings):
        assert path.exists(), path


def test_chat_without_a_known_workspace_goes_to_primary(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-orphan": "p-gone"})
    _archive(_shared(config), "chat-orphan")

    migrate_if_needed(config)

    primary = config.primary_workspace()
    assert (config.chats_root(primary) / "chat-orphan" / "claude" / "s.md").is_file()


def test_a_chat_with_a_pending_turn_journal_is_skipped_and_retried(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-busy": "p-work"})
    _archive(_shared(config), "chat-busy")
    journal = runtime / "transcripts" / "chat-busy" / "journal" / "claude.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}\n", encoding="utf-8")

    first = migrate_if_needed(config)

    assert first["status"] == "partial"
    assert first["busy"] == ["chat-busy"]
    assert (_shared(config) / "chat-busy").is_dir()
    assert read_receipt(runtime) is None

    journal.unlink()
    second = migrate_if_needed(config)

    assert second["status"] == "migrated"
    assert (tmp_path / "work" / "Logs" / "Chats" / "chat-busy" / "claude" / "s.md").is_file()


def test_rerun_is_a_noop_after_the_receipt(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-0": "p-work"})
    _archive(_shared(config), "chat-0")
    assert migrate_if_needed(config)["status"] == "migrated"

    _registry(runtime, {"chat-0": "p-work", "chat-late": "p-work"})
    _archive(_shared(config), "chat-late")
    second = migrate_if_needed(config)

    assert second["status"] == "already_migrated"
    assert (_shared(config) / "chat-late" / "claude" / "s.md").is_file()


def test_half_moved_state_recovers(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-h": "p-work"})
    _archive(_shared(config), "chat-h", "a.md", "same")
    _archive(_shared(config), "chat-h", "b.md", "only-shared")
    _archive(tmp_path / "work" / "Logs" / "Chats", "chat-h", "a.md", "same")

    result = migrate_if_needed(config)

    assert result["merged"] == 1
    assert result["status"] == "migrated"
    work_chat = tmp_path / "work" / "Logs" / "Chats" / "chat-h" / "claude"
    assert (work_chat / "a.md").read_text(encoding="utf-8") == "same"
    assert (work_chat / "b.md").read_text(encoding="utf-8") == "only-shared"
    assert not (_shared(config) / "chat-h").exists()


def test_conflicting_file_stays_readable_in_the_shared_root(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-h": "p-work"})
    _archive(_shared(config), "chat-h", "a.md", "shared-bytes")
    _archive(tmp_path / "work" / "Logs" / "Chats", "chat-h", "a.md", "work-bytes")

    result = migrate_if_needed(config)

    assert result["conflicts"] == ["chat-h"]
    assert result["status"] == "migrated"
    assert (_shared(config) / "chat-h" / "claude" / "a.md").read_text(encoding="utf-8") == "shared-bytes"
    work_a = tmp_path / "work" / "Logs" / "Chats" / "chat-h" / "claude" / "a.md"
    assert work_a.read_text(encoding="utf-8") == "work-bytes"


def test_pre_reroot_install_is_not_applicable(tmp_path: Path) -> None:
    config = _install(tmp_path, rerooted=False)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-0": "p-work"})
    archive = _archive(config.logs_root / "Chats", "chat-0")

    result = migrate_if_needed(config)

    assert result["status"] == "not_applicable"
    assert archive.is_file()
    assert not receipt_path(runtime).exists()


def test_bootstrap_mode_is_skipped(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    config = SimpleNamespace(bootstrap_mode=True, state_path=runtime / "state.json")

    assert migrate_if_needed(config) == {"status": "skipped"}


def test_never_raises_on_a_broken_config(tmp_path: Path) -> None:
    assert migrate_if_needed(SimpleNamespace())["status"] == "error"


def test_rename_failure_is_partial_and_left_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-0": "p-work"})
    archive = _archive(_shared(config), "chat-0")

    def refuse(src: object, dst: object) -> None:
        raise OSError(errno.EXDEV, "x")

    monkeypatch.setattr(logs_split.os, "rename", refuse)
    result = migrate_if_needed(config)

    assert result["failed"] == ["chat-0"]
    assert archive.is_file()
    assert result["status"] == "partial"


def test_discovery_repoints_after_the_split(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-0000": "p-work"})
    _archive(_shared(config), "chat-0000", "s.md")
    assert migrate_if_needed(config)["status"] == "migrated"

    # A registry written before the split: the chat is archived under the shared root.
    data = json.loads((runtime / "web_projects.json").read_text(encoding="utf-8"))
    data["chats"]["chat-0000"] = {
        "project_id": "p-work",
        "archived": True,
        "archive_path": "Logs/Chats/chat-0000/claude/s.md",
    }
    (runtime / "web_projects.json").write_text(json.dumps(data), encoding="utf-8")

    pcm = attach_stub_mcp(
        ProjectChatManager(
            config,
            state_store=StateStore(config.state_path, tmp_path, config.media_root),
            transcript_store=TranscriptStore(runtime, config.logs_root / "Chats"),
            path=runtime / "web_projects.json",
        )
    )
    pcm.list_projects()

    chat = pcm.get_chat("chat-0000")
    assert chat is not None
    assert chat.archive_path == "work/Logs/Chats/chat-0000/claude/s.md"


def test_startup_splits_after_the_reroot_and_before_the_index(tmp_path: Path) -> None:
    # Comments stripped, including trailing ones: a call moved into a comment still
    # contains the string, so a raw text search would pass with the step disabled.
    source = "\n".join(
        line.split("#", 1)[0] for line in inspect.getsource(main).splitlines()
    )
    reroot = source.index("migrate_if_needed, config")
    split = source.index("split_chat_logs, config")
    index = source.index("_refresh_vault_index,")
    registry = source.index("ProjectChatManager(")

    assert reroot < split < index < registry, (reroot, split, index, registry)


def test_an_unreadable_registry_with_chats_on_disk_is_not_migrated(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    (runtime / "web_projects.json").write_text("{not json", encoding="utf-8")
    _archive(_shared(config), "chat-0")

    first = migrate_if_needed(config)

    assert first["status"] != "migrated"
    assert first["registry_unreadable"] is True
    assert (_shared(config) / "chat-0").is_dir()
    assert read_receipt(runtime) is None

    _registry(runtime, {"chat-0": "p-work"})
    second = migrate_if_needed(config)

    assert second["status"] == "migrated"
    assert (tmp_path / "work" / "Logs" / "Chats" / "chat-0" / "claude" / "s.md").is_file()


def test_a_missing_registry_with_chats_on_disk_is_not_migrated(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _archive(_shared(config), "chat-0")

    result = migrate_if_needed(config)

    assert result["status"] == "partial"
    assert result["registry_unreadable"] is True
    assert (_shared(config) / "chat-0").is_dir()
    assert read_receipt(runtime) is None


def test_a_fresh_install_with_no_registry_and_no_chats_is_migrated(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent

    result = migrate_if_needed(config)

    assert result["status"] == "migrated"
    assert result["registry_unreadable"] is False
    assert read_receipt(runtime) is not None


def test_an_absent_shared_archive_is_migrated_with_no_moves(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-0": "p-work"})
    assert not _shared(config).exists()

    result = migrate_if_needed(config)

    assert result["status"] == "migrated"
    assert result["moved"] == 0
    assert read_receipt(runtime) is not None


def test_a_destination_that_is_a_file_fails_and_stays_partial(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-f": "p-work"})
    archive = _archive(_shared(config), "chat-f")
    blocker = tmp_path / "work" / "Logs" / "Chats" / "chat-f"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a folder", encoding="utf-8")

    result = migrate_if_needed(config)

    assert result["failed"] == ["chat-f"]
    assert archive.is_file()
    assert result["status"] == "partial"
    assert read_receipt(runtime) is None


def test_symlinks_in_a_merged_chat_stay_in_the_shared_root(tmp_path: Path) -> None:
    config = _install(tmp_path)
    runtime = config.state_path.parent
    _registry(runtime, {"chat-l": "p-work"})
    # The destination already holds the chat, so the move is a merge.
    _archive(tmp_path / "work" / "Logs" / "Chats", "chat-l", "keep.md", "keep")
    provider = _shared(config) / "chat-l" / "claude"
    provider.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside", encoding="utf-8")
    try:
        (provider / "link.md").symlink_to(outside / "secret.md")
        (provider / "linkdir").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this platform cannot create symlinks")

    result = migrate_if_needed(config)

    assert result["conflicts"] == ["chat-l"]
    assert result["status"] == "migrated"
    assert (provider / "link.md").is_symlink()
    assert (provider / "linkdir").is_symlink()
    assert (outside / "secret.md").read_text(encoding="utf-8") == "outside"
    assert (tmp_path / "work" / "Logs" / "Chats" / "chat-l" / "claude" / "keep.md").is_file()
