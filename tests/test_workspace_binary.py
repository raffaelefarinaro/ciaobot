"""Tests for the /api/workspace-binary endpoint.

Mirror of ``test_workspace_image`` for the binary download variant: same
sandbox contract (path must canonicalise under workspace_root, no symlink
escape), but the allowlist covers PDFs/ZIPs/office docs and the response
sets ``Content-Disposition: inline`` with the original filename so PDFs
preview in a tab and other types download with a sensible name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import workspace_binary

# Minimal valid PDF (header + EOF marker is enough for a 200 response;
# the server doesn't validate content beyond extension and size).
_PDF_BYTES = b"%PDF-1.4\n%%EOF\n"
_ZIP_BYTES = b"PK\x05\x06" + b"\x00" * 18  # empty ZIP central directory record


@dataclass
class _FakeConfig:
    workspace_root: Path
    state_path: Path


def _make_client(workspace_root: Path) -> TestClient:
    app = Starlette(routes=[Route("/api/workspace-binary", workspace_binary, methods=["GET"])])
    app.state.config = _FakeConfig(
        workspace_root=workspace_root,
        state_path=workspace_root / "state.json",
    )
    return TestClient(app)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "docs").mkdir()
    (ws / "docs" / "spec.pdf").write_bytes(_PDF_BYTES)
    (ws / "docs" / "bundle.zip").write_bytes(_ZIP_BYTES)
    (ws / "docs" / "readme.md").write_text("# hello\n", encoding="utf-8")
    (ws / "docs" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (ws / "docs" / "presentation.pptx").write_bytes(b"PPTX_BYTES")
    return ws


def test_valid_pdf_returns_bytes(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "docs/spec.pdf"})
    assert resp.status_code == 200
    assert resp.content == _PDF_BYTES
    assert resp.headers["content-type"].startswith("application/pdf")
    # Inline disposition with the original filename so PDFs preview in a
    # browser tab; other binary types still download but with the right name.
    assert "inline" in resp.headers["content-disposition"]
    assert "spec.pdf" in resp.headers["content-disposition"]


def test_zip_returns_zip_mime(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "docs/bundle.zip"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")


def test_markdown_is_rejected(workspace: Path) -> None:
    # The binary endpoint must not accidentally serve text files even though
    # they're under the workspace and pass the sandbox check.
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "docs/readme.md"})
    assert resp.status_code == 415


def test_image_is_rejected(workspace: Path) -> None:
    # And not images either — those have their own dedicated endpoint.
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "docs/pic.png"})
    assert resp.status_code == 415


def test_missing_path_returns_400(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary")
    assert resp.status_code == 400


def test_traversal_returns_403(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(_PDF_BYTES)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "../secret.pdf"})
    assert resp.status_code == 403


def test_absolute_path_outside_workspace_returns_403(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "other.pdf"
    outside.write_bytes(_PDF_BYTES)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": str(outside)})
    assert resp.status_code == 403


def test_symlink_escape_returns_403(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(_PDF_BYTES)
    link = workspace / "escape.pdf"
    link.symlink_to(outside)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "escape.pdf"})
    assert resp.status_code == 403


def test_missing_file_returns_404(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "docs/nope.pdf"})
    assert resp.status_code == 404


def test_file_too_large_returns_413(workspace: Path) -> None:
    big = workspace / "big.pdf"
    big.write_bytes(b"x" * (50 * 1024 * 1024 + 1))
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "big.pdf"})
    assert resp.status_code == 413


def test_directory_path_returns_404(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "docs"})
    assert resp.status_code == 404


def test_pptx_raw_returns_bytes(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "docs/presentation.pptx", "raw": "1"})
    assert resp.status_code == 200
    assert resp.content == b"PPTX_BYTES"
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.presentationml.presentation")
    assert "presentation.pptx" in resp.headers["content-disposition"]


def test_pptx_no_libreoffice_returns_500(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.web import routes_api
    monkeypatch.setattr(routes_api, "_find_soffice", lambda: None)
    
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "docs/presentation.pptx"})
    assert resp.status_code == 500
    assert "LibreOffice is required" in resp.json()["error"]


def test_pptx_conversion_success(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.web import routes_api
    import subprocess
    
    monkeypatch.setattr(routes_api, "_find_soffice", lambda: "/usr/bin/soffice")
    
    def mock_run(args, **kwargs):
        outdir = args[5]
        generated_pdf = Path(outdir) / "presentation.pdf"
        generated_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        
        class MockCompletedProcess:
            returncode = 0
            stdout = "success"
            stderr = ""
        return MockCompletedProcess()
        
    monkeypatch.setattr(subprocess, "run", mock_run)
    
    client = _make_client(workspace)
    resp = client.get("/api/workspace-binary", params={"path": "docs/presentation.pptx"})
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4\n%%EOF\n"
    assert resp.headers["content-type"] == "application/pdf"
    assert "presentation.pdf" in resp.headers["content-disposition"]


