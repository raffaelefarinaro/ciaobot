"""Compact startup catalogs and on-demand archive metadata."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.project_chats import ChatInfo
from ciao.web.routes_api import chat_detail, list_all_chats, project_chats


@pytest.fixture
def catalog():
    archived = [ChatInfo(chat_id=f"old-{i:03d}", project_id="p1", title=f"Archive {i}",
                         archived=True, created_at="2026-10-01", archive_path=f"chats/{i}.md")
                for i in range(23)]
    active = ChatInfo(chat_id="active", project_id="p1", title="Current")
    memory = ChatInfo(chat_id="memory", project_id="p1", helper={"kind": "memory_pass", "source_chat_id": "old-022"})
    rows = [active, memory, *archived]
    manager = SimpleNamespace(
        list_chats=lambda project_id=None: [c for c in rows if project_id is None or c.project_id == project_id],
        get_chat=lambda chat_id: next((c for c in rows if c.chat_id == chat_id), None),
        get_project=lambda project_id: object() if project_id == "p1" else None,
        is_session_local=Mock(return_value=True),
    )
    app = Starlette(routes=[Route("/api/chats", list_all_chats),
                            Route("/api/chats/{chat_id}", chat_detail),
                            Route("/api/projects/{project_id}/chats", project_chats)])
    app.state.project_chat_manager = manager
    with TestClient(app) as client:
        yield client, manager


def test_startup_omits_archives_but_preserves_memory_source(catalog):
    client, manager = catalog
    rows = client.get("/api/chats?active_only=1").json()
    assert [c["chat_id"] for c in rows] == ["active", "memory"]
    assert rows[1]["memory_source"]["title"] == "Archive 22"
    assert rows[1]["memory_source"]["archive_path"] == "chats/22.md"
    assert set(rows[1]["memory_source"]) == {"chat_id", "title", "created_at", "last_activity_at", "archive_path"}
    manager.is_session_local.assert_not_called()


def test_archives_are_paginated_with_stable_order_and_no_session_probes(catalog):
    client, manager = catalog
    first = client.get("/api/projects/p1/chats?archived=1&limit=10").json()
    second = client.get("/api/projects/p1/chats?archived=1&offset=10&limit=10").json()
    assert first["total"] == second["total"] == 23
    assert len(first["items"]) == len(second["items"]) == 10
    assert first["items"][0]["chat_id"] == "old-022"
    assert second["items"][0]["chat_id"] == "old-012"
    assert not ({c["chat_id"] for c in first["items"]} & {c["chat_id"] for c in second["items"]})
    assert set(first["items"][0]) == {"chat_id", "title", "created_at", "archive_path"}
    manager.is_session_local.assert_not_called()


@pytest.mark.parametrize("query", ["offset=-1", "limit=0", "limit=101", "offset=x", "limit=x"])
def test_invalid_archive_pagination(catalog, query):
    client, _ = catalog
    assert client.get(f"/api/projects/p1/chats?archived=1&{query}").status_code == 400


def test_archive_metadata_deep_link_and_missing_ids(catalog):
    client, manager = catalog
    row = client.get("/api/chats/old-022").json()
    assert row["archived"] is True
    assert row["local"] is True
    manager.is_session_local.assert_not_called()
    assert client.get("/api/chats/missing").status_code == 404
    assert client.get("/api/projects/missing/chats?archived=1").status_code == 404
