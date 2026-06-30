"""Permission-request push notification dispatch.

When the SDK's auto-mode classifier escalates a tool call to the user, the PWA
needs to surface an Approve/Deny bubble. But the user's device might be
backgrounded, the screen locked, or the PWA may not even be open. Web-push
notifications cover those cases so an in-flight turn doesn't stall silently
until the user happens to look.

This tests the plumbing between ProjectChatManager and the push manager via
the ``notify_permission_cb`` hook, the same shape as ``notify_result_cb``
used for final-reply pushes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.models import PermissionRequestEvent
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


def test_notify_permission_invokes_callback(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    calls: list[tuple[str, str, str, str]] = []

    def _cb(chat_id: str, tool_name: str, message: str, request_id: str) -> None:
        calls.append((chat_id, tool_name, message, request_id))

    pcm.notify_permission_cb = _cb

    event = PermissionRequestEvent(
        type="permission_request",
        tool_name="Bash",
        tool_input='{"command": "rm -rf /"}',
        message="Run risky shell?",
        request_id="req-42",
    )
    pcm._notify_permission(chat.chat_id, event)

    assert calls == [(chat.chat_id, "Bash", "Run risky shell?", "req-42")]


def test_notify_permission_is_no_op_without_callback(tmp_path: Path) -> None:
    """No callback configured (e.g. push manager not wired in tests) must not raise."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    event = PermissionRequestEvent(
        type="permission_request",
        tool_name="Bash",
        tool_input="",
        message="Test",
        request_id="req-1",
    )
    pcm._notify_permission(chat.chat_id, event)  # must not raise


def test_notify_permission_swallows_callback_errors(tmp_path: Path) -> None:
    """Callback errors must never propagate — push failure shouldn't abort the turn."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    def _cb(chat_id: str, tool_name: str, message: str, request_id: str) -> None:
        raise RuntimeError("push broken")

    pcm.notify_permission_cb = _cb

    event = PermissionRequestEvent(
        type="permission_request",
        tool_name="Bash",
        tool_input="",
        message="Test",
        request_id="req-1",
    )
    # Must not raise.
    pcm._notify_permission(chat.chat_id, event)
