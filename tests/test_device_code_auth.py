"""The device_code needs-you card: gh and az sign-in as a helper terminal for the chat."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from ciao.chat_actions import (
    ChatAction,
    ChatActionError,
    action_key,
    check_agent_params,
    validate_params,
)
from ciao.terminal_sessions import TerminalSessionError
from ciao.web import device_code_auth, mcp_auth_claude
from ciao.web.chat_actions_base import ActionContext
from ciao.web.chat_actions_runtime import ACTION_HANDLERS
from ciao.web.device_code_auth import (
    CAPACITY,
    CLOSED,
    NO_TERMINAL,
    DeviceCodeFlow,
)
from tests.test_agent_surface import _client as _agent_client
from tests.test_agent_surface import _offer_chat, _offer_service, _offer_token, _post
from tests.test_mcp_auth_claude import FakePcm, FakeTerminals, _DismissedPcm

BIN = Path("/opt/homebrew/bin")
GH_ARGV = [
    "auth",
    "login",
    "--web",
    "--hostname",
    "github.com",
    "--git-protocol",
    "https",
]
# What `gh auth login --web` writes to its PTY on gh 2.103 (ANSI included): the code, then a wait for Enter.
GH_OUTPUT = (
    b"\x1b[0;33m!\x1b[0m Did you know that expiring tokens can be requested with \x1b[0;1;39m--expiring\x1b[0m?"
    b" Read more at https://gh.io/gh-expiring-tokens\r\n\r\n"
    b"\x1b[0;33m!\x1b[0m One-time code (\x1b[0;1;39mC891-07AA\x1b[0m) copied to clipboard\r\n"
    b"\x1b[0;1;39mPress Enter\x1b[0m to open https://github.com/login/device in your browser... "
)
AZ_OUTPUT = (
    b"To sign in, use a web browser to open the page https://microsoft.com/devicelogin "
    b"and enter the code FGH7JKL2M to authenticate.\r\n"
)


class FakeDeviceTerminals(FakeTerminals):
    """C2's fake plus the input write the gh flow needs."""

    def __init__(self, *, input_error: str | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.input_error = input_error
        self.inputs: list[bytes] = []

    def helper_input(self, session_id: str, data: bytes) -> int:
        if self.input_error is not None:
            raise TerminalSessionError(cast(Any, self.input_error))
        self.inputs.append(data)
        return len(data)


def _action(tool: str = "gh", state: str = "open", **extra: str) -> ChatAction:
    return ChatAction(
        id="act_1",
        kind="device_code",
        origin="agent",
        state=cast(Any, state),
        params=MappingProxyType({"tool": tool, **extra}),
    )


def _ctx(pcm: Any, terminals: Any, agent_root: Path | None) -> ActionContext:
    return ActionContext(
        pcm=cast(Any, pcm),
        chat=cast(Any, SimpleNamespace(chat_id="c1", archived=False)),
        provider=None,
        is_loopback=True,
        terminals=cast(Any, terminals),
        agent_root=agent_root,
    )


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        device_code_auth, "resolve_command", lambda name: [str(BIN / name)]
    )


@pytest.mark.asyncio
async def test_gh_start_runs_the_fixed_argv(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals()
    pcm = FakePcm(_action("gh"))

    await DeviceCodeFlow().start(_ctx(pcm, terminals, tmp_path), pcm.action, {})

    assert terminals.created == [
        (tmp_path, "chat", "c1", [str(BIN / "gh"), *GH_ARGV])
    ]
    assert pcm.last == (
        "working",
        {"session_id": "sess_1", "code": "", "url": "", "entered": ""},
        "",
    )


@pytest.mark.asyncio
async def test_az_start_runs_the_fixed_argv(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals()
    pcm = FakePcm(_action("az"))

    await DeviceCodeFlow().start(_ctx(pcm, terminals, tmp_path), pcm.action, {})

    assert terminals.created == [
        (tmp_path, "chat", "c1", [str(BIN / "az"), "login", "--use-device-code"])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["aws", "gh; rm -rf ~", ""])
async def test_unknown_tool_never_spawns(tmp_path: Path, tool: str) -> None:
    terminals = FakeDeviceTerminals()
    pcm = FakePcm(_action(tool))

    await DeviceCodeFlow().start(_ctx(pcm, terminals, tmp_path), pcm.action, {})

    assert pcm.last[0] == "failed"
    assert pcm.last[2] == "That tool has no sign-in card."
    assert terminals.created == []


@pytest.mark.asyncio
async def test_missing_tool_reports_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(device_code_auth, "resolve_command", lambda name: [])
    terminals = FakeDeviceTerminals()
    pcm = FakePcm(_action("gh"))

    await DeviceCodeFlow().start(_ctx(pcm, terminals, tmp_path), pcm.action, {})

    assert pcm.last[0] == "failed"
    assert pcm.last[2] == "gh is not installed on this computer. Ask the agent for the install command."
    assert terminals.created == []


@pytest.mark.asyncio
async def test_unresolvable_tool_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(name: str) -> list[str]:
        raise OSError("x")

    monkeypatch.setattr(device_code_auth, "resolve_command", broken)
    terminals = FakeDeviceTerminals()
    pcm = FakePcm(_action("gh"))

    await DeviceCodeFlow().start(_ctx(pcm, terminals, tmp_path), pcm.action, {})

    assert pcm.last[2] == device_code_auth.CANNOT_RUN.format(tool="gh")
    assert terminals.created == []


@pytest.mark.asyncio
async def test_unrelated_interpreter_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        device_code_auth, "resolve_command", lambda name: [str(Path("C:/Python/python.exe"))]
    )
    terminals = FakeDeviceTerminals()
    pcm = FakePcm(_action("az"))

    await DeviceCodeFlow().start(_ctx(pcm, terminals, tmp_path), pcm.action, {})

    assert pcm.last[2] == device_code_auth.CANNOT_RUN.format(tool="az")
    assert terminals.created == []


@pytest.mark.asyncio
async def test_node_wrapped_script_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = [str(Path("C:/node.exe")), str(Path("C:/npm/gh.js"))]
    monkeypatch.setattr(device_code_auth, "resolve_command", lambda name: list(prefix))
    terminals = FakeDeviceTerminals()
    pcm = FakePcm(_action("gh"))

    await DeviceCodeFlow().start(_ctx(pcm, terminals, tmp_path), pcm.action, {})

    assert terminals.created[0][3][:2] == prefix


@pytest.mark.asyncio
async def test_no_terminal_manager_or_folder_fails(tmp_path: Path, resolved: None) -> None:
    pcm = FakePcm(_action("gh"))
    await DeviceCodeFlow().start(_ctx(pcm, None, tmp_path), pcm.action, {})
    assert pcm.last[2] == NO_TERMINAL

    pcm2 = FakePcm(_action("gh"))
    await DeviceCodeFlow().start(_ctx(pcm2, FakeDeviceTerminals(), None), pcm2.action, {})
    assert pcm2.last[2] == NO_TERMINAL


@pytest.mark.asyncio
async def test_capacity_maps_to_the_c2_copy(tmp_path: Path, resolved: None) -> None:
    pcm = FakePcm(_action("gh"))
    await DeviceCodeFlow().start(
        _ctx(pcm, FakeDeviceTerminals(create_error="capacity"), tmp_path), pcm.action, {}
    )
    assert pcm.last[2] == mcp_auth_claude.CAPACITY

    pcm2 = FakePcm(_action("gh"))
    await DeviceCodeFlow().start(
        _ctx(pcm2, FakeDeviceTerminals(create_error="spawn_failed"), tmp_path), pcm2.action, {}
    )
    assert pcm2.last[2] == NO_TERMINAL


@pytest.mark.asyncio
async def test_start_ends_the_helper_when_the_action_was_dismissed_meanwhile(
    tmp_path: Path, resolved: None
) -> None:
    terminals = FakeDeviceTerminals()
    pcm = _DismissedPcm(_action("gh"))

    await DeviceCodeFlow().start(_ctx(pcm, terminals, tmp_path), pcm.action, {})

    assert terminals.ended == ["sess_1"]
    assert pcm.updates == []


@pytest.mark.asyncio
async def test_restart_after_failure_clears_the_old_code(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals(output=GH_OUTPUT)
    pcm = FakePcm(
        _action(
            "gh",
            state="failed",
            session_id="old",
            code="ABCD-1234",
            url="https://github.com/login/device",
            entered="1",
        )
    )

    await DeviceCodeFlow().start(_ctx(pcm, terminals, tmp_path), pcm.action, {})
    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)

    assert pcm.last == (
        "waiting",
        {"code": "C891-07AA", "url": "https://github.com/login/device", "entered": "1"},
        "",
    )
    assert terminals.inputs == [b"\r"]


@pytest.mark.asyncio
async def test_gh_code_scraped_from_real_output_and_enter_pressed(
    tmp_path: Path, resolved: None
) -> None:
    terminals = FakeDeviceTerminals(output=GH_OUTPUT)
    pcm = FakePcm(_action("gh", state="working", session_id="sess_1"))

    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)

    assert pcm.last == (
        "waiting",
        {"code": "C891-07AA", "url": "https://github.com/login/device", "entered": "1"},
        "",
    )
    assert terminals.inputs == [b"\r"]

    updates_before = len(pcm.updates)
    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)
    assert terminals.inputs == [b"\r"]
    assert len(pcm.updates) == updates_before


@pytest.mark.asyncio
async def test_gh_enter_failure_is_not_marked_entered(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals(output=GH_OUTPUT, input_error="closed")
    pcm = FakePcm(_action("gh", state="working", session_id="sess_1"))

    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)

    state, params_update, _error = pcm.last
    assert state == "waiting"
    assert params_update["code"] == "C891-07AA"
    assert "entered" not in params_update


@pytest.mark.asyncio
async def test_gh_old_wording_still_scrapes(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals(output=b"! First copy your one-time code: ABCD-1234\r\n")
    pcm = FakePcm(_action("gh", state="working", session_id="sess_1"))

    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)

    assert pcm.last[1]["code"] == "ABCD-1234"


@pytest.mark.asyncio
async def test_az_code_and_allowlisted_url(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals(output=AZ_OUTPUT)
    pcm = FakePcm(_action("az", state="working", session_id="sess_1"))

    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)

    assert pcm.last == (
        "waiting",
        {"code": "FGH7JKL2M", "url": "https://microsoft.com/devicelogin"},
        "",
    )
    assert terminals.inputs == []


@pytest.mark.asyncio
async def test_az_unlisted_url_falls_back_to_the_default(tmp_path: Path, resolved: None) -> None:
    output = AZ_OUTPUT.replace(b"https://microsoft.com/devicelogin", b"https://evil.example/devicelogin")
    terminals = FakeDeviceTerminals(output=output)
    pcm = FakePcm(_action("az", state="working", session_id="sess_1"))

    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)

    assert pcm.last[1]["url"] == "https://microsoft.com/devicelogin"


@pytest.mark.asyncio
async def test_exit_zero_is_done_and_ends_the_session(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals(exited=True, exit_code=0, output=GH_OUTPUT)
    pcm = FakePcm(_action("gh", state="waiting", session_id="sess_1", code="C891-07AA"))

    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)

    assert pcm.last[0] == "done"
    assert terminals.ended == ["sess_1"]


@pytest.mark.asyncio
async def test_exit_nonzero_fails_with_the_error_line(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals(
        exited=True, exit_code=1, output=b"error: authentication failed\r\n"
    )
    pcm = FakePcm(_action("gh", state="waiting", session_id="sess_1", code="C891-07AA"))

    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)

    assert pcm.last == ("failed", {}, "error: authentication failed")
    assert terminals.ended == ["sess_1"]


@pytest.mark.asyncio
async def test_exit_nonzero_without_an_error_line(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals(exited=True, exit_code=2, output=b"bye\r\n")
    pcm = FakePcm(_action("gh", state="waiting", session_id="sess_1", code="C891-07AA"))

    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)

    assert pcm.last[2] == "gh sign-in ended with exit code 2."


@pytest.mark.asyncio
async def test_poll_on_a_closed_helper_fails(tmp_path: Path, resolved: None) -> None:
    terminals = FakeDeviceTerminals(missing=True)
    pcm = FakePcm(_action("gh", state="working", session_id="sess_1"))
    await DeviceCodeFlow().poll(_ctx(pcm, terminals, tmp_path), pcm.action)
    assert pcm.last[0] == "failed"
    assert pcm.last[2] == CLOSED

    pcm2 = FakePcm(_action("gh", state="working", session_id="sess_1"))
    await DeviceCodeFlow().poll(_ctx(pcm2, None, tmp_path), pcm2.action)
    assert pcm2.last[2] == CLOSED


@pytest.mark.asyncio
async def test_cancel_ends_the_helper_and_swallows_a_gone_session(tmp_path: Path) -> None:
    terminals = FakeDeviceTerminals()
    pcm = FakePcm(_action("gh", state="working", session_id="sess_1"))
    await DeviceCodeFlow().cancel(_ctx(pcm, terminals, tmp_path), pcm.action)
    assert terminals.ended == ["sess_1"]

    gone = FakeDeviceTerminals(end_error="not_found")
    await DeviceCodeFlow().cancel(_ctx(pcm, gone, tmp_path), pcm.action)
    assert gone.ended == []

    bare = FakeDeviceTerminals()
    await DeviceCodeFlow().cancel(_ctx(pcm, bare, tmp_path), _action("gh"))
    assert bare.ended == []


def test_registered_in_the_runtime() -> None:
    assert isinstance(ACTION_HANDLERS["device_code"], DeviceCodeFlow)
    assert DeviceCodeFlow.poll_states == frozenset({"working", "waiting"})


def test_wake_prompt_names_the_tool_and_not_the_code() -> None:
    action = _action("gh", code="C891-07AA", url="https://github.com/login/device")
    prompt = DeviceCodeFlow().wake_prompt(action)
    assert "gh" in prompt
    assert "C891-07AA" not in prompt
    assert "github.com" not in prompt


def test_validator_accepts_a_bare_tool_and_trims_the_note() -> None:
    assert validate_params("device_code", {"tool": "gh"}) == {"tool": "gh"}
    assert validate_params("device_code", {"tool": "gh", "note": " hi "}) == {
        "tool": "gh",
        "note": "hi",
    }


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ({"tool": "aws"}, "invalid_param"),
        ({"tool": "gh "}, "invalid_param"),
        ({"tool": 3}, "invalid_type"),
        ({}, "missing_param"),
        ({"tool": "gh", "x": "1"}, "unknown_param"),
        ({"tool": "gh", "code": "x"}, "invalid_param"),
        ({"tool": "gh", "url": "http://github.com/login/device"}, "invalid_url"),
        ({"tool": "gh", "entered": "2"}, "invalid_param"),
        ({"tool": "gh", "session_id": "a b"}, "invalid_param"),
    ],
)
def test_validator_refuses_bad_params(raw: dict[str, object], reason: str) -> None:
    with pytest.raises(ChatActionError) as exc:
        validate_params("device_code", raw)
    assert exc.value.reason == reason


def test_validator_drops_empty_engine_keys() -> None:
    assert validate_params(
        "device_code",
        {"tool": "gh", "code": "", "url": "", "entered": "", "session_id": "s1"},
    ) == {"tool": "gh", "session_id": "s1"}


def test_agent_may_not_raise_engine_keys() -> None:
    with pytest.raises(ChatActionError) as exc:
        check_agent_params("device_code", {"tool": "gh", "code": "ABCD-1234"})
    assert exc.value.reason == "unknown_param"


def test_dedupe_key_is_the_kind_and_tool() -> None:
    assert action_key("device_code", {"tool": "az"}) == "device_code:az"


def test_agent_raise_device_code(tmp_path: Path) -> None:
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)
    with _agent_client(service) as client:
        ok = _post(client, token, "chat_needs_action", {"kind": "device_code", "params": {"tool": "gh"}})
        aws = _post(client, token, "chat_needs_action", {"kind": "device_code", "params": {"tool": "aws"}})
        smuggled = _post(
            client,
            token,
            "chat_needs_action",
            {"kind": "device_code", "params": {"tool": "gh", "code": "ABCD-1234"}},
        )

    assert ok.status_code == 200
    action = ok.json()["data"]["action"]
    assert action["kind"] == "device_code"
    assert action["origin"] == "agent"
    assert action["params"] == {"tool": "gh"}
    assert len(manager.get_chat(chat.chat_id).actions) == 1
    assert aws.status_code == 422
    assert aws.json()["error"]["code"] == "invalid_action"
    assert aws.json()["error"]["message"] == "The request was refused (invalid_param)."
    assert smuggled.status_code == 422
    assert "(unknown_param)" in smuggled.json()["error"]["message"]
    assert len(manager.get_chat(chat.chat_id).actions) == 1
