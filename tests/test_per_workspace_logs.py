"""Per-workspace chat archives: the writer seam, the reader roots and the repoints.

A re-rooted install archives each chat under its workspace's agent root
(``<install>/<name>/Logs/Chats``); the shared ``<install>/Logs`` keeps what no
workspace claims. Readers scan every root.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ciao import config as config_module
from ciao import main
from ciao.config import CiaoConfig, WorkspaceConfig, logs_roots_for, reset_reroot_cache
from ciao.models import AgentRequest, ChatContext
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ChatInfo, ProjectChatManager
from ciao.workspace_reroot import mark_born_per_root, registry_file
from tests.conftest import attach_stub_mcp


@pytest.fixture(autouse=True)
def _clean_reroot_cache() -> Iterator[None]:
    reset_reroot_cache()
    yield
    reset_reroot_cache()


def _config(tmp_path: Path, *, rerooted: bool) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if rerooted:
        mark_born_per_root(tmp_path, runtime, ["personal", "work"])
        registry_file(runtime).write_text(
            json.dumps([{"name": "personal"}, {"name": "work"}]), encoding="utf-8"
        )
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


def _manager(tmp_path: Path, *, rerooted: bool = True) -> ProjectChatManager:
    config = _config(tmp_path, rerooted=rerooted)
    runtime = config.state_path.parent
    return attach_stub_mcp(ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, config.logs_root / "Chats"),
        path=runtime / "web_projects.json",
    ))


# --- config seam -----------------------------------------------------------


def test_chats_root_is_per_workspace_after_the_re_rooting(tmp_path: Path) -> None:
    config = _config(tmp_path, rerooted=True)

    assert config.chats_root("work") == tmp_path / "work" / "Logs" / "Chats"
    assert config.chats_root("") == tmp_path / "Logs" / "Chats"


def test_chats_root_is_the_shared_root_before_the_re_rooting(tmp_path: Path) -> None:
    config = _config(tmp_path, rerooted=False)

    assert config.chats_root("work") == config.logs_root / "Chats"
    assert config.chats_root("work") == tmp_path / "memory-vault" / "Logs" / "Chats"
    assert config.logs_roots() == [config.logs_root]


def test_logs_roots_lists_every_agent_root_then_the_shared_root(tmp_path: Path) -> None:
    config = _config(tmp_path, rerooted=True)
    expected = [
        tmp_path / "personal" / "Logs",
        tmp_path / "work" / "Logs",
        tmp_path / "Logs",
    ]

    assert config.logs_roots() == expected
    assert logs_roots_for(tmp_path, tmp_path / "memory-vault", tmp_path / ".runtime") == expected


def test_an_agent_root_with_logs_is_not_vault_evidence(tmp_path: Path) -> None:
    agent_root = tmp_path / "work"
    (agent_root / "Logs" / "Chats").mkdir(parents=True)
    (agent_root / "AGENTS.md").write_text("# guide\n", encoding="utf-8")

    assert config_module._looks_like_vault(agent_root) is False

    plain = tmp_path / "plain"
    (plain / "Logs").mkdir(parents=True)
    assert config_module._looks_like_vault(plain) is True


def test_main_wires_the_per_chat_resolver() -> None:
    # Comments stripped, including trailing ones: a call moved into a comment
    # still contains the string, so a raw text search would pass with the wiring
    # disabled.
    source = "\n".join(
        line.split("#", 1)[0] for line in inspect.getsource(main).splitlines()
    )

    assert "set_archive_root_resolver(pcm.chat_archive_root)" in source


# --- writer -----------------------------------------------------------------


def _record_one_turn(store: TranscriptStore, chat_id: str) -> ChatContext:
    ctx = ChatContext.for_web(chat_id)
    store.record_turn(
        AgentRequest(prompt="hello", model="sonnet", mode="bypass", resume_session=None, images=[]),
        ctx=ctx,
        response_text="hi there",
        effective_model="sonnet",
        session_id="sess-w",
        usage={},
        quota={},
        input_kind="text",
    )
    return ctx


def test_a_new_archive_lands_in_the_chats_workspace(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    store = pcm._transcripts
    store.set_archive_root_resolver(pcm.chat_archive_root)
    project = pcm.create_project("Work project", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Work chat")
    ctx = _record_one_turn(store, chat.chat_id)

    archived = store.archive_session(
        ctx=ctx, active_model="sonnet", last_effective_model="sonnet", session_id="sess-w"
    )

    assert archived is not None
    expected_root = tmp_path / "work" / "Logs" / "Chats" / chat.chat_id / "claude"
    assert archived.parent == expected_root
    assert not (tmp_path / "Logs" / "Chats" / chat.chat_id).exists()


def test_an_unknown_chat_key_archives_to_the_shared_root(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)

    assert pcm.chat_archive_root("chat-nope") == tmp_path / "Logs" / "Chats"


# --- discovery ----------------------------------------------------------------


_TRANSCRIPT = (
    "---\n"
    "provider: claude\n"
    "context: Work Chat Title\n"
    "active_model: sonnet\n"
    "session_id: sess-d\n"
    "started: 2026-06-10T12:00:00Z\n"
    "ended: 2026-06-10T13:00:00Z\n"
    "---\n\n"
    "## Turn 1\n\n"
    "### User\n\n"
    "```text\n"
    "[CIAO_CONTEXT_BEGIN]\n"
    "[CONTEXT: work -- Workspace. ...]\n"
    '[Project: "Work project"]\n'
    "[CIAO_CONTEXT_END]\n\n"
    "Hello!\n"
    "```\n"
)


def _write_transcript(root: Path, chat_id: str, name: str = "2026-06-10T12-00-00Z-s.md") -> Path:
    path = root / chat_id / "claude" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TRANSCRIPT, encoding="utf-8")
    return path


def test_discovery_imports_archives_from_every_root(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    pcm.create_project("Work project", workspace="work")
    _write_transcript(tmp_path / "work" / "Logs" / "Chats", "chat-a")
    _write_transcript(tmp_path / "Logs" / "Chats", "chat-b")

    pcm.list_projects()

    assert pcm.get_chat("chat-a") is not None
    assert pcm.get_chat("chat-b") is not None
    chat_a = pcm.get_chat("chat-a")
    assert chat_a is not None
    assert chat_a.archive_path.startswith("work/Logs/Chats/chat-a/")


def test_discovery_repoints_a_moved_archive_instead_of_pruning(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("Work project", workspace="work")
    _write_transcript(tmp_path / "work" / "Logs" / "Chats", "chat-m", "s.md")
    pcm._chats["chat-m"] = ChatInfo(
        chat_id="chat-m",
        project_id=project.project_id,
        title="Moved",
        archived=True,
        archive_path="Logs/Chats/chat-m/claude/s.md",
    )
    pcm._save()

    # Called directly: list_projects() also saves unconditionally after discovery,
    # which would hide a dropped save inside _discover_archived_chats.
    pcm._discover_archived_chats()

    moved = pcm.get_chat("chat-m")
    assert moved is not None
    assert moved.archive_path == "work/Logs/Chats/chat-m/claude/s.md"
    # A fresh manager would repoint again in memory, so read the saved registry.
    saved = json.loads((tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8"))
    assert saved["chats"]["chat-m"]["archive_path"] == "work/Logs/Chats/chat-m/claude/s.md"


def test_discovery_still_prunes_an_archive_that_is_nowhere(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("Work project", workspace="work")
    (tmp_path / "work" / "Logs" / "Chats").mkdir(parents=True)
    pcm._chats["chat-gone"] = ChatInfo(
        chat_id="chat-gone",
        project_id=project.project_id,
        title="Gone",
        archived=True,
        archive_path="Logs/Chats/chat-gone/claude/s.md",
    )
    pcm._save()

    pcm.list_projects()

    assert pcm.get_chat("chat-gone") is None


def test_memory_pass_helper_archive_path_is_repointed(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("Work project", workspace="work")
    chat = pcm.create_chat(
        project.project_id,
        helper={
            "kind": "memory_pass",
            "source_chat_id": "chat-m",
            "archive_path": str(tmp_path / "Logs" / "Chats" / "chat-m" / "claude" / "s.md"),
            "archive_policy": "when_clean",
        },
    )
    archive = _write_transcript(tmp_path / "work" / "Logs" / "Chats", "chat-m", "s.md")

    pcm.list_projects()

    reloaded = pcm.get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.helper["archive_path"] == str(archive)


def test_reconcile_sees_a_per_workspace_archive(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("Routines", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Weekly customer intel sweep")
    archived_md = _write_transcript(
        tmp_path / "work" / "Logs" / "Chats", chat.chat_id, "2026-07-10T08-13-21Z-sess.md"
    )
    # The registry reverted exactly as a half-archived chat leaves it.
    chat.session_id = "fake-heal-session-id"
    chat.archived = False
    chat.archive_path = ""
    pcm._save()

    healed = _manager(tmp_path).get_chat(chat.chat_id)

    assert healed is not None
    assert healed.archived is True
    assert healed.archive_path == archived_md.relative_to(tmp_path).as_posix()


# --- memory-pass grant --------------------------------------------------------


@pytest.fixture
def workspace_scope_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectChatManager:
    monkeypatch.setattr("ciao.fs_sandbox.gws_tool_read_roots", lambda path: [])
    manager = _manager(tmp_path)
    workspace = manager._config.workspace("personal")
    assert workspace is not None
    manager._config.workspaces["personal"] = dataclasses.replace(
        workspace, agent_fs_scope="workspace"
    )
    return manager


def _memory_pass_chat_for(manager: ProjectChatManager, source_chat_id: str, archive: Path) -> ChatInfo:
    project = manager._memory_pass.ensure_project("personal")
    return manager.create_chat(
        project.project_id,
        provider="claude",
        helper={
            "kind": "memory_pass",
            "source_chat_id": source_chat_id,
            "archive_path": str(archive),
            "archive_policy": "when_clean",
        },
    )


def test_memory_pass_grant_accepts_a_per_workspace_archive(
    workspace_scope_manager: ProjectChatManager, tmp_path: Path
) -> None:
    manager = workspace_scope_manager
    archive = tmp_path / "personal" / "Logs" / "Chats" / "src-1" / "claude" / "x.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("transcript", encoding="utf-8")
    chat = _memory_pass_chat_for(manager, "src-1", archive)

    request = manager.build_agent_request(chat, prompt="hi")

    assert str(archive.resolve()) in request.agent_read_only
    # A helper pointing at a sibling chat's folder grants nothing.
    (tmp_path / "personal" / "Logs" / "Chats" / "src-2").mkdir()
    sibling = _memory_pass_chat_for(manager, "src-2", archive)
    sibling_request = manager.build_agent_request(sibling, prompt="hi")
    assert str(archive.resolve()) not in sibling_request.agent_read_only


def test_chat_workspaces_is_gone() -> None:
    assert not hasattr(ProjectChatManager, "chat_workspaces")


def test_deleting_a_chat_removes_its_folder_in_every_root(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("Disposable", workspace="work")
    pcm._chats["chat-d"] = ChatInfo(chat_id="chat-d", project_id=project.project_id, title="D")
    pcm._save()
    for root in (tmp_path / "work" / "Logs" / "Chats", tmp_path / "Logs" / "Chats"):
        (root / "chat-d" / "claude").mkdir(parents=True)
        (root / "chat-d" / "claude" / "s.md").write_text("x", encoding="utf-8")
    sibling = tmp_path / "work" / "Logs" / "Chats" / "chat-e"
    sibling.mkdir(parents=True)

    assert pcm.delete_chat("chat-d") is True

    assert not (tmp_path / "work" / "Logs" / "Chats" / "chat-d").exists()
    assert not (tmp_path / "Logs" / "Chats" / "chat-d").exists()
    assert sibling.is_dir()


def test_source_index_spans_every_chats_root(tmp_path: Path) -> None:
    from ciao.web import routes_api

    config = _config(tmp_path, rerooted=True)
    for root, chat_id, stem in (
        (tmp_path / "work" / "Logs" / "Chats", "c1", "a"),
        (tmp_path / "Logs" / "Chats", "c2", "b"),
    ):
        transcript = root / chat_id / "claude" / f"{stem}.md"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("x", encoding="utf-8")
    routes_api._SOURCE_INDEX.clear()

    index = routes_api._source_index(config)

    assert index["a"] == str(tmp_path / "work" / "Logs" / "Chats" / "c1" / "claude" / "a.md")
    assert index["b"] == str(tmp_path / "Logs" / "Chats" / "c2" / "claude" / "b.md")
    routes_api._SOURCE_INDEX.clear()


# --- first root wins (finding 2) ---------------------------------------------


def test_source_index_first_root_wins(tmp_path: Path) -> None:
    from ciao.web import routes_api

    config = _config(tmp_path, rerooted=True)
    work_root = tmp_path / "work" / "Logs" / "Chats"
    shared_root = tmp_path / "Logs" / "Chats"
    for stem in ("dup-a", "dup-b"):
        for root in (work_root, shared_root):
            transcript = root / f"c-{stem}" / "claude" / f"{stem}.md"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("x", encoding="utf-8")
    routes_api._SOURCE_INDEX.clear()

    index = routes_api._source_index(config)

    assert index["dup-a"] == str(work_root / "c-dup-a" / "claude" / "dup-a.md")
    assert index["dup-b"] == str(work_root / "c-dup-b" / "claude" / "dup-b.md")
    routes_api._SOURCE_INDEX.clear()


def test_discovery_first_root_wins_for_a_duplicate_chat(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    pcm.create_project("Work project", workspace="work")
    for chat_id in ("chat-p", "chat-q"):
        _write_transcript(tmp_path / "work" / "Logs" / "Chats", chat_id)
        _write_transcript(tmp_path / "Logs" / "Chats", chat_id)

    pcm.list_projects()

    for chat_id in ("chat-p", "chat-q"):
        chat = pcm.get_chat(chat_id)
        assert chat is not None
        assert chat.archive_path.startswith(f"work/Logs/Chats/{chat_id}/")


# --- pre-reroot manager (finding 3) ------------------------------------------


def test_pre_reroot_archive_lands_in_the_shared_root(tmp_path: Path) -> None:
    pcm = _manager(tmp_path, rerooted=False)
    store = pcm._transcripts
    store.set_archive_root_resolver(pcm.chat_archive_root)
    project = pcm.create_project("Work project", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Pre-split chat")
    ctx = _record_one_turn(store, chat.chat_id)

    archived = store.archive_session(
        ctx=ctx, active_model="sonnet", last_effective_model="sonnet", session_id="sess-w"
    )

    assert archived is not None
    assert archived.parent == tmp_path / "memory-vault" / "Logs" / "Chats" / chat.chat_id / "claude"


def test_pre_reroot_discovery_imports_from_the_shared_root(tmp_path: Path) -> None:
    pcm = _manager(tmp_path, rerooted=False)
    pcm.create_project("Work project", workspace="work")
    _write_transcript(tmp_path / "memory-vault" / "Logs" / "Chats", "chat-pre")

    pcm.list_projects()

    chat = pcm.get_chat("chat-pre")
    assert chat is not None
    assert chat.archive_path == "memory-vault/Logs/Chats/chat-pre/claude/2026-06-10T12-00-00Z-s.md"


def test_pre_reroot_prunes_an_archive_that_is_nowhere(tmp_path: Path) -> None:
    pcm = _manager(tmp_path, rerooted=False)
    project = pcm.create_project("Work project", workspace="work")
    (tmp_path / "memory-vault" / "Logs" / "Chats").mkdir(parents=True)
    pcm._chats["chat-old"] = ChatInfo(
        chat_id="chat-old",
        project_id=project.project_id,
        title="Old",
        archived=True,
        archive_path="memory-vault/Logs/Chats/chat-old/claude/s.md",
    )
    pcm._save()

    pcm.list_projects()

    assert pcm.get_chat("chat-old") is None


# --- delete through a symlinked chat folder (finding 4) ----------------------


@pytest.mark.skipif(sys.platform == "win32", reason="needs a symlink privilege")
def test_deleting_through_a_symlinked_chat_folder_spares_the_sibling(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("Disposable", workspace="work")
    chats_root = tmp_path / "work" / "Logs" / "Chats"
    sibling = chats_root / "chat-sib"
    sibling.mkdir(parents=True)
    (sibling / "s.md").write_text("keep me", encoding="utf-8")
    (chats_root / "chat-s").symlink_to(sibling, target_is_directory=True)
    pcm._chats["chat-s"] = ChatInfo(chat_id="chat-s", project_id=project.project_id, title="S")
    pcm._save()

    pcm.delete_chat("chat-s")

    assert (sibling / "s.md").read_text(encoding="utf-8") == "keep me"


# --- locate archive (finding 5) -----------------------------------------------


def test_locate_archive_refuses_a_real_file_outside_every_root(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    outside = tmp_path / "outside" / "s.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("not an archive", encoding="utf-8")

    assert pcm._locate_archive("chat-x", str(outside)) is None


def test_locate_archive_refuses_a_path_with_fewer_than_two_parts(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)

    assert pcm._locate_archive("chat-x", "s.md") is None


def test_locate_archive_refuses_a_relative_path_that_does_not_resolve(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)

    assert pcm._locate_archive("chat-x", "Logs/Chats/chat-x/claude/none.md") is None


@pytest.mark.skipif(sys.platform == "win32", reason="needs a symlink privilege")
def test_locate_archive_refuses_a_symlink_escaping_the_roots(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    outside = tmp_path / "outside" / "s.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("not an archive", encoding="utf-8")
    link = tmp_path / "work" / "Logs" / "Chats" / "chat-y" / "claude" / "s.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    assert pcm._locate_archive("chat-y", "Logs/Chats/chat-y/claude/s.md") is None


# --- memory-pass grant edges (finding 6) -------------------------------------


def test_memory_pass_grant_accepts_an_archive_in_the_shared_root(
    workspace_scope_manager: ProjectChatManager, tmp_path: Path
) -> None:
    manager = workspace_scope_manager
    archive = tmp_path / "Logs" / "Chats" / "src-s" / "claude" / "x.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("transcript", encoding="utf-8")
    chat = _memory_pass_chat_for(manager, "src-s", archive)

    request = manager.build_agent_request(chat, prompt="hi")

    assert str(archive.resolve()) in request.agent_read_only


def test_memory_pass_grant_refuses_a_source_with_no_folder_in_any_root(
    workspace_scope_manager: ProjectChatManager, tmp_path: Path
) -> None:
    manager = workspace_scope_manager
    archive = tmp_path / "personal" / "Logs" / "Chats" / "src-1" / "claude" / "x.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("transcript", encoding="utf-8")
    chat = _memory_pass_chat_for(manager, "src-missing", archive)

    request = manager.build_agent_request(chat, prompt="hi")

    assert str(archive.resolve()) not in request.agent_read_only


@pytest.mark.skipif(sys.platform == "win32", reason="needs a symlink privilege")
def test_memory_pass_grant_refuses_a_symlinked_source_folder(
    workspace_scope_manager: ProjectChatManager, tmp_path: Path
) -> None:
    manager = workspace_scope_manager
    chats_root = tmp_path / "personal" / "Logs" / "Chats"
    archive = chats_root / "src-real" / "claude" / "x.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("transcript", encoding="utf-8")
    (chats_root / "src-link").symlink_to(chats_root / "src-real", target_is_directory=True)
    chat = _memory_pass_chat_for(manager, "src-link", archive)

    request = manager.build_agent_request(chat, prompt="hi")

    assert str(archive.resolve()) not in request.agent_read_only


def test_memory_pass_grant_refuses_a_traversal_source_id(
    workspace_scope_manager: ProjectChatManager, tmp_path: Path
) -> None:
    manager = workspace_scope_manager
    archive = tmp_path / "personal" / "Logs" / "x" / "claude" / "x.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("transcript", encoding="utf-8")
    chat = _memory_pass_chat_for(manager, "../x", archive)

    request = manager.build_agent_request(chat, prompt="hi")

    assert str(archive.resolve()) not in request.agent_read_only
