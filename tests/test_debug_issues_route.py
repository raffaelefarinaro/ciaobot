"""Route tests for GET /api/debug/issues (dev-mode runtime issue report)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_api import debug_issues

_ORIGIN = "https://ciao.example"


def _client(*, dev_mode: bool, workspace_root: Path):
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/debug/issues", debug_issues, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        dev_mode=dev_mode, workspace_root=workspace_root
    )
    client = TestClient(app, base_url=_ORIGIN)
    return client, {SESSION_COOKIE: serializer.dumps({"user": "owner"})}


def test_debug_issues_hidden_without_dev_mode(tmp_path: Path) -> None:
    client, cookies = _client(dev_mode=False, workspace_root=tmp_path)
    resp = client.get("/api/debug/issues", cookies=cookies)
    assert resp.status_code == 404


def test_debug_issues_reports_errors_in_dev_mode(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "server_errors.log").write_text(
        "2026-07-06 ERROR ciao.web: boom\n", encoding="utf-8"
    )
    client, cookies = _client(dev_mode=True, workspace_root=tmp_path)

    resp = client.get("/api/debug/issues", cookies=cookies)

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_issues"] is True
    assert data["error_log_lines"] == 1
    assert "boom" in data["report_text"]
    assert isinstance(data["failed_jobs"], list)


def test_debug_issues_empty_report(tmp_path: Path) -> None:
    client, cookies = _client(dev_mode=True, workspace_root=tmp_path)
    resp = client.get("/api/debug/issues", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["has_issues"] is False
