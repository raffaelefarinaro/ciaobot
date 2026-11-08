"""The `system_setting` needs-you kind: a card that opens one macOS Privacy & Security pane.

The agent names a pane from a closed set. The engine maps the name to a constant
URL and runs `open <url>` with a fixed argv on its own Mac. The model never
supplies a URL, and a start from a non-loopback peer changes nothing.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.chat_actions import SYSTEM_SETTINGS, ChatActionError, action_key, validate_params
from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import chat_actions_runtime
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.chat_actions_runtime import (
    ACTION_HANDLERS,
    SYSTEM_SETTING_OFF_MAC_ERROR,
    SYSTEM_SETTING_OPEN_ERROR,
    SYSTEM_SETTING_URLS,
)
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_chat_actions import chat_action_start
from tests.test_agent_surface import _client as _agent_client
from tests.test_agent_surface import _offer_chat, _offer_service, _offer_token, _post

_ORIGIN = "https://ciao.example"
_HEADERS = {"Origin": _ORIGIN}
_FDA_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def _make_manager(tmp_path: Path) -> ProjectChatManager:
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


def _chat(manager: ProjectChatManager) -> str:
    project = manager.create_project("Settings", workspace="work")
    chat = manager.create_chat(project.project_id, title="Settings", provider="claude")
    return str(chat.chat_id)


def _raise(manager: ProjectChatManager, chat_id: str, setting: str = "full_disk_access") -> str:
    payload = manager.raise_action(chat_id, "system_setting", {"setting": setting}, origin="agent")
    assert payload is not None
    return str(payload["id"])


class _Recorder:
    """Stands in for `subprocess.run` and records each call; it can be told to fail."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self._error = error

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))
        if self._error is not None:
            raise self._error


def _set_platform(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)


# ── The validator ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("setting", SYSTEM_SETTINGS)
def test_validate_system_setting(setting: str) -> None:
    assert validate_params("system_setting", {"setting": setting}) == {"setting": setting}


def test_a_note_comes_back_stripped() -> None:
    assert validate_params("system_setting", {"setting": "camera", "note": "  to read Mail  "}) == {
        "setting": "camera",
        "note": "to read Mail",
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"setting": "Full_Disk_Access"},
        {"setting": _FDA_URL},
        {"setting": ""},
        {"setting": " camera"},
    ],
)
def test_a_setting_outside_the_closed_set_is_invalid_param(raw: dict[str, Any]) -> None:
    with pytest.raises(ChatActionError) as excinfo:
        validate_params("system_setting", raw)
    assert excinfo.value.reason == "invalid_param"


@pytest.mark.parametrize("raw", [{"setting": None}, {"setting": 1}])
def test_a_non_text_setting_is_invalid_type(raw: dict[str, Any]) -> None:
    with pytest.raises(ChatActionError) as excinfo:
        validate_params("system_setting", raw)
    assert excinfo.value.reason == "invalid_type"


@pytest.mark.parametrize("raw", [{}, {"note": "x"}])
def test_a_missing_setting_is_missing_param(raw: dict[str, Any]) -> None:
    with pytest.raises(ChatActionError) as excinfo:
        validate_params("system_setting", raw)
    assert excinfo.value.reason == "missing_param"


def test_an_extra_key_is_unknown_param() -> None:
    with pytest.raises(ChatActionError) as excinfo:
        validate_params("system_setting", {"setting": "camera", "url": "file:///etc/passwd"})
    assert excinfo.value.reason == "unknown_param"


def test_key_is_per_setting() -> None:
    assert action_key("system_setting", {"setting": "camera"}) == "system_setting:camera"


def test_every_setting_has_a_constant_privacy_url() -> None:
    assert set(SYSTEM_SETTING_URLS) == set(SYSTEM_SETTINGS) == set(chat_actions_runtime.SYSTEM_SETTING_TITLES)
    for url in SYSTEM_SETTING_URLS.values():
        assert url.startswith("x-apple.systempreferences:com.apple.preference.security?Privacy_")
        assert re.fullmatch(r"^Privacy_[A-Za-z]+$", url.split("?", 1)[1]) is not None


# ── The raise (control plane, over the real agent route) ───────────────────


def test_raise_refused_off_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)

    _set_platform(monkeypatch, "linux")
    with _agent_client(service) as client:
        refused = _post(client, token, "chat_needs_action", {"kind": "system_setting", "params": {"setting": "camera"}})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_action"
    assert "only when Ciaobot runs on macOS" in refused.json()["error"]["message"]
    assert manager.get_chat(chat.chat_id).actions == []

    _set_platform(monkeypatch, "darwin")
    with _agent_client(service) as client:
        accepted = _post(client, token, "chat_needs_action", {"kind": "system_setting", "params": {"setting": "camera"}})
    assert accepted.status_code == 200
    assert accepted.json()["data"]["action"]["params"] == {"setting": "camera"}


# ── The start (handler) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_runs_open_with_fixed_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    action_id = _raise(manager, cid, "full_disk_access")
    _set_platform(monkeypatch, "darwin")
    recorder = _Recorder()
    monkeypatch.setattr(chat_actions_runtime.subprocess, "run", recorder)

    await manager.actions_runtime.start(
        cid, action_id, {"url": "file:///etc/passwd", "setting": "camera"}, is_loopback=True
    )

    assert recorder.calls == [((["open", SYSTEM_SETTING_URLS["full_disk_access"]],), {"check": True, "timeout": 10})]
    current = manager.get_action(cid, action_id)
    assert current is not None and current.state == "done"


@pytest.mark.asyncio
async def test_start_remote_refused_and_action_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    action_id = _raise(manager, cid)
    _set_platform(monkeypatch, "darwin")
    recorder = _Recorder()
    monkeypatch.setattr(chat_actions_runtime.subprocess, "run", recorder)

    with pytest.raises(ChatActionError) as excinfo:
        await manager.actions_runtime.start(cid, action_id, {}, is_loopback=False)

    assert excinfo.value.reason == "not_local"
    assert recorder.calls == []
    current = manager.get_action(cid, action_id)
    assert current is not None
    assert current.state == "open"
    assert current.error == ""


def test_forged_remote_start_route_is_400_not_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    action_id = _raise(manager, cid)
    _set_platform(monkeypatch, "darwin")
    recorder = _Recorder()
    monkeypatch.setattr(chat_actions_runtime.subprocess, "run", recorder)

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/chats/{chat_id}/actions/{action_id}/start", chat_action_start, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = manager._config
    app.state.project_chat_manager = manager
    client = TestClient(app, base_url=_ORIGIN, raise_server_exceptions=False)
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))

    response = client.post(f"/api/chats/{cid}/actions/{action_id}/start", headers=_HEADERS, json={})

    assert response.status_code == 400
    assert response.json()["error"] == "not_local"
    assert response.json()["action"]["state"] == "open"
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_start_off_macos_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    action_id = _raise(manager, cid)
    _set_platform(monkeypatch, "win32")
    recorder = _Recorder()
    monkeypatch.setattr(chat_actions_runtime.subprocess, "run", recorder)

    await manager.actions_runtime.start(cid, action_id, {}, is_loopback=True)

    current = manager.get_action(cid, action_id)
    assert current is not None
    assert current.state == "failed"
    assert current.error == SYSTEM_SETTING_OFF_MAC_ERROR
    assert recorder.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["open"], output="SECRET", stderr="SECRET"),
        FileNotFoundError("SECRET"),
    ],
)
async def test_start_open_error_fails_with_static_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    action_id = _raise(manager, cid)
    _set_platform(monkeypatch, "darwin")
    monkeypatch.setattr(chat_actions_runtime.subprocess, "run", _Recorder(error))

    await manager.actions_runtime.start(cid, action_id, {}, is_loopback=True)

    current = manager.get_action(cid, action_id)
    assert current is not None
    assert current.state == "failed"
    assert current.error == SYSTEM_SETTING_OPEN_ERROR
    assert "SECRET" not in current.error


@pytest.mark.asyncio
async def test_start_from_failed_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    action_id = _raise(manager, cid)
    _set_platform(monkeypatch, "darwin")
    monkeypatch.setattr(chat_actions_runtime.subprocess, "run", _Recorder(FileNotFoundError("SECRET")))
    await manager.actions_runtime.start(cid, action_id, {}, is_loopback=True)
    assert manager.get_action(cid, action_id).state == "failed"  # type: ignore[union-attr]

    good = _Recorder()
    monkeypatch.setattr(chat_actions_runtime.subprocess, "run", good)
    await manager.actions_runtime.start(cid, action_id, {}, is_loopback=True)

    assert len(good.calls) == 1
    current = manager.get_action(cid, action_id)
    assert current is not None
    assert current.state == "done"
    assert current.error == ""


def test_wake_prompt_names_the_pane(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    action_id = _raise(manager, cid, "screen_recording")
    stored = manager.get_action(cid, action_id)
    assert stored is not None

    prompt = ACTION_HANDLERS["system_setting"].wake_prompt(stored)

    assert "Screen & System Audio Recording" in prompt
    assert "Try the step that was blocked again" in prompt
