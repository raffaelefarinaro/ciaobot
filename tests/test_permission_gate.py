"""PermissionGate: routes SDK ``can_use_tool`` callbacks through the PWA.

The gate is invoked by the Claude Agent SDK whenever Auto mode needs a manual
approval (the classifier blocked something and the model kept trying). Each
call becomes a ``PermissionRequestEvent`` published into the active chat
stream; the client replies with ``permission_response`` which ``answer()``
converts into an ``allow`` or ``deny`` result.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from ciao.models import PermissionRequestEvent
from ciao.providers.permission_gate import PermissionGate


def _ctx(tool_use_id: str | None = "tu-1") -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None,
        suggestions=[],
        tool_use_id=tool_use_id,
        agent_id=None,
    )


@pytest.mark.asyncio
async def test_allow_returns_permission_result_allow() -> None:
    published: list[PermissionRequestEvent] = []
    gate = PermissionGate(emit=published.append)

    task = asyncio.create_task(
        gate.handle("Bash", {"command": "ls"}, _ctx("tu-1"))
    )
    # Let the gate publish and suspend before we answer
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(published) == 1
    event = published[0]
    assert event.tool_name == "Bash"
    assert event.request_id == "tu-1"
    assert "command" in event.tool_input

    assert gate.answer("tu-1", approved=True) is True
    result = await task

    assert isinstance(result, PermissionResultAllow)
    # Future cleaned up after answering
    assert gate.pending_count == 0


@pytest.mark.asyncio
async def test_deny_returns_permission_result_deny_with_reason() -> None:
    gate = PermissionGate(emit=lambda _ev: None)

    task = asyncio.create_task(
        gate.handle("Write", {"file_path": "/etc/passwd"}, _ctx("tu-2"))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert gate.answer("tu-2", approved=False, reason="touch nothing in /etc") is True

    result = await task
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "touch nothing in /etc"


@pytest.mark.asyncio
async def test_answer_unknown_request_id_returns_false() -> None:
    gate = PermissionGate(emit=lambda _ev: None)
    assert gate.answer("does-not-exist", approved=True) is False


@pytest.mark.asyncio
async def test_missing_tool_use_id_falls_back_to_generated_id() -> None:
    published: list[PermissionRequestEvent] = []
    gate = PermissionGate(emit=published.append)

    task = asyncio.create_task(
        gate.handle("Edit", {"file_path": "x"}, _ctx(tool_use_id=None))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    event = published[0]
    assert event.request_id  # non-empty fallback id
    assert gate.answer(event.request_id, approved=True) is True
    await task


@pytest.mark.asyncio
async def test_cancel_cleans_up_pending() -> None:
    """If the caller cancels (e.g. client disconnects, turn is stopped), the
    gate must drop its future so stale answers can't land on a new prompt with
    the same id."""
    gate = PermissionGate(emit=lambda _ev: None)
    task = asyncio.create_task(
        gate.handle("Bash", {"command": "ls"}, _ctx("tu-3"))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert gate.pending_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.pending_count == 0
    # A late "answer" for this id must not blow up, just no-op.
    assert gate.answer("tu-3", approved=True) is False


@pytest.mark.asyncio
async def test_set_emit_rebinds_publisher_mid_lifetime() -> None:
    """The provider creates one gate and rebinds ``emit`` to each turn's
    event queue. Old turns must not receive events from new ones."""
    first: list[PermissionRequestEvent] = []
    second: list[PermissionRequestEvent] = []
    gate = PermissionGate(emit=first.append)

    t1 = asyncio.create_task(gate.handle("Bash", {}, _ctx("r1")))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(first) == 1 and len(second) == 0

    gate.set_emit(second.append)
    t2 = asyncio.create_task(gate.handle("Bash", {}, _ctx("r2")))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(first) == 1  # stale
    assert len(second) == 1

    gate.cancel_all("shutdown")
    await t1
    await t2


@pytest.mark.asyncio
async def test_cancel_all_pending_denies_and_clears() -> None:
    """When a turn ends (user stop, error), we must not leave futures hanging;
    ``cancel_all`` resolves them as deny so ``handle`` returns cleanly."""
    gate = PermissionGate(emit=lambda _ev: None)

    t1 = asyncio.create_task(gate.handle("Bash", {"command": "ls"}, _ctx("a")))
    t2 = asyncio.create_task(gate.handle("Bash", {"command": "pwd"}, _ctx("b")))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert gate.pending_count == 2

    gate.cancel_all("turn aborted")

    r1 = await t1
    r2 = await t2
    assert isinstance(r1, PermissionResultDeny)
    assert isinstance(r2, PermissionResultDeny)
    assert r1.message == "turn aborted"
    assert gate.pending_count == 0
