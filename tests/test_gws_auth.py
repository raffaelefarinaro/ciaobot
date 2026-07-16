"""Tests for ciao.gws_auth: OAuth helpers, token-health monitor, and the
server-managed loopback re-login manager (issue #145).

Network is never hit: the token exchange is injected/monkeypatched and the
loopback callback is driven by a real local HTTP request to the bound port.
No real OAuth token, secret, or code is used.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import gws_auth


def _config(tmp_path: Path) -> SimpleNamespace:
    (tmp_path / ".runtime").mkdir(exist_ok=True)
    return SimpleNamespace(
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
    )


def _write_client_secret(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "client_secret.json").write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )


# ── pure helpers ──────────────────────────────────────────────────────────


def test_profile_config_dir_mapping(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert gws_auth.profile_config_dir(cfg, "personal") == tmp_path / "secrets" / "gws-personal"
    assert gws_auth.profile_config_dir(cfg, "work") == tmp_path / "secrets" / "gws"
    assert gws_auth.profile_config_dir(cfg, "Client A!") == tmp_path / "secrets" / "gws-client-a"
    assert gws_auth.profile_config_dir(cfg, "!!!") is None


def test_scopes_differ_by_profile() -> None:
    assert "drive" in gws_auth.scopes_for_profile("work")
    assert "drive" not in gws_auth.scopes_for_profile("personal")


def test_build_auth_url_includes_state_and_client() -> None:
    url = gws_auth.build_auth_url(
        client_id="cid", redirect_uri="http://127.0.0.1:5000/", scopes="openid", state="xyz"
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert "client_id=cid" in url
    assert "state=xyz" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A5000%2F" in url


def test_extract_code_from_input() -> None:
    assert gws_auth.extract_code_from_input("plain-code") == "plain-code"
    assert gws_auth.extract_code_from_input("http://localhost/?code=abc&scope=x") == "abc"
    with pytest.raises(ValueError):
        gws_auth.extract_code_from_input("http://localhost/?error=access_denied")
    with pytest.raises(ValueError):
        gws_auth.extract_code_from_input("http://localhost/?state=only")


def test_store_credentials_writes_0600_and_retires_stale(tmp_path: Path) -> None:
    config_dir = tmp_path / "secrets" / "gws-personal"
    config_dir.mkdir(parents=True)
    (config_dir / "credentials.enc").write_text("stale", encoding="utf-8")
    gws_auth.store_credentials(
        config_dir,
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtok",
        email="me@example.com",
    )
    creds_path = config_dir / "credentials.json"
    creds = json.loads(creds_path.read_text())
    assert creds["refresh_token"] == "rtok"
    assert creds["email"] == "me@example.com"
    assert (creds_path.stat().st_mode & 0o777) == 0o600
    # Stale encrypted copy is moved aside so gws doesn't keep using it.
    assert not (config_dir / "credentials.enc").exists()
    assert (config_dir / "credentials.enc.old").exists()


def test_exchange_and_store_uses_injected_exchange(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    config_dir = gws_auth.profile_config_dir(cfg, "personal")
    _write_client_secret(config_dir)

    def fake_exchange(*, client_id, client_secret, code, redirect_uri):
        assert client_id == "cid"
        assert code == "the-code"
        # id_token payload carries the email (base64url of a JSON blob).
        import base64

        payload = base64.urlsafe_b64encode(b'{"email":"who@example.com"}').decode()
        return {"refresh_token": "rtok", "id_token": f"h.{payload}.s"}

    monkeypatch.setattr(gws_auth, "exchange_code", fake_exchange)
    result = gws_auth.exchange_and_store(
        cfg, "personal", code="the-code", redirect_uri="http://127.0.0.1:9/"
    )
    assert result == {"ok": True, "email": "who@example.com"}
    creds = json.loads((config_dir / "credentials.json").read_text())
    assert creds["refresh_token"] == "rtok"


def test_exchange_and_store_errors_without_refresh_token(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    _write_client_secret(gws_auth.profile_config_dir(cfg, "personal"))
    monkeypatch.setattr(gws_auth, "exchange_code", lambda **kw: {"access_token": "x"})
    with pytest.raises(ValueError, match="No refresh token"):
        gws_auth.exchange_and_store(cfg, "personal", code="c", redirect_uri="r")


# ── auth_status ─────────────────────────────────────────────────────────────


def _install_wrapper(tmp_path: Path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "gws-profile.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    from ciao import tool_path

    monkeypatch.setattr(tool_path, "resolve_tool", lambda name: "/usr/bin/gws")
    monkeypatch.setattr(tool_path, "login_shell_path", lambda: "/usr/bin")


def test_auth_status_parses_valid(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    _install_wrapper(tmp_path, monkeypatch)

    def runner(*args, **kwargs):
        out = (
            "Using keyring backend: file\n"
            '{"token_valid": true, "has_refresh_token": true, "token_error": ""}\n'
        )
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    status = gws_auth.auth_status(cfg, "personal", runner=runner)
    assert status == {
        "available": True,
        "token_valid": True,
        "token_error": "",
        "has_refresh_token": True,
    }


def test_auth_status_parses_revoked(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    _install_wrapper(tmp_path, monkeypatch)

    def runner(*args, **kwargs):
        out = '{"token_valid": false, "token_error": "Token has been expired or revoked.", "has_refresh_token": true}'
        return subprocess.CompletedProcess(args, 1, stdout=out, stderr="")

    status = gws_auth.auth_status(cfg, "personal", runner=runner)
    assert status["available"] is True
    assert status["token_valid"] is False
    assert "revoked" in status["token_error"]


def test_auth_status_unavailable_when_wrapper_missing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)  # no scripts/gws-profile.sh
    status = gws_auth.auth_status(cfg, "personal")
    assert status["available"] is False


# ── GwsHealthMonitor ─────────────────────────────────────────────────────────


class _FakePush:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, payload: dict) -> None:
        self.sent.append(payload)


class _FakeEvents:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, payload: dict) -> None:
        self.published.append(payload)


def test_health_monitor_debounces_and_rearms(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    runtime = tmp_path / ".runtime"
    # personal is configured (has credentials.json); work is not.
    config_dir = gws_auth.profile_config_dir(cfg, "personal")
    config_dir.mkdir(parents=True)
    (config_dir / "credentials.json").write_text("{}", encoding="utf-8")

    valid = {"available": True, "token_valid": True, "token_error": "", "has_refresh_token": True}
    invalid = {
        "available": True,
        "token_valid": False,
        "token_error": "Token has been expired or revoked.",
        "has_refresh_token": True,
    }
    state = {"value": invalid}
    push, events = _FakePush(), _FakeEvents()
    monitor = gws_auth.GwsHealthMonitor(
        cfg,
        push_manager=push,
        events_hub=events,
        runtime_root=runtime,
        status_fn=lambda config, profile: state["value"],
    )

    # First invalid check → one notification for the affected profile.
    s1 = monitor.check_once()
    assert s1["invalid"] == ["personal"]
    assert s1["notified"] == ["personal"]
    assert len(push.sent) == 1
    assert push.sent[0]["profile"] == "personal"
    assert "personal" in push.sent[0]["body"]
    assert len(events.published) == 1
    assert events.published[0]["type"] == "gws_health"

    # Still invalid → debounced, no new notification.
    s2 = monitor.check_once()
    assert s2["notified"] == []
    assert len(push.sent) == 1

    # Recovered → clears the alert (re-arms).
    state["value"] = valid
    monitor.check_once()
    cache = gws_auth.read_health_cache(runtime)
    assert cache["personal"]["token_valid"] is True
    assert cache["personal"]["notified_invalid"] is False

    # Breaks again → re-notifies.
    state["value"] = invalid
    s4 = monitor.check_once()
    assert s4["notified"] == ["personal"]
    assert len(push.sent) == 2


def test_health_monitor_skips_unavailable(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    config_dir = gws_auth.profile_config_dir(cfg, "personal")
    config_dir.mkdir(parents=True)
    (config_dir / "credentials.json").write_text("{}", encoding="utf-8")
    push = _FakePush()
    monitor = gws_auth.GwsHealthMonitor(
        cfg,
        push_manager=push,
        runtime_root=tmp_path / ".runtime",
        status_fn=lambda config, profile: {"available": False, "reason": "gws missing"},
    )
    summary = monitor.check_once()
    assert summary["checked"] == ["personal"]
    assert summary["invalid"] == []
    assert push.sent == []


# ── GwsReloginManager (loopback callback captured in-process) ────────────────


def _drive_callback(port: int, query: str) -> None:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/{query}", timeout=5) as resp:
        resp.read()


def test_relogin_completes_via_loopback(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_client_secret(gws_auth.profile_config_dir(cfg, "personal"))

    captured: dict = {}

    def fake_exchange(config, profile, *, code, redirect_uri):
        captured["code"] = code
        captured["redirect_uri"] = redirect_uri
        return {"ok": True, "email": "loop@example.com"}

    manager = gws_auth.GwsReloginManager(cfg, exchange_fn=fake_exchange, session_ttl=10)
    started = manager.start("personal")
    assert started["ok"] is True
    assert "accounts.google.com" in started["auth_url"]
    port, state = started["port"], started["state"]
    assert f":{port}/" in started["redirect_uri"]

    # A mismatched state must be ignored (session stays pending).
    _drive_callback(port, "?code=evil&state=wrong")
    assert manager.status("personal")["status"] == "pending"

    # The real redirect with the matching state completes the exchange.
    _drive_callback(port, f"?code=good-code&state={state}")
    final = manager.wait("personal", timeout=5)
    assert final["status"] == "completed"
    assert final["email"] == "loop@example.com"
    assert captured["code"] == "good-code"
    assert captured["redirect_uri"] == started["redirect_uri"]


def test_relogin_reports_google_error(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_client_secret(gws_auth.profile_config_dir(cfg, "personal"))
    manager = gws_auth.GwsReloginManager(
        cfg, exchange_fn=lambda *a, **k: {"ok": True}, session_ttl=10
    )
    started = manager.start("personal")
    _drive_callback(started["port"], f"?error=access_denied&state={started['state']}")
    final = manager.wait("personal", timeout=5)
    assert final["status"] == "error"
    assert "access_denied" in final["error"]


def test_relogin_requires_client_secret(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    manager = gws_auth.GwsReloginManager(cfg)
    with pytest.raises(ValueError, match="client_secret.json not found"):
        manager.start("personal")


def test_relogin_rejects_unknown_profile(tmp_path: Path) -> None:
    manager = gws_auth.GwsReloginManager(_config(tmp_path))
    with pytest.raises(ValueError, match="Invalid profile"):
        manager.start("bogus")
