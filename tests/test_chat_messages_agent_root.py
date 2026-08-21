"""A chat's transcript lives under the root the session RAN in.

Claude Code (and codex, and opencode) key their session storage on a slug of the
cwd. After the re-rooting a chat runs with its workspace's agent root as cwd, so a
session started today is recorded under `-Users-me-repos-ciao-work` — while
`/messages` was reading `-Users-me-repos-ciao`, the install root.

Nothing raised. `get_session_messages_full` returned an empty list for a
directory that holds no such session, the caller treated it as "this segment is
missing", and the endpoint answered with an empty conversation. Every chat created
since the migration rendered BLANK, in the UI, silently. Measured on the real
install: 0 messages where the file on disk had 154.

The install root is still tried, second: every chat from before the migration has
its transcript under exactly that slug and must keep rendering.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import _read_session_segment, chat_messages


def _manager(tmp_path: Path, *, migrated: bool) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if migrated:
        receipt = runtime / "migration" / "workspace-rooting.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    reset_reroot_cache()
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(
                name=name,
                vault_root=f"{name}/memory-vault" if migrated else f"memory-vault/{name}",
            )
            for name in ("personal", "work")
        },
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


class _Msg:
    def __init__(self, role: str, text: str) -> None:
        self.type = role
        self.message = {"role": role, "content": [{"type": "text", "text": text}]}


def _request(pcm: ProjectChatManager, chat_id: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": f"/api/chats/{chat_id}/messages",
        "headers": [],
        "path_params": {"chat_id": chat_id},
        "app": SimpleNamespace(
            state=SimpleNamespace(project_chat_manager=pcm, config=pcm._config),
        ),
    })


@pytest.mark.asyncio
async def test_a_session_recorded_under_the_agent_root_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: the chat was blank because only the install root was tried."""
    pcm = _manager(tmp_path, migrated=True)
    project = pcm.create_project("Sweep", workspace="work")
    chat = pcm.create_chat(project.project_id, title="weekly sweep")
    chat.session_id = "sess-under-work"
    pcm._save()

    agent_root = str(tmp_path / "work")
    def fake(session_id, directory):
        if directory == agent_root:
            return [_Msg("user", "run the sweep"), _Msg("assistant", "swept 12 accounts")]
        return []

    monkeypatch.setitem(sys.modules, "claude_agent_sdk",
                        SimpleNamespace(get_session_messages=fake))

    payload = (await chat_messages(_request(pcm, chat.chat_id))).body.decode()

    assert "swept 12 accounts" in payload


@pytest.mark.asyncio
async def test_a_pre_migration_session_under_the_install_root_still_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chats from before the migration have their transcripts under the install
    root's slug. Preferring the agent root must not lose them."""
    pcm = _manager(tmp_path, migrated=True)
    project = pcm.create_project("Old", workspace="work")
    chat = pcm.create_chat(project.project_id, title="older chat")
    chat.session_id = "sess-under-install"
    pcm._save()

    install_root = str(tmp_path)
    def fake(session_id, directory):
        if directory == install_root:
            return [_Msg("user", "an older question"), _Msg("assistant", "an older answer")]
        return []

    monkeypatch.setitem(sys.modules, "claude_agent_sdk",
                        SimpleNamespace(get_session_messages=fake))

    payload = (await chat_messages(_request(pcm, chat.chat_id))).body.decode()

    assert "an older answer" in payload


def test_the_agent_root_is_tried_before_the_install_root(tmp_path: Path) -> None:
    """Order matters: two roots can hold a session with the same id, and the
    chat's own root is the one that recorded it."""
    seen: list[str] = []

    def fake_full(session_id, directory=None, **kwargs):
        seen.append(str(directory))
        raise FileNotFoundError(directory)

    import ciao.transcripts as transcripts

    original = transcripts.get_session_messages_full
    transcripts.get_session_messages_full = fake_full
    try:
        with pytest.raises(FileNotFoundError):
            _read_session_segment("sess", [str(tmp_path / "work"), str(tmp_path)])
    finally:
        transcripts.get_session_messages_full = original

    assert seen == [str(tmp_path / "work"), str(tmp_path)]


def test_a_session_no_root_holds_still_raises(tmp_path: Path) -> None:
    """Falling through every root must keep raising, so the caller's
    skip-this-segment path still works instead of silently returning nothing."""
    import ciao.transcripts as transcripts

    original = transcripts.get_session_messages_full
    transcripts.get_session_messages_full = lambda *a, **k: (_ for _ in ()).throw(
        FileNotFoundError("nope")
    )
    try:
        with pytest.raises(FileNotFoundError):
            _read_session_segment("sess", [str(tmp_path), str(tmp_path / "work")])
    finally:
        transcripts.get_session_messages_full = original


@pytest.mark.asyncio
async def test_when_both_roots_hold_the_session_the_agent_root_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both roots can hold a session with the same id — a chat resumed after the
    migration writes under its agent root while the pre-migration file lingers
    under the install root. The chat's own root is the current one, so it must be
    read first; the other is a stale copy that stops at the older turns.
    """
    pcm = _manager(tmp_path, migrated=True)
    project = pcm.create_project("Both", workspace="work")
    chat = pcm.create_chat(project.project_id, title="resumed chat")
    chat.session_id = "sess-in-both"
    pcm._save()

    agent_root = str(tmp_path / "work")
    def fake(session_id, directory):
        if directory == agent_root:
            return [_Msg("assistant", "the current turns")]
        return [_Msg("assistant", "a stale copy")]

    monkeypatch.setitem(sys.modules, "claude_agent_sdk",
                        SimpleNamespace(get_session_messages=fake))

    payload = (await chat_messages(_request(pcm, chat.chat_id))).body.decode()

    assert "the current turns" in payload
    assert "a stale copy" not in payload
