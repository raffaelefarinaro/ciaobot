"""Tests for the /api/workspace-html endpoint (HTML artifact rendering).

The Preview side of an artifact: same file ``/api/workspace-file`` serves as
``text/plain`` for the Code view, served here as ``text/html`` under a CSP that
sandboxes it. The header assertions below are the contract that makes embedding
model-authored script in the PWA safe, so they are written as "this exact
directive must be present", not as a loose substring check on the whole policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import workspace_html
from ciao.web.security import SecurityHeadersMiddleware

_ARTIFACT = "<!DOCTYPE html><title>t</title><script>document.title='ran'</script>"


@dataclass
class _FakeConfig:
    workspace_root: Path
    state_path: Path


def _make_client(workspace_root: Path, *, with_middleware: bool = False) -> TestClient:
    app = Starlette(
        routes=[Route("/api/workspace-html", workspace_html, methods=["GET"])],
        middleware=[Middleware(SecurityHeadersMiddleware)] if with_middleware else [],
    )
    app.state.config = _FakeConfig(
        workspace_root=workspace_root,
        state_path=workspace_root / "state.json",
    )
    return TestClient(app)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "Workspace").mkdir()
    (ws / "Workspace" / "dashboard.html").write_text(_ARTIFACT, encoding="utf-8")
    (ws / "Workspace" / "legacy.htm").write_text(_ARTIFACT, encoding="utf-8")
    (ws / "Workspace" / "report.md").write_text("# hello\n", encoding="utf-8")
    (ws / "Workspace" / "app.py").write_text("print('x')\n", encoding="utf-8")
    return ws


def test_serves_html_as_html(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/dashboard.html"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.text == _ARTIFACT


def test_htm_extension_also_renders(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/legacy.htm"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_inline_script_is_permitted(workspace: Path) -> None:
    # Load-bearing, not an oversight: an artifact inlines its own <script>, so
    # dropping 'unsafe-inline' does not harden this endpoint, it breaks every
    # artifact and leaves a blank frame. Containment lives in the directives
    # asserted by the next two tests.
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/dashboard.html"})
    csp = resp.headers["content-security-policy"]
    assert "script-src 'unsafe-inline'" in csp


def test_artifact_is_sandboxed_into_an_opaque_origin(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/dashboard.html"})
    csp = resp.headers["content-security-policy"]
    # No allow-same-origin: with it, model-authored script would run with the
    # user's session cookie and full same-origin access to the PWA.
    assert "sandbox allow-scripts" in csp
    assert "allow-same-origin" not in csp


def test_artifact_cannot_reach_the_network(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/dashboard.html"})
    csp = resp.headers["content-security-policy"]
    for directive in (
        "default-src 'none'",
        "connect-src 'none'",   # fetch / XHR / WebSocket / EventSource
        "img-src data:",        # no https: image beacon
        "form-action 'none'",
        "base-uri 'none'",
    ):
        assert directive in csp, directive
    # An https: source anywhere in the policy would reopen an exfiltration path.
    assert "https:" not in csp


def test_frame_headers_allow_same_origin_embedding(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/dashboard.html"})
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in resp.headers["content-security-policy"]


def test_middleware_does_not_downgrade_artifact_headers(workspace: Path) -> None:
    # SecurityHeadersMiddleware sets X-Frame-Options: DENY and a default-src
    # 'self' CSP through setdefault. If that ever becomes an unconditional
    # assignment, the panel's frame stops loading and every artifact breaks.
    client = _make_client(workspace, with_middleware=True)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/dashboard.html"})
    assert resp.status_code == 200
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    csp = resp.headers["content-security-policy"]
    assert "sandbox allow-scripts" in csp
    assert "default-src 'none'" in csp


def test_response_is_not_heuristically_cached(workspace: Path) -> None:
    # The panel reloads the frame after the model revises an artifact; a
    # heuristically fresh cache entry would show the pre-edit page.
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/dashboard.html"})
    assert resp.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("name", ["Workspace/report.md", "Workspace/app.py"])
def test_non_html_is_rejected(workspace: Path, name: str) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": name})
    assert resp.status_code == 415


def test_over_cap_is_rejected(workspace: Path) -> None:
    # Same 2 MB cap as the text viewer and the snapshot store, so a file cannot
    # end up renderable but unreadable (or vice versa).
    big = workspace / "Workspace" / "huge.html"
    big.write_text("<p>" + "x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/huge.html"})
    assert resp.status_code == 413


def test_missing_path_returns_400(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html")
    assert resp.status_code == 400


def test_missing_file_returns_404(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/nope.html"})
    assert resp.status_code == 404


def test_absolute_path_outside_workspace_is_served(workspace: Path, tmp_path: Path) -> None:
    # Parity with the sibling endpoints: there is no workspace sandbox, the
    # extension allowlist is the gate.
    outside = tmp_path / "elsewhere.html"
    outside.write_text(_ARTIFACT, encoding="utf-8")
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": str(outside)})
    assert resp.status_code == 200


def test_fuzzy_match_resolves_bare_filename(workspace: Path) -> None:
    # Kept deliberately, so the same path string that shows source in Code view
    # also renders in Preview.
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "dashboard.html"})
    assert resp.status_code == 200
    assert resp.text == _ARTIFACT
