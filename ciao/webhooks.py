"""Webhook trigger configuration, its separately revocable secrets (#981), and
the ingress receiver that records what a secret caused (#1010).

Two layers, deliberately separable:

* **The store** — a typed record, one strict on-disk schema, and the credential
  lifecycle that must never be redone:

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

* **The receiver** — ``WebhookReceiver`` and the durable receipt journal beside
  the store (``<runtime>/webhook-receipts.jsonl``). It owns what an accepted
  event leaves behind: a stable receipt id so a retry collapses instead of
  duplicating, a request digest so the *same* key with a *different* body is a
  conflict rather than a second event, the bounds an unattended sender is held
  to (body bytes, a per-trigger rate window, a per-trigger pending count), and
  the one ordering rule that makes a crash reviewable instead of replayable: the
  receipt is durable before any launch is attempted, and a launch allocation is
  durable before the model turn, so a crash in that window can only ever be
  ``interrupted``.

  Deliberately **not** in the receiver: dispatch. ``WebhookReceiver.begin_launch``
  and ``WebhookReceiver.fail`` exist for A4, which owns ``start_stream``; this
  child never calls them. An accepted event therefore stays open, which is
  exactly what ``MAX_PENDING_RECEIPTS`` bounds. The HTTP edge around it is
  ``ciao/web/routes_hooks.py`` and it shares nothing with the browser session:
  the receiver is a machine surface, authorized only by one trigger's own
  secret.

Still **not** here, because a later child owns each of them: whether the named
workspace is registered and the named project exists (this module validates the
*shape* of a target, never membership), dispatch into an ordinary chat, the
Automations UI, and the CLI/skills/recipes that would describe all of it.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from ciao.async_reads import keyed_lock
from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.links import is_link
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import mkstemp_private, open_private
from ciao.workspaces import WORKSPACE_NAME_RE

logger = logging.getLogger(__name__)

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

    @property
    def path(self) -> Path:
        """The file this store reads and writes.

        Public so the ingress receiver can put its receipt journal beside the
        store without a second spelling of where the store lives — two paths
        that agreed today would be free to drift, and a journal somewhere other
        than next to the triggers it describes is a journal nobody finds.
        """
        return self._path

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


# ── The receiver: durable ingress receipts (#1010, child A3 of #974) ────────
#
# The store answers "may this secret call this trigger". This section answers
# "what happened when it did", durably enough that a retry collapses instead of
# duplicating and a crash is reviewable instead of a silent replay.
#
# The journal is append-only JSONL beside the store
# (``<runtime>/webhook-receipts.jsonl``), modelled on ``ciao/memory_receipts.py``
# — a private lock, ``fsync`` per row, latest row per id wins — and not on
# ``ciao/job_runs.py``, which is fail-open and unlocked. It carries the sender's
# own event text, so it is created 0600 like the store.
#
# Two rules the code enforces rather than documents:
#
# * **Intent is durable before the side effect it describes.** ``accepted`` is on
#   disk before any launch is attempted, and ``launched`` is on disk before the
#   model turn. A crash in the second window is genuinely ambiguous — the turn
#   may or may not have run — so recovery records ``interrupted`` and leaves it
#   for a person. Replaying it would be a guess in the one direction that costs
#   a second run nobody asked for.
# * **A retry is not a new event.** ``(trigger_id, idempotency_key)`` names one
#   attempt; the same key with the same body returns the receipt already written
#   and appends nothing, and the same key with a *different* body is a conflict
#   rather than a second event. That is what makes an at-least-once sender safe.

RECEIPT_VERSION = 1

RECEIPTS_NAME = "webhook-receipts.jsonl"

ACCEPTED = "accepted"
LAUNCHED = "launched"
FAILED = "failed"
INTERRUPTED = "interrupted"

#: The states a receipt never leaves. ``accepted`` (recorded, not yet launched)
#: and ``launched`` (allocation recorded, outcome unknown) are both open, which
#: is what the pending bound counts.
_TERMINAL_RECEIPT_STATES = frozenset({FAILED, INTERRUPTED})

#: Receipt statuses, in the order a healthy attempt walks them.
RECEIPT_STATUSES = (ACCEPTED, LAUNCHED, FAILED, INTERRUPTED)

#: Bytes of request body the receiver will read. A webhook event is a sentence
#: and a handful of fields; 64 KiB is two orders of magnitude above a real one
#: and low enough that an unauthenticated flood cannot become a disk problem.
#: Enforced twice on the HTTP path (declared ``Content-Length``, then the
#: chunked read) because a caller controls the header independently of the body.
MAX_BODY_BYTES = 65_536

#: Characters of event text one event may carry. The body cap is the byte
#: bound; this is the bound on the one field that is actually read.
MAX_EVENT_TEXT_CHARS = 8_000

#: Characters a failure or interruption note may carry. It is a sentence about
#: the engine's own outcome, not the sender's payload, so it is bounded far
#: tighter than the event text it sits beside.
MAX_DETAIL_CHARS = 500

#: Attempts allowed per trigger per minute. A per-trigger window rather than a
#: per-caller one: the credential *is* the caller, and a shared IP is not.
RATE_LIMIT_PER_MINUTE = 10

#: Width of that window, and therefore the ``Retry-After`` a rate-limited
#: sender is given: the window empties a second after the last attempt in it.
RATE_WINDOW_SECONDS = 60

#: How long a pending receipt blocks its trigger before the attempt is refused.
#: Without a dispatch path (A4) an accepted receipt never leaves ``accepted``,
#: so this is what bounds an unattended sender rather than letting receipts
#: accumulate forever.
MAX_PENDING_RECEIPTS = 20

#: How long an ``(trigger_id, idempotency_key)`` pair keeps collapsing retries
#: onto one receipt. A week is longer than any sender's retry schedule; past it
#: the key is the sender's to reuse and a fresh attempt gets a fresh id, so a
#: settled receipt's outcome is never erased by a later arrival.
DEDUPE_RETENTION_DAYS = 7

#: Journal bounds. Bytes and rows both, because a receipt carries event text:
#: a few thousand large rows clear the byte cap long before the row count.
RECEIPTS_MAX_BYTES = 4 * 1024 * 1024
RECEIPTS_KEEP_ROWS = 4000

# Stable error codes, matching the store's convention: a route matches on the
# code, not on the exception class, so adding a reason does not add a class the
# caller has to import.
INVALID_EVENT = "invalid_event"
PAYLOAD_TOO_LARGE = "payload_too_large"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
INVALID_IDEMPOTENCY_KEY = "invalid_idempotency_key"
RATE_LIMITED = "rate_limited"
TOO_MANY_PENDING = "too_many_pending"
RECEIPT_UNAVAILABLE = "receipt_unavailable"
INVALID_RECEIPT = "invalid_receipt"
#: The receiver's codes, for a caller that wants to enumerate them.
RECEIPT_ERROR_CODES = (
    INVALID_EVENT,
    PAYLOAD_TOO_LARGE,
    IDEMPOTENCY_CONFLICT,
    INVALID_IDEMPOTENCY_KEY,
    RATE_LIMITED,
    TOO_MANY_PENDING,
    RECEIPT_UNAVAILABLE,
    INVALID_RECEIPT,
)

#: An idempotency key is a sender-chosen opaque token; anything long enough to
#: be a payload, or holding a control character, is refused rather than stored.
MAX_IDEMPOTENCY_KEY_LENGTH = 200


class WebhookReceiverError(RuntimeError):
    """A refusal from the ingress receiver, carrying a stable ``code``.

    One class with a code, like :class:`WebhookStoreError`, because the route
    maps codes to statuses and would otherwise import a class per reason. The
    codes:

    - ``invalid_event``: the body is not a JSON object, or is not exactly
      ``{"text": ...}`` with text that is a bounded non-empty string. Nothing
      was recorded.
    - ``payload_too_large``: the body exceeds :data:`MAX_BODY_BYTES`. Nothing was
      read past the cap and nothing was recorded.
    - ``invalid_idempotency_key``: the key is missing, empty, over
      :data:`MAX_IDEMPOTENCY_KEY_LENGTH`, or holds a control character.
    - ``idempotency_conflict``: this ``(trigger, key)`` was already accepted
      with a *different* body. A key names one event; two bodies under it mean
      the sender is not retrying, and guessing which one to run is not a
      decision this receiver may make. The existing receipt is untouched.
    - ``rate_limited``: more than :data:`RATE_LIMIT_PER_MINUTE` attempts for this
      trigger in a minute. Nothing was recorded.
    - ``too_many_pending``: the trigger already has
      :data:`MAX_PENDING_RECEIPTS` open receipts. Nothing was recorded.
    - ``receipt_unavailable``: the journal could not be read, written or locked.
      Raised *before* an event is reported accepted, because an accepted event
      with no durable record is the one outcome that cannot be repaired later.
    - ``invalid_receipt``: a journal row cannot be decoded as a receipt this
      code wrote. Failed closed rather than treated as absent.
    """

    #: Whether a sender should simply retry. A rate window and a full trigger
    #: both clear on their own; a corrupt or unwritable journal does not.
    retryable = False

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class PayloadTooLarge(WebhookReceiverError):
    """The body exceeds :data:`MAX_BODY_BYTES`."""

    def __init__(self, size: int) -> None:
        super().__init__(
            f"the request body is over the {MAX_BODY_BYTES}-byte limit ({size} bytes)",
            code=PAYLOAD_TOO_LARGE,
        )


class IdempotencyConflict(WebhookReceiverError):
    """The key was already accepted with a different body."""

    def __init__(self, receipt_id: str) -> None:
        super().__init__(
            f"idempotency key already recorded as receipt {receipt_id} with a "
            "different request body; use a new key",
            code=IDEMPOTENCY_CONFLICT,
        )


class RateLimited(WebhookReceiverError):
    """The trigger is over its per-minute window."""

    retryable = True

    def __init__(self, trigger_id: str) -> None:
        super().__init__(
            f"webhook trigger {trigger_id} is over its limit of "
            f"{RATE_LIMIT_PER_MINUTE} attempts per minute",
            code=RATE_LIMITED,
        )


class TooManyPending(WebhookReceiverError):
    """The trigger already has its bound of open receipts."""

    retryable = True

    def __init__(self, trigger_id: str, pending: int) -> None:
        super().__init__(
            f"webhook trigger {trigger_id} already has {pending} receipts awaiting "
            f"launch (the limit is {MAX_PENDING_RECEIPTS})",
            code=TOO_MANY_PENDING,
        )


class ReceiptUnavailable(WebhookReceiverError):
    """The journal could not be read, appended to, or locked.

    Retryable in the sense that a full disk or a locked file clears, but the
    caller's answer does not change: the event was not recorded, so it did not
    happen, and the sender must retry with the same key.
    """

    retryable = True

    def __init__(self, message: str) -> None:
        super().__init__(message, code=RECEIPT_UNAVAILABLE)


# ── Receipt helpers ────────────────────────────────────────────────────────


def _digest(body: bytes) -> str:
    """The digest of one request body, hex.

    Over the raw bytes, not the parsed event: the question a retry has to answer
    is "is this the same request", and a sender that reformats its JSON between
    attempts has sent a different request, which is worth a conflict rather than
    a silent collapse.
    """
    return hashlib.sha256(body).hexdigest()


def _safe_row(line: str) -> dict[str, Any] | None:
    """One journal line as a row, or None when it is not usable."""
    if not line.strip():
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def read_rows(journal: Path) -> list[dict[str, Any]]:
    """Fold the journal into one effective row per receipt id, oldest first.

    The latest row for an id wins, which is what makes an append-only journal
    order-independent: a crash can lose a trailing row and the prior state stays
    readable. An unparsable line is skipped rather than fatal — a journal is
    appended to, so a torn last line is expected after a crash, and the rows
    before it are still evidence.
    """
    if not journal.exists():
        return []
    try:
        raw = journal.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ReceiptUnavailable(
            f"the webhook receipt journal cannot be read: {exc.strerror or exc}"
        ) from None
    # Split on "\n", never `splitlines()`: a receipt carries the sender's own
    # event text, which may contain U+2028/U+2029/U+0085 — all of which
    # `str.splitlines` treats as line breaks although `_append` writes them
    # literally. Splitting there cut a row in half and the receipt was lost.
    folded: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in raw.split("\n"):
        row = _safe_row(line)
        if row is None:
            continue
        rid = str(row.get("id") or "")
        if not rid:
            continue
        if rid not in folded:
            order.append(rid)
        folded[rid] = row
    return [folded[rid] for rid in order]


def _ends_mid_line(descriptor: int) -> bool:
    """Whether the journal's last byte is not a newline.

    A crash can leave a torn final line, and :func:`read_rows` skips an
    unparsable line *because* that is expected. Appending straight onto the
    fragment welds the two into one unparsable line, so the receipt the sender
    was just told was accepted has no readable row and a retry of its key is
    appended again as a fresh event. So the append opens a line of its own first
    when it has to.

    Read from the descriptor the append goes to, under the journal's lock.
    ``O_APPEND`` moves every write to the end whatever the offset is, so seeking
    here to look cannot redirect the row that follows.
    """
    size = os.fstat(descriptor).st_size
    if size == 0:
        return False
    os.lseek(descriptor, size - 1, os.SEEK_SET)
    return os.read(descriptor, 1) != b"\n"


def _write_all(descriptor: int, data: bytes) -> None:
    """Write every byte of ``data`` to an ``O_APPEND`` descriptor, or raise.

    ``os.write`` is allowed to write short and says how much it wrote; ignoring
    that returns a durable-looking receipt for a row that is only half on disk,
    and a half row is a row :func:`read_rows` skips. The journal's advisory lock
    makes each write its own atomic append, so looping here cannot interleave
    with another writer's row.
    """
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError(
                errno.ENOSPC, "the webhook receipt journal write made no progress"
            )
        remaining = remaining[written:]


def _append(journal: Path, row: dict[str, Any]) -> None:
    """Append one receipt row, fsync it, and trim the journal if it grew.

    One ``O_APPEND`` write of a whole line followed by ``fsync`` on the
    descriptor: the writer holds the journal's advisory lock, so no
    read-merge-write of the journal itself can race this. A crash can lose this
    row and nothing earlier.

    Two things about that write are load-bearing, because a partial row is worse
    than no row at all — the sender has already been told the event was accepted.
    A torn tail from an earlier crash is closed off with a newline first (see
    :func:`_ends_mid_line`), and a short write is completed rather than accepted
    (see :func:`_write_all`).

    The trim runs here, while that lock is still held — releasing it first let a
    concurrent append land between the trim's read and its ``os.replace``, and
    the stale snapshot then deleted the newer receipt.
    """
    payload = {**row, "v": RECEIPT_VERSION}
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    try:
        journal.parent.mkdir(parents=True, exist_ok=True)
        # O_RDWR, not O_WRONLY: the same descriptor has to answer whether the
        # file's last byte is a newline.
        descriptor = open_private(
            journal, os.O_RDWR | os.O_APPEND, follow_symlinks=False
        )
    except OSError as exc:
        raise ReceiptUnavailable(
            f"the webhook receipt journal cannot be opened for append: "
            f"{exc.strerror or exc}"
        ) from None
    try:
        data = line.encode("utf-8")
        if _ends_mid_line(descriptor):
            data = b"\n" + data
        _write_all(descriptor, data)
        os.fsync(descriptor)
    except OSError as exc:
        raise ReceiptUnavailable(
            f"the webhook receipt journal could not be appended to: "
            f"{exc.strerror or exc}"
        ) from None
    finally:
        os.close(descriptor)
    _trim_if_large(journal)


def _trim_if_large(journal: Path) -> None:
    """Keep the journal bounded, never dropping a receipt that is still open.

    Runs under the journal's advisory lock, so a concurrent append cannot land
    between this read and the ``os.replace``. Best-effort: a trim that fails is
    logged and skipped, and the next append tries again.

    Retention is by filtering, not by cutting a prefix off the tail. Every line
    whose id is unsettled survives wherever it sits, and the *newest* settled
    lines fill the budget from the tail backwards; original order is kept. A
    prefix cut fails twice over, and both failures are real: it met the oldest
    line first, so a single unsettled ``accepted`` row at the head froze the byte
    trim for good (every later append then re-read and rewrote the whole file,
    which makes the "bounded in bytes" claim false and each append O(n)), and it
    dropped everything past the last :data:`RECEIPTS_KEEP_ROWS` lines before the
    unsettled check, so an unsettled row older than the cut was deleted — and a
    retry of its key then became a second event.

    The byte budget is shared and spent on unsettled rows first: an unsettled row
    is kept whatever it costs, because it is the only evidence that an event is
    in flight, so it is settled history — never the open receipt — that gives way
    when the two cannot both fit. That is what keeps the cap a bound on the file
    rather than a hope, and :data:`MAX_PENDING_RECEIPTS` per trigger is what
    keeps the number of such rows finite.
    """
    try:
        if not journal.exists() or journal.stat().st_size < RECEIPTS_MAX_BYTES:
            return
        raw = journal.read_text(encoding="utf-8", errors="replace")
        lines = [line for line in raw.split("\n") if line.strip()]
        sizes = [len(line.encode("utf-8")) + 1 for line in lines]
        ids = [str((_safe_row(line) or {}).get("id") or "") for line in lines]
        # An id's effective status is its *last* row anywhere in the journal,
        # not just among the lines this trim might drop: a `launched` row with
        # no terminal row can sit before any cut, so computing unsettled ids
        # from a candidate tail alone would find nothing and the trim would drop
        # it.
        unsettled = {
            str(row.get("id") or "")
            for row in read_rows(journal)
            if str(row.get("status") or "") not in _TERMINAL_RECEIPT_STATES
        }
        budget = RECEIPTS_MAX_BYTES - sum(
            size for size, rid in zip(sizes, ids, strict=True) if rid in unsettled
        )
        retained: set[int] = set()
        settled_rows = 0
        settled_bytes = 0
        for index in range(len(lines) - 1, -1, -1):
            if ids[index] in unsettled:
                retained.add(index)
                continue
            too_many = settled_rows >= RECEIPTS_KEEP_ROWS
            too_wide = settled_bytes + sizes[index] > budget
            if too_many or too_wide:
                continue
            retained.add(index)
            settled_rows += 1
            settled_bytes += sizes[index]
        kept = [line for index, line in enumerate(lines) if index in retained]
        payload = "\n".join(kept).rstrip() + "\n"
        descriptor, temp_name = mkstemp_private(
            dir=journal.parent, prefix=f".{journal.name}.", suffix=".trim"
        )
        temporary = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(temporary, journal)
        finally:
            # A no-op once the rename landed, a cleanup when it did not.
            temporary.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — trimming is best-effort
        logger.debug("webhook receipts: trim failed", exc_info=True)


def _event_text(body: bytes) -> str:
    """The one field a sender may set: the event's text.

    ``input_policy`` is ``event_text`` and this is that policy, literally — a
    JSON object whose only key is ``text``. Refusing the rest rather than
    ignoring it is what makes "a sender cannot choose the workspace, project,
    model or permission" a property of the shape instead of a blocklist of
    field names that would have to be kept current: there is no key here that
    means anything but the event.
    """
    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise WebhookReceiverError(
            "the request body must be a JSON object", code=INVALID_EVENT
        ) from None
    if not isinstance(document, dict):
        raise WebhookReceiverError(
            "the request body must be a JSON object", code=INVALID_EVENT
        )
    unexpected = sorted(key for key in document if key != "text")
    if unexpected:
        raise WebhookReceiverError(
            "the request body carries only \"text\"; "
            f"unexpected keys: {', '.join(unexpected)}",
            code=INVALID_EVENT,
        )
    text = document.get("text")
    if not isinstance(text, str):
        raise WebhookReceiverError(
            'the request body must carry "text" as a string', code=INVALID_EVENT
        )
    if not text.strip():
        raise WebhookReceiverError('"text" must not be empty', code=INVALID_EVENT)
    if len(text) > MAX_EVENT_TEXT_CHARS:
        raise WebhookReceiverError(
            f'"text" must be at most {MAX_EVENT_TEXT_CHARS} characters',
            code=INVALID_EVENT,
        )
    return text


def _validated_idempotency_key(value: Any) -> str:
    """A sender's idempotency key, or refuse.

    Bounded and control-character-free: it is stored in the journal and used as
    receipt identity, so it has to be a token rather than a payload. Surrounding
    whitespace is stripped, which is what HTTP itself does to a header value.
    """
    if not isinstance(value, str):
        raise WebhookReceiverError(
            "an Idempotency-Key header is required", code=INVALID_IDEMPOTENCY_KEY
        )
    key = value.strip()
    if not key:
        raise WebhookReceiverError(
            "an Idempotency-Key header is required", code=INVALID_IDEMPOTENCY_KEY
        )
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise WebhookReceiverError(
            f"an Idempotency-Key may be at most {MAX_IDEMPOTENCY_KEY_LENGTH} "
            "characters",
            code=INVALID_IDEMPOTENCY_KEY,
        )
    if any(character < " " or character == "\x7f" for character in key):
        raise WebhookReceiverError(
            "an Idempotency-Key may not contain control characters",
            code=INVALID_IDEMPOTENCY_KEY,
        )
    return key


def _receipt_id(basis: str) -> str:
    """A stable receipt id for one ``(trigger, idempotency key)`` attempt.

    Derived from the pair rather than minted, so a retry that arrives while the
    first attempt is still in the journal can only collapse onto the same id.
    A *later* attempt of the same pair (the dedupe window has passed, so the
    sender is free to reuse the key) takes the next generation — see
    :meth:`WebhookReceiver._next_receipt_id` — so an id is never reused for a
    different event.
    """
    return f"wbrcpt_{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:20]}"


class _RateWindow:
    """A per-trigger sliding window of attempts, in this process.

    In-process on purpose, and the same precedent as the login window in
    ``ciao/web/routes_auth.py``: this is a second line against a runaway or
    hostile sender, not a quota system and not a security boundary — the
    trigger's secret is that. A restart resets it, which is correct for what it
    is: it bounds a burst, and a burst that spans a restart is bounded by the
    bearer check instead.
    """

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._entries: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Whether this attempt is inside the window; records it either way."""
        now = time.monotonic()
        with self._lock:
            attempts = [
                stamp
                for stamp in self._entries.get(key, ())
                if stamp > now - self._window
            ]
            if len(attempts) >= self._limit:
                self._entries[key] = attempts
                return False
            attempts.append(now)
            self._entries[key] = attempts
            return True

    def reset(self) -> None:
        """Forget every window. For tests, and for a deliberate restart."""
        with self._lock:
            self._entries.clear()


#: The window the receiver uses unless a caller injects its own. Module-level
#: so it survives the per-request receiver construction the route does (the
#: same reason the store is cheap and cross-process-safe instead of cached).
_RATE_WINDOW = _RateWindow(
    limit=RATE_LIMIT_PER_MINUTE, window_seconds=RATE_WINDOW_SECONDS
)


def reset_rate_limits() -> None:
    """Clear the shared per-trigger rate window.

    For tests and for a deliberate restart of the engine. Not an operator
    control: there is nothing here worth configuring, and a Settings switch for
    an in-process burst counter would be a promise nobody can keep across a
    restart.
    """
    _RATE_WINDOW.reset()


@dataclass(frozen=True, slots=True)
class WebhookReceipt:
    """What one accepted webhook event left behind.

    Deliberately free of any credential. It names the trigger, not the secret
    that authorized it, and holds only the sender's own event text plus the
    digest of the exact bytes that carried it — which is what a later reader
    needs to answer "was this the same request?" without keeping the body.
    """

    id: str
    trigger_id: str
    trigger_name: str
    workspace: str
    #: ``None`` means the workspace's General project, as in the store.
    project_id: str | None
    idempotency_key: str
    status: str
    #: SHA-256 of the request body as it arrived, hex.
    body_digest: str
    #: The sender's event text, bounded by :data:`MAX_EVENT_TEXT_CHARS`.
    event_text: str
    created_at: str
    updated_at: str
    #: Why a receipt failed or was interrupted. Empty otherwise.
    detail: str = ""

    def to_row(self) -> dict[str, Any]:
        """The journal row for this receipt.

        Flat, and shaped like the store's records rather than like
        ``dataclasses.asdict``: a journal row is read by humans during an
        incident, and ``None`` stays ``null`` instead of becoming a string that
        looks like a project id.
        """
        return {
            "id": self.id,
            "trigger_id": self.trigger_id,
            "trigger_name": self.trigger_name,
            "workspace": self.workspace,
            "project_id": self.project_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "body_digest": self.body_digest,
            "event_text": self.event_text,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "detail": self.detail,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> WebhookReceipt:
        """Parse a journal row, or raise ``invalid_receipt``.

        Strict for the same reason the store is: a row this code cannot read is
        a row it must not reason about. Failing closed keeps a hand-edited or
        half-written journal from being treated as "no receipt", which would let
        a second launch through for an event that already has one.
        """

        def text_field(name: str) -> str:
            value = row.get(name)
            if not isinstance(value, str):
                raise WebhookReceiverError(
                    f"receipt journal row is missing a string {name}",
                    code=INVALID_RECEIPT,
                )
            return value

        project_id = row.get("project_id")
        if project_id is not None and not isinstance(project_id, str):
            raise WebhookReceiverError(
                "receipt journal row has a project_id that is not a string or null",
                code=INVALID_RECEIPT,
            )
        status = text_field("status")
        if status not in RECEIPT_STATUSES:
            raise WebhookReceiverError(
                f"receipt journal row has an unknown status {status!r}",
                code=INVALID_RECEIPT,
            )
        for name in ("created_at", "updated_at"):
            text_field(name)
        return cls(
            id=text_field("id"),
            trigger_id=text_field("trigger_id"),
            trigger_name=text_field("trigger_name"),
            workspace=text_field("workspace"),
            project_id=project_id,
            idempotency_key=text_field("idempotency_key"),
            status=status,
            body_digest=text_field("body_digest"),
            event_text=text_field("event_text"),
            created_at=text_field("created_at"),
            updated_at=text_field("updated_at"),
            detail=text_field("detail"),
        )


class WebhookReceiver:
    """The ingress receiver: one durable receipt per accepted event.

    Store-adjacent and transport-free, so its whole contract is testable
    without Starlette and without a socket. It takes an already-authenticated
    :class:`WebhookTrigger` — the bearer check is
    :meth:`WebhookStore.authenticate`'s, and nothing here re-implements it — and
    answers with a receipt.

    Every mutation is one critical section: the journal is read and appended to
    under the same lock, so a dedupe decision and the row that depends on it
    cannot be interleaved with another process's attempt at the same key. The
    lock is a lock, not a sandbox: it coordinates the clients that take it and
    does not stop a local process that writes the journal without taking it.
    """

    def __init__(
        self,
        store_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        window: _RateWindow | None = None,
    ) -> None:
        # The journal sits beside the trigger store rather than at a path of its
        # own, and ``store_path`` is the store's path rather than the runtime
        # directory: a receipt describes what a trigger caused, and the one place
        # that says where the store lives is the store itself.
        self._journal = Path(store_path).parent / RECEIPTS_NAME
        self._lock_path = self._journal.with_name(f"{self._journal.name}.lock")
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._lock_key = f"webhook-receipts:{os.fspath(self._journal)}"
        self._window = window if window is not None else _RATE_WINDOW

    @property
    def journal(self) -> Path:
        """The receipt journal this receiver appends to."""
        return self._journal

    # -- paths and locking ------------------------------------------------

    def _require_safe_paths(self) -> None:
        """Refuse a journal or lock path this receiver must not write through.

        The same three refusals the store makes — a ``.``/``..`` component, a
        file that is itself a link, a path naming no file — for the same
        reasons. Only the final component is checked for being a link; a link in
        a parent directory is a layout choice, not an attack on this journal.
        """
        for candidate, role in (
            (self._journal, "webhook receipt journal"),
            (self._lock_path, "webhook receipt journal lock"),
        ):
            if not candidate.name:
                raise ReceiptUnavailable(f"the {role} path names no file")
            if any(part in _UNSAFE_COMPONENTS for part in candidate.parts):
                raise ReceiptUnavailable(
                    f"the {role} path {candidate} contains a '.' or '..' component"
                )
            if is_link(candidate):
                raise ReceiptUnavailable(
                    f"refusing to use the {role} at {candidate}: it is a link"
                )

    @contextmanager
    def _journal_locked(self) -> Iterator[None]:
        """Hold both locks a receipt write needs, and nothing else.

        ``keyed_lock`` serializes writers in this process; the advisory sibling
        lock serializes them against another process — which is the case that
        would otherwise let two engines (or an engine and a CLI) each dedupe
        against a stale read and both accept the same event.
        """
        with keyed_lock(self._lock_key):
            self._require_safe_paths()
            try:
                self._journal.parent.mkdir(parents=True, exist_ok=True)
                descriptor = open_private(
                    self._lock_path, os.O_RDWR | os.O_CREAT, follow_symlinks=False
                )
            except OSError as exc:
                raise ReceiptUnavailable(
                    f"the webhook receipt journal lock cannot be opened: "
                    f"{exc.strerror or exc}"
                ) from None
            try:
                lock_exclusive(descriptor)
                try:
                    yield
                finally:
                    unlock(descriptor)
            finally:
                os.close(descriptor)

    # -- the receipt lifecycle --------------------------------------------

    def receive(
        self,
        trigger: WebhookTrigger,
        *,
        idempotency_key: str,
        body: bytes,
    ) -> WebhookReceipt:
        """Record one accepted event and return its receipt.

        The whole decision is one critical section: rate window, dedupe, pending
        bound, append. Ordered so that the cheap refusals happen before any
        write, and so that a *retry* is answered before the pending bound is
        consulted — a sender retrying an event it already had accepted must get
        that receipt back, not a "too many pending" refusal for an event nobody
        is waiting on twice.

        Raises rather than returning a refusal: nothing is recorded on any
        refusal, so the sender can retry the same key and get the same answer.
        """
        key = _validated_idempotency_key(idempotency_key)
        if len(body) > MAX_BODY_BYTES:
            raise PayloadTooLarge(len(body))
        text = _event_text(body)
        # The window is per trigger and counts *attempts*, not accepts: a
        # sender retrying in a tight loop is the case the window exists for, and
        # a retry that gets a receipt back is cheap anyway.
        if not self._window.allow(trigger.trigger_id):
            raise RateLimited(trigger.trigger_id)
        digest = _digest(body)
        with self._journal_locked():
            rows = read_rows(self._journal)
            existing = _find_receipt(rows, trigger.trigger_id, key, now=self._moment())
            if existing is not None:
                if existing.body_digest != digest:
                    raise IdempotencyConflict(existing.id)
                return existing
            pending = _pending_for(rows, trigger.trigger_id)
            if pending >= MAX_PENDING_RECEIPTS:
                raise TooManyPending(trigger.trigger_id, pending)
            stamp = self._stamp()
            receipt = WebhookReceipt(
                id=self._next_receipt_id(rows, trigger.trigger_id, key),
                trigger_id=trigger.trigger_id,
                trigger_name=trigger.name,
                workspace=trigger.workspace,
                project_id=trigger.project_id,
                idempotency_key=key,
                status=ACCEPTED,
                body_digest=digest,
                event_text=text,
                created_at=stamp,
                updated_at=stamp,
            )
            _append(self._journal, receipt.to_row())
        return receipt

    def begin_launch(self, receipt_id: str) -> WebhookReceipt:
        """Record the launch allocation for an accepted receipt.

        Called by the dispatcher (A4) *before* the model turn, and durable
        before it. This is the ambiguous window: once the allocation is on disk
        a crash leaves no way to know whether the turn ran, so
        :meth:`recover_interrupted` records ``interrupted`` rather than letting
        a retry start a second turn.

        Idempotent: an allocation that is already recorded returns the receipt
        as it stands, so a caller that crashed and retried does not fail on its
        own earlier write.
        """
        with self._journal_locked():
            receipt = self._require_receipt(receipt_id)
            if receipt.status != ACCEPTED:
                if receipt.status == LAUNCHED:
                    return receipt
                raise WebhookReceiverError(
                    f"receipt {receipt_id} is {receipt.status} and cannot be "
                    "launched again",
                    code=INVALID_RECEIPT,
                )
            launched = replace(receipt, status=LAUNCHED, updated_at=self._stamp())
            _append(self._journal, launched.to_row())
        return launched

    def settle_failed(self, receipt_id: str, *, detail: str) -> WebhookReceipt:
        """Record that a launch was attempted and did not complete.

        Terminal, and deliberately not a retry: the sender's next delivery is a
        new attempt with a new key, or the same key once the dedupe window has
        passed. A failed launch is an event that happened and failed, and
        re-running it is a decision for the operator.
        """
        with self._journal_locked():
            receipt = self._require_receipt(receipt_id)
            if receipt.status in _TERMINAL_RECEIPT_STATES:
                return receipt
            failed = replace(
                receipt,
                status=FAILED,
                updated_at=self._stamp(),
                detail=detail[:MAX_DETAIL_CHARS],
            )
            _append(self._journal, failed.to_row())
        return failed

    def recover_interrupted(self) -> list[WebhookReceipt]:
        """Settle every receipt left in the ambiguous window, as ``interrupted``.

        A ``launched`` receipt with no terminal row means a process died between
        the allocation and the outcome. Nothing can tell whether the model turn
        ran, so the honest record is ``interrupted`` with a detail saying so:
        it needs a person, and it must never be replayed automatically. Returns
        the receipts it settled.
        """
        stranded: list[WebhookReceipt] = []
        with self._journal_locked():
            for row in read_rows(self._journal):
                receipt = WebhookReceipt.from_row(row)
                if receipt.status != LAUNCHED:
                    continue
                settled = replace(
                    receipt,
                    status=INTERRUPTED,
                    updated_at=self._stamp(),
                    detail=(
                        "the launch allocation was recorded but the outcome was "
                        "not; review before retrying"
                    ),
                )
                _append(self._journal, settled.to_row())
                stranded.append(settled)
        return stranded

    # -- reading ----------------------------------------------------------

    def receipts_for(self, trigger_id: str) -> list[WebhookReceipt]:
        """Every receipt for one trigger, oldest first."""
        return [
            receipt
            for row in read_rows(self._journal)
            if str(row.get("trigger_id") or "") == trigger_id
            for receipt in (WebhookReceipt.from_row(row),)
        ]

    def get(self, receipt_id: str) -> WebhookReceipt:
        """One receipt by id, or raise ``invalid_receipt``."""
        return self._require_receipt(receipt_id)

    # -- internals --------------------------------------------------------

    def _require_receipt(self, receipt_id: str) -> WebhookReceipt:
        """The effective receipt for ``receipt_id``, or raise."""
        for row in read_rows(self._journal):
            if str(row.get("id") or "") == receipt_id:
                return WebhookReceipt.from_row(row)
        raise WebhookReceiverError(
            f"no webhook receipt {receipt_id} is recorded", code=INVALID_RECEIPT
        )

    def _next_receipt_id(
        self, rows: list[dict[str, Any]], trigger_id: str, key: str
    ) -> str:
        """The id for a *new* attempt of ``(trigger_id, key)``.

        The first attempt of a pair gets the content-derived id, so a retry that
        races the dedupe window collapses onto it. A later attempt — the pair has
        aged past :data:`DEDUPE_RETENTION_DAYS`, so the sender is free to reuse
        the key — takes the next generation, because reusing the id would fold
        the new attempt onto the old receipt and erase how the old one settled.
        """
        base = _receipt_id(f"{trigger_id}|{key}")
        used = {
            str(row.get("id") or "")
            for row in rows
            if str(row.get("trigger_id") or "") == trigger_id
            and str(row.get("idempotency_key") or "") == key
        }
        candidate = base
        generation = 2
        while candidate in used:
            candidate = f"{base}.{generation}"
            generation += 1
        return candidate

    def _moment(self) -> datetime:
        """Now, from the injected clock, as an aware UTC datetime.

        One place that normalizes the clock, because :meth:`_stamp` and the
        dedupe window both have to agree about what time it is: a naive clock
        reading means UTC here rather than an exception, since a caller that
        injected one meant it.
        """
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC)

    def _stamp(self) -> str:
        """The ISO-8601 UTC stamp a receipt carries, from the injected clock."""
        return self._moment().isoformat(timespec="seconds")


def _find_receipt(
    rows: list[dict[str, Any]], trigger_id: str, key: str, *, now: datetime
) -> WebhookReceipt | None:
    """The live receipt for ``(trigger_id, key)``, or None.

    "Live" is the dedupe window: a receipt older than
    :data:`DEDUPE_RETENTION_DAYS` is not a retry target, it is history. Latest
    attempt wins, since a key may have more than one after the window passed.

    ``now`` is the receiver's own clock, so the window is measured against the
    same time the stamps were written with rather than against a second,
    independent reading of the wall clock.
    """
    live: WebhookReceipt | None = None
    for row in rows:
        if (
            str(row.get("trigger_id") or "") != trigger_id
            or str(row.get("idempotency_key") or "") != key
        ):
            continue
        receipt = WebhookReceipt.from_row(row)
        if _within_retention(receipt.created_at, now=now):
            live = receipt
    return live


def _within_retention(created_at: str, *, now: datetime) -> bool:
    """Whether a receipt's ``created_at`` is inside the dedupe window at ``now``.

    An unparsable stamp is treated as outside the window: the conservative
    direction here is a second attempt (which the pending bound and the sender's
    own idempotency discipline still bound) rather than a sender locked out of
    its own key by a clock problem.
    """
    try:
        moment = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment >= now - timedelta(days=DEDUPE_RETENTION_DAYS)


def _pending_for(rows: list[dict[str, Any]], trigger_id: str) -> int:
    """How many receipts for one trigger are still open.

    Counts ``accepted`` and ``launched``: an event that was recorded but never
    launched is still occupying the trigger, and an accepted one that a crash
    left unsettled is exactly what must not be compounded by another arrival.
    """
    return sum(
        1
        for row in rows
        if str(row.get("trigger_id") or "") == trigger_id
        and str(row.get("status") or "") not in _TERMINAL_RECEIPT_STATES
    )
