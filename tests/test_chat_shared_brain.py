"""A chat in a workspace with shared brains gets one block naming them, once per
provider session. Memory-pass chats and other workspaces never see a name or a path,
and no provider request names a brain folder as a directory (#3006)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from ciao.config import CiaoConfig, SharedBrainLink, WorkspaceConfig
from ciao.models import AgentRequest
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from tests.conftest import attach_stub_mcp


def _build(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("work", "personal")
        },
    )
    return attach_stub_mcp(ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    ))


def _brain(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / "brains" / name
    folder.mkdir(parents=True)
    return folder


def _link(manager: ProjectChatManager, workspace: str, *links: SharedBrainLink) -> None:
    manager._config.workspaces[workspace].shared_brains = list(links)


def _take_turn(manager: ProjectChatManager, chat) -> str:
    """Build the next turn's prefix, then mark its stable context as delivered."""
    prefix = manager._build_prompt_prefix(chat)
    chat.session_id = "sess-1"
    chat.context_digest, chat.context_session_id = manager._stable_context_marker(chat)
    return prefix


def test_chat_in_a_linked_workspace_gets_the_block_once_per_session(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    brain = _brain(tmp_path, "acme-notes")
    _link(manager, "work", SharedBrainLink(name="acme-notes", path=str(brain), identity="github.com/acme/notes"))
    project = manager.create_project("Team", workspace="work")
    chat = manager.create_chat(project.project_id, provider="claude")

    first = _take_turn(manager, chat)
    later = manager._build_prompt_prefix(chat)

    assert "[Shared brains]" in first
    assert f"name=acme-notes path={brain}" in first
    assert "[Shared brains]" not in later


def test_memory_pass_chat_gets_no_block(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    brain = _brain(tmp_path, "acme-notes")
    _link(manager, "work", SharedBrainLink(name="acme-notes", path=str(brain), identity="github.com/acme/notes"))
    project = manager._memory_pass.ensure_project("work")
    chat = manager.create_chat(
        project.project_id,
        helper={"kind": "memory_pass", "source_chat_id": "chat-1", "archive_policy": "when_clean"},
    )

    assert manager.shared_brains_for_chat(chat) == []
    assert str(brain) not in manager._build_prompt_prefix(chat)


def test_other_workspace_gets_no_block(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    work_a = _brain(tmp_path, "acme-notes")
    work_b = _brain(tmp_path, "acme-ops")
    home = _brain(tmp_path, "family-notes")
    _link(
        manager,
        "work",
        SharedBrainLink(name="acme-notes", path=str(work_a), identity="github.com/acme/notes"),
        SharedBrainLink(name="acme-ops", path=str(work_b), identity="github.com/acme/ops"),
    )
    _link(manager, "personal", SharedBrainLink(name="family", path=str(home), identity="github.com/me/family"))
    work_chat = manager.create_chat(manager.create_project("Team", workspace="work").project_id)
    home_chat = manager.create_chat(manager.create_project("Home", workspace="personal").project_id)
    bare_chat = manager.create_chat(manager.create_project("Bare", workspace="").project_id)

    work_prefix = manager._build_prompt_prefix(work_chat)
    home_prefix = manager._build_prompt_prefix(home_chat)

    assert "name=acme-notes" in work_prefix and "name=acme-ops" in work_prefix
    assert str(home) not in work_prefix and "family" not in work_prefix
    assert "name=family" in home_prefix
    assert str(work_a) not in home_prefix and str(work_b) not in home_prefix
    assert "[Shared brains]" not in manager._build_prompt_prefix(bare_chat)


def test_a_background_update_does_not_resend_the_stable_block(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    brain = _brain(tmp_path, "acme-notes")
    (brain / ".git").mkdir()
    fetch_head = brain / ".git" / "FETCH_HEAD"
    fetch_head.write_text("", encoding="utf-8")
    os.utime(fetch_head, (1_700_000_000, 1_700_000_000))
    _link(manager, "work", SharedBrainLink(name="acme-notes", path=str(brain), identity="github.com/acme/notes"))
    project = manager.create_project("Team", workspace="work")
    chat = manager.create_chat(project.project_id, provider="claude")
    _take_turn(manager, chat)
    digest_before = manager._stable_context_marker(chat)

    os.utime(fetch_head, (1_800_000_000, 1_800_000_000))

    assert manager._stable_context_marker(chat) == digest_before
    assert not manager._needs_stable_context(chat)


def test_shared_brain_context_spawns_no_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _build(tmp_path)
    brain = _brain(tmp_path, "acme-notes")
    _link(manager, "work", SharedBrainLink(name="acme-notes", path=str(brain), identity="github.com/acme/notes"))
    project = manager.create_project("Team", workspace="work")
    chat = manager.create_chat(project.project_id, provider="claude")

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the per-turn shared-brain helper ran a subprocess")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)

    assert "[Shared brains]" in manager._build_prompt_prefix(chat)
    assert manager._stable_context_marker(chat)[0]


def test_agent_request_carries_no_extra_directory(tmp_path: Path) -> None:
    """The brain folder reaches the model as text only, never as a provider directory."""
    manager = _build(tmp_path)
    brain = _brain(tmp_path, "acme-notes")
    _link(manager, "work", SharedBrainLink(name="acme-notes", path=str(brain), identity="github.com/acme/notes"))
    project = manager.create_project("Team", workspace="work")
    chat = manager.create_chat(project.project_id, provider="claude")

    request = manager.build_agent_request(chat, prompt="hi")

    assert isinstance(request, AgentRequest)
    assert request.linked_repo == ""
    for field in dataclasses.fields(request):
        assert getattr(request, field.name) != str(brain), field.name
