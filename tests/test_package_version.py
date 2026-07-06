from __future__ import annotations

import json
from urllib.error import URLError

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.package_version import (
    DEFAULT_GITHUB_REPO,
    DEFAULT_PACKAGE_INDEX_URL,
    package_changelog,
    package_status,
)
from ciao.web.routes_api import package_changelog_endpoint, package_status_endpoint


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


def test_package_changelog_lists_commits_newest_first() -> None:
    captured: dict[str, object] = {}

    def opener(request, timeout: float):
        captured["url"] = request.full_url
        captured["accept"] = request.headers.get("Accept")
        return _Response(
            {
                "commits": [
                    {"sha": "aaaaaaaa1", "commit": {"message": "feat: older change\n\nbody"}},
                    {"sha": "bbbbbbbb2", "commit": {"message": "fix: newer change"}},
                ]
            }
        )

    data = package_changelog(
        current_version="0.2.0", latest_version="0.3.0", opener=opener
    )

    assert captured["url"] == (
        f"https://api.github.com/repos/{DEFAULT_GITHUB_REPO}/compare/v0.2.0...v0.3.0"
    )
    assert captured["accept"] == "application/vnd.github+json"
    assert data["commits"] == [
        {"sha": "bbbbbbb", "subject": "fix: newer change"},
        {"sha": "aaaaaaa", "subject": "feat: older change"},
    ]
    assert data["compare_url"] == (
        f"https://github.com/{DEFAULT_GITHUB_REPO}/compare/v0.2.0...v0.3.0"
    )
    assert data["error"] == ""


def test_package_changelog_without_latest_returns_no_commits() -> None:
    def opener(request, timeout: float):  # pragma: no cover - must not be called
        raise AssertionError("network should not be hit without a latest version")

    data = package_changelog(current_version="0.2.0", latest_version="", opener=opener)

    assert data["commits"] == []
    assert data["error"]


def test_package_changelog_handles_network_failure() -> None:
    def opener(request, timeout: float):
        raise URLError("offline")

    data = package_changelog(
        current_version="0.2.0", latest_version="0.3.0", opener=opener
    )

    assert data["commits"] == []
    assert "offline" in data["error"]
    assert data["compare_url"].endswith("/compare/v0.2.0...v0.3.0")


def test_package_changelog_endpoint_combines_status_and_commits(monkeypatch) -> None:
    import ciao.web.routes_api as routes_api

    monkeypatch.setattr(
        routes_api,
        "package_changelog",
        lambda **kwargs: {
            "commits": [{"sha": "abc1234", "subject": "fix: thing"}],
            "compare_url": "https://example.test/compare",
            "repo": DEFAULT_GITHUB_REPO,
            "error": "",
        },
    )

    app = Starlette(
        routes=[
            Route(
                "/api/package/changelog",
                package_changelog_endpoint,
                methods=["GET"],
            )
        ]
    )
    app.state.package_status_fetcher = lambda: {
        "current_version": "0.2.0",
        "latest_version": "0.3.0",
        "update_available": True,
        "source": "test",
        "error": "",
    }

    data = TestClient(app).get("/api/package/changelog").json()

    assert data["current_version"] == "0.2.0"
    assert data["latest_version"] == "0.3.0"
    assert data["update_available"] is True
    assert data["commits"] == [{"sha": "abc1234", "subject": "fix: thing"}]
    assert data["compare_url"] == "https://example.test/compare"


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
