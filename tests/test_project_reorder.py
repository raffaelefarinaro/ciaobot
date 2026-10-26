"""Tests for drag-to-reorder project ordering (ProjectChatManager.reorder_projects)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager, ProjectOrderError
from ciao.web.routes_api import reorder_projects as reorder_route


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _ids(pcm: ProjectChatManager, workspace: str = "personal") -> list[str]:
    return pcm.reorderable_project_ids(workspace)


def _general(pcm: ProjectChatManager, workspace: str = "personal"):
    return next(p for p in pcm.list_projects(workspace) if p.name == "General")


def test_reorder_rewrites_order_for_permutation_and_keeps_general_first(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    a = pcm.create_project("Alpha", workspace="personal")
    b = pcm.create_project("Beta", workspace="personal")
    c = pcm.create_project("Gamma", workspace="personal")
    expected = _ids(pcm)
    assert expected == [a.project_id, b.project_id, c.project_id]

    result = pcm.reorder_projects(
        "personal", [c.project_id, a.project_id, b.project_id], expected
    )

    assert [p.name for p in pcm.list_projects("personal")] == ["General", "Gamma", "Alpha", "Beta"]
    assert [p.name for p in result] == ["General", "Gamma", "Alpha", "Beta"]


def test_reorder_pinned_rows_keep_their_order_values(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    a = pcm.create_project("Alpha", workspace="personal")
    b = pcm.create_project("Beta", workspace="personal")
    general = _general(pcm)
    before = general.order

    pcm.reorder_projects("personal", [b.project_id, a.project_id], _ids(pcm))

    assert general.order == before
    assert pcm.list_projects("personal")[0].name == "General"


def test_reorder_rejects_missing_unknown_foreign_pinned_and_duplicate_ids(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    a = pcm.create_project("Alpha", workspace="personal")
    b = pcm.create_project("Beta", workspace="personal")
    foreign = pcm.create_project("WorkOne", workspace="work")
    general = _general(pcm)
    expected = _ids(pcm)
    bad_orders = {
        "missing": [a.project_id],
        "unknown": [a.project_id, b.project_id, "nope"],
        "foreign": [a.project_id, foreign.project_id],
        "pinned": [general.project_id, a.project_id, b.project_id],
        "duplicate": [a.project_id, a.project_id],
    }
    for label, order in bad_orders.items():
        with pytest.raises(ProjectOrderError) as info:
            pcm.reorder_projects("personal", order, expected)
        assert info.value.code == "project_order_invalid", label
    assert _ids(pcm) == expected


def test_reorder_stale_expected_order_conflicts_and_leaves_state(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    a = pcm.create_project("Alpha", workspace="personal")
    b = pcm.create_project("Beta", workspace="personal")
    orders = {p.project_id: p.order for p in pcm.list_projects("personal")}

    with pytest.raises(ProjectOrderError) as info:
        pcm.reorder_projects(
            "personal", [b.project_id, a.project_id], [b.project_id, a.project_id]
        )

    assert info.value.code == "project_order_conflict"
    assert info.value.current == [a.project_id, b.project_id]
    assert {p.project_id: p.order for p in pcm.list_projects("personal")} == orders


def test_reorder_excludes_memory_project_from_reorderable_set(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    a = pcm.create_project("Alpha", workspace="personal")
    memory = pcm.create_project("Memory", workspace="personal")
    memory.kind = "memory"

    assert _ids(pcm) == [a.project_id]
    with pytest.raises(ProjectOrderError) as info:
        pcm.reorder_projects("personal", [a.project_id, memory.project_id], [a.project_id])
    assert info.value.code == "project_order_invalid"


def test_reorder_slots_preserved_with_interleaved_auto_project(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    a = pcm.create_project("Alpha", workspace="personal")
    cli = pcm.create_project("Claude Code CLI", workspace="personal")
    b = pcm.create_project("Beta", workspace="personal")
    a.order, b.order, cli.order = 1, 4, 3

    pcm.reorder_projects("personal", [b.project_id, a.project_id], _ids(pcm))

    assert (b.order, a.order, cli.order) == (1, 4, 3)
    names = [p.name for p in pcm.list_projects("personal")]
    assert names.index("Beta") < names.index("Claude Code CLI") < names.index("Alpha")


def test_reorder_publishes_projects_reordered_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pcm = _make_manager(tmp_path)
    a = pcm.create_project("Alpha", workspace="personal")
    b = pcm.create_project("Beta", workspace="personal")
    events: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", events.append)

    result = pcm.reorder_projects("personal", [b.project_id, a.project_id], _ids(pcm))

    assert events == [
        {
            "type": "projects_reordered",
            "workspace": "personal",
            "order": [p.project_id for p in result],
        }
    ]


@pytest.fixture()
def route_client(tmp_path: Path):
    pcm = _make_manager(tmp_path)
    app = Starlette(routes=[Route("/api/projects/reorder", reorder_route, methods=["POST"])])
    app.state.project_chat_manager = pcm
    with TestClient(app) as client:
        yield client, pcm


def test_reorder_route_rejects_malformed_bodies(route_client) -> None:
    client, _pcm = route_client
    bodies = [
        {},
        {"workspace": "personal", "order": []},
        {"workspace": "personal", "expected_order": []},
        {"workspace": "", "order": [], "expected_order": []},
        {"workspace": "personal", "order": [1], "expected_order": []},
        {"workspace": "personal", "order": [], "expected_order": "x"},
    ]
    for body in bodies:
        response = client.post("/api/projects/reorder", json=body)
        assert response.status_code == 400, body


def test_reorder_route_maps_invalid_and_conflict(route_client) -> None:
    client, pcm = route_client
    a = pcm.create_project("Alpha", workspace="personal")
    b = pcm.create_project("Beta", workspace="personal")

    invalid = client.post(
        "/api/projects/reorder",
        json={"workspace": "personal", "order": [a.project_id], "expected_order": _ids(pcm)},
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "project_order_invalid"

    conflict = client.post(
        "/api/projects/reorder",
        json={
            "workspace": "personal",
            "order": [b.project_id, a.project_id],
            "expected_order": [b.project_id, a.project_id],
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "project_order_conflict"
    assert conflict.json()["order"] == [a.project_id, b.project_id]


def test_reorder_route_returns_the_workspace_list(route_client) -> None:
    client, pcm = route_client
    a = pcm.create_project("Alpha", workspace="personal")
    b = pcm.create_project("Beta", workspace="personal")

    response = client.post(
        "/api/projects/reorder",
        json={
            "workspace": "personal",
            "order": [b.project_id, a.project_id],
            "expected_order": [a.project_id, b.project_id],
        },
    )

    assert response.status_code == 200
    assert [p["name"] for p in response.json()] == ["General", "Beta", "Alpha"]
