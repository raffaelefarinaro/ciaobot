"""Recorder for memory-proposal resolution outcomes.

Job runs say whether the extraction pipeline *ran*; this log says whether its
output was *useful*: every time a queued proposal leaves the review queue as a
decision — promoted (accepted into its destination) or dismissed — one JSON
line lands in ``.runtime/proposal_outcomes.jsonl``::

    {"ts": "...", "workspace": "personal", "kind": "memory",
     "action": "promoted", "via": "pwa"}

``via`` records which surface made the decision: the PWA routes or the
``ciao memory-proposal-dismiss`` CLI the nightly curation agent drives.
Only memory-extraction kinds are counted (:data:`EXTRACTION_KINDS`): skill
proposals and rehome judgements share the review surface but answer to
different pipelines.

Everything mirrors :mod:`ciao.job_runs`: an append-only, size-trimmed JSONL
under ``.runtime``, written strictly best-effort so a recording failure can
never break the resolution it is describing, plus a fail-open aggregator
(:func:`tally`) that folds the log into the counts the Automation page shows.
When the log rotates, the dropped lines are archived verbatim
(``proposal_outcomes.rotated-<ts>.jsonl``) so lifetime counts survive
trimming; the fold de-duplicates by event content, which is what makes the
rotation's two-step swap crash-recoverable without ever double-counting.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROPOSAL_OUTCOMES_NAME = "proposal_outcomes.jsonl"
ACTIONS: tuple[str, ...] = ("promoted", "dismissed")
# Every current caller is the PWA route handlers or the ``ciao
# memory-proposal-dismiss`` CLI the nightly curation agent drives (see the
# module docstring). The whitelist is defense-in-depth: call sites already
# gate on this, but a typo (e.g. "agents") must not silently fragment the
# by-surface split instead of being refused loudly (in debug logs).
VIA_VALUES: tuple[str, ...] = ("pwa", "agent")
MAX_BYTES = 2 * 1024 * 1024  # trim the log once it passes ~2 MB
KEEP_LINES = 2000            # lines retained after a trim

# Kinds this ledger counts. It measures the MEMORY extraction pipeline's
# usefulness; other producers share the review surface but answer to different
# questions: `[skill]` rows come from skill evolution, `[rehome]` rows are
# note-move judgements queued by vault hygiene (`vault_rehome`).
EXTRACTION_KINDS: frozenset[str] = frozenset(
    {"memory", "profile", "user", "project", "people", "learnings", "review"}
)


def is_extraction_kind(kind: str) -> bool:
    """Whether a proposal kind belongs in the promoted-vs-dismissed tally."""
    return kind in EXTRACTION_KINDS

# Recent-window length for :func:`tally`, in days.
RECENT_DAYS = 30

_runtime_dir_override: Path | None = None


def configure(runtime_dir: Path | str) -> None:
    """Pin the runtime directory. Called once at server startup so the
    recorder writes to the same ``.runtime`` the rest of the config uses,
    regardless of the process cwd. Tests can point it at a temp dir.

    Also drops the cached tally: it is keyed by nothing but the TTL, so
    without this a runtime-dir change (a test re-pointing it, or a real
    re-configure) would keep serving a tally read from the previous
    directory until the TTL happened to expire.
    """
    global _runtime_dir_override
    _runtime_dir_override = Path(runtime_dir)
    reset_tally_cache()


def _runtime_dir() -> Path:
    if _runtime_dir_override is not None:
        return _runtime_dir_override
    raw = (
        os.environ.get("CIAO_RUNTIME_ROOT")
        or ".runtime"
    )
    return Path(raw).resolve()


def _log_path() -> Path:
    return _runtime_dir() / PROPOSAL_OUTCOMES_NAME


def _private_open(path: Path, mode: str):
    """Open ``path`` for text append/write, creating it 0600 like
    :func:`ciao.jsonio.write_private_text`, rather than the umask default
    (typically 0644). This log, the writer lock, and its rotation archives
    hold workspace names, so they get the same create-private-then-chmod
    convention that module already uses for on-disk secrets: create with the
    tight mode directly, then chmod in case an older version of this file
    left a pre-existing file looser.
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if "w" in mode else os.O_APPEND)
    fd = os.open(path, flags, 0o600)
    f = os.fdopen(fd, mode, encoding="utf-8")
    os.chmod(path, 0o600)
    return f


def record(
    kind: str,
    action: str,
    *,
    workspace: str = "",
    via: str = "pwa",
) -> None:
    """Append one resolution event. Never raises.

    An unknown ``action`` is refused rather than written: the aggregator can
    only fold the two decisions it knows, and a third spelling would silently
    vanish from every count while claiming to be recorded. ``via`` and
    ``kind`` are refused the same way — every current call site already
    passes one of :data:`VIA_VALUES` and gates on :func:`is_extraction_kind`
    before calling this, so these checks are defense-in-depth: a typo like
    "agents" must not silently fragment the by-surface split, and a
    non-extraction kind (``skill``, ``rehome``) must not sneak into a tally
    that only measures the memory-extraction pipeline. The append and the
    size-triggered rotation share an inter-process lock, so a rotation can
    never rewrite away an event another process appended between its read and
    its write.
    """
    if action not in ACTIONS:
        logger.debug("Refusing to record proposal outcome with action %r", action)
        return
    if via not in VIA_VALUES:
        logger.debug("Refusing to record proposal outcome with via %r", via)
        return
    if not is_extraction_kind(kind):
        logger.debug("Refusing to record proposal outcome with kind %r", kind)
        return
    try:
        with _writer_lock():
            path = _log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": datetime.now(UTC).isoformat(),
                "workspace": str(workspace or ""),
                "kind": kind,
                "action": action,
                "via": via,
            }
            with _private_open(path, "a") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            _trim_if_large(path)
    except Exception:  # noqa: BLE001 — recording must never break a resolution
        logger.debug("Failed to record proposal outcome", exc_info=True)


try:  # pragma: no cover - platform selection
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX degrades to unlocked
    fcntl = None  # type: ignore[assignment]

_LOCK_NAME = "proposal_outcomes.lock"


@contextmanager
def _writer_lock(exclusive: bool = True):
    """Serialize writers against each other across processes.

    Every mutation (append + trim + archive write) runs under the exclusive
    lock; :func:`tally` reads under a shared one. On platforms without
    ``fcntl`` this degrades to unlocked behaviour — same-process callers were
    already serialized by the event loop.
    """
    if fcntl is None:
        yield
        return
    lock_path = _runtime_dir() / _LOCK_NAME
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = _private_open(lock_path, "a")
    except Exception:  # noqa: BLE001 — locking is an optimization, not a gate
        yield
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _trim_if_large(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < MAX_BYTES:
            return
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        dropped, kept = lines[:-KEEP_LINES], lines[-KEEP_LINES:]
        # Rotation is a two-step swap whose crash windows are all recoverable
        # because the fold (see ``tally``) de-duplicates events by content:
        #
        # 1. The dropped lines are archived VERBATIM in their own new file,
        #    written via temp+rename so it appears whole or not at all.
        # 2. The live log is atomically swapped for the kept lines.
        #
        # A crash between the two leaves every dropped line in BOTH files;
        # the fold counts each event once. There is deliberately no folded
        # totals sidecar: absorb-then-delete is a transaction that cannot be
        # rolled back after a crash (the sidecar would already count lines
        # the log still holds, double-counting them forever), while verbatim
        # archives either lost nothing or duplicated something the dedupe
        # absorbs.
        rotated_name = (
            "proposal_outcomes.rotated-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            + ".jsonl"
        )
        tmp = path.with_name(f".{rotated_name}.tmp")
        with _private_open(tmp, "w") as f:
            f.writelines(dropped)
        os.replace(tmp, _runtime_dir() / rotated_name)
        tmp = path.with_name(f".{path.name}.trim.tmp")
        with _private_open(tmp, "w") as f:
            f.writelines(kept)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to trim proposal-outcome log", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass


_ARCHIVE_GLOB = "proposal_outcomes.rotated-*.jsonl"


def _event_key(event: dict[str, Any]) -> str:
    """The identity of one outcome event, for crash-recovery dedupe.

    Rotation copies a line verbatim, so a crash between the archive step and
    the log swap leaves the same event in both files. The full event — every
    key and value, order-normalized — is the identity: two genuine
    resolutions always differ somewhere (the recorder stamps microseconds),
    while a crash duplicate is byte-identical. Anything weaker (kind+action+
    workspace, say) would collapse two legitimate resolutions that only
    disagree in a field this key forgot about. The fold keeps the first
    occurrence.
    """
    return json.dumps(event, sort_keys=True, ensure_ascii=False)


def _iter_event_lines(paths: list[Path]) -> list[str]:
    """Read every JSONL source under one shared lock, newest file first.

    The live log is read last so its line wins the dedupe (it is the file
    the recorder appends to, and the archive only holds what a rotation
    removed from it).
    """
    lines: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                lines.extend(f.readlines())
        except OSError:
            continue
    return lines


def tally(*, now: datetime | None = None) -> dict[str, Any]:
    """Fold the outcome log into the Automation-page counts.

    Returns ``{"promoted": n, "dismissed": m, "by_workspace": {name:
    {"promoted": n, "dismissed": m}}, "recent_30d": {"promoted": n,
    "dismissed": m}}``. Rotation moves dropped lines verbatim into
    ``proposal_outcomes.rotated-<ts>.jsonl`` archives, so lifetime counts
    survive trimming, and the fold de-duplicates events by content — a crash
    between the archive step and the log swap leaves a line in both files,
    and each event is still counted exactly once.
    Malformed lines are skipped, never fatal; an event
    with an unreadable timestamp still counts in the totals, just not in the
    30-day window. Never raises: a broken log degrades to zeros rather than
    failing the page that reads it.
    """
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff = cutoff_ts(reference)

    by_workspace: dict[str, dict[str, int]] = {}
    recent = {"promoted": 0, "dismissed": 0}
    action_totals = {action: 0 for action in ACTIONS}

    def bucket(name: str) -> dict[str, int]:
        return by_workspace.setdefault(name, {"promoted": 0, "dismissed": 0})

    lines: list[str] = []
    try:
        with _writer_lock(exclusive=False):
            runtime = _runtime_dir()
            rotated = sorted(runtime.glob(_ARCHIVE_GLOB)) if runtime.is_dir() else []
            path = _log_path()
            lines = _iter_event_lines(rotated + [path])
    except Exception:  # noqa: BLE001 — a broken log is zeros, not a failed page
        logger.debug("Failed to tally proposal outcomes", exc_info=True)
        lines = []

    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        action = event.get("action")
        if action not in ACTIONS:
            continue
        key = _event_key(event)
        if key in seen:
            # A crash mid-rotation can leave the same event in the archive
            # and the live log; the fold counts it once.
            continue
        seen.add(key)
        workspace = str(event.get("workspace") or "")
        action_totals[action] += 1
        bucket(workspace)[action] += 1
        ts = _parsed_ts(event.get("ts"))
        if ts is not None and ts.astimezone(UTC) >= cutoff:
            recent[action] += 1  # type: ignore[literal-required]

    return {
        "promoted": action_totals["promoted"],
        "dismissed": action_totals["dismissed"],
        "by_workspace": by_workspace,
        "recent_30d": recent,
    }


def cutoff_ts(reference: datetime) -> datetime:
    return reference - timedelta(days=RECENT_DAYS)


def _parsed_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# ── Endpoint cache ────────────────────────────────────────────────────────
# The tally walks the whole log, so /api/automation serves it through a short
# in-process TTL: the first page load pays the read, refreshes within a minute
# do not. Same spirit as make_cached_package_status, minus the error tier a
# local file read cannot produce.

_TALLY_TTL_SECONDS = 60.0
_tally_cache: dict[str, Any] = {"value": None, "expires": 0.0}
# Guards reads and writes of ``_tally_cache``: /api/automation can be hit by
# more than one request-handling thread inside the TTL window, and without a
# lock two racing misses could both mutate the two-key dict at once (one
# thread's ``expires`` write landing between the other's read and write),
# which could hand a caller a ``value``/``expires`` pair that were never set
# together.
_tally_cache_lock = threading.Lock()


def tally_cached() -> dict[str, Any]:
    """Tally through the short TTL cache. Never raises.

    Returns the same dict object stored in the cache on a cache hit — no
    defensive copy — matching :func:`ciao.package_version.make_cached_package_status`,
    the module's own reference point for this cache's "same spirit" above.
    Callers must treat the result as read-only; mutating it would corrupt
    what every other caller sees until the TTL expires.
    """
    now = time.monotonic()
    with _tally_cache_lock:
        cached: dict[str, Any] | None = _tally_cache["value"]
        if cached is not None and now < _tally_cache["expires"]:
            return cached
    fresh = tally()
    with _tally_cache_lock:
        _tally_cache["value"] = fresh
        _tally_cache["expires"] = now + _TALLY_TTL_SECONDS
    return fresh


def reset_tally_cache() -> None:
    """Drop the cached tally. Tests call this between cases; ``configure``
    calls it too, since a runtime-dir change makes any cached tally stale."""
    with _tally_cache_lock:
        _tally_cache["value"] = None
        _tally_cache["expires"] = 0.0
