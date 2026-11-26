"""``create_project(scaffold=True)`` writes and binds the project's vault note."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
import ciao.web.project_chats as project_chats_module
from ciao.web.project_chats import ProjectChatManager


WS = "personal"


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    """Build a ProjectChatManager backed by tmp_path-only stores."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _frontmatter(doc: Path) -> dict:
    text = doc.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n")[1])


def _make_adopted_manager(tmp_path: Path, root: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={WS: WorkspaceConfig(name=WS, vault_root=str(root))},
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def test_scaffold_writes_parseable_note_with_colon_in_name_and_context(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("SLC: No-code configurator", WS, "Q4: label tool", scaffold=True)
    slug = "slc-no-code-configurator"
    doc = pcm._vault_active_root(WS) / slug / f"{slug}.md"
    assert project.vault_folder == slug
    assert doc.is_file()
    meta = _frontmatter(doc)
    assert meta["title"] == "SLC: No-code configurator"
    assert meta["description"] == "Q4: label tool"
    assert meta["type"] == "project"
    assert meta["status"] == "active"
    assert pcm._read_project_metadata(doc, "x") == ("SLC: No-code configurator", "Q4: label tool")


def test_scaffold_sets_vault_doc_path_and_persists(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Q4 Launch", WS, scaffold=True)
    assert project.vault_doc_path.endswith("q4-launch/q4-launch.md")
    again = _make_manager(tmp_path).get_project(project.project_id)
    assert again is not None
    assert again.vault_folder == "q4-launch"
    assert again.vault_doc_path == project.vault_doc_path


def test_default_create_does_not_touch_the_vault(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Q4 Launch", WS)
    assert project.vault_folder == ""
    assert not (pcm._vault_active_root(WS) / "q4-launch").exists()


def test_scaffold_adopts_existing_folder_without_overwriting(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    doc = pcm._vault_active_root(WS) / "q4-launch" / "q4-launch.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\ntitle: Mine\n---\n\nhand written\n", encoding="utf-8")
    before = doc.read_bytes()
    project = pcm.create_project("Q4 Launch", WS, "ctx", scaffold=True)
    assert doc.read_bytes() == before
    assert project.vault_folder == "q4-launch"


def test_scaffold_skips_a_slug_bound_to_another_project(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    first = pcm.create_project("Q4 Launch", WS, scaffold=True)
    doc = pcm._vault_active_root(WS) / "q4-launch" / "q4-launch.md"
    before = doc.read_bytes()
    second = pcm.create_project("q4 launch", WS, scaffold=True)
    assert first.vault_folder == "q4-launch"
    assert second.vault_folder == ""
    assert doc.read_bytes() == before


def test_scaffold_skips_when_completed_folder_exists(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    (pcm._vault_completed_root(WS) / "q4-launch").mkdir(parents=True)
    project = pcm.create_project("Q4 Launch", WS, scaffold=True)
    assert project.vault_folder == ""
    assert not (pcm._vault_active_root(WS) / "q4-launch").exists()


def test_scaffold_without_context_omits_description(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    pcm.create_project("Plain", WS, scaffold=True)
    doc = pcm._vault_active_root(WS) / "plain" / "plain.md"
    assert "description" not in _frontmatter(doc)


def test_scaffold_skips_unknown_workspace(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("X", "nowhere", scaffold=True)
    assert project.vault_folder == ""
    assert not (pcm._workspace_vault_root("nowhere") / "projects").exists()


def test_scaffold_skips_non_ascii_slug(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Café", WS, scaffold=True)
    assert project.vault_folder == ""
    assert not (pcm._vault_active_root(WS) / "café").exists()


def test_scaffold_write_failure_leaves_no_partial_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)

    real_rename = project_chats_module._publish_exclusive_at

    def boom(src_fd, source, dst_fd, destination, *, expected_fd):
        if destination == "q4-launch.md":
            raise OSError("disk full")
        return real_rename(src_fd, source, dst_fd, destination, expected_fd=expected_fd)

    monkeypatch.setattr(project_chats_module, "_publish_exclusive_at", boom)
    with pytest.raises(ValueError, match="project note could not be created"):
        pcm.create_project("Q4 Launch", WS, scaffold=True)
    folder = pcm._vault_active_root(WS) / "q4-launch"
    assert folder.is_dir()
    assert not (folder / "q4-launch.md").exists()


def test_scaffold_does_not_publish_note_when_temp_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)

    def fail_fsync(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(ValueError, match="project note could not be created"):
        pcm.create_project("Q4 Launch", WS, scaffold=True)

    folder = pcm._vault_active_root(WS) / "q4-launch"
    assert folder.is_dir()
    assert not (folder / "q4-launch.md").exists()


def test_scaffold_fails_closed_when_private_stage_is_on_another_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno
    from types import SimpleNamespace

    pcm = _make_manager(tmp_path)
    vault = pcm._vault_active_root(WS)
    vault.mkdir(parents=True, exist_ok=True)
    staging = tmp_path / ".runtime"
    stage_fd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    dest_fd = os.open(vault, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    helper_stage_fd = -1
    original_open_chain = project_chats_module._open_directory_chain
    original_fstat = os.fstat

    def capture_stage(root, parts, **kwargs):
        nonlocal helper_stage_fd
        fd = original_open_chain(root, parts, **kwargs)
        if root == staging:
            helper_stage_fd = fd
        return fd

    def different_volume(fd: int):
        result = original_fstat(fd)
        if fd in {stage_fd, helper_stage_fd}:
            return SimpleNamespace(st_dev=result.st_dev + 1)
        return result

    monkeypatch.setattr(project_chats_module, "_open_directory_chain", capture_stage)
    monkeypatch.setattr(os, "fstat", different_volume)
    try:
        with pytest.raises(OSError) as error:
            project_chats_module._write_file_exclusive_at(
                dest_fd, "project.md", b"note\n", staging
            )
        assert error.value.errno == errno.EXDEV
        assert not (vault / "project.md").exists()
    finally:
        os.close(stage_fd)
        os.close(dest_fd)


def test_adopted_scaffold_is_folder_only_even_when_rename_and_link_fail_exdev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno

    cloud_root = tmp_path / "cloud-vault"
    cloud_root.mkdir()
    pcm = _make_adopted_manager(tmp_path, cloud_root)
    events: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", events.append)

    def fail_rename(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device rename")

    def fail_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(project_chats_module, "_publish_exclusive_at", fail_rename)
    monkeypatch.setattr(os, "link", fail_link)
    general = next(p for p in pcm._projects.values() if p.name == "General")
    project = pcm.create_project("Garden Plan", WS, "local context", scaffold=True)

    assert general is not None and general.vault_folder_only
    assert project.vault_folder == "garden-plan"
    assert project.vault_folder_only is True
    assert project.vault_doc_path == ""
    persisted = json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )["projects"][project.project_id]
    assert persisted["vault_folder_only"] is True
    assert project.to_dict()["vault_folder_only"] is True
    assert (cloud_root / "projects/active/garden-plan").is_dir()
    assert not (cloud_root / "projects/active/garden-plan/garden-plan.md").exists()
    assert [event["type"] for event in events] == ["project_created"]


def test_adopted_folder_later_discovers_user_note_without_changing_bytes(
    tmp_path: Path,
) -> None:
    cloud_root = tmp_path / "cloud-vault"
    cloud_root.mkdir()
    pcm = _make_adopted_manager(tmp_path, cloud_root)
    project = pcm.create_project("Garden Plan", WS, "local context", scaffold=True)
    note = cloud_root / "projects/active/garden-plan/README.md"
    note_bytes = b"---\ntitle: Garden plan\ndescription: Owner-written\n---\r\n\nKeep these exact bytes.\r\n"
    note.write_bytes(note_bytes)

    listed = pcm.list_projects(WS)
    discovered = next(item for item in listed if item.project_id == project.project_id)

    assert discovered.vault_folder_only is False
    assert discovered.vault_doc_path.endswith("garden-plan/README.md")
    assert discovered.context == "Owner-written"
    assert note.read_bytes() == note_bytes


def test_adopted_first_bind_does_not_write_context_into_user_note(
    tmp_path: Path,
) -> None:
    cloud_root = tmp_path / "cloud-vault"
    cloud_root.mkdir()
    pcm = _make_adopted_manager(tmp_path, cloud_root)
    project = pcm.create_project("Garden Plan", WS, "local context")
    note = cloud_root / "projects/active/garden-plan/README.md"
    note.parent.mkdir(parents=True)
    note_bytes = b"---\ntitle: Garden Plan\n---\n\nCloud authored.\r\n"
    note.write_bytes(note_bytes)

    discovered = next(
        item for item in pcm.list_projects(WS) if item.project_id == project.project_id
    )

    assert discovered.vault_folder == "garden-plan"
    assert discovered.vault_doc_path.endswith("garden-plan/README.md")
    assert note.read_bytes() == note_bytes


def test_adopted_legacy_personal_note_is_never_promoted_or_removed(
    tmp_path: Path,
) -> None:
    cloud_root = tmp_path / "cloud-vault"
    legacy = cloud_root / "projects/active/Old Project.md"
    legacy.parent.mkdir(parents=True)
    original = b"owner's original note\r\n"
    legacy.write_bytes(original)

    pcm = _make_adopted_manager(tmp_path, cloud_root)

    assert legacy.read_bytes() == original
    assert not (cloud_root / "projects/active/Old Project/Old Project.md").exists()
    general = next(p for p in pcm._projects.values() if p.name == "General")
    assert general.vault_folder_only
    assert not (cloud_root / "projects/active/general/general.md").exists()


def test_unavailable_adopted_root_fails_without_project_registration_or_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud_root = tmp_path / "missing-cloud-vault"
    pcm = _make_adopted_manager(tmp_path, cloud_root)
    events: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", events.append)

    with pytest.raises(ValueError, match="workspace notes folder is unavailable"):
        pcm.create_project("Garden Plan", WS, scaffold=True)

    assert not cloud_root.exists()
    assert not any(p.name == "Garden Plan" for p in pcm._projects.values())
    assert not any(event["type"] == "project_created" for event in events)


@pytest.mark.parametrize("swap_root", [False, True])
def test_adopted_scaffold_swap_before_bind_registers_nothing_and_emits_no_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap_root: bool,
) -> None:
    cloud_root = tmp_path / "cloud-vault"
    cloud_root.mkdir()
    pcm = _make_adopted_manager(tmp_path, cloud_root)
    events: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", events.append)
    if swap_root:
        original = pcm._same_active_directory

        def swap_active(workspace: str, fd: int) -> bool:
            held = tmp_path / "detached-cloud-root"
            cloud_root.rename(held)
            cloud_root.mkdir()
            return original(workspace, fd)

        monkeypatch.setattr(pcm, "_same_active_directory", swap_active)
    else:
        original_same_child = project_chats_module._same_child_directory
        swapped = False

        def swap_slug(parent_fd: int, name: str, expected_fd: int) -> bool:
            nonlocal swapped
            if name == "garden-plan" and not swapped:
                swapped = True
                os.rename(name, "detached-slug", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.mkdir(name, dir_fd=parent_fd)
            return original_same_child(parent_fd, name, expected_fd)

        monkeypatch.setattr(project_chats_module, "_same_child_directory", swap_slug)

    with pytest.raises(ValueError, match="project note could not be created"):
        pcm.create_project("Garden Plan", WS, scaffold=True)

    assert not any(event["type"] == "project_created" for event in events)
    assert not any(project.name == "Garden Plan" for project in pcm._projects.values())


def test_scaffold_does_not_unlink_replacement_at_staging_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)
    replacement_name = ""
    real_rename = project_chats_module._publish_exclusive_at

    def replace_staging(src_fd: int, source: str, dst_fd: int, destination: str, *, expected_fd: int) -> None:
        nonlocal replacement_name
        if destination == "q4-launch.md":
            replacement_name = source
            os.rename(source, "held-staging", src_dir_fd=src_fd, dst_dir_fd=src_fd)
            fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=src_fd)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("sync-owned")
            return real_rename(src_fd, source, dst_fd, destination, expected_fd=expected_fd)
        return real_rename(src_fd, source, dst_fd, destination, expected_fd=expected_fd)
    monkeypatch.setattr(project_chats_module, "_publish_exclusive_at", replace_staging)
    with pytest.raises(ValueError, match="project note could not be created"):
        pcm.create_project("Q4 Launch", WS, scaffold=True)

    staging = tmp_path / ".runtime"
    assert (staging / replacement_name).read_text(encoding="utf-8") == "sync-owned"


def test_scaffold_publishes_open_file_when_stage_is_swapped_at_syscall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)
    staging = tmp_path / ".runtime"
    real_publish = project_chats_module.publish_new_from_fd
    replacement: Path | None = None

    def swap_and_publish(fd: int, directory_fd: int, name: str) -> None:
        nonlocal replacement
        if name == "q4-launch.md":
            replacement = next(path for path in staging.glob(".ciao-write-*.tmp")
                               if os.path.samestat(path.stat(), os.fstat(fd)))
            replacement.rename(staging / "held-stage")
            replacement.write_bytes(b"sync-owned")
        real_publish(fd, directory_fd, name)

    monkeypatch.setattr(project_chats_module, "publish_new_from_fd", swap_and_publish)
    project = pcm.create_project("Q4 Launch", WS, scaffold=True)
    note = pcm._vault_active_root(WS) / project.vault_folder / "q4-launch.md"
    assert _frontmatter(note)["title"] == "Q4 Launch"
    assert replacement is not None and replacement.read_bytes() == b"sync-owned"


def test_description_with_dashes_round_trips(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    pcm.create_project("Dash", WS, "before --- after", scaffold=True)
    doc = pcm._vault_active_root(WS) / "dash" / "dash.md"
    assert pcm._read_project_metadata(doc, "x")[1] == "before --- after"
