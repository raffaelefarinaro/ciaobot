from __future__ import annotations

import json
from pathlib import Path
import pytest
from typing import AsyncGenerator

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.provider_subchats import ProviderSubchatManager, ProviderRoute
from ciao.models import AssistantTextDelta, ResultEvent, StreamEvent
from ciao.web.routes_api import (
    chat_provider_subchats_list,
    chat_provider_subchats_create,
    provider_subchat_events,
    provider_subchat_message,
    provider_subchat_close,
    provider_subchat_cancel,
    provider_subchat_extend,
    provider_subchat_permission_response,
    provider_subchat_question_response,
)


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


def _make_client(pcm: ProjectChatManager, sub_manager: ProviderSubchatManager) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/chats/{chat_id}/provider-subchats", chat_provider_subchats_list, methods=["GET"]),
            Route("/api/chats/{chat_id}/provider-subchats", chat_provider_subchats_create, methods=["POST"]),
            Route("/api/provider-subchats/{subchat_id}/events", provider_subchat_events, methods=["GET"]),
            Route("/api/provider-subchats/{subchat_id}/messages", provider_subchat_message, methods=["POST"]),
            Route("/api/provider-subchats/{subchat_id}/close", provider_subchat_close, methods=["POST"]),
            Route("/api/provider-subchats/{subchat_id}/cancel", provider_subchat_cancel, methods=["POST"]),
            Route("/api/provider-subchats/{subchat_id}/extend", provider_subchat_extend, methods=["POST"]),
            Route("/api/provider-subchats/{subchat_id}/permission-response", provider_subchat_permission_response, methods=["POST"]),
            Route("/api/provider-subchats/{subchat_id}/question-response", provider_subchat_question_response, methods=["POST"]),
        ]
    )
    app.state.project_chat_manager = pcm
    app.state.provider_subchat_manager = sub_manager
    pcm._provider_subchat_manager = sub_manager
    return TestClient(app, raise_server_exceptions=False)


class MockService:
    current_session_id = "test-route-session"

    def __init__(self) -> None:
        self.stopped = False
        self.provider = None

    async def execute_streaming(self, _request) -> AsyncGenerator[StreamEvent, None]:
        yield AssistantTextDelta(type="assistant", text="answer", phase="", parent_tool_use_id="")
        yield ResultEvent(
            type="assistant",
            result="done",
            is_error=False,
            effective_model="gpt-4",
            usage={"input_tokens": "5", "output_tokens": "2"},
            session_id="test-route-session",
        )

    async def stop_active(self) -> bool:
        self.stopped = True
        return True

    async def disconnect(self) -> None:
        pass


def test_provider_subchat_api_routes(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    proj = pcm.create_project("test", workspace="personal")
    chat = pcm.create_chat(proj.project_id, model="opus", provider="claude")

    sub_path = tmp_path / ".runtime" / "provider_subchats.json"
    sub_manager = ProviderSubchatManager(pcm._config, pcm, sub_path)

    client = _make_client(pcm, sub_manager)

    owner = {"provider": "claude", "model": "opus", "label": "Claude"}
    participant = {"provider": "codex", "model": "gpt-4", "label": "Codex"}

    # 1. Create subchat
    response = client.post(
        f"/api/chats/{chat.chat_id}/provider-subchats",
        json={
            "parent_turn_index": 0,
            "owner": owner,
            "participant": participant,
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert "record" in data
    subchat_id = data["record"]["subchat_id"]
    assert data["record"]["status"] == "created"

    # 2. List subchats
    response = client.get(f"/api/chats/{chat.chat_id}/provider-subchats")
    assert response.status_code == 200
    records = response.json()
    assert len(records) == 1
    assert records[0]["subchat_id"] == subchat_id

    # 3. Create subchat with task_prompt (should execute turn)
    # Inject service mock
    service = MockService()
    sub_manager._services[subchat_id] = service  # type: ignore[assignment]

    response = client.post(
        f"/api/provider-subchats/{subchat_id}/messages",
        json={"message": "Do work"}
    )
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == "waiting_owner"
    assert res_data["reply"] == "done"

    # 4. Get events
    response = client.get(f"/api/provider-subchats/{subchat_id}/events")
    assert response.status_code == 200
    events = response.json()
    assert len(events) == 3  # owner message + 1 text delta + 1 result

    # 5. Extend limits
    response = client.post(
        f"/api/provider-subchats/{subchat_id}/extend",
        json={"user_authorized": True}
    )
    assert response.status_code == 200
    assert response.json()["limit_messages_extended"] == 12

    # 6. Close subchat
    response = client.post(f"/api/provider-subchats/{subchat_id}/close")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
