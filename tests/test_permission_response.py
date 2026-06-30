"""PWA permission-response round-trip.

Validates the full chain from the client-facing route down to the provider's
PermissionGate:

    ws client  →  respond_permission(chat_id, request_id, approved, reason)
               →  ProjectChatManager.respond_permission
               →  ProviderService.provider.permission_gate.answer
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
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


@pytest.mark.asyncio
async def test_respond_permission_forwards_to_provider_gate(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    # Force a provider to exist so we can simulate a pending permission.
    provider_service = pcm._get_provider(chat.chat_id)
    gate = provider_service.provider.permission_gate

    from claude_agent_sdk.types import (
        PermissionResultAllow,
        ToolPermissionContext,
    )

    ctx = ToolPermissionContext(
        signal=None, suggestions=[], tool_use_id="tool-1", agent_id=None
    )
    pending = asyncio.create_task(gate.handle("Bash", {"command": "ls"}, ctx))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    ok = pcm.respond_permission(
        chat.chat_id, request_id="tool-1", approved=True, reason=""
    )
    assert ok is True

    result = await pending
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_respond_permission_returns_false_when_no_provider(tmp_path: Path) -> None:
    """A permission reply for a chat with no provider yet must be a no-op."""
    pcm = _make_manager(tmp_path)
    ok = pcm.respond_permission("no-such-chat", request_id="x", approved=True, reason="")
    assert ok is False


@pytest.mark.asyncio
async def test_respond_permission_unknown_request_id_returns_false(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    pcm._get_provider(chat.chat_id)  # instantiate gate

    ok = pcm.respond_permission(
        chat.chat_id, request_id="never-asked", approved=True, reason=""
    )
    assert ok is False


@pytest.mark.asyncio
async def test_respond_permission_strips_buffered_event_from_active_stream(
    tmp_path: Path,
) -> None:
    """An answered permission must not replay on the next subscribe.

    Repro: the user opens a chat, the SDK asks for Bash approval, the
    user taps Approve, then later reopens the chat. Without buffer
    cleanup the broker replays the original ``permission_request`` and
    the PWA renders a phantom Approve/Deny card for a request that's
    already been answered.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    # Simulate an in-flight stream that already published a permission_request.
    stream = ChatStream("hi")
    pcm._broker.register(chat.chat_id, stream)
    stream.publish({
        "type": "permission_request",
        "tool_name": "Bash",
        "tool_input": "ls",
        "message": "Approve use of Bash?",
        "request_id": "tool-99",
    })

    # Ensure a provider exists so respond_permission's gate hop is exercised.
    pcm._get_provider(chat.chat_id)

    # Stale reply (gate has nothing pending) — should still strip the buffer.
    pcm.respond_permission(
        chat.chat_id, request_id="tool-99", approved=True, reason=""
    )

    replay = stream.buffered_events()
    assert all(
        ev.get("type") != "permission_request" for ev in replay
    ), f"buffered permission_request leaked into replay: {replay}"
