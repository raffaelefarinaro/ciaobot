"""Tests for the operator-action housekeeping API routes (P4.5).

The routes live in ``ciao/web/routes_api.py``. Unlike the older route tests,
these assert the REAL route table in ``ciao/web/app.py`` serves every handler,
because a route that is registered nowhere does not exist no matter how well
it is unit-tested.
"""

from __future__ import annotations

import json
from pathlib import Path

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
        self.vault_mode = "scratch"
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


def _seed_vocabulary_unresolved(tmp_path: Path) -> _Config:
    config = _config(tmp_path)
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {"log": ["x.md"]}}),
        encoding="utf-8",
    )
    return config


def _starred(tmp_path: Path) -> None:
    """Silence the GitHub-star nudge so exact action lists stay deterministic."""
    runtime = _runtime(tmp_path)
    (runtime / "star-receipt.json").write_text(
        json.dumps({"status": "starred", "at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )


def test_get_returns_actions_payload(tmp_path: Path) -> None:
    config = _seed_vocabulary_unresolved(tmp_path)
    resp = _client(config).get("/api/housekeeping")
    assert resp.status_code == 200
    data = resp.json()
    assert "actions" in data
    ids = [a["id"] for a in data["actions"]]
    assert "vault-vocabulary" in ids
    action = next(a for a in data["actions"] if a["id"] == "vault-vocabulary")
    # Every action carries the run/chat surface the client needs.
    assert action["run_label"]
    assert action["chat_prompt"]


def test_run_returns_redetected_list_after_clearing(tmp_path: Path) -> None:
    config = _seed_vocabulary_unresolved(tmp_path)
    # The run action migrates the vault, which needs the vault dir to exist.
    config.vault_root.mkdir(parents=True, exist_ok=True)
    client = _client(config)
    # The unresolved type is a chat-only decision, but the tile's run button
    # re-applies the mechanical renames. A migration that resolves nothing keeps
    # the tile, so assert the payload shape and the re-detected list rather than
    # a specific clearing.
    resp = client.post("/api/housekeeping/vault-vocabulary/run")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["action_id"] == "vault-vocabulary"
    assert isinstance(data["actions"], list)


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


def test_failed_run_keeps_same_id_with_failure_detail(tmp_path: Path) -> None:
    # A run whose condition persists returns the same id with its detail
    # replaced by the failure text — never silently re-offered as a fresh tile.
    config = _seed_vocabulary_unresolved(tmp_path)
    client = _client(config)
    # The run migrates an existing vault. Force a failure by making the vault
    # root a file, so the migration raises.
    config.vault_root.write_text("not a directory", encoding="utf-8")
    _starred(tmp_path)
    before = client.get("/api/housekeeping").json()["actions"]
    assert [a["id"] for a in before] == ["vault-vocabulary"]

    resp = client.post("/api/housekeeping/vault-vocabulary/run")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    after = {a["id"]: a for a in data["actions"]}
    assert "vault-vocabulary" in after
    assert "failed" in after["vault-vocabulary"]["detail"].lower()
    # The tile is still actionable (same shape), not silently re-offered as
    # though nothing happened.
    assert after["vault-vocabulary"]["run_label"]


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
