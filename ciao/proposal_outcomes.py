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
When the log rotates, the dropped lines are folded into a sidecar totals file
(``proposal_outcomes_totals.json``) so lifetime counts survive trimming.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROPOSAL_OUTCOMES_NAME = "proposal_outcomes.jsonl"
ACTIONS: tuple[str, ...] = ("promoted", "dismissed")
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
    regardless of the process cwd. Tests can point it at a temp dir."""
    global _runtime_dir_override
    _runtime_dir_override = Path(runtime_dir)


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
    vanish from every count while claiming to be recorded. The append and the
    size-triggered rotation share an inter-process lock, so a rotation can
    never rewrite away an event another process appended between its read and
    its write.
    """
    if action not in ACTIONS:
        logger.debug("Refusing to record proposal outcome with action %r", action)
        return
    try:
        with _writer_lock():
            path = _log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": datetime.now(UTC).isoformat(),
                "workspace": str(workspace or ""),
                "kind": str(kind or ""),
                "action": action,
                "via": str(via or ""),
            }
            with path.open("a", encoding="utf-8") as f:
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

    Every mutation (append + trim + sidecar update) runs under the exclusive
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
        fh = lock_path.open("a")
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
        # Rotation must not rewrite history: the lifetime totals the Settings
        # page shows are carried in a sidecar that absorbs whatever this trim
        # drops, so tally() = sidecar + what is still in the file.
        _absorb_dropped_into_totals(dropped)
        with path.open("w", encoding="utf-8") as f:
            f.writelines(kept)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to trim proposal-outcome log", exc_info=True)


_TOTALS_NAME = "proposal_outcomes_totals.json"

# Day-bucket keys live here so a rotation that drops events younger than
# :data:`RECENT_DAYS` keeps contributing to ``recent_30d``; buckets older than
# the window are pruned on write.
_RECENT_KEEP_DAYS = RECENT_DAYS + 1


def _totals_path() -> Path:
    return _runtime_dir() / _TOTALS_NAME


def _empty_totals() -> dict[str, Any]:
    return {"promoted": 0, "dismissed": 0, "by_workspace": {}, "days": {}}


def _prune_days(days: dict[str, Any], reference: datetime) -> dict[str, Any]:
    cutoff_day = (reference - timedelta(days=_RECENT_KEEP_DAYS)).date()
    kept: dict[str, Any] = {}
    for day, counts in days.items():
        try:
            day_date = date.fromisoformat(str(day))
        except ValueError:
            continue
        if day_date >= cutoff_day and isinstance(counts, dict):
            kept[str(day)] = counts
    return kept


def _read_totals(reference: datetime | None = None) -> dict[str, Any]:
    reference = reference or datetime.now(UTC)
    try:
        raw = _totals_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — missing/corrupt totals degrade to zeros
        return _empty_totals()
    if not isinstance(data, dict):
        return _empty_totals()
    totals = _empty_totals()
    for action in ACTIONS:
        value = data.get(action)
        totals[action] = value if isinstance(value, int) and value >= 0 else 0
    by_workspace = data.get("by_workspace")
    if isinstance(by_workspace, dict):
        for name, counts in by_workspace.items():
            if not isinstance(counts, dict):
                continue
            bucket = totals["by_workspace"].setdefault(
                str(name), {"promoted": 0, "dismissed": 0}
            )
            for action in ACTIONS:
                value = counts.get(action)
                bucket[action] = value if isinstance(value, int) and value >= 0 else 0
    days = data.get("days")
    if isinstance(days, dict):
        totals["days"] = _prune_days(days, reference)
    return totals


def _event_day(raw: Any, reference: datetime) -> str | None:
    """The UTC calendar day of an event's timestamp, for the day buckets."""
    parsed = _parsed_ts(raw)
    if parsed is None:
        return None
    return parsed.astimezone(UTC).date().isoformat()


def _absorb_dropped_into_totals(
    dropped_lines: list[str], reference: datetime | None = None
) -> None:
    """Fold the lines a rotation is about to drop into the sidecar totals.

    Best-effort: it runs only inside the trim, immediately before the file is
    rewritten without those lines, and under the same lock as every writer.
    Lifetime counts and the recent-window day buckets are both carried, so a
    rotation cannot make history — recent or total — disappear.
    """
    reference = reference or datetime.now(UTC)
    totals = _read_totals(reference)
    for line in dropped_lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("action") not in ACTIONS:
            continue
        action = event["action"]
        workspace = str(event.get("workspace") or "")
        totals[action] += 1  # type: ignore[literal-required]
        bucket = totals["by_workspace"].setdefault(
            workspace, {"promoted": 0, "dismissed": 0}
        )
        bucket[action] += 1  # type: ignore[literal-required]
        day = _event_day(event.get("ts"), reference)
        if day is not None:
            day_bucket = totals["days"].setdefault(day, {"promoted": 0, "dismissed": 0})
            day_bucket[action] += 1  # type: ignore[literal-required]
    totals["days"] = _prune_days(totals["days"], reference)
    tmp = _totals_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(totals, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_totals_path())


def tally(*, now: datetime | None = None) -> dict[str, Any]:
    """Fold the outcome log into the Automation-page counts.

    Returns ``{"promoted": n, "dismissed": m, "by_workspace": {name:
    {"promoted": n, "dismissed": m}}, "recent_30d": {"promoted": n,
    "dismissed": m}}``. Lifetime totals are the rotation sidecar plus whatever
    is still in the log, so trimming never makes history disappear, and
    ``recent_30d`` adds the sidecar's per-day buckets — a rotation that drops
    still-recent events cannot make the 30-day count fall either.
    Malformed lines are skipped, never fatal; an event
    with an unreadable timestamp still counts in the totals, just not in the
    30-day window. Never raises: a broken log degrades to zeros rather than
    failing the page that reads it.
    """
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff_day = (reference - timedelta(days=RECENT_DAYS)).date()

    totals = _read_totals(reference)
    by_workspace: dict[str, dict[str, int]] = {
        name: dict(counts) for name, counts in totals["by_workspace"].items()
    }
    recent = {"promoted": 0, "dismissed": 0}

    def bucket(name: str) -> dict[str, int]:
        return by_workspace.setdefault(name, {"promoted": 0, "dismissed": 0})

    lines: list[str] = []
    try:
        with _writer_lock(exclusive=False):
            path = _log_path()
            if path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
    except Exception:  # noqa: BLE001 — a broken log is zeros, not a failed page
        logger.debug("Failed to tally proposal outcomes", exc_info=True)
        lines = []

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
        workspace = str(event.get("workspace") or "")
        totals[action] += 1  # type: ignore[literal-required]
        bucket(workspace)[action] += 1  # type: ignore[literal-required]
        # Live-file rows keep full timestamp precision; only ROTATED events
        # fall back to day granularity (they live in undated-per-event
        # buckets).
        ts = _parsed_ts(event.get("ts"))
        if ts is not None and ts.astimezone(UTC) >= reference - timedelta(days=RECENT_DAYS):
            recent[action] += 1  # type: ignore[literal-required]

    # Recent events carried by the rotation sidecar count toward the window
    # too; buckets older than the window were pruned when the sidecar was
    # written.
    for day, counts in totals.get("days", {}).items():
        if date.fromisoformat(str(day)) >= cutoff_day:
            for action in ACTIONS:
                value = counts.get(action)
                if isinstance(value, int) and value > 0:
                    recent[action] += value

    return {
        "promoted": totals["promoted"],
        "dismissed": totals["dismissed"],
        "by_workspace": by_workspace,
        "recent_30d": recent,
    }


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


def tally_cached() -> dict[str, Any]:
    """Tally through the short TTL cache. Never raises."""
    now = time.monotonic()
    cached: dict[str, Any] | None = _tally_cache["value"]
    if cached is not None and now < _tally_cache["expires"]:
        return cached
    fresh = tally()
    _tally_cache["value"] = fresh
    _tally_cache["expires"] = now + _TALLY_TTL_SECONDS
    return fresh


def reset_tally_cache() -> None:
    """Drop the cached tally. Tests call this between cases."""
    _tally_cache["value"] = None
    _tally_cache["expires"] = 0.0
