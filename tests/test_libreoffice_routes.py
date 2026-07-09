from __future__ import annotations

from unittest.mock import AsyncMock, patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.upgrade import UpgradeResult
from ciao.web.routes_api import libreoffice_install_endpoint, libreoffice_status_endpoint


def test_libreoffice_status_reports_available(monkeypatch) -> None:
    app = Starlette(
        routes=[Route("/api/libreoffice-status", libreoffice_status_endpoint, methods=["GET"])]
    )
    monkeypatch.setattr("ciao.web.routes_api._find_soffice", lambda: "/usr/bin/soffice")

    resp = TestClient(app).get("/api/libreoffice-status")

    assert resp.status_code == 200
    assert resp.json() == {"available": True}


def test_libreoffice_status_reports_unavailable(monkeypatch) -> None:
    app = Starlette(
        routes=[Route("/api/libreoffice-status", libreoffice_status_endpoint, methods=["GET"])]
    )
    monkeypatch.setattr("ciao.web.routes_api._find_soffice", lambda: None)

    resp = TestClient(app).get("/api/libreoffice-status")

    assert resp.status_code == 200
    assert resp.json() == {"available": False}


def test_libreoffice_install_endpoint_success() -> None:
    app = Starlette(
        routes=[Route("/api/libreoffice-install", libreoffice_install_endpoint, methods=["POST"])]
    )
    result = UpgradeResult(
        command=["brew", "install", "--cask", "libreoffice"],
        changed=True, success=True,
        stdout="==> Installing libreoffice", stderr="",
        before_version="", after_version="9.1",
    )
    with patch("ciao.upgrade.upgrade_libreoffice", AsyncMock(return_value=result)):
        resp = TestClient(app).post("/api/libreoffice-install")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "output": "==> Installing libreoffice"}


def test_libreoffice_install_endpoint_reports_failure() -> None:
    app = Starlette(
        routes=[Route("/api/libreoffice-install", libreoffice_install_endpoint, methods=["POST"])]
    )
    result = UpgradeResult(
        command=["brew", "install", "--cask", "libreoffice"],
        changed=False, success=False,
        stdout="", stderr="brew not found (Homebrew is required for LibreOffice on macOS)",
        before_version="", after_version="",
    )
    with patch("ciao.upgrade.upgrade_libreoffice", AsyncMock(return_value=result)):
        resp = TestClient(app).post("/api/libreoffice-install")

    assert resp.status_code == 500
    assert resp.json() == {
        "ok": False,
        "error": "brew not found (Homebrew is required for LibreOffice on macOS)",
    }
