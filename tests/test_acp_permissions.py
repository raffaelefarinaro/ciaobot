"""ACP permission broker and mode-value selection (#3137)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ciao.models import PermissionRequestEvent, SystemStatusEvent
from ciao.providers.acp.permissions import PermissionBroker, mode_choices, pick_value
from ciao.providers.acp.spec import AgentSpec, ModeChoice
from ciao.providers.base import QuestionResponseResult


def _option(option_id: str, kind: str, name: str | None = None) -> dict[str, str]:
    return {"optionId": option_id, "kind": kind, "name": name or option_id}


async def _settle(broker: PermissionBroker, request_id: str) -> None:
    for _ in range(20):
        if broker.has(request_id):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"{request_id} was never stored")


@pytest.mark.asyncio
async def test_approve_selects_allow_once_over_allow_always() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    options = [_option("always", "allow_always", "Always"), _option("once", "allow_once", "Once")]
    task = asyncio.create_task(broker.request(
        "sess",
        {"toolCallId": "call-1", "title": "Bash", "rawInput": {"command": "ls"}},
        options,
    ))
    await _settle(broker, "call-1")
    assert broker.respond("call-1", True, session_id="sess") == QuestionResponseResult(ok=True)
    assert await task == {"outcome": "selected", "optionId": "once"}
    assert len(emitted) == 1
    event = emitted[0]
    assert isinstance(event, PermissionRequestEvent)
    assert event.message == "Bash"
    assert event.tool_name == "Bash"
    assert event.tool_input == "ls"
    assert event.request_id == "call-1"
    assert event.session_id == "sess"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            [
                _option("allow", "allow_once"),
                _option("once", "reject_once", "Reject"),
                _option("never", "reject_always", "Never"),
            ],
            {"outcome": "selected", "optionId": "once"},
        ),
        (
            [_option("never", "reject_always", "Never"), _option("allow", "allow_once")],
            {"outcome": "selected", "optionId": "never"},
        ),
        (
            [_option("allow", "allow_once")],
            {"outcome": "cancelled"},
        ),
        (
            # opencode: once/allow_once, always/allow_always, reject/reject_once
            [
                _option("once", "allow_once", "Once"),
                _option("always", "allow_always", "Always"),
                _option("reject", "reject_once", "Reject"),
            ],
            {"outcome": "selected", "optionId": "reject"},
        ),
    ],
)
async def test_deny_selects_reject_once_then_reject_always_then_cancelled(
    options: list[dict[str, str]],
    expected: dict[str, str],
) -> None:
    broker = PermissionBroker(lambda _event: None)
    task = asyncio.create_task(broker.request("sess", {"toolCallId": "call-1", "title": "Bash"}, options))
    await _settle(broker, "call-1")
    assert broker.respond("call-1", False, session_id="sess").ok
    assert await task == expected


@pytest.mark.asyncio
async def test_unattended_rejects_without_emitting_card() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    broker.unattended = True
    outcome = await broker.request(
        "sess",
        {"toolCallId": "call-1", "title": "Bash"},
        [_option("allow", "allow_once"), _option("reject", "reject_once", "Reject")],
    )
    assert outcome == {"outcome": "selected", "optionId": "reject"}
    assert not any(isinstance(event, PermissionRequestEvent) for event in emitted)
    assert len(emitted) == 1
    status = emitted[0]
    assert isinstance(status, SystemStatusEvent)
    assert status.status == "Denied Bash: scheduled runs cannot wait for approval."
    # Typed, so a schedule run can fail on it without parsing the sentence.
    assert status.denied_tool == "Bash"
    assert not broker.has("call-1")


def test_respond_unknown_id_is_acknowledged_as_already_resolved() -> None:
    broker = PermissionBroker(lambda _event: None)
    result = broker.respond("missing", True)
    assert result == QuestionResponseResult(ok=True, already_resolved=True)


@pytest.mark.asyncio
async def test_respond_wrong_session_is_expired() -> None:
    broker = PermissionBroker(lambda _event: None)
    task = asyncio.create_task(broker.request("sess", {"toolCallId": "call-1", "title": "Bash"}, [
        _option("once", "allow_once"),
    ]))
    await _settle(broker, "call-1")
    assert broker.session_of("call-1") == "sess"
    result = broker.respond("call-1", True, session_id="other")
    assert result == QuestionResponseResult(
        ok=False,
        error="This approval belongs to an older session.",
        retryable=False,
        expired=True,
    )
    assert broker.has("call-1", "sess")
    assert not task.done()
    assert broker.respond("call-1", True, session_id="sess").ok
    assert await task == {"outcome": "selected", "optionId": "once"}


@pytest.mark.asyncio
async def test_duplicate_request_id_cancels_the_older_future() -> None:
    broker = PermissionBroker(lambda _event: None)
    options = [_option("once", "allow_once")]
    first = asyncio.create_task(broker.request(
        "sess", {"toolCallId": "call-1", "title": "Bash"}, options,
    ))
    await _settle(broker, "call-1")
    second = asyncio.create_task(broker.request(
        "sess", {"toolCallId": "call-1", "title": "Read"}, options,
    ))
    await asyncio.sleep(0)
    assert await asyncio.wait_for(first, timeout=1) == {"outcome": "cancelled"}
    assert broker.has("call-1")
    assert not second.done()
    broker.cancel_all()
    assert await second == {"outcome": "cancelled"}
    assert not broker.has("call-1")


@pytest.mark.asyncio
async def test_cancel_all_answers_every_pending_with_cancelled() -> None:
    broker = PermissionBroker(lambda _event: None)
    first = asyncio.create_task(broker.request("sess", {"toolCallId": "a", "title": "Read"}, [
        _option("once", "allow_once"),
    ]))
    second = asyncio.create_task(broker.request("sess", {"toolCallId": "b", "title": "Bash"}, [
        _option("once", "allow_once"),
    ]))
    await _settle(broker, "a")
    await _settle(broker, "b")
    broker.cancel_all()
    assert await first == {"outcome": "cancelled"}
    assert await second == {"outcome": "cancelled"}
    assert broker.respond("a", True).already_resolved
    assert not broker.has("b")


def test_mode_choice_falls_back_to_next_offered_value() -> None:
    choice = ModeChoice("mode", ("auto", "acceptEdits", "default"))
    # Preference order is the choice, so auto wins when the agent offers both.
    assert pick_value(choice, ["acceptEdits", "auto", "default"]) == "auto"
    # Claude does not offer auto, so the next preferred value is chosen.
    assert pick_value(choice, ["default", "acceptEdits", "plan"]) == "acceptEdits"
    assert pick_value(choice, ["plan"]) is None
    spec = AgentSpec(id="claude", binary="claude", modes={"auto": (choice,)})
    assert mode_choices(spec, "auto") == (choice,)
    assert mode_choices(spec, "plan") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "approved", "expected"),
    [
        # An allow option with no usable optionId is skipped, never answered with "None".
        ([{"kind": "allow_once", "name": "Once"}, _option("always", "allow_always")], True,
         {"outcome": "selected", "optionId": "always"}),
        ([{"optionId": None, "kind": "allow_once"}], True, {"outcome": "cancelled"}),
        ([{"optionId": "   ", "kind": "allow_once"}], True, {"outcome": "cancelled"}),
        ([{"optionId": 7, "kind": "allow_once"}], True, {"outcome": "cancelled"}),
        # Reject follows the same rule: a blank reject_once falls through to reject_always.
        ([{"optionId": "", "kind": "reject_once"}, _option("never", "reject_always")], False,
         {"outcome": "selected", "optionId": "never"}),
        ([{"optionId": "", "kind": "reject_once"}], False, {"outcome": "cancelled"}),
    ],
)
async def test_option_without_a_usable_id_is_never_selected(
    options: list[dict[str, Any]], approved: bool, expected: dict[str, Any]
) -> None:
    broker = PermissionBroker(lambda _event: None)
    task = asyncio.create_task(broker.request("sess", {"toolCallId": "call-1", "title": "Bash"}, options))
    await _settle(broker, "call-1")
    assert broker.respond("call-1", approved, session_id="sess").ok
    assert await task == expected
