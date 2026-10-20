"""Bounded, revocable authorization grants for the browser terminal.

This is the *authorization primitive* the browser terminal will use, and
nothing else: no route, no shell, no session, no file. It exists because
Ciaobot's login cookie is not an identity. Two browsers that sign in within
the same second can hold byte-identical cookies, so "the login cookie" cannot
answer "which browser is this?" — and a terminal that runs a shell needs an
answer to that question that survives a logout, a password change and an
expiry.

So a terminal grant is its own credential:

* the **bearer** is 256 random bits handed to the browser exactly once and
  kept by the caller in a host-only, HttpOnly, SameSite=Strict cookie. It
  never leaves the return value of :meth:`TerminalGrantStore.issue`; the
  store keeps a SHA-256 digest of it and never the token itself, so a
  repr, a traceback or a debug dump cannot leak it.
* the **grant id** is a separate random, non-secret identifier. It names the
  grant in a log line or a UI row without carrying any authority, and it is
  distinct from the bearer: knowing a grant id must never be enough to
  unlock anything.
* the **session binding** is a SHA-256 digest of the login cookie the
  unlock arrived on. A grant is only valid for the browser that minted it,
  and a fresh unlock replaces the grant it renews rather than piling up.

The grant lifetime is fixed (not sliding): a verified grant keeps the same
deadline it was minted with, so a browser cannot keep a terminal alive by
talking to it. Grants are bounded in number, expired grants are reclaimed,
and revocation is explicit — one grant, or all of them.

The password policy is the second half of the module. Unlocking a terminal
asks for the PWA password again, so guessing it must be expensive: a rolling
window of failures per client IP, a closed window that even a correct
password cannot pass, and a bounded number of tracked clients so the
limiter's own memory cannot be used as the attack surface.

Everything here is in memory and dies with the process. A grant is not a
durable session: it is a bounded, short-lived capability, and a restart
ends every one of them.
"""

from __future__ import annotations

import hashlib
import hmac
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

#: Failed unlock attempts allowed per client IP inside the window.
TERMINAL_UNLOCK_FAIL_LIMIT = 5

#: The rolling window those failures are counted in.
TERMINAL_UNLOCK_FAIL_WINDOW_S = 900

#: How many client IPs can be tracked as failing at once.
TERMINAL_MAX_FAILURE_CLIENTS = 256

#: Bearer strength: 32 random bytes, URL-safe.
_TOKEN_BYTES = 32

#: The grant id is an identifier, not a credential: 16 random bytes is
#: plenty to be unique without being guessable enough to matter.
_GRANT_ID_BYTES = 16


class TerminalAuthError(Exception):
    """A typed refusal from the terminal-grant store.

    ``reason`` is a closed vocabulary, so a caller can map it to a status
    code without parsing prose:

    - ``invalid_session``: the unlock arrived without a login cookie.
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
    """A granted terminal authorization, as its holder sees it.

    ``grant_id`` names the grant without carrying authority. ``expires_at``
    is a reading of the injected monotonic clock, and it is the deadline the
    grant was minted with — verifying it never moves it.
    """

    grant_id: str
    #: SHA-256 of the login cookie this grant is bound to. A digest, never
    #: the cookie: excluded from the repr so a diagnostic dump cannot
    #: correlate a grant with a live session credential.
    session_fingerprint: str = field(repr=False)
    issued_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class _StoredGrant:
    """What the store keeps for a live grant: digests and deadlines only."""

    grant_id: str
    token_digest: str = field(repr=False)
    session_fingerprint: str = field(repr=False)
    issued_at: float
    expires_at: float


def _digest(value: str) -> str:
    """SHA-256 of a value, as lowercase hex.

    Used for both the bearer and the login-cookie binding: the store never
    holds either secret in the clear, so a dump of its state is a list of
    digests.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TerminalGrantStore:
    """Live terminal grants and the unlock guessing budget.

    One store per engine, held in memory. The clock is injected so the
    expiry and the rolling failure window are testable without sleeping.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._grants: dict[str, _StoredGrant] = {}
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

        The bearer is returned exactly once and is never stored in the
        clear: the caller puts it in the terminal cookie and forgets it.

        ``replace_token`` is the bearer of the grant this unlock renews. It
        is revoked first, so a browser that re-unlocks does not accumulate a
        grant per unlock and its previous grant stops working at once. An
        unknown or already-expired replacement token is not an error: there
        is simply nothing to revoke.
        """
        if not session_cookie.strip():
            raise TerminalAuthError("invalid_session")
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            if replace_token:
                self._revoke_locked(replace_token)
            if len(self._grants) >= TERMINAL_MAX_GRANTS:
                raise TerminalAuthError("capacity")

            token = secrets.token_urlsafe(_TOKEN_BYTES)
            digest = _digest(token)
            grant = TerminalGrant(
                grant_id=secrets.token_urlsafe(_GRANT_ID_BYTES),
                session_fingerprint=_digest(session_cookie),
                issued_at=now,
                expires_at=now + TERMINAL_UNLOCK_TTL_S,
            )
            self._grants[digest] = _StoredGrant(
                grant_id=grant.grant_id,
                token_digest=digest,
                session_fingerprint=grant.session_fingerprint,
                issued_at=now,
                expires_at=grant.expires_at,
            )
            return token, grant

    def verify(self, token: str, session_cookie: str) -> TerminalGrant | None:
        """The grant ``token`` authorizes, or ``None``.

        ``None`` is the answer for every failure — an unknown token, an
        expired one, one presented with a different login cookie — so a
        caller cannot tell them apart, and nothing about the attempt is
        echoed back. The comparison is over digests and is done with
        :func:`hmac.compare_digest`.
        """
        if not token or not session_cookie:
            return None
        digest = _digest(token)
        fingerprint = _digest(session_cookie)
        now = self._clock()
        with self._lock:
            found = self._find_locked(digest)
            if found is None:
                return None
            key, record = found
            if now >= record.expires_at:
                del self._grants[key]
                return None
            if not hmac.compare_digest(record.session_fingerprint, fingerprint):
                return None
            return TerminalGrant(
                grant_id=record.grant_id,
                session_fingerprint=record.session_fingerprint,
                issued_at=record.issued_at,
                expires_at=record.expires_at,
            )

    def revoke(self, token: str) -> None:
        """Revoke the grant ``token`` names, and only that one.

        A logout presents the grant it is ending; other grants that happen
        to share a login cookie are other browsers' grants and stay live.
        """
        if not token:
            return
        with self._lock:
            self._revoke_locked(token)

    def revoke_all(self) -> None:
        """Revoke every live grant (a password change ends them all)."""
        with self._lock:
            self._grants.clear()

    def prune(self) -> None:
        """Drop the grants whose fixed deadline has passed."""
        with self._lock:
            self._prune_locked(self._clock())

    def _prune_locked(self, now: float) -> None:
        expired = [
            digest
            for digest, record in self._grants.items()
            if now >= record.expires_at
        ]
        for digest in expired:
            del self._grants[digest]

    def _revoke_locked(self, token: str) -> None:
        digest = _digest(token)
        found = self._find_locked(digest)
        if found is not None:
            del self._grants[found[0]]

    def _find_locked(self, digest: str) -> tuple[str, _StoredGrant] | None:
        """The live grant whose bearer digest is ``digest``.

        Compared over every live grant with :func:`hmac.compare_digest`
        rather than looked up by key, so the answer does not depend on where
        the digest sits in the table. The matched key comes back with the
        record, so a caller that removes the grant removes the entry it
        actually matched.
        """
        for candidate_digest, record in self._grants.items():
            if hmac.compare_digest(candidate_digest, digest):
                return candidate_digest, record
        return None

    # ── Password policy ────────────────────────────────────────────────────

    def check_password(self, client_ip: str, supplied: str, expected: str) -> bool:
        """Whether ``supplied`` is the terminal unlock password.

        ``True`` only for a correct password. A blank ``expected`` never
        authenticates: an engine with no password configured has no terminal
        unlock, and an empty guess is not one either.

        The failure budget is per client IP and rolling. Five failures
        inside :data:`TERMINAL_UNLOCK_FAIL_WINDOW_S` close the window for
        that IP, and a closed window refuses *every* attempt — including the
        correct password — until it clears. A success before the limit
        clears that IP's count, so one correct guess is not a way to reset
        a shared address's budget for an attacker who sits behind it.

        :raises TerminalAuthError: with reason ``rate_limited`` when the
            window is closed, or when the failure table is full and this
            client is not already tracked. A full table refuses new
            guessing rather than evicting a live bucket, because evicting
            one would hand the next guess a fresh budget. That refusal is
            fail-closed for the whole table, the correct password
            included: a full table is a locked door, not a queue.
        """
        now = self._clock()
        with self._lock:
            self._prune_failures_locked(now)
            failures = self._failures.get(client_ip)
            if failures is not None:
                if len(failures) >= TERMINAL_UNLOCK_FAIL_LIMIT:
                    raise TerminalAuthError("rate_limited")
            elif len(self._failures) >= TERMINAL_MAX_FAILURE_CLIENTS:
                # Every tracked client is inside its own window; a new one
                # may not start guessing, and no live bucket is dropped to
                # make room for it.
                raise TerminalAuthError("rate_limited")

            if expected and hmac.compare_digest(
                supplied.encode("utf-8"), expected.encode("utf-8")
            ):
                self._failures.pop(client_ip, None)
                return True

            self._failures.setdefault(client_ip, []).append(now)
            return False

    def _prune_failures_locked(self, now: float) -> None:
        """Forget the failures that fell out of the rolling window."""
        window_start = now - TERMINAL_UNLOCK_FAIL_WINDOW_S
        for client_ip in list(self._failures):
            recent = [
                stamp for stamp in self._failures[client_ip] if stamp > window_start
            ]
            if recent:
                self._failures[client_ip] = recent
            else:
                del self._failures[client_ip]
