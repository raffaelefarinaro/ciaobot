"""ACP permission broker and mode-value selection (#3137)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ciao.models import PermissionRequestEvent, PermissionResolvedEvent, StreamEvent, SystemStatusEvent
from ciao.providers.acp.permissions import PermissionBroker, mode_choices, pick_value
from ciao.providers.acp.spec import AgentSpec, ModeChoice
from ciao.providers.base import QuestionResponseResult
from ciao.web.chat_broker import ChatStream, event_to_json


def _option(option_id: str, kind: str, name: str | None = None) -> dict[str, str]:
    return {"optionId": option_id, "kind": kind, "name": name or option_id}


async def _settle(
    broker: PermissionBroker,
    tool_call_id: str,
    *,
    exclude: str = "",
    session_id: str = "sess",
) -> str:
    for _ in range(20):
        request_id = broker._tool_call_pending.get((session_id, tool_call_id), "")
        if request_id and request_id != exclude and broker.has(request_id):
            return request_id
        await asyncio.sleep(0)
    raise AssertionError(f"{tool_call_id} was never stored")


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
    request_id = await _settle(broker, "call-1")
    assert broker.respond(request_id, True, session_id="sess") == QuestionResponseResult(ok=True)
    assert await task == {"outcome": "selected", "optionId": "once"}
    assert len(emitted) == 1
    event = emitted[0]
    assert isinstance(event, PermissionRequestEvent)
    assert event.message == "Bash"
    assert event.tool_name == "Bash"
    assert event.tool_input == "ls"
    assert event.request_id == request_id
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
            {"outcome": "cancelled"},
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
async def test_deny_selects_reject_once_or_cancelled(
    options: list[dict[str, str]],
    expected: dict[str, str],
) -> None:
    broker = PermissionBroker(lambda _event: None)
    task = asyncio.create_task(broker.request("sess", {"toolCallId": "call-1", "title": "Tool"}, options))
    request_id = await _settle(broker, "call-1")
    assert broker.respond(request_id, False, session_id="sess").ok
    assert await task == expected


@pytest.mark.asyncio
async def test_attended_deny_cancels_when_reject_once_id_is_ambiguous() -> None:
    broker = PermissionBroker(lambda _event: None)
    task = asyncio.create_task(broker.request("sess", {"toolCallId": "deny-ambiguous"}, [
        _option("allow", "allow_once"),
        _option("same", "reject_once"),
        _option("same", "reject_always"),
    ]))
    request_id = await _settle(broker, "deny-ambiguous")
    assert broker.respond(request_id, False, session_id="sess").ok
    assert await task == {"outcome": "cancelled"}


@pytest.mark.asyncio
async def test_unattended_deny_cancels_when_reject_once_id_is_ambiguous() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    broker.unattended = True
    outcome = await broker.request("sess", {"toolCallId": "deny-ambiguous"}, [
        _option("same", "reject_once"),
        _option("same", "reject_always"),
    ])
    assert outcome == {"outcome": "cancelled"}
    assert len(emitted) == 1 and isinstance(emitted[0], SystemStatusEvent)


@pytest.mark.asyncio
async def test_unattended_rejects_without_emitting_card() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    broker.unattended = True
    outcome = await broker.request(
        "sess",
        {"toolCallId": "call-1", "title": "Some tool"},
        [_option("allow", "allow_once"), _option("reject", "reject_once", "Reject")],
    )
    assert outcome == {"outcome": "selected", "optionId": "reject"}
    assert not any(isinstance(event, PermissionRequestEvent) for event in emitted)
    assert len(emitted) == 1
    status = emitted[0]
    assert isinstance(status, SystemStatusEvent)
    assert status.status == "Denied Some tool: scheduled runs cannot wait for approval."
    # Typed, so a schedule run can fail on it without parsing the sentence. No
    # kind was sent, so the generic name stands in for the title.
    assert status.denied_tool == "tool"
    assert not broker.has("call-1")


async def test_unattended_denial_names_the_tool_kind_not_the_raw_title() -> None:
    """Adapters often put the full shell command in the title. That text can carry
    tokens or newlines, so the run's error names only the tool kind."""
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    broker.unattended = True
    secret_title = "Bash: curl -H 'Authorization: Bearer sk-FAKE-123' https://x.example\nrm -rf /"
    await broker.request(
        "sess",
        {"toolCallId": "call-2", "title": secret_title, "kind": "execute", "rawInput": {"command": "echo valid"}},
        [_option("reject", "reject_once", "Reject")],
    )
    status = emitted[0]
    assert isinstance(status, SystemStatusEvent)
    assert status.denied_tool == "shell"
    assert "sk-FAKE-123" not in status.denied_tool and "\n" not in status.denied_tool
    assert "sk-FAKE-123" not in status.status and "echo valid" not in status.status


async def test_unattended_malformed_shell_is_denied_without_leaking_its_title() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    broker.unattended = True
    secret = "PRIVATE_COMMAND_IN_TITLE"
    outcome = await broker.request("sess", {
        "toolCallId": "missing-command", "kind": "execute", "title": secret, "rawInput": {},
    }, [_option("reject", "reject_once")])
    assert outcome == {"outcome": "selected", "optionId": "reject"}
    assert isinstance(emitted[0], SystemStatusEvent)
    assert secret not in emitted[0].status


@pytest.mark.asyncio
async def test_unattended_permission_never_selects_a_persistent_rejection() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    broker.unattended = True
    outcome = await broker.request(
        "sess", {"toolCallId": "call-persistent", "title": "Read"},
        [_option("never", "reject_always")],
    )
    assert outcome == {"outcome": "cancelled"}
    assert len(emitted) == 1
    assert isinstance(emitted[0], SystemStatusEvent)


async def test_unattended_denial_without_a_kind_names_a_generic_tool() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    broker.unattended = True
    await broker.request(
        "sess",
        {"toolCallId": "call-3", "title": "Some title"},
        [_option("reject", "reject_once", "Reject")],
    )
    status = emitted[0]
    assert isinstance(status, SystemStatusEvent)
    assert status.denied_tool == "tool"


def test_respond_unknown_id_is_acknowledged_as_already_resolved() -> None:
    broker = PermissionBroker(lambda _event: None)
    result = broker.respond("missing", True)
    assert result == QuestionResponseResult(ok=True, already_resolved=True)


@pytest.mark.asyncio
async def test_respond_wrong_session_is_expired() -> None:
    broker = PermissionBroker(lambda _event: None)
    task = asyncio.create_task(broker.request("sess", {"toolCallId": "call-1", "title": "Tool"}, [
        _option("once", "allow_once"),
    ]))
    request_id = await _settle(broker, "call-1")
    assert broker.session_of(request_id) == "sess"
    result = broker.respond(request_id, True, session_id="other")
    assert result == QuestionResponseResult(
        ok=False,
        error="This approval belongs to an older session.",
        retryable=False,
        expired=True,
    )
    assert broker.has(request_id, "sess")
    assert not task.done()
    assert broker.respond(request_id, True, session_id="sess").ok
    assert await task == {"outcome": "selected", "optionId": "once"}


@pytest.mark.asyncio
async def test_duplicate_request_id_cancels_the_older_future() -> None:
    broker = PermissionBroker(lambda _event: None)
    options = [_option("once", "allow_once")]
    first = asyncio.create_task(broker.request(
        "sess", {"toolCallId": "call-1", "title": "Tool"}, options,
    ))
    first_id = await _settle(broker, "call-1")
    second = asyncio.create_task(broker.request(
        "sess", {"toolCallId": "call-1", "title": "Read"}, options,
    ))
    second_id = await _settle(broker, "call-1", exclude=first_id)
    assert await asyncio.wait_for(first, timeout=1) == {"outcome": "cancelled"}
    assert first_id != second_id
    assert broker.respond(first_id, True, session_id="sess").already_resolved
    assert broker.has(second_id, "sess")
    assert not second.done()
    assert broker.respond(second_id, True, session_id="sess").ok
    assert await second == {"outcome": "selected", "optionId": "once"}


@pytest.mark.asyncio
async def test_reused_provider_id_never_lets_old_card_approve_new_command() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    options = [_option("once", "allow_once")]
    old = asyncio.create_task(broker.request("sess", {
        "toolCallId": "reused", "kind": "execute", "rawInput": {"command": "echo old"},
    }, options))
    old_id = await _settle(broker, "reused")
    new = asyncio.create_task(broker.request("sess", {
        "toolCallId": "reused", "kind": "execute", "rawInput": {"command": "echo new"},
    }, options))
    new_id = await _settle(broker, "reused", exclude=old_id)
    assert old_id != new_id
    assert await old == {"outcome": "cancelled"}
    assert broker.respond(old_id, True, session_id="sess").already_resolved
    assert not new.done()
    assert broker.respond(new_id, True, session_id="sess").ok
    assert await new == {"outcome": "selected", "optionId": "once"}
    assert isinstance(emitted[0], PermissionRequestEvent)
    assert emitted[0].request_id == old_id
    assert isinstance(emitted[1], PermissionResolvedEvent)
    assert emitted[1].request_id == old_id
    assert isinstance(emitted[2], PermissionRequestEvent)
    assert emitted[2].request_id == new_id


@pytest.mark.asyncio
async def test_refused_replacement_retires_old_card_before_returning() -> None:
    emitted: list[object] = []
    stream = ChatStream("prompt")

    def publish(event: StreamEvent) -> None:
        emitted.append(event)
        if isinstance(event, PermissionResolvedEvent):
            stream.resolve_permission(event.request_id, event.session_id)
            return
        payload = event_to_json(event)
        if payload is not None:
            stream.publish(payload)

    broker = PermissionBroker(publish)
    options = [_option("once", "allow_once")]
    old = asyncio.create_task(broker.request("sess", {
        "toolCallId": "reused", "kind": "execute", "rawInput": {"command": "echo old"},
    }, options))
    old_id = await _settle(broker, "reused")

    result = await broker.request("sess", {
        "toolCallId": "reused", "kind": "execute", "rawInput": {},
    }, options)

    assert result == {"outcome": "cancelled"}
    assert await old == {"outcome": "cancelled"}
    assert not broker.has(old_id, "sess")
    assert isinstance(emitted[-1], PermissionResolvedEvent)
    assert emitted[-1].request_id == old_id
    assert not any(event.get("type") == "permission_request" for event in stream.buffered_events())


@pytest.mark.asyncio
async def test_shell_cwd_is_carried_exactly_or_unknown_when_absent() -> None:
    for raw_input, expected in [
        ({"command": "echo ok", "cwd": "/workspace/project"}, "/workspace/project"),
        ({"command": "echo ok"}, ""),
    ]:
        emitted: list[object] = []
        broker = PermissionBroker(emitted.append)
        tool_call_id = f"cwd-{bool(expected)}"
        task = asyncio.create_task(broker.request("sess", {
            "toolCallId": tool_call_id, "kind": "execute", "rawInput": raw_input,
        }, [_option("once", "allow_once")]))
        request_id = await _settle(broker, tool_call_id)
        event = emitted[0]
        assert isinstance(event, PermissionRequestEvent)
        assert event.shell_cwd == expected
        broker.respond(request_id, True, session_id="sess")
        assert await task == {"outcome": "selected", "optionId": "once"}


@pytest.mark.parametrize("cwd", [None, 12, "", "  ", "/tmp\nsecret", "x" * 4097])
@pytest.mark.asyncio
async def test_malformed_shell_cwd_fails_closed(cwd: Any) -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    result = await broker.request("sess", {
        "toolCallId": "bad-cwd", "kind": "execute",
        "rawInput": {"command": "echo ok", "cwd": cwd},
    }, [_option("once", "allow_once")])
    assert result == {"outcome": "cancelled"}
    assert emitted == []


@pytest.mark.asyncio
async def test_same_provider_id_in_another_session_does_not_replace_the_first() -> None:
    broker = PermissionBroker(lambda _event: None)
    options = [_option("once", "allow_once")]
    first = asyncio.create_task(broker.request("session-a", {"toolCallId": "same"}, options))
    for _ in range(20):
        first_id = next(iter(broker._pending), "")
        if first_id:
            break
        await asyncio.sleep(0)
    second = asyncio.create_task(broker.request("session-b", {"toolCallId": "same"}, options))
    second_id = ""
    for _ in range(20):
        pending = list(broker._pending)
        if len(pending) == 2:
            second_id = next(request_id for request_id in pending if request_id != first_id)
            break
        await asyncio.sleep(0)
    assert broker.has(first_id, "session-a")
    assert broker.has(second_id, "session-b")
    assert not first.done()
    broker.respond(first_id, True, session_id="session-a")
    broker.respond(second_id, True, session_id="session-b")
    assert await first == {"outcome": "selected", "optionId": "once"}
    assert await second == {"outcome": "selected", "optionId": "once"}


@pytest.mark.asyncio
async def test_cancel_all_answers_every_pending_with_cancelled() -> None:
    broker = PermissionBroker(lambda _event: None)
    first = asyncio.create_task(broker.request("sess", {"toolCallId": "a", "title": "Read"}, [
        _option("once", "allow_once"),
    ]))
    second = asyncio.create_task(broker.request("sess", {"toolCallId": "b", "title": "Tool"}, [
        _option("once", "allow_once"),
    ]))
    first_id = await _settle(broker, "a")
    second_id = await _settle(broker, "b")
    broker.cancel_all()
    assert await first == {"outcome": "cancelled"}
    assert await second == {"outcome": "cancelled"}
    assert broker.respond(first_id, True).already_resolved
    assert not broker.has(second_id)


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
        ([{"optionId": None, "kind": "allow_once"}], True, {"outcome": "cancelled"}),
        ([{"optionId": "   ", "kind": "allow_once"}], True, {"outcome": "cancelled"}),
        ([{"optionId": 7, "kind": "allow_once"}], True, {"outcome": "cancelled"}),
    ],
)
async def test_option_without_a_usable_allow_once_never_opens_a_card(
    options: list[dict[str, Any]], approved: bool, expected: dict[str, Any]
) -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    assert await broker.request("sess", {"toolCallId": "call-1", "title": "Tool"}, options) == expected
    assert emitted == []
    assert not broker.has("call-1")

@pytest.mark.asyncio
async def test_shell_permission_emits_the_exact_bounded_command() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    command = 'printf "{\\"x\\":1}"\n' + "# " + "x" * 300 + " tail-unique"
    task = asyncio.create_task(broker.request(
        "sess", {"toolCallId": "shell-1", "kind": "execute", "title": "Untrusted title", "rawInput": {"command": command}},
        [_option("once", "allow_once"), _option("always", "allow_always")],
    ))
    request_id = await _settle(broker, "shell-1")
    event = emitted[0]
    assert isinstance(event, PermissionRequestEvent)
    assert event.tool_input == command
    assert broker.respond(request_id, True, session_id="sess").ok
    assert await task == {"outcome": "selected", "optionId": "once"}


@pytest.mark.asyncio
async def test_shell_command_at_the_size_limit_is_accepted_exactly() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    command = "x" * 4096
    task = asyncio.create_task(broker.request(
        "sess", {"toolCallId": "shell-boundary", "name": "shell", "rawInput": {"command": command}},
        [_option("once", "allow_once")],
    ))
    request_id = await _settle(broker, "shell-boundary")
    event = emitted[0]
    assert isinstance(event, PermissionRequestEvent)
    assert event.tool_input == command
    assert broker.respond(request_id, True, session_id="sess").ok
    assert await task == {"outcome": "selected", "optionId": "once"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_call", [
    {"kind": "execute", "rawInput": {"command": "  \n"}},
    {"kind": "execute", "rawInput": {}},
    {"name": "shell", "rawInput": {"command": "x" * 4097}},
    {"kind": "execute", "rawInput": {"command": "echo\u202ehello"}},
    {"kind": "execute", "rawInput": {"command": "echo\tsecret"}},
    {"kind": "execute", "rawInput": {"command": 42}},
])
async def test_invalid_shell_request_is_refused_without_emitting_command(tool_call: dict[str, Any]) -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    outcome = await broker.request("sess", {"toolCallId": "unsafe", "title": "secret command", **tool_call},
                                   [_option("once", "allow_once")])
    assert outcome == {"outcome": "cancelled"}
    assert emitted == []
    assert not broker.has("unsafe")


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    [_option("always", "allow_always")],
    [_option("same", "allow_once"), _option("same", "allow_always")],
    [_option("once", "allow_once"), _option("once", "reject_once")],
])
async def test_shell_request_requires_unambiguous_allow_once(options: list[dict[str, str]]) -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    outcome = await broker.request("sess", {"toolCallId": "unsafe", "name": "shell", "rawInput": {"command": "echo ok"}}, options)
    assert outcome == {"outcome": "cancelled"}
    assert emitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_call", [
    {"title": "Terminal", "rawInput": {"command": "echo title"}},
    {"title": "Bash: echo untrusted title", "rawInput": {"command": "echo actual request"}},
    {"_meta": {"claudeCode": {"toolName": "Bash"}}, "rawInput": {"command": "echo metadata"}},
])
async def test_shell_indicators_in_title_and_claude_meta_require_and_show_current_command(
    tool_call: dict[str, Any],
) -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    command = tool_call["rawInput"]["command"]
    task = asyncio.create_task(broker.request("sess", {"toolCallId": "shell-indicator", **tool_call}, [
        _option("once", "allow_once"),
    ]))
    request_id = await _settle(broker, "shell-indicator")
    event = emitted[0]
    assert isinstance(event, PermissionRequestEvent)
    assert event.shell_command is True
    assert event.tool_input == command
    broker.respond(request_id, True, session_id="sess")
    assert await task == {"outcome": "selected", "optionId": "once"}


@pytest.mark.asyncio
async def test_shell_title_without_a_current_command_is_refused() -> None:
    emitted: list[object] = []
    broker = PermissionBroker(emitted.append)
    outcome = await broker.request("sess", {
        "toolCallId": "missing-current-command", "title": "Terminal", "rawInput": {},
    }, [_option("once", "allow_once")])
    assert outcome == {"outcome": "cancelled"}
    assert emitted == []


@pytest.mark.asyncio
async def test_shell_command_replays_from_stream_then_is_removed_after_approval() -> None:
    stream = ChatStream("prompt")
    command = 'printf "{\\"x\\":1}"\n' + "# " + "x" * 300 + " unique-tail"

    def publish(event: StreamEvent) -> None:
        payload = event_to_json(event)
        if payload is not None:
            stream.publish(payload)

    broker = PermissionBroker(publish)
    task = asyncio.create_task(broker.request("session", {
        "toolCallId": "shell-replay",
        "kind": "execute",
        "title": "Terminal",
        "rawInput": {"command": command},
    }, [_option("once", "allow_once"), _option("reject", "reject_once")]))
    request_id = await _settle(broker, "shell-replay", session_id="session")
    replayed = stream.buffered_events()
    assert replayed[0]["tool_input"] == command
    assert replayed[0]["shell_command"] is True
    assert broker.respond(request_id, True, session_id="session").ok
    assert await task == {"outcome": "selected", "optionId": "once"}
    assert stream.resolve_permission(request_id, "session")
    assert not any(event.get("type") == "permission_request" for event in stream.buffered_events())


@pytest.mark.asyncio
async def test_shell_denial_never_selects_persistent_reject() -> None:
    broker = PermissionBroker(lambda _event: None)
    task = asyncio.create_task(broker.request("sess", {"toolCallId": "shell", "kind": "execute", "rawInput": {"command": "echo ok"}},
                                              [_option("once", "allow_once"), _option("never", "reject_always")]))
    request_id = await _settle(broker, "shell")
    assert broker.respond(request_id, False, session_id="sess").ok
    assert await task == {"outcome": "cancelled"}


@pytest.mark.asyncio
async def test_refused_same_id_request_cancels_the_existing_pending_request() -> None:
    broker = PermissionBroker(lambda _event: None)
    first = asyncio.create_task(broker.request("sess", {"toolCallId": "same", "kind": "execute", "rawInput": {"command": "echo first"}},
                                               [_option("once", "allow_once")]))
    first_id = await _settle(broker, "same")
    refused = await broker.request("sess", {"toolCallId": "same", "kind": "execute", "rawInput": {}},
                                   [_option("once", "allow_once")])
    assert refused == {"outcome": "cancelled"}
    assert await first == {"outcome": "cancelled"}
    assert broker.respond(first_id, True, session_id="sess").already_resolved
    assert not broker._tool_call_pending.get(("sess", "same"))
