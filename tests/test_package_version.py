from __future__ import annotations

import json
from urllib.error import URLError

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.package_version import DEFAULT_PACKAGE_INDEX_URL, package_status
from ciao.web.routes_api import package_status_endpoint


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_package_status_reports_available_update() -> None:
    def opener(url: str, timeout: float):
        assert url == DEFAULT_PACKAGE_INDEX_URL
        assert timeout == 2.5
        return _Response({"info": {"version": "0.3.0"}})

    data = package_status(current_version="0.2.0", opener=opener)

    assert data == {
        "current_version": "0.2.0",
        "latest_version": "0.3.0",
        "update_available": True,
        "source": DEFAULT_PACKAGE_INDEX_URL,
        "error": "",
    }


def test_package_status_handles_unreachable_index() -> None:
    def opener(url: str, timeout: float):
        raise URLError("offline")

    data = package_status(current_version="0.2.0", opener=opener)

    assert data["current_version"] == "0.2.0"
    assert data["latest_version"] == ""
    assert data["update_available"] is False
    assert "offline" in data["error"]


def test_package_status_endpoint_uses_app_fetcher() -> None:
    app = Starlette(
        routes=[Route("/api/package/status", package_status_endpoint, methods=["GET"])]
    )
    app.state.package_status_fetcher = lambda: {
        "current_version": "0.2.0",
        "latest_version": "0.2.1",
        "update_available": True,
        "source": "test",
        "error": "",
    }

    data = TestClient(app).get("/api/package/status").json()

    assert data["current_version"] == "0.2.0"
    assert data["latest_version"] == "0.2.1"
    assert data["update_available"] is True
