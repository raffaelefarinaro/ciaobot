"""The Claude half of the mcp_auth card: start, poll, cancel and detection for a Claude chat."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from ciao.chat_actions import ChatAction, ChatActionError, check_agent_params, validate_params
from ciao.providers.claude import ClaudeProvider
from ciao.terminal_sessions import TerminalSessionError
from ciao.web import mcp_auth_claude
from ciao.web.chat_actions_base import ActionContext
from ciao.web.chat_actions_runtime import ACTION_HANDLERS, MCP_AUTH_FLOWS, McpAuthHandler
from ciao.web.mcp_auth_claude import (
    BAD_NAME,
    CAPACITY,
    CLAUDEAI_CONNECTOR,
    CLOSED,
    NOT_CONFIGURED,
    NOT_CONNECTED,
    NOT_RUNNING,
    ClaudeMcpAuthFlow,
)
from tests.test_chat_actions_runtime import make_manager, make_runtime

AUTH_URL = (
    "https://mcp.notion.com/authorize?response_type=code&client_id=x"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A5555%2Fcallback&state=abc"
)
# What `claude mcp login --no-browser notion` writes to its PTY: the URL is an OSC 8 link ended by BEL.
CLAUDE_OUTPUT = (
    b'Starting authentication for "notion"\xe2\x80\xa6\r\n'
    b"Visit this URL to authorize:\r\n"
    b"  \x1b]8;;" + AUTH_URL.encode() + b"\x07\x1b[94m" + AUTH_URL.encode() + b"\x1b[39m\x1b]8;;\x07\r\n"
    b"\r\nWaiting for authorization... (^C to cancel)\r\n"
)


class FakePcm:
    """Records every card update; holds one action."""

    def __init__(self, action: ChatAction) -> None:
        self.action = action
        self.updates: list[tuple[str, dict[str, object], str]] = []

    def update_action(
        self,
        chat_id: str,
        action_id: str,
        *,
        state: str,
        params_update: dict[str, object] | None = None,
        error: str = "",
    ) -> dict[str, object] | None:
        self.updates.append((state, dict(params_update or {}), error))
        params = {**self.action.params, **(params_update or {})}
        self.action = ChatAction(
            id=self.action.id,
            kind=self.action.kind,
            origin=self.action.origin,
            state=cast(Any, state),
            params=MappingProxyType(params),
            error=error,
        )
        return None

    def get_action(self, chat_id: str, action_id: str) -> ChatAction | None:
        return self.action

    @property
    def last(self) -> tuple[str, dict[str, object], str]:
        return self.updates[-1]


class FakeTerminals:
    """The helper-session surface the flow uses, with settable outcomes."""

    def __init__(
        self,
        *,
        create_error: str | None = None,
        missing: bool = False,
        exited: bool = False,
        exit_code: int | None = None,
        output: bytes = b"",
        end_error: str | None = None,
    ) -> None:
        self.create_error = create_error
        self.missing = missing
        self.exited = exited
        self.exit_code = exit_code
        self.output = output
        self.end_error = end_error
        self.created: list[tuple[Path, str, str, list[str]]] = []
        self.ended: list[str] = []

    async def create(
        self, folder: Path, target_kind: str, target_id: str, *, argv: list[str] | None = None
    ) -> SimpleNamespace:
        if self.create_error is not None:
            raise TerminalSessionError(cast(Any, self.create_error))
        self.created.append((folder, target_kind, target_id, list(argv or [])))
        return SimpleNamespace(session_id="sess_1")

    def get(self, session_id: str) -> SimpleNamespace:
        if self.missing:
            raise TerminalSessionError("not_found")
        return SimpleNamespace(session_id=session_id, exited=self.exited, exit_code=self.exit_code)

    def helper_output(self, session_id: str) -> bytes:
        if self.missing:
            raise TerminalSessionError("not_found")
        return self.output

    async def end(self, session_id: str) -> None:
        if self.end_error is not None:
            raise TerminalSessionError(cast(Any, self.end_error))
        self.ended.append(session_id)


class FakeClaude(ClaudeProvider):
    """A connected Claude provider whose MCP status the test sets."""

    def __init__(
        self,
        workspace: Path,
        *,
        states: Mapping[str, str],
        states_after_reconnect: Mapping[str, str] | None = None,
        connected: bool = True,
    ) -> None:
        super().__init__(workspace)
        self._connected = connected
        self._states = dict(states)
        self._after = dict(states_after_reconnect) if states_after_reconnect is not None else None
        self.reconnects: list[str] = []

    async def read_mcp_status(self) -> dict[str, str]:
        return dict(self._states)

    async def reconnect_mcp_server(self, server_name: str) -> bool:
        self.reconnects.append(server_name)
        if self._after is not None:
            self._states = dict(self._after)
        return True


def _action(server: str = "notion", **extra: str) -> ChatAction:
    return ChatAction(
        id="act_1",
        kind="mcp_auth",
        origin="engine",
        state="open",
        params=MappingProxyType({"server": server, "provider": "claude", **extra}),
    )


def _ctx(pcm: FakePcm, provider: object, terminals: object) -> ActionContext:
    chat = cast(Any, SimpleNamespace(chat_id="c1", provider="claude", archived=False))
    return ActionContext(
        pcm=cast(Any, pcm),
        chat=chat,
        provider=cast(Any, provider),
        is_loopback=True,
        terminals=cast(Any, terminals),
        agent_root=None,
    )


@pytest.fixture
def binary(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(mcp_auth_claude, "get_bundled_claude_path", lambda: "/x/claude")
    return "/x/claude"


@pytest.mark.asyncio
async def test_start_builds_the_fixed_argv_in_the_workspace_root(tmp_path: Path, binary: str) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    terminals = FakeTerminals()
    pcm = FakePcm(_action())

    await ClaudeMcpAuthFlow().start(_ctx(pcm, provider, terminals), pcm.action, {})

    assert terminals.created == [
        (tmp_path, "chat", "c1", ["/x/claude", "mcp", "login", "--no-browser", "notion"])
    ]
    assert pcm.last == ("working", {"session_id": "sess_1"}, "")


@pytest.mark.asyncio
async def test_start_refuses_a_server_the_chat_does_not_have(tmp_path: Path, binary: str) -> None:
    provider = FakeClaude(tmp_path, states={"other": "needs_auth"})
    terminals = FakeTerminals()
    pcm = FakePcm(_action())

    await ClaudeMcpAuthFlow().start(_ctx(pcm, provider, terminals), pcm.action, {})

    assert pcm.last[0] == "failed"
    assert pcm.last[2] == NOT_CONFIGURED
    assert terminals.created == []


@pytest.mark.asyncio
async def test_start_refuses_a_claude_ai_connector(tmp_path: Path, binary: str) -> None:
    """A claude.ai connector is connected on claude.ai: no login terminal is started."""
    server = "claude.ai Salesloft"
    provider = FakeClaude(tmp_path, states={server: "needs_auth"})
    terminals = FakeTerminals()
    pcm = FakePcm(_action(server))

    await ClaudeMcpAuthFlow().start(_ctx(pcm, provider, terminals), pcm.action, {})

    assert pcm.last[0] == "failed"
    assert pcm.last[2] == CLAUDEAI_CONNECTOR
    assert terminals.created == []


@pytest.mark.asyncio
async def test_start_refuses_option_like_and_control_names(tmp_path: Path, binary: str) -> None:
    provider = FakeClaude(tmp_path, states={"--help": "needs_auth"})
    for name in ("--help", "a\x1b[2J", "", "x" * 129):
        terminals = FakeTerminals()
        pcm = FakePcm(_action(name))

        await ClaudeMcpAuthFlow().start(_ctx(pcm, provider, terminals), pcm.action, {})

        assert pcm.last[0] == "failed", name
        assert pcm.last[2] == BAD_NAME, name
        assert terminals.created == [], name


@pytest.mark.asyncio
async def test_start_when_already_connected_is_done(tmp_path: Path, binary: str) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "connected"})
    terminals = FakeTerminals()
    pcm = FakePcm(_action())

    await ClaudeMcpAuthFlow().start(_ctx(pcm, provider, terminals), pcm.action, {})

    assert pcm.last[0] == "done"
    assert terminals.created == []


@pytest.mark.asyncio
async def test_start_without_live_session_fails_clearly(tmp_path: Path, binary: str) -> None:
    terminals = FakeTerminals()
    pcm = FakePcm(_action())

    await ClaudeMcpAuthFlow().start(_ctx(pcm, None, terminals), pcm.action, {})

    assert pcm.last[0] == "failed"
    assert pcm.last[2] == NOT_RUNNING
    assert terminals.created == []


@pytest.mark.asyncio
async def test_start_capacity_maps_to_capacity_message(tmp_path: Path, binary: str) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    terminals = FakeTerminals(create_error="capacity")
    pcm = FakePcm(_action())

    await ClaudeMcpAuthFlow().start(_ctx(pcm, provider, terminals), pcm.action, {})

    assert pcm.last[0] == "failed"
    assert pcm.last[2] == CAPACITY


@pytest.mark.asyncio
async def test_poll_scrapes_the_osc8_url_into_waiting(tmp_path: Path) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    terminals = FakeTerminals(output=CLAUDE_OUTPUT)
    pcm = FakePcm(_action(session_id="sess_1"))

    await ClaudeMcpAuthFlow().poll(_ctx(pcm, provider, terminals), pcm.action)

    assert pcm.last == ("waiting", {"url": AUTH_URL}, "")


@pytest.mark.asyncio
async def test_poll_exit_zero_reconnects_then_done_and_ends_helper(tmp_path: Path) -> None:
    provider = FakeClaude(
        tmp_path,
        states={"notion": "needs_auth"},
        states_after_reconnect={"notion": "connected"},
    )
    terminals = FakeTerminals(exited=True, exit_code=0, output=CLAUDE_OUTPUT)
    pcm = FakePcm(_action(session_id="sess_1", url=AUTH_URL))

    await ClaudeMcpAuthFlow().poll(_ctx(pcm, provider, terminals), pcm.action)

    assert provider.reconnects == ["notion"]
    assert pcm.last[0] == "done"
    assert terminals.ended == ["sess_1"]


@pytest.mark.asyncio
async def test_poll_exit_zero_but_not_connected_fails(tmp_path: Path) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    terminals = FakeTerminals(exited=True, exit_code=0, output=CLAUDE_OUTPUT)
    pcm = FakePcm(_action(session_id="sess_1", url=AUTH_URL))

    await ClaudeMcpAuthFlow().poll(_ctx(pcm, provider, terminals), pcm.action)

    assert pcm.last[0] == "failed"
    assert pcm.last[2] == NOT_CONNECTED
    assert terminals.ended == ["sess_1"]


@pytest.mark.asyncio
async def test_poll_exit_nonzero_reports_the_scraped_error(tmp_path: Path) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    output = b'\x1b[31mCouldn\'t complete authentication for "notion": Issuer mismatch\x1b[39m\r\n'
    terminals = FakeTerminals(exited=True, exit_code=1, output=output)
    pcm = FakePcm(_action(session_id="sess_1", url=AUTH_URL))

    await ClaudeMcpAuthFlow().poll(_ctx(pcm, provider, terminals), pcm.action)

    assert pcm.last[0] == "failed"
    assert pcm.last[2] == 'Couldn\'t complete authentication for "notion": Issuer mismatch'
    assert terminals.ended == ["sess_1"]


@pytest.mark.asyncio
async def test_poll_closed_helper_fails(tmp_path: Path) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    terminals = FakeTerminals(missing=True)
    pcm = FakePcm(_action(session_id="sess_1"))

    await ClaudeMcpAuthFlow().poll(_ctx(pcm, provider, terminals), pcm.action)

    assert pcm.last[0] == "failed"
    assert pcm.last[2] == CLOSED


@pytest.mark.asyncio
async def test_cancel_ends_the_helper(tmp_path: Path) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    terminals = FakeTerminals()
    pcm = FakePcm(_action(session_id="sess_1"))

    await ClaudeMcpAuthFlow().cancel(_ctx(pcm, provider, terminals), pcm.action)

    assert terminals.ended == ["sess_1"]


@pytest.mark.asyncio
async def test_cancel_swallows_a_session_that_is_already_gone(tmp_path: Path) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    terminals = FakeTerminals(end_error="not_found")
    pcm = FakePcm(_action(session_id="sess_1"))

    await ClaudeMcpAuthFlow().cancel(_ctx(pcm, provider, terminals), pcm.action)

    assert terminals.ended == []


@pytest.mark.asyncio
async def test_claude_chat_with_needs_auth_raises_a_claude_card(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    project = manager.create_project("Claude", workspace="work")
    chat = manager.create_chat(project.project_id, title="Claude", provider="claude")
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    manager._providers[chat.chat_id] = cast(Any, SimpleNamespace(provider=provider))
    runtime = make_runtime(manager)

    await runtime.detect_mcp_auth(chat.chat_id)

    cards = [a for a in manager.get_chat(chat.chat_id).actions if a.kind == "mcp_auth"]
    assert len(cards) == 1
    assert dict(cards[0].params) == {"server": "notion", "provider": "claude"}


@pytest.mark.asyncio
async def test_claude_ai_connectors_do_not_raise_sign_in_cards(tmp_path: Path) -> None:
    """A claude.ai connector the user never connected is not asked about.

    The organization's catalog lists every connector as needs-auth until the
    user connects it on claude.ai; a card per connector asked for sign-ins to
    services the user does not use.
    """
    manager = make_manager(tmp_path)
    project = manager.create_project("Claude", workspace="work")
    chat = manager.create_chat(project.project_id, title="Claude", provider="claude")
    provider = FakeClaude(
        tmp_path,
        states={"claude.ai Salesloft": "needs_auth", "claude.ai incident.io": "needs_auth", "notion": "needs_auth"},
    )
    manager._providers[chat.chat_id] = cast(Any, SimpleNamespace(provider=provider))
    runtime = make_runtime(manager)

    await runtime.detect_mcp_auth(chat.chat_id)

    cards = [a for a in manager.get_chat(chat.chat_id).actions if a.kind == "mcp_auth"]
    assert [c.params["server"] for c in cards] == ["notion"]


@pytest.mark.asyncio
async def test_mcp_auth_dispatches_a_claude_action_to_the_claude_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[str] = []

    async def record(self: object, ctx: object, action: ChatAction, body: object) -> None:
        recorded.append(action.params["server"])

    monkeypatch.setattr(ClaudeMcpAuthFlow, "start", record)
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    pcm = FakePcm(_action())

    assert set(MCP_AUTH_FLOWS) == {"claude", "opencode"}
    assert isinstance(ACTION_HANDLERS["mcp_auth"], McpAuthHandler)
    await ACTION_HANDLERS["mcp_auth"].start(_ctx(pcm, provider, FakeTerminals()), pcm.action, {})

    assert recorded == ["notion"]


def test_a_claude_session_id_is_engine_only() -> None:
    raw = {"server": "notion", "provider": "claude"}

    with pytest.raises(ChatActionError) as refused:
        check_agent_params("mcp_auth", {**raw, "session_id": "s1"})
    assert refused.value.reason == "unknown_param"

    accepted = validate_params("mcp_auth", {**raw, "session_id": "s1"})
    assert accepted["session_id"] == "s1"

    with pytest.raises(ChatActionError) as bad:
        validate_params("mcp_auth", {**raw, "session_id": "a/b"})
    assert bad.value.reason == "invalid_param"


class _DismissedPcm(FakePcm):
    """A card the user dismissed while the helper was starting."""

    def get_action(self, chat_id: str, action_id: str) -> ChatAction | None:
        return None


@pytest.mark.asyncio
async def test_start_ends_the_helper_when_the_action_was_dismissed_meanwhile(
    tmp_path: Path, binary: str
) -> None:
    provider = FakeClaude(tmp_path, states={"notion": "needs_auth"})
    terminals = FakeTerminals()
    pcm = _DismissedPcm(_action())

    await ClaudeMcpAuthFlow().start(_ctx(pcm, provider, terminals), pcm.action, {})

    assert terminals.ended == ["sess_1"]
    assert pcm.updates == []


@pytest.mark.asyncio
async def test_exit_zero_without_a_live_provider_is_done(tmp_path: Path) -> None:
    terminals = FakeTerminals(exited=True, exit_code=0, output=CLAUDE_OUTPUT)
    pcm = FakePcm(_action(session_id="sess_1", url=AUTH_URL))

    await ClaudeMcpAuthFlow().poll(_ctx(pcm, None, terminals), pcm.action)

    assert pcm.last[0] == "done"
    assert terminals.ended == ["sess_1"]


@pytest.mark.asyncio
async def test_a_reconnect_that_raises_lets_the_status_read_decide(tmp_path: Path) -> None:
    class _RaisingReconnect(FakeClaude):
        async def reconnect_mcp_server(self, server_name: str) -> bool:
            raise RuntimeError("reconnect failed")

    provider = _RaisingReconnect(tmp_path, states={"notion": "connected"})
    terminals = FakeTerminals(exited=True, exit_code=0, output=CLAUDE_OUTPUT)
    pcm = FakePcm(_action(session_id="sess_1", url=AUTH_URL))

    await ClaudeMcpAuthFlow().poll(_ctx(pcm, provider, terminals), pcm.action)

    assert pcm.last[0] == "done"
    assert terminals.ended == ["sess_1"]


def test_the_wake_prompt_quotes_the_server_name() -> None:
    prompt = ClaudeMcpAuthFlow().wake_prompt(_action("claude.ai Slack"))

    assert prompt.startswith("[Ciaobot] ")
    assert prompt.startswith(
        "[Ciaobot] The user signed in to the MCP server " + json.dumps("claude.ai Slack") + "."
    )
