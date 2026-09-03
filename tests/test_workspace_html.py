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

# The bridge is spliced in after the doctype, so the artifact is no longer one
# contiguous substring of the response. Assert on its parts instead.
_ARTIFACT_HEAD = "<!DOCTYPE html>"
_ARTIFACT_BODY = "<title>t</title><script>document.title='ran'</script>"


def _assert_document_preserved(text: str) -> None:
    assert text.startswith(_ARTIFACT_HEAD)
    assert text.endswith(_ARTIFACT_BODY)
    assert "data-ciao-artifact-bridge" in text


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
    # The original document is preserved (plus the injected comment bridge).
    _assert_document_preserved(resp.text)


def test_injects_comment_bridge(workspace: Path) -> None:
    # The bridge script is what makes the preview commentable: it watches
    # selections inside the sandboxed frame and posts anchors to the panel.
    client = _make_client(workspace)
    resp = client.get("/api/workspace-html", params={"path": "Workspace/dashboard.html"})
    assert "data-ciao-artifact-bridge" in resp.text
    assert "ciao:artifact-comment" in resp.text


def test_bridge_tag_is_syntactically_valid_javascript() -> None:
    """The injected <script> must actually parse.

    ``BRIDGE_TAG`` was built with ``\\n`` in a non-raw f-string, so it shipped
    a literal backslash where a newline belonged. A stray ``\\`` at statement
    position is a JS SyntaxError, and the whole bridge silently never
    initialized — while a substring assertion on the script body still passed.
    """
    from ciao.web.artifact_bridge import BRIDGE_SCRIPT, BRIDGE_TAG

    body = BRIDGE_TAG.partition(">")[2].rpartition("</script>")[0]
    assert body == f"\n{BRIDGE_SCRIPT}\n"
    # `\s` inside the regexes in BRIDGE_SCRIPT is legitimate; a backslash
    # anywhere in the glue around it is not.
    assert "\\" not in body.replace(BRIDGE_SCRIPT, "")


def test_compose_affordances_are_gated_on_a_parent_handshake() -> None:
    """No Comment pill until the parent says it is listening.

    The PWA half of selection comments does not exist yet, so an ungated pill
    posts `ciao:artifact-comment` into the void — a control that looks like a
    feature and does nothing. `ciao:comments-enable`, or any
    `ciao:apply-comments` (which proves a parent is on the line), turns it on.
    """
    from ciao.web.artifact_bridge import BRIDGE_SCRIPT

    # The flag starts false and both compose entry points check it.
    assert "var enabled = false" in BRIDGE_SCRIPT
    assert BRIDGE_SCRIPT.count("if (!enabled) return") == 2
    # Both handshakes flip it, and highlight application still implies it.
    assert "'ciao:comments-enable'" in BRIDGE_SCRIPT
    assert "'ciao:comments-disable'" in BRIDGE_SCRIPT
    # `ready` is still announced unconditionally, so a parent knows to enable.
    assert "action: 'ready'" in BRIDGE_SCRIPT


def test_bridge_injection_is_idempotent() -> None:
    from ciao.web.artifact_bridge import BRIDGE_TAG, inject_bridge

    once = inject_bridge("<html><head></head><body></body></html>")
    assert once.count("data-ciao-artifact-bridge") == 1
    assert inject_bridge(once) == once
    # A bare fragment (no doctype, no structural tags) is still prepended
    # rather than dropped.
    fragment = inject_bridge("<p>hello</p>")
    assert fragment.startswith(BRIDGE_TAG[:40])
    assert "<p>hello</p>" in fragment


def test_bridge_never_precedes_the_doctype() -> None:
    """A <script> ahead of the doctype puts the artifact into quirks mode.

    Different box model, ``line-height``, and percentage heights than the same
    file rendered before injection existed — a visible regression in every
    head-less artifact, which is a shape the model writes often.
    """
    from ciao.web.artifact_bridge import inject_bridge

    for doc in (
        "<!DOCTYPE html><html><body><p>x</p></body></html>",
        "<!doctype html><p>x</p>",
        "<!DOCTYPE html><title>t</title>",
    ):
        out = inject_bridge(doc)
        assert out.lower().startswith("<!doctype html>"), out[:60]
        assert out.count("data-ciao-artifact-bridge") == 1


def test_bridge_placement_prefers_head_then_body() -> None:
    from ciao.web.artifact_bridge import BRIDGE_TAG, inject_bridge

    head = inject_bridge("<!DOCTYPE html><html><head><title>t</title></head><body></body></html>")
    assert head.startswith("<!DOCTYPE html><html><head>" + BRIDGE_TAG)

    body = inject_bridge("<!DOCTYPE html><html><body><p>x</p></body></html>")
    assert body.startswith("<!DOCTYPE html><html><body>" + BRIDGE_TAG)


def test_bridge_defers_its_ready_handshake_to_the_parsed_dom() -> None:
    """``ready`` is the parent's cue to push highlights.

    The script runs from ``<head>``, so posting it during parse would have the
    parent query a tree with no ``<body>`` and match nothing.
    """
    from ciao.web.artifact_bridge import BRIDGE_SCRIPT

    assert "DOMContentLoaded" in BRIDGE_SCRIPT
    assert "document.readyState === 'loading'" in BRIDGE_SCRIPT


def test_bridge_anchors_whole_elements_without_a_quote() -> None:
    """Alt+Click exists for nodes with no text (an SVG shape, a chart bar).

    Those anchors are an outline on the element, not a <mark> around text, so
    the reapplication path must not gate them on a non-empty quote.
    """
    from ciao.web.artifact_bridge import BRIDGE_SCRIPT

    assert "ciao-comment-el" in BRIDGE_SCRIPT
    # The quote gate applies only after the whole-element branch has returned.
    assert BRIDGE_SCRIPT.index("if (c.wholeElement)") < BRIDGE_SCRIPT.index("if (!c.quote) continue")


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
        "media-src data:",      # self-contained audio/video only
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
    _assert_document_preserved(resp.text)


def test_non_utf8_artifact_still_renders(workspace: Path) -> None:
    """A cp1252 HTML export streamed fine as a FileResponse.

    Injecting the comment bridge means decoding the file in the handler, and a
    strict decode turned one stray byte into a 500 for the whole preview.
    """
    (workspace / "Workspace" / "legacy-cp1252.html").write_bytes(
        "<html><body><p>caf\u00e9 \u2014 r\u00e9sum\u00e9</p></body></html>".encode("cp1252")
    )
    client = _make_client(workspace)

    resp = client.get(
        "/api/workspace-html", params={"path": "Workspace/legacy-cp1252.html"}
    )

    assert resp.status_code == 200
    assert "<p>caf" in resp.text
    # The bridge is still injected into the salvaged document.
    assert "data-ciao-artifact-bridge" in resp.text
