"""The private webhook trigger store: schema, secrets, and what it refuses (#981).

Everything here runs against a `tmp_path` store with an injected clock, so no
test touches an operator's runtime directory, reads a real credential, or
depends on wall-clock time. There is no live path to touch yet: this child adds
no route, no receiver and no startup wiring.
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from ciao import webhooks
from ciao.os_support.private import is_private


class _Clock:
    """A UTC clock a test moves by hand, so a record's bytes are reproducible."""

    def __init__(self, start: datetime | None = None) -> None:
        self.moment = start or datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: int) -> None:
        self.moment += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    # A runtime-shaped path whose parent does not exist yet: the store must be
    # able to create it, and a read must not.
    return tmp_path / "runtime" / "webhooks.json"


def _store(path: Path, clock: Callable[[], datetime] | None = None) -> webhooks.WebhookStore:
    return webhooks.WebhookStore(path, clock or _Clock())


def _enable(
    store: webhooks.WebhookStore, trigger: webhooks.WebhookTrigger
) -> webhooks.WebhookTrigger:
    return store.update(trigger.trigger_id, expected_revision=trigger.revision, enabled=True)


def _enabled_trigger(
    store: webhooks.WebhookStore, *, name: str = "CI", workspace: str = "personal"
) -> tuple[webhooks.WebhookTrigger, str]:
    trigger, secret = store.create(
        name=name, workspace=workspace, instructions="File the intake note"
    )
    return _enable(store, trigger), secret


def _document(path: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


def _temp_residue(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.name.endswith(".tmp"))


def _refused(call: Callable[[], Any]) -> webhooks.WebhookStoreError:
    with pytest.raises(webhooks.WebhookStoreError) as raised:
        call()
    return raised.value


# ── Reading an unwritten store ─────────────────────────────────────────────


def test_missing_store_is_empty_without_writing(path: Path, clock: _Clock) -> None:
    """A store that was never written reads as empty and leaves no file."""
    store = _store(path, clock)

    assert store.list("personal") == []
    assert _refused(lambda: store.get("0" * 32)).code == webhooks.NOT_FOUND
    assert store.authenticate("0" * 32, "anything") is None
    assert not path.exists(), "a read must not create the store"
    assert not path.parent.exists(), "a read must not create the runtime directory"

    # ...and the first write creates the directory and the file together.
    store.create(name="first", workspace="personal", instructions="go")
    assert path.is_file()


# ── A secret is returned once and never stored ─────────────────────────────


def test_create_returns_secret_once_and_persists_only_verifier(
    path: Path, clock: _Clock
) -> None:
    """Only the SHA-256 of the secret reaches the file, a record or a repr."""
    store = _store(path, clock)

    trigger, secret = store.create(
        name="CI push", workspace="personal", instructions="Run the intake"
    )
    raw = path.read_text(encoding="utf-8")

    assert secret and secret not in raw, "the raw secret is never written"
    entry = _document(path)["triggers"][trigger.trigger_id]
    assert set(entry) == {"trigger", "secret_sha256"}
    assert entry["secret_sha256"] == hashlib.sha256(secret.encode("utf-8")).hexdigest()

    # Nothing a caller can print or return carries it.
    assert "secret" not in trigger.to_dict()
    assert secret not in repr(trigger)
    assert secret not in repr(trigger.to_dict())
    internal = webhooks._StoredTrigger(trigger=trigger, secret_sha256=entry["secret_sha256"])
    assert entry["secret_sha256"] not in repr(internal)

    # Nor does a later read of the same record, from this store or another.
    assert store.get(trigger.trigger_id).to_dict() == trigger.to_dict()
    other = _store(path, _Clock())
    assert [row.trigger_id for row in other.list("personal")] == [trigger.trigger_id]
    assert other.get(trigger.trigger_id) == trigger
    assert secret not in path.read_text(encoding="utf-8")

    # Nor an error: a conflict message names the revision and nothing else.
    stale = _refused(
        lambda: store.update(
            trigger.trigger_id, expected_revision=trigger.revision + 5, name="x"
        )
    )
    assert stale.code == webhooks.REVISION_CONFLICT
    assert secret not in str(stale)
    assert secret not in repr(stale)


# ── Disabled by default, and scoped to one trigger ──────────────────────────


def test_trigger_starts_disabled_and_authentication_is_scoped(
    path: Path, clock: _Clock
) -> None:
    """A secret authorizes its own trigger only, and only while it is enabled."""
    store = _store(path, clock)
    trigger, secret = store.create(
        name="CI push", workspace="personal", instructions="Run the intake"
    )
    other_trigger, other_secret = store.create(
        name="Billing", workspace="work", instructions="Run billing"
    )

    assert trigger.enabled is False, "a new trigger is never born callable"
    assert store.authenticate(trigger.trigger_id, secret) is None

    enabled = _enable(store, trigger)
    assert enabled.enabled is True
    assert store.authenticate(trigger.trigger_id, secret) == enabled

    # Scoped: not to another trigger, not to another workspace's trigger, and
    # not to any password or cookie this app might have.
    assert store.authenticate(other_trigger.trigger_id, secret) is None
    assert store.authenticate(trigger.trigger_id, other_secret) is None
    assert store.authenticate("f" * 32, secret) is None
    for wrong in ("", secret + "x", secret + "\n", " " + secret, "a" * 4096):
        assert store.authenticate(trigger.trigger_id, wrong) is None

    # There is no login or session in this contract at all: a check is exactly
    # a trigger id and that trigger's own secret, and what it returns is a
    # public record rather than a principal.
    assert list(inspect.signature(webhooks.WebhookStore.authenticate).parameters) == [
        "self",
        "trigger_id",
        "secret",
    ]
    assert store.authenticate(trigger.trigger_id, secret).to_dict() == enabled.to_dict()


# ── Rotation ───────────────────────────────────────────────────────────────


def test_rotate_invalidates_old_secret_and_keeps_enabled_state(
    path: Path, clock: _Clock
) -> None:
    """The previous secret stops working the moment the rotation lands."""
    store = _store(path, clock)
    trigger, secret = _enabled_trigger(store)

    clock.advance(60)
    rotated, new_secret = store.rotate_secret(
        trigger.trigger_id, expected_revision=trigger.revision
    )

    assert rotated.revision == trigger.revision + 1
    assert rotated.enabled is True, "rotating a secret is not enabling a trigger"
    assert new_secret != secret
    assert rotated.updated_at != trigger.updated_at

    # A brand-new store object over the same file: the old secret is gone for
    # good, not just for the object that rotated it.
    reopened = _store(path, _Clock())
    assert reopened.authenticate(trigger.trigger_id, secret) is None
    assert reopened.authenticate(trigger.trigger_id, new_secret) == rotated
    assert secret not in path.read_text(encoding="utf-8")

    # Rotation never enables a disabled trigger.
    pending, pending_secret = reopened.create(
        name="later", workspace="personal", instructions="go"
    )
    reopened.rotate_secret(pending.trigger_id, expected_revision=pending.revision)
    assert reopened.get(pending.trigger_id).enabled is False
    assert reopened.authenticate(pending.trigger_id, pending_secret) is None


# ── Revocation ─────────────────────────────────────────────────────────────


def test_revoke_workspace_is_idempotent_and_requires_new_secret(
    path: Path, clock: _Clock
) -> None:
    """Revocation destroys the verifier, so re-enabling cannot restore it."""
    store = _store(path, clock)
    trigger, secret = _enabled_trigger(store, name="CI push")
    bystander, bystander_secret = _enabled_trigger(
        store, name="Bystander", workspace="work"
    )

    assert store.revoke_workspace("personal") == 1
    assert store.revoke_workspace("personal") == 0, "revocation is idempotent"

    revoked = store.get(trigger.trigger_id)
    assert revoked.enabled is False
    assert revoked.revision > trigger.revision
    assert _document(path)["triggers"][trigger.trigger_id]["secret_sha256"] == "", (
        "the verifier is destroyed, not merely disabled"
    )
    assert secret not in path.read_text(encoding="utf-8")

    # An unrelated workspace keeps its trigger and its working credential.
    assert store.get(bystander.trigger_id) == bystander
    assert store.authenticate(bystander.trigger_id, bystander_secret) == bystander

    # Toggling `enabled` back on is not an authorization.
    refused = _refused(
        lambda: store.update(
            trigger.trigger_id, expected_revision=revoked.revision, enabled=True
        )
    )
    assert refused.code == webhooks.INVALID_TRIGGER
    assert store.get(trigger.trigger_id).enabled is False
    assert store.authenticate(trigger.trigger_id, secret) is None

    # Configuring a revoked trigger stays allowed; enabling it needs a new secret.
    renamed = store.update(
        trigger.trigger_id, expected_revision=revoked.revision, name="CI push (rotating)"
    )
    assert renamed.name == "CI push (rotating)"
    assert store.get(trigger.trigger_id).enabled is False

    rotated, fresh = store.rotate_secret(
        trigger.trigger_id, expected_revision=renamed.revision
    )
    re_enabled = _enable(store, rotated)
    assert re_enabled.enabled is True
    assert store.authenticate(trigger.trigger_id, secret) is None, (
        "the pre-revocation secret must stay invalid"
    )
    assert store.authenticate(trigger.trigger_id, fresh) == re_enabled


# ── Optimistic concurrency ─────────────────────────────────────────────────


def test_stale_revision_does_not_overwrite_metadata(path: Path, clock: _Clock) -> None:
    """A refused write leaves the bytes, and the credential, exactly as they were."""
    store = _store(path, clock)
    trigger, secret = _enabled_trigger(store)
    before = path.read_bytes()

    stale_update = _refused(
        lambda: store.update(
            trigger.trigger_id,
            expected_revision=trigger.revision + 1,
            name="renamed by a stale caller",
        )
    )
    assert stale_update.code == webhooks.REVISION_CONFLICT

    clock.advance(60)
    stale_rotate = _refused(
        lambda: store.rotate_secret(
            trigger.trigger_id, expected_revision=trigger.revision - 1
        )
    )
    assert stale_rotate.code == webhooks.REVISION_CONFLICT

    assert path.read_bytes() == before, "a refused write must not touch the bytes"
    stored = store.get(trigger.trigger_id)
    assert stored.name == trigger.name
    assert stored.revision == trigger.revision
    assert stored.updated_at == trigger.updated_at
    assert store.authenticate(trigger.trigger_id, secret) == stored

    # ...and the same revision still works for the caller that read it.
    rotated, fresh = store.rotate_secret(
        trigger.trigger_id, expected_revision=trigger.revision
    )
    assert rotated.revision == trigger.revision + 1
    assert store.authenticate(trigger.trigger_id, secret) is None
    assert store.authenticate(trigger.trigger_id, fresh) == rotated


# ── Two store objects over one file ────────────────────────────────────────


def test_independent_store_instances_preserve_both_creates(
    path: Path, clock: _Clock
) -> None:
    """Neither store object holds a document, so neither create loses the other."""
    first = _store(path, clock)
    second = _store(path, _Clock(datetime(2026, 10, 3, 12, 0, 1, tzinfo=UTC)))

    older, _ = first.create(name="older", workspace="personal", instructions="go")
    newer, _ = second.create(name="newer", workspace="personal", instructions="go")

    rows = [row.trigger_id for row in first.list("personal")]
    assert set(rows) == {older.trigger_id, newer.trigger_id}
    # Ordered by (created_at, trigger_id): deterministic, and not dict order.
    assert rows == [older.trigger_id, newer.trigger_id]
    assert first.get(newer.trigger_id).name == "newer"
    assert second.get(older.trigger_id).name == "older"

    # An object created before either write still sees both when it writes.
    stale_view = _store(path, _Clock())
    third, _ = stale_view.create(name="third", workspace="work", instructions="go")
    assert len(_store(path).list("personal")) == 2
    assert _store(path).get(third.trigger_id).name == "third"


# ── Validation ─────────────────────────────────────────────────────────────


def test_validation_rejects_invalid_schema_fields(path: Path, clock: _Clock) -> None:
    """Nothing is coerced or repaired: bad input and bad stored fields both fail loud."""
    store = _store(path, clock)

    # 1. Caller input. `mode="bypass"` is a real BridgeMode (`ciao.models`) and
    #    deliberately not a webhook mode: an unattended turn is not a decision a
    #    remote sender may make.
    for field, value in (
        ("name", ""),
        ("name", "   "),
        ("name", "x" * (webhooks.MAX_NAME_LENGTH + 1)),
        ("name", 7),
        ("name", None),
        ("workspace", ""),
        ("workspace", "../elsewhere"),
        ("workspace", "personal/other"),
        # A trailing newline is a different name, not the same one with a
        # newline: accepted, it would be filed under a key `revoke_workspace`
        # can never match.
        ("workspace", "personal\n"),
        ("workspace", 3),
        ("project_id", ""),
        ("project_id", "proj/../secret"),
        ("project_id", "proj-1\n"),
        ("project_id", 12),
        ("instructions", "x" * (webhooks.MAX_INSTRUCTIONS_LENGTH + 1)),
        ("instructions", ["do", "it"]),
        ("mode", "bypass"),
        ("mode", "AUTO"),
        ("mode", True),
    ):
        fields: dict[str, Any] = {
            "name": "CI push",
            "workspace": "personal",
            "instructions": "go",
        }
        fields[field] = value
        refused = _refused(lambda fields=fields: store.create(**fields))
        assert refused.code == webhooks.INVALID_TRIGGER, field
    assert not path.exists(), "a refused create must not leave a file"

    # 2. Update arguments: a revision that is not a revision (`True` is an int),
    #    a state that is not a boolean, a workspace that is not a name.
    trigger, _ = store.create(name="CI push", workspace="personal", instructions="go")
    before = path.read_bytes()
    for revision in (0, -1, "1", 1.0, True, None):
        refused = _refused(
            lambda revision=revision: store.update(  # type: ignore[arg-type]
                trigger.trigger_id, expected_revision=revision, name="x"
            )
        )
        assert refused.code == webhooks.INVALID_TRIGGER
    for enabled in (1, "true", "yes"):
        refused = _refused(
            lambda enabled=enabled: store.update(  # type: ignore[arg-type]
                trigger.trigger_id,
                expected_revision=trigger.revision,
                enabled=enabled,
            )
        )
        assert refused.code == webhooks.INVALID_TRIGGER
    for workspace in ("", "../elsewhere"):
        assert (
            _refused(  # type: ignore[arg-type]
                lambda workspace=workspace: store.list(workspace)
            ).code
            == webhooks.INVALID_TRIGGER
        )
    assert path.read_bytes() == before

    # 3. A stored field this store could not have written is corruption, not a
    #    value: including an unsupported input policy, and an unknown key, which
    #    a newer engine's field must never be dropped by an older one.
    good = _document(path)
    for field, value in (
        ("enabled", "true"),
        ("enabled", 1),
        ("mode", "bypass"),
        ("input_policy", "caller_prompt"),
        ("workspace", ""),
        ("name", ""),
        ("project_id", "proj/../x"),
        ("workspace", "personal\n"),
        ("project_id", "proj-1\n"),
        ("instructions", "x" * (webhooks.MAX_INSTRUCTIONS_LENGTH + 1)),
        ("revision", 0),
        ("revision", True),
        ("created_at", "not-a-timestamp"),
        ("updated_at", "2026-10-03T12:00:00"),  # no UTC offset
        ("revoked_at", "2026-10-04T00:00:00+00:00"),  # a field this code has no rules for
    ):
        document = json.loads(json.dumps(good))
        document["triggers"][trigger.trigger_id]["trigger"][field] = value
        path.write_text(json.dumps(document), encoding="utf-8", newline="")
        assert _refused(lambda: store.list("personal")).code == webhooks.CORRUPT_STORE, field


# ── Corruption is never repaired ───────────────────────────────────────────


def test_corrupt_or_unreadable_existing_store_is_not_reset(
    path: Path, clock: _Clock
) -> None:
    """Every way the file can be unusable raises, and leaves its own bytes alone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stored_trigger = {
        "trigger_id": "a" * 32,
        "name": "CI",
        "workspace": "personal",
        "project_id": None,
        "instructions": "go",
        "enabled": False,
        "mode": "auto",
        "input_policy": "event_text",
        "created_at": "2026-10-03T12:00:00+00:00",
        "updated_at": "2026-10-03T12:00:00+00:00",
        "revision": 1,
    }
    good = {
        "schema": 1,
        "triggers": {
            "a" * 32: {"trigger": stored_trigger, "secret_sha256": "b" * 64}
        },
    }

    broken: list[tuple[str, str]] = [
        ("{not json at all", webhooks.CORRUPT_STORE),
        ("", webhooks.CORRUPT_STORE),
        ('"a string"', webhooks.CORRUPT_STORE),
        ('{"schema": 2, "triggers": {}}', webhooks.UNSUPPORTED_SCHEMA),
        ('{"schema": true, "triggers": {}}', webhooks.UNSUPPORTED_SCHEMA),
        ('{"triggers": {}}', webhooks.UNSUPPORTED_SCHEMA),
        ('{"schema": 1}', webhooks.CORRUPT_STORE),
        ('{"schema": 1, "triggers": []}', webhooks.CORRUPT_STORE),
        (
            json.dumps({"schema": 1, "triggers": {"not-a-minted-id": {}}}),
            webhooks.CORRUPT_STORE,
        ),
        (
            json.dumps({"schema": 1, "triggers": {"c" * 32: {}}}),
            webhooks.CORRUPT_STORE,
        ),
        (
            json.dumps(
                {
                    "schema": 1,
                    "triggers": {
                        "d" * 32: {"trigger": {}, "secret_sha256": "e" * 64}
                    },
                }
            ),
            webhooks.CORRUPT_STORE,
        ),
        (
            json.dumps(
                {
                    "schema": 1,
                    "triggers": {
                        # An id the record itself disagrees with.
                        "f" * 32: {
                            "trigger": stored_trigger,
                            "secret_sha256": "e" * 64,
                        }
                    },
                }
            ),
            webhooks.CORRUPT_STORE,
        ),
        (
            json.dumps(
                {
                    "schema": 1,
                    "triggers": {
                        "a" * 32: {"trigger": stored_trigger, "secret_sha256": "zz"}
                    },
                }
            ),
            webhooks.CORRUPT_STORE,
        ),
        # A digest that is correct except for a trailing newline. Read as a
        # prefix it would verify a secret that was never issued.
        (
            json.dumps(
                {
                    "schema": 1,
                    "triggers": {
                        "a" * 32: {
                            "trigger": stored_trigger,
                            "secret_sha256": "b" * 64 + "\n",
                        }
                    },
                }
            ),
            webhooks.CORRUPT_STORE,
        ),
        # An id this store did not mint, with the record agreeing with it: the
        # trailing newline is the whole defect, not a key/record disagreement.
        (
            json.dumps(
                {
                    "schema": 1,
                    "triggers": {
                        "a" * 32 + "\n": {
                            "trigger": {**stored_trigger, "trigger_id": "a" * 32 + "\n"},
                            "secret_sha256": "b" * 64,
                        }
                    },
                }
            ),
            webhooks.CORRUPT_STORE,
        ),
    ]

    for text, code in broken:
        path.write_text(text, encoding="utf-8", newline="")
        before = path.read_bytes()
        store = _store(path, clock)
        assert _refused(lambda: store.list("personal")).code == code, text
        assert _refused(lambda: store.get("a" * 32)).code == code, text
        # A receiver has to fail closed rather than answer "no" to everything,
        # which would look like a misconfigured sender instead of a broken file.
        assert _refused(lambda: store.authenticate("a" * 32, "token")).code == code, text
        assert (
            _refused(
                lambda: store.create(name="x", workspace="personal", instructions="go")
            ).code
            == code
        )
        assert _refused(lambda: store.rotate_secret("a" * 32, expected_revision=1)).code == code
        assert _refused(lambda: store.revoke_workspace("personal")).code == code
        assert path.read_bytes() == before, f"a broken store was rewritten: {text!r}"

    # An unreadable file is the same class of refusal, not an empty store.
    if not sys.platform.startswith("win") and getattr(os, "geteuid", lambda: 1)() != 0:
        path.write_bytes(json.dumps(good).encode("utf-8"))
        path.chmod(0o000)
        assert _refused(lambda: _store(path, clock).list("personal")).code == webhooks.CORRUPT_STORE
        path.chmod(0o600)
        assert [row.trigger_id for row in _store(path, clock).list("personal")] == ["a" * 32]

    # A directory where the store belongs is a path refusal, not a reset.
    directory = path.parent / "webhooks-dir.json"
    directory.mkdir()
    assert _refused(lambda: _store(directory, clock).list("personal")).code == webhooks.UNSAFE_PATH


# ── A failed write preserves the previous document ─────────────────────────


def test_private_atomic_write_failure_preserves_prior_file(
    path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating the private temp, writing it, or replacing it can all fail."""
    store = _store(path, clock)
    trigger, secret = _enabled_trigger(store)
    before = path.read_bytes()

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.ENOSPC, "no space left on device")

    # 1. The owner-private temp cannot be created at all.
    monkeypatch.setattr(webhooks, "mkstemp_private", boom)
    with pytest.raises(OSError):
        store.create(name="no temp", workspace="personal", instructions="go")
    assert path.read_bytes() == before

    # 2. The temp exists but the write cannot land.
    monkeypatch.undo()
    monkeypatch.setattr(webhooks.os, "fsync", boom)
    with pytest.raises(OSError):
        store.create(name="unwritten", workspace="personal", instructions="go")
    assert path.read_bytes() == before
    assert _temp_residue(path) == [], "the temp of a failed write is cleaned up"

    # 3. The replace fails, which is the window where a naive writer leaves a
    #    half-installed document behind.
    monkeypatch.undo()
    monkeypatch.setattr(webhooks, "replace_file", boom)
    with pytest.raises(OSError):
        store.create(name="unreplaced", workspace="personal", instructions="go")
    assert path.read_bytes() == before
    assert _temp_residue(path) == []

    # The prior trigger and its credential are untouched throughout: no call
    # ever reported a credential it could not store.
    monkeypatch.undo()
    assert store.get(trigger.trigger_id) == trigger
    assert store.authenticate(trigger.trigger_id, secret) == trigger


# ── Path safety ────────────────────────────────────────────────────────────


def test_symlinked_store_or_lock_is_refused(tmp_path: Path, clock: _Clock) -> None:
    """A link at the store path or at the lock path is refused, not followed."""
    store_path = tmp_path / "webhooks.json"
    lock_path = tmp_path / "webhooks.json.lock"
    store = _store(store_path, clock)

    for linked in (store_path, lock_path):
        target = tmp_path / f"target-{linked.name}"
        target.write_text('{"schema": 1, "triggers": {}}\n', encoding="utf-8", newline="")
        try:
            linked.symlink_to(target)
        except OSError as exc:  # Windows without Developer Mode or admin
            pytest.skip(f"cannot create a symlink here: {exc}")
        original = target.read_bytes()
        assert _refused(lambda: store.list("personal")).code == webhooks.UNSAFE_PATH
        assert (
            _refused(
                lambda: store.create(name="x", workspace="personal", instructions="go")
            ).code
            == webhooks.UNSAFE_PATH
        )
        assert target.read_bytes() == original, "the link target is never written through"
        linked.unlink()

    # A dangling link is refused too, before anything is created.
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "never-created.json")
    assert _refused(lambda: _store(dangling, clock).list("personal")).code == webhooks.UNSAFE_PATH

    # A relative component is refused without touching the filesystem, so it
    # cannot resolve differently later.
    indirect = tmp_path / "runtime" / ".." / "runtime" / "webhooks.json"
    assert _refused(lambda: _store(indirect, clock).list("personal")).code == webhooks.UNSAFE_PATH
    assert (
        _refused(
            lambda: _store(indirect, clock).create(
                name="x", workspace="personal", instructions="go"
            )
        ).code
        == webhooks.UNSAFE_PATH
    )


def test_store_is_private_using_platform_privacy_helper(
    path: Path, clock: _Clock
) -> None:
    """The store and its lock are owner-only by the platform's own definition."""
    store = _store(path, clock)
    trigger, _ = store.create(name="CI push", workspace="personal", instructions="go")

    assert is_private(path), "a file holding credential verifiers is owner-only"
    assert is_private(path.with_name(f"{path.name}.lock"))

    # Rotation replaces the file, and the replacement is private too: mode bits
    # on POSIX, a protected DACL on Windows, through the same helper.
    store.rotate_secret(trigger.trigger_id, expected_revision=trigger.revision)
    assert is_private(path)
    assert path.read_bytes().startswith(b"{\n")

    # Even over a store something else left readable, the next write tightens it.
    path.chmod(0o644)
    store.create(name="second", workspace="personal", instructions="go")
    assert is_private(path), "a replaced store must not inherit a looser mode"
    assert store.get(trigger.trigger_id).name == "CI push"


# ── Deletion ───────────────────────────────────────────────────────────────


def test_delete_removes_the_record_under_a_revision_check(
    path: Path, clock: _Clock
) -> None:
    """Delete drops the record so `get` raises `not_found`.

    A stale revision writes nothing first: the bytes, the record and the
    credential are exactly as they were until the caller names the revision it
    actually read.
    """
    store = _store(path, clock)
    trigger, secret = _enabled_trigger(store)
    before = path.read_bytes()

    stale = _refused(
        lambda: store.delete(trigger.trigger_id, expected_revision=trigger.revision + 1)
    )
    assert stale.code == webhooks.REVISION_CONFLICT
    assert path.read_bytes() == before, "a refused delete must not touch the bytes"

    store.delete(trigger.trigger_id, expected_revision=trigger.revision)

    assert _refused(lambda: store.get(trigger.trigger_id)).code == webhooks.NOT_FOUND
    assert store.list("personal") == []
    assert store.authenticate(trigger.trigger_id, secret) is None
    assert (
        _refused(
            lambda: store.delete(trigger.trigger_id, expected_revision=trigger.revision)
        ).code
        == webhooks.NOT_FOUND
    )
