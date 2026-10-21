"""Bounded terminal authorization grants (the #1682 foundation).

Pure unit tests over an injected clock: no HTTP, no cookies, no shell, no
files. Every test names the behavior it pins, and the module under test is
the only thing that decides grants — the routes that will use it come later.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import hashlib
import hmac
import inspect
import logging
from pathlib import Path
from typing import Any

import pytest

from ciao.terminal_auth import (
    TERMINAL_COOKIE,
    TERMINAL_MAX_FAILURE_CLIENTS,
    TERMINAL_MAX_GRANTS,
    TERMINAL_UNLOCK_FAIL_LIMIT,
    TERMINAL_UNLOCK_FAIL_WINDOW_S,
    TERMINAL_UNLOCK_TTL_S,
    TerminalAuthError,
    TerminalGrant,
    TerminalGrantStore,
)

#: A lone surrogate: what a hostile body can carry and what UTF-8 refuses.
#: It is written as an escape so the source file itself stays valid UTF-8.
_MALFORMED = "\ud800"


class FakeClock:
    """A monotonic clock the test moves by hand. No sleeps, no wall time."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _store(clock: FakeClock) -> TerminalGrantStore:
    return TerminalGrantStore(clock=clock)


def _b64_bytes(value: str) -> bytes:
    """The bytes behind a URL-safe base64 string, padding and all."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _reachable_strings(value: object) -> list[str]:
    """Every string reachable from an object's attributes."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (bytes, bytearray)):
        return [value.decode("utf-8", "replace")]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        found: list[str] = []
        for spec in dataclasses.fields(value):
            found.extend(_reachable_strings(getattr(value, spec.name, None)))
        return found
    if isinstance(value, dict):
        found = []
        for key, item in value.items():
            found.extend(_reachable_strings(key))
            found.extend(_reachable_strings(item))
        return found
    if isinstance(value, (list, tuple, set, frozenset)):
        found = []
        for item in value:
            found.extend(_reachable_strings(item))
        return found
    if hasattr(value, "__dict__"):
        found = []
        for item in vars(value).values():
            found.extend(_reachable_strings(item))
        return found
    return []


# ── Grants ─────────────────────────────────────────────────────────────────


def test_issue_and_verify_bind_the_grant_to_the_login_cookie() -> None:
    clock = FakeClock()
    store = _store(clock)

    token, grant = store.issue("cookie-a")
    other_token, _ = store.issue("cookie-b")

    verified = store.verify(token, "cookie-a")
    assert verified is not None
    assert verified.grant_id == grant.grant_id
    # The same bearer presented with a different login cookie is not a grant.
    assert store.verify(token, "cookie-b") is None
    # And a bearer that was never issued is not a grant either.
    assert store.verify(other_token, "cookie-a") is None


def test_identical_login_cookies_get_distinct_grants() -> None:
    """Two browsers that happen to hold the same login cookie are still two grants.

    The login cookie is not a device identity: two sign-ins inside one second
    can produce byte-identical cookies, so a grant has to carry its own
    random identity or one browser's logout would revoke the other's grant.
    """
    clock = FakeClock()
    store = _store(clock)

    first_token, first = store.issue("same-cookie")
    second_token, second = store.issue("same-cookie")

    assert first.grant_id != second.grant_id
    assert first_token != second_token
    # Revoking one leaves the other usable.
    store.revoke(first_token)
    assert store.verify(first_token, "same-cookie") is None
    assert store.verify(second_token, "same-cookie") is not None


def test_the_retained_state_holds_digests_and_not_the_bearer() -> None:
    clock = FakeClock()
    store = _store(clock)

    token, grant = store.issue("cookie-a")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256("cookie-a".encode("utf-8")).hexdigest()

    # The public record carries the login-cookie fingerprint, never the
    # cookie itself, and never the bearer.
    assert grant.session_fingerprint == fingerprint
    assert token not in repr(grant)
    assert "cookie-a" not in repr(grant)
    assert fingerprint not in repr(grant)

    # The store retains digests and safe metadata only: nothing it holds is
    # the raw bearer or the raw login cookie.
    blobs = _reachable_strings(store) + _reachable_strings(grant)
    assert token not in blobs
    assert "cookie-a" not in blobs
    assert digest in blobs
    assert fingerprint in blobs

    # And no repr of the store or its records carries the bearer either.
    assert token not in repr(store)
    assert "cookie-a" not in repr(store)


def test_the_bearer_is_256_bits_and_the_grant_id_is_128_bits() -> None:
    """The sizes are the contract, checked by decoding, not by statistics."""
    clock = FakeClock()
    store = _store(clock)

    token, grant = store.issue("cookie-a")

    assert len(_b64_bytes(token)) == 32
    assert len(_b64_bytes(grant.grant_id)) == 16
    # The grant id is not the bearer: it carries no authority on its own.
    assert grant.grant_id != token


def test_grant_expires_at_the_fixed_deadline() -> None:
    clock = FakeClock()
    store = _store(clock)

    token, grant = store.issue("cookie-a")
    assert grant.expires_at == grant.issued_at + TERMINAL_UNLOCK_TTL_S

    clock.advance(TERMINAL_UNLOCK_TTL_S - 1)
    assert store.verify(token, "cookie-a") is not None

    # Verifying must not slide the deadline: the grant still dies at the
    # instant it was minted to die.
    clock.advance(1)
    assert store.verify(token, "cookie-a") is None

    # And it stays dead afterwards.
    clock.advance(TERMINAL_UNLOCK_TTL_S)
    assert store.verify(token, "cookie-a") is None


def test_verify_returns_the_original_timestamps_and_binding() -> None:
    clock = FakeClock()
    store = _store(clock)

    token, grant = store.issue("cookie-a")
    verified = store.verify(token, "cookie-a")

    assert verified is not None
    assert verified.grant_id == grant.grant_id
    assert verified.issued_at == grant.issued_at
    assert verified.expires_at == grant.expires_at
    assert verified.session_fingerprint == grant.session_fingerprint


def test_revoke_affects_only_the_presented_grant() -> None:
    clock = FakeClock()
    store = _store(clock)

    first_token, _ = store.issue("cookie-a")
    second_token, _ = store.issue("cookie-a")

    store.revoke(first_token)

    assert store.verify(first_token, "cookie-a") is None
    assert store.verify(second_token, "cookie-a") is not None


def test_revoke_all_invalidates_every_grant() -> None:
    clock = FakeClock()
    store = _store(clock)

    first_token, _ = store.issue("cookie-a")
    second_token, _ = store.issue("cookie-b")

    store.revoke_all()

    assert store.verify(first_token, "cookie-a") is None
    assert store.verify(second_token, "cookie-b") is None


def test_issue_replaces_the_previous_token() -> None:
    """A re-unlock replaces the grant it renews, and does not leak capacity."""
    clock = FakeClock()
    store = _store(clock)

    first_token, _ = store.issue("cookie-a")
    second_token, second = store.issue("cookie-a", replace_token=first_token)

    assert store.verify(first_token, "cookie-a") is None
    assert store.verify(second_token, "cookie-a") is not None
    assert second.grant_id

    # The renew consumed no extra capacity: 31 more grants still fit, and
    # the 32nd live grant is the one that is refused.
    for index in range(TERMINAL_MAX_GRANTS - 1):
        store.issue(f"cookie-{index}")
    with pytest.raises(TerminalAuthError) as excinfo:
        store.issue("cookie-overflow")
    assert excinfo.value.reason == "capacity"


def test_a_renewal_at_capacity_replaces_the_grant_it_renews() -> None:
    """At the bound, renewing one of the live grants still works."""
    clock = FakeClock()
    store = _store(clock)

    tokens = [store.issue(f"cookie-{index}")[0] for index in range(TERMINAL_MAX_GRANTS)]
    assert store.retained_grant_count() == TERMINAL_MAX_GRANTS

    renewed_token, _ = store.issue("cookie-0", replace_token=tokens[0])

    # The renewal took the freed slot, so the count did not grow.
    assert store.retained_grant_count() == TERMINAL_MAX_GRANTS
    assert store.verify(tokens[0], "cookie-0") is None
    assert store.verify(renewed_token, "cookie-0") is not None


def test_a_foreign_replacement_does_not_revoke_or_free_a_slot() -> None:
    """A replacement bound to another session is not ours to revoke."""
    clock = FakeClock()
    store = _store(clock)

    victim_token, _ = store.issue("cookie-victim")
    for index in range(TERMINAL_MAX_GRANTS - 1):
        store.issue(f"cookie-{index}")
    assert store.retained_grant_count() == TERMINAL_MAX_GRANTS

    with pytest.raises(TerminalAuthError) as excinfo:
        store.issue("cookie-attacker", replace_token=victim_token)
    assert excinfo.value.reason == "capacity"

    # The victim is untouched and the foreign replacement freed no slot.
    assert store.retained_grant_count() == TERMINAL_MAX_GRANTS
    assert store.verify(victim_token, "cookie-victim") is not None


def test_an_unknown_or_expired_replacement_token_is_a_no_op() -> None:
    clock = FakeClock()
    store = _store(clock)

    existing_token, _ = store.issue("cookie-a")

    # An unknown replacement is not an error and revokes nothing.
    store.issue("cookie-a", replace_token="never-issued")
    assert store.verify(existing_token, "cookie-a") is not None

    # An expired replacement is a no-op too: there is nothing left to revoke.
    clock.advance(TERMINAL_UNLOCK_TTL_S + 1)
    store.issue("cookie-a", replace_token=existing_token)


def test_grant_capacity_is_bounded_and_expired_entries_are_reclaimed() -> None:
    clock = FakeClock()
    store = _store(clock)

    for index in range(TERMINAL_MAX_GRANTS):
        store.issue(f"cookie-{index}")

    with pytest.raises(TerminalAuthError) as excinfo:
        store.issue("cookie-overflow")
    assert excinfo.value.reason == "capacity"

    # An expired grant frees its slot: the store prunes before it counts.
    clock.advance(TERMINAL_UNLOCK_TTL_S + 1)
    token, _ = store.issue("cookie-fresh")
    assert store.verify(token, "cookie-fresh") is not None


def test_prune_removes_only_the_expired_grants() -> None:
    """A prune removes the records whose deadline has passed, and no other.

    The retained count is read before any ``verify``, because ``verify``
    prunes too: a no-op prune would still look right through ``verify``.
    """
    clock = FakeClock()
    store = _store(clock)

    live_token, _ = store.issue("cookie-live")
    clock.advance(TERMINAL_UNLOCK_TTL_S // 2)
    dying_token, _ = store.issue("cookie-dying")
    assert store.retained_grant_count() == 2

    # The first grant is exactly at its deadline; the second is not.
    clock.advance(TERMINAL_UNLOCK_TTL_S // 2)
    store.prune()

    assert store.retained_grant_count() == 1
    assert store.verify(live_token, "cookie-live") is None
    assert store.verify(dying_token, "cookie-dying") is not None


def test_blank_session_cannot_receive_a_grant() -> None:
    clock = FakeClock()
    store = _store(clock)

    for blank in ("", "   "):
        with pytest.raises(TerminalAuthError) as excinfo:
            store.issue(blank)
        assert excinfo.value.reason == "invalid_session"


def test_blank_verify_and_revoke_edges() -> None:
    clock = FakeClock()
    store = _store(clock)

    token, _ = store.issue("cookie-a")

    assert store.verify("", "cookie-a") is None
    assert store.verify(token, "") is None
    assert store.verify("   ", "cookie-a") is None
    assert store.verify(token, "   ") is None

    # A blank revocation revokes nothing and raises nothing.
    store.revoke("")
    store.revoke("   ")
    assert store.verify(token, "cookie-a") is not None


def test_malformed_credential_unicode_is_refused_not_raised() -> None:
    """A credential that is not valid UTF-8 is a value, never an exception."""
    clock = FakeClock()
    store = _store(clock)

    token, _ = store.issue("cookie-a")

    # A session that is not valid UTF-8 is refused, not raised on.
    with pytest.raises(TerminalAuthError) as excinfo:
        store.issue(_MALFORMED)
    assert excinfo.value.reason == "invalid_session"

    # A malformed bearer or cookie is simply not a grant.
    assert store.verify(_MALFORMED, "cookie-a") is None
    assert store.verify(token, _MALFORMED) is None

    # A malformed bearer revokes nothing.
    store.revoke(_MALFORMED)
    assert store.verify(token, "cookie-a") is not None

    # A malformed replacement token is a no-op, not an error.
    store.issue("cookie-a", replace_token=_MALFORMED)
    assert store.verify(token, "cookie-a") is not None


# ── Password policy ───────────────────────────────────────────────────────


def test_password_comparison_handles_unicode_and_refuses_empty_expected() -> None:
    clock = FakeClock()
    store = _store(clock)

    assert store.check_password("10.0.0.1", "pässwörd☃", "pässwörd☃") is True
    assert store.check_password("10.0.0.2", "pässwörd☃", "pässwörd") is False
    # An unset password never authenticates, not even an empty guess.
    assert store.check_password("10.0.0.3", "", "") is False
    assert store.check_password("10.0.0.4", "anything", "") is False

    # A blank expected password still counts as a failed attempt.
    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        assert store.check_password("10.0.0.5", "anything", "") is False
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.5", "right", "")
    assert excinfo.value.reason == "rate_limited"


def test_a_malformed_password_is_a_failure_and_reaches_the_limit() -> None:
    """A password that is not valid UTF-8 fails, and it counts."""
    clock = FakeClock()
    store = _store(clock)

    # Matching malformed values must not authorize through surrogate encoding.
    assert store.check_password("10.0.0.3", _MALFORMED, _MALFORMED) is False

    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        assert store.check_password("10.0.0.1", _MALFORMED, "right") is False
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.1", "right", "right")
    assert excinfo.value.reason == "rate_limited"

    # The same holds for a malformed expected password.
    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        assert store.check_password("10.0.0.2", "right", _MALFORMED) is False
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.2", "right", "right")
    assert excinfo.value.reason == "rate_limited"


def test_a_whitespace_only_expected_password_never_authenticates() -> None:
    clock = FakeClock()
    store = _store(clock)

    for blank in ("", "   ", "\t\n"):
        assert store.check_password("10.0.0.1", "anything", blank) is False
        assert store.check_password("10.0.0.2", blank, blank) is False


def test_a_password_with_significant_spaces_is_compared_byte_for_byte() -> None:
    """Neither operand is stripped: the spaces are part of the password."""
    clock = FakeClock()
    store = _store(clock)

    assert store.check_password("10.0.0.1", " pass ", " pass ") is True
    assert store.check_password("10.0.0.2", "pass", " pass ") is False
    assert store.check_password("10.0.0.3", " pass", " pass ") is False
    assert store.check_password("10.0.0.4", "pass ", " pass") is False


def test_an_empty_supplied_password_fails_against_a_nonempty_expected() -> None:
    clock = FakeClock()
    store = _store(clock)

    assert store.check_password("10.0.0.1", "", "right") is False
    # And it counts as an attempt like any other.
    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT - 1):
        assert store.check_password("10.0.0.1", "", "right") is False
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.1", "right", "right")
    assert excinfo.value.reason == "rate_limited"


def test_five_failures_block_attempts_including_correct_password_until_window_expires() -> None:
    clock = FakeClock()
    store = _store(clock)

    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT - 1):
        assert store.check_password("10.0.0.1", "wrong", "right") is False

    # The fifth failure is the last one that is answered.
    assert store.check_password("10.0.0.1", "wrong", "right") is False

    # The window is closed: even the correct password is refused, and the
    # refusal is a typed one rather than a False.
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.1", "right", "right")
    assert excinfo.value.reason == "rate_limited"

    clock.advance(TERMINAL_UNLOCK_FAIL_WINDOW_S + 1)
    assert store.check_password("10.0.0.1", "right", "right") is True


def test_the_rolling_window_closes_at_its_exact_boundary() -> None:
    clock = FakeClock()
    store = _store(clock)

    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        assert store.check_password("10.0.0.1", "wrong", "right") is False

    # One second before the window closes the failures still count.
    clock.advance(TERMINAL_UNLOCK_FAIL_WINDOW_S - 1)
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.1", "right", "right")
    assert excinfo.value.reason == "rate_limited"

    # At the boundary the oldest failure has aged out and the window is open.
    clock.advance(1)
    assert store.check_password("10.0.0.1", "right", "right") is True


def test_the_rolling_window_expires_failures_one_by_one() -> None:
    """Failures age out individually, so a staggered run frees the window."""
    clock = FakeClock()
    store = _store(clock)

    for _ in range(3):
        assert store.check_password("10.0.0.1", "wrong", "right") is False
    clock.advance(100)
    for _ in range(2):
        assert store.check_password("10.0.0.1", "wrong", "right") is False

    # Five failures: the window is closed.
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.1", "right", "right")
    assert excinfo.value.reason == "rate_limited"

    # The three old failures age out; the two recent ones stay, so the count
    # is exactly two. Three more failures therefore fit, and the fourth
    # attempt is the one that closes the window again.
    clock.advance(801)
    for _ in range(3):
        assert store.check_password("10.0.0.1", "wrong", "right") is False
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.1", "right", "right")
    assert excinfo.value.reason == "rate_limited"


def test_success_before_the_failure_limit_resets_that_clients_count() -> None:
    clock = FakeClock()
    store = _store(clock)

    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT - 1):
        assert store.check_password("10.0.0.1", "wrong", "right") is False
    assert store.check_password("10.0.0.1", "right", "right") is True

    # The budget is spent again from zero, not continued from where it was.
    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT - 1):
        assert store.check_password("10.0.0.1", "wrong", "right") is False
    assert store.check_password("10.0.0.1", "right", "right") is True


def test_failure_buckets_are_bounded_without_evicting_live_limits() -> None:
    clock = FakeClock()
    store = _store(clock)

    for index in range(TERMINAL_MAX_FAILURE_CLIENTS):
        assert store.check_password(f"10.0.0.{index}", "wrong", "right") is False

    # The bucket table is full: a new client is refused rather than allowed
    # to start guessing.
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.9.9.9", "wrong", "right")
    assert excinfo.value.reason == "rate_limited"

    # Fail-closed at the bound: a correct password is refused too while the
    # table is full, so a correct guess is never a way past a full table.
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.9.9.9", "right", "right")
    assert excinfo.value.reason == "rate_limited"

    # No live bucket was evicted to make room for it, so the first client
    # still remembers its earlier failure and reaches the limit on its own.
    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT - 1):
        assert store.check_password("10.0.0.0", "wrong", "right") is False
    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.0", "right", "right")
    assert excinfo.value.reason == "rate_limited"

    # A stale bucket is reclaimed once its window has passed.
    clock.advance(TERMINAL_UNLOCK_FAIL_WINDOW_S + 1)
    assert store.check_password("10.9.9.9", "right", "right") is True


# ── The client identity the failures are counted against ──────────────────


def test_two_spellings_of_one_address_share_a_failure_bucket() -> None:
    """The bucket is the address, not the string the caller happened to pass."""
    clock = FakeClock()
    store = _store(clock)

    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        assert store.check_password("2001:0db8:0000:0000:0000:0000:0000:0001", "wrong", "right") is False

    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("2001:db8::1", "right", "right")
    assert excinfo.value.reason == "rate_limited"


def test_an_ipv4_mapped_address_shares_the_ipv4_bucket() -> None:
    clock = FakeClock()
    store = _store(clock)

    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        assert store.check_password("::ffff:10.0.0.1", "wrong", "right") is False

    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("10.0.0.1", "right", "right")
    assert excinfo.value.reason == "rate_limited"


def test_the_ipv4_and_ipv6_loopbacks_are_separate_buckets() -> None:
    """They are two different addresses, so they are two different budgets."""
    clock = FakeClock()
    store = _store(clock)

    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        assert store.check_password("127.0.0.1", "wrong", "right") is False

    assert store.check_password("::1", "right", "right") is True


def test_unusable_addresses_share_one_unknown_client_bucket() -> None:
    """An address the server cannot parse is one bucket, not unbounded keys."""
    clock = FakeClock()
    store = _store(clock)

    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        assert store.check_password("not-an-ip", "wrong", "right") is False

    with pytest.raises(TerminalAuthError) as excinfo:
        store.check_password("another-garbage", "right", "right")
    assert excinfo.value.reason == "rate_limited"


# ── The comparisons are constant-time and the digests are compared ─────────


def test_the_password_comparison_goes_through_compare_digest(monkeypatch) -> None:
    """The password bytes are compared with ``hmac.compare_digest``."""
    from ciao import terminal_auth

    real = hmac.compare_digest
    calls: list[tuple[Any, Any]] = []

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(terminal_auth.hmac, "compare_digest", spy)
    clock = FakeClock()
    store = _store(clock)

    assert store.check_password("10.0.0.1", "secret", "secret") is True

    # Exactly one comparison: the password's bytes. The bearer lookup is a
    # keyed digest lookup, not a scan over the stored digests.
    assert calls == [(b"secret", b"secret")]


def test_the_session_binding_comparison_goes_through_compare_digest(
    monkeypatch,
) -> None:
    from ciao import terminal_auth

    real = hmac.compare_digest
    calls: list[tuple[Any, Any]] = []

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(terminal_auth.hmac, "compare_digest", spy)
    clock = FakeClock()
    store = _store(clock)

    token, _ = store.issue("cookie-a")
    calls.clear()
    assert store.verify(token, "cookie-a") is not None

    # The session binding is compared over its digests, in constant time.
    assert len(calls) == 1
    left, right = calls[0]
    assert isinstance(left, str) and isinstance(right, str)
    assert left == hashlib.sha256("cookie-a".encode("utf-8")).hexdigest()


# ── Nothing leaves the process ────────────────────────────────────────────


def test_no_terminal_auth_data_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    store = _store(clock)

    with caplog.at_level(logging.DEBUG):
        token, _ = store.issue("cookie-a")
        store.check_password("10.0.0.1", "supplied-secret", "expected-secret")
        store.revoke(token)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in logged
    assert "supplied-secret" not in logged
    assert "expected-secret" not in logged
    assert "cookie-a" not in logged


def test_the_module_depends_on_no_filesystem_or_logging_module() -> None:
    """What this checks, exactly: this module's own imports contain nothing
    that opens files, writes logs, spawns a process or talks to the network,
    and it calls no builtin that opens a file or starts a logger. It is a
    statement about this module's dependency list and its own call sites,
    not a proof that no I/O is possible.
    """
    from ciao import terminal_auth

    path = Path(inspect.getfile(terminal_auth))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    forbidden_modules = {
        "os",
        "pathlib",
        "io",
        "logging",
        "tempfile",
        "shutil",
        "glob",
        "subprocess",
        "socket",
        "urllib",
        "http",
        "requests",
        "sqlite3",
        "pickle",
        "shelve",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not (imported & forbidden_modules), (
        "ciao/terminal_auth.py must stay in memory: it imports "
        f"{sorted(imported & forbidden_modules)}"
    )

    forbidden_calls = {
        "open",
        "write_text",
        "write_bytes",
        "mkdir",
        "makedirs",
        "replace",
        "rename",
        "unlink",
        "remove",
        "rmdir",
        "touch",
        "mkstemp",
        "mkdtemp",
        "NamedTemporaryFile",
        "TemporaryFile",
        "getLogger",
        "basicConfig",
    }
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    assert not (called & forbidden_calls), (
        "ciao/terminal_auth.py must stay in memory and silent: it calls "
        f"{sorted(called & forbidden_calls)}"
    )


# ── The constants the contract fixes ───────────────────────────────────────


def test_the_terminal_grant_constants_match_the_contract() -> None:
    assert TERMINAL_COOKIE == "ciao_terminal"
    assert TERMINAL_UNLOCK_TTL_S == 900
    assert TERMINAL_MAX_GRANTS == 32
    assert TERMINAL_UNLOCK_FAIL_LIMIT == 5
    assert TERMINAL_UNLOCK_FAIL_WINDOW_S == 900
    assert TERMINAL_MAX_FAILURE_CLIENTS == 256


def test_a_grant_is_a_frozen_public_record() -> None:
    clock = FakeClock()
    store = _store(clock)

    _, grant = store.issue("cookie-a")
    assert isinstance(grant, TerminalGrant)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(grant, "grant_id", "other")
