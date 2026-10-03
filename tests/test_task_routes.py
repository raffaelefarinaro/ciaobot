"""``/api/tasks*``: the session-authenticated board routes (#1021, B3).

A real config with two workspaces, a real control plane, and the signed session
cookie the ``AuthMiddleware`` checks — so these are the routes as the browser
calls them, not the handlers called directly. What is asserted is the transport
contract and the three properties only this surface can be asked about: a
request may name a workspace but never a root, a write must present the
revision it read, and completing works here because this is the user's session.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import CiaoControlPlane
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_tasks import (
    task_complete,
    task_create,
    task_delete,
    task_list,
    task_update,
)


class _Pcm:
    """One project in `personal`, none in `work`, and a vault-root resolver."""

    def __init__(self, config: CiaoConfig) -> None:
        self.config = config
        self.projects = {
            "project-home": SimpleNamespace(
                project_id="project-home", name="Home", workspace="personal"
            ),
            "project-work": SimpleNamespace(
                project_id="project-work", name="Work", workspace="work"
            ),
        }

    def _workspace_vault_root(self, workspace: str) -> Path:
        return self.config.workspace_vault_root(workspace)

    def get_project(self, project_id: str):
        return self.projects.get(project_id)

    def list_projects(self, workspace: str | None = None):
        return [
            project
            for project in self.projects.values()
            if workspace is None or project.workspace == workspace
        ]

    def get_chat(self, _chat_id: str):
        return None

    def get_active_stream(self, _chat_id: str):
        return None


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A session-guarded app over a real config with `personal` and `work`."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr(
        "ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None
    )
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        pwa_auth_required=True,
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    plane = CiaoControlPlane(
        config,
        project_chat_manager=_Pcm(config),
        schedule_manager=SimpleNamespace(),
    )
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[
            # Literal `complete` first, exactly as app.py registers it.
            Route("/api/tasks", task_list, methods=["GET"]),
            Route("/api/tasks", task_create, methods=["POST"]),
            Route("/api/tasks/{task_id}/complete", task_complete, methods=["POST"]),
            Route("/api/tasks/{task_id}", task_update, methods=["PATCH"]),
            Route("/api/tasks/{task_id}", task_delete, methods=["DELETE"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    app.state.mcp_service = SimpleNamespace(control_plane=plane)
    with TestClient(app) as client:
        yield client, {SESSION_COOKIE: serializer.dumps({"user": "owner"})}, config


def _create(client: TestClient, cookies: dict[str, str], **body) -> dict:
    payload = {"workspace": "personal", **body}
    response = client.post("/api/tasks", json=payload, cookies=cookies)
    assert response.status_code == 201, response.text
    return response.json()["task"]


# ── Authorization and the workspace check ───────────────────────────────


def test_the_routes_need_the_session_cookie(world) -> None:
    client, _cookies, _config = world
    anonymous = client.get("/api/tasks?workspace=personal")
    assert anonymous.status_code == 401


def test_an_unknown_or_missing_workspace_is_a_400(world) -> None:
    client, cookies, _config = world
    for query in ("", "?workspace=", "?workspace=somewhere-else", "?workspace=../etc"):
        response = client.get(f"/api/tasks{query}", cookies=cookies)
        assert response.status_code == 400, query
        assert response.json()["error"]["code"] == "workspace_required"

    # A write refuses it in the body, the same way.
    assert client.post("/api/tasks", json={"title": "x"}, cookies=cookies).status_code == 400
    assert (
        client.post(
            "/api/tasks",
            json={"workspace": "somewhere-else", "title": "x"},
            cookies=cookies,
        ).status_code
        == 400
    )


def test_the_board_is_per_workspace(world) -> None:
    client, cookies, _config = world
    mine = _create(client, cookies, title="Personal task")
    theirs = _create(client, cookies, title="Work task", workspace="work")

    personal = client.get("/api/tasks?workspace=personal", cookies=cookies).json()
    work = client.get("/api/tasks?workspace=work", cookies=cookies).json()
    assert [row["id"] for row in personal["tasks"]] == [mine["id"]]
    assert [row["id"] for row in work["tasks"]] == [theirs["id"]]


def test_a_task_id_from_another_workspace_is_a_404(world) -> None:
    client, cookies, _config = world
    mine = _create(client, cookies, title="Personal task")
    theirs = _create(client, cookies, title="Work task", workspace="work")

    response = client.patch(
        f"/api/tasks/{mine['id']}",
        json={"workspace": "work", "expected_revision": mine["revision"], "title": "edited"},
        cookies=cookies,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"

    unknown = client.patch(
        f"/api/tasks/{'f' * 32}",
        json={"workspace": "personal", "expected_revision": "x", "title": "edited"},
        cookies=cookies,
    )
    assert unknown.status_code == 404
    assert theirs["id"] != mine["id"]


# ── create → update → complete ──────────────────────────────────────────


def test_a_create_update_complete_round_trip(world) -> None:
    client, cookies, config = world
    created = _create(
        client,
        cookies,
        title="Ship the board",
        body="Describe it.",
        project_id="Home",
        due="2026-10-20",
    )
    assert created["status"] == "backlog"
    assert created["project_id"] == "project-home"
    assert created["due"] == "2026-10-20"
    assert created["body"] == "Describe it."
    on_disk = (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Tasks"
        / f"{created['id']}.md"
    )
    assert on_disk.is_file()

    updated = client.patch(
        f"/api/tasks/{created['id']}",
        json={
            "workspace": "personal",
            "expected_revision": created["revision"],
            "title": "Ship the task board",
            "body": "Describe the board.",
        },
        cookies=cookies,
    )
    assert updated.status_code == 200, updated.text
    revised = updated.json()["task"]
    assert revised["title"] == "Ship the task board"
    assert revised["body"] == "Describe the board."
    assert revised["revision"] != created["revision"]
    # A field nobody sent is untouched.
    assert revised["due"] == "2026-10-20"

    # This is the user's own session, so completion is theirs to make.
    done = client.post(
        f"/api/tasks/{created['id']}/complete",
        json={"workspace": "personal", "expected_revision": revised["revision"]},
        cookies=cookies,
    )
    assert done.status_code == 200, done.text
    assert done.json()["task"]["status"] == "done"

    listed = client.get("/api/tasks?workspace=personal", cookies=cookies).json()
    assert [row["status"] for row in listed["tasks"]] == ["done"]


def test_delete_removes_the_record(world) -> None:
    client, cookies, config = world
    created = _create(client, cookies, title="A throwaway task")
    response = client.request(
        "DELETE",
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal", "expected_revision": created["revision"]},
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert not (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Tasks"
        / f"{created['id']}.md"
    ).exists()
    assert client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"] == []


# ── Revisions are required, and a stale one writes nothing ──────────────


def test_a_write_without_an_expected_revision_is_a_400(world) -> None:
    client, cookies, _config = world
    created = _create(client, cookies, title="Read before you write")

    patch = client.patch(
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal", "title": "written without a read"},
        cookies=cookies,
    )
    assert patch.status_code == 400
    assert patch.json()["error"]["code"] == "expected_revision_required"

    delete = client.request(
        "DELETE",
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert delete.status_code == 400
    assert delete.json()["error"]["code"] == "expected_revision_required"

    complete = client.post(
        f"/api/tasks/{created['id']}/complete",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert complete.status_code == 400
    assert complete.json()["error"]["code"] == "expected_revision_required"

    # Nothing was written or removed by any of the three.
    rows = client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"]
    assert len(rows) == 1
    assert rows[0]["revision"] == created["revision"]
    assert rows[0]["title"] == "Read before you write"
    assert rows[0]["status"] == "backlog"


def test_a_stale_revision_is_a_409_and_writes_nothing(world) -> None:
    client, cookies, _config = world
    created = _create(client, cookies, title="Original title")
    fresh = client.patch(
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal", "expected_revision": created["revision"], "due": "2026-10-05"},
        cookies=cookies,
    ).json()["task"]

    stale = client.patch(
        f"/api/tasks/{created['id']}",
        json={
            "workspace": "personal",
            "expected_revision": created["revision"],
            "title": "written from a stale read",
        },
        cookies=cookies,
    )
    assert stale.status_code == 409
    body = stale.json()["error"]
    assert body["code"] == "task_revision_conflict"
    assert body["retryable"] is True

    rows = client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"]
    assert rows[0]["revision"] == fresh["revision"]
    assert rows[0]["title"] == "Original title"

    assert (
        client.request(
            "DELETE",
            f"/api/tasks/{created['id']}",
            json={"workspace": "personal", "expected_revision": created["revision"]},
            cookies=cookies,
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/tasks/{created['id']}/complete",
            json={"workspace": "personal", "expected_revision": created["revision"]},
            cookies=cookies,
        ).status_code
        == 409
    )
    assert len(client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"]) == 1


# ── Body shape ──────────────────────────────────────────────────────────


def test_a_bad_body_is_refused_before_anything_is_written(world) -> None:
    client, cookies, _config = world
    not_json = client.post(
        "/api/tasks",
        content=b"[1, 2]",
        headers={"content-type": "application/json"},
        cookies=cookies,
    )
    assert not_json.status_code == 400
    assert not_json.json()["error"]["code"] == "invalid_json"

    untitled = client.post(
        "/api/tasks", json={"workspace": "personal", "title": "  "}, cookies=cookies
    )
    assert untitled.status_code == 400
    assert untitled.json()["error"]["code"] == "title_required"

    created = _create(client, cookies, title="A task")
    unknown_field = client.patch(
        f"/api/tasks/{created['id']}",
        json={
            "workspace": "personal",
            "expected_revision": created["revision"],
            "chat_id": "chat-1",
        },
        cookies=cookies,
    )
    assert unknown_field.status_code == 400
    assert unknown_field.json()["error"]["code"] == "invalid_task_field"

    nothing = client.patch(
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal", "expected_revision": created["revision"]},
        cookies=cookies,
    )
    assert nothing.status_code == 400
    assert nothing.json()["error"]["code"] == "nothing_to_change"


def test_a_foreign_project_is_a_400(world) -> None:
    client, cookies, _config = world
    response = client.post(
        "/api/tasks",
        json={"workspace": "personal", "title": "Bound elsewhere", "project_id": "project-work"},
        cookies=cookies,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "project_not_found"
    assert client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"] == []


def test_an_unavailable_control_plane_is_a_503(tmp_path: Path, monkeypatch) -> None:
    """First-run bootstrap has no control plane; that is a 503, not a 400."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")},
    )
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[Route("/api/tasks", task_list, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.mcp_service = None
    with TestClient(app) as client:
        response = client.get(
            "/api/tasks?workspace=personal",
            cookies={SESSION_COOKIE: serializer.dumps({"user": "owner"})},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"