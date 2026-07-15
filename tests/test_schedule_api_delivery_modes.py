from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.schedules import ScheduleManager, ScheduleStore
from ciao.sessions import StateStore
from ciao.web.routes_api import create_schedule, run_schedule_now, schedule_detail


class _Config:
    def __init__(self, workspaces: tuple[str, ...] = ("personal", "work")) -> None:
        self._workspaces = workspaces

    def workspace_names(self) -> list[str]:
        return list(self._workspaces)


class _ProjectChats:
    def __init__(self, workspaces: tuple[str, ...] = ("personal", "work")) -> None:
        self._config = _Config(workspaces)

    def schedule_default_model(self, project_id: str) -> str:
        return "sonnet"

    def get_project(self, project_id: str):
        return None

    def get_chat(self, chat_id: str):
        return None


def _make_client(
    tmp_path: Path, *, workspaces: tuple[str, ...] = ("personal", "work")
) -> TestClient:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    store = ScheduleStore(runtime)
    manager = ScheduleManager(store=store)
    state = StateStore(runtime / "state.json", tmp_path, runtime / "media")
    app = Starlette(
        routes=[
            Route("/api/schedules", create_schedule, methods=["POST"]),
            Route("/api/schedules/{schedule_id}", schedule_detail, methods=["PATCH"]),
        ]
    )
    app.state.schedule_manager = manager
    app.state.state_store = state
    app.state.project_chat_manager = _ProjectChats(workspaces)
    return TestClient(app)


def _make_run_now_client(tmp_path: Path) -> tuple[TestClient, str]:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    store = ScheduleStore(runtime)
    manager = ScheduleManager(store=store)
    entry = manager.create(
        daily_time_utc="01:00",
        prompt="curate",
        model="sonnet",
        mode="print",
        chat_id=0,
        frequency="manual",
    )
    app = Starlette(
        routes=[
            Route(
                "/api/schedule-run/{schedule_id}",
                run_schedule_now,
                methods=["POST"],
            ),
        ]
    )
    app.state.schedule_manager = manager
    return TestClient(app), entry.schedule_id


def test_run_schedule_now_missing_schedule_returns_404(tmp_path: Path) -> None:
    client, _ = _make_run_now_client(tmp_path)
    response = client.post("/api/schedule-run/does-not-exist")
    assert response.status_code == 404, response.text


def test_run_schedule_now_paused_instance_returns_409() -> None:
    class _PausedScheduleManager:
        async def dispatch_now(self, schedule_id: str) -> dict:
            raise RuntimeError("Instance is paused; cannot run schedule now.")

    app = Starlette(
        routes=[
            Route(
                "/api/schedule-run/{schedule_id}",
                run_schedule_now,
                methods=["POST"],
            ),
        ]
    )
    app.state.schedule_manager = _PausedScheduleManager()

    response = TestClient(app).post("/api/schedule-run/sched-1")

    assert response.status_code == 409
    assert response.json()["error"] == "Instance is paused; cannot run schedule now."


def test_create_schedule_rejects_unknown_archive_policy(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post(
        "/api/schedules",
        json={
            "time": "01:00",
            "prompt": "curate",
            "frequency": "daily",
            "archive_policy": "sometimes-maybe",
        },
    )
    assert response.status_code == 400
    assert "archive_policy" in response.json()["error"]


def test_patch_schedule_updates_archive_policy(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    created = client.post(
        "/api/schedules",
        json={
            "time": "01:00",
            "prompt": "curate",
            "frequency": "daily",
        },
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["schedule_id"]

    updated = client.patch(
        f"/api/schedules/{schedule_id}",
        json={"archive_policy": "auto"},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["archive_policy"] == "auto"


def test_create_schedule_preserves_configured_workspace(tmp_path: Path) -> None:
    client = _make_client(tmp_path, workspaces=("home", "client"))

    response = client.post(
        "/api/schedules",
        json={
            "time": "01:00",
            "prompt": "client review",
            "frequency": "daily",
            "workspace": "client",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["workspace"] == "client"


def test_create_schedule_keeps_empty_model_as_dynamic_inheritance(
    tmp_path: Path,
) -> None:
    client = _make_client(tmp_path)

    response = client.post(
        "/api/schedules",
        json={
            "time": "01:00",
            "prompt": "inherit workspace routing",
            "frequency": "daily",
            "web_project_id": "proj-personal",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["model"] == ""
    assert response.json()["provider"] == ""


def test_patch_schedule_empty_model_restores_dynamic_inheritance(
    tmp_path: Path,
) -> None:
    client = _make_client(tmp_path)
    created = client.post(
        "/api/schedules",
        json={
            "time": "01:00",
            "prompt": "override then inherit",
            "frequency": "daily",
            "model": "opus",
            "provider": "claude",
        },
    )
    assert created.status_code == 201, created.text

    updated = client.patch(
        f"/api/schedules/{created.json()['schedule_id']}",
        json={"model": "", "provider": ""},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["model"] == ""
    assert updated.json()["provider"] == ""


def test_patch_schedule_preserves_configured_workspace(tmp_path: Path) -> None:
    client = _make_client(tmp_path, workspaces=("home", "client"))
    created = client.post(
        "/api/schedules",
        json={
            "time": "01:00",
            "prompt": "client review",
            "frequency": "daily",
            "workspace": "home",
        },
    )
    assert created.status_code == 201, created.text

    updated = client.patch(
        f"/api/schedules/{created.json()['schedule_id']}",
        json={"workspace": "client"},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["workspace"] == "client"
