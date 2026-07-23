"""Google Workspace OAuth helpers, token-health monitoring, and a
server-managed re-login flow.

This module centralizes the Google Workspace (GWS) OAuth logic that used to
live inline in ``ciao/web/routes_api.py`` so it can be reused by:

* the existing PWA-native OAuth panel (upload ``client_secret.json`` → get the
  consent URL → paste the redirect code back → exchange it server-side), and
* the new *reliable re-login* flow (:class:`GwsReloginManager`), which keeps the
  loopback OAuth callback server alive **inside the long-lived engine process**
  so an agent/chat can trigger re-login without the listener dying between
  turns.

It also provides :class:`GwsHealthMonitor`, a cheap periodic check of each
configured profile's token validity that surfaces a PWA notification and an
in-app status signal (debounced) when a login goes dead.

Security invariants (see issue #145):

* OAuth tokens, client secrets, and authorization codes are **never** printed,
  logged, or written anywhere except the per-profile credential files. Error
  paths surface only coarse, secret-free descriptions.
* All loopback callback listeners bind to ``127.0.0.1`` only and validate the
  OAuth ``state`` parameter before touching the authorization code.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

BUILTIN_PROFILES = ("personal", "work")

# OAuth scopes granted per profile. Both profiles request the full set of core
# Workspace services gws supports, so the in-process re-login flow can mint
# tokens that cover any feature the user turns on later (Forms, Contacts, etc.)
# without a re-consent round-trip. Keep this list in sync with
# `FULL_SCOPES` in `scripts/gws-auth-helper.py` (both ciao and ciaobot copies).
# Extra/enterprise services (admin-reports, keep, classroom, chat, meet) are
# omitted because they need admin grants or extra API enablement; pass a
# custom scope set to `GwsReloginManager.start` when one is required.
_PERSONAL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify "
    "https://www.googleapis.com/auth/calendar "
    "https://www.googleapis.com/auth/drive "
    "https://www.googleapis.com/auth/spreadsheets "
    "https://www.googleapis.com/auth/documents "
    "https://www.googleapis.com/auth/presentations "
    "https://www.googleapis.com/auth/tasks "
    "https://www.googleapis.com/auth/contacts "
    "https://www.googleapis.com/auth/forms.body "
    "openid "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile"
)
_WORK_SCOPES = _PERSONAL_SCOPES

_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/auth"

# gws prints this banner to stdout before JSON; strip it before parsing.
_KEYRING_BANNER = re.compile(r"^\s*Using keyring backend:.*$", re.MULTILINE)

HEALTH_CACHE_NAME = "gws_health.json"


# ── Path + client_secret helpers ─────────────────────────────────────────


def profile_config_dir(config, profile: str) -> Path | None:
    """Credential directory for a profile under ``<workspace>/secrets``.

    Mirrors the wrapper script's ``personal`` → ``gws-personal`` / ``work`` →
    ``gws`` mapping, and gives wizard-named profiles their own ``gws-<slug>``
    directory. Returns ``None`` for a profile whose slug is empty.
    """
    root = Path(config.workspace_root).resolve()
    if profile == "personal":
        return root / "secrets" / "gws-personal"
    if profile == "work":
        return root / "secrets" / "gws"
    safe = re.sub(r"[^a-z0-9_-]+", "-", profile.strip().lower()).strip("-")
    if not safe:
        return None
    return root / "secrets" / f"gws-{safe}"


def scopes_for_profile(profile: str) -> str:
    return _WORK_SCOPES if profile == "work" else _PERSONAL_SCOPES


def load_client_secret(config_dir: Path) -> dict[str, Any]:
    """Return the ``installed``/``web`` section of a profile's client secret.

    Raises :class:`ValueError` (secret-free message) if the file is missing or
    malformed.
    """
    secret_path = config_dir / "client_secret.json"
    if not secret_path.is_file():
        raise ValueError("client_secret.json not found for this profile")
    with open(secret_path, "r", encoding="utf-8") as handle:
        secret = json.load(handle)
    installed: dict[str, Any] = secret.get("installed") or secret.get("web")
    if not installed:
        raise ValueError("client_secret.json missing 'installed' or 'web' section")
    return installed


# ── Consent URL + token exchange (shared by all flows) ───────────────────


def build_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str | None = None,
) -> str:
    params = {
        "scope": scopes,
        "access_type": "offline",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "client_id": client_id,
        "prompt": "select_account consent",
    }
    if state:
        params["state"] = state
    return _AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def extract_code_from_input(code_or_url: str) -> str:
    """Accept either a bare code or a full redirect URL and return the code.

    Raises :class:`ValueError` when the URL carries an ``error`` or no ``code``.
    """
    code = (code_or_url or "").strip()
    if "code=" in code or code.startswith("http"):
        parsed = urllib.parse.urlparse(code)
        query = urllib.parse.parse_qs(parsed.query)
        if "error" in query:
            raise ValueError(f"Google returned error: {query['error'][0]}")
        if "code" not in query:
            raise ValueError("No authorization 'code' found in the redirect URL")
        code = query["code"][0]
    return code


def extract_email_from_id_token(id_token: str | None) -> str:
    if not id_token:
        return ""
    try:
        import base64

        parts = id_token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1]
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            payload_json = base64.urlsafe_b64decode(
                payload_b64.encode("utf-8")
            ).decode("utf-8")
            return json.loads(payload_json).get("email") or ""
    except Exception:
        pass
    return ""


def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens at Google's token endpoint.

    Blocking (uses ``urllib``); call from a worker thread. Raises
    :class:`ValueError` with a secret-free message on failure. The returned
    dict is the raw token response and MUST NOT be logged.
    """
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            return payload
    except urllib.error.HTTPError as exc:
        try:
            err_json = json.loads(exc.read().decode("utf-8"))
            desc = (
                err_json.get("error_description")
                or err_json.get("error")
                or "Unknown OAuth error"
            )
        except Exception:
            desc = f"HTTP {exc.code}"
        raise ValueError(f"Token exchange failed: {desc}") from None
    except Exception as exc:  # network, JSON, etc.
        raise ValueError(f"Token exchange failed: {exc}") from None


def store_credentials(
    config_dir: Path,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    email: str = "",
) -> None:
    """Write ``credentials.json`` (0600) and retire any stale encrypted copy.

    The refresh token lives only inside this file; nothing here is logged.
    """
    creds: dict[str, Any] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "type": "authorized_user",
    }
    if email:
        creds["email"] = email

    for name in ("credentials.enc", "token_cache.json"):
        stale = config_dir / name
        if stale.exists():
            backup = config_dir / (name + ".old")
            try:
                if backup.exists():
                    backup.unlink()
                stale.rename(backup)
            except Exception as exc:
                logger.warning("Failed to move stale %s: %s", name, exc)

    config_dir.mkdir(parents=True, exist_ok=True)
    creds_path = config_dir / "credentials.json"
    creds_path.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    creds_path.chmod(0o600)

    key_file = config_dir / ".encryption_key"
    if key_file.exists():
        try:
            key_file.chmod(0o600)
        except Exception as exc:
            logger.warning("Failed to fix .encryption_key permissions: %s", exc)


def exchange_and_store(
    config,
    profile: str,
    *,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Full server-side code→credentials step, reused by every flow.

    Returns ``{"ok": True, "email": ...}`` on success. Raises
    :class:`ValueError` with a secret-free message otherwise.
    """
    config_dir = profile_config_dir(config, profile)
    if config_dir is None:
        raise ValueError("Could not determine config directory")
    installed = load_client_secret(config_dir)
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    if not client_id or not client_secret:
        raise ValueError("client_secret.json missing client_id or client_secret")

    tokens = exchange_code(
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
    )
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise ValueError(
            "No refresh token returned. The account might already be authorized. "
            "Revoke the old grant at https://myaccount.google.com/permissions "
            "and try again."
        )
    email = extract_email_from_id_token(tokens.get("id_token"))
    store_credentials(
        config_dir,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        email=email,
    )
    return {"ok": True, "email": email}


# ── Token health (cheap ``auth status`` ping) ─────────────────────────────


def wrapper_path(config) -> Path:
    return Path(config.workspace_root).resolve() / "scripts" / "gws-profile.sh"


def auth_status(
    config,
    profile: str,
    *,
    timeout: float = 30.0,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    """Run ``scripts/gws-profile.sh <profile> auth status`` and parse the JSON.

    Returns a dict with ``available`` (whether the check could run at all) and,
    when available, ``token_valid`` / ``token_error`` / ``has_refresh_token``.
    Never logs the raw subprocess output.
    """
    from ciao.tool_path import login_shell_path, resolve_tool

    script = wrapper_path(config)
    if not script.is_file():
        return {"available": False, "reason": "wrapper script not found"}
    if not resolve_tool("gws"):
        return {"available": False, "reason": "gws CLI not installed"}

    env = dict(os.environ)
    env["PATH"] = login_shell_path()
    try:
        result = runner(
            ["bash", str(script), profile, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except Exception as exc:
        return {"available": False, "reason": f"status check failed: {exc}"}

    stdout = _KEYRING_BANNER.sub("", result.stdout or "").strip()
    # gws emits a JSON object somewhere in stdout; isolate it defensively.
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {"available": False, "reason": "no JSON in status output"}
    try:
        payload = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError:
        return {"available": False, "reason": "unparseable status output"}

    return {
        "available": True,
        "token_valid": bool(payload.get("token_valid")),
        "token_error": str(payload.get("token_error") or ""),
        "has_refresh_token": bool(payload.get("has_refresh_token")),
    }


def read_health_cache(runtime_root: Path) -> dict[str, dict[str, Any]]:
    """Return the persisted per-profile health snapshot (fail-open: {})."""
    path = Path(runtime_root) / HEALTH_CACHE_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    profiles = data.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


class GwsHealthMonitor:
    """Debounced token-validity check for configured GWS profiles.

    On the transition to an invalid token it emits a PWA notification (via the
    push manager) and an in-app ``gws_health`` status event (via the events
    hub). It does not re-notify while the token stays invalid, and it re-arms
    once the token recovers.
    """

    def __init__(
        self,
        config,
        *,
        push_manager=None,
        events_hub=None,
        runtime_root: Path | None = None,
        status_fn: Callable[..., dict[str, Any]] = auth_status,
    ) -> None:
        self._config = config
        self._push = push_manager
        self._events = events_hub
        self._runtime = Path(
            runtime_root
            if runtime_root is not None
            else Path(config.state_path).parent
        )
        self._status_fn = status_fn
        self._lock = threading.Lock()

    def _cache_path(self) -> Path:
        return self._runtime / HEALTH_CACHE_NAME

    def _load(self) -> dict[str, dict[str, Any]]:
        return read_health_cache(self._runtime)

    def _save(self, profiles: dict[str, dict[str, Any]]) -> None:
        self._runtime.mkdir(parents=True, exist_ok=True)
        self._cache_path().write_text(
            json.dumps({"profiles": profiles}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _configured_profiles(self) -> list[str]:
        out: list[str] = []
        for profile in BUILTIN_PROFILES:
            config_dir = profile_config_dir(self._config, profile)
            if config_dir is None:
                continue
            if any(
                (config_dir / name).is_file()
                for name in ("credentials.json", "credentials.enc")
            ):
                out.append(profile)
        return out

    def check_once(self) -> dict[str, Any]:
        """Check every configured profile once. Returns a summary for logging.

        Serialized with a lock so overlapping periodic + on-demand runs cannot
        interleave their read-modify-write of the cache.
        """
        with self._lock:
            state = self._load()
            summary: dict[str, Any] = {"checked": [], "invalid": [], "notified": []}
            for profile in self._configured_profiles():
                status = self._status_fn(self._config, profile)
                summary["checked"].append(profile)
                prior = state.get(profile, {})
                if not status.get("available"):
                    # Could not check (gws missing, transient error): leave the
                    # prior state untouched so we neither spam nor clear a
                    # standing alert on a flaky probe.
                    continue
                token_valid = bool(status.get("token_valid"))
                token_error = status.get("token_error", "")
                entry = {
                    "token_valid": token_valid,
                    "token_error": token_error,
                    "has_refresh_token": bool(status.get("has_refresh_token")),
                    "checked_at": time.time(),
                    "notified_invalid": bool(prior.get("notified_invalid")),
                }
                if not token_valid:
                    summary["invalid"].append(profile)
                    if not prior.get("notified_invalid"):
                        self._notify(profile, token_error)
                        entry["notified_invalid"] = True
                        summary["notified"].append(profile)
                else:
                    entry["notified_invalid"] = False
                state[profile] = entry
            try:
                self._save(state)
            except Exception:
                logger.exception("Failed to persist GWS health cache")
            return summary

    def _notify(self, profile: str, token_error: str) -> None:
        title = "Google Workspace login needs attention"
        body = (
            f"The '{profile}' Google login has expired or been revoked. "
            "Re-authenticate in Settings → Integrations to restore Gmail, "
            "Calendar, Drive, and scheduled Google tasks."
        )
        if self._push is not None:
            try:
                self._push.send(
                    {
                        "title": title,
                        "body": body,
                        "kind": "gws_health",
                        "profile": profile,
                    }
                )
            except Exception:
                logger.exception("Failed to send GWS health push")
        if self._events is not None:
            try:
                self._events.publish(
                    {
                        "type": "gws_health",
                        "profile": profile,
                        "token_valid": False,
                        # token_error is a coarse Google message (no secret).
                        "token_error": token_error,
                        "title": title,
                        "body": body,
                    }
                )
            except Exception:
                logger.exception("Failed to publish GWS health event")


# ── Reliable re-login (in-process loopback callback server) ───────────────


@dataclass
class _ReloginSession:
    profile: str
    state: str
    port: int
    redirect_uri: str
    auth_url: str
    created_at: float
    expires_at: float
    server: HTTPServer
    thread: threading.Thread
    status: str = "pending"  # pending | completed | error
    email: str = ""
    error: str = ""
    _done: threading.Event = field(default_factory=threading.Event)


class GwsReloginManager:
    """Runs the OAuth consent→callback→exchange flow in-process.

    Unlike ``gws auth login`` in a background bash task (which dies between
    chat turns), the loopback callback listener here lives in the long-lived
    engine process, so the redirect is always captured and the code exchanged.
    Only builtin profiles are supported (the wrapper's ``personal``/``work``).
    """

    def __init__(
        self,
        config,
        *,
        exchange_fn: Callable[..., dict[str, Any]] = exchange_and_store,
        session_ttl: float = 300.0,
    ) -> None:
        self._config = config
        self._exchange_fn = exchange_fn
        self._ttl = session_ttl
        self._lock = threading.Lock()
        self._sessions: dict[str, _ReloginSession] = {}

    def start(self, profile: str) -> dict[str, Any]:
        """Begin a re-login: bind a loopback listener and return the consent URL.

        Raises :class:`ValueError` (secret-free) if the profile is unknown or
        has no ``client_secret.json``.
        """
        if profile not in BUILTIN_PROFILES:
            raise ValueError(f"Invalid profile: {profile}")
        config_dir = profile_config_dir(self._config, profile)
        if config_dir is None:
            raise ValueError("Could not determine config directory")
        installed = load_client_secret(config_dir)
        client_id = installed.get("client_id")
        if not client_id:
            raise ValueError("client_secret.json missing client_id")

        with self._lock:
            self._cancel_locked(profile)

            state = secrets.token_urlsafe(24)
            handler_cls = self._make_handler(profile, state)
            # Port 0 → OS assigns a free ephemeral loopback port. Google's
            # installed-app OAuth allows a loopback redirect on any port.
            server = HTTPServer(("127.0.0.1", 0), handler_cls)
            port = server.server_address[1]
            redirect_uri = f"http://127.0.0.1:{port}/"
            auth_url = build_auth_url(
                client_id=client_id,
                redirect_uri=redirect_uri,
                scopes=scopes_for_profile(profile),
                state=state,
            )
            now = time.time()
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"gws-relogin-{profile}",
                daemon=True,
            )
            session = _ReloginSession(
                profile=profile,
                state=state,
                port=port,
                redirect_uri=redirect_uri,
                auth_url=auth_url,
                created_at=now,
                expires_at=now + self._ttl,
                server=server,
                thread=thread,
            )
            server._ciao_session = session  # type: ignore[attr-defined]
            self._sessions[profile] = session
            thread.start()
            self._arm_timeout(session)

        return {
            "ok": True,
            "profile": profile,
            "auth_url": auth_url,
            "state": state,
            "port": port,
            "redirect_uri": redirect_uri,
            "expires_in": int(self._ttl),
        }

    def status(self, profile: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(profile)
            if session is None:
                return {"status": "none", "profile": profile}
            return {
                "status": session.status,
                "profile": profile,
                "email": session.email,
                "error": session.error,
                "expires_in": max(0, int(session.expires_at - time.time())),
            }

    def cancel(self, profile: str) -> dict[str, Any]:
        with self._lock:
            existed = self._cancel_locked(profile)
        return {"ok": True, "cancelled": existed, "profile": profile}

    def wait(self, profile: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until the callback resolves (used by tests)."""
        with self._lock:
            session = self._sessions.get(profile)
        if session is None:
            return {"status": "none", "profile": profile}
        session._done.wait(timeout)
        return self.status(profile)

    # ── internals ────────────────────────────────────────────────────

    def _cancel_locked(self, profile: str) -> bool:
        session = self._sessions.pop(profile, None)
        if session is None:
            return False
        self._shutdown_server(session)
        return True

    @staticmethod
    def _shutdown_server(session: _ReloginSession) -> None:
        def _stop() -> None:
            try:
                session.server.shutdown()
                session.server.server_close()
            except Exception:
                pass

        # shutdown() must run off the serving thread.
        threading.Thread(target=_stop, name="gws-relogin-stop", daemon=True).start()

    def _arm_timeout(self, session: _ReloginSession) -> None:
        def _expire() -> None:
            time.sleep(self._ttl)
            with self._lock:
                current = self._sessions.get(session.profile)
                if current is not session:
                    return
                if session.status == "pending":
                    session.status = "error"
                    session.error = "Re-login timed out before the redirect arrived."
                    session._done.set()
                self._cancel_locked(session.profile)

        threading.Thread(
            target=_expire, name=f"gws-relogin-timeout-{session.profile}", daemon=True
        ).start()

    def _finish(
        self,
        session: _ReloginSession,
        *,
        code: str | None = None,
        error: str | None = None,
    ) -> None:
        """Called from the callback handler thread with the captured redirect."""
        if session.status != "pending":
            return
        if error:
            session.status = "error"
            session.error = error
            session._done.set()
        else:
            try:
                result = self._exchange_fn(
                    self._config,
                    session.profile,
                    code=code or "",
                    redirect_uri=session.redirect_uri,
                )
                session.email = result.get("email", "")
                session.status = "completed"
            except Exception as exc:
                session.status = "error"
                session.error = str(exc)
            session._done.set()
        # Tear the listener down after a single redirect (off the serving thread).
        self._shutdown_server(session)

    def _make_handler(self, profile: str, expected_state: str):
        manager = self

        class _CallbackHandler(BaseHTTPRequestHandler):
            # Silence the default stderr access log so codes never leak.
            def log_message(self, *args: Any) -> None:  # noqa: D401
                return

            def do_GET(self) -> None:  # noqa: N802
                session: _ReloginSession = self.server._ciao_session  # type: ignore[attr-defined]
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                got_state = (query.get("state") or [""])[0]
                error = (query.get("error") or [""])[0]
                code = (query.get("code") or [""])[0]

                if error:
                    manager._finish(session, error=f"Google returned error: {error}")
                    self._respond(False, "Google reported an error. You can close this tab.")
                    return
                if got_state != expected_state:
                    # Do not touch the code for a mismatched state.
                    self._respond(False, "Ignoring an unexpected callback.")
                    return
                if not code:
                    manager._finish(session, error="No authorization code in redirect.")
                    self._respond(False, "No authorization code received.")
                    return
                manager._finish(session, code=code)
                ok = session.status == "completed"
                self._respond(
                    ok,
                    "Google Workspace re-login complete. You can close this tab "
                    "and return to Ciaobot."
                    if ok
                    else "Re-login failed. Return to Ciaobot for details.",
                )

            def _respond(self, ok: bool, message: str) -> None:
                body = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>Ciaobot · Google re-login</title></head>"
                    "<body style='font-family:system-ui;padding:2rem;'>"
                    f"<h2>{'OK' if ok else 'Attention'} · Ciaobot</h2>"
                    f"<p>{message}</p></body></html>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass

        return _CallbackHandler
