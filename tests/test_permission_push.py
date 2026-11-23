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

import json
from pathlib import Path


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


def _pass(pcm: ProjectChatManager, title: str):
    """A memory pass chat in this workspace's Memory project.

    The engine only opens a chat there for a pass, so this is the only way to
    get one: an ordinary chat in that project would be refused.
    """
    memory_project = pcm._memory_pass.ensure_project("personal")
    return pcm.create_chat(
        memory_project.project_id,
        title=title,
        helper={
            "kind": "memory_pass",
            "source_chat_id": "chat-1",
            "archive_policy": "when_clean",
        },
    )


def _failed_closed_pass(pcm: ProjectChatManager, title: str):
    """A pass whose helper no longer identifies it.

    The registry loader normalises every stored helper, so a malformed one
    comes back empty: the chat is a pass only by the project it lives in,
    which is what the project-kind fallback of the pass predicate is for.
    """
    chat = _pass(pcm, title)
    chat.helper = {}
    return chat


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
        shell_command=True,
        message="Run risky shell?",
        request_id="req-42",
    )
    pcm._notify_permission(chat.chat_id, event)

    assert calls == [(chat.chat_id, "Shell", "Shell command needs approval", "req-42")]


def test_notify_permission_persists_pending_permission_on_chat(tmp_path: Path) -> None:
    """A backgrounded chat must be able to show it needs attention in-app,

    not just via the OS push — before this fix `pending_permission` didn't
    exist and only the push callback + the open ChatPanel's ephemeral state
    ever learned about an outstanding Approve/Deny prompt.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    event = PermissionRequestEvent(
        type="permission_request",
        tool_name="Bash: echo REGISTRY_SECRET",
        tool_input='{"command": "echo REGISTRY_SECRET"}',
        shell_command=True,
        message="REGISTRY_SECRET in title too",
        request_id="req-42",
    )
    pcm._notify_permission(chat.chat_id, event)

    stored = pcm._chats[chat.chat_id].pending_permission
    marker = json.loads(stored)
    assert marker == {
        "request_id": "req-42",
        "tool_name": "Shell",
        "message": "Shell command needs approval",
        "shell_command": True,
    }
    assert "REGISTRY_SECRET" not in stored


def test_shell_marker_is_cleared_after_engine_restart(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    pcm._notify_permission(chat.chat_id, PermissionRequestEvent(
        type="permission_request",
        tool_name="Shell",
        tool_input="echo PRIVATE_COMMAND",
        message="PRIVATE_COMMAND",
        request_id="restart-request",
        session_id="restart-session",
        shell_command=True,
    ))

    restarted = _make_manager(tmp_path)
    assert restarted._chats[chat.chat_id].pending_permission == ""
    saved = json.loads((tmp_path / ".runtime" / "web_projects.json").read_text())
    assert saved["chats"][chat.chat_id]["pending_permission"] == ""


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


async def test_delayed_push_suppressed_when_chat_archived(tmp_path: Path) -> None:
    """An auto-archive schedule that archives on completion must not notify:
    _delayed_push skips a chat archived during the delay window (the run needed
    no user action, so there's nothing to open)."""
    pcm = _make_manager(tmp_path)
    pcm._push_delay_seconds = 0
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Morning briefing")
    chat.last_activity_at = "2026-07-14T09:00:00Z"
    chat.last_read_at = ""
    chat.archived = True  # auto-archived on completion

    calls: list[tuple[str, str, str]] = []
    pcm.notify_result_cb = lambda cid, title, snippet: calls.append((cid, title, snippet))

    await pcm._delayed_push(chat.chat_id, "Morning briefing", "done")
    assert calls == []  # archived → no notification


async def test_delayed_push_fires_for_active_unread_chat(tmp_path: Path) -> None:
    """Control: a still-active, unread chat gets its push as before."""
    pcm = _make_manager(tmp_path)
    pcm._push_delay_seconds = 0
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Live chat")
    chat.last_activity_at = "2026-07-14T09:00:00Z"
    chat.last_read_at = ""
    chat.archived = False

    calls: list[tuple[str, str, str]] = []
    pcm.notify_result_cb = lambda cid, title, snippet: calls.append((cid, title, snippet))

    await pcm._delayed_push(chat.chat_id, "Live chat", "reply")
    assert calls == [(chat.chat_id, "Live chat", "reply")]


async def test_delayed_push_suppressed_for_memory_pass_and_project_fallback(
    tmp_path: Path,
) -> None:
    """A push already in the delay window is still silent for a pass."""
    pcm = _make_manager(tmp_path)
    pcm._push_delay_seconds = 0
    declared = _pass(pcm, "Memory pass · Pricing rework")
    fallback = _failed_closed_pass(pcm, "Memory pass · Deck figures")
    for chat in (declared, fallback):
        chat.last_activity_at = "2026-07-14T09:00:00Z"
        chat.last_read_at = ""
        chat.archived = False

    calls: list[tuple[str, str, str]] = []
    pcm.notify_result_cb = lambda cid, title, snippet: calls.append((cid, title, snippet))

    await pcm._delayed_push(declared.chat_id, "t", "s")
    await pcm._delayed_push(fallback.chat_id, "t", "s")

    assert calls == []


def test_notify_permission_silent_for_memory_pass_but_persists_state(
    tmp_path: Path,
) -> None:
    """A blocked pass keeps the pending card and lifecycle, but never pushes."""
    pcm = _make_manager(tmp_path)
    declared = _pass(pcm, "Memory pass · Pricing rework")
    fallback = _failed_closed_pass(pcm, "Memory pass · Deck figures")
    ordinary_project = pcm.create_project("General", workspace="personal")
    ordinary = pcm.create_chat(ordinary_project.project_id, title="Live chat")

    calls: list[tuple[str, str, str, str]] = []
    pcm.notify_permission_cb = (
        lambda cid, tool, message, request: calls.append((cid, tool, message, request))
    )
    event = PermissionRequestEvent(
        type="permission_request",
        tool_name="Bash",
        tool_input='{"command": "rm -rf /"}',
        message="Approve use of Bash?",
        request_id="req-42",
    )

    pcm._notify_permission(declared.chat_id, event)
    pcm._notify_permission(fallback.chat_id, event)

    assert calls == []
    # The pass stays blocked on the owner: the card and its state are still there.
    assert json.loads(declared.pending_permission)["request_id"] == "req-42"
    assert json.loads(fallback.pending_permission)["request_id"] == "req-42"

    pcm._notify_permission(ordinary.chat_id, event)
    assert calls == [(ordinary.chat_id, "Bash", "Approve use of Bash?", "req-42")]


def test_notify_question_silent_for_memory_pass(tmp_path: Path) -> None:
    """A pass asking a question is still visible in its memory-insight row, not a push."""
    pcm = _make_manager(tmp_path)
    declared = _pass(pcm, "Memory pass · Pricing rework")
    fallback = _failed_closed_pass(pcm, "Memory pass · Deck figures")
    ordinary_project = pcm.create_project("General", workspace="personal")
    ordinary = pcm.create_chat(ordinary_project.project_id, title="Live chat")

    calls: list[tuple[str, str]] = []
    pcm.notify_question_cb = lambda cid, body: calls.append((cid, body))
    payload = json.dumps({"questions": [{"question": "Which project?"}]})

    pcm._notify_question(declared.chat_id, payload)
    pcm._notify_question(fallback.chat_id, payload)

    assert calls == []

    pcm._notify_question(ordinary.chat_id, payload)
    assert calls == [(ordinary.chat_id, "Which project?")]


async def test_permission_push_is_resent_only_while_the_request_is_pending() -> None:
    """One re-send during the first wait, while the request is still open; the user answers
    during the second wait, so no further re-send goes out."""
    from types import SimpleNamespace

    from ciao.main import PERMISSION_REFIRE_DELAY_S, _refire_permission_push

    pending = {"req-1"}
    provider = SimpleNamespace(has_permission_request=lambda rid, session_id="": rid in pending)
    providers = {"chat-1": SimpleNamespace(provider=provider)}
    sent: list[dict] = []
    waits: list[float] = []

    async def answer_on_second_wait(delay: float) -> None:
        waits.append(delay)
        if len(waits) == 2:
            pending.discard("req-1")

    payload = {"title": "Ciaobot needs approval", "chat_id": "chat-1"}
    await _refire_permission_push(
        payload,
        chat_id="chat-1",
        request_id="req-1",
        providers=providers,
        focused_chats={},
        dispatch=sent.append,
        sleep=answer_on_second_wait,
    )

    assert sent == [payload]
    assert waits == [PERMISSION_REFIRE_DELAY_S, PERMISSION_REFIRE_DELAY_S]
