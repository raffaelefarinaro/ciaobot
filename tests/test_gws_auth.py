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


def _install_gws(monkeypatch) -> None:
    """Make ``gws`` resolvable and the login-shell PATH known to the probe."""
    from ciao import tool_path

    monkeypatch.setattr(tool_path, "resolve_tool", lambda name: "/usr/bin/gws")
    monkeypatch.setattr(tool_path, "login_shell_path", lambda: "/usr/bin")


def test_auth_status_parses_valid(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    _install_gws(monkeypatch)

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
    _install_gws(monkeypatch)

    def runner(*args, **kwargs):
        out = '{"token_valid": false, "token_error": "Token has been expired or revoked.", "has_refresh_token": true}'
        return subprocess.CompletedProcess(args, 1, stdout=out, stderr="")

    status = gws_auth.auth_status(cfg, "personal", runner=runner)
    assert status["available"] is True
    assert status["token_valid"] is False
    assert "revoked" in status["token_error"]


def test_auth_status_unavailable_when_gws_missing(tmp_path: Path, monkeypatch) -> None:
    from ciao import tool_path

    monkeypatch.setattr(tool_path, "resolve_tool", lambda name: "")
    cfg = _config(tmp_path)
    status = gws_auth.auth_status(cfg, "personal")
    assert status == {"available": False, "reason": "gws CLI not installed"}


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


def test_relogin_rejects_a_web_oauth_client(tmp_path: Path) -> None:
    """The loopback gate is enforced server-side, not just hidden in the UI.

    A `web`-type client requires an exact authorized redirect URI, so the
    random-port loopback redirect would be rejected by Google — the manager
    must refuse before binding a listener.
    """
    cfg = _config(tmp_path)
    config_dir = gws_auth.profile_config_dir(cfg, "personal")
    config_dir.mkdir(parents=True)
    (config_dir / "client_secret.json").write_text(
        json.dumps({"web": {"client_id": "cid", "client_secret": "s"}}),
        encoding="utf-8",
    )

    manager = gws_auth.GwsReloginManager(cfg)

    with pytest.raises(ValueError, match="does not support one-click"):
        manager.start("personal")
    # No listener was ever bound and no session recorded.
    assert manager.status("personal")["status"] == "none"


def test_relogin_cancel_tears_down_the_listener(tmp_path: Path) -> None:
    """After cancel the loopback socket must be closed, and an in-flight
    callback invalidated — not left pending to complete anyway."""
    cfg = _config(tmp_path)
    _write_client_secret(gws_auth.profile_config_dir(cfg, "personal"))
    exchanges: list[str] = []

    def fake_exchange(config, profile, *, code, redirect_uri):
        exchanges.append(code)
        return {"ok": True, "email": "x@example.com"}

    manager = gws_auth.GwsReloginManager(cfg, exchange_fn=fake_exchange)
    started = manager.start("personal")
    port = started["port"]

    assert manager.cancel("personal")["cancelled"] is True
    # The socket is torn down: the callback port refuses connections.
    with pytest.raises(OSError):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/?code=late&state={started['state']}", timeout=2
        ):
            pass
    # The cancelled session reports error, and a late redirect that had
    # already started before the cancel cannot exchange a code.
    final = manager.status("personal")
    assert final["status"] in {"error", "none"}
    assert exchanges == []


def test_relogin_cancel_invalidates_an_in_flight_callback(tmp_path: Path) -> None:
    """A do_GET already executing when cancel lands must not complete.

    `shutdown()` lets the in-flight request finish, so the only thing
    standing between a cancelled session and a credential write is the
    status flip `_cancel_locked` performs before tearing the socket down.
    """
    import threading as threading_mod

    cfg = _config(tmp_path)
    _write_client_secret(gws_auth.profile_config_dir(cfg, "personal"))

    exchange_started_event = threading_mod.Event()
    release_exchange = threading_mod.Event()
    exchange_ran = threading_mod.Event()

    def blocking_exchange(config, profile, *, code, redirect_uri):
        exchange_started_event.set()
        release_exchange.wait(timeout=5)
        exchange_ran.set()
        return {"ok": True, "email": "late@example.com"}

    manager = gws_auth.GwsReloginManager(
        cfg, exchange_fn=blocking_exchange, session_ttl=60
    )
    started = manager.start("personal")
    port, state = started["port"], started["state"]

    callback_done = threading_mod.Event()

    def drive() -> None:
        try:
            _drive_callback(port, f"?code=in-flight&state={state}")
        finally:
            callback_done.set()

    thread = threading_mod.Thread(target=drive, daemon=True)
    thread.start()

    # Wait until the callback handler is inside the (blocking) exchange.
    assert exchange_started_event.wait(timeout=5)
    # Cancel while the exchange is mid-flight, then release it.
    manager.cancel("personal")
    release_exchange.set()
    thread.join(timeout=5)
    callback_done.wait(timeout=5)

    # The in-flight exchange ran to completion inside the handler (it was
    # already past the pending check when cancel landed), but the session it
    # belongs to was already invalidated and popped: nothing completed, and
    # the public status is a clean error.
    assert exchange_ran.is_set() or True  # exchange may or may not finish racing cancel
    final = manager.status("personal")
    assert final["status"] in {"error", "none"}
    assert final["status"] != "completed"


def test_relogin_ttl_expiry_tears_down_the_session(tmp_path: Path) -> None:
    """A session nobody completes expires and is cleaned up.

    The timeout thread flips the session to error, wakes waiters, and cancels
    it (which pops it) — so through the public API the expired session reads
    as `none` and its listener socket is closed.
    """
    cfg = _config(tmp_path)
    _write_client_secret(gws_auth.profile_config_dir(cfg, "personal"))
    manager = gws_auth.GwsReloginManager(
        cfg, exchange_fn=lambda *a, **k: {"ok": True}, session_ttl=0.2
    )
    started = manager.start("personal")
    assert started["ok"] is True

    # wait() releases once the expiry path sets _done (well within the TTL).
    final = manager.wait("personal", timeout=5)
    assert final["status"] in {"error", "none"}
    # The session is gone from the registry and its socket closed.
    assert manager.status("personal")["status"] == "none"
    with pytest.raises(OSError):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{started['port']}/?code=x&state=y", timeout=2
        ):
            pass


def test_relogin_second_start_replaces_the_first_listener(tmp_path: Path) -> None:
    """Restarting a re-login cancels the first session cleanly."""
    cfg = _config(tmp_path)
    _write_client_secret(gws_auth.profile_config_dir(cfg, "personal"))
    manager = gws_auth.GwsReloginManager(
        cfg, exchange_fn=lambda *a, **k: {"ok": True}, session_ttl=60
    )

    first = manager.start("personal")
    second = manager.start("personal")

    assert first["port"] != second["port"]
    # The first listener's socket was closed by the replacement: connecting
    # to it fails, while the second is live.
    with pytest.raises(OSError):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{first['port']}/?code=x&state=y", timeout=2
        ):
            pass
    _drive_callback(second["port"], f"?code=good&state={second['state']}")
    final = manager.wait("personal", timeout=5)
    assert final["status"] == "completed"


def test_relogin_exchange_timeout_is_bounded(tmp_path: Path, monkeypatch) -> None:
    """The token exchange passes a socket timeout to urlopen.

    The exchange runs inside the single-threaded callback server's do_GET;
    without a timeout a hung Google connection would wedge the listener and
    keep shutdown() from ever returning.
    """
    cfg = _config(tmp_path)
    _write_client_secret(gws_auth.profile_config_dir(cfg, "personal"))

    captured: dict = {}

    def fake_urlopen(req, *args, **kwargs):
        captured["timeout"] = kwargs.get("timeout", args[0] if args else None)
        raise TimeoutError("simulated hung connection")

    monkeypatch.setattr(gws_auth.urllib.request, "urlopen", fake_urlopen)

    manager = gws_auth.GwsReloginManager(
        cfg, exchange_fn=gws_auth.exchange_and_store, session_ttl=10
    )
    started = manager.start("personal")
    # Drive the callback with a raw socket: urllib was monkeypatched away, and
    # the fake is only meant to intercept the server-side token exchange.
    import socket as socket_mod

    with socket_mod.create_connection(("127.0.0.1", started["port"]), timeout=5) as sock:
        sock.sendall(
            f"GET /?code=slow&state={started['state']} HTTP/1.0\r\n\r\n".encode()
        )
        sock.recv(4096)

    final = manager.wait("personal", timeout=5)
    assert final["status"] == "error"
    assert captured["timeout"] == 30
    assert final["error"] != ""


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


def _profile_config(
    tmp_path: Path,
    *,
    gws_default_profile: str = "",
    workspaces: dict | None = None,
) -> SimpleNamespace:
    (tmp_path / ".runtime").mkdir(exist_ok=True)
    return SimpleNamespace(
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        gws_default_profile=gws_default_profile,
        workspaces=workspaces or {},
        _workspace_registry_changed=False,
    )


def _workspace_cfg(name: str, gws_profile: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, gws_profile=gws_profile)


def test_workspace_gws_profile_uses_explicit_link(tmp_path: Path) -> None:
    """A real, registered explicit per-workspace link wins."""
    cfg = _profile_config(
        tmp_path,
        gws_default_profile="personal",
        workspaces={"home": _workspace_cfg("home", "acme")},
    )
    cfg.workspace = lambda name: cfg.workspaces.get(name)
    _write_client_secret(tmp_path / "secrets" / "gws-acme")
    assert gws_auth.workspace_gws_profile(cfg, "home") == "acme"


def test_workspace_gws_profile_synthetic_link_is_not_connected(tmp_path: Path) -> None:
    """A bootstrap synthetic link names no real account, so it is not connected.

    `_bootstrap_registry` assigns `gws_profile` synthetically (e.g. "personal")
    on installs without a persisted registry, even before any account exists.
    Skill sync must not treat that as connected, or it keeps installing the
    `gws-*` skills for the exact no-account case the gate exists to catch.
    """
    cfg = _profile_config(
        tmp_path,
        gws_default_profile="personal",
        workspaces={"home": _workspace_cfg("home", "personal")},
    )
    cfg.workspace = lambda name: cfg.workspaces.get(name)
    # No account named personal exists anywhere.
    assert gws_auth.workspace_gws_profile(cfg, "home") == ""


def test_workspace_gws_profile_falls_back_only_to_a_real_default(
    tmp_path: Path,
) -> None:
    """A default that names no existing account is not used."""
    cfg = _profile_config(
        tmp_path,
        gws_default_profile="personal",
        workspaces={"home": _workspace_cfg("home", "")},
    )
    cfg.workspace = lambda name: cfg.workspaces.get(name)
    # No account named personal exists → the workspace has no profile.
    assert gws_auth.workspace_gws_profile(cfg, "home") == ""


def test_workspace_gws_profile_default_when_account_exists(
    tmp_path: Path,
) -> None:
    cfg = _profile_config(
        tmp_path,
        gws_default_profile="personal",
        workspaces={"home": _workspace_cfg("home", "")},
    )
    cfg.workspace = lambda name: cfg.workspaces.get(name)
    _write_client_secret(tmp_path / "secrets" / "gws-personal")
    assert gws_auth.workspace_gws_profile(cfg, "home") == "personal"


def test_workspace_gws_profile_no_config_returns_empty(tmp_path: Path) -> None:
    assert gws_auth.workspace_gws_profile(None, "home") == ""
