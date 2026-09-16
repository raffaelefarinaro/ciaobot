"""Recoverable receipts for managed memory mutations.

Bounded memory is stored as fenced regions inside a workspace ``CLAUDE.md``
(``ciao/memory_tool.py``) and the review queue is a Markdown file
(``ciao/memory_proposals.py``). Neither is a database: a single managed
operation can touch the guide, the queue and the decision sidecar, and a crash
between any two of those leaves an install nobody can reason about.

This module is the small recoverable journal the review asked for, layered over
the existing APIs rather than replacing them. Every managed write records a
*receipt*:

* a stable id derived from the operation's content, so a retry collapses onto
  the same receipt instead of piling up duplicates;
* the actor (``operator``/``agent``/``auto``) and surface (``pwa``/``cli``/
  ``archive``/``mcp``) that caused it;
* the expected, before and after revisions of the destination — a region body
  digest for a guide write, the file digest for a queue write;
* the before/after images for reversible operations, so an undo can restore the
  exact predecessor;
* a status: ``prepared`` (intent recorded, file not yet replaced), ``applied``,
  ``failed``, ``rolled_back``, ``conflict`` or ``undone``.

The journal is append-only JSONL beside the proposal queue
(``Workspace/Memory-Receipts.jsonl``). Each state change appends a full row and
the effective receipt is the latest row per id, so no read-modify-write of the
journal itself is needed and a crash can only ever lose the final row, never
corrupt an earlier one.

Two rules the code enforces rather than documents:

* A lock that cannot be taken is a *failure*, never a reason to write anyway.
  Serializing managed writes per guide is what makes the read-merge-write
  correct, so an unavailable lock returns a retryable outcome and leaves the
  destination untouched.
* A destination whose revision changed since the operation was planned is a
  *conflict*, not a licence to overwrite. Preview/apply/undo all compare
  revisions, so an external direct edit is reported as a conflict instead of
  being silently replaced. External edits are not audited — only managed writes
  produce receipts.

Recovery runs at startup (:func:`recover_pending`) and reconciles every
receipt interrupted between ``prepared`` and its terminal state by comparing
the destination on disk against the recorded before/after images. An uncertain
outcome — neither image matches — is recorded as a conflict and left
view-only; it is never resolved by appending a competing fact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


RECEIPT_VERSION = 1

RECEIPTS_NAME = "Memory-Receipts.jsonl"

PREPARED = "prepared"
APPLIED = "applied"
FAILED = "failed"
ROLLED_BACK = "rolled_back"
CONFLICT = "conflict"
UNDONE = "undone"

# Terminal states a recovery pass will not touch again.
_TERMINAL = frozenset({APPLIED, FAILED, ROLLED_BACK, CONFLICT, UNDONE})

# Kinds whose before image is enough to undo. A queue resolution restores a
# bullet, which is meaningful too; a prune is reversible for the same reason.
UNDOABLE_KINDS = frozenset(
    {"region_apply", "region_update", "region_remove", "queue_resolve", "prune_expired"}
)

# Above this size an image is omitted and the operation stays view-only: the
# regions are cap-bounded, so this only ever trips on a hand-corrupted guide
# and never on a fact the pipeline itself wrote.
MAX_IMAGE_CHARS = 200_000

MAX_BYTES = 4 * 1024 * 1024
KEEP_LINES = 4000


class MemoryReceiptError(RuntimeError):
    """Base class for receipt-protocol failures."""


class RevisionConflict(MemoryReceiptError):
    """The destination changed since the operation was planned."""


class UndoUnsupported(MemoryReceiptError):
    """The receipt has no before image or an operation this protocol cannot reverse."""


# ── Digest / id helpers ───────────────────────────────────────────────────


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_revision(text: str) -> str:
    """The revision of one destination's text at a point in time."""
    return _sha(text)


def new_receipt_id(basis: str) -> str:
    """A stable id for one logical operation.

    Derived from the operation's content so two attempts at the same mutation
    (a retry after a timeout, the same archive re-processed) share an id and can
    be folded rather than duplicated. A random suffix keeps concurrent
    operations that happen to share a basis distinguishable.
    """
    return f"mrcpt_{_sha(basis)[:20]}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── Journal location and append ───────────────────────────────────────────


def journal_path(vault_root: Path | None, guide: Path | None = None) -> Path:
    """The receipt journal for one workspace vault (or a guide's own folder).

    Prefers the workspace vault so the receipts sit beside the proposal queue
    and the decision sidecar they describe. A caller that only knows the guide
    (the MCP control plane, which has no vault seam) gets a journal under that
    guide's ``Workspace/`` folder instead — never the developer's real
    ``.runtime``.
    """
    if vault_root is not None:
        return Path(vault_root) / "Workspace" / RECEIPTS_NAME
    if guide is not None:
        return Path(guide).parent / "Workspace" / RECEIPTS_NAME
    raise MemoryReceiptError("a vault root or a guide path is required")


QUEUE_LOCK_TIMEOUT_S = 30.0
"""How long a managed queue write waits for the queue lock before failing.

Bounded like the guide lock so a wedged holder cannot pin a request forever,
and long enough that ordinary concurrency never trips it.
"""

# Queue locks live outside the vault. The lock sits beside the queue today only
# because it was simplest, but the vault is user-owned content: a stray
# ``*.lock`` there pollutes the tree and broke the re-rooting round-trip's
# byte-identical invariant. Keep the lock file in a stable per-install
# directory keyed by the resolved queue path, so the vault stays untouched.
_QUEUE_LOCK_DIR_ENV = "CIAO_QUEUE_LOCK_DIR"


def _queue_lock_path(resolved_key: str) -> Path:
    """A lock file for a queue path, outside the vault it guards.

    Uses ``CIAO_QUEUE_LOCK_DIR`` when set (tests pin it), else a per-user
    directory under the system temp root. Deterministic in the resolved queue
    path so every process and thread guarding the same queue picks the same
    lock. The uid component keeps two local accounts from colliding on a shared
    ``/tmp``.
    """
    base = os.environ.get(_QUEUE_LOCK_DIR_ENV, "").strip()
    if base:
        root = Path(base)
    else:
        try:
            uid = os.getuid()
        except AttributeError:  # pragma: no cover - non-POSIX
            uid = 0
        root = Path(tempfile.gettempdir()) / f"ciao-queue-locks-{uid}"
    digest = hashlib.sha256(resolved_key.encode("utf-8")).hexdigest()[:32]
    return root / f"{digest}.lock"


# Re-entrancy depth per resolved queue path for the current thread. A wrapper
# (`queue_resolution`) holds the lock across a body that calls a writer
# (`remove_proposal_by_substring`) which also takes it; the inner acquire must
# not try to flock the same file again (that would deadlock against ourselves).
_QUEUE_LOCK_DEPTH = threading.local()


class QueueLockError(RuntimeError):
    """The proposal queue lock could not be acquired.

    Deliberately fatal to the write it guards, exactly like
    :class:`ciao.memory_tool.MemoryLockError`: a caller that swallowed this and
    wrote anyway would reintroduce the lost-update race the lock exists to
    prevent.
    """

    retryable = True


@contextmanager
def queue_lock(
    proposals_path: Path, *, timeout_s: float = QUEUE_LOCK_TIMEOUT_S
):
    """Serialize a read-check-replace on one proposal queue file.

    The undo path re-reads the queue, compares its revision against the
    receipt's after image, and only then replaces the file. Without a lock the
    check and the replace are not atomic: another writer can land in between,
    and the stale replacement silently discards that update. Every managed
    queue writer (``append_proposals``, ``remove_proposal_by_substring``, the
    PWA batch/sweep/single routes, and :func:`_undo_queue`) takes this same
    lock, so the revision check is meaningful across processes and threads.

    Re-entrant within one thread so a wrapper can hold it across a body that
    calls a writer which takes it again; the cross-process guard is the
    ``flock`` held by the outermost acquire.

    A lock that cannot be taken raises :class:`QueueLockError`; callers must let
    it propagate rather than fall through to an unlocked write.
    """
    import fcntl
    import time

    try:
        key = str(proposals_path.resolve())
    except OSError:
        key = str(proposals_path)
    depths = getattr(_QUEUE_LOCK_DEPTH, "depths", None)
    if depths is None:
        depths = {}
        _QUEUE_LOCK_DEPTH.depths = depths
    if depths.get(key, 0) > 0:
        # Already held by this thread; the outermost acquire owns the flock.
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    lock_path = _queue_lock_path(key)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise QueueLockError(f"could not open queue lock {lock_path}: {exc}") from exc
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise QueueLockError(
                    f"queue lock {lock_path} is held; the write was not applied"
                )
            time.sleep(0.05)
        except OSError as exc:
            handle.close()
            raise QueueLockError(f"could not lock {lock_path}: {exc}") from exc
    depths[key] = 1
    try:
        yield
    finally:
        depths.pop(key, None)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _append(journal: Path, payload: dict[str, Any]) -> None:
    """Append one receipt row, serialized across processes and fsynced."""
    journal.parent.mkdir(parents=True, exist_ok=True)
    row = {**payload, "v": RECEIPT_VERSION}
    line = json.dumps(row, ensure_ascii=False) + "\n"
    lock = journal.with_name(journal.name + ".lock")
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        fcntl = None  # type: ignore[assignment]
    handle = lock.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        with journal.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        # Trim while still holding the lock. Running it after the release let a
        # concurrent append land between this trim's read and its os.replace,
        # and the stale snapshot then silently deleted that newer receipt.
        _trim_if_large(journal)
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _trim_if_large(journal: Path) -> None:
    try:
        if not journal.exists() or journal.stat().st_size < MAX_BYTES:
            return
        lines = journal.read_text(encoding="utf-8", errors="replace").splitlines()
        # Keep the newest rows, but never drop a non-terminal receipt: an
        # interrupted operation must stay recoverable until it settles.
        #
        # An id's effective status is its *last* row anywhere in the journal,
        # not just in the retained tail. A `prepared` row with no terminal row
        # can sit before the cut, so computing pending ids from the retained
        # lines alone found nothing and the trim silently dropped the
        # unresolved receipt. Fold the whole journal first, then refuse to trim
        # while any id that would lose a line is still non-terminal.
        kept = lines[-KEEP_LINES:]
        dropped_lines = lines[:-KEEP_LINES]
        last_status: dict[str, object] = {}
        for line in lines:
            row = _safe_row(line)
            if row is None:
                continue
            rid = str(row.get("id", ""))
            if rid:
                last_status[rid] = row.get("status")
        pending_ids = {
            rid for rid, status in last_status.items() if status not in _TERMINAL
        }
        dropped_ids = {
            str(row.get("id", ""))
            for row in (_safe_row(line) for line in dropped_lines)
            if row is not None and row.get("id")
        }
        if dropped_ids & pending_ids:
            return
        tmp = journal.with_name(f".{journal.name}.trim.tmp")
        tmp.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        os.replace(tmp, journal)
    except Exception:  # noqa: BLE001 — trimming is best-effort
        logger.debug("memory receipts: trim failed", exc_info=True)


def _safe_row(line: str) -> dict[str, Any] | None:
    """Parse one journal line into a row, or None when it is unusable."""
    if not line.strip():
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def read_receipts(journal: Path) -> list[dict[str, Any]]:
    """Fold the journal into one effective receipt per id, oldest first.

    The latest row for an id wins, which is what makes the append-only journal
    order-independent: a crash can only lose a trailing row, and the prior state
    remains intact and readable.
    """
    if not journal.exists():
        return []
    folded: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    try:
        raw = journal.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id", ""))
        if not rid:
            continue
        if rid not in folded:
            order.append(rid)
        folded[rid] = row
    return [folded[rid] for rid in order]


def find_receipt(journal: Path, receipt_id: str) -> dict[str, Any] | None:
    for receipt in read_receipts(journal):
        if receipt.get("id") == receipt_id:
            return receipt
    return None


def is_undoable(receipt: dict[str, Any]) -> bool:
    """Whether this receipt carries a before image this protocol can restore.

    Legacy or unsupported rows — no image, an unknown kind, a non-``applied``
    status — are view-only. The History surface must render them without an
    Undo affordance rather than pretending the change is reversible.

    ``undoable`` lets a multi-row batch record per-fact history rows without
    offering an undo on each: one row carries the transaction's whole-file
    before image, and the rest are explicitly ``undoable=False``. Without it,
    undoing any one row restored the whole pre-batch file and resurrected every
    fact the batch had removed.
    """
    return (
        receipt.get("status") == APPLIED
        and receipt.get("undoable", True) is not False
        and str(receipt.get("kind", "")) in UNDOABLE_KINDS
        and receipt.get("before_text") is not None
        and receipt.get("after_text") is not None
        and not receipt.get("undo_of")
    )


# ── Region mutation ───────────────────────────────────────────────────────


@dataclass(slots=True)
class RegionMutation:
    """The result one region mutation decided, before it is committed."""

    entries: list[str] = field(default_factory=list)
    wrote: bool = False
    outcome: str = "written"
    fact_text: str = ""
    destination: str = ""
    removed_texts: list[str] = field(default_factory=list)
    kind: str = "region_apply"


def _image(text: str) -> str | None:
    return text if len(text) <= MAX_IMAGE_CHARS else None


def commit_region_change(
    guide: Path,
    region: str,
    *,
    entries: list[str],
    actor: str,
    source: str,
    workspace: str = "",
    vault_root: Path | None = None,
    lock: Any | None = None,
    expected_revision: str | None = None,
    fact_text: str = "",
    destination: str = "",
    removed_texts: list[str] | None = None,
    kind: str = "region_apply",
) -> dict[str, Any]:
    """Replace one region's body through the receipt protocol.

    The caller normally already holds the guide lock (``lock``), because it had
    to read the region to compute the merge. When ``lock`` is None this function
    takes it, so a direct caller cannot skip serialization. Either way the
    destination is re-read under the lock and its revision is compared against
    ``expected_revision`` before anything is written: a mismatch raises
    :class:`RevisionConflict` with the file untouched.

    Ordering is intent-then-write-then-confirm: the ``prepared`` row is durable
    before the guide is replaced, and the ``applied`` row after. A crash between
    them is reconciled by :func:`recover_pending` from the recorded images.
    """
    from ciao.memory_tool import (
        MemoryLockError,
        diagnose_region,
        guide_lock,
        read_region,
        replace_region_body,
        resolve_region as resolve_region_name,
        serialize_entries,
    )

    owned = lock is None
    handle = lock
    if owned:
        handle = guide_lock(guide)
    try:
        canonical = resolve_region_name(region)
        try:
            text = guide.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise MemoryReceiptError(f"guide not found: {guide}") from exc
        diags = diagnose_region(text, canonical)
        if diags:
            raise MemoryReceiptError("; ".join(d.message for d in diags))

        existing, read_diags = read_region(guide, canonical)
        if read_diags:
            raise MemoryReceiptError(
                "; ".join(d.message for d in read_diags)
            )

        before_text = serialize_entries(existing)
        after_text = serialize_entries(entries)
        before_revision = content_revision(before_text)
        after_revision = content_revision(after_text)

        if expected_revision is not None and expected_revision != before_revision:
            raise RevisionConflict(
                "the destination changed since this operation was planned"
            )

        rid = new_receipt_id(
            f"{guide}|{region}|{kind}|{fact_text}|{before_revision}"
        )
        journal = journal_path(vault_root, guide)
        base: dict[str, Any] = {
            "id": rid,
            "ts": _now(),
            "actor": actor,
            "source": source,
            "workspace": workspace,
            "kind": kind,
            "guide": str(guide),
            "region": canonical,
            "expected_revision": expected_revision or "",
            "before_revision": before_revision,
            "after_revision": after_revision,
            "vault_root": str(vault_root) if vault_root is not None else "",
            "before_text": _image(before_text),
            "after_text": _image(after_text),
            "fact_text": fact_text,
            "destination": destination,
            "removed_texts": list(removed_texts or []),
        }
        if before_text == after_text:
            # Nothing to write: a no-op still gets a receipt so a caller can
            # distinguish "already satisfied" from "wrote something".
            receipt = {**base, "status": APPLIED, "changed": False}
            _append(journal, receipt)
            return receipt

        _append(journal, {**base, "status": PREPARED, "changed": True})
        updated = replace_region_body(text, canonical, entries)
        from ciao.memory_tool import write_guide_atomically

        write_guide_atomically(guide, updated)
        receipt = {**base, "status": APPLIED, "changed": True}
        _append(journal, receipt)
        return receipt
    except (MemoryLockError, RevisionConflict, MemoryReceiptError):
        raise
    except Exception as exc:  # noqa: BLE001 — record the failure, then surface it
        try:
            journal = journal_path(vault_root, guide)
            _append(
                journal,
                {
                    "id": new_receipt_id(f"{guide}|{region}|{kind}|{fact_text}"),
                    "ts": _now(),
                    "actor": actor,
                    "source": source,
                    "workspace": workspace,
                    "kind": kind,
                    "guide": str(guide),
                    "region": region,
                    "status": FAILED,
                    "error": str(exc),
                },
            )
        except Exception:  # noqa: BLE001 — recording must not mask the failure
            logger.debug("memory receipts: failed to record failure", exc_info=True)
        raise
    finally:
        if owned and handle is not None:
            from ciao.memory_tool import release_guide_lock

            release_guide_lock(handle)


# ── Queue mutation receipt ────────────────────────────────────────────────


def record_queue_resolution(
    proposals_path: Path,
    *,
    removed_text: str,
    kind: str,
    promoted: bool,
    actor: str,
    source: str,
    workspace: str = "",
    vault_root: Path | None = None,
    before_text: str | None = None,
    after_text: str | None = None,
    undoable: bool = True,
) -> dict[str, Any]:
    """Record an already-performed queue bullet removal as a receipt.

    Prefer :func:`queue_resolution`, which brackets the file rewrite with a
    ``prepared`` row so a crash mid-rewrite is recoverable. This entry point
    remains for callers that only want to record a completed removal.

    ``undoable=False`` records a per-fact history row without a reversible
    before image. A multi-row batch uses it for every fact except one: the
    batch is a single atomic file rewrite, so exactly one row may carry the
    whole-file before image. Marking each row undoable let undoing any one of
    them restore the whole pre-batch file and resurrect every fact the batch
    had removed.

    Returns the effective receipt. Never raises: a receipt is a record, and
    failing to record one must not fail the removal it describes.
    """
    try:
        return _write_queue_receipt(
            proposals_path,
            removed_text=removed_text,
            kind=kind,
            promoted=promoted,
            actor=actor,
            source=source,
            workspace=workspace,
            vault_root=vault_root,
            before_text=before_text,
            after_text=after_text,
            status=APPLIED,
            undoable=undoable,
        )
    except Exception:  # noqa: BLE001 — recording must not break removal
        logger.debug("memory receipts: queue receipt failed", exc_info=True)
        return {}


@contextmanager
def queue_resolution(
    proposals_path: Path,
    *,
    removed_text: str,
    kind: str,
    promoted: bool,
    actor: str,
    source: str,
    workspace: str = "",
    vault_root: Path | None = None,
):
    """Bracket a queue rewrite with ``prepared`` then ``applied`` receipts.

    The body performs the actual removal (with its own content-verified
    rewrite). A crash between the ``prepared`` row and the rewrite is
    reconciled by :func:`recover_pending` on the next startup by asking whether
    the bullet is still present: gone means the removal landed, present means
    it did not.

    The queue lock is held for the whole block, so the ``prepared`` before
    image this records cannot be made stale by a concurrent writer landing
    between the read and the body's rewrite.
    """
    with queue_lock(proposals_path):
        try:
            before = proposals_path.read_text(encoding="utf-8")
        except OSError:
            before = ""
        rid = new_receipt_id(
            f"{proposals_path}|queue_resolve|{removed_text}|{content_revision(before)}"
        )
        journal = journal_path(vault_root, proposals_path.parent)
        base: dict[str, Any] = {
            "id": rid,
            "ts": _now(),
            "actor": actor,
            "source": source,
            "workspace": workspace,
            "kind": "queue_resolve",
            "queue": str(proposals_path),
            "action": "promoted" if promoted else "dismissed",
            "fact_text": removed_text,
            "removed_text": removed_text,
            "promoted": promoted,
            # The proposal kind is part of the recovery identity: removing
            # `[memory] Use Python` must not be blocked by a remaining
            # `[profile] Use Python`.
            "proposal_kind": kind,
            # The before image and revision live on the prepared row so a
            # crash-recovered receipt can still be undone: recovery fills in the
            # after image/revision from disk, and `is_undoable` needs both.
            "before_revision": content_revision(before),
            "before_text": _image(before),
        }
        try:
            _append(journal, {**base, "status": PREPARED})
        except Exception:  # noqa: BLE001 — the removal proceeds regardless
            logger.debug("memory receipts: could not prepare queue receipt", exc_info=True)
        try:
            yield base
        finally:
            try:
                _write_queue_receipt(
                    proposals_path,
                    removed_text=removed_text,
                    kind=kind,
                    promoted=promoted,
                    actor=actor,
                    source=source,
                    workspace=workspace,
                    vault_root=vault_root,
                    before_text=before,
                    after_text=None,
                    status=APPLIED,
                    receipt_id=rid,
                    prepared=True,
                )
            except Exception:  # noqa: BLE001 — recording must not break removal
                logger.debug("memory receipts: queue receipt failed", exc_info=True)


def _write_queue_receipt(
    proposals_path: Path,
    *,
    removed_text: str,
    kind: str,
    promoted: bool,
    actor: str,
    source: str,
    workspace: str,
    vault_root: Path | None,
    before_text: str | None,
    after_text: str | None,
    status: str,
    receipt_id: str | None = None,
    prepared: bool = False,
    undoable: bool = True,
) -> dict[str, Any]:
    before = before_text
    if before is None:
        try:
            before = proposals_path.read_text(encoding="utf-8")
        except OSError:
            before = ""
    after = after_text
    if after is None:
        try:
            after = proposals_path.read_text(encoding="utf-8")
        except OSError:
            after = ""
    rid = receipt_id or new_receipt_id(
        f"{proposals_path}|queue_resolve|{removed_text}|{content_revision(before)}"
    )
    receipt = {
        "id": rid,
        "ts": _now(),
        "actor": actor,
        "source": source,
        "workspace": workspace,
        "kind": "queue_resolve",
        "proposal_kind": kind,
        "queue": str(proposals_path),
        "action": "promoted" if promoted else "dismissed",
        "fact_text": removed_text,
        "removed_text": removed_text,
        "promoted": promoted,
        "before_revision": content_revision(before),
        "after_revision": content_revision(after),
        "before_text": _image(before),
        "after_text": _image(after),
        "status": status,
    }
    if not undoable:
        # A per-fact history row for a batch that already records its
        # transaction-level before image on one row. It must not offer an undo:
        # restoring the whole pre-batch file from every row resurrected facts
        # the batch had removed.
        receipt["undoable"] = False
    if prepared:
        # The caller already wrote the ``prepared`` row; this is its
        # confirmation. A bullet that is still queued means the intended
        # removal did not land, whatever else changed the file around it.
        present = any(
            removed_text in line for line in after.splitlines()
        )
        receipt["bullet_present"] = present
        if present:
            receipt["status"] = ROLLED_BACK
    journal = journal_path(vault_root, proposals_path.parent)
    _append(journal, receipt)
    return receipt


def record_queue_resolution_batch(
    proposals_path: Path,
    removals: list[dict[str, Any]],
    *,
    before_text: str,
    after_text: str,
    actor: str,
    source: str,
    workspace: str = "",
    vault_root: Path | None = None,
    undoable_first: bool = True,
) -> list[dict[str, Any]]:
    """Record one queue rewrite that removed several bullets.

    The batch is a single atomic file rewrite, so it is one transaction: the
    first row carries the whole-file before/after images and is the only
    undoable receipt. The remaining facts get history rows with
    ``undoable=False`` so the History list still shows them without offering an
    undo that would restore every other fact in the batch (and resurrect an
    accepted fact's bullet). Each item in ``removals`` carries ``text``,
    ``kind`` and ``promoted``.

    ``undoable_first=False`` records every row non-undoable, for the bracket
    that writes its own transaction-level undoable row instead.

    Returns the recorded receipts; never raises.
    """
    receipts: list[dict[str, Any]] = []
    for index, removal in enumerate(removals):
        try:
            receipts.append(
                record_queue_resolution(
                    proposals_path,
                    removed_text=str(removal.get("text") or ""),
                    kind=str(removal.get("kind") or ""),
                    promoted=bool(removal.get("promoted")),
                    actor=actor,
                    source=source,
                    workspace=workspace,
                    vault_root=vault_root,
                    before_text=before_text,
                    after_text=after_text,
                    undoable=undoable_first and index == 0,
                )
            )
        except Exception:  # noqa: BLE001 — recording must not break removal
            logger.debug("memory receipts: batch row failed", exc_info=True)
    return receipts


@contextmanager
def queue_resolution_multi(
    proposals_path: Path,
    removals: list[dict[str, Any]],
    *,
    actor: str,
    source: str,
    workspace: str = "",
    vault_root: Path | None = None,
):
    """Bracket a multi-bullet queue rewrite with prepared then applied receipts.

    The batch/sweep routes rewrite the queue and only then record receipts, so
    a crash between the write and the record left no evidence at all: the
    bullets were gone and startup recovery could not reconstruct the mutation.
    This writes a single transaction-level ``prepared`` row (listing every
    removed text) before the body's rewrite, and the applied row after it, so
    :func:`recover_pending` reconciles an interrupted batch exactly as it does a
    single resolution.

    The queue lock is held for the whole block; the body is responsible for the
    actual rewrite. ``removals`` items carry ``text`` and ``kind``. An empty
    ``removals`` list yields without recording anything — there is no mutation
    to bracket.
    """
    if not removals:
        yield {}
        return
    with queue_lock(proposals_path):
        try:
            before = proposals_path.read_text(encoding="utf-8")
        except OSError:
            before = ""
        texts = [str(r.get("text") or "") for r in removals]
        removal_kinds = [str(r.get("kind") or "") for r in removals]
        rid = new_receipt_id(
            f"{proposals_path}|queue_resolve_batch|"
            f"{'|'.join(texts)}|{content_revision(before)}"
        )
        journal = journal_path(vault_root, proposals_path.parent)
        base: dict[str, Any] = {
            "id": rid,
            "ts": _now(),
            "actor": actor,
            "source": source,
            "workspace": workspace,
            "kind": "queue_resolve",
            "queue": str(proposals_path),
            "action": "promoted"
            if any(bool(r.get("promoted")) for r in removals)
            else "dismissed",
            "removed_texts": texts,
            "removed_text": texts[0] if texts else "",
            "removed_kinds": removal_kinds,
            "promoted": any(bool(r.get("promoted")) for r in removals),
            "batch": True,
            # Before image/revision on the prepared row so a crash-recovered
            # batch receipt is still undoable.
            "before_revision": content_revision(before),
            "before_text": _image(before),
        }
        try:
            _append(journal, {**base, "status": PREPARED})
        except Exception:  # noqa: BLE001 — the removal proceeds regardless
            logger.debug("memory receipts: could not prepare batch receipt", exc_info=True)
        completed = False
        try:
            yield base
            completed = True
        finally:
            try:
                after = proposals_path.read_text(encoding="utf-8")
            except OSError:
                after = ""
            all_gone = bool(texts) and not any(
                _bullets_match(after, text, removal_kinds[i] if i < len(removal_kinds) else "")
                for i, text in enumerate(texts)
            )
            if completed and all_gone:
                # Only a completed, content-verified rewrite earns applied
                # rows. The body raising (disk full, permission error) leaves
                # the prepared row for startup recovery instead of falsely
                # claiming the facts were accepted or dismissed.
                try:
                    record_queue_resolution_batch(
                        proposals_path,
                        removals,
                        before_text=before,
                        after_text=after,
                        actor=actor,
                        source=source,
                        workspace=workspace,
                        vault_root=vault_root,
                        # The transaction row below is the only undoable one;
                        # the per-fact rows are history-only so undoing one
                        # cannot restore the whole pre-batch file.
                        undoable_first=False,
                    )
                except Exception:  # noqa: BLE001 — recording must not break removal
                    logger.debug("memory receipts: batch receipt failed", exc_info=True)
                settled = {
                    **base,
                    "before_revision": content_revision(before),
                    "after_revision": content_revision(after),
                    "before_text": _image(before),
                    "after_text": _image(after),
                    "kind": "queue_resolve",
                    "removed_texts": texts,
                    "removed_text": texts[0] if texts else "",
                    "status": APPLIED,
                }
                try:
                    _append(journal, settled)
                except Exception:  # noqa: BLE001
                    logger.debug("memory receipts: batch settle failed", exc_info=True)
            elif content_revision(after) == content_revision(before):
                # A completed rewrite that removed nothing left the queue at its
                # exact before-image, so there is nothing to recover: settle it
                # rolled_back now.
                try:
                    _append(
                        journal,
                        {
                            **base,
                            "before_revision": content_revision(before),
                            "after_revision": content_revision(after),
                            "before_text": _image(before),
                            "after_text": _image(after),
                            "status": ROLLED_BACK,
                            "detail": "batch rewrite removed nothing",
                        },
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("memory receipts: batch rollback failed", exc_info=True)
            # Otherwise (`write_text` raised mid-way, or only some bullets are
            # gone) leave the prepared row non-terminal: the queue may match
            # neither image, and startup recovery is the one place that can
            # classify it as applied, rolled_back or conflict. Writing a
            # terminal rollback here would make recovery skip a file that may
            # have lost unrelated proposals.


# ── Recovery ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RecoveryResult:
    """What one recovery pass found and did."""

    reconciled: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    scanned: int = 0


def recover_pending(
    guide: Path | None = None,
    *,
    vault_root: Path | None = None,
    journal: Path | None = None,
    proposals_path: Path | None = None,
) -> RecoveryResult:
    """Reconcile every interrupted receipt against the files on disk.

    Called at startup (and directly by tests). For each non-terminal receipt:

    * region write whose body now equals the after image → ``applied`` (the
      crash landed after the rename);
    * region write whose body still equals the before image → ``rolled_back``
      (the crash landed before the rename);
    * neither → ``conflict``: the destination changed out from under us, so it
      is left alone and surfaced rather than guessed at;
    * queue resolution whose bullet is gone → ``applied``; still present →
      ``rolled_back``.

    An ``applied`` receipt with a fact but no recorded outcome has its decision
    sidecar completed here, so a crash between the guide write and the decision
    update still yields exactly one consistent operation.
    """
    if journal is None:
        journal = journal_path(vault_root, guide)
    result = RecoveryResult()
    receipts = read_receipts(journal)
    result.scanned = len(receipts)
    for receipt in receipts:
        if str(receipt.get("status", "")) in _TERMINAL:
            continue
        try:
            settled = _reconcile(receipt, journal, proposals_path=proposals_path)
        except Exception:  # noqa: BLE001 — recovery is best-effort per row
            logger.exception(
                "memory receipts: could not reconcile %s", receipt.get("id")
            )
            continue
        if settled is None:
            continue
        if settled.get("status") == CONFLICT:
            result.conflicts.append(settled)
        else:
            result.reconciled.append(settled)
    return result


def _reconcile(
    receipt: dict[str, Any],
    journal: Path,
    *,
    proposals_path: Path | None,
) -> dict[str, Any] | None:
    kind = str(receipt.get("kind", ""))
    if kind == "queue_resolve":
        return _reconcile_queue(receipt, journal, proposals_path=proposals_path)
    return _reconcile_region(receipt, journal)


def _reconcile_region(
    receipt: dict[str, Any], journal: Path
) -> dict[str, Any] | None:
    from ciao.memory_tool import read_region, serialize_entries

    guide = Path(str(receipt.get("guide", "")))
    region = str(receipt.get("region", ""))
    if not guide.name or not region:
        return None
    if not guide.exists():
        return _settle(journal, receipt, ROLLED_BACK, "guide disappeared")
    try:
        entries, diags = read_region(guide, region)
    except Exception:  # noqa: BLE001
        return _settle(journal, receipt, CONFLICT, "guide unreadable")
    if diags:
        return _settle(journal, receipt, CONFLICT, "; ".join(d.message for d in diags))
    current = content_revision(serialize_entries(entries))
    if current == str(receipt.get("after_revision", "")):
        settled = _settle(journal, receipt, APPLIED, "recovered after crash")
        _complete_outcome(receipt)
        return settled
    if current == str(receipt.get("before_revision", "")):
        return _settle(journal, receipt, ROLLED_BACK, "write never landed")
    return _settle(
        journal,
        receipt,
        CONFLICT,
        "destination changed while the operation was interrupted",
    )


def _bullets_match(text: str, needle: str, kind: str = "") -> bool:
    """True when a parsed bullet matches ``needle`` (exactly) and ``kind``.

    Compares parsed bullet text, not the raw line: a substring search reported
    a removed ``Use Python`` as still queued while ``Use Python 3`` remained,
    which rolled back a successful resolution. When the receipt names a
    proposal kind, the bullet's kind must match too, so removing
    ``[memory] Use Python`` is not blocked by a remaining ``[profile] Use
    Python``. A receipt with no kind (the CLI's free-substring path) falls back
    to text-only matching.
    """
    from ciao.memory_proposals import _one_line
    from ciao.proposal_kinds import parse_bullet

    target = _one_line(str(needle))
    if not target:
        return False
    wanted_kind = str(kind or "").strip().lower()
    for line in text.splitlines():
        bullet = parse_bullet(line)
        if bullet is None:
            continue
        if wanted_kind and bullet.kind.lower() != wanted_kind:
            continue
        if _one_line(bullet.text) == target:
            return True
    return False


def _bullets_containing(text: str, needle: str) -> bool:
    """Back-compat shim: exact text match with no kind constraint."""
    return _bullets_match(text, needle)


def _reconcile_queue(
    receipt: dict[str, Any],
    journal: Path,
    *,
    proposals_path: Path | None,
) -> dict[str, Any] | None:
    path = proposals_path or Path(str(receipt.get("queue", "")))
    if not path.name:
        return None
    # A batch prepared row lists every removed bullet; the removal landed only
    # when all of them are gone. A single row names one ``removed_text``.
    removed_texts = receipt.get("removed_texts")
    kinds = receipt.get("removed_kinds")
    if isinstance(removed_texts, list) and removed_texts:
        kinds_list = kinds if isinstance(kinds, list) else []
        needles = [
            (str(t), str(kinds_list[i]) if i < len(kinds_list) else "")
            for i, t in enumerate(removed_texts)
            if str(t)
        ]
    else:
        single = str(receipt.get("removed_text", ""))
        single_kind = str(receipt.get("proposal_kind", ""))
        needles = [(single, single_kind)] if single else []
    if not needles:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _settle(journal, receipt, ROLLED_BACK, "queue missing")
    still_present = [
        needle for needle, kind in needles if _bullets_match(text, needle, kind)
    ]
    if not still_present:
        # The rewrite landed. Populate the after image/revision from disk so a
        # recovered receipt stays undoable: a prepared row carries only the
        # before image, and `is_undoable` requires both.
        settled_receipt = {
            **receipt,
            "after_revision": content_revision(text),
            "after_text": _image(text),
        }
        settled = _settle(journal, settled_receipt, APPLIED, "bullet already removed")
        _complete_outcome(settled_receipt)
        return settled
    detail = (
        f"{len(still_present)} batch bullet(s) still queued"
        if len(needles) > 1
        else "bullet still queued"
    )
    return _settle(journal, receipt, ROLLED_BACK, detail)


def _settle(
    journal: Path, receipt: dict[str, Any], status: str, detail: str
) -> dict[str, Any]:
    settled = {
        **{k: v for k, v in receipt.items() if k != "v"},
        "status": status,
        "recovered": True,
        "detail": detail,
        "settled_at": _now(),
    }
    _append(journal, settled)
    return settled


def _complete_outcome(receipt: dict[str, Any]) -> None:
    """Idempotently finish the decision record an interrupted apply missed.

    Region receipts complete a promotion for the fact they wrote. Queue
    receipts complete the dismissal/promotion sidecar entry the route or CLI
    would have written after the receipt: a crash between the queue wrapper's
    terminal row and that call otherwise left the resolved proposal re-filable
    by the next archive pass.
    """
    if receipt.get("outcome_recorded"):
        return
    fact = str(receipt.get("fact_text", "")).strip() or str(
        receipt.get("removed_text", "")
    ).strip()
    vault_root = receipt.get("vault_root")
    if not fact:
        return
    try:
        if str(receipt.get("kind", "")) == "queue_resolve" or receipt.get("queue"):
            from ciao.memory_proposals import record_dismissal, record_promotion

            queue_raw = str(receipt.get("queue", ""))
            queue = Path(queue_raw) if queue_raw else (
                Path(str(vault_root)) / "Workspace" / "Memory-Proposals.md"
                if vault_root
                else None
            )
            if queue is None:
                return
            texts = receipt.get("removed_texts")
            facts = (
                [str(t) for t in texts if str(t)]
                if isinstance(texts, list) and texts
                else [fact]
            )
            promoted = bool(receipt.get("promoted"))
            for item in facts:
                if promoted:
                    record_promotion(
                        queue,
                        text=item,
                        kind=str(receipt.get("proposal_kind", "")),
                        via=str(receipt.get("source", "")),
                        source=str(receipt.get("source", "")),
                        destination=str(receipt.get("destination", "")),
                        outcome="written",
                        once=True,
                    )
                else:
                    record_dismissal(
                        queue,
                        text=item,
                        kind=str(receipt.get("proposal_kind", "")),
                        via=str(receipt.get("source", "")),
                        source=str(receipt.get("source", "")),
                        outcome=str(receipt.get("action", "")),
                    )
            return
        from ciao.memory_proposals import record_promotion

        if vault_root:
            queue = Path(str(vault_root)) / "Workspace" / "Memory-Proposals.md"
        else:
            return
        record_promotion(
            queue,
            text=fact,
            kind=str(receipt.get("region", "memory")),
            via="auto",
            destination=str(receipt.get("destination", "")),
            outcome="written",
            once=True,
        )
    except Exception:  # noqa: BLE001 — completing the record is best-effort
        logger.debug("memory receipts: could not complete outcome", exc_info=True)


# ── Undo ──────────────────────────────────────────────────────────────────


def undo_receipt(
    receipt_id: str,
    *,
    vault_root: Path | None = None,
    journal: Path | None = None,
    actor: str = "operator",
    source: str = "pwa",
) -> dict[str, Any]:
    """Reverse one applied receipt, refusing when the destination moved.

    A receipt is undoable only when it is ``applied``, carries a before/after
    image, and names an operation this protocol knows how to reverse. Any
    unsupported or legacy row raises :class:`UndoUnsupported` — it stays
    view-only rather than being approximated.

    The destination is re-read under the guide lock and its revision compared
    with the receipt's after revision. A mismatch means an unrelated fact was
    written after this operation, so replacing the region with the before image
    would delete it: that is a :class:`RevisionConflict`, not an undo.
    """
    if journal is None:
        journal = journal_path(vault_root, None)
    receipt = find_receipt(journal, receipt_id)
    if receipt is None:
        raise MemoryReceiptError(f"unknown receipt: {receipt_id}")
    if not is_undoable(receipt):
        raise UndoUnsupported(
            "this operation has no reversible before-image; it stays view-only"
        )

    kind = str(receipt.get("kind", ""))
    if kind == "queue_resolve":
        return _undo_queue(receipt, journal, vault_root, actor, source)
    return _undo_region(receipt, journal, vault_root, actor, source)


def _undo_region(
    receipt: dict[str, Any],
    journal: Path,
    vault_root: Path | None,
    actor: str,
    source: str,
) -> dict[str, Any]:
    from ciao.memory_tool import guide_lock, read_region, release_guide_lock
    from ciao.memory_tool import MemoryLockError

    guide = Path(str(receipt.get("guide", "")))
    region = str(receipt.get("region", ""))
    if not guide.name:
        raise UndoUnsupported("receipt names no guide")
    handle = guide_lock(guide)
    try:
        entries, diags = read_region(guide, region)
        if diags:
            raise MemoryReceiptError("; ".join(d.message for d in diags))
        from ciao.memory_tool import serialize_entries

        current = content_revision(serialize_entries(entries))
        if current != str(receipt.get("after_revision", "")):
            raise RevisionConflict(
                "the destination changed after this operation; undo would remove "
                "unrelated facts, so it was refused"
            )
        # Rebuild the before image under the same lock so the reverse write is
        # itself a journaled, atomic replacement. ``region_undo`` is not in
        # UNDOABLE_KINDS, so an undo cannot be itself undone.
        reverse = commit_region_change(
            guide,
            region,
            entries=_entries_from_body(str(receipt.get("before_text", ""))),
            actor=actor,
            source=source,
            workspace=str(receipt.get("workspace", "")),
            vault_root=vault_root or journal.parent.parent,
            lock=handle,
            expected_revision=str(receipt.get("after_revision", "")),
            kind="region_undo",
            fact_text="",
        )
        _append(
            journal,
            {
                **{k: v for k, v in receipt.items() if k != "v"},
                "status": UNDONE,
                "undo_of": str(receipt.get("id", "")),
                "undo_receipt": reverse.get("id"),
                "settled_at": _now(),
            },
        )
        return reverse
    finally:
        release_guide_lock(handle)


def _entries_from_body(body: str) -> list[str]:
    from ciao.memory_tool import parse_entries

    return parse_entries(body)


def _undo_queue(
    receipt: dict[str, Any],
    journal: Path,
    vault_root: Path | None,
    actor: str,
    source: str,
) -> dict[str, Any]:
    """Restore the queue file to its before image when it has not moved since.

    Read, revision check and replace happen under :func:`queue_lock`, so a
    concurrent managed writer cannot land between them. Without the lock the
    revision check proved nothing: another update could arrive after the check
    and before ``os.replace``, and the undo would silently discard it.
    """
    path = Path(str(receipt.get("queue", "")))
    if not path.name:
        raise UndoUnsupported("receipt names no queue")
    before = receipt.get("before_text")
    if before is None:
        raise UndoUnsupported("receipt carries no queue image")
    with queue_lock(path):
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MemoryReceiptError(f"queue unreadable: {exc}") from exc
        if content_revision(current) != str(receipt.get("after_revision", "")):
            raise RevisionConflict(
                "the queue changed after this operation; undo was refused"
            )
        tmp = path.with_name(f".{path.name}.undo.tmp")
        tmp.write_text(str(before), encoding="utf-8")
        os.replace(tmp, path)
    undone = {
        **{k: v for k, v in receipt.items() if k != "v"},
        "status": UNDONE,
        "undo_of": str(receipt.get("id", "")),
        "settled_at": _now(),
    }
    _append(journal, undone)
    return undone


# ── View helpers ──────────────────────────────────────────────────────────


def list_receipts(
    vault_root: Path | None = None,
    *,
    journal: Path | None = None,
    workspace: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Newest-first receipts for the History surface, with an ``undoable`` flag."""
    if journal is None:
        journal = journal_path(vault_root, None)
    rows = read_receipts(journal)
    if workspace:
        rows = [row for row in rows if str(row.get("workspace", "")) == workspace]
    rows.sort(key=lambda row: str(row.get("ts", "")), reverse=True)
    out: list[dict[str, Any]] = []
    for row in rows[: max(1, limit)]:
        # Never leak multi-kilobyte before/after images through a list surface;
        # the undo path reads them straight from the journal.
        public = {k: v for k, v in row.items() if k not in {"before_text", "after_text", "v"}}
        public["undoable"] = is_undoable(row)
        out.append(public)
    return out


def receipt_journal_candidates(config: Any) -> list[Path]:
    """Every per-workspace receipt journal this install owns.

    Each workspace vault gets one journal under its ``Workspace/`` folder (the
    same place the proposal queue and decision sidecar live). A workspace whose
    vault is not resolvable is skipped rather than guessed at.
    """
    journals: list[Path] = []
    seen: set[str] = set()
    names = getattr(config, "workspace_names", None)
    try:
        workspace_names = list(names()) if callable(names) else []
    except Exception:  # noqa: BLE001 — a broken registry must not block startup
        workspace_names = []
    for name in workspace_names:
        try:
            vault = Path(config.workspace_vault_root(name))
        except (AttributeError, ValueError, OSError):
            continue
        key = str(vault)
        if key in seen:
            continue
        seen.add(key)
        journals.append(journal_path(vault, None))
    # The pre-re-rooting layout has one shared vault (``workspace_names`` may be
    # empty or single). Cover the config's own vault root too so an install
    # that keeps one global guide still gets its journal reconciled.
    vault_root = getattr(config, "vault_root", None)
    if vault_root is not None:
        try:
            key = str(vault_root)
        except Exception:  # noqa: BLE001
            key = ""
        if key and key not in seen:
            journals.append(journal_path(Path(vault_root), None))
    # The guide-local fallback journal. A managed write that reaches
    # ``journal_path`` with no vault root (a provider prune whose caller passes
    # only the guide) records beside that guide's ``Workspace/`` folder. Include
    # every agent root's fallback journal so those receipts are still discovered
    # and reconciled after a crash.
    targets = getattr(config, "agent_root_targets", None)
    try:
        roots = list(targets()) if callable(targets) else []
    except Exception:  # noqa: BLE001 — a broken registry must not block startup
        roots = []
    for root, _name in roots:
        try:
            fallback = journal_path(None, Path(root) / "CLAUDE.md")
        except MemoryReceiptError:
            continue
        key = str(fallback)
        if key in seen:
            continue
        seen.add(key)
        journals.append(fallback)
    return journals


def recover_memory_journals(config: Any) -> dict[str, list[dict[str, Any]]]:
    """Startup recovery across every workspace's receipt journal.

    Idempotent and never raises: an install with no journals, or a journal that
    cannot be read, is a no-op. Returns the reconciled and conflicted rows for
    the startup tracker to report.
    """
    reconciled: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for journal in receipt_journal_candidates(config):
        try:
            # Each receipt names its own queue, so recovery needs only the
            # journal: a region write reconciles against its guide, a queue
            # receipt against the queue it recorded.
            result = recover_pending(journal=journal)
            reconciled.extend(result.reconciled)
            conflicts.extend(result.conflicts)
        except Exception:  # noqa: BLE001 — recovery must never fail startup
            logger.exception("memory receipts: recovery failed for %s", journal)
    return {"reconciled": reconciled, "conflicts": conflicts}

