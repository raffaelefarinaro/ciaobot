"""Webhook trigger configuration and its separately revocable secrets (#981).

This is the *foundation* for the webhook feature tracked in #974, not a shipped
endpoint. Nothing here is mounted: there is no route, no receiver, no model
turn, no startup wiring, and no runtime instance. What it owns is the part that
is expensive to add later — a typed record, one strict on-disk schema, and the
credential lifecycle that must never be redone:

- **A trigger credential is not a dashboard credential.** Each trigger owns a
  random 32-byte token; only its SHA-256 verifier is stored. The raw token is
  returned exactly once, by ``create`` and ``rotate_secret``, and appears
  nowhere else: not in the file, not in a public record, not in ``repr``, not
  in an error, not in a log. Login passwords and session cookies are unrelated
  to this store and grant nothing here — the only thing that ever verifies is
  one trigger's own secret, against one trigger.
- **Disabled by default.** A new trigger is stored with ``enabled: false`` and
  cannot authenticate until it is enabled on purpose.
- **Two different ways to lose access, and they are not the same thing.**
  ``rotate_secret`` replaces the verifier, which invalidates the previous
  secret immediately. ``revoke_workspace`` disables the trigger *and destroys*
  its verifier, so toggling ``enabled`` back on afterwards restores no
  authorization: the trigger has to be rotated before it can be enabled again.
- **Fail closed, never fail silent.** A store that cannot be read the way it was
  written — bad JSON, an unknown schema version, a record this code cannot
  parse — raises. It is never reset, repaired or silently emptied, because the
  records in it are the only copy of what an operator configured, and a
  "recovered" store would quietly delete every trigger.

The schema is ``{"schema": 1, "triggers": {"<id>": {"trigger": {...},
"secret_sha256": "..."}}}``: the public record and the credential verifier are
two separate objects, so the verifier cannot reach a caller's hands by accident
through ``WebhookTrigger``, a ``dict`` or a traceback. See
``docs/WEBHOOK_TRIGGER_STORE.md`` for the full contract.

Deliberately **not** here, because a later child owns each of them: whether the
named workspace is registered and the named project exists (this store
validates the *shape* of a target, never membership), the HTTP ingress and its
session/bearer split, idempotency receipts and queue limits, dispatch into an
ordinary chat, and the Automations UI.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from ciao.async_reads import keyed_lock
from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.links import is_link
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import mkstemp_private, open_private
from ciao.workspaces import WORKSPACE_NAME_RE

SCHEMA_VERSION = 1

# Ordinary permission modes only. ``bypass`` is a real ``BridgeMode``
# (``ciao.models``) and is deliberately absent: an unattended turn is a trust
# decision a remote sender must not be able to make, and a foundation that can
# store it would hand that decision to whoever writes the store next.
WEBHOOK_MODES = ("normal", "auto", "plan")
# Event text is the only input a trigger accepts for now. Caller-supplied
# prompts, payload URLs and file attachments are all later trust decisions.
WEBHOOK_INPUT_POLICIES = ("event_text",)

WebhookMode = Literal["normal", "auto", "plan"]
WebhookInputPolicy = Literal["event_text"]

#: The one input policy this child supports; a record can hold nothing else.
DEFAULT_INPUT_POLICY: WebhookInputPolicy = "event_text"
DEFAULT_MODE: WebhookMode = "auto"

MAX_NAME_LENGTH = 120
MAX_INSTRUCTIONS_LENGTH = 16_000

#: Bytes of entropy in a minted secret. Only the SHA-256 of it is stored.
SECRET_BYTES = 32
#: A presented secret longer than this is refused before it is hashed, so an
#: unbounded request body cannot turn a credential check into unbounded work.
#: ``secrets.token_urlsafe(32)`` is 43 characters, so real secrets are far
#: below it and no legitimate token is ever rejected by this bound.
MAX_SECRET_LENGTH = 128

# Stable error codes. They are part of the contract a later route and its tests
# match on, so they are module constants rather than free strings.
INVALID_TRIGGER = "invalid_trigger"
UNSUPPORTED_SCHEMA = "unsupported_schema"
CORRUPT_STORE = "corrupt_store"
NOT_FOUND = "not_found"
REVISION_CONFLICT = "revision_conflict"
UNSAFE_PATH = "unsafe_path"
ERROR_CODES = (
    INVALID_TRIGGER,
    UNSUPPORTED_SCHEMA,
    CORRUPT_STORE,
    NOT_FOUND,
    REVISION_CONFLICT,
    UNSAFE_PATH,
)

# These four are always applied with ``fullmatch``, never ``match``: ``$`` also
# matches just before a trailing newline, so ``match("personal\n")`` would accept
# a name that is then filed under a different key than the one a caller revokes.
#
# ``uuid4().hex``, exactly: the id is opaque, carries nothing about the target,
# and is compared as a string everywhere.
_TRIGGER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# A presented secret is a base64url token and nothing else. Rejecting every
# other shape (a header with padding, a JSON object, an embedded NUL) keeps the
# comparison a plain digest comparison instead of a comparison against repaired
# input.
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# SHA-256 in hex, or the empty string for a revoked record whose verifier has
# been destroyed.
_VERIFIER_RE = re.compile(r"^[0-9a-f]{64}$")

# Path components that must never be written through: they make a stored path
# depend on a directory's current contents rather than naming one file.
_UNSAFE_COMPONENTS = frozenset({".", ".."})

_TRIGGER_FIELDS = (
    "trigger_id",
    "name",
    "workspace",
    "project_id",
    "instructions",
    "enabled",
    "mode",
    "input_policy",
    "created_at",
    "updated_at",
    "revision",
)
_ENTRY_FIELDS = frozenset({"trigger", "secret_sha256"})

# The registry's own workspace-name rule, reused for the project half of a
# target rather than a second one: it already refuses an empty name, a slash and
# a dot-segment, and the ids minted for projects are ``proj-<8 hex>``. What it
# does *not* answer is whether either target exists — that is a later service's
# obligation, not this store's.
_IDENTIFIER_RE = WORKSPACE_NAME_RE


class WebhookStoreError(Exception):
    """A refusal from the webhook trigger store, carrying a stable ``code``.

    One class with a code rather than a subclass per failure, because a caller
    (a route, a CLI, a receiver) matches on the code and a growing set of
    exception classes would make it import one per reason. The codes:

    - ``invalid_trigger``: an argument is not a trigger this store will store —
      a wrong type, an empty or overlong name, a bad target, an unsupported mode
      or input policy, or a stale-looking field. Nothing was written.
    - ``unsupported_schema``: the file on disk is a schema this code does not
      implement. Never migrated, never rewritten; the bytes are left alone.
    - ``corrupt_store``: the file is there and this code cannot read it as a
      schema-1 document (bad JSON, a missing or mistyped field, an unknown key,
      an id that is not one this code minted). Never reset.
    - ``not_found``: no such trigger id in the store.
    - ``revision_conflict``: the caller's ``expected_revision`` is not the
      record's current revision. Nothing was written.
    - ``unsafe_path``: the store path or its lock is a link, contains a
      ``.``/``..`` component, names no file, or cannot be opened. Nothing was
      written.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WebhookTrigger:
    """One configured trigger, as everything outside this module may see it.

    There is no field here that holds, carries or can reveal a credential: the
    verifier lives in a separate internal record (:class:`_StoredTrigger`), so
    this dataclass — and every dict built from it — is safe to return from an
    API, write into a log, or put in a ``repr``.
    """

    trigger_id: str
    name: str
    workspace: str
    # ``None`` means the workspace's General project. An explicit project is
    # validated against the registry by a later service; this store only knows
    # the shape.
    project_id: str | None
    instructions: str
    enabled: bool
    mode: WebhookMode
    input_policy: WebhookInputPolicy
    created_at: str
    updated_at: str
    #: Per-record optimistic concurrency counter, from 1. It guards this one
    #: record, not the file: two records can be written without either caller
    #: naming the other's revision.
    revision: int

    def to_dict(self) -> dict[str, Any]:
        """The public record as JSON stores it. Never contains a secret."""
        return {
            "trigger_id": self.trigger_id,
            "name": self.name,
            "workspace": self.workspace,
            "project_id": self.project_id,
            "instructions": self.instructions,
            "enabled": self.enabled,
            "mode": self.mode,
            "input_policy": self.input_policy,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class _StoredTrigger:
    """A trigger record together with its credential verifier.

    ``repr=False`` on the verifier is the belt to the record separation's
    braces: even a future field, a ``dataclasses.asdict`` in a debug helper or a
    traceback that prints this object cannot print a stored credential digest.
    """

    trigger: WebhookTrigger
    secret_sha256: str = field(repr=False)

    def to_entry(self) -> dict[str, Any]:
        return {"trigger": self.trigger.to_dict(), "secret_sha256": self.secret_sha256}


# ── Field validation ───────────────────────────────────────────────────────
#
# Every validator takes the error ``code`` it reports under, because the same
# rule answers two different questions: "is this a trigger this store will
# store?" (caller input, ``invalid_trigger``) and "is this a record this store
# wrote?" (bytes on disk, ``corrupt_store``). Neither coerces a type and
# neither repairs a value: a strict refusal is the only way a mistyped field
# becomes loud instead of silently becoming something else.


def _text(value: Any, *, field_name: str, code: str) -> str:
    """A required string, unaltered.

    ``isinstance`` and not a truthiness test, so ``0``, ``False`` and a list are
    refused as the wrong type rather than read as "empty".
    """
    if not isinstance(value, str):
        raise WebhookStoreError(
            f"{field_name} must be a string, not {type(value).__name__}", code=code
        )
    return value


def _validated_name(value: Any, *, code: str) -> str:
    """A display name: 1-120 characters after surrounding whitespace is trimmed.

    Surrounding whitespace is the one repair allowed here, and it is a display
    label; nothing else about the value is adjusted.
    """
    name = _text(value, field_name="trigger name", code=code).strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise WebhookStoreError(
            f"trigger name must be 1-{MAX_NAME_LENGTH} characters", code=code
        )
    return name


def _validated_workspace(value: Any, *, code: str) -> str:
    """A workspace name in the registry's own spelling.

    Shape only. Whether the workspace is registered, and whether it is live, is
    a later service's check — refusing an unregistered name here would make this
    store own workspace lifecycle.
    """
    workspace = _text(value, field_name="trigger workspace", code=code)
    if not _IDENTIFIER_RE.fullmatch(workspace):
        raise WebhookStoreError(
            "trigger workspace must be a registered-style name (letters, digits, "
            "dashes or underscores, up to 64 characters)",
            code=code,
        )
    return workspace


def _validated_project_id(value: Any, *, code: str) -> str | None:
    """``None`` for the workspace's General project, or a project id."""
    if value is None:
        return None
    project_id = _text(value, field_name="trigger project_id", code=code)
    if not _IDENTIFIER_RE.fullmatch(project_id):
        raise WebhookStoreError(
            "trigger project_id must look like a project id (letters, digits, "
            "dashes or underscores, up to 64 characters)",
            code=code,
        )
    return project_id


def _validated_instructions(value: Any, *, code: str) -> str:
    """Fixed instructions for the turn, bounded at 16,000 characters.

    An empty string is stored as given: refusing it would be a policy about what
    a trigger is *for*, and this child does not own that.
    """
    instructions = _text(value, field_name="trigger instructions", code=code)
    if len(instructions) > MAX_INSTRUCTIONS_LENGTH:
        raise WebhookStoreError(
            f"trigger instructions must be at most {MAX_INSTRUCTIONS_LENGTH} "
            "characters",
            code=code,
        )
    return instructions


def _validated_mode(value: Any, *, code: str) -> WebhookMode:
    """One of the ordinary permission modes."""
    mode = _text(value, field_name="trigger mode", code=code)
    if mode not in WEBHOOK_MODES:
        raise WebhookStoreError(
            f"trigger mode {mode!r} is not supported ({', '.join(WEBHOOK_MODES)})",
            code=code,
        )
    return cast(WebhookMode, mode)


def _validated_input_policy(value: Any, *, code: str) -> WebhookInputPolicy:
    """The one input policy this child supports."""
    policy = _text(value, field_name="trigger input_policy", code=code)
    if policy not in WEBHOOK_INPUT_POLICIES:
        raise WebhookStoreError(
            f"trigger input_policy {policy!r} is not supported "
            f"({', '.join(WEBHOOK_INPUT_POLICIES)})",
            code=code,
        )
    return cast(WebhookInputPolicy, policy)


def _validated_enabled(value: Any, *, code: str) -> bool:
    """A real boolean. ``1`` is refused: it is not ``True``, and coercing it
    would let a JSON ``true``/``1`` drift through a schema as a state change."""
    if not isinstance(value, bool):
        raise WebhookStoreError(
            f"trigger enabled must be a boolean, not {type(value).__name__}", code=code
        )
    return value


def _validated_revision(value: Any, *, code: str) -> int:
    """A record revision: a real ``int`` of at least 1.

    ``type(...) is int`` rather than ``isinstance``, because ``True`` is an
    ``int`` and ``expected_revision=True`` must not pass as revision 1.
    """
    if type(value) is not int or value < 1:
        raise WebhookStoreError(
            "expected_revision must be an integer of at least 1", code=code
        )
    return value


def _validated_stored_verifier(value: Any, *, code: str) -> str:
    """A stored verifier: SHA-256 hex, or empty for a revoked record."""
    verifier = _text(value, field_name="secret_sha256", code=code)
    if verifier and not _VERIFIER_RE.fullmatch(verifier):
        raise WebhookStoreError(
            "secret_sha256 must be a lowercase SHA-256 hex digest, or empty for "
            "a revoked trigger",
            code=code,
        )
    return verifier


def _verifier(secret: str) -> str:
    """The stored form of a raw secret: its SHA-256, hex.

    A digest of a 32-byte random token, so the stored value cannot be turned
    back into the secret without the secret. Compared with
    ``hmac.compare_digest`` everywhere it is checked.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ── Reading the file ───────────────────────────────────────────────────────


def _decode_trigger(raw: Any, *, trigger_id: str, path: Path) -> WebhookTrigger:
    """Parse one stored public record, or raise ``corrupt_store``.

    Unknown keys are a refusal rather than something to drop: a record written
    by a newer engine may carry a field whose loss would silently restore an
    authorization this code cannot see (a future ``revoked_at``, say), so the
    loud failure is the safe one.
    """
    if not isinstance(raw, dict):
        raise WebhookStoreError(
            f"webhook trigger {trigger_id} in {path.name} is not a record object",
            code=CORRUPT_STORE,
        )
    fields = set(_TRIGGER_FIELDS)
    missing = [name for name in _TRIGGER_FIELDS if name not in raw]
    if missing or set(raw) != fields:
        unexpected = sorted(set(raw) - fields)
        raise WebhookStoreError(
            f"webhook trigger {trigger_id} in {path.name} does not carry exactly "
            f"the stored fields (missing: {', '.join(missing) or 'none'}; "
            f"unexpected: {', '.join(unexpected) or 'none'})",
            code=CORRUPT_STORE,
        )
    if raw["trigger_id"] != trigger_id:
        raise WebhookStoreError(
            f"webhook trigger {trigger_id} in {path.name} is filed under an id "
            "its record does not carry",
            code=CORRUPT_STORE,
        )
    return WebhookTrigger(
        trigger_id=trigger_id,
        name=_validated_name(raw["name"], code=CORRUPT_STORE),
        workspace=_validated_workspace(raw["workspace"], code=CORRUPT_STORE),
        project_id=_validated_project_id(raw["project_id"], code=CORRUPT_STORE),
        instructions=_validated_instructions(raw["instructions"], code=CORRUPT_STORE),
        enabled=_validated_enabled(raw["enabled"], code=CORRUPT_STORE),
        mode=_validated_mode(raw["mode"], code=CORRUPT_STORE),
        input_policy=_validated_input_policy(raw["input_policy"], code=CORRUPT_STORE),
        created_at=_stored_instant(
            raw["created_at"], field_name="created_at", path=path, trigger_id=trigger_id
        ),
        updated_at=_stored_instant(
            raw["updated_at"], field_name="updated_at", path=path, trigger_id=trigger_id
        ),
        revision=_stored_revision(raw["revision"], path=path, trigger_id=trigger_id),
    )


def _stored_instant(value: Any, *, field_name: str, path: Path, trigger_id: str) -> str:
    """A stored UTC timestamp: an ISO-8601 instant that names a time zone.

    Checked, not merely carried, because ``list`` orders by ``created_at`` and a
    value that is not an instant would order records by something arbitrary.
    """
    stamp = _text(value, field_name=field_name, code=CORRUPT_STORE)
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        raise WebhookStoreError(
            f"webhook trigger {trigger_id} in {path.name} has a {field_name} that "
            "is not an ISO-8601 timestamp",
            code=CORRUPT_STORE,
        ) from None
    if moment.tzinfo is None:
        raise WebhookStoreError(
            f"webhook trigger {trigger_id} in {path.name} has a {field_name} "
            "without a UTC offset",
            code=CORRUPT_STORE,
        )
    return stamp


def _stored_revision(value: Any, *, path: Path, trigger_id: str) -> int:
    """A stored revision of at least 1 (the ``True == 1`` case included)."""
    if type(value) is not int or value < 1:
        raise WebhookStoreError(
            f"webhook trigger {trigger_id} in {path.name} has a revision that is "
            "not an integer of at least 1",
            code=CORRUPT_STORE,
        )
    return value


def _decode_entry(trigger_id: Any, entry: Any, *, path: Path) -> _StoredTrigger:
    """Parse one ``{"trigger": ..., "secret_sha256": ...}`` entry."""
    if not isinstance(trigger_id, str) or not _TRIGGER_ID_RE.fullmatch(trigger_id):
        raise WebhookStoreError(
            f"{path.name} holds a trigger under an id this store did not mint",
            code=CORRUPT_STORE,
        )
    if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
        raise WebhookStoreError(
            f"webhook trigger {trigger_id} in {path.name} is not a "
            "{trigger, secret_sha256} entry",
            code=CORRUPT_STORE,
        )
    return _StoredTrigger(
        trigger=_decode_trigger(entry["trigger"], trigger_id=trigger_id, path=path),
        secret_sha256=_validated_stored_verifier(
            entry["secret_sha256"], code=CORRUPT_STORE
        ),
    )


def _decode(raw: str, *, path: Path) -> dict[str, _StoredTrigger]:
    """Parse a whole store document, or raise.

    A refusal names the file and the record it choked on; it never quotes what
    the file held, so a message that reaches a log or a chat cannot carry a
    stored record out with it.
    """
    try:
        document: Any = json.loads(raw)
    except (ValueError, RecursionError):
        # `RecursionError` is not a `ValueError`: a document nested deeply
        # enough to exhaust the parser's stack fails this way, and it is
        # corruption like any other rather than a crash.
        raise WebhookStoreError(
            f"the webhook store at {path.name} is not valid JSON", code=CORRUPT_STORE
        ) from None
    if not isinstance(document, dict):
        raise WebhookStoreError(
            f"the webhook store at {path.name} is not a JSON object",
            code=CORRUPT_STORE,
        )
    # `type(...) is not int`, not `!= SCHEMA_VERSION`: a document whose schema is
    # the JSON `true` would otherwise pass as version 1.
    if type(document.get("schema")) is not int:
        raise WebhookStoreError(
            f"the webhook store at {path.name} has no integer schema version",
            code=UNSUPPORTED_SCHEMA,
        )
    if document["schema"] != SCHEMA_VERSION:
        raise WebhookStoreError(
            f"the webhook store at {path.name} is schema {document['schema']}; this "
            f"engine reads schema {SCHEMA_VERSION} and will not rewrite it",
            code=UNSUPPORTED_SCHEMA,
        )
    entries = document.get("triggers")
    if not isinstance(entries, dict):
        raise WebhookStoreError(
            f"the webhook store at {path.name} has no triggers object",
            code=CORRUPT_STORE,
        )
    return {
        trigger_id: _decode_entry(trigger_id, entry, path=path)
        for trigger_id, entry in entries.items()
    }


# ── The store ──────────────────────────────────────────────────────────────


class WebhookStore:
    """Configured webhook triggers and their revocable secrets, in one file.

    The constructor takes the path explicitly and reads no operator state, so
    this class is inert until a later child decides where the engine's file
    lives (the plan names ``<runtime>/webhooks.json``) and instantiates it.

    Reads (:meth:`list`, :meth:`get`, :meth:`authenticate`) take no lock: the
    file is replaced atomically, so a lock-free reader sees one whole document
    and is never made to queue behind a writer's read-modify-write.
    """

    def __init__(
        self, path: Path, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_name(f"{self._path.name}.lock")
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._lock_key = f"webhooks:{os.fspath(self._path)}"

    # -- paths and locking ------------------------------------------------

    def _require_safe_paths(self) -> None:
        """Refuse a store or lock path this store must not write through.

        Three refusals: a ``.``/``..`` component (the path names a directory's
        current contents rather than a file), a store or lock file that is
        itself a link, and a path that names no file at all. Only the final
        component is checked for being a link: parent directories are ordinary
        on any platform a person installs on, and a link there is a layout
        choice (macOS resolves ``/var`` to ``/private/var``), not an attack on
        this store. The link refusal is duplicated at the open — ``O_NOFOLLOW``
        on POSIX, ``FILE_FLAG_OPEN_REPARSE_POINT`` on Windows — so there is no
        window between this check and the open; this one exists to make the
        refusal a named, testable error instead of an ``OSError`` from deep
        inside a write.
        """
        for candidate, role in (
            (self._path, "webhook store"),
            (self._lock_path, "webhook store lock"),
        ):
            if not candidate.name:
                raise WebhookStoreError(
                    f"the {role} path {candidate} names no file", code=UNSAFE_PATH
                )
            if any(part in _UNSAFE_COMPONENTS for part in candidate.parts):
                raise WebhookStoreError(
                    f"the {role} path {candidate} contains a '.' or '..' component",
                    code=UNSAFE_PATH,
                )
            if is_link(candidate):
                raise WebhookStoreError(
                    f"refusing to use the {role} at {candidate}: it is a link",
                    code=UNSAFE_PATH,
                )

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        """Hold both locks a mutation needs, and nothing else.

        ``keyed_lock`` serializes writers inside this process; the advisory
        sibling lock serializes them across processes and against a second
        ``WebhookStore`` object — which is the case that loses a record, because
        a store object that cached the document would write back its own older
        copy. The two are always taken in this order.

        The advisory lock is a lock: it coordinates callers that take it. It is
        not a sandbox — a local process that writes the store without taking it
        is not stopped, and neither lock prevents that. What they do guarantee
        is that two store clients, in this process or another, cannot lose one
        another's records.
        """
        with keyed_lock(self._lock_key):
            self._require_safe_paths()
            # The lock is a sibling of the store, so it needs the store's
            # directory to exist before it can be created. This is the only
            # state a mutation creates before its write: no store file, no
            # record, nothing a read would see.
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = self._open_lock()
            try:
                lock_exclusive(descriptor)
                try:
                    yield
                finally:
                    unlock(descriptor)
            finally:
                os.close(descriptor)

    def _open_lock(self) -> int:
        """Open (creating if needed) the owner-private advisory lock file.

        ``open_private`` with ``follow_symlinks=False`` is both halves at once:
        the lock is created 0600 — a protected DACL on Windows, so it is private
        from the instant it exists — and a link at that path is refused with
        ``ELOOP`` rather than followed.
        """
        try:
            return open_private(
                self._lock_path, os.O_RDWR | os.O_CREAT, 0o600, follow_symlinks=False
            )
        except OSError as exc:
            raise WebhookStoreError(
                f"cannot open the webhook store lock at {self._lock_path.name}: "
                f"{exc.strerror or exc}",
                code=UNSAFE_PATH,
            ) from None

    # -- reading and writing ----------------------------------------------

    def _read(self) -> dict[str, _StoredTrigger]:
        """The whole document, or ``{}`` when there is no file yet.

        A missing file is a store that has never been written, and reading it
        creates nothing: a read must not leave a file behind that later reads
        then treat as state.
        """
        self._require_safe_paths()
        if self._path.is_dir():
            raise WebhookStoreError(
                f"the webhook store at {self._path.name} is a directory",
                code=UNSAFE_PATH,
            )
        try:
            descriptor = open_fd(self._path, os.O_RDONLY, follow_symlinks=False)
        except FileNotFoundError:
            return {}
        except IsADirectoryError:
            raise WebhookStoreError(
                f"the webhook store at {self._path.name} is a directory",
                code=UNSAFE_PATH,
            ) from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise WebhookStoreError(
                    f"refusing to read the webhook store at {self._path.name}: "
                    "it is a link",
                    code=UNSAFE_PATH,
                ) from None
            raise WebhookStoreError(
                f"the webhook store at {self._path.name} cannot be read: "
                f"{exc.strerror or exc}",
                code=CORRUPT_STORE,
            ) from None
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as handle:
                raw = handle.read()
        except (OSError, ValueError):
            # A fixed message, not `{exc}`: the `UnicodeDecodeError` this branch
            # usually catches renders the offending byte, and this is the one
            # refusal whose cause could quote the file's own contents back out.
            raise WebhookStoreError(
                f"the webhook store at {self._path.name} is not valid UTF-8",
                code=CORRUPT_STORE,
            ) from None
        return _decode(raw, path=self._path)

    def _write(self, records: dict[str, _StoredTrigger]) -> None:
        """Write the whole document atomically, owner-private.

        A unique sibling temp (``mkstemp_private``: 0600 on POSIX, a protected
        DACL on Windows), flushed and fsynced, then one ``replace_file``. A
        reader therefore sees the old document or the new one, never half of
        either. Only *this* call's temp is cleaned up in the ``finally``, so
        another writer's temp is never deleted out from under its rename.

        The replaced file's mode is deliberately not carried over: this file
        holds credential verifiers, so a store left world-readable by anything
        else is tightened to owner-only by the next write rather than
        preserved.

        A failure to create, write or replace raises and leaves the previous
        document in place. Nothing here reports success it cannot back up with
        bytes on disk, which is what keeps a freshly minted secret from being
        handed back as usable when it was never stored.
        """
        self._require_safe_paths()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema": SCHEMA_VERSION,
            "triggers": {
                trigger_id: record.to_entry() for trigger_id, record in records.items()
            },
        }
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        descriptor, temp_name = mkstemp_private(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        temporary = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(temporary, self._path)
        finally:
            # A no-op once the rename landed, a cleanup when it did not.
            temporary.unlink(missing_ok=True)

    # -- reading ----------------------------------------------------------

    def list(self, workspace: str) -> list[WebhookTrigger]:
        """Every trigger configured for ``workspace``, oldest first.

        Ordered by ``(created_at, trigger_id)``, which is total and stable: the
        id breaks a tie between two records stamped in the same second, so the
        order does not depend on dict iteration or on the clock.
        """
        checked = _validated_workspace(workspace, code=INVALID_TRIGGER)
        records = self._read()
        return sorted(
            (
                record.trigger
                for record in records.values()
                if record.trigger.workspace == checked
            ),
            key=lambda trigger: (trigger.created_at, trigger.trigger_id),
        )

    def get(self, trigger_id: str) -> WebhookTrigger:
        """One trigger by id, or raise ``not_found``."""
        return _require_record(self._read(), trigger_id).trigger

    # -- writing ----------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        workspace: str,
        project_id: str | None = None,
        instructions: str,
        mode: WebhookMode = DEFAULT_MODE,
    ) -> tuple[WebhookTrigger, str]:
        """Store a new trigger and mint its first secret.

        Returns ``(trigger, secret)``. The secret is the only copy that will
        ever exist: it is written nowhere, and the file holds only its SHA-256.
        A caller that loses it rotates; there is nothing to recover.

        The trigger is stored **disabled**, whatever the caller asks for, and
        with ``input_policy`` fixed to the only policy this child supports. A
        new trigger can be configured freely while disabled; it simply cannot
        authenticate until it is enabled on purpose, with the current revision.
        """
        checked_name = _validated_name(name, code=INVALID_TRIGGER)
        checked_workspace = _validated_workspace(workspace, code=INVALID_TRIGGER)
        checked_project = _validated_project_id(project_id, code=INVALID_TRIGGER)
        checked_instructions = _validated_instructions(
            instructions, code=INVALID_TRIGGER
        )
        checked_mode = _validated_mode(mode, code=INVALID_TRIGGER)
        with self._mutation():
            records = self._read()
            stamp = self._stamp()
            # Minted inside the critical section, after the read that this write
            # will replace: a secret that is never stored must never be handed
            # back, and a store that cannot be read must not mint one at all.
            secret = secrets.token_urlsafe(SECRET_BYTES)
            trigger = WebhookTrigger(
                trigger_id=uuid.uuid4().hex,
                name=checked_name,
                workspace=checked_workspace,
                project_id=checked_project,
                instructions=checked_instructions,
                enabled=False,
                mode=checked_mode,
                input_policy=DEFAULT_INPUT_POLICY,
                created_at=stamp,
                updated_at=stamp,
                revision=1,
            )
            records[trigger.trigger_id] = _StoredTrigger(
                trigger=trigger, secret_sha256=_verifier(secret)
            )
            self._write(records)
        return trigger, secret

    def update(
        self,
        trigger_id: str,
        *,
        expected_revision: int,
        name: str | None = None,
        instructions: str | None = None,
        enabled: bool | None = None,
    ) -> WebhookTrigger:
        """Change a trigger's configuration under an optimistic revision check.

        ``expected_revision`` is the revision the caller read. A mismatch
        raises ``revision_conflict`` and writes nothing, so two callers editing
        the same trigger cannot silently overwrite one another; the loser
        re-reads and decides again.

        Only these three fields are mutable here. The target (``workspace``,
        ``project_id``) and the ``mode`` are not parameters, because retargeting
        an existing trigger or escalating its permission mode under a secret
        somebody already holds is a trust change, and it needs its own design
        rather than an optional argument. Enable a revoked trigger's
        configuration freely, though: ``enabled`` is exactly how a trigger is
        configured while it stays uncallable.

        An update that would change nothing returns the stored record as it is,
        with its own revision, and writes nothing.
        """
        expected = _validated_revision(expected_revision, code=INVALID_TRIGGER)
        checked_name = (
            None
            if name is None
            else _validated_name(name, code=INVALID_TRIGGER)
        )
        checked_instructions = (
            None
            if instructions is None
            else _validated_instructions(instructions, code=INVALID_TRIGGER)
        )
        checked_enabled = (
            None if enabled is None else _validated_enabled(enabled, code=INVALID_TRIGGER)
        )
        with self._mutation():
            records = self._read()
            record = _require_record(records, trigger_id)
            _require_current(record, expected)
            if checked_enabled is True and not record.secret_sha256:
                raise WebhookStoreError(
                    f"webhook trigger {trigger_id} was revoked and holds no "
                    "verifier; rotate its secret before enabling it",
                    code=INVALID_TRIGGER,
                )
            current = record.trigger
            next_name = current.name if checked_name is None else checked_name
            next_instructions = (
                current.instructions
                if checked_instructions is None
                else checked_instructions
            )
            next_enabled = current.enabled if checked_enabled is None else checked_enabled
            if (next_name, next_instructions, next_enabled) == (
                current.name,
                current.instructions,
                current.enabled,
            ):
                return current
            updated = replace(
                current,
                name=next_name,
                instructions=next_instructions,
                enabled=next_enabled,
                revision=current.revision + 1,
                updated_at=self._stamp(),
            )
            records[trigger_id] = _StoredTrigger(
                trigger=updated, secret_sha256=record.secret_sha256
            )
            self._write(records)
            return updated

    def rotate_secret(
        self, trigger_id: str, *, expected_revision: int
    ) -> tuple[WebhookTrigger, str]:
        """Replace a trigger's secret and return ``(trigger, secret)``.

        Immediate: the old secret stops verifying the moment the new document
        is on disk, and it is never recoverable from this store.

        Rotation preserves the trigger's ``enabled`` state, in both directions.
        Rotating does not enable a disabled trigger — a secret is not consent to
        run anything — and rotating an enabled one keeps it enabled, because
        the operator who rotated it asked for the trigger to keep working.
        """
        expected = _validated_revision(expected_revision, code=INVALID_TRIGGER)
        with self._mutation():
            records = self._read()
            record = _require_record(records, trigger_id)
            _require_current(record, expected)
            secret = secrets.token_urlsafe(SECRET_BYTES)
            updated = replace(
                record.trigger,
                revision=record.trigger.revision + 1,
                updated_at=self._stamp(),
            )
            records[trigger_id] = _StoredTrigger(
                trigger=updated, secret_sha256=_verifier(secret)
            )
            self._write(records)
            return updated, secret

    def delete(self, trigger_id: str, *, expected_revision: int) -> None:
        """Remove a trigger and its verifier under an optimistic revision check.

        ``expected_revision`` is the revision the caller read. A mismatch
        raises ``revision_conflict`` and writes nothing, so a caller working
        from a stale read cannot delete a trigger it never saw; an unknown id
        raises ``not_found``. A deleted secret is gone with its record: there
        is nothing to recover, and re-creating the trigger mints a new secret.
        """
        expected = _validated_revision(expected_revision, code=INVALID_TRIGGER)
        with self._mutation():
            records = self._read()
            record = _require_record(records, trigger_id)
            _require_current(record, expected)
            del records[record.trigger.trigger_id]
            self._write(records)

    def revoke_workspace(self, workspace: str) -> int:
        """Destroy every verifier in ``workspace``; return how many changed.

        Disabling is not enough here. A disabled trigger can be enabled again,
        and enabling it would hand its old secret back its authorization — so
        revocation also destroys the verifier, which means the only way back is
        to rotate. Triggers in other workspaces are untouched.

        Only records that actually change are advanced, so a second call is a
        no-op that writes nothing and returns 0: revocation is idempotent.
        """
        checked = _validated_workspace(workspace, code=INVALID_TRIGGER)
        with self._mutation():
            records = self._read()
            stamp = self._stamp()
            revoked = 0
            for trigger_id, record in list(records.items()):
                if record.trigger.workspace != checked:
                    continue
                if not record.trigger.enabled and not record.secret_sha256:
                    continue  # already revoked: nothing to advance
                records[trigger_id] = _StoredTrigger(
                    trigger=replace(
                        record.trigger,
                        enabled=False,
                        revision=record.trigger.revision + 1,
                        updated_at=stamp,
                    ),
                    secret_sha256="",
                )
                revoked += 1
            if revoked:
                self._write(records)
            return revoked

    # -- authenticating ---------------------------------------------------

    def authenticate(self, trigger_id: str, secret: str) -> WebhookTrigger | None:
        """The trigger ``secret`` authorizes right now, or ``None``.

        Returns ``None`` — never a record, never a principal, never an
        exception — for a missing trigger, a disabled one, a revoked one, and a
        secret that is empty, malformed, too long or simply wrong. The verifier
        is compared with ``hmac.compare_digest``.

        A corrupt store is *not* one of those: it raises ``corrupt_store``, so
        that the receiver a later child adds fails closed instead of answering
        "no" to every request and looking like a misconfigured sender.

        The input is bounded and shape-checked before it is hashed, so an
        unbounded request cannot make this an unbounded amount of work, and no
        input is repaired: a secret with a trailing newline does not verify.

        The document is re-read here rather than cached, so a secret revoked by
        another process stops verifying on the next call. A check that has
        already returned is a snapshot: a revocation landing after it cannot be
        seen by the turn it authorized, which is the receiver's ordering
        problem and not this store's.
        """
        if (
            not isinstance(trigger_id, str)
            or not isinstance(secret, str)
            or not secret
            or len(secret) > MAX_SECRET_LENGTH
            or not _SECRET_RE.fullmatch(secret)
        ):
            return None
        record = self._read().get(trigger_id)
        if record is None or not record.trigger.enabled or not record.secret_sha256:
            return None
        if not hmac.compare_digest(_verifier(secret), record.secret_sha256):
            return None
        return record.trigger

    # -- clock ------------------------------------------------------------

    def _stamp(self) -> str:
        """The ISO-8601 UTC stamp a changed record carries.

        From the injected clock, so a test (and a replay) can say exactly when
        a record changed; a clock read inside the writer would make a record's
        own bytes depend on when the write happened.
        """
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat(timespec="seconds")


def _require_record(
    records: dict[str, _StoredTrigger], trigger_id: str
) -> _StoredTrigger:
    """The stored record for ``trigger_id``, or raise ``not_found``.

    The id is not shape-checked here: an id this store never minted is simply
    not in the store, which is the same answer a caller can act on.
    """
    record = records.get(trigger_id) if isinstance(trigger_id, str) else None
    if record is None:
        raise WebhookStoreError(
            f"no webhook trigger {trigger_id!r} is configured", code=NOT_FOUND
        )
    return record


def _require_current(record: _StoredTrigger, expected: int) -> None:
    """Refuse a write whose ``expected_revision`` is not the current one."""
    current = record.trigger.revision
    if current != expected:
        raise WebhookStoreError(
            f"webhook trigger {record.trigger.trigger_id} is at revision "
            f"{current}, not {expected}; re-read it before writing",
            code=REVISION_CONFLICT,
        )
