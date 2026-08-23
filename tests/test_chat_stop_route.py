"""HTTP fallback for stopping an in-flight turn (ciao/web/routes_api.py:chat_stop).

The websocket `stop` message is the normal path, but it silently drops if the
chat's socket happens to be mid-reconnect when the user clicks Stop -- with no
error and no retry, leaving the turn unstoppable from the UI. This route lets
the PWA reach `stop_chat` over plain HTTP instead.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_stop


def _manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "archives"),
        path=runtime / "web_projects.json",
    )


def _request(chat_id: str, app) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": f"/api/chats/{chat_id}/stop",
        "headers": [],
        "app": app,
        "path_params": {"chat_id": chat_id},
        "query_string": b"",
    })


def _app(pcm: ProjectChatManager) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(
        config=pcm._config,
        project_chat_manager=pcm,
    ))


def test_chat_stop_reaches_stop_chat_without_a_socket(tmp_path: Path, monkeypatch) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("stop-route", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="stop-test")

    calls: list[tuple[str, str]] = []

    async def fake_stop_chat(chat_id: str, *, by: str = "user") -> bool:
        calls.append((chat_id, by))
        return True

    monkeypatch.setattr(pcm, "stop_chat", fake_stop_chat)

    response = asyncio.run(chat_stop(_request(chat.chat_id, _app(pcm))))
    body = json.loads(response.body)

    # The route is the human's Stop button, so it must attribute the stop to
    # the user: that is what keeps a stopped delegate from waking (and
    # falsely alarming) its supervisor.
    assert calls == [(chat.chat_id, "user")]
    assert body == {"stopped": True}


def test_chat_stop_unknown_chat_returns_404(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    response = asyncio.run(chat_stop(_request("chat-missing", _app(pcm))))
    assert response.status_code == 404
