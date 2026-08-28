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

import base64
import hashlib
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
from typing import Any, Callable, Sequence

from ciao.jsonio import write_private_text

logger = logging.getLogger(__name__)

# OAuth scopes granted per profile. Both profiles request the full set of core
# Workspace services gws supports, so the in-process re-login flow can mint
# tokens that cover any feature the user turns on later (Forms, Contacts, etc.)
# without a re-consent round-trip. Keep this list in sync with
# `FULL_SCOPES` in the old `scripts/gws-auth-helper.py` (now `ciao/gws_auth_helper.py`).
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

# These names are positional commands to ``gws``.  Profile slugs using one of
# them would be indistinguishable from the service argument in
# ``ciao gws <profile> <service> ...``.
GWS_SERVICE_NAMES = frozenset(
    {
        "gmail",
        "calendar",
        "drive",
        "docs",
        "sheets",
        "slides",
        "tasks",
        "contacts",
        "forms",
        "auth",
    }
)

_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/auth"

# gws prints this banner to stdout before JSON; strip it before parsing.
_KEYRING_BANNER = re.compile(r"^\s*Using keyring backend:.*$", re.MULTILINE)

HEALTH_CACHE_NAME = "gws_health.json"


def fingerprint(value: str) -> str:
    """Short irreversible digest so client_secret.json contents never hit stdout."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


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


# ── Account registry ─────────────────────────────────────────────────────
#
# Which Google accounts exist is the user's choice, not ours. ``personal`` and
# ``work`` are only the two names whose credential directories predate the
# registry, so they keep their legacy paths and labels; a fresh install starts
# with no accounts at all and the user adds the ones they actually have.

PROFILE_REGISTRY_NAME = "gws_profiles.json"

# Directory name → profile name for the two pre-registry layouts.
_LEGACY_DIR_PROFILES = {"gws": "work", "gws-personal": "personal"}

_PROFILE_MATERIAL = ("credentials.json", "credentials.enc", "client_secret.json")


def slugify_profile(name: str) -> str:
    """Normalize a user-supplied account name into a profile slug."""
    return re.sub(r"[^a-z0-9_-]+", "-", str(name).strip().lower()).strip("-")


def profile_registry_path(config) -> Path:
    return Path(config.state_path).parent / PROFILE_REGISTRY_NAME


def load_profile_registry(config) -> list[dict[str, str]]:
    """Accounts the user has added, in display order. Missing file → empty."""
    try:
        raw = json.loads(profile_registry_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = slugify_profile(item.get("name", ""))
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append({"name": name, "label": str(item.get("label", "")).strip()})
    return entries


def save_profile_registry(config, entries: Sequence[dict[str, str]]) -> None:
    """Atomically persist the account registry."""
    path = profile_registry_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {"name": slugify_profile(entry.get("name", "")), "label": str(entry.get("label", "")).strip()}
        for entry in entries
        if slugify_profile(entry.get("name", ""))
    ]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def discover_profiles_on_disk(config) -> list[str]:
    """Profiles that already have credential material under ``secrets/``.

    This is what keeps an install that predates the registry whole: whatever
    ``personal``/``work`` (or custom) account was connected before stays
    listed without the user re-adding it.
    """
    secrets_dir = Path(config.workspace_root).resolve() / "secrets"
    try:
        entries = [p for p in secrets_dir.iterdir() if p.is_dir()]
    except OSError:
        return []
    found: list[str] = []
    for entry in entries:
        if entry.name in _LEGACY_DIR_PROFILES:
            profile = _LEGACY_DIR_PROFILES[entry.name]
        elif entry.name.startswith("gws-"):
            profile = entry.name[len("gws-") :]
        else:
            continue
        if not profile or profile in found:
            continue
        if any((entry / name).is_file() for name in _PROFILE_MATERIAL):
            found.append(profile)
    return sorted(found)


def known_profiles(config) -> list[str]:
    """Every Google account this install should show, registry order first."""
    names = [entry["name"] for entry in load_profile_registry(config)]
    for profile in discover_profiles_on_disk(config):
        if profile not in names:
            names.append(profile)
    return names


def workspace_gws_profile(config, workspace_name: str | None) -> str:
    """The Google account a workspace actually uses, or "" when none is linked.

    An explicit per-workspace link and the operator-level default both only
    count when they name an account that actually exists: pointing a chat at a
    credential directory nobody ever created just produces confusing auth
    errors mid-task. The explicit link is validated against the same
    ``known_profiles()`` set as the default, because on an install without a
    persisted registry the bootstrap registry assigns ``gws_profile``
    synthetically (e.g. ``"personal"``) even before any account is connected.
    A workspace with no resolvable account gets "" — which is exactly the
    "no profile connected to this workspace" case skill sync must recognise.
    """
    if not config:
        return ""
    known: set[str] | None = None

    def _known() -> set[str]:
        nonlocal known
        if known is None:
            try:
                known = set(known_profiles(config))
            except Exception:
                known = set()
        return known

    workspace_config = getattr(config, "workspace", lambda _name: None)(workspace_name)
    explicit = str(getattr(workspace_config, "gws_profile", "") or "")
    if explicit and explicit in _known():
        return explicit
    if explicit:
        # A synthetic or stale explicit link (no account actually exists).
        # Fall through to the default rather than treating it as connected.
        pass
    default = getattr(config, "gws_default_profile", "")
    if default and default in _known():
        return default
    return ""


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


def client_uses_loopback(config_dir: Path | None) -> bool:
    """Whether a profile's OAuth client can use the random-port loopback redirect.

    ``GwsReloginManager.start`` always redirects to ``http://127.0.0.1:<port>/``.
    Installed/desktop clients accept any loopback port, but *web* clients require
    an exact authorized redirect URI match, so the one-click flow cannot complete
    for them. Returns ``False`` when the client is a ``web`` app, and ``True``
    for an ``installed``/desktop client (or when the file is absent, so the UI
    can still offer the button before an upload).
    """
    if config_dir is None:
        return True
    secret_path = config_dir / "client_secret.json"
    if not secret_path.is_file():
        return True
    try:
        with open(secret_path, "r", encoding="utf-8") as handle:
            secret = json.load(handle)
    except (OSError, ValueError):
        return True
    # A web section (with no installed section) is the incompatible case.
    return bool(secret.get("installed")) or not bool(secret.get("web"))



# ── PKCE (RFC 7636) ───────────────────────────────────────────────────────
#
# Manual/paste OAuth flows (the PWA "paste the redirect code" panel, and the
# headless `ciao gws-auth-helper`) cannot validate `state` on paste-back: the
# user, not the browser, carries the code across the trust boundary, so a
# copy-pasted `state` is not proof of anything. PKCE is the RFC 8252 remedy —
# it binds the authorization code to the client that requested it, so a code
# intercepted or replayed elsewhere is useless without the verifier that
# never left this process. See issue #354.

# RFC 7636 §4.1: 43-128 characters from [A-Za-z0-9-._~]. `secrets.token_urlsafe`
# already draws only from the base64url alphabet (A-Za-z0-9-_), a subset of
# that unreserved charset, so no extra filtering is needed.
_PKCE_MIN_LENGTH = 43
_PKCE_MAX_LENGTH = 128


def generate_code_verifier(length: int = 64) -> str:
    """Return a cryptographically random PKCE code verifier (RFC 7636 §4.1)."""
    if not (_PKCE_MIN_LENGTH <= length <= _PKCE_MAX_LENGTH):
        raise ValueError(
            f"PKCE code verifier length must be between {_PKCE_MIN_LENGTH} and {_PKCE_MAX_LENGTH}"
        )
    verifier = ""
    while len(verifier) < length:
        verifier += secrets.token_urlsafe(length)
    return verifier[:length]


def code_challenge_s256(verifier: str) -> str:
    """Derive the S256 PKCE code challenge from a verifier (RFC 7636 §4.2).

    base64url(SHA-256(ASCII(verifier))), no padding.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ── Consent URL + token exchange (shared by all flows) ───────────────────


def build_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str = "S256",
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
    if code_challenge:
        # Optional: Google's OAuth endpoint accepts PKCE for installed-app
        # clients and ignores it for a client that predates this, so an
        # in-flight legacy flow (URL built before this code shipped) is
        # unaffected — the exchange below only sends a verifier when a
        # challenge was actually requested.
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = code_challenge_method
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
    code_verifier: str | None = None,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens at Google's token endpoint.

    Blocking (uses ``urllib``); call from a worker thread. Raises
    :class:`ValueError` with a secret-free message on failure. The returned
    dict is the raw token response and MUST NOT be logged.

    ``code_verifier`` is the PKCE verifier matching the ``code_challenge`` sent
    to :func:`build_auth_url` for this flow (issue #354); omitted when the
    auth URL was built without one (or predates PKCE support).

    The socket timeout matters: the exchange runs inside the single-threaded
    callback server's ``do_GET``, so an unbounded request would hang the
    listener and keep ``shutdown()`` from ever returning, leaking the socket.
    """
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        _TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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


def normalize_scopes(scopes: Sequence[str] | str | None) -> list[str]:
    """Return a sorted, deduped list of OAuth scope URLs.

    Accepts the typical shapes: a list/tuple of scope URLs, a single
    space-separated string from the token endpoint, or ``None``.
    """
    if not scopes:
        return []
    if isinstance(scopes, str):
        parts = scopes.split()
    else:
        parts = []
        for s in scopes:
            if s:
                parts.extend(s.split())
    return sorted({p for p in parts if p})


# Scope URL → the name a user would recognise, and the order chips render in.
# This lives next to the scope sets above deliberately: adding a scope there
# without naming it here means Settings shows a raw googleapis.com URL to the
# user, which is how `openid` and the two `userinfo.*` scopes — requested by
# every profile — ended up rendered verbatim in the profile description.
_SCOPE_LABELS: dict[str, str] = {
    "https://www.googleapis.com/auth/gmail.modify": "Gmail",
    "https://www.googleapis.com/auth/gmail.readonly": "Gmail (read)",
    "https://www.googleapis.com/auth/calendar": "Calendar",
    "https://www.googleapis.com/auth/drive": "Drive",
    "https://www.googleapis.com/auth/documents": "Docs",
    "https://www.googleapis.com/auth/spreadsheets": "Sheets",
    "https://www.googleapis.com/auth/presentations": "Slides",
    "https://www.googleapis.com/auth/tasks": "Tasks",
    "https://www.googleapis.com/auth/contacts": "Contacts",
    "https://www.googleapis.com/auth/forms.body": "Forms",
}

# Granted to every profile purely to identify the account. Naming them in the
# UI would be noise ("Connected to ... and openid"), so they are dropped rather
# than labelled — but they must be listed, or they fall through as raw URLs.
_SIGN_IN_SCOPES = frozenset({
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
})


def scope_labels(scopes: Sequence[str] | str | None) -> list[str]:
    """Map granted scope URLs to display names, in a stable render order.

    Sign-in scopes are dropped. An unrecognised scope is kept as its verbatim
    URL rather than silently vanishing — an ugly chip is a bug report; a
    missing one is invisible.
    """
    granted = set(normalize_scopes(scopes))
    chips = [label for url, label in _SCOPE_LABELS.items() if url in granted]
    chips += sorted(
        url for url in granted
        if url not in _SCOPE_LABELS and url not in _SIGN_IN_SCOPES
    )
    return chips


def store_credentials(
    config_dir: Path,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    email: str = "",
    scopes: Sequence[str] | str | None = None,
) -> None:
    """Write ``credentials.json`` (0600) and retire any stale encrypted copy.

    The refresh token lives only inside this file; nothing here is logged.

    ``scopes`` records the OAuth scopes that were actually granted at consent
    time so the Settings UI can show what the user connected. Accepts a
    sequence of full scope URLs, a single space-separated string, or ``None``.
    """
    creds: dict[str, Any] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "type": "authorized_user",
    }
    if email:
        creds["email"] = email
    normalized_scopes = normalize_scopes(scopes)
    if normalized_scopes:
        creds["scopes"] = normalized_scopes

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
    try:
        # everything under here is OAuth material; tighten the profile dir for
        # installs whose older setup left it group/world-readable
        config_dir.chmod(0o700)
    except OSError as exc:
        logger.warning("Failed to tighten %s permissions: %s", config_dir, exc)
    creds_path = config_dir / "credentials.json"
    # owner-only from creation: this file carries the refresh token
    write_private_text(creds_path, json.dumps(creds, indent=2))

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
    code_verifier: str | None = None,
) -> dict[str, Any]:
    """Full server-side code→credentials step, reused by every flow.

    ``code_verifier`` is the PKCE verifier generated alongside the auth URL
    for this flow, when one was used (issue #354); pass ``None`` for a flow
    that did not send a ``code_challenge``.

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
        code_verifier=code_verifier,
    )
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise ValueError(
            "No refresh token returned. The account might already be authorized. "
            "Revoke the old grant at https://myaccount.google.com/permissions "
            "and try again."
        )
    email = extract_email_from_id_token(tokens.get("id_token"))
    granted_scopes = tokens.get("scope") or ""
    store_credentials(
        config_dir,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        email=email,
        scopes=granted_scopes,
    )
    return {"ok": True, "email": email}


# ── Manual/paste flow PKCE state (issue #354) ─────────────────────────────
#
# The manual "upload client_secret.json → get consent URL → paste the
# redirect back" panel is genuinely stateless server-side today: the auth-url
# and exchange endpoints are two independent requests that share nothing but
# the profile name. PKCE needs the verifier generated for the auth URL to be
# the one sent at exchange time, so this tiny store carries it across that
# gap — the smallest piece of cross-request state the flow needs, kept only
# long enough for a human to consent in the browser and paste the code back.

# A generous window. PKCE's threat model is a code intercepted or replayed
# outside this process (a hostile inspecting network traffic or a browser
# history), not how long the verifier sits in memory — the verifier is opaque
# random data held only here and is replaced the instant `start` runs again
# for the same profile, so a long TTL costs essentially nothing. It has to
# comfortably outlast a human reading Google's consent screen, picking an
# account, granting scopes, and copying the redirect URL back — a distracted
# or slow user must not be punished for taking their time (issue #354).
MANUAL_PKCE_TTL_SECONDS = 3600.0


class ManualPkceStore:
    """Holds pending PKCE verifiers between auth-url and exchange.

    Each flow has its own opaque identifier, so multiple tabs can authorize the
    same profile without replacing one another's verifier. Never logged;
    verifiers are opaque, secret-adjacent material.

    Expired and superseded entries are kept as tombstones, so :meth:`status`
    can tell "a challenge was issued but its verifier is gone" apart from "no
    PKCE flow was ever started" — the exchange endpoint needs that distinction:
    the first case must fail loudly (Google is holding a challenge for the
    code; silently omitting the verifier gets a confusing ``invalid_grant``),
    while the second must silently omit the verifier, matching whatever the
    auth URL itself sent (issue #354). A restart of the server process is the
    one case this cannot help with — the tombstone lives only in memory — see
    ``gws_exchange_code`` for how that degrades.
    """

    def __init__(self, ttl: float = MANUAL_PKCE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        # flow_id -> (profile, verifier or "" once invalidated, expires_at,
        # status: active/expired/superseded)
        self._pending: dict[str, tuple[str, str, float, str]] = {}

    def start(self, profile: str) -> tuple[str, str]:
        """Generate a flow ID and verifier for ``profile``."""
        flow_id = secrets.token_urlsafe(32)
        verifier = generate_code_verifier()
        with self._lock:
            self._pending[flow_id] = (profile, verifier, time.time() + self._ttl, "active")
        return flow_id, verifier

    def peek(self, flow_id: str, profile: str | None = None) -> str | None:
        """Return the live verifier for ``flow_id``, or ``None``.

        ``None`` covers both "no flow pending" and "expired" — callers that
        need to tell those apart (to avoid silently sending Google's token
        endpoint a request with no verifier when one is expected) should use
        :meth:`status` first.

        Non-destructive: a failed paste-back (wrong code, typo) can be
        retried with the same verifier without restarting the whole flow.
        """
        with self._lock:
            entry = self._pending.get(flow_id)
            if entry is None:
                return None
            entry_profile, verifier, expires_at, _status = entry
            if profile is not None and entry_profile != profile:
                return None
            if time.time() > expires_at or not verifier:
                return None
            return verifier

    def status(self, flow_id: str, profile: str | None = None) -> str:
        """``"active"`` | ``"expired"`` | ``"superseded"`` | ``"none"``.

        ``"expired"`` means a challenge was issued and is still live at
        Google but the verifier is gone — the caller must not proceed
        without one. ``"none"`` means no PKCE flow is pending at all (never
        started, already consumed by a later ``start``, or this process
        restarted since the auth URL was built) — a caller may safely treat
        that like a flow that never used PKCE.
        """
        with self._lock:
            entry = self._pending.get(flow_id)
            if entry is None:
                return "none"
            entry_profile, verifier, expires_at, status = entry
            if profile is not None and entry_profile != profile:
                return "none"
            if status == "superseded":
                return status
            if time.time() > expires_at:
                # Tombstone it in place: keep the "something was issued"
                # signal, but drop the verifier itself (never hold expired
                # secret-adjacent material longer than necessary).
                if verifier:
                    self._pending[flow_id] = (entry_profile, "", expires_at, "expired")
                return "expired"
            return "active"

    def status_for_profile(self, profile: str) -> str:
        """Return whether ``profile`` has any active manual flow."""
        with self._lock:
            for entry_profile, _verifier, _expires_at, status in self._pending.values():
                if entry_profile == profile and status == "active":
                    return "active"
        return "none"


# ── Token health (cheap ``auth status`` ping) ─────────────────────────────


def auth_status(
    config,
    profile: str,
    *,
    timeout: float = 30.0,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    """Run ``gws auth status`` for a profile and parse the JSON.

    Computes the profile's environment in-process and invokes ``gws`` directly
    (no bash wrapper dependency), so the check works on an installed app where
    ``scripts/gws-profile.sh`` does not ship.

    Returns a dict with ``available`` (whether the check could run at all) and,
    when available, ``token_valid`` / ``token_error`` / ``has_refresh_token``.
    Never logs the raw subprocess output.
    """
    from ciao.tool_path import login_shell_path, resolve_tool

    if not resolve_tool("gws"):
        return {"available": False, "reason": "gws CLI not installed"}

    env = _profile_env_for_status(config, profile)
    if env is None:
        return {"available": False, "reason": "invalid profile"}
    env["PATH"] = login_shell_path()
    try:
        result = runner(
            ["gws", "auth", "status"],
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


def _profile_env_for_status(config, profile: str) -> dict[str, str] | None:
    """Environment for a profile-aware ``gws`` status check (or None if invalid)."""
    config_dir = profile_config_dir(config, profile)
    if config_dir is None:
        return None
    env = dict(os.environ)
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(config_dir)
    # The workspace .env stores GOOGLE_APPLICATION_CREDENTIALS as a base64 string
    # meant for the BigQuery runner; gws expects a file path and must use its own
    # OAuth token cache, not a service account.
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    return env


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
        # When ``auth status`` reports the token invalid (but the probe itself
        # ran), re-probe once after this delay before believing it. A single
        # ``token_valid: false`` reading can be a transient Google API hiccup
        # (momentary 401/500/rate-limit) or a startup refresh race that
        # recovers on its own; see issue #173.
        retry_delay: float = 8.0,
        # Only surface the "re-authenticate" notification after this many
        # consecutive invalid readings across separate ``check_once`` runs,
        # so one transient reading (even after the in-process retry) cannot
        # false-alarm the user. Reset to 0 on the first valid reading.
        notify_threshold: int = 2,
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
        self._retry_delay = max(0.0, retry_delay)
        self._notify_threshold = max(1, int(notify_threshold))
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
        for profile in known_profiles(self._config):
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

        A single ``token_valid: false`` reading does not notify on its own.
        The monitor first re-probes in-process (``retry_delay``) to ride out
        transient Google API errors or startup refresh races, and then requires
        ``notify_threshold`` consecutive invalid readings across separate runs
        before alerting the user (issue #173). The counter and the
        ``notified_invalid`` flag both reset on the first valid reading.
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
                if not token_valid:
                    # Re-probe once: a single invalid reading may be a transient
                    # false negative (momentary 401/500/rate-limit, or a startup
                    # race where the first refresh fails and a later one
                    # succeeds). Only treat the token as invalid if the retry
                    # agrees. If the retry itself goes unavailable, treat the
                    # whole probe as inconclusive and skip, like an
                    # unavailable first probe.
                    if self._retry_delay > 0:
                        time.sleep(self._retry_delay)
                    retry = self._status_fn(self._config, profile)
                    if not retry.get("available"):
                        continue
                    if retry.get("token_valid"):
                        token_valid = True
                    else:
                        # Prefer the most recent error text if non-empty.
                        retry_error = retry.get("token_error", "")
                        if retry_error:
                            token_error = retry_error
                consecutive_invalid = int(prior.get("consecutive_invalid", 0))
                entry = {
                    "token_valid": token_valid,
                    "token_error": token_error,
                    "has_refresh_token": bool(status.get("has_refresh_token")),
                    "checked_at": time.time(),
                    "notified_invalid": bool(prior.get("notified_invalid")),
                    "consecutive_invalid": consecutive_invalid,
                }
                if not token_valid:
                    summary["invalid"].append(profile)
                    consecutive_invalid += 1
                    entry["consecutive_invalid"] = consecutive_invalid
                    if (
                        consecutive_invalid >= self._notify_threshold
                        and not prior.get("notified_invalid")
                    ):
                        self._notify(profile, token_error)
                        entry["notified_invalid"] = True
                        summary["notified"].append(profile)
                else:
                    entry["notified_invalid"] = False
                    entry["consecutive_invalid"] = 0
                state[profile] = entry
            try:
                self._save(state)
            except Exception:
                logger.exception("Failed to persist GWS health cache")
            return summary

    def _notify(self, profile: str, token_error: str) -> None:
        title = "Google Workspace login needs attention"
        body = (
            f"The '{profile}' Google login may have expired or been revoked. "
            "Re-authenticate in Settings → Workspaces to restore Gmail, "
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
    # PKCE verifier for this session (issue #354). The loopback flow already
    # validates `state`, so this is defense in depth rather than the primary
    # fix (that's the manual/paste flows), but it is a small, safe addition
    # since the session already carries per-flow state end to end.
    code_verifier: str = ""
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

        Raises :class:`ValueError` (secret-free) if the profile is unknown,
        has no ``client_secret.json``, or uses a *web* OAuth client — a web
        client requires an exact authorized redirect URI, so the random-port
        loopback redirect would be rejected by Google. That check is enforced
        here, not only in the UI, so direct API callers get the same gate.
        """
        if profile not in known_profiles(self._config):
            raise ValueError(f"Invalid profile: {profile}")
        config_dir = profile_config_dir(self._config, profile)
        if config_dir is None:
            raise ValueError("Could not determine config directory")
        if not client_uses_loopback(config_dir):
            raise ValueError(
                "This OAuth client is a web app and does not support one-click "
                "sign-in; use manual connect (paste the authorization code)."
            )
        installed = load_client_secret(config_dir)
        client_id = installed.get("client_id")
        if not client_id:
            raise ValueError("client_secret.json missing client_id")

        with self._lock:
            self._cancel_locked(profile)

            state = secrets.token_urlsafe(24)
            code_verifier = generate_code_verifier()
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
                code_challenge=code_challenge_s256(code_verifier),
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
                code_verifier=code_verifier,
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
        # Invalidate BEFORE tearing the listener down, exactly as the timeout
        # path does: `shutdown()` lets an in-flight `do_GET` run to completion,
        # so a callback that started before this cancel would otherwise pass
        # the pending check in `_finish` and still exchange the code + write
        # credentials for a session the operator just cancelled.
        if session.status == "pending":
            session.status = "error"
            session.error = "cancelled"
            session._done.set()
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
                    code_verifier=session.code_verifier,
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
