"""Route tests for the server-managed GWS re-login endpoints (issue #145).

A fake exchange function stands in for Google's token endpoint; the loopback
callback is driven by a real local HTTP GET so the in-process listener path is
exercised end to end without touching the network.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import gws_auth
from ciao.config import CiaoConfig
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
    def fake_exchange(cfg, profile, *, code, redirect_uri):
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
    assert "accounts.google.com" in data["auth_url"]
    port, state = data["port"], data["state"]

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
