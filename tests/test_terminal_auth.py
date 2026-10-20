"""Bounded terminal authorization grants (the #1682 foundation).

Pure unit tests over an injected clock: no HTTP, no cookies, no shell, no
files. Every test names the behavior it pins, and the module under test is
the only thing that decides grants — the routes that will use it come later.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import logging
from pathlib import Path

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


def test_only_hashes_are_retained_and_repr_does_not_expose_bearer() -> None:
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

    # The store keeps digests only: nothing it holds is the raw bearer or
    # the raw login cookie.
    blobs = _reachable_strings(store) + _reachable_strings(grant)
    assert token not in blobs
    assert "cookie-a" not in blobs
    assert digest in blobs
    assert fingerprint in blobs

    # And no repr of the store or its records carries the bearer either.
    assert token not in repr(store)
    assert "cookie-a" not in repr(store)


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


def test_blank_session_cannot_receive_a_grant() -> None:
    clock = FakeClock()
    store = _store(clock)

    for blank in ("", "   "):
        with pytest.raises(TerminalAuthError) as excinfo:
            store.issue(blank)
        assert excinfo.value.reason == "invalid_session"


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


# ── Nothing leaves the process ────────────────────────────────────────────


def test_no_terminal_auth_data_is_written_or_logged(
    tmp_path: Path,
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

    # Nothing under the test's tree carries the bearer or either password.
    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        assert token.encode("utf-8") not in blob, path
        assert b"supplied-secret" not in blob, path
        assert b"expected-secret" not in blob, path
        assert b"cookie-a" not in blob, path


def test_the_module_has_no_file_writing_code_path() -> None:
    """The store is in memory only: it cannot open a file, so it writes none."""
    from ciao import terminal_auth

    path = Path(inspect.getfile(terminal_auth))
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    forbidden: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name in {
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
        }:
            forbidden.add(f"{name} (line {node.lineno})")

    assert not forbidden, (
        "ciao/terminal_auth.py must stay in memory: it writes no file, so it "
        f"must not call {sorted(forbidden)}"
    )


def test_the_module_cannot_log() -> None:
    """No logger, no log call: there is no path from a grant to the log."""
    from ciao import terminal_auth

    path = Path(inspect.getfile(terminal_auth))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "logging" not in imported
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"debug", "info", "warning", "error", "exception"}
        for node in ast.walk(tree)
    ), "ciao/terminal_auth.py must not log a grant, a token or a password"


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
        grant.grant_id = "other"  # type: ignore[misc]
