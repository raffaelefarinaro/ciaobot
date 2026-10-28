"""`CiaoControlPlane.chat_reload`: restart a chat's provider so changed tools load."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import AgentPrincipal, CiaoControlPlane, ControlPlaneError
from ciao.provider_service import ProviderService
from ciao.providers.opencode import OpencodeProvider
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


class _FakeProvider:
    background_work_outstanding = False
    background_activity_age = 0.0

    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


def _setup(tmp_path: Path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    pcm = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    plane = CiaoControlPlane(
        config, project_chat_manager=pcm, schedule_manager=SimpleNamespace()
    )
    project = pcm.create_project("2026-q2-reload-cp", workspace="work")
    return pcm, plane, project


def _principal(chat_id: str, project_id: str) -> AgentPrincipal:
    return AgentPrincipal(
        token_id="t",
        chat_id=chat_id,
        project_id=project_id,
        workspace="work",
        provider="claude",
    )


async def test_own_chat_with_a_foreground_stream_arms_the_reload(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    chat = pcm.create_chat(project.project_id, title="own")
    gate = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        await gate.wait()
        return
        yield

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "initial")
    for _ in range(100):
        if pcm.get_active_stream(chat.chat_id) is not None:
            break
        await asyncio.sleep(0.01)

    result = plane.chat_reload(_principal(chat.chat_id, project.project_id), "")

    assert result["data"]["reload"] == "after_turn"
    assert stream.reload_continue is True
    gate.set()


async def test_busy_chat_is_refused(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    chat = pcm.create_chat(project.project_id, title="busy")
    pcm._subagents._background_agents_last[chat.chat_id] = 1

    with pytest.raises(ControlPlaneError) as exc:
        plane.chat_reload(_principal(chat.chat_id, project.project_id), chat.chat_id)
    assert exc.value.code == "chat_busy"


async def test_chat_reload_own_opencode_chat_not_blocked_by_foreground_shell(
    tmp_path: Path,
) -> None:
    pcm, _plane, project = _setup(tmp_path)
    chat = pcm.create_chat(project.project_id, title="own")
    provider = OpencodeProvider(tmp_path)
    service = ProviderService(pcm._config)
    service._provider = provider
    pcm._providers[chat.chat_id] = service
    fixture = Path(__file__).parent / "fixtures" / "opencode" / "foreground_shell_turn.jsonl"
    for line in fixture.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        provider._track_background_event(event, "ses_fgshell_parent")
        if event["type"] == "session.tool.progress":
            break
    else:
        raise AssertionError("fixture has no session.tool.progress")

    assert pcm.provider_reload_blocked(chat.chat_id) is False


async def test_another_idle_chat_has_its_provider_popped(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    caller = pcm.create_chat(project.project_id, title="caller")
    other = pcm.create_chat(project.project_id, title="other")
    provider = _FakeProvider()
    pcm._providers[other.chat_id] = provider  # type: ignore[assignment]

    result = plane.chat_reload(_principal(caller.chat_id, project.project_id), other.chat_id)
    await asyncio.sleep(0.05)

    assert result["data"]["reload"] == "next_turn"
    assert other.chat_id not in pcm._providers
    assert provider.disconnected is True


async def test_another_active_chat_is_refused(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    caller = pcm.create_chat(project.project_id, title="caller")
    other = pcm.create_chat(project.project_id, title="other")
    pcm.active_chat_ids = lambda: {other.chat_id}  # type: ignore[method-assign]

    with pytest.raises(ControlPlaneError) as exc:
        plane.chat_reload(_principal(caller.chat_id, project.project_id), other.chat_id)
    assert exc.value.code == "chat_busy"


async def test_unknown_chat_is_not_found(tmp_path: Path) -> None:
    _, plane, project = _setup(tmp_path)
    with pytest.raises(ControlPlaneError) as exc:
        plane.chat_reload(_principal("chat-x", project.project_id), "missing")
    assert exc.value.code == "chat_not_found"
