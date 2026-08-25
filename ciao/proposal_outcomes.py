"""Recorder for memory-proposal resolution outcomes.

Job runs say whether the extraction pipeline *ran*; this log says whether its
output was *useful*: every time a queued proposal leaves the review queue as a
decision — promoted (accepted into its destination) or dismissed — one JSON
line lands in ``.runtime/proposal_outcomes.jsonl``::

    {"ts": "...", "workspace": "personal", "kind": "memory",
     "action": "promoted", "via": "pwa"}

``via`` records which surface made the decision: the PWA routes or the
``ciao memory-proposal-dismiss`` CLI the nightly curation agent drives.

Everything mirrors :mod:`ciao.job_runs`: an append-only, size-trimmed JSONL
under ``.runtime``, written strictly best-effort so a recording failure can
never break the resolution it is describing, plus a fail-open aggregator
(:func:`tally`) that folds the log into the counts the Automation page shows.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROPOSAL_OUTCOMES_NAME = "proposal_outcomes.jsonl"
ACTIONS: tuple[str, ...] = ("promoted", "dismissed")
MAX_BYTES = 2 * 1024 * 1024  # trim the log once it passes ~2 MB
KEEP_LINES = 2000            # lines retained after a trim

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
    vanish from every count while claiming to be recorded.
    """
    if action not in ACTIONS:
        logger.debug("Refusing to record proposal outcome with action %r", action)
        return
    try:
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


def _trim_if_large(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < MAX_BYTES:
            return
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        with path.open("w", encoding="utf-8") as f:
            f.writelines(lines[-KEEP_LINES:])
    except Exception:  # noqa: BLE001
        logger.debug("Failed to trim proposal-outcome log", exc_info=True)


def tally(*, now: datetime | None = None) -> dict[str, Any]:
    """Fold the whole outcome log into the Automation-page counts.

    Returns ``{"promoted": n, "dismissed": m, "by_workspace": {name:
    {"promoted": n, "dismissed": m}}, "recent_30d": {"promoted": n,
    "dismissed": m}}``. Malformed lines are skipped, never fatal; an event
    with an unreadable timestamp still counts in the totals, just not in the
    30-day window. Never raises: a broken log degrades to zeros rather than
    failing the page that reads it.
    """
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff = reference - timedelta(days=RECENT_DAYS)

    totals = {"promoted": 0, "dismissed": 0}
    recent = {"promoted": 0, "dismissed": 0}
    by_workspace: dict[str, dict[str, int]] = {}

    def bucket(name: str) -> dict[str, int]:
        return by_workspace.setdefault(name, {"promoted": 0, "dismissed": 0})

    try:
        path = _log_path()
        if not path.exists():
            return {
                "promoted": 0,
                "dismissed": 0,
                "by_workspace": {},
                "recent_30d": dict(recent),
            }
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
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
                ts = _parsed_ts(event.get("ts"))
                if ts is not None and ts >= cutoff:
                    recent[action] += 1  # type: ignore[literal-required]
    except Exception:  # noqa: BLE001 — a broken log is zeros, not a failed page
        logger.debug("Failed to tally proposal outcomes", exc_info=True)

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
    cached = _tally_cache["value"]
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
