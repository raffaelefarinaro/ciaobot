from __future__ import annotations

import json
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import menubar_chats_endpoint


def _client(pcm=None) -> TestClient:
    app = Starlette(routes=[Route("/api/menubar-chats", menubar_chats_endpoint, methods=["GET"])])
    app.state.project_chat_manager = pcm
    return TestClient(app)


def test_menubar_chats_endpoint_without_manager_returns_empty() -> None:
    resp = _client(pcm=None).get("/api/menubar-chats")
    assert resp.status_code == 200
    assert resp.json() == {"chats": [], "attention_count": 0}


def test_menubar_chats_endpoint_filters_sorts_and_flags() -> None:
    personal = SimpleNamespace(workspace="personal")
    work = SimpleNamespace(workspace="work")
    chats = [
        SimpleNamespace(
            chat_id="old",
            project_id="p-personal",
            title="Older",
            archived=False,
            last_activity_at="2026-07-01T00:00:00Z",
            last_read_at="2026-07-01T00:00:00Z",
            pending_question="",
        ),
        SimpleNamespace(
            chat_id="working",
            project_id="p-work",
            title="Needs eyes",
            archived=False,
            last_activity_at="2026-07-27T12:00:00Z",
            last_read_at="2026-07-27T11:00:00Z",
            pending_question="",
        ),
        SimpleNamespace(
            chat_id="blocked",
            project_id="p-personal",
            title="Waiting",
            archived=False,
            last_activity_at="2026-07-27T11:00:00Z",
            last_read_at="2026-07-27T11:30:00Z",
            pending_question=json.dumps({"questions": [{"id": "q1"}]}),
        ),
        SimpleNamespace(
            chat_id="archived",
            project_id="p-work",
            title="Gone",
            archived=True,
            last_activity_at="2026-07-28T00:00:00Z",
            last_read_at="",
            pending_question="",
        ),
        SimpleNamespace(
            chat_id="orphan",
            project_id="missing",
            title="Orphan",
            archived=False,
            last_activity_at="2026-07-29T00:00:00Z",
            last_read_at="",
            pending_question="",
        ),
    ]
    pcm = SimpleNamespace(
        list_chats=lambda: chats,
        get_project=lambda project_id: {
            "p-personal": personal,
            "p-work": work,
        }.get(project_id),
    )

    resp = _client(pcm=pcm).get("/api/menubar-chats?limit=10")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["attention_count"] == 2
    assert [row["chat_id"] for row in payload["chats"]] == ["working", "blocked", "old"]
    assert payload["chats"][0] == {
        "chat_id": "working",
        "title": "Needs eyes",
        "workspace": "work",
        "last_activity_at": "2026-07-27T12:00:00Z",
        "unread": True,
        "needs_input": False,
    }
    assert payload["chats"][1]["needs_input"] is True
    assert payload["chats"][1]["unread"] is False
