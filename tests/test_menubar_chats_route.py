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


def test_a_chat_needing_attention_is_never_truncated_out_of_the_list() -> None:
    # attention_count is counted over every non-archived chat while the list is
    # capped at `limit`. If a stale chat needing attention fell outside the cap,
    # the menu bar badge would show a count with no row explaining it.
    work = SimpleNamespace(workspace="work")
    chats = [
        SimpleNamespace(
            chat_id="stale-but-unread",
            project_id="p-work",
            title="Finished ages ago, never read",
            archived=False,
            last_activity_at="2026-01-01T00:00:00Z",
            last_read_at="2025-12-31T00:00:00Z",
            pending_question="",
        )
    ] + [
        SimpleNamespace(
            chat_id=f"recent-{index}",
            project_id="p-work",
            title=f"Recent {index}",
            archived=False,
            last_activity_at=f"2026-07-{index + 10:02d}T00:00:00Z",
            last_read_at=f"2026-07-{index + 10:02d}T00:00:00Z",
            pending_question="",
        )
        for index in range(12)
    ]
    pcm = SimpleNamespace(
        list_chats=lambda: chats,
        get_project=lambda project_id: work if project_id == "p-work" else None,
    )

    payload = _client(pcm=pcm).get("/api/menubar-chats?limit=5").json()

    assert payload["attention_count"] == 1
    ids = [row["chat_id"] for row in payload["chats"]]
    assert len(ids) == 5
    assert ids[0] == "stale-but-unread", ids
    # Recency order still holds among the chats that need nothing.
    assert ids[1:] == ["recent-11", "recent-10", "recent-9", "recent-8"], ids
    flagged = [row for row in payload["chats"] if row["unread"] or row["needs_input"]]
    assert len(flagged) == payload["attention_count"]


def test_nested_delegate_completion_does_not_create_a_second_unread_row() -> None:
    work = SimpleNamespace(workspace="work")
    chats = [
        SimpleNamespace(
            chat_id="supervisor",
            project_id="p-work",
            title="Supervisor",
            archived=False,
            last_activity_at="2026-08-11T22:00:00Z",
            last_read_at="2026-08-11T21:00:00Z",
            pending_question="",
            spawned_from_chat_id="",
        ),
        SimpleNamespace(
            chat_id="delegate",
            project_id="p-work",
            title="Internal task",
            archived=False,
            last_activity_at="2026-08-11T22:01:00Z",
            last_read_at="2026-08-11T21:00:00Z",
            pending_question="",
            spawned_from_chat_id="supervisor",
        ),
    ]
    pcm = SimpleNamespace(
        list_chats=lambda: chats,
        get_project=lambda project_id: work if project_id == "p-work" else None,
    )

    payload = _client(pcm=pcm).get("/api/menubar-chats").json()

    assert payload["attention_count"] == 1
    rows = {row["chat_id"]: row for row in payload["chats"]}
    assert rows["supervisor"]["unread"] is True
    assert rows["delegate"]["unread"] is False
