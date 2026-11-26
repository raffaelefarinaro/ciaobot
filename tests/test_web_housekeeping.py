"""Tests for the operator-action housekeeping API routes (P4.5).

The routes live in ``ciao/web/routes_api.py``. Unlike the older route tests,
these assert the REAL route table in ``ciao/web/app.py`` serves every handler,
because a route that is registered nowhere does not exist no matter how well
it is unit-tested.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.web.routes_api import (
    dismiss_housekeeping_action,
    list_housekeeping,
    run_housekeeping_action,
)


class _Config(CiaoConfig):
    def __init__(self, tmp_path: Path, workspaces: tuple[str, ...] = ("personal",)) -> None:
        self.workspace_root = tmp_path
        self.vault_root = tmp_path / "memory-vault"
        self.state_path = tmp_path / ".runtime" / "state.json"
        self._names = list(workspaces)

    def workspace_names(self) -> list[str]:
        return list(self._names)

    def workspace_vault_root(self, name: str) -> Path:
        return self.vault_root / name

    def canonical_workspace_vault_root(self, name: str) -> Path:
        return self.vault_root / name


class _Entry:
    def __init__(self, **kw):
        self.frequency = kw.get("frequency", "daily")
        self.enabled = kw.get("enabled", True)
        self.last_triggered_on = kw.get("last_triggered_on", "")
        self.run_at_date = kw.get("run_at_date", "")
        self.timezone_name = kw.get("timezone_name", "UTC")
        self.schedule_id = kw.get("schedule_id", "sched-1")


class _Store:
    def __init__(self, entries=None):
        self._entries = entries or []

    def list_entries(self, **kwargs):
        return list(self._entries)

    def dispatch_now(self, schedule_id: str) -> dict:
        self._entries = [e for e in self._entries if e.schedule_id != schedule_id]
        return {"schedule_id": schedule_id}


def _config(tmp_path: Path) -> _Config:
    (tmp_path / "personal").mkdir(exist_ok=True)  # the bootstrap workspace's folder
    return _Config(tmp_path)


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _client(config: _Config) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/housekeeping", list_housekeeping, methods=["GET"]),
            Route(
                "/api/housekeeping/{action_id}/run",
                run_housekeeping_action,
                methods=["POST"],
            ),
            Route(
                "/api/housekeeping/{action_id}/dismiss",
                dismiss_housekeeping_action,
                methods=["POST"],
            ),
        ]
    )
    app.state.config = config
    app.state.schedule_manager = _Store()
    return TestClient(app)


def _missed(schedule_id: str = "sched-missed") -> _Entry:
    return _Entry(frequency="once", run_at_date="2026-01-18", schedule_id=schedule_id)


def _starred(tmp_path: Path) -> None:
    """Silence the GitHub-star nudge so exact action lists stay deterministic."""
    runtime = _runtime(tmp_path)
    (runtime / "star-receipt.json").write_text(
        json.dumps({"status": "starred", "at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )


def test_get_returns_actions_payload(tmp_path: Path) -> None:
    _starred(tmp_path)
    client = _client(_config(tmp_path))
    client.app.state.schedule_manager = _Store([_missed()])
    resp = client.get("/api/housekeeping")
    assert resp.status_code == 200
    data = resp.json()
    ids = [a["id"] for a in data["actions"]]
    assert ids == ["missed-schedules"]
    # Every action carries the run/chat surface the client needs.
    assert data["actions"][0]["run_label"]


def test_failed_run_keeps_same_id_with_failure_detail(tmp_path: Path) -> None:
    # A run whose condition persists returns the same id with its detail
    # replaced by the failure text — never silently re-offered as a fresh tile.
    _starred(tmp_path)
    client = _client(_config(tmp_path))
    client.app.state.package_status_fetcher = lambda: {
        "update_available": True,
        "latest_version": "9.9.9",
        "current_version": "0.1.0",
    }
    with patch(
        "ciao.package_version.update_package",
        return_value={"ok": False, "error": "This checkout cannot self-update."},
    ):
        resp = client.post("/api/housekeeping/package-update/run")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    after = {a["id"]: a for a in data["actions"]}
    assert after["package-update"]["detail"] == "Run failed: This checkout cannot self-update."


def test_run_clears_tile_when_condition_resolved(tmp_path: Path) -> None:
    # A missed one-time reminder is surfaced as one tile; firing it consumes the
    # entry, so re-detection clears the tile in the same response.
    config = _config(tmp_path)
    store = _Store(
        [
            _Entry(
                frequency="once",
                run_at_date="2026-01-18",
                schedule_id="sched-missed",
            )
        ]
    )
    app = Starlette(
        routes=[
            Route("/api/housekeeping", list_housekeeping, methods=["GET"]),
            Route(
                "/api/housekeeping/{action_id}/run",
                run_housekeeping_action,
                methods=["POST"],
            ),
        ]
    )
    app.state.config = config
    app.state.schedule_manager = store
    client = TestClient(app)

    _starred(tmp_path)
    before = client.get("/api/housekeeping").json()["actions"]
    assert [a["id"] for a in before] == ["missed-schedules"]

    resp = client.post("/api/housekeeping/missed-schedules/run")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    # The tile is gone: the one-timer was consumed and re-detection saw it.
    assert [a["id"] for a in data["actions"]] == []


def test_run_clears_the_legacy_env_tile_through_the_route(tmp_path: Path) -> None:
    """The tile that asks to comment `.env` lines can now actually be cleared.

    The chat path can never do it — `.env` is credential-denied for every
    provider — so the run handler is the only way the tile reaches zero (#1134).
    """
    config = _config(tmp_path)
    (tmp_path / ".env").write_text(
        "PWA_AUTH_TOKEN=secret\nCIAO_DEV_MODE=true\n", encoding="utf-8"
    )
    _runtime(tmp_path)
    _starred(tmp_path)
    client = _client(config)
    # The route's detector reads the process environment; seed it as startup's
    # one-time `load_dotenv` would have.
    with patch.dict(os.environ, {"CIAO_DEV_MODE": "true"}):
        before = client.get("/api/housekeeping").json()["actions"]
        assert "legacy-env-ignored" in [a["id"] for a in before]
        tile = next(a for a in before if a["id"] == "legacy-env-ignored")
        assert tile["run_label"], "the tile must offer the engine-owned fix"

        resp = client.post("/api/housekeeping/legacy-env-ignored/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "legacy-env-ignored" not in [a["id"] for a in data["actions"]]

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "# CIAO_DEV_MODE=true" in text
    assert "PWA_AUTH_TOKEN=secret" in text


def test_run_unknown_action_returns_404(tmp_path: Path) -> None:
    client = _client(_config(tmp_path))
    resp = client.post("/api/housekeeping/not-a-real-action/run")
    assert resp.status_code == 404


def test_dismiss_snoozes_the_star_nudge(tmp_path: Path) -> None:
    """"Later" on the star ask writes a receipt and clears the tile."""
    config = _config(tmp_path)
    client = _client(config)
    before = client.get("/api/housekeeping").json()["actions"]
    assert "github-star" in [a["id"] for a in before]

    resp = client.post("/api/housekeeping/github-star/dismiss")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["action_id"] == "github-star"
    assert "github-star" not in [a["id"] for a in data["actions"]]
    # The receipt is on disk so the snooze survives a restart.
    receipt = json.loads(
        (tmp_path / ".runtime" / "star-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "later"


def test_dismiss_unknown_action_returns_404(tmp_path: Path) -> None:
    client = _client(_config(tmp_path))
    resp = client.post("/api/housekeeping/not-a-real-action/dismiss")
    assert resp.status_code == 404


def test_real_app_registers_housekeeping_routes() -> None:
    """Every housekeeping route must exist in the real app.py route table."""
    repo = Path(__file__).resolve().parents[1]
    app_source = (repo / "ciao" / "web" / "app.py").read_text(encoding="utf-8")
    assert 'Route("/api/housekeeping", list_housekeeping, methods=["GET"])' in app_source
    assert (
        'Route("/api/housekeeping/{action_id}/run", run_housekeeping_action, methods=["POST"])'
        in app_source
    )
    assert (
        'Route("/api/housekeeping/{action_id}/dismiss", dismiss_housekeeping_action, methods=["POST"])'
        in app_source
    )


def test_housekeeping_lists_provider_update_tile(tmp_path: Path) -> None:
    _starred(tmp_path)
    client = _client(_config(tmp_path))
    below = {
        "state": "below_tested",
        "installed": "2.0.32",
        "tested": "2.1.0",
        "command": "opencode upgrade 2.1.0",
        "terminal_action": "opencode-update",
    }
    client.app.state.provider_checks_fetcher = lambda: {"opencode": below}
    tiles = [a for a in client.get("/api/housekeeping").json()["actions"] if a["kind"] == "provider-update"]
    assert [t["id"] for t in tiles] == ["provider-update:opencode"]
    assert tiles[0]["terminal_action"] == "opencode-update"
    assert tiles[0]["command"] == "opencode upgrade 2.1.0"

    resp = client.post("/api/housekeeping/provider-update:opencode/dismiss")
    assert resp.status_code == 200
    assert not [a for a in resp.json()["actions"] if a["kind"] == "provider-update"]
    assert client.post("/api/housekeeping/provider-update:nope/dismiss").status_code == 404
