"""Route tests for the server-managed GWS re-login endpoints (issue #145).

A fake exchange function stands in for Google's token endpoint; the loopback
callback is driven by a real local HTTP GET so the in-process listener path is
exercised end to end without touching the network.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import gws_auth
from ciao.config import CiaoConfig
from ciao.web import routes_api as gws_routes
from ciao.web.routes_api import (
    gws_integration_settings,
    gws_relogin_cancel,
    gws_relogin_start,
    gws_relogin_status,
)


def _client(tmp_path: Path):
    env = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_OLLAMA_LOCAL_DISCOVERY": "0",
    }
    config = CiaoConfig.from_env(env)
    app = Starlette(
        routes=[
            Route("/api/integrations/gws", gws_integration_settings, methods=["GET"]),
            Route("/api/integrations/gws/relogin/start", gws_relogin_start, methods=["POST"]),
            Route("/api/integrations/gws/relogin/status", gws_relogin_status, methods=["GET"]),
            Route("/api/integrations/gws/relogin/cancel", gws_relogin_cancel, methods=["POST"]),
        ]
    )
    app.state.config = config
    return TestClient(app), config


def _write_client_secret(config) -> None:
    config_dir = gws_auth.profile_config_dir(config, "personal")
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret"}}),
        encoding="utf-8",
    )


def test_relogin_start_requires_client_secret(tmp_path: Path) -> None:
    client, _config = _client(tmp_path)
    gws_auth.save_profile_registry(_config, [{"name": "personal", "label": "Personal"}])
    resp = client.post("/api/integrations/gws/relogin/start", json={"profile": "personal"})
    assert resp.status_code == 400
    assert "client_secret" in resp.json()["error"]


def test_relogin_rejects_unknown_profile(tmp_path: Path) -> None:
    client, _config = _client(tmp_path)
    resp = client.post("/api/integrations/gws/relogin/start", json={"profile": "bogus"})
    assert resp.status_code == 400


def test_relogin_full_flow(tmp_path: Path) -> None:
    client, config = _client(tmp_path)
    _write_client_secret(config)

    # Inject a manager whose exchange writes real credentials without network.
    def fake_exchange(cfg, profile, *, code, redirect_uri, code_verifier=None):
        gws_auth.store_credentials(
            gws_auth.profile_config_dir(cfg, profile),
            client_id="cid",
            client_secret="csecret",
            refresh_token="rtok",
            email="routed@example.com",
        )
        return {"ok": True, "email": "routed@example.com"}

    client.app.state.gws_relogin_manager = gws_auth.GwsReloginManager(
        config, exchange_fn=fake_exchange, session_ttl=10
    )

    start = client.post("/api/integrations/gws/relogin/start", json={"profile": "personal"})
    assert start.status_code == 200
    data = start.json()
    auth = urlparse(data["auth_url"])
    assert auth.scheme == "https"
    assert auth.hostname == "accounts.google.com"
    assert auth.path == "/o/oauth2/auth"
    query = parse_qs(auth.query)
    assert query["client_id"] == ["cid"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"][0] == data["redirect_uri"]
    port, state = data["port"], data["state"]
    assert urlparse(query["redirect_uri"][0]).port == port
    # PKCE (issue #354): a small, safe addition to the loopback flow, which
    # already validates `state`.
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query

    # Before the redirect, status is pending.
    pending = client.get("/api/integrations/gws/relogin/status?profile=personal")
    assert pending.json()["status"] == "pending"

    # Drive the loopback callback exactly as Google's browser redirect would.
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?code=abc&state={state}", timeout=5
    ) as resp:
        assert resp.status == 200

    # Poll until completed (the exchange runs on the callback thread).
    final = client.app.state.gws_relogin_manager.wait("personal", timeout=5)
    assert final["status"] == "completed"
    status = client.get("/api/integrations/gws/relogin/status?profile=personal").json()
    assert status["status"] == "completed"
    assert status["email"] == "routed@example.com"

    # Credentials were written; the integration payload now reports configured.
    creds = json.loads(
        (gws_auth.profile_config_dir(config, "personal") / "credentials.json").read_text()
    )
    assert creds["refresh_token"] == "rtok"


def test_relogin_cancel(tmp_path: Path) -> None:
    client, config = _client(tmp_path)
    _write_client_secret(config)
    client.app.state.gws_relogin_manager = gws_auth.GwsReloginManager(
        config, exchange_fn=lambda *a, **k: {"ok": True}, session_ttl=10
    )
    client.post("/api/integrations/gws/relogin/start", json={"profile": "personal"})
    resp = client.post("/api/integrations/gws/relogin/cancel", json={"profile": "personal"})
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True
    assert client.get("/api/integrations/gws/relogin/status?profile=personal").json()["status"] == "none"


class _BlockingMonitor:
    """Fake health monitor whose check_once blocks until released."""

    def __init__(self, fail: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.fail = fail

    def check_once(self) -> dict:
        self.calls += 1
        self.started.set()
        self.release.wait(10)
        if self.fail:
            raise RuntimeError("probe boom")
        return {}


def _completed_client(tmp_path: Path, monitor):
    client, config = _client(tmp_path)
    _write_client_secret(config)
    gws_auth.save_profile_registry(config, [{"name": "personal", "label": "Personal"}])
    manager = gws_auth.GwsReloginManager(config)
    session = gws_auth._ReloginSession.__new__(gws_auth._ReloginSession)
    manager._sessions["personal"] = session
    session.status = "completed"
    session.email = "a@b.c"
    session.error = ""
    session.expires_at = time.time() + 60
    client.app.state.gws_relogin_manager = manager
    client.app.state.gws_health_monitor = monitor
    # Entering the client keeps one event loop alive across requests, so the
    # background refresh task survives past the response.
    client.__enter__()
    return client


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_status_completed_does_not_wait_for_health_refresh(tmp_path: Path) -> None:
    monitor = _BlockingMonitor()
    client = _completed_client(tmp_path, monitor)
    try:
        started = time.monotonic()
        resp = client.get("/api/integrations/gws/relogin/status?profile=personal")
        elapsed = time.monotonic() - started
        assert resp.json()["status"] == "completed"
        # check_once is still blocked, yet the response already came back.
        assert not monitor.release.is_set()
        assert elapsed < 5
        assert _wait(monitor.started.is_set)
    finally:
        monitor.release.set()
        client.__exit__(None, None, None)


def test_second_completed_poll_does_not_start_second_refresh(tmp_path: Path) -> None:
    monitor = _BlockingMonitor()
    client = _completed_client(tmp_path, monitor)
    try:
        client.get("/api/integrations/gws/relogin/status?profile=personal")
        assert _wait(monitor.started.is_set)
        client.get("/api/integrations/gws/relogin/status?profile=personal")
        assert monitor.calls == 1
    finally:
        monitor.release.set()
        client.__exit__(None, None, None)


def test_background_refresh_error_is_logged_not_raised(tmp_path: Path, caplog) -> None:
    monitor = _BlockingMonitor(fail=True)
    monitor.release.set()
    client = _completed_client(tmp_path, monitor)
    with caplog.at_level("ERROR"):
        resp = client.get("/api/integrations/gws/relogin/status?profile=personal")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        try:
            assert _wait(lambda: "Post-relogin health refresh failed" in caplog.text)
        finally:
            client.__exit__(None, None, None)


def _stale_health_client(tmp_path: Path, *, creds_mtime: float, checked_at: float):
    client, config = _client(tmp_path)
    gws_auth.save_profile_registry(config, [{"name": "personal", "label": "Personal"}])
    config_dir = gws_auth.profile_config_dir(config, "personal")
    config_dir.mkdir(parents=True, exist_ok=True)
    creds = config_dir / "credentials.json"
    creds.write_text(json.dumps({"email": "a@b.c"}), encoding="utf-8")
    os.utime(creds, (creds_mtime, creds_mtime))
    runtime = Path(config.state_path).parent
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / gws_auth.HEALTH_CACHE_NAME).write_text(
        json.dumps(
            {"profiles": {"personal": {"token_valid": False, "checked_at": checked_at}}}
        ),
        encoding="utf-8",
    )
    return client


def _personal(client) -> dict:
    data = client.get("/api/integrations/gws").json()
    return next(p for p in data["profiles"] if p["name"] == "personal")


def test_health_older_than_credentials_is_unknown(tmp_path: Path) -> None:
    client = _stale_health_client(tmp_path, creds_mtime=2_000_000.0, checked_at=1_000_000.0)
    profile = _personal(client)
    assert profile["needs_relogin"] is False
    assert profile["token_valid"] is None


def test_health_newer_than_credentials_still_needs_relogin(tmp_path: Path) -> None:
    client = _stale_health_client(tmp_path, creds_mtime=1_000_000.0, checked_at=2_000_000.0)
    profile = _personal(client)
    assert profile["needs_relogin"] is True
    assert profile["token_valid"] is False
