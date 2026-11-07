"""The needs-you runtime: start, poll, dismiss, continue, detection and lifecycle cancel.

The shared fakes live here; ``tests/test_opencode_mcp_oauth.py`` and
``tests/test_routes_chat_actions.py`` import them from this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import MappingProxyType
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from ciao.agent_surface import AGENT_TOKEN_ENV
from ciao.chat_actions import ChatAction, ChatActionError, _VALIDATORS
from ciao.config import CiaoConfig
from ciao.models import AssistantTextDelta, ResultEvent
from ciao.provider_service import ProviderService
from ciao.providers.claude import ClaudeProvider
from ciao.providers.opencode import OpencodeProvider
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_actions_base import ActionContext, ActionHandler
from ciao.web.chat_actions_runtime import (
    ACTION_HANDLERS,
    BAD_URL_ERROR,
    CODE_MODE_ERROR,
    EXPIRED_ERROR,
    NO_SIGN_IN_ERROR,
    MCP_AUTH_FLOWS,
    OPENCODE_MISSING_ERROR,
    POLL_DEADLINE_S,
    SERVER_START_FAILED_ERROR,
    START_FAILED_ERROR,
    STOPPED_ERROR,
    TIMEOUT_ERROR,
    ChatActionsRuntime,
    McpAuthHandler,
)
from ciao.web.chat_broker import ChatStream
from ciao.web.chat_streaming import ChatStreaming, ChatStreamingHost
from ciao.web.project_chats import (
    _PROVIDER_IDLE_TIMEOUT_SECONDS,
    ChatActionConflictError,
    ChatInfo,
    ProjectChatManager,
)
from tests.conftest import attach_stub_mcp
from tests.test_chat_streaming_boundary import _DriveHost


@contextmanager
def _record_events(manager: ProjectChatManager) -> Iterator[list[dict]]:
    """Collect the events the manager publishes while the block runs."""
    published: list[dict] = []
    hub = manager._events
    original = hub.publish
    hub.publish = published.append  # type: ignore[method-assign]
    try:
        yield published
    finally:
        hub.publish = original  # type: ignore[method-assign]


# ── Shared fakes ───────────────────────────────────────────────────────────

INTEGRATION_ID = "mcp_27245be96d0b1111"
ATTEMPT_ID = "con_1a8c0e1dd001zPI6AkLqNBi67w"
AUTH_URL = (
    "https://mcp.linear.app/authorize?response_type=code&client_id=x"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A56446%2Fcallback"
)
INTEGRATION_PATH = f"/api/integration/{INTEGRATION_ID}"
ATTEMPT_PATH = f"{INTEGRATION_PATH}/connect/oauth/{ATTEMPT_ID}"
CONNECT_PATH = f"{INTEGRATION_PATH}/connect/oauth"


class FakeResponse:
    """A minimal httpx-like response: status, JSON body, raise_for_status."""

    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://opencode.test")
            raise httpx.HTTPStatusError(
                "error",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class FakeClient:
    """Routes ``(method, path)`` to canned responses and records every call.

    A list is consumed in order and its last element then repeats. An unrouted
    call answers 404 with an empty object.
    """

    def __init__(
        self,
        routes: dict[tuple[str, str], list[FakeResponse] | FakeResponse] | None = None,
    ) -> None:
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str, object]] = []

    def _answer(self, method: str, path: str) -> FakeResponse:
        entry = self.routes.get((method, path))
        if entry is None:
            return FakeResponse(404, {})
        if isinstance(entry, list):
            if len(entry) > 1:
                return entry.pop(0)
            return entry[0]
        return entry

    async def get(self, path: str, json: object = None, timeout: float | None = None) -> FakeResponse:
        self.calls.append(("GET", path, json))
        return self._answer("GET", path)

    async def post(self, path: str, json: object = None, timeout: float | None = None) -> FakeResponse:
        self.calls.append(("POST", path, json))
        return self._answer("POST", path)

    async def delete(self, path: str, json: object = None, timeout: float | None = None) -> FakeResponse:
        self.calls.append(("DELETE", path, json))
        return self._answer("DELETE", path)


def live_opencode(tmp_path: Path, client: FakeClient) -> OpencodeProvider:
    provider = OpencodeProvider(tmp_path)
    provider._client = cast(Any, client)
    provider._process = cast(Any, SimpleNamespace(returncode=None))
    return provider


def make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def opencode_chat(manager: ProjectChatManager, provider: object) -> ChatInfo:
    project = manager.create_project("Actions", workspace="work")
    chat = manager.create_chat(project.project_id, title="Sign in", provider="opencode")
    manager._providers[chat.chat_id] = cast(Any, SimpleNamespace(provider=provider))
    return chat


def mcp_listing(*rows: tuple[str, str]) -> dict[str, object]:
    """A ``GET /api/mcp`` body with one row per ``(server, status)`` pair."""
    data: list[dict[str, object]] = []
    for name, status in rows:
        data.append({
            "name": name,
            "status": {"status": status, "error": ""},
            "integrationID": INTEGRATION_ID,
        })
    return {"location": {"directory": "/w"}, "data": data}


def sign_in_routes(
    *,
    mcp: FakeResponse | list[FakeResponse] | None = None,
    attempt: FakeResponse | list[FakeResponse] | None = None,
    mode: str = "auto",
) -> dict[tuple[str, str], list[FakeResponse] | FakeResponse]:
    """The verified OpenCode 2.2 integration flow, answering with fixtures."""
    return {
        ("GET", "/api/mcp"): mcp or FakeResponse(200, mcp_listing(("linear", "needs_auth"))),
        ("GET", INTEGRATION_PATH): FakeResponse(200, {
            "location": {"directory": "/w"},
            "data": {
                "id": INTEGRATION_ID,
                "name": "linear",
                "methods": [{"id": INTEGRATION_ID, "type": "oauth", "label": "linear"}],
                "connections": [],
            },
        }),
        ("POST", CONNECT_PATH): FakeResponse(200, {
            "location": {"directory": "/w"},
            "data": {
                "attemptID": ATTEMPT_ID,
                "url": AUTH_URL,
                "instructions": "Authorize linear in your browser.",
                "mode": mode,
                "time": {"created": 1, "expires": 2},
            },
        }),
        ("GET", ATTEMPT_PATH): attempt or FakeResponse(200, {"data": {"status": "pending"}}),
        ("DELETE", ATTEMPT_PATH): FakeResponse(204),
    }


def make_runtime(manager: ProjectChatManager, **overrides: float) -> ChatActionsRuntime:
    settings: dict[str, float] = {
        "poll_interval_s": 0.01,
        "poll_deadline_s": 5.0,
        "detect_timeout_s": 1.0,
    }
    settings.update(overrides)
    runtime = ChatActionsRuntime(manager, **settings)
    manager.actions_runtime = runtime
    return runtime


def raise_sign_in(manager: ProjectChatManager, chat: ChatInfo) -> str:
    payload = manager.raise_action(
        chat.chat_id,
        "mcp_auth",
        {"server": "linear", "provider": "opencode"},
        origin="engine",
    )
    assert payload is not None
    return str(payload["id"])


async def until(predicate: Any, timeout: float = 1.0) -> None:
    """Wait for ``predicate()`` to hold, polling briefly, or fail."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _state(manager: ProjectChatManager, chat: ChatInfo, action_id: str) -> str:
    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    return action.state


# ── Start, poll, dismiss ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_asks_opencode_for_a_sign_in_and_waits(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes())
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)

    payload = await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    assert payload["state"] == "waiting"
    assert cast(dict, payload["params"])["url"] == AUTH_URL
    assert action_id in runtime._polls
    runtime._polls[action_id].cancel()


def fake_server_start(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> list[Any]:
    """Patch the server start to make the provider live on ``client``; returns the requests it saw."""
    seen: list[Any] = []

    async def ensure(self: OpencodeProvider, request: Any) -> Any:
        seen.append(request)
        self._client = cast(Any, client)
        self._process = cast(Any, SimpleNamespace(returncode=None))
        return client

    monkeypatch.setattr(OpencodeProvider, "_ensure_server", ensure)
    return seen


def bare_opencode_chat(tmp_path: Path) -> tuple[ProjectChatManager, ChatInfo]:
    """An OpenCode chat with no provider service: the idle sweep's state."""
    manager = attach_stub_mcp(make_manager(tmp_path))
    project = manager.create_project("Actions", workspace="work")
    chat = manager.create_chat(project.project_id, title="Sign in", provider="opencode")
    return manager, chat


@pytest.mark.asyncio
async def test_sign_in_after_the_idle_sweep_starts_the_chat_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(sign_in_routes())
    started = fake_server_start(monkeypatch, client)
    manager, chat = bare_opencode_chat(tmp_path)
    assert chat.chat_id not in manager._providers  # the idle sweep removed it
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)

    payload = await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    assert (payload["state"], payload.get("error", "")) == ("waiting", "")
    assert cast(dict, payload["params"])["url"] == AUTH_URL
    assert len(started) == 1
    assert started[0].extra_env[AGENT_TOKEN_ENV] == "tok-test"
    provider = manager._providers[chat.chat_id].provider
    assert isinstance(provider, OpencodeProvider) and provider.has_live_server
    assert chat.chat_id in manager._provider_last_used
    runtime._polls[action_id].cancel()


@pytest.mark.asyncio
async def test_sign_in_restarts_a_server_that_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(sign_in_routes())
    started = fake_server_start(monkeypatch, client)
    manager, chat = bare_opencode_chat(tmp_path)
    service = ProviderService(manager._config, provider="opencode", agent_root=tmp_path)
    dead = cast(OpencodeProvider, service.provider)
    dead._client = cast(Any, FakeClient())
    dead._process = cast(Any, SimpleNamespace(returncode=1))
    manager._providers[chat.chat_id] = service
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)

    payload = await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    assert payload["state"] == "waiting"
    assert len(started) == 1
    assert manager._providers[chat.chat_id] is service
    runtime._polls[action_id].cancel()


@pytest.mark.asyncio
async def test_sign_in_on_a_live_server_refreshes_the_idle_stamp(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes())
    manager = attach_stub_mcp(make_manager(tmp_path))
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    manager._provider_last_used[chat.chat_id] = time.monotonic() - 10_000
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)

    async def no_start(self: OpencodeProvider, request: Any) -> Any:
        raise AssertionError("a live server must not be started again")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(OpencodeProvider, "_ensure_server", no_start)
        payload = await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    assert payload["state"] == "waiting"
    assert time.monotonic() - manager._provider_last_used[chat.chat_id] < 5
    runtime._polls[action_id].cancel()


def test_the_sign_in_wait_ends_before_the_idle_sweep() -> None:
    # The wait stops stamping; the sweep must not reap a server during the wait.
    assert POLL_DEADLINE_S < _PROVIDER_IDLE_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_start_without_opencode_installed_fails_with_engine_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ciao.providers.opencode.resolve_opencode_binary", lambda _env=None: None
    )
    manager, chat = bare_opencode_chat(tmp_path)
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)

    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.state == "failed"
    assert action.error == OPENCODE_MISSING_ERROR
    assert action_id not in runtime._polls


@pytest.mark.asyncio
async def test_a_server_that_fails_to_start_fails_the_card_without_its_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    async def broken(self: OpencodeProvider, request: Any) -> Any:
        raise RuntimeError("OpenCode serve exited with code 1: secret-detail")

    monkeypatch.setattr(OpencodeProvider, "_ensure_server", broken)
    manager, chat = bare_opencode_chat(tmp_path)
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)

    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.state == "failed"
    assert action.error == SERVER_START_FAILED_ERROR
    assert "secret-detail" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_a_server_started_into_a_dropped_provider_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, chat = bare_opencode_chat(tmp_path)
    cid = chat.chat_id
    disconnects: list[bool] = []

    async def recorder(self: ProviderService) -> None:
        disconnects.append(True)

    async def start_then_drop(self: OpencodeProvider, request: Any) -> Any:
        self._client = cast(Any, FakeClient(sign_in_routes()))
        self._process = cast(Any, SimpleNamespace(returncode=None))
        manager._pop_provider(cid)  # a concurrent new session drops the service
        return self._client

    monkeypatch.setattr(OpencodeProvider, "_ensure_server", start_then_drop)
    monkeypatch.setattr(ProviderService, "disconnect", recorder)
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)

    await runtime.start(cid, action_id, {}, is_loopback=True)

    action = manager.get_action(cid, action_id)
    assert action is not None
    assert action.state == "failed"
    assert action.error == SERVER_START_FAILED_ERROR
    assert len(disconnects) == 1
    assert cid not in manager._providers


@pytest.mark.asyncio
async def test_start_without_an_integration_fails(tmp_path: Path) -> None:
    client = FakeClient({("GET", "/api/mcp"): FakeResponse(200, mcp_listing())})
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)

    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.state == "failed"
    assert action.error == NO_SIGN_IN_ERROR


@pytest.mark.asyncio
async def test_start_in_code_mode_fails_and_cancels_the_attempt(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes(mode="code"))
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)

    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.state == "failed"
    assert action.error == CODE_MODE_ERROR
    assert ("DELETE", ATTEMPT_PATH, None) in client.calls


@pytest.mark.asyncio
async def test_start_while_waiting_is_an_invalid_state(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes())
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    with pytest.raises(ChatActionError) as info:
        await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    assert info.value.reason == "invalid_state"
    runtime._polls[action_id].cancel()


@pytest.mark.asyncio
async def test_start_states_and_poll_states_come_from_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class WorkingHandler(ActionHandler):
        start_states = frozenset({"working"})
        poll_states = frozenset({"working"})

        async def start(self, ctx: ActionContext, action: Any, body: Any) -> None:
            del ctx, action, body

        def wake_prompt(self, action: Any) -> str:
            del action
            return "restart"

    monkeypatch.setitem(ACTION_HANDLERS, "restart", WorkingHandler())
    monkeypatch.setitem(_VALIDATORS, "restart", lambda raw: {})
    manager = make_manager(tmp_path)
    chat = manager.create_chat(manager.create_project("P", workspace="work").project_id)
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action = manager.raise_action(chat.chat_id, "restart", {}, origin="engine")
    assert action is not None
    action_id = str(action["id"])

    with pytest.raises(ChatActionError) as info:
        await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)
    assert info.value.reason == "invalid_state"

    manager.update_action(chat.chat_id, action_id, state="working")
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    assert action_id in runtime._polls
    runtime._polls[action_id].cancel()


def test_the_context_carries_app_singletons(tmp_path: Path) -> None:
    client = FakeClient()
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager)

    bare = runtime.context(chat.chat_id)
    assert bare is not None
    assert bare.app_state is None
    assert bare.terminals is None

    sentinel = object()
    app_state = SimpleNamespace(terminal_sessions=sentinel)
    runtime.bind_app_state(app_state)
    ctx = runtime.context(chat.chat_id)
    assert ctx is not None
    assert ctx.terminals is sentinel
    assert ctx.app_state is app_state
    assert ctx.agent_root == manager._agent_root_for_chat(chat.chat_id)


@pytest.mark.asyncio
async def test_continue_cancels_before_removing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled: list[str] = []

    class RecordingHandler(ActionHandler):
        async def cancel(self, ctx: ActionContext, action: Any) -> None:
            del ctx
            cancelled.append(action.id)

        def wake_prompt(self, action: Any) -> str:
            del action
            return "continue"

    monkeypatch.setitem(ACTION_HANDLERS, "restart", RecordingHandler())
    monkeypatch.setitem(_VALIDATORS, "restart", lambda raw: {})
    manager = make_manager(tmp_path)
    chat = manager.create_chat(manager.create_project("P", workspace="work").project_id)
    monkeypatch.setattr(manager, "_deliver_wake", lambda *a, **k: "started")
    runtime = make_runtime(manager)
    action = manager.raise_action(chat.chat_id, "restart", {}, origin="engine")
    assert action is not None
    manager.update_action(chat.chat_id, str(action["id"]), state="done")

    await runtime.continue_action(chat.chat_id, str(action["id"]))

    assert cancelled == [str(action["id"])]


@pytest.mark.asyncio
async def test_the_base_handler_refuses_a_callback() -> None:
    with pytest.raises(ChatActionError) as info:
        await ActionHandler().callback(cast(Any, None), cast(Any, None), "x")
    assert info.value.reason == "unsupported_kind"


@pytest.mark.asyncio
async def test_start_of_an_unknown_action_conflicts(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, None)
    runtime = make_runtime(manager)

    with pytest.raises(ChatActionConflictError):
        await runtime.start(chat.chat_id, "missing", {}, is_loopback=True)


@pytest.mark.asyncio
async def test_polling_marks_the_action_done_when_the_server_connects(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes(
        mcp=[
            FakeResponse(200, mcp_listing(("linear", "needs_auth"))),
            FakeResponse(200, mcp_listing(("linear", "connected"))),
        ],
    ))
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    await until(
        lambda: _state(manager, chat, action_id) == "done" and action_id not in runtime._polls
    )


@pytest.mark.asyncio
async def test_a_failed_attempt_fails_the_action_with_its_first_line(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes(attempt=FakeResponse(200, {
        "data": {"status": "failed", "message": "invalid_client: redirect\nmore"},
    })))
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    await until(lambda: _state(manager, chat, action_id) == "failed")

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.error == "invalid_client: redirect"


@pytest.mark.asyncio
async def test_an_expired_attempt_fails_the_action(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes(attempt=FakeResponse(404, {})))
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    await until(lambda: _state(manager, chat, action_id) == "failed")

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.error == EXPIRED_ERROR


@pytest.mark.asyncio
async def test_polling_times_out_after_the_deadline(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes())
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=0.05)
    action_id = raise_sign_in(manager, chat)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    await until(lambda: _state(manager, chat, action_id) == "failed")

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.error == TIMEOUT_ERROR
    assert ("DELETE", ATTEMPT_PATH, None) in client.calls


@pytest.mark.asyncio
async def test_a_dismissal_stops_polling_and_cancels_the_attempt(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes())
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    remaining = await runtime.dismiss(chat.chat_id, action_id)

    assert remaining == []
    assert action_id not in runtime._polls
    assert ("DELETE", ATTEMPT_PATH, None) in client.calls
    assert "mcp_auth:linear" in chat.dismissed_action_keys


@pytest.mark.asyncio
async def test_continue_wakes_the_chat_and_removes_the_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    woken: list[dict[str, object]] = []

    def record_wake(parent: ChatInfo, prompt: str, *, count: int, origin: str) -> str:
        woken.append({"chat": parent.chat_id, "prompt": prompt, "count": count, "origin": origin})
        return "started"

    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, None)
    monkeypatch.setattr(manager, "_deliver_wake", record_wake)
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)
    manager.update_action(chat.chat_id, action_id, state="done")

    remaining, delivery = await runtime.continue_action(chat.chat_id, action_id)

    assert (remaining, delivery) == ([], "started")
    assert len(woken) == 1
    assert woken[0]["count"] == 1
    assert woken[0]["origin"] == "chat_action"
    assert "linear" in str(woken[0]["prompt"])
    assert chat.dismissed_action_keys == set()


@pytest.mark.asyncio
async def test_continue_before_done_is_an_invalid_state(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, None)
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)

    with pytest.raises(ChatActionError) as info:
        await runtime.continue_action(chat.chat_id, action_id)

    assert info.value.reason == "invalid_state"


# ── Turn-end detection ─────────────────────────────────────────────────────


def _detection_routes(rows: list[tuple[str, str]]) -> FakeClient:
    return FakeClient({("GET", "/api/mcp"): FakeResponse(200, mcp_listing(*rows))})


@pytest.mark.asyncio
async def test_detection_raises_an_action_for_each_server_that_needs_a_sign_in(
    tmp_path: Path,
) -> None:
    client = _detection_routes([
        ("linear", "needs_auth"),
        ("notion", "connected"),
        ("x", "failed"),
    ])
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager)

    await runtime.detect_mcp_auth(chat.chat_id)

    assert len(chat.actions) == 1
    action = chat.actions[0]
    assert action.kind == "mcp_auth"
    assert dict(action.params) == {"server": "linear", "provider": "opencode"}
    assert action.origin == "engine"


@pytest.mark.asyncio
async def test_detection_skips_a_dismissed_server(tmp_path: Path) -> None:
    client = _detection_routes([("linear", "needs_auth")])
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    chat.dismissed_action_keys.add("mcp_auth:linear")
    runtime = make_runtime(manager)

    await runtime.detect_mcp_auth(chat.chat_id)

    assert chat.actions == []


@pytest.mark.asyncio
async def test_not_now_on_a_detected_sign_in_holds_for_every_chat(tmp_path: Path) -> None:
    """Not now on a detected sign-in is not asked again in another chat.

    Detection runs after each turn and a provider can list every connector
    of the account, so remembering the answer per chat raised the same cards
    again in every new chat.
    """
    client = _detection_routes([("linear", "needs_auth"), ("sentry", "needs_auth")])
    manager = make_manager(tmp_path)
    first = opencode_chat(manager, live_opencode(tmp_path, client))
    project = manager.create_project("Other", workspace="work")
    second = manager.create_chat(project.project_id, title="Later", provider="opencode")
    manager._providers[second.chat_id] = cast(Any, SimpleNamespace(provider=live_opencode(tmp_path, client)))
    action_id = raise_sign_in(manager, first)
    runtime = make_runtime(manager)

    manager.dismiss_action(first.chat_id, action_id)
    await runtime.detect_mcp_auth(second.chat_id)

    assert [a.params["server"] for a in second.actions] == ["sentry"]


@pytest.mark.asyncio
async def test_not_now_on_a_detected_sign_in_is_per_provider(tmp_path: Path) -> None:
    client = _detection_routes([("linear", "needs_auth")])
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    manager.declined_mcp_auth.add((manager._workspace_for_chat(chat.chat_id), "claude", "mcp_auth:linear"))
    runtime = make_runtime(manager)

    await runtime.detect_mcp_auth(chat.chat_id)

    assert [a.params["server"] for a in chat.actions] == ["linear"]


@pytest.mark.asyncio
async def test_not_now_on_a_detected_sign_in_is_per_workspace(tmp_path: Path) -> None:
    client = _detection_routes([("linear", "needs_auth")])
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    manager.declined_mcp_auth.add(("another-workspace", "opencode", "mcp_auth:linear"))
    runtime = make_runtime(manager)

    await runtime.detect_mcp_auth(chat.chat_id)

    assert [a.params["server"] for a in chat.actions] == ["linear"]


@pytest.mark.asyncio
async def test_a_connected_server_clears_its_not_now(tmp_path: Path) -> None:
    client = _detection_routes([("linear", "connected")])
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    manager.declined_mcp_auth.add(manager.mcp_auth_decline_key(chat, "mcp_auth:linear"))
    runtime = make_runtime(manager)

    await runtime.detect_mcp_auth(chat.chat_id)

    assert manager.declined_mcp_auth == set()


@pytest.mark.asyncio
async def test_detection_marks_an_open_action_done_when_its_server_connects(
    tmp_path: Path,
) -> None:
    client = _detection_routes([("linear", "connected")])
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)

    await runtime.detect_mcp_auth(chat.chat_id)

    assert _state(manager, chat, action_id) == "done"


@pytest.mark.asyncio
async def test_detection_ignores_a_claude_chat_without_a_live_session(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    project = manager.create_project("Claude", workspace="work")
    chat = manager.create_chat(project.project_id, title="Claude", provider="claude")
    idle = ClaudeProvider(tmp_path)
    manager._providers[chat.chat_id] = cast(Any, SimpleNamespace(provider=idle))
    runtime = make_runtime(manager)

    assert runtime.detection_provider(chat.chat_id) is None
    await runtime.detect_mcp_auth(chat.chat_id)

    assert chat.actions == []


@pytest.mark.asyncio
async def test_detection_survives_a_provider_error(tmp_path: Path) -> None:
    client = FakeClient({("GET", "/api/mcp"): FakeResponse(500, {})})
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager)

    await runtime.detect_mcp_auth(chat.chat_id)

    assert chat.actions == []


@pytest.mark.asyncio
async def test_detection_is_bounded_by_its_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never_answers(self: OpencodeProvider) -> dict[str, str]:
        del self
        await asyncio.sleep(10)
        return {}

    client = _detection_routes([("linear", "needs_auth")])
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    monkeypatch.setattr(OpencodeProvider, "read_mcp_status", never_answers)
    runtime = make_runtime(manager, detect_timeout_s=0.05)

    started = time.monotonic()
    await runtime.detect_mcp_auth(chat.chat_id)

    assert time.monotonic() - started < 1.0
    assert chat.actions == []


@pytest.mark.asyncio
async def test_a_finished_turn_schedules_action_detection(tmp_path: Path) -> None:
    host = _DriveHost(
        [
            AssistantTextDelta(type="text", text="hello"),
            ResultEvent(
                type="result",
                result="done",
                session_id="session-1",
                effective_model="opus",
                usage={"input_tokens": "3", "output_tokens": "5"},
                quota={"five_hour": "7"},
            ),
        ],
        tmp_path,
    )
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    stream = ChatStream(prompt_text="hello")
    host._broker.register("chat-1", stream)

    await streaming.drive(
        chat_id="chat-1",
        project_id="project-1",
        prompt="hello",
        images=None,
        turn_index=None,
        chat_meta=host.chat,
        stream=stream,
        is_retry=False,
        unattended=False,
    )

    assert host.detections == ["chat-1"]


@pytest.mark.asyncio
async def test_detection_is_only_spawned_for_a_live_opencode_chat(tmp_path: Path) -> None:
    def detection_tasks(manager: ProjectChatManager) -> list[asyncio.Task[object]]:
        return [t for t in manager._detached_tasks if t.get_name().startswith("chat-actions-detect-")]

    manager = make_manager(tmp_path)
    claude = manager.create_chat(manager.create_project("C", workspace="work").project_id)
    make_runtime(manager)
    manager._schedule_action_detection(claude.chat_id)
    assert detection_tasks(manager) == []

    client = _detection_routes([("linear", "needs_auth")])
    live = opencode_chat(manager, live_opencode(tmp_path, client))
    manager._schedule_action_detection(live.chat_id)
    tasks = detection_tasks(manager)
    assert len(tasks) == 1
    await tasks[0]


# ── Fix round 1 (review of #2569) ─────────────────────────────────────────


class _Service:
    """A provider service as the lifecycle paths see it: a provider and a disconnect."""

    def __init__(self, provider: object) -> None:
        self.provider = provider

    async def disconnect(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_dismissal_during_start_cancels_the_attempt(tmp_path: Path) -> None:
    gate = asyncio.Event()

    class GatedClient(FakeClient):
        async def post(self, path: str, json: object = None, timeout: float | None = None) -> FakeResponse:
            if path == CONNECT_PATH:
                await gate.wait()
            return await super().post(path, json=json, timeout=timeout)

    client = GatedClient(sign_in_routes())
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)
    start = asyncio.create_task(runtime.start(chat.chat_id, action_id, {}, is_loopback=True))
    await until(lambda: _state(manager, chat, action_id) == "working")

    await runtime.dismiss(chat.chat_id, action_id)
    gate.set()

    with pytest.raises(ChatActionConflictError):
        await start
    assert ("DELETE", ATTEMPT_PATH, None) in client.calls
    assert action_id not in MCP_AUTH_FLOWS["opencode"]._attempts  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["new_session", "handover", "archive"])
async def test_a_lifecycle_clear_cancels_the_opencode_attempt(tmp_path: Path, lifecycle: str) -> None:
    client = FakeClient(sign_in_routes())
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    manager._providers[chat.chat_id] = cast(Any, _Service(live_opencode(tmp_path, client)))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    if lifecycle == "new_session":
        manager.new_session(chat.chat_id)
    elif lifecycle == "handover":
        manager.handover_chat(chat.chat_id, provider="claude", model="haiku")
    else:
        await manager.archive_chat(chat.chat_id)

    await until(lambda: ("DELETE", ATTEMPT_PATH, None) in client.calls)
    assert action_id not in runtime._polls


def test_the_wake_prompt_quotes_the_server_name() -> None:
    server = 'lin\near "x"'
    action = ChatAction(
        id="a1",
        kind="mcp_auth",
        origin="engine",
        state="done",
        params=MappingProxyType({"server": server, "provider": "opencode"}),
    )

    prompt = McpAuthHandler().wake_prompt(action)

    assert json.dumps(server) in prompt
    assert server not in prompt


@pytest.mark.asyncio
async def test_start_refuses_an_authorize_url_that_is_not_http(tmp_path: Path) -> None:
    routes = sign_in_routes()
    routes[("POST", CONNECT_PATH)] = FakeResponse(200, {
        "data": {"attemptID": ATTEMPT_ID, "url": "javascript:alert(1)", "mode": "auto"},
    })
    client = FakeClient(routes)
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)

    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.state == "failed"
    assert action.error == BAD_URL_ERROR
    assert "url" not in action.params
    assert ("DELETE", ATTEMPT_PATH, None) in client.calls
    assert action_id not in runtime._polls


@pytest.mark.asyncio
async def test_a_server_that_stops_after_start_fails_the_action(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes())
    manager = make_manager(tmp_path)
    provider = live_opencode(tmp_path, client)
    chat = opencode_chat(manager, provider)
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    provider._process = cast(Any, SimpleNamespace(returncode=1))

    await until(lambda: _state(manager, chat, action_id) == "failed" and action_id not in runtime._polls)
    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.error == STOPPED_ERROR


@pytest.mark.asyncio
async def test_a_connect_failure_fails_the_action_without_logging_the_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    routes = sign_in_routes()
    routes[("POST", CONNECT_PATH)] = FakeResponse(500, {})
    client = FakeClient(routes)
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)

    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.state == "failed"
    assert action.error == START_FAILED_ERROR
    assert AUTH_URL not in caplog.text


@pytest.mark.asyncio
async def test_a_poll_tick_that_raises_does_not_end_the_wait(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes(
        mcp=[
            FakeResponse(200, mcp_listing(("linear", "needs_auth"))),
            FakeResponse(500, {}),
            FakeResponse(200, mcp_listing(("linear", "connected"))),
        ],
    ))
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager, poll_deadline_s=600.0)
    action_id = raise_sign_in(manager, chat)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    await until(
        lambda: _state(manager, chat, action_id) == "done" and action_id not in runtime._polls
    )


@pytest.mark.asyncio
async def test_a_hanging_cancel_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.web.chat_actions_runtime as runtime_module

    class HangingHandler(ActionHandler):
        async def cancel(self, ctx: ActionContext, action: Any) -> None:
            del ctx, action
            await asyncio.sleep(10)

    monkeypatch.setattr(runtime_module, "CANCEL_TIMEOUT_S", 0.05)
    monkeypatch.setitem(ACTION_HANDLERS, "restart", HangingHandler())
    monkeypatch.setitem(_VALIDATORS, "restart", lambda raw: {})
    manager = make_manager(tmp_path)
    chat = manager.create_chat(manager.create_project("P", workspace="work").project_id)
    runtime = make_runtime(manager)
    action = manager.raise_action(chat.chat_id, "restart", {}, origin="engine")
    assert action is not None

    started = time.monotonic()
    remaining = await runtime.dismiss(chat.chat_id, str(action["id"]))

    assert time.monotonic() - started < 1.0
    assert remaining == []


@pytest.mark.asyncio
async def test_detection_twice_keeps_one_action_and_its_id(tmp_path: Path) -> None:
    client = _detection_routes([("linear", "needs_auth")])
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager)

    with _record_events(manager) as published:
        await runtime.detect_mcp_auth(chat.chat_id)
        first_id = chat.actions[0].id
        await runtime.detect_mcp_auth(chat.chat_id)

    assert [a.id for a in chat.actions] == [first_id]
    assert len([e for e in published if e["type"] == "chat_actions_changed"]) == 1


@pytest.mark.asyncio
async def test_detection_leaves_a_waiting_action_waiting(tmp_path: Path) -> None:
    client = _detection_routes([("linear", "needs_auth")])
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client))
    runtime = make_runtime(manager)
    action_id = raise_sign_in(manager, chat)
    manager.update_action(chat.chat_id, action_id, state="waiting")

    await runtime.detect_mcp_auth(chat.chat_id)

    assert [a.id for a in chat.actions] == [action_id]
    assert _state(manager, chat, action_id) == "waiting"
