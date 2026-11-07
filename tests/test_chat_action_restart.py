"""The `restart` needs-you kind: its validator and its handler.

A `restart` card points the user at the Settings control that restarts or
updates Ciaobot. The engine never restarts from it: the handler only records
the card, and the restart itself stays behind the Settings confirmation.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.chat_actions import ChatActionError, validate_params
from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import chat_actions_runtime
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
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def test_validate_restart_reason() -> None:
    assert validate_params("restart", {"reason": "config"}) == {"reason": "config"}
    assert validate_params("restart", {"reason": "update"}) == {"reason": "update"}

    for raw in ({"reason": "mcp"}, {"reason": "now"}, {"reason": ""}):
        with pytest.raises(ChatActionError) as excinfo:
            validate_params("restart", raw)
        assert excinfo.value.reason == "invalid_param"

    with pytest.raises(ChatActionError) as excinfo:
        validate_params("restart", {"reason": None})
    assert excinfo.value.reason == "invalid_type"


@pytest.mark.asyncio
async def test_restart_handler_never_restarts(tmp_path: Path) -> None:
    restarts: list[object] = []

    def request_restart(*args: object, **kwargs: object) -> None:
        restarts.append((args, kwargs))

    manager = _make_manager(tmp_path)
    project = manager.create_project("Restarts", workspace="work")
    chat = manager.create_chat(project.project_id, title="Restart it", provider="claude")
    action = manager.raise_action(chat.chat_id, "restart", {"reason": "config"}, origin="agent")
    assert action is not None
    action_id = str(action["id"])

    manager.actions_runtime.bind_app_state(SimpleNamespace(request_restart=request_restart))
    payload: Any = await manager.actions_runtime.start(
        chat.chat_id, action_id, {}, is_loopback=True
    )

    assert restarts == []
    assert payload["state"] == "open"
    current = manager.get_action(chat.chat_id, action_id)
    assert current is not None
    assert current.state == "open"
    assert "request_restart" not in inspect.getsource(chat_actions_runtime.RestartHandler)
