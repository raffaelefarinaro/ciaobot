from __future__ import annotations

import json
from urllib.error import HTTPError, URLError

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.package_version import (
    DEFAULT_GITHUB_REPO,
    _github_request,
    _tag_from_url,
    latest_release_redirect_url,
    make_cached_package_status,
    package_changelog,
    package_status,
    running_install_present,
)
from ciao.web.routes_api import package_changelog_endpoint, package_status_endpoint


class _Response:
    def __init__(self, payload: dict | None = None, *, url: str = ""):
        self._payload = payload or {}
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def geturl(self) -> str:
        return self._url


def test_tag_from_url_strips_v_prefix() -> None:
    base = f"https://github.com/{DEFAULT_GITHUB_REPO}/releases/tag"
    assert _tag_from_url(f"{base}/v0.4.2") == "0.4.2"
    assert _tag_from_url(f"{base}/0.4.2") == "0.4.2"
    assert _tag_from_url("https://github.com/x/y/releases") == ""


def test_package_status_reports_available_update() -> None:
    def opener(request, timeout: float):
        # The recurring check uses the public web redirect, not the REST API.
        assert request.full_url == (
            f"https://github.com/{DEFAULT_GITHUB_REPO}/releases/latest"
        )
        assert timeout == 2.5
        return _Response(
            url=f"https://github.com/{DEFAULT_GITHUB_REPO}/releases/tag/v0.3.0"
        )

    data = package_status(current_version="0.2.0", opener=opener)

    assert data == {
        "current_version": "0.2.0",
        "latest_version": "0.3.0",
        "update_available": True,
        "source": latest_release_redirect_url(),
        "error": "",
    }


def test_package_status_no_tag_reports_error() -> None:
    def opener(request, timeout: float):
        return _Response(url=f"https://github.com/{DEFAULT_GITHUB_REPO}/releases")

    data = package_status(current_version="0.2.0", opener=opener)

    assert data["latest_version"] == ""
    assert data["update_available"] is False
    assert data["error"]


def test_package_status_handles_unreachable_api() -> None:
    def opener(request, timeout: float):
        raise URLError("offline")

    data = package_status(current_version="0.2.0", opener=opener)

    assert data["current_version"] == "0.2.0"
    assert data["latest_version"] == ""
    assert data["update_available"] is False
    assert "offline" in data["error"]


def test_package_status_rate_limit_is_reported_as_transient() -> None:
    def opener(request, timeout: float):
        raise HTTPError(request.full_url, 403, "rate limit exceeded", {}, None)

    data = package_status(current_version="0.2.0", opener=opener)

    assert data["latest_version"] == ""
    assert data["update_available"] is False
    assert "rate limit" in str(data["error"]).lower()
    # The raw "HTTP Error 403" string is not surfaced to the user.
    assert "403" not in str(data["error"])


def test_package_status_non_ratelimit_http_error_keeps_code() -> None:
    def opener(request, timeout: float):
        raise HTTPError(request.full_url, 500, "Server Error", {}, None)

    data = package_status(current_version="0.2.0", opener=opener)

    assert "500" in str(data["error"])


def test_github_request_adds_auth_header_when_token_present(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("CIAO_GITHUB_TOKEN", "secret-token")

    request = _github_request("https://api.github.com/x")

    assert request.headers.get("Authorization") == "Bearer secret-token"


def test_github_request_omits_auth_header_without_token(monkeypatch) -> None:
    for name in ("CIAO_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    request = _github_request("https://api.github.com/x")

    assert request.headers.get("Authorization") is None


def test_cached_package_status_serves_fresh_within_ttl() -> None:
    now = {"t": 0.0}
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"latest_version": "0.3.0", "error": ""}

    cached = make_cached_package_status(
        fetch=fetch, ttl_ok=100.0, clock=lambda: now["t"]
    )

    assert cached()["latest_version"] == "0.3.0"
    now["t"] = 50.0
    assert cached()["latest_version"] == "0.3.0"
    assert calls["n"] == 1  # served from cache, no second fetch

    now["t"] = 150.0  # past ttl_ok
    cached()
    assert calls["n"] == 2  # expired, refetched


def test_cached_package_status_serves_last_good_on_error() -> None:
    now = {"t": 0.0}
    results = [
        {"latest_version": "0.3.0", "error": ""},
        {"latest_version": "", "error": "GitHub rate limit reached; retry later."},
    ]

    def fetch():
        return results.pop(0)

    cached = make_cached_package_status(
        fetch=fetch, ttl_ok=100.0, ttl_error=10.0, clock=lambda: now["t"]
    )

    assert cached()["latest_version"] == "0.3.0"
    now["t"] = 200.0  # expire the good value; next fetch returns an error
    served = cached()
    assert served["latest_version"] == "0.3.0"  # last good served through the error
    assert served["error"] == ""


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


def test_running_install_present_true_for_live_package() -> None:
    # The running test process imports ciao from a real path, so its files
    # exist; the stale-install self-heal must not fire in this case.
    assert running_install_present() is True


def test_running_install_present_false_when_files_gone(monkeypatch, tmp_path) -> None:
    # Simulate the Homebrew-swap symptom: ciao.__file__ points at a keg dir
    # that no longer exists on disk.
    import ciao

    monkeypatch.setattr(ciao, "__file__", str(tmp_path / "gone" / "ciao" / "__init__.py"))
    assert running_install_present() is False


# ── Stale-install watcher ────────────────────────────────────


def test_install_watcher_files_vanished_restarts_immediately() -> None:
    from ciao.package_version import InstallWatcher

    w = InstallWatcher("0.4.27", probe=lambda: "0.4.27", present=lambda: False)
    reason = w.check_files()
    assert reason is not None and "vanished" in reason


def test_install_watcher_version_bump_needs_two_consistent_probes() -> None:
    from ciao.package_version import InstallWatcher

    w = InstallWatcher("0.4.27", probe=lambda: "0.4.28", present=lambda: True)
    assert w.check_files() is None
    assert w.check_version() is None          # first differing reading: pending
    reason = w.check_version()                # second consistent reading: restart
    assert reason is not None and "0.4.28" in reason and "0.4.27" in reason


def test_install_watcher_flaky_probe_never_restarts() -> None:
    from ciao.package_version import InstallWatcher

    readings = iter(["0.4.28", None, "0.4.28", "0.4.27", "0.4.28", "0.4.29"])
    w = InstallWatcher("0.4.27", probe=lambda: next(readings), present=lambda: True)
    # differ -> fail -> differ -> same-as-running -> differ -> different-differ:
    # no two *consecutive consistent* readings, so never a restart.
    for _ in range(6):
        assert w.check_version() is None


def test_installed_version_rejects_junk_output(monkeypatch) -> None:
    import subprocess
    from ciao import package_version

    class R:
        returncode = 0
        stdout = "Traceback (most recent call last):\n..."

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert package_version.installed_version() is None


def test_installed_version_missing_interpreter_fails_open(monkeypatch) -> None:
    import subprocess
    from ciao import package_version

    def boom(*a, **k):
        raise FileNotFoundError("python is gone")

    monkeypatch.setattr(subprocess, "run", boom)
    assert package_version.installed_version() is None


def test_stable_executable_maps_cellar_to_opt(monkeypatch, tmp_path) -> None:
    import sys
    from ciao import package_version

    opt = tmp_path / "opt" / "ciaobot" / "libexec" / "bin"
    opt.mkdir(parents=True)
    (opt / "python").write_text("", encoding="utf-8")
    cellar = tmp_path / "Cellar" / "ciaobot" / "0.4.20" / "libexec" / "bin" / "python"
    monkeypatch.setattr(sys, "executable", str(cellar))
    assert package_version._stable_executable() == str(opt / "python")
