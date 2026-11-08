"""The gws_auth needs-you card: a one-click Google sign-in for an expired workspace login.

The handler drives the app's GwsReloginManager; a fake stands in for it so each
state transition is observed at the seam (what the handler writes to the card).
The agent raise and the callback route run over the real control plane and the
real manager.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

import ciao.gws_auth
from ciao.chat_actions import (
    ChatAction,
    ChatActionError,
    action_key,
    check_agent_params,
    validate_params,
)
from ciao.web.chat_actions_base import ActionContext
from ciao.web.chat_actions_runtime import ACTION_HANDLERS
from ciao.web.gws_auth_action import FAILED_DEFAULT, TIMED_OUT, UNAVAILABLE, GwsAuthHandler
from tests.test_agent_surface import _client as _agent_client
from tests.test_agent_surface import _offer_chat, _offer_service, _offer_token
from tests.test_agent_surface import _post as _agent_post
from tests.test_chat_action_callback_route import _client as _route_client
from tests.test_chat_action_callback_route import _post as _route_post
from tests.test_chat_action_callback_route import _recorder
from tests.test_chat_actions_runtime import make_manager
from tests.test_mcp_auth_claude import FakePcm

AUTH_URL = "https://accounts.google.com/o/oauth2/auth?x"


class FakeRelogin:
    """Records the calls the handler makes on the app's GwsReloginManager."""

    def __init__(
        self,
        *,
        start_result: Mapping[str, Any] | None = None,
        start_error: Exception | None = None,
        complete_error: Exception | None = None,
    ) -> None:
        self.start_result = dict(start_result or {"ok": True, "auth_url": AUTH_URL})
        self.start_error = start_error
        self.complete_error = complete_error
        self.status_result: dict[str, Any] = {"status": "pending"}
        self.start_calls: list[str] = []
        self.status_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.complete_calls: list[tuple[str, str]] = []

    def start(self, profile: str) -> dict[str, Any]:
        self.start_calls.append(profile)
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    def status(self, profile: str) -> dict[str, Any]:
        self.status_calls.append(profile)
        return self.status_result

    def cancel(self, profile: str) -> dict[str, Any]:
        self.cancel_calls.append(profile)
        return {"ok": True, "cancelled": True, "profile": profile}

    def complete_from_redirect(self, profile: str, url: str) -> dict[str, Any]:
        self.complete_calls.append((profile, url))
        if self.complete_error is not None:
            raise self.complete_error
        self.status_result = {"status": "completed", "profile": profile}
        return self.status_result


class FakeMonitor:
    def __init__(self) -> None:
        self.calls = 0

    def check_once(self) -> dict[str, Any]:
        self.calls += 1
        return {}


def _action(state: str = "open", params: Mapping[str, str] | None = None) -> ChatAction:
    return ChatAction(
        id="a1",
        kind="gws_auth",
        origin="engine",
        state=cast(Any, state),
        params=MappingProxyType(dict(params if params is not None else {"profile": "work"})),
    )


def _ctx(pcm: FakePcm, relogin: Any, monitor: Any = None) -> ActionContext:
    app_state = None
    if relogin is not None or monitor is not None:
        app_state = SimpleNamespace(gws_relogin_manager=relogin, gws_health_monitor=monitor)
    return ActionContext(
        pcm=cast(Any, pcm),
        chat=cast(Any, SimpleNamespace(chat_id="c1", archived=False)),
        provider=None,
        is_loopback=True,
        app_state=app_state,
    )


def test_validate_gws_auth_params() -> None:
    assert validate_params("gws_auth", {"profile": "work"}) == {"profile": "work"}
    assert validate_params(
        "gws_auth", {"profile": "work", "url": "https://accounts.google.com/o/oauth2/auth?x"}
    ) == {"profile": "work", "url": "https://accounts.google.com/o/oauth2/auth?x"}
    for profile, reason in [("Work", "invalid_param"), ("a b", "invalid_param"), ("", "invalid_param"), (3, "invalid_type")]:
        with pytest.raises(ChatActionError) as exc:
            validate_params("gws_auth", {"profile": profile})
        assert exc.value.reason == reason
    with pytest.raises(ChatActionError) as missing:
        validate_params("gws_auth", {})
    assert missing.value.reason == "missing_param"
    with pytest.raises(ChatActionError) as extra:
        validate_params("gws_auth", {"profile": "work", "x": "1"})
    assert extra.value.reason == "unknown_param"
    for url in ["https://evil.example/", "http://accounts.google.com/"]:
        with pytest.raises(ChatActionError) as bad:
            validate_params("gws_auth", {"profile": "work", "url": url})
        assert bad.value.reason == "invalid_url"
    with pytest.raises(ChatActionError) as agent_url:
        check_agent_params("gws_auth", {"profile": "work", "url": "https://accounts.google.com/x"})
    assert agent_url.value.reason == "unknown_param"
    assert action_key("gws_auth", {"profile": "work"}) == "gws_auth:work"


def test_agent_raise_refuses_an_unlinked_profile(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)
    seen: list[str] = []
    monkeypatch.setattr(
        ciao.gws_auth, "workspace_gws_profile", lambda config, ws: seen.append(ws) or "work"
    )
    with _agent_client(service) as client:
        refused = _agent_post(
            client, token, "chat_needs_action", {"kind": "gws_auth", "params": {"profile": "personal"}}
        )
        accepted = _agent_post(
            client, token, "chat_needs_action", {"kind": "gws_auth", "params": {"profile": "work"}}
        )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_action"
    message = refused.json()["error"]["message"]
    assert "linked Google account" in message
    assert "personal" not in message
    assert accepted.status_code == 200
    action = accepted.json()["data"]["action"]
    assert action["origin"] == "agent"
    assert action["params"] == {"profile": "work"}
    assert seen == ["personal", "personal"]

    monkeypatch.setattr(ciao.gws_auth, "workspace_gws_profile", lambda config, ws: "")
    with _agent_client(service) as client:
        unlinked = _agent_post(
            client, token, "chat_needs_action", {"kind": "gws_auth", "params": {"profile": "work"}}
        )
    assert unlinked.status_code == 422
    assert unlinked.json()["error"]["code"] == "invalid_action"


@pytest.mark.asyncio
async def test_start_sets_waiting_with_the_managers_url() -> None:
    relogin = FakeRelogin()
    pcm = FakePcm(_action())

    await GwsAuthHandler().start(_ctx(pcm, relogin), _action(), {"url": "https://evil"})

    assert relogin.start_calls == ["work"]
    assert pcm.last == ("waiting", {"url": AUTH_URL}, "")


@pytest.mark.asyncio
async def test_start_value_error_fails_action() -> None:
    message = "This OAuth client is a web app and does not support one-click sign-in."
    relogin = FakeRelogin(start_error=ValueError(message))
    pcm = FakePcm(_action())

    await GwsAuthHandler().start(_ctx(pcm, relogin), _action(), {})

    assert pcm.last == ("failed", {}, message)


@pytest.mark.asyncio
async def test_start_without_a_manager_fails() -> None:
    pcm = FakePcm(_action())

    await GwsAuthHandler().start(_ctx(pcm, None), _action(), {})

    assert pcm.last == ("failed", {}, UNAVAILABLE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, expected_state, expected_error",
    [
        ({"status": "pending"}, None, ""),
        ({"status": "completed"}, "done", ""),
        ({"status": "error", "error": "Google returned error: access_denied"}, "failed", "Google returned error: access_denied"),
        ({"status": "none"}, "failed", TIMED_OUT),
    ],
)
async def test_poll_maps_manager_status(
    status: dict[str, Any], expected_state: str | None, expected_error: str
) -> None:
    relogin = FakeRelogin()
    relogin.status_result = status
    monitor = FakeMonitor()
    pcm = FakePcm(_action("waiting"))
    handler = GwsAuthHandler()

    await handler.poll(_ctx(pcm, relogin, monitor), _action("waiting"))
    await asyncio.gather(*handler._refresh_tasks)

    if expected_state is None:
        assert pcm.updates == []
        assert monitor.calls == 0
    else:
        assert pcm.last == (expected_state, {}, expected_error)
        assert monitor.calls == (1 if expected_state == "done" else 0)


@pytest.mark.asyncio
async def test_callback_delegates_to_the_manager() -> None:
    url = "http://127.0.0.1:5555/?code=a&state=b"
    relogin = FakeRelogin()
    monitor = FakeMonitor()
    pcm = FakePcm(_action("waiting"))
    handler = GwsAuthHandler()

    await handler.callback(_ctx(pcm, relogin, monitor), _action("waiting"), url)
    await asyncio.gather(*handler._refresh_tasks)

    assert relogin.complete_calls == [("work", url)]
    assert pcm.last[0] == "done"
    assert monitor.calls == 1

    for message, reason in [
        (ciao.gws_auth.RELOGIN_NO_PENDING, "no_pending_sign_in"),
        (ciao.gws_auth.RELOGIN_BAD_REDIRECT, "bad_callback_url"),
        (ciao.gws_auth.RELOGIN_STATE_MISMATCH, "state_mismatch"),
    ]:
        failing = FakeRelogin(complete_error=ValueError(message))
        refused_pcm = FakePcm(_action("waiting"))
        with pytest.raises(ChatActionError) as exc:
            await GwsAuthHandler().callback(_ctx(refused_pcm, failing), _action("waiting"), url)
        assert exc.value.reason == reason
        assert refused_pcm.updates == []


class _ErroringRelogin(FakeRelogin):
    """A paste is accepted, but the exchange it starts ends in an error."""

    def complete_from_redirect(self, profile: str, url: str) -> dict[str, Any]:
        self.complete_calls.append((profile, url))
        self.status_result = {"status": "error", "error": "boom"}
        return self.status_result


class _ValidatingPcm(FakePcm):
    """Applies the control plane's params check to a waiting update, as the real card does."""

    def update_action(
        self,
        chat_id: str,
        action_id: str,
        *,
        state: str,
        params_update: dict[str, object] | None = None,
        error: str = "",
    ) -> dict[str, object] | None:
        if state == "waiting":
            validate_params(
                "gws_auth", {"profile": self.action.params["profile"], **(params_update or {})}
            )
        return super().update_action(
            chat_id, action_id, state=state, params_update=params_update, error=error
        )


@pytest.mark.asyncio
async def test_callback_without_a_manager_is_no_pending_sign_in() -> None:
    pcm = FakePcm(_action("waiting"))

    with pytest.raises(ChatActionError) as exc:
        await GwsAuthHandler().callback(
            _ctx(pcm, None), _action("waiting"), "http://127.0.0.1:5555/?code=a&state=b"
        )

    assert exc.value.reason == "no_pending_sign_in"
    assert pcm.updates == []


@pytest.mark.asyncio
async def test_a_paste_whose_exchange_errors_ends_the_card_failed() -> None:
    url = "http://127.0.0.1:5555/?code=a&state=b"
    relogin = _ErroringRelogin()
    pcm = FakePcm(_action("waiting"))

    await GwsAuthHandler().callback(_ctx(pcm, relogin), _action("waiting"), url)

    assert relogin.complete_calls == [("work", url)]
    assert pcm.last == ("failed", {}, "boom")


@pytest.mark.asyncio
async def test_an_unknown_refusal_falls_back_to_bad_callback_url() -> None:
    relogin = FakeRelogin(complete_error=ValueError("some message nobody mapped"))
    pcm = FakePcm(_action("waiting"))

    with pytest.raises(ChatActionError) as exc:
        await GwsAuthHandler().callback(
            _ctx(pcm, relogin), _action("waiting"), "http://127.0.0.1:5555/?code=a&state=b"
        )

    assert exc.value.reason == "bad_callback_url"
    assert pcm.updates == []


@pytest.mark.asyncio
async def test_start_with_a_non_google_url_cancels_the_manager_and_fails() -> None:
    relogin = FakeRelogin(start_result={"ok": True, "auth_url": "https://evil.example/x"})
    pcm = _ValidatingPcm(_action())

    await GwsAuthHandler().start(_ctx(pcm, relogin), _action(), {})

    assert relogin.start_calls == ["work"]
    assert relogin.cancel_calls == ["work"]
    assert pcm.last == ("failed", {}, FAILED_DEFAULT)


def test_wake_prompt_names_the_profile() -> None:
    prompt = GwsAuthHandler().wake_prompt(_action("done"))
    assert "'work' Google account" in prompt


def test_gws_auth_is_registered() -> None:
    assert isinstance(ACTION_HANDLERS["gws_auth"], GwsAuthHandler)


def test_the_callback_route_reaches_the_gws_handler(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    relogin = FakeRelogin(complete_error=ValueError(ciao.gws_auth.RELOGIN_STATE_MISMATCH))
    manager.actions_runtime.bind_app_state(SimpleNamespace(gws_relogin_manager=relogin))
    project = manager.create_project("Google", workspace="work")
    chat = manager.create_chat(project.project_id, title="Google")
    aid = str(manager.raise_action(chat.chat_id, "gws_auth", {"profile": "work"}, origin="engine")["id"])
    manager.update_action(chat.chat_id, aid, state="waiting", params_update={"url": AUTH_URL})
    calls = _recorder(monkeypatch)
    body = {"url": "http://127.0.0.1:5555/?code=a&state=b"}

    refused = _route_post(_route_client(manager), chat.chat_id, aid, body)

    assert refused.status_code == 400
    assert refused.json()["error"] == "state_mismatch"
    assert refused.json()["action"]["state"] == "waiting"

    relogin.complete_error = None
    accepted = _route_post(_route_client(manager), chat.chat_id, aid, body)

    assert accepted.status_code == 200
    assert accepted.json()["action"]["state"] == "done"
    assert calls == []
