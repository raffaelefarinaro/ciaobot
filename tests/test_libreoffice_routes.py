from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import libreoffice_status_endpoint


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


def test_external_package_install_handlers_are_gone() -> None:
    """The server never shells out to brew/npm on a button click.

    External-package installs are operator or agent work (issue #359): the
    app surfaces status and a chat prompt, not a POST that mutates the host.
    """
    import ciao.web.routes_api as routes_api

    assert not hasattr(routes_api, "libreoffice_install_endpoint")
    assert not hasattr(routes_api, "gws_install")

    import ciao.upgrade as upgrade

    assert not hasattr(upgrade, "upgrade_libreoffice")