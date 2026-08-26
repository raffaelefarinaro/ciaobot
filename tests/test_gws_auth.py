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
from urllib.parse import parse_qs, urlparse

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


def test_scopes_cover_all_supported_services() -> None:
    # Both profiles request the full Workspace scope set so the in-process
    # re-login can mint a token that covers any gws subcommand (including
    # Forms, Drive, Contacts) without a re-consent round-trip.
    expected = {
        "gmail.modify",
        "calendar",
        "drive",
        "spreadsheets",
        "documents",
        "presentations",
        "tasks",
        "contacts",
        "forms.body",
    }
    for profile in ("personal", "work"):
        scopes = gws_auth.scopes_for_profile(profile)
        for fragment in expected:
            assert fragment in scopes, f"{profile} scopes missing {fragment}"
        # openid + userinfo are always present.
        assert "openid" in scopes
        assert "userinfo.email" in scopes


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
    # simulate an older install that left the dir and any previous credentials
    # group/world-readable; storing must repair both, not just new files
    config_dir.chmod(0o755)
    creds_path = config_dir / "credentials.json"
    creds_path.write_text("{}", encoding="utf-8")
    creds_path.chmod(0o644)
    gws_auth.store_credentials(
        config_dir,
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtok",
        email="me@example.com",
    )
    creds = json.loads(creds_path.read_text())
    assert creds["refresh_token"] == "rtok"
    assert creds["email"] == "me@example.com"
    assert (creds_path.stat().st_mode & 0o777) == 0o600
    assert (config_dir.stat().st_mode & 0o777) == 0o700
    # Stale encrypted copy is moved aside so gws doesn't keep using it.
    assert not (config_dir / "credentials.enc").exists()
    assert (config_dir / "credentials.enc.old").exists()


def test_store_credentials_persists_granted_scopes(tmp_path: Path) -> None:
    config_dir = tmp_path / "secrets" / "gws-personal"
    config_dir.mkdir(parents=True)
    granted = (
        "https://www.googleapis.com/auth/gmail.modify "
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/gmail.modify"  # duplicate, should dedupe
    )
    gws_auth.store_credentials(
        config_dir,
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtok",
        scopes=granted,
    )
    creds = json.loads((config_dir / "credentials.json").read_text())
    assert creds["scopes"] == sorted(
        {
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
        }
    )


def test_store_credentials_omits_scopes_when_none_granted(tmp_path: Path) -> None:
    config_dir = tmp_path / "secrets" / "gws-personal"
    config_dir.mkdir(parents=True)
    gws_auth.store_credentials(
        config_dir,
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtok",
    )
    creds = json.loads((config_dir / "credentials.json").read_text())
    assert "scopes" not in creds


def test_client_uses_loopback_detects_web_clients(tmp_path: Path) -> None:
    config_dir = tmp_path / "secrets" / "gws-personal"
    config_dir.mkdir(parents=True)
    # Web-only client: the one-click loopback flow cannot work.
    (config_dir / "client_secret.json").write_text(
        json.dumps({"web": {"client_id": "cid", "client_secret": "s"}}),
        encoding="utf-8",
    )
    assert gws_auth.client_uses_loopback(config_dir) is False
    # Desktop/installed client (with no web section): loopback is fine.
    (config_dir / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}}),
        encoding="utf-8",
    )
    assert gws_auth.client_uses_loopback(config_dir) is True
    # Missing file / no dir → True so the UI can still offer the button.
    assert gws_auth.client_uses_loopback(tmp_path / "nope") is True
    assert gws_auth.client_uses_loopback(None) is True


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
        retry_delay=0,  # skip the in-process backoff in tests
        notify_threshold=2,
    )

    # First invalid check → counted but not yet notified (needs 2 consecutive).
    s1 = monitor.check_once()
    assert s1["invalid"] == ["personal"]
    assert s1["notified"] == []
    assert push.sent == []
    cache = gws_auth.read_health_cache(runtime)
    assert cache["personal"]["consecutive_invalid"] == 1
    assert cache["personal"]["notified_invalid"] is False

    # Second consecutive invalid → now notify.
    s2 = monitor.check_once()
    assert s2["notified"] == ["personal"]
    assert len(push.sent) == 1
    assert push.sent[0]["profile"] == "personal"
    assert "personal" in push.sent[0]["body"]
    assert "may have expired or been revoked" in push.sent[0]["body"]
    assert len(events.published) == 1
    assert events.published[0]["type"] == "gws_health"
    cache = gws_auth.read_health_cache(runtime)
    assert cache["personal"]["consecutive_invalid"] == 2
    assert cache["personal"]["notified_invalid"] is True

    # Still invalid → debounced, no new notification.
    s3 = monitor.check_once()
    assert s3["notified"] == []
    assert len(push.sent) == 1

    # Recovered → clears the alert (re-arms) and resets the counter.
    state["value"] = valid
    monitor.check_once()
    cache = gws_auth.read_health_cache(runtime)
    assert cache["personal"]["token_valid"] is True
    assert cache["personal"]["notified_invalid"] is False
    assert cache["personal"]["consecutive_invalid"] == 0

    # Breaks again → needs 2 consecutive again before re-notifying.
    state["value"] = invalid
    s4 = monitor.check_once()
    assert s4["notified"] == []
    assert len(push.sent) == 1
    s5 = monitor.check_once()
    assert s5["notified"] == ["personal"]
    assert len(push.sent) == 2


def test_health_monitor_in_process_retry_suppresses_transient_invalid(tmp_path: Path) -> None:
    """A single transient token_valid=false that recovers on retry must not
    advance the consecutive-failure counter or notify (issue #173)."""
    cfg = _config(tmp_path)
    config_dir = gws_auth.profile_config_dir(cfg, "personal")
    config_dir.mkdir(parents=True)
    (config_dir / "credentials.json").write_text("{}", encoding="utf-8")

    calls: list[str] = []
    valid = {"available": True, "token_valid": True, "token_error": "", "has_refresh_token": True}
    invalid = {
        "available": True,
        "token_valid": False,
        "token_error": "Token has been expired or revoked.",
        "has_refresh_token": True,
    }
    seq = {"i": 0}

    def status_fn(config, profile):
        calls.append("call")
        seq["i"] += 1
        # First probe invalid, retry (same run) valid.
        return invalid if seq["i"] == 1 else valid

    push = _FakePush()
    monitor = gws_auth.GwsHealthMonitor(
        cfg,
        push_manager=push,
        runtime_root=tmp_path / ".runtime",
        status_fn=status_fn,
        retry_delay=0,
        notify_threshold=2,
    )
    summary = monitor.check_once()
    # The retry recovered, so the run reports a valid token and no notify.
    assert summary["invalid"] == []
    assert summary["notified"] == []
    assert push.sent == []
    cache = gws_auth.read_health_cache(tmp_path / ".runtime")
    assert cache["personal"]["token_valid"] is True
    assert cache["personal"]["consecutive_invalid"] == 0
    # status_fn was invoked twice: initial probe + in-process retry.
    assert len(calls) == 2


def test_health_monitor_retry_unavailable_skips(tmp_path: Path) -> None:
    """If the in-process retry itself becomes unavailable, treat the run as
    inconclusive: leave prior state untouched (no counter bump, no notify)."""
    cfg = _config(tmp_path)
    config_dir = gws_auth.profile_config_dir(cfg, "personal")
    config_dir.mkdir(parents=True)
    (config_dir / "credentials.json").write_text("{}", encoding="utf-8")

    invalid = {
        "available": True,
        "token_valid": False,
        "token_error": "Token has been expired or revoked.",
        "has_refresh_token": True,
    }
    unavailable = {"available": False, "reason": "gws missing"}
    seq = {"i": 0}

    def status_fn(config, profile):
        seq["i"] += 1
        return invalid if seq["i"] == 1 else unavailable

    push = _FakePush()
    monitor = gws_auth.GwsHealthMonitor(
        cfg,
        push_manager=push,
        runtime_root=tmp_path / ".runtime",
        status_fn=status_fn,
        retry_delay=0,
        notify_threshold=2,
    )
    summary = monitor.check_once()
    assert summary["invalid"] == []
    assert summary["notified"] == []
    assert push.sent == []
    # No state persisted for the profile on an inconclusive run.
    assert gws_auth.read_health_cache(tmp_path / ".runtime") == {}


def test_health_monitor_single_invalid_does_not_notify_with_default_threshold(
    tmp_path: Path,
) -> None:
    """Default threshold (2) holds even when retry_delay is disabled: one
    invalid run alone never fires a notification."""
    cfg = _config(tmp_path)
    config_dir = gws_auth.profile_config_dir(cfg, "personal")
    config_dir.mkdir(parents=True)
    (config_dir / "credentials.json").write_text("{}", encoding="utf-8")

    invalid = {
        "available": True,
        "token_valid": False,
        "token_error": "Token has been expired or revoked.",
        "has_refresh_token": True,
    }
    push = _FakePush()
    monitor = gws_auth.GwsHealthMonitor(
        cfg,
        push_manager=push,
        runtime_root=tmp_path / ".runtime",
        status_fn=lambda config, profile: invalid,
        retry_delay=0,
    )
    s1 = monitor.check_once()
    assert s1["invalid"] == ["personal"]
    assert s1["notified"] == []
    assert push.sent == []
    cache = gws_auth.read_health_cache(tmp_path / ".runtime")
    assert cache["personal"]["consecutive_invalid"] == 1


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
    auth = urlparse(started["auth_url"])
    assert auth.scheme == "https"
    assert auth.hostname == "accounts.google.com"
    assert auth.path == "/o/oauth2/auth"
    query = parse_qs(auth.query)
    assert query["client_id"] == ["cid"]
    assert query["response_type"] == ["code"]
    assert query["state"] == [started["state"]]
    assert query["redirect_uri"][0] == started["redirect_uri"]
    port, state = started["port"], started["state"]
    assert urlparse(query["redirect_uri"][0]).port == port
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
    # The account exists (the user added it); only its OAuth client is missing.
    gws_auth.save_profile_registry(cfg, [{"name": "personal", "label": "Personal"}])
    manager = gws_auth.GwsReloginManager(cfg)
    with pytest.raises(ValueError, match="client_secret.json not found"):
        manager.start("personal")


def test_relogin_rejects_unknown_profile(tmp_path: Path) -> None:
    """Only accounts this install actually has can be re-logged-in."""
    manager = gws_auth.GwsReloginManager(_config(tmp_path))
    with pytest.raises(ValueError, match="Invalid profile"):
        manager.start("bogus")


# ── Account registry ─────────────────────────────────────────────────────


def test_known_profiles_is_empty_on_a_fresh_install(tmp_path: Path) -> None:
    """No built-in personal/work pair: the user names their own accounts."""
    assert gws_auth.known_profiles(_config(tmp_path)) == []


def test_known_profiles_keeps_accounts_connected_before_the_registry(
    tmp_path: Path,
) -> None:
    """Credential dirs written by an older release still list their accounts."""
    cfg = _config(tmp_path)
    (tmp_path / "secrets" / "gws").mkdir(parents=True)
    (tmp_path / "secrets" / "gws" / "credentials.json").write_text("{}", encoding="utf-8")
    _write_client_secret(tmp_path / "secrets" / "gws-acme")
    # A directory with no credential material is not an account.
    (tmp_path / "secrets" / "gws-empty").mkdir(parents=True)

    assert gws_auth.known_profiles(cfg) == ["acme", "work"]


def test_profile_registry_round_trip_keeps_registry_order_first(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_client_secret(tmp_path / "secrets" / "gws-ondisk")
    gws_auth.save_profile_registry(
        cfg, [{"name": "Acme Corp", "label": "Acme"}, {"name": "side", "label": ""}]
    )

    assert [entry["name"] for entry in gws_auth.load_profile_registry(cfg)] == [
        "acme-corp",
        "side",
    ]
    assert gws_auth.known_profiles(cfg) == ["acme-corp", "side", "ondisk"]


def test_slugify_profile_cannot_escape_the_secrets_directory(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert gws_auth.slugify_profile("../../etc") == "etc"
    assert gws_auth.slugify_profile("   ") == ""
    assert gws_auth.profile_config_dir(cfg, "../../etc") == (
        tmp_path / "secrets" / "gws-etc"
    )
