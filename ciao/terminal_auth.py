"""Bounded, revocable authorization grants for the browser terminal.

This is the *authorization primitive* the browser terminal will use, and
nothing else: no route, no shell, no session, no file. It exists because
Ciaobot's login cookie is not an identity. Two browsers that sign in within
the same second can hold byte-identical cookies, so "the login cookie" cannot
answer "which browser is this?" — and a terminal that runs a shell needs an
answer to that question that survives a logout, a password change and an
expiry.

So a terminal grant is its own credential:

* the **bearer** is 256 random bits, minted by
  :meth:`TerminalGrantStore.issue` and handed to the caller exactly once. The
  store retains only its SHA-256 digest, so the records it keeps name the
  bearer without carrying it. The raw bearer exists in ``issue``'s local
  variable and in its return value, and a frame capture or a debugger that
  is looking at that call can see it there — that is where a bearer is while
  it is being minted, and the caller that puts it in a cookie owns it from
  then on.
* the **grant id** is a separate random, non-secret identifier. It names the
  grant in a log line or a UI row without carrying any authority, and it is
  distinct from the bearer: knowing a grant id must never be enough to
  unlock anything.
* the **session binding** is a SHA-256 digest of the login cookie the
  unlock arrived on. It is internal binding metadata: a grant is only valid
  for the browser that minted it, and a fresh unlock replaces the grant it
  renews rather than piling up. A route that answers a client must shape the
  fields it returns explicitly — a grant is never serialized as a whole.

The grant lifetime is fixed (not sliding): a verified grant keeps the same
deadline it was minted with, so a browser cannot keep a terminal alive by
talking to it. Grants are bounded in number, expired grants are reclaimed,
and revocation is explicit — one grant, or all of them.

The password policy is the second half of the module. Unlocking a terminal
asks for the PWA password again, so guessing it must be expensive: a rolling
window of failures per client address, a closed window that even a correct
password cannot pass, and a bounded number of tracked clients so the
limiter's own memory cannot be used as the attack surface.

**What this module does not do: it authenticates nobody.** A route must
verify the session cookie (that it is signed, unexpired and the caller's)
and the unlock password before it calls in here. The store binds a grant to
a session it is handed and compares bytes; it trusts the identity its caller
gives it. In the same way, ``client_ip`` must be the address the *server*
selected — the TCP peer, or whatever the deployment's proxy trust policy
decides. A value taken from a request body or an untrusted header is not a
client address, and normalizing one does not make it trustworthy.

Everything here is in memory and dies with the process. A grant is not a
durable session: it is a bounded, short-lived capability, and a restart
ends every one of them.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

#: The cookie the browser terminal's grant will travel in. Host-only,
#: HttpOnly, SameSite=Strict, ``Path=/`` (the terminal needs it for both
#: ``/api/terminal`` and ``/ws/terminal``). The name is fixed here so the
#: routes and the frontend cannot disagree about it.
TERMINAL_COOKIE = "ciao_terminal"

#: How long a grant lives. Fixed, not sliding: verifying a grant never
#: extends it.
TERMINAL_UNLOCK_TTL_S = 900

#: How many grants can be live at once. A grant is minted by an unlock, so
#: this bounds how many browsers can hold terminal access.
TERMINAL_MAX_GRANTS = 32

#: Failed unlock attempts allowed per client address inside the window.
TERMINAL_UNLOCK_FAIL_LIMIT = 5

#: The rolling window those failures are counted in.
TERMINAL_UNLOCK_FAIL_WINDOW_S = 900

#: How many client addresses can be tracked as failing at once.
TERMINAL_MAX_FAILURE_CLIENTS = 256

#: Bearer strength: 32 random bytes, URL-safe.
_TOKEN_BYTES = 32

#: The grant id is an identifier, not a credential: 16 random bytes is
#: plenty to be unique without being guessable enough to matter.
_GRANT_ID_BYTES = 16

#: The one bucket every address the server cannot parse is counted against.
#: A fixed key, so a caller cannot make the table grow by varying the
#: spelling of an unusable address.
_UNKNOWN_CLIENT_KEY = "unknown"


class TerminalAuthError(Exception):
    """A typed refusal from the terminal-grant store.

    ``reason`` is a closed vocabulary, so a caller can map it to a status
    code without parsing prose:

    - ``invalid_session``: the unlock arrived without a usable login cookie.
    - ``capacity``: too many live grants; the oldest has to expire first.
    - ``rate_limited``: the client is inside a closed guessing window, or
      the failure table is full and no new client may start guessing.

    The message is the reason and nothing else: a refused attempt must not
    put a password, a bearer or a cookie into an error string.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TerminalGrant:
    """A granted terminal authorization.

    This is both the record a caller receives and the record the store
    retains (under the bearer's digest), so it holds digests and safe
    metadata only: never the bearer, never the login cookie.

    ``grant_id`` names the grant without carrying authority. ``expires_at``
    is a reading of the injected monotonic clock, and it is the deadline the
    grant was minted with — verifying it never moves it.
    ``session_fingerprint`` is internal binding metadata; a route shapes the
    fields it returns rather than handing this record out whole.
    """

    grant_id: str
    #: SHA-256 of the login cookie this grant is bound to. A digest, never
    #: the cookie: excluded from the repr so a diagnostic dump does not
    #: spread a session-identifying value.
    session_fingerprint: str = field(repr=False)
    issued_at: float
    expires_at: float


def _encode(value: str) -> bytes | None:
    """UTF-8 bytes, or ``None`` when the value is not valid UTF-8.

    A credential can arrive from a hostile body carrying a lone surrogate,
    which UTF-8 refuses. Refusing it here is what keeps a malformed
    credential a *value* the caller can refuse, rather than an exception
    that skips the accounting that has to follow it.
    """
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _digest_bytes(data: bytes) -> str:
    """SHA-256 of bytes, as lowercase hex."""
    return hashlib.sha256(data).hexdigest()


def _digest(value: str) -> str | None:
    """SHA-256 of a value, as lowercase hex — or ``None`` if it is not UTF-8."""
    encoded = _encode(value)
    if encoded is None:
        return None
    return _digest_bytes(encoded)


def _client_key(client_ip: str) -> str:
    """The canonical failure bucket for a client address.

    Two spellings of one address are one bucket (``2001:0db8::1`` and
    ``2001:db8::1``), and an IPv4-mapped IPv6 address is the IPv4 address it
    carries (``::ffff:10.0.0.1`` is ``10.0.0.1``). Two genuinely different
    addresses stay different buckets, so the IPv4 and the IPv6 loopback are
    two budgets.

    An address the server cannot parse is one fixed unknown-client bucket,
    not a new key per spelling — otherwise a caller could grow the table
    without bound by varying a string the server already refuses to read.
    """
    try:
        address = ipaddress.ip_address(client_ip.strip())
    except ValueError:
        return _UNKNOWN_CLIENT_KEY
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return str(mapped)
    return str(address)


class TerminalGrantStore:
    """Live terminal grants and the unlock guessing budget.

    One store per engine, held in memory. The clock is injected so the
    expiry and the rolling failure window are testable without sleeping.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # Keyed by the bearer's SHA-256 digest: the store never holds the
        # bearer itself.
        self._grants: dict[str, TerminalGrant] = {}
        self._failures: dict[str, list[float]] = {}
        # The engine reads grants from request handlers and from a watchdog
        # that closes sockets; the lock keeps a check-then-write (the
        # capacity check) from interleaving across threads.
        self._lock = threading.Lock()

    # ── Grants ─────────────────────────────────────────────────────────────

    def issue(
        self, session_cookie: str, *, replace_token: str = ""
    ) -> tuple[str, TerminalGrant]:
        """Mint a grant bound to ``session_cookie`` and return ``(bearer, grant)``.

        The bearer is returned exactly once. The caller puts it in the
        terminal cookie and is responsible for it from then on.

        ``replace_token`` is the bearer of the grant this unlock renews. It
        is revoked only when it is a live grant bound to *this* session, so
        a renewal neither accumulates a grant per unlock nor lets a bearer
        from another session end one. A replacement that is unknown, expired,
        malformed or bound to another session is a no-op, not an error.
        """
        if not session_cookie.strip():
            raise TerminalAuthError("invalid_session")
        fingerprint = _digest(session_cookie)
        if fingerprint is None:
            raise TerminalAuthError("invalid_session")
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            if replace_token:
                self._revoke_replacement_locked(replace_token, fingerprint)
            if len(self._grants) >= TERMINAL_MAX_GRANTS:
                raise TerminalAuthError("capacity")

            token = secrets.token_urlsafe(_TOKEN_BYTES)
            # token_urlsafe emits base64url characters, so this cannot fail
            # to encode the way a caller's value can.
            token_digest = _digest_bytes(token.encode("ascii"))
            grant = TerminalGrant(
                grant_id=secrets.token_urlsafe(_GRANT_ID_BYTES),
                session_fingerprint=fingerprint,
                issued_at=now,
                expires_at=now + TERMINAL_UNLOCK_TTL_S,
            )
            self._grants[token_digest] = grant
            return token, grant

    def verify(self, token: str, session_cookie: str) -> TerminalGrant | None:
        """The grant ``token`` authorizes, or ``None``.

        ``None`` is the answer for every failure — an unknown token, an
        expired one, one presented with a different login cookie, one that is
        not valid UTF-8 — so a caller cannot tell them apart, and nothing
        about the attempt is echoed back. The session binding is compared
        with :func:`hmac.compare_digest`.
        """
        if not token or not session_cookie:
            return None
        token_digest = _digest(token)
        fingerprint = _digest(session_cookie)
        if token_digest is None or fingerprint is None:
            return None
        now = self._clock()
        with self._lock:
            record = self._grants.get(token_digest)
            if record is None:
                return None
            if now >= record.expires_at:
                del self._grants[token_digest]
                return None
            if not hmac.compare_digest(record.session_fingerprint, fingerprint):
                return None
            return record

    def revoke(self, token: str) -> None:
        """Revoke the grant ``token`` names, and only that one.

        A logout presents the grant it is ending; other grants that happen
        to share a login cookie are other browsers' grants and stay live.
        A token that is not valid UTF-8 names nothing, so it revokes
        nothing.
        """
        if not token:
            return
        digest = _digest(token)
        if digest is None:
            return
        with self._lock:
            self._grants.pop(digest, None)

    def revoke_all(self) -> None:
        """Revoke every live grant (a password change ends them all)."""
        with self._lock:
            self._grants.clear()

    def prune(self) -> None:
        """Drop the grants whose fixed deadline has passed."""
        with self._lock:
            self._prune_locked(self._clock())

    def retained_grant_count(self) -> int:
        """Retained records, including expired ones not yet pruned."""
        with self._lock:
            return len(self._grants)

    def _prune_locked(self, now: float) -> None:
        for digest, record in list(self._grants.items()):
            if now >= record.expires_at:
                del self._grants[digest]

    def _revoke_replacement_locked(self, token: str, fingerprint: str) -> None:
        """Revoke the grant a renewal names, when it is this session's.

        A replacement that is unknown, expired, malformed or bound to
        another session is a no-op: there is nothing of ours to revoke, and
        another session's grant is not ours to end. Defence in depth against
        a caller whose credentials do not match — not a claim that an
        unauthenticated caller could take a grant over.
        """
        digest = _digest(token)
        if digest is None:
            return
        record = self._grants.get(digest)
        if record is None:
            return
        if not hmac.compare_digest(record.session_fingerprint, fingerprint):
            return
        del self._grants[digest]

    # ── Password policy ────────────────────────────────────────────────────

    def check_password(self, client_ip: str, supplied: str, expected: str) -> bool:
        """Whether ``supplied`` is the terminal unlock password.

        ``True`` only for a correct password. A blank ``expected`` never
        authenticates: an engine with no password configured has no terminal
        unlock, and an empty guess is not one either. A password that is not
        valid UTF-8 is a failed attempt, not an exception.

        The failure budget is per client *address* — normalized, so two
        spellings of one address share one bucket. Five failures inside
        :data:`TERMINAL_UNLOCK_FAIL_WINDOW_S` close the window for that
        address, and a closed window refuses *every* attempt, the correct
        password included, until it clears. A success before the limit
        clears that address's count, which is also how a success behind a
        shared address clears the count for everyone else behind it.

        The costs of that policy are deliberate and are the caller's to
        weigh: a client that trips the window cannot unlock until the
        window ages out or the engine restarts, and a full failure table
        locks every untracked client out. There is no bypass and no
        exception path; the only way back is time or a restart, both of
        which clear the in-memory counts.

        :raises TerminalAuthError: with reason ``rate_limited`` when the
            window is closed, or when the failure table is full and this
            client is not already tracked. A full table refuses new
            guessing rather than evicting a live bucket, because evicting
            one would hand the next guess a fresh budget. That refusal is
            fail-closed for the whole table, the correct password
            included: a full table is a locked door, not a queue.
        """
        key = _client_key(client_ip)
        now = self._clock()
        with self._lock:
            self._prune_failures_locked(now)
            failures = self._failures.get(key)
            if failures is not None:
                if len(failures) >= TERMINAL_UNLOCK_FAIL_LIMIT:
                    raise TerminalAuthError("rate_limited")
            elif len(self._failures) >= TERMINAL_MAX_FAILURE_CLIENTS:
                # Every tracked client is inside its own window; a new one
                # may not start guessing, and no live bucket is dropped to
                # make room for it.
                raise TerminalAuthError("rate_limited")

            supplied_bytes = _encode(supplied)
            expected_bytes = _encode(expected)
            if (
                supplied_bytes is not None
                and expected_bytes is not None
                and expected.strip()
                and hmac.compare_digest(supplied_bytes, expected_bytes)
            ):
                self._failures.pop(key, None)
                return True

            # A wrong guess, a blank expected password and a malformed one
            # are all the same thing to this budget: a failed attempt.
            self._failures.setdefault(key, []).append(now)
            return False

    def _prune_failures_locked(self, now: float) -> None:
        """Forget the failures that fell out of the rolling window."""
        window_start = now - TERMINAL_UNLOCK_FAIL_WINDOW_S
        for key in list(self._failures):
            recent = [stamp for stamp in self._failures[key] if stamp > window_start]
            if recent:
                self._failures[key] = recent
            else:
                del self._failures[key]
