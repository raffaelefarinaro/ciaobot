"""Private batch store for conversation-import runs (#1032, C6 child of #975).

An import can discover and preview conversations
(:mod:`ciao.import_discover`), but until this module there was nowhere to
remember an in-progress import: which conversations were selected, what was
read, progress, dedupe across attempts, and per-fact provenance. One batch is
one import run over a selection; it holds the selection, per-source digests,
progress, cancellation, and per-fact provenance. No extraction lives here —
C7 consumes this store — and no model is ever called.

**Private runtime state, never the vault's public ledger.** The file is
``<runtime>/import/import-batches.json`` (see :func:`engine_store_path`, the
one place that path is built), written owner-private through
:mod:`ciao.os_support.private` (``mkstemp_private``, ``make_private_dir``),
replaced atomically, and mutated under ``keyed_lock`` plus an owner-private
advisory sibling lock, exactly like ``WebhookStore._mutation``. A corrupt
file fails closed — it raises, never silently resets — because a reset store
would forget what was already imported and re-import it.

**Dedupe keys** (from ``docs/CONVERSATION_IMPORT_FEASIBILITY.md``):

* ``(provider, source_id, anchor)`` for re-scans is per-adapter and stays
  there;
* ``(destination workspace, one-line normalized fact text)`` reuses
  ``append_proposals`` exact-text dedupe and stays C7's to call;
* **Ciaobot session identity** is enforced here at creation:
  ``create`` takes the caller's ``known_own_ids`` (Ciaobot's own
  ``(provider, session_id)`` pairs from
  :func:`ciao.import_decouple.ciaobot_own_session_ids`) and refuses a
  Ciaobot-own session, as well as a bare Ciaobot chat id, rather than filing
  a batch for it. Content-level classification (the marker in the session's
  own first turn) is C7's, which refuses again before any turn.
* the store owns the batch-level
  ``(provider, source_id, content_digest, destination, extraction_revision)``
  key: re-selecting a conversation a live batch already covers is a
  ``conflict``, and only :meth:`ImportStore.forget` (or expiry) releases it.

**Progress and cancel**: a batch is
``queued → running → done | failed | cancelled | partial``. Cancellation stops
future extraction, retains already-recorded progress and provenance, and
cannot unsend provider input. One batch at a time per workspace: creating
while one is open is a ``conflict``.

**Retention**: unaccepted source snapshots expire after
:data:`IMPORT_SNAPSHOT_RETENTION_DAYS` (30 days, a named constant);
:meth:`ImportStore.prune_expired` drops terminal batches past that window but
keeps their accepted fact evidence in the store's private retained-provenance
list. The engine runs that sweep once per boot
(:func:`sweep_import_batches`, called from ``ciao/main.py`` next to the other
startup sweeps — plenty for a 30-day window). Removing an import
(:meth:`ImportStore.forget`) never deletes accepted memories — it only drops
the batch record; the review queue and the vault are untouched, and that is
the existing review/undo path.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

from ciao.async_reads import keyed_lock
from ciao.import_decouple import (
    ProvenanceNotExternal,
    assert_external_provenance,
    canonical_provider,
    is_ciaobot_chat_id,
)
from ciao.import_discover import BATCH_CAP
from ciao.import_sources import KNOWN_PROVIDERS
from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.links import is_link
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import make_private_dir, mkstemp_private, open_private
from ciao.workspaces import WORKSPACE_NAME_RE

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: The extraction contract version this store's batches were filed under. Part
#: of the batch-level dedupe key: a future extractor revision re-imports
#: rather than colliding with this one's batches.
EXTRACTION_REVISION = 1

#: How long an unaccepted source snapshot survives. Terminal batches older
#: than this (by ``updated_at``) are dropped by :meth:`ImportStore.prune_expired`,
#: which the engine's retention sweep calls once per boot; accepted fact
#: evidence is retained, not dropped. Thirty days is disclosed, not derived:
#: long enough that a paused import is still there, short enough that
#: abandoned selections do not accumulate private snapshots next to the
#: runtime forever.
IMPORT_SNAPSHOT_RETENTION_DAYS = 30

QUEUED: BatchStatus = "queued"
RUNNING: BatchStatus = "running"
DONE: BatchStatus = "done"
FAILED: BatchStatus = "failed"
CANCELLED: BatchStatus = "cancelled"
PARTIAL: BatchStatus = "partial"

BatchStatus = Literal["queued", "running", "done", "failed", "cancelled", "partial"]

#: A batch still holding a selection somebody may act on.
OPEN_STATUSES: tuple[str, ...] = (QUEUED, RUNNING)

#: Every status a batch can rest in. Cancellation is terminal: a cancelled
#: batch never resumes, and its sources stay covered until the batch is
#: forgotten or expires past retention.
TERMINAL_STATUSES: tuple[str, ...] = (DONE, FAILED, CANCELLED, PARTIAL)

BATCH_STATUSES: tuple[str, ...] = (QUEUED, RUNNING, DONE, FAILED, CANCELLED, PARTIAL)

#: The statuses :meth:`ImportStore.finish` accepts. ``cancelled`` is not one:
#: cancellation goes through :meth:`ImportStore.cancel` so it stays explicit
#: and idempotent rather than a finish mode.
FINISH_STATUSES: tuple[str, ...] = (DONE, FAILED, PARTIAL)

SOURCE_PENDING = "pending"
SOURCE_EXTRACTED = "extracted"
SOURCE_SKIPPED = "skipped"
SOURCE_REFUSED = "refused"
SOURCE_STATUSES: tuple[str, ...] = (
    SOURCE_PENDING,
    SOURCE_EXTRACTED,
    SOURCE_SKIPPED,
    SOURCE_REFUSED,
)

# Stable error codes, part of the contract the routes and their tests match
# on, mirroring ciao/webhooks.py rather than inventing a second vocabulary.
INVALID_BATCH = "invalid_batch"
UNSUPPORTED_SCHEMA = "unsupported_schema"
CORRUPT_STORE = "corrupt_store"
NOT_FOUND = "not_found"
CONFLICT = "conflict"
UNSAFE_PATH = "unsafe_path"
ERROR_CODES = (
    INVALID_BATCH,
    UNSUPPORTED_SCHEMA,
    CORRUPT_STORE,
    NOT_FOUND,
    CONFLICT,
    UNSAFE_PATH,
)

# Path components that must never be written through: they make a stored path
# depend on a directory's current contents rather than naming one file.
_UNSAFE_COMPONENTS = frozenset({".", ".."})

_BATCH_FIELDS = (
    "batch_id",
    "workspace",
    "destination",
    "status",
    "extraction_revision",
    "sources",
    "progress",
    "provenance",
    "created_at",
    "updated_at",
    "error",
)
_SOURCE_FIELDS = frozenset({"provider", "source_id", "content_digest", "status"})
_PROGRESS_FIELDS = frozenset(
    {"total_sources", "completed_sources", "proposals_filed", "skipped", "current_source_id"}
)
_PROVENANCE_FIELDS = frozenset(
    {"provider", "source_id", "anchor", "destination", "accepted", "note"}
)

_BATCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class ImportStoreError(Exception):
    """A refusal from the import batch store, carrying a stable ``code``.

    One class with a code rather than a subclass per failure, because a
    caller (a route, C7) matches on the code:

    - ``invalid_batch``: an argument is not a batch this store will store —
      a bad workspace, provider or source, a Ciaobot-own session, an over-cap
      selection, a refused provenance tag, or a transition from the wrong
      state. Nothing was written.
    - ``unsupported_schema``: the file on disk is a schema this code does not
      implement, or a batch filed under a different extraction revision.
      Never migrated, never rewritten; the bytes are left alone.
    - ``corrupt_store``: the file is there and this code cannot read it as a
      schema-1 document. Never reset.
    - ``not_found``: no such batch id in the store.
    - ``conflict``: the write would break the one-batch-per-workspace rule,
      re-cover a conversation a live batch already holds, or move a batch out
      of a terminal state. Nothing was written.
    - ``unsafe_path``: the store path or its lock is a link, contains a
      ``.``/``..`` component, names no file, or cannot be opened. Nothing was
      written.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BatchSource:
    """One selected conversation inside a batch, and what became of it.

    ``content_digest`` is the SHA-256 of the normalized session once C7 has
    read it, and ``""`` until then — the batch-level dedupe key treats an
    unknown digest as matching, so re-selecting an unread conversation is a
    conflict rather than a second batch. ``status`` is one of
    :data:`SOURCE_STATUSES`; it is per-source bookkeeping for C7's resume,
    not the batch lifecycle.
    """

    provider: str
    source_id: str
    content_digest: str = ""
    status: str = SOURCE_PENDING

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source_id": self.source_id,
            "content_digest": self.content_digest,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class BatchProgress:
    """How far a batch got, in numbers a progress surface can render.

    ``total_sources`` is fixed at creation from the selection; the rest move
    through :meth:`ImportStore.record_progress`. ``current_source_id`` names
    the source being extracted when the snapshot was taken, and is ``""``
    when none is.
    """

    total_sources: int = 0
    completed_sources: int = 0
    proposals_filed: int = 0
    skipped: int = 0
    current_source_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "total_sources": self.total_sources,
            "completed_sources": self.completed_sources,
            "proposals_filed": self.proposals_filed,
            "skipped": self.skipped,
            "current_source_id": self.current_source_id,
        }


@dataclass(frozen=True, slots=True)
class FactProvenance:
    """One imported fact's external source, kept after the batch is gone.

    ``provider:source_id:anchor`` names the external conversation and the
    message inside it; ``destination`` is the workspace the fact was filed
    into. ``accepted`` says whether the fact survived review: accepted
    evidence is what :meth:`ImportStore.prune_expired` retains when the batch
    itself expires, so an expired import stays attributable. ``note`` is a
    short human-readable remark (a refusal reason, a filing note), never
    transcript text.
    """

    provider: str
    source_id: str
    anchor: str
    destination: str
    accepted: bool = False
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source_id": self.source_id,
            "anchor": self.anchor,
            "destination": self.destination,
            "accepted": self.accepted,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ImportBatch:
    """One import run over a selection, as every reader outside this module sees it."""

    batch_id: str
    workspace: str
    destination: str
    status: str
    extraction_revision: int = EXTRACTION_REVISION
    sources: tuple[BatchSource, ...] = ()
    progress: BatchProgress = field(default_factory=BatchProgress)
    provenance: tuple[FactProvenance, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "workspace": self.workspace,
            "destination": self.destination,
            "status": self.status,
            "extraction_revision": self.extraction_revision,
            "sources": [source.to_json() for source in self.sources],
            "progress": self.progress.to_json(),
            "provenance": [row.to_json() for row in self.provenance],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


# ── Field validation ───────────────────────────────────────────────────
#
# Every validator takes the error ``code`` it reports under: the same rule
# answers "is this a batch this store will store?" (caller input,
# ``invalid_batch``) and "is this a record this store wrote?" (bytes on disk,
# ``corrupt_store``). Neither coerces a type and neither repairs a value.


def _text(value: Any, *, field_name: str, code: str) -> str:
    """A required string, unaltered."""
    if not isinstance(value, str):
        raise ImportStoreError(
            f"{field_name} must be a string, not {type(value).__name__}", code=code
        )
    return value


def _validated_workspace(value: Any, *, code: str) -> str:
    """A workspace name in the registry's own spelling.

    Shape only. Whether the workspace is registered is the route's check —
    refusing an unregistered name here would make this store own workspace
    lifecycle.
    """
    workspace = _text(value, field_name="workspace", code=code)
    if not WORKSPACE_NAME_RE.fullmatch(workspace):
        raise ImportStoreError(
            "workspace must be a registered-style name (letters, digits, "
            "dashes or underscores, starting alphanumerically, up to 64 characters)",
            code=code,
        )
    return workspace


def _validated_provider(value: Any, *, code: str) -> str:
    """A provider the import contract knows."""
    provider = _text(value, field_name="source provider", code=code)
    if provider not in KNOWN_PROVIDERS:
        raise ImportStoreError(
            f"source provider {provider!r} is not one of {list(KNOWN_PROVIDERS)}",
            code=code,
        )
    return provider


def _validated_source_id(value: Any, *, code: str) -> str:
    """A non-empty session id. Never used as a path by this store."""
    source_id = _text(value, field_name="source_id", code=code).strip()
    if not source_id:
        raise ImportStoreError("each source must name a session id", code=code)
    return source_id


def _validated_digest(value: Any, *, code: str) -> str:
    """A content digest: empty (not read yet) or SHA-256 hex."""
    digest = _text(value, field_name="content_digest", code=code)
    if digest and not _DIGEST_RE.fullmatch(digest):
        raise ImportStoreError(
            "content_digest must be empty or a lowercase SHA-256 hex digest",
            code=code,
        )
    return digest


def _validated_source_status(value: Any, *, code: str) -> str:
    """One of :data:`SOURCE_STATUSES`, unaltered."""
    status = _text(value, field_name="source status", code=code)
    if status not in SOURCE_STATUSES:
        raise ImportStoreError(
            f"source status {status!r} is not one of {list(SOURCE_STATUSES)}",
            code=code,
        )
    return status


def _validated_count(value: Any, *, field_name: str, code: str) -> int:
    """A non-negative real ``int`` (``True`` is refused: it is not a count)."""
    if type(value) is not int or value < 0:
        raise ImportStoreError(
            f"{field_name} must be a non-negative integer", code=code
        )
    return value


def _validated_accepted(value: Any, *, code: str) -> bool:
    """A real boolean. ``0``/``1`` are refused, like the webhook store."""
    if type(value) is not bool:
        raise ImportStoreError(
            f"provenance accepted must be a boolean, not {type(value).__name__}",
            code=code,
        )
    return value


def _stored_instant(value: Any, *, field_name: str, batch_id: str) -> str:
    """A stored UTC timestamp: an ISO-8601 instant that names a time zone."""
    stamp = _text(value, field_name=field_name, code=CORRUPT_STORE)
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        raise ImportStoreError(
            f"import batch {batch_id} has a {field_name} that is not an "
            "ISO-8601 timestamp",
            code=CORRUPT_STORE,
        ) from None
    if moment.tzinfo is None:
        raise ImportStoreError(
            f"import batch {batch_id} has a {field_name} without a UTC offset",
            code=CORRUPT_STORE,
        )
    return stamp


# ── Reading the file ───────────────────────────────────────────────────


def _decode_source(raw: Any, *, batch_id: str) -> BatchSource:
    """Parse one stored source, or raise ``corrupt_store``."""
    if not isinstance(raw, dict) or set(raw) != _SOURCE_FIELDS:
        raise ImportStoreError(
            f"import batch {batch_id} holds a source that is not a "
            "{provider, source_id, content_digest, status} record",
            code=CORRUPT_STORE,
        )
    return BatchSource(
        provider=_validated_provider(raw["provider"], code=CORRUPT_STORE),
        source_id=_validated_source_id(raw["source_id"], code=CORRUPT_STORE),
        content_digest=_validated_digest(raw["content_digest"], code=CORRUPT_STORE),
        status=_validated_source_status(raw["status"], code=CORRUPT_STORE),
    )


def _decode_progress(raw: Any, *, batch_id: str) -> BatchProgress:
    """Parse one stored progress record, or raise ``corrupt_store``."""
    if not isinstance(raw, dict) or set(raw) != _PROGRESS_FIELDS:
        raise ImportStoreError(
            f"import batch {batch_id} holds progress that is not a "
            "{total_sources, completed_sources, proposals_filed, skipped, "
            "current_source_id} record",
            code=CORRUPT_STORE,
        )
    return BatchProgress(
        total_sources=_validated_count(
            raw["total_sources"], field_name="total_sources", code=CORRUPT_STORE
        ),
        completed_sources=_validated_count(
            raw["completed_sources"], field_name="completed_sources", code=CORRUPT_STORE
        ),
        proposals_filed=_validated_count(
            raw["proposals_filed"], field_name="proposals_filed", code=CORRUPT_STORE
        ),
        skipped=_validated_count(
            raw["skipped"], field_name="skipped", code=CORRUPT_STORE
        ),
        current_source_id=_text(
            raw["current_source_id"],
            field_name="current_source_id",
            code=CORRUPT_STORE,
        ),
    )


def _decode_provenance(raw: Any, *, batch_id: str) -> FactProvenance:
    """Parse one stored provenance row, or raise ``corrupt_store``."""
    if not isinstance(raw, dict) or set(raw) != _PROVENANCE_FIELDS:
        raise ImportStoreError(
            f"import batch {batch_id} holds provenance that is not a "
            "{provider, source_id, anchor, destination, accepted, note} record",
            code=CORRUPT_STORE,
        )
    return FactProvenance(
        provider=_validated_provider(raw["provider"], code=CORRUPT_STORE),
        source_id=_validated_source_id(raw["source_id"], code=CORRUPT_STORE),
        anchor=_text(raw["anchor"], field_name="anchor", code=CORRUPT_STORE).strip()
        or _raise_anchor(batch_id),
        destination=_validated_workspace(raw["destination"], code=CORRUPT_STORE),
        accepted=_validated_accepted(raw["accepted"], code=CORRUPT_STORE),
        note=_text(raw["note"], field_name="note", code=CORRUPT_STORE),
    )


def _raise_anchor(batch_id: str) -> NoReturn:
    """The empty-anchor refusal, as an expression for the decoder above."""
    raise ImportStoreError(
        f"import batch {batch_id} holds provenance with an empty anchor",
        code=CORRUPT_STORE,
    )


def _decode_batch(batch_id: Any, raw: Any) -> ImportBatch:
    """Parse one stored batch, or raise ``corrupt_store``/``unsupported_schema``.

    Unknown keys are a refusal rather than something to drop: a batch written
    by a newer engine may carry a field whose loss would silently change what
    the batch means (a future consent flag, say), so the loud failure is the
    safe one.
    """
    if not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id):
        raise ImportStoreError(
            "the import batch store holds a batch under an id this store "
            "did not mint",
            code=CORRUPT_STORE,
        )
    if not isinstance(raw, dict):
        raise ImportStoreError(
            f"import batch {batch_id} is not a record object", code=CORRUPT_STORE
        )
    missing = [name for name in _BATCH_FIELDS if name not in raw]
    if missing or set(raw) != set(_BATCH_FIELDS):
        unexpected = sorted(set(raw) - set(_BATCH_FIELDS))
        raise ImportStoreError(
            f"import batch {batch_id} does not carry exactly the stored fields "
            f"(missing: {', '.join(missing) or 'none'}; "
            f"unexpected: {', '.join(unexpected) or 'none'})",
            code=CORRUPT_STORE,
        )
    if raw["batch_id"] != batch_id:
        raise ImportStoreError(
            f"import batch {batch_id} is filed under an id its record does "
            "not carry",
            code=CORRUPT_STORE,
        )
    status = _text(raw["status"], field_name="status", code=CORRUPT_STORE)
    if status not in BATCH_STATUSES:
        raise ImportStoreError(
            f"import batch {batch_id} has status {status!r}, not one of "
            f"{list(BATCH_STATUSES)}",
            code=CORRUPT_STORE,
        )
    revision = raw["extraction_revision"]
    if type(revision) is not int:
        raise ImportStoreError(
            f"import batch {batch_id} has an extraction_revision that is not "
            "an integer",
            code=CORRUPT_STORE,
        )
    if revision != EXTRACTION_REVISION:
        raise ImportStoreError(
            f"import batch {batch_id} was filed under extraction revision "
            f"{revision}; this engine reads revision {EXTRACTION_REVISION} "
            "and will not rewrite it",
            code=UNSUPPORTED_SCHEMA,
        )
    raw_sources = raw["sources"]
    if not isinstance(raw_sources, list):
        raise ImportStoreError(
            f"import batch {batch_id} has sources that are not a list",
            code=CORRUPT_STORE,
        )
    raw_provenance = raw["provenance"]
    if not isinstance(raw_provenance, list):
        raise ImportStoreError(
            f"import batch {batch_id} has provenance that is not a list",
            code=CORRUPT_STORE,
        )
    progress = _decode_progress(raw["progress"], batch_id=batch_id)
    sources = tuple(
        _decode_source(item, batch_id=batch_id) for item in raw_sources
    )
    if progress.total_sources != len(sources):
        raise ImportStoreError(
            f"import batch {batch_id} counts {progress.total_sources} sources "
            f"but holds {len(sources)}",
            code=CORRUPT_STORE,
        )
    if progress.completed_sources > progress.total_sources:
        raise ImportStoreError(
            f"import batch {batch_id} completed more sources than it holds",
            code=CORRUPT_STORE,
        )
    return ImportBatch(
        batch_id=batch_id,
        workspace=_validated_workspace(raw["workspace"], code=CORRUPT_STORE),
        destination=_validated_workspace(raw["destination"], code=CORRUPT_STORE),
        status=status,
        extraction_revision=revision,
        sources=sources,
        progress=progress,
        provenance=tuple(
            _decode_provenance(item, batch_id=batch_id) for item in raw_provenance
        ),
        created_at=_stored_instant(
            raw["created_at"], field_name="created_at", batch_id=batch_id
        ),
        updated_at=_stored_instant(
            raw["updated_at"], field_name="updated_at", batch_id=batch_id
        ),
        error=_text(raw["error"], field_name="error", code=CORRUPT_STORE),
    )


def _decode(raw: str, *, path: Path) -> tuple[dict[str, ImportBatch], list[FactProvenance]]:
    """Parse a whole store document, or raise.

    Returns ``(batches, retained_provenance)``. A refusal names the file, never
    what it held.
    """
    try:
        document: Any = json.loads(raw)
    except (ValueError, RecursionError):
        raise ImportStoreError(
            f"the import batch store at {path.name} is not valid JSON",
            code=CORRUPT_STORE,
        ) from None
    if not isinstance(document, dict):
        raise ImportStoreError(
            f"the import batch store at {path.name} is not a JSON object",
            code=CORRUPT_STORE,
        )
    if type(document.get("schema")) is not int:
        raise ImportStoreError(
            f"the import batch store at {path.name} has no integer schema version",
            code=UNSUPPORTED_SCHEMA,
        )
    if document["schema"] != SCHEMA_VERSION:
        raise ImportStoreError(
            f"the import batch store at {path.name} is schema "
            f"{document['schema']}; this engine reads schema {SCHEMA_VERSION} "
            "and will not rewrite it",
            code=UNSUPPORTED_SCHEMA,
        )
    entries = document.get("batches")
    if not isinstance(entries, dict):
        raise ImportStoreError(
            f"the import batch store at {path.name} has no batches object",
            code=CORRUPT_STORE,
        )
    batches = {
        batch_id: _decode_batch(batch_id, entry)
        for batch_id, entry in entries.items()
    }
    retained_raw = document.get("retained_provenance", [])
    if not isinstance(retained_raw, list):
        raise ImportStoreError(
            f"the import batch store at {path.name} has retained provenance "
            "that is not a list",
            code=CORRUPT_STORE,
        )
    retained = [
        _decode_provenance(item, batch_id="retained") for item in retained_raw
    ]
    return batches, retained


# ── The store ──────────────────────────────────────────────────────────


class ImportStore:
    """One workspace-scoped import run after another, privately and durably.

    The constructor takes the path explicitly and reads no operator state, so
    this class is inert until a later child decides where the engine's file
    lives (see :func:`engine_store_path`) and instantiates it.

    Reads (:meth:`get`, :meth:`list_for_workspace`, :meth:`retained_provenance`)
    take no lock: the file is replaced atomically, so a lock-free reader sees
    one whole document and is never made to queue behind a writer's
    read-modify-write.
    """

    def __init__(
        self, path: Path, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_name(f"{self._path.name}.lock")
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._lock_key = f"import-batches:{os.fspath(self._path)}"

    @property
    def path(self) -> Path:
        """The file this store reads and writes."""
        return self._path

    # -- paths and locking ------------------------------------------------

    def _require_safe_paths(self) -> None:
        """Refuse a store or lock path this store must not write through.

        A ``.``/``..`` component, a store or lock file that is itself a link,
        or a path that names no file at all. Only the final component is
        checked for being a link: parent directories are ordinary on any
        platform a person installs on. The link refusal is duplicated at the
        open (``O_NOFOLLOW`` on POSIX, ``FILE_FLAG_OPEN_REPARSE_POINT`` on
        Windows), so there is no window between this check and the open.
        """
        for candidate, role in (
            (self._path, "import batch store"),
            (self._lock_path, "import batch store lock"),
        ):
            if not candidate.name:
                raise ImportStoreError(
                    f"the {role} path {candidate} names no file", code=UNSAFE_PATH
                )
            if any(part in _UNSAFE_COMPONENTS for part in candidate.parts):
                raise ImportStoreError(
                    f"the {role} path {candidate} contains a '.' or '..' component",
                    code=UNSAFE_PATH,
                )
            if is_link(candidate):
                raise ImportStoreError(
                    f"refusing to use the {role} at {candidate}: it is a link",
                    code=UNSAFE_PATH,
                )

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        """Hold both locks a mutation needs, and nothing else.

        ``keyed_lock`` serializes writers inside this process; the advisory
        sibling lock serializes them across processes and against a second
        ``ImportStore`` object — which is the case that loses a batch, because
        a store object that cached the document would write back its own older
        copy. The two are always taken in this order.
        """
        with keyed_lock(self._lock_key):
            self._require_safe_paths()
            # The store lives in its own owner-private directory, created here
            # (and only here) before the lock file needs it. This is the only
            # state a mutation creates before its write: no store file, no
            # batch, nothing a read would see.
            self._path.parent.mkdir(parents=True, exist_ok=True)
            make_private_dir(self._path.parent)
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
        """Open (creating if needed) the owner-private advisory lock file."""
        try:
            return open_private(
                self._lock_path, os.O_RDWR | os.O_CREAT, 0o600, follow_symlinks=False
            )
        except OSError:
            raise ImportStoreError(
                f"cannot open the import batch store lock at {self._lock_path.name}",
                code=UNSAFE_PATH,
            ) from None

    # -- reading and writing ----------------------------------------------

    def _read(self) -> tuple[dict[str, ImportBatch], list[FactProvenance]]:
        """The whole document, or empty when there is no file yet.

        A missing file is a store that has never been written, and reading it
        creates nothing: a read must not leave a file behind that later reads
        then treat as state.
        """
        self._require_safe_paths()
        if self._path.is_dir():
            raise ImportStoreError(
                f"the import batch store at {self._path.name} is a directory",
                code=UNSAFE_PATH,
            )
        try:
            descriptor = open_fd(self._path, os.O_RDONLY, follow_symlinks=False)
        except FileNotFoundError:
            return {}, []
        except IsADirectoryError:
            raise ImportStoreError(
                f"the import batch store at {self._path.name} is a directory",
                code=UNSAFE_PATH,
            ) from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ImportStoreError(
                    f"refusing to read the import batch store at {self._path.name}: "
                    "it is a link",
                    code=UNSAFE_PATH,
                ) from None
            raise ImportStoreError(
                f"the import batch store at {self._path.name} cannot be read",
                code=CORRUPT_STORE,
            ) from None
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as handle:
                raw = handle.read()
        except (OSError, ValueError):
            raise ImportStoreError(
                f"the import batch store at {self._path.name} is not valid UTF-8",
                code=CORRUPT_STORE,
            ) from None
        return _decode(raw, path=self._path)

    def _write(
        self,
        batches: dict[str, ImportBatch],
        retained: list[FactProvenance],
    ) -> None:
        """Write the whole document atomically, owner-private.

        A unique sibling temp (``mkstemp_private``: 0600 on POSIX, a protected
        DACL on Windows), flushed and fsynced, then one ``replace_file``. A
        reader therefore sees the old document or the new one, never half of
        either. Only *this* call's temp is cleaned up in the ``finally``, so
        another writer's temp is never deleted out from under its rename.

        The replaced file's mode is deliberately not carried over: a store
        left world-readable by anything else is tightened to owner-only by
        the next write rather than preserved.
        """
        self._require_safe_paths()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        make_private_dir(self._path.parent)
        document = {
            "schema": SCHEMA_VERSION,
            "batches": {
                batch_id: batch.to_json() for batch_id, batch in batches.items()
            },
            "retained_provenance": [row.to_json() for row in retained],
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

    def get(self, batch_id: str) -> ImportBatch:
        """One batch by id, or raise ``not_found``."""
        batches, _retained = self._read()
        batch = batches.get(batch_id) if isinstance(batch_id, str) else None
        if batch is None:
            raise ImportStoreError(
                f"no import batch {batch_id!r} is recorded", code=NOT_FOUND
            )
        return batch

    def list_for_workspace(self, workspace: str) -> list[ImportBatch]:
        """Every batch filed for ``workspace``, oldest first.

        Ordered by ``(created_at, batch_id)``, which is total and stable: the
        id breaks a tie between two batches stamped in the same second.
        """
        checked = _validated_workspace(workspace, code=INVALID_BATCH)
        batches, _retained = self._read()
        return sorted(
            (batch for batch in batches.values() if batch.workspace == checked),
            key=lambda batch: (batch.created_at, batch.batch_id),
        )

    def retained_provenance(self) -> list[FactProvenance]:
        """Accepted fact evidence that outlived its batch.

        What :meth:`prune_expired` kept when the terminal batch aged out:
        per-fact ``provider:source_id:anchor`` rows with the workspace each
        was filed into. Evidence, not a dedupe index — re-import dedupe is the
        live batches' key plus C7's queue-level exact-text check.
        """
        _batches, retained = self._read()
        return list(retained)

    # -- writing ----------------------------------------------------------

    def create(
        self,
        *,
        workspace: str,
        sources: Sequence[Mapping[str, Any] | BatchSource],
        destination: str | None = None,
        known_own_ids: Collection[tuple[str, str]] = (),
    ) -> ImportBatch:
        """File a new batch over a selection, in ``queued`` state.

        ``sources`` is one ``{provider, source_id}`` pair per selected
        conversation — ids, never paths — with an optional ``content_digest``
        when the caller already read the session. At most
        :data:`ciao.import_discover.BATCH_CAP` pairs: a selection is a
        person's decision about a handful of conversations, and the batch
        cap is what the consent screen states before the batch runs.

        ``known_own_ids`` is Ciaobot's own ``(provider, session_id)`` answer
        for this workspace. A selected session in it — or a bare Ciaobot
        chat id — is refused rather than filed: a Ciaobot-own session is
        never an import source.

        Refused with ``conflict`` when another batch is still open for this
        workspace (one batch at a time), or when a live batch already covers
        one of these conversations under the
        ``(provider, source_id, content_digest, destination,
        extraction_revision)`` key. An unknown digest matches: re-selecting
        a conversation whose content was never read is the same conversation
        until proven otherwise.
        """
        checked_workspace = _validated_workspace(workspace, code=INVALID_BATCH)
        checked_destination = (
            _validated_workspace(destination, code=INVALID_BATCH)
            if destination is not None
            else checked_workspace
        )
        requested = [_requested_source(item) for item in sources]
        if not requested:
            raise ImportStoreError(
                "a batch covers at least one selected conversation", code=INVALID_BATCH
            )
        if len(requested) > BATCH_CAP:
            raise ImportStoreError(
                f"a batch covers at most {BATCH_CAP} conversations, "
                f"got {len(requested)}",
                code=INVALID_BATCH,
            )
        seen: set[tuple[str, str]] = set()
        for source in requested:
            key = (canonical_provider(source.provider), source.source_id)
            if key in seen:
                raise ImportStoreError(
                    f"{source.provider} session {source.source_id!r} is "
                    "selected twice; a batch covers each conversation once",
                    code=INVALID_BATCH,
                )
            seen.add(key)
        own = {
            (canonical_provider(str(provider)), str(session_id).strip())
            for provider, session_id in known_own_ids
        }
        for source in requested:
            key = (canonical_provider(source.provider), source.source_id)
            if key in own or is_ciaobot_chat_id(source.source_id):
                raise ImportStoreError(
                    f"{source.provider} session {source.source_id!r} is Ciaobot's "
                    "own; it is not an import source",
                    code=INVALID_BATCH,
                )
        with self._mutation():
            batches, retained = self._read()
            for batch in batches.values():
                if batch.workspace != checked_workspace:
                    continue
                if batch.status in OPEN_STATUSES:
                    raise ImportStoreError(
                        f"workspace {checked_workspace!r} already has an open "
                        f"import batch ({batch.batch_id}, {batch.status}); finish "
                        "it, cancel it or forget it before filing another",
                        code=CONFLICT,
                    )
            for source in requested:
                holder = _dedupe_holder(
                    batches,
                    workspace=checked_workspace,
                    destination=checked_destination,
                    source=source,
                )
                if holder is not None:
                    raise ImportStoreError(
                        f"{source.provider} session {source.source_id!r} is "
                        f"already covered by import batch {holder}; forget that "
                        "batch to re-run this conversation",
                        code=CONFLICT,
                    )
            stamp = self._stamp()
            batch = ImportBatch(
                batch_id=uuid.uuid4().hex,
                workspace=checked_workspace,
                destination=checked_destination,
                status=QUEUED,
                extraction_revision=EXTRACTION_REVISION,
                sources=tuple(requested),
                progress=BatchProgress(total_sources=len(requested)),
                provenance=(),
                created_at=stamp,
                updated_at=stamp,
            )
            batches[batch.batch_id] = batch
            self._write(batches, retained)
            return batch

    def begin(self, batch_id: str) -> ImportBatch:
        """Move a ``queued`` batch to ``running``. Anything else is a conflict."""
        with self._mutation():
            batches, retained = self._read()
            batch = _require_batch(batches, batch_id)
            if batch.status != QUEUED:
                raise ImportStoreError(
                    f"import batch {batch_id} is {batch.status}, not queued; "
                    "only a queued batch can begin",
                    code=CONFLICT,
                )
            updated = _touch(batch, self._stamp(), status=RUNNING)
            batches[batch_id] = updated
            self._write(batches, retained)
            return updated

    def record_source(
        self,
        batch_id: str,
        provider: str,
        source_id: str,
        *,
        content_digest: str = "",
        status: str = SOURCE_PENDING,
    ) -> ImportBatch:
        """Record what reading one selected source found.

        Upserts by ``(provider, source_id)`` against the batch's own
        selection: a source outside the selection is refused rather than
        added, because the batch is the record of what a person consented to.
        Only a ``running`` batch moves; a cancelled batch stays where the
        cancellation left it.
        """
        checked_provider = _validated_provider(provider, code=INVALID_BATCH)
        checked_source = _validated_source_id(source_id, code=INVALID_BATCH)
        checked_digest = _validated_digest(content_digest, code=INVALID_BATCH)
        checked_status = _validated_source_status(status, code=INVALID_BATCH)
        with self._mutation():
            batches, retained = self._read()
            batch = _require_batch(batches, batch_id)
            _require_running(batch, "recorded")
            key = (canonical_provider(checked_provider), checked_source)
            known = {
                (canonical_provider(item.provider), item.source_id)
                for item in batch.sources
            }
            if key not in known:
                raise ImportStoreError(
                    f"{checked_provider} session {checked_source!r} is not in "
                    f"import batch {batch_id}'s selection",
                    code=INVALID_BATCH,
                )
            sources = tuple(
                BatchSource(
                    provider=item.provider,
                    source_id=item.source_id,
                    content_digest=checked_digest,
                    status=checked_status,
                )
                if (canonical_provider(item.provider), item.source_id) == key
                else item
                for item in batch.sources
            )
            updated = _touch(batch, self._stamp(), sources=sources)
            batches[batch_id] = updated
            self._write(batches, retained)
            return updated

    def record_progress(
        self,
        batch_id: str,
        *,
        completed_sources: int | None = None,
        proposals_filed: int | None = None,
        skipped: int | None = None,
        current_source_id: str | None = None,
    ) -> ImportBatch:
        """Move a ``running`` batch's counters. Only named counters change.

        Cancellation stops at the current step: recording against a batch
        that is no longer running is a conflict, so a turn that finished
        after the cancel cannot rewrite what the cancellation retained.
        """
        with self._mutation():
            batches, retained = self._read()
            batch = _require_batch(batches, batch_id)
            _require_running(batch, "advanced")
            progress = batch.progress
            if completed_sources is not None:
                checked = _validated_count(
                    completed_sources,
                    field_name="completed_sources",
                    code=INVALID_BATCH,
                )
                if checked > progress.total_sources:
                    raise ImportStoreError(
                        f"import batch {batch_id} holds {progress.total_sources} "
                        "sources; it cannot have completed more",
                        code=INVALID_BATCH,
                    )
                progress = BatchProgress(
                    total_sources=progress.total_sources,
                    completed_sources=checked,
                    proposals_filed=progress.proposals_filed,
                    skipped=progress.skipped,
                    current_source_id=progress.current_source_id,
                )
            if proposals_filed is not None:
                progress = BatchProgress(
                    total_sources=progress.total_sources,
                    completed_sources=progress.completed_sources,
                    proposals_filed=_validated_count(
                        proposals_filed,
                        field_name="proposals_filed",
                        code=INVALID_BATCH,
                    ),
                    skipped=progress.skipped,
                    current_source_id=progress.current_source_id,
                )
            if skipped is not None:
                progress = BatchProgress(
                    total_sources=progress.total_sources,
                    completed_sources=progress.completed_sources,
                    proposals_filed=progress.proposals_filed,
                    skipped=_validated_count(
                        skipped, field_name="skipped", code=INVALID_BATCH
                    ),
                    current_source_id=progress.current_source_id,
                )
            if current_source_id is not None:
                progress = BatchProgress(
                    total_sources=progress.total_sources,
                    completed_sources=progress.completed_sources,
                    proposals_filed=progress.proposals_filed,
                    skipped=progress.skipped,
                    current_source_id=_text(
                        current_source_id,
                        field_name="current_source_id",
                        code=INVALID_BATCH,
                    ),
                )
            updated = _touch(batch, self._stamp(), progress=progress)
            batches[batch_id] = updated
            self._write(batches, retained)
            return updated

    def record_fact_provenance(
        self, batch_id: str, provenance: FactProvenance
    ) -> ImportBatch:
        """Attach one imported fact's external source to a ``running`` batch.

        The tag is checked the way the admission check checks it — an
        external ``provider:source_id:anchor`` that names no Ciaobot chat —
        so a batch can never carry provenance for Ciaobot's own work.
        """
        if not isinstance(provenance, FactProvenance):
            raise ImportStoreError(
                "provenance must be a FactProvenance record", code=INVALID_BATCH
            )
        tag = (
            f"{provenance.provider}:{provenance.source_id}:{provenance.anchor}"
        )
        try:
            assert_external_provenance(tag)
        except ProvenanceNotExternal as exc:
            raise ImportStoreError(str(exc), code=INVALID_BATCH) from None
        if not provenance.destination or not WORKSPACE_NAME_RE.fullmatch(
            provenance.destination
        ):
            raise ImportStoreError(
                "provenance destination must be a registered-style workspace name",
                code=INVALID_BATCH,
            )
        with self._mutation():
            batches, retained = self._read()
            batch = _require_batch(batches, batch_id)
            _require_running(batch, "cited")
            updated = _touch(
                batch, self._stamp(), provenance=batch.provenance + (provenance,)
            )
            batches[batch_id] = updated
            self._write(batches, retained)
            return updated

    def finish(self, batch_id: str, status: str, *, error: str = "") -> ImportBatch:
        """Settle a ``running`` batch as ``done``, ``failed`` or ``partial``.

        ``partial`` is the honest answer when some sources were extracted and
        others were not: the filed proposals stay filed, and the batch says
        so. Cancellation is not a finish mode — see :meth:`cancel`.
        """
        if status not in FINISH_STATUSES:
            raise ImportStoreError(
                f"cannot finish an import batch as {status!r}; expected one of "
                f"{list(FINISH_STATUSES)}",
                code=INVALID_BATCH,
            )
        checked_error = _text(error, field_name="error", code=INVALID_BATCH)
        with self._mutation():
            batches, retained = self._read()
            batch = _require_batch(batches, batch_id)
            _require_running(batch, "finished")
            updated = _touch(batch, self._stamp(), status=status, error=checked_error)
            batches[batch_id] = updated
            self._write(batches, retained)
            return updated

    def cancel(self, batch_id: str, *, reason: str = "") -> ImportBatch:
        """Stop a ``queued`` or ``running`` batch, keeping what it produced.

        Idempotent: cancelling a cancelled batch returns it unchanged, so a
        second press (or a retry) is not an error. Cancelling a batch that
        already settled as ``done``, ``failed`` or ``partial`` is a conflict:
        its outcome stands, and re-running it is a new batch.
        """
        checked_reason = _text(reason, field_name="reason", code=INVALID_BATCH)
        with self._mutation():
            batches, retained = self._read()
            batch = _require_batch(batches, batch_id)
            if batch.status == CANCELLED:
                return batch
            if batch.status not in OPEN_STATUSES:
                raise ImportStoreError(
                    f"import batch {batch_id} already settled as {batch.status}; "
                    "it cannot be cancelled",
                    code=CONFLICT,
                )
            updated = _touch(batch, self._stamp(), status=CANCELLED, error=checked_reason)
            batches[batch_id] = updated
            self._write(batches, retained)
            return updated

    def forget(self, batch_id: str) -> None:
        """Drop a batch record. Queue and vault are untouched.

        Removing an import never implicitly deletes accepted memories: the
        proposals C7 filed stay queued (or decided), and the facts a person
        accepted stay in the vault. What goes away is the selection, the
        digests, the progress and the unaccepted provenance — which is also
        what releases the dedupe key, so the conversation may be re-run.
        """
        with self._mutation():
            batches, retained = self._read()
            _require_batch(batches, batch_id)
            del batches[batch_id]
            self._write(batches, retained)

    def prune_expired(self, *, now: datetime | None = None) -> int:
        """Drop terminal batches past the retention window; keep accepted evidence.

        A batch whose ``updated_at`` is older than
        :data:`IMPORT_SNAPSHOT_RETENTION_DAYS` and which has settled is an
        unaccepted source snapshot nobody is coming back to: it is removed.
        Its accepted provenance rows are appended to the store's retained
        list first, so an expired import stays attributable after the batch
        is gone. Open batches are never pruned — in-progress work is not a
        snapshot — and a run that removes nothing writes nothing. Returns how
        many batches were removed.
        """
        moment = now or self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        cutoff = moment.astimezone(UTC) - timedelta(
            days=IMPORT_SNAPSHOT_RETENTION_DAYS
        )
        with self._mutation():
            batches, retained = self._read()
            expired = [
                batch_id
                for batch_id, batch in batches.items()
                if batch.status in TERMINAL_STATUSES
                and datetime.fromisoformat(batch.updated_at) <= cutoff
            ]
            if not expired:
                return 0
            kept = list(retained)
            for batch_id in expired:
                batch = batches.pop(batch_id)
                for row in batch.provenance:
                    if row.accepted and row not in kept:
                        kept.append(row)
            self._write(batches, kept)
            return len(expired)

    def rename_workspace(self, old: str, new: str) -> int:
        """Point every batch filed under ``old`` at ``new``.

        Only ``workspace`` changes; ``destination`` is left alone.
        Idempotent: a second call sees no ``old`` rows and writes nothing.
        """
        with self._mutation():
            batches, retained = self._read()
            stamp = self._stamp()
            renamed = 0
            for batch_id, batch in list(batches.items()):
                if batch.workspace != old:
                    continue
                batches[batch_id] = _touch(batch, stamp, workspace=new)
                renamed += 1
            if renamed:
                self._write(batches, retained)
            return renamed

    # -- clock ------------------------------------------------------------

    def _stamp(self) -> str:
        """The ISO-8601 UTC stamp a changed batch carries.

        From the injected clock, so a test can say exactly when a batch
        changed; a clock read inside the writer would make a batch's own
        bytes depend on when the write happened.
        """
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat(timespec="seconds")


def _require_batch(batches: dict[str, ImportBatch], batch_id: str) -> ImportBatch:
    """The stored batch for ``batch_id``, or raise ``not_found``."""
    batch = batches.get(batch_id) if isinstance(batch_id, str) else None
    if batch is None:
        raise ImportStoreError(
            f"no import batch {batch_id!r} is recorded", code=NOT_FOUND
        )
    return batch


def _require_running(batch: ImportBatch, verb: str) -> None:
    """Refuse a move against a batch that is no longer running."""
    if batch.status != RUNNING:
        raise ImportStoreError(
            f"import batch {batch.batch_id} is {batch.status}, not running; "
            f"it cannot be {verb}",
            code=CONFLICT,
        )


def _touch(batch: ImportBatch, stamp: str, **changes: Any) -> ImportBatch:
    """The batch with ``changes`` applied and ``updated_at`` set to ``stamp``.

    Every store move passes its own stamp, so retention always measures from
    the last activity, not the filing.
    """
    return cast(ImportBatch, replace(batch, updated_at=stamp, **changes))


def _requested_source(item: Mapping[str, Any] | BatchSource) -> BatchSource:
    """One requested selection entry as a :class:`BatchSource`.

    Mappings carry ``provider`` and ``source_id`` with an optional
    ``content_digest``; a ``BatchSource`` is carried as-is after validation.
    Anything else is refused rather than guessed at.
    """
    if isinstance(item, BatchSource):
        _validated_provider(item.provider, code=INVALID_BATCH)
        _validated_source_id(item.source_id, code=INVALID_BATCH)
        _validated_digest(item.content_digest, code=INVALID_BATCH)
        _validated_source_status(item.status, code=INVALID_BATCH)
        return BatchSource(
            provider=item.provider,
            source_id=item.source_id.strip(),
            content_digest=item.content_digest,
            status=item.status,
        )
    if not isinstance(item, Mapping):
        raise ImportStoreError(
            "each selected source must be a {provider, source_id} pair",
            code=INVALID_BATCH,
        )
    provider = _validated_provider(item.get("provider"), code=INVALID_BATCH)
    source_id = _validated_source_id(item.get("source_id"), code=INVALID_BATCH)
    raw_digest = item.get("content_digest", "")
    digest = _validated_digest(raw_digest, code=INVALID_BATCH)
    if set(item) - {"provider", "source_id", "content_digest"}:
        raise ImportStoreError(
            "a selected source carries only provider, source_id and "
            "content_digest",
            code=INVALID_BATCH,
        )
    return BatchSource(
        provider=provider, source_id=source_id, content_digest=digest
    )


def _dedupe_holder(
    batches: Mapping[str, ImportBatch],
    *,
    workspace: str,
    destination: str,
    source: BatchSource,
) -> str | None:
    """The live batch id already covering ``source``, or ``None``.

    The batch-level ``(provider, source_id, content_digest, destination,
    extraction_revision)`` key: same canonical provider and session, same
    destination, this extraction revision, and digests that do not disagree
    (an unknown digest on either side matches — an unread conversation is the
    same conversation until its content says otherwise).
    """
    wanted = (canonical_provider(source.provider), source.source_id)
    for batch_id, batch in batches.items():
        if batch.workspace != workspace or batch.destination != destination:
            continue
        if batch.extraction_revision != EXTRACTION_REVISION:
            continue
        for item in batch.sources:
            if (canonical_provider(item.provider), item.source_id) != wanted:
                continue
            if (
                item.content_digest
                and source.content_digest
                and item.content_digest != source.content_digest
            ):
                continue
            return batch_id
    return None


# ── Engine wiring ──────────────────────────────────────────────────────


def engine_store_path(config: Any) -> Path:
    """The engine's batch store file: ``<runtime>/import/import-batches.json``.

    The one place that path is built: the batch routes and the retention
    sweep both resolve through here, so the two spellings cannot drift.
    ``config`` is the engine config; only ``state_path`` is read.
    """
    return Path(config.state_path).parent / "import" / "import-batches.json"


def sweep_import_batches(config: Any) -> int:
    """Run the once-per-boot retention sweep. Fail-soft: never raises.

    Drops terminal batches past :data:`IMPORT_SNAPSHOT_RETENTION_DAYS` while
    retaining accepted fact evidence (see :meth:`ImportStore.prune_expired`).
    The sweep is one bounded file read-modify-write — no vault walk, no
    provider call — and any failure (a corrupt store included, which stays
    fail-closed for readers) is logged here and answered as zero, so a bad
    batch file can never block engine startup. Returns how many batches were
    removed.
    """
    try:
        path = engine_store_path(config)
        if not path.exists():
            return 0
        return ImportStore(path).prune_expired()
    except Exception:
        logger.exception("Import batch retention sweep failed; nothing was pruned")
        return 0
