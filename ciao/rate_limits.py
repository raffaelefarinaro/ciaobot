"""Per-bucket rate-limit persistence for Claude subscription quotas.

The Claude Agent SDK emits ``RateLimitEvent`` once per turn carrying
information for a *single* bucket (5-hour, 7-day plan-wide, 7-day Opus,
7-day Sonnet, or overage). A single event doesn't tell you the full
picture — to show all buckets in the PWA we have to remember the last
value we saw for each one and expose them together.

This module owns that remembered state. Keeps it simple: JSON on disk
at ``.runtime/rate_limits.json``, one dict per ``rate_limit_type``, plus
a ``last_updated`` timestamp for the UI to say "as of 2 minutes ago".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FILENAME = "rate_limits.json"

# Declare the buckets explicitly so the UI always renders a row per bucket
# once we have data for it. The SDK's rate_limit_type values come from
# claude_agent_sdk.types.RateLimitInfo.
KNOWN_BUCKETS: tuple[str, ...] = (
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "overage",
)


@dataclass(slots=True)
class RateLimitStore:
    """JSON-backed dict of {bucket_type: snapshot}."""

    path: Path

    def load(self) -> dict[str, Any]:
        """Return the full payload: ``{buckets: {...}, last_updated: iso8601}``.

        Returns an empty-but-well-formed payload if the file is missing
        or corrupt so the frontend never has to special-case "uninitialized".
        """
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"buckets": {}, "last_updated": None}

    def update(self, rate_limit_info: Any) -> None:
        """Record a RateLimitInfo snapshot under its ``rate_limit_type`` key.

        Silent on missing attributes so an SDK schema change never breaks
        the live chat pipeline — the worst case is a stale card in Settings.
        """
        rate_limit_type = getattr(rate_limit_info, "rate_limit_type", None)
        if not rate_limit_type:
            return
        snapshot = _snapshot(rate_limit_info)
        payload = self.load()
        buckets = payload.setdefault("buckets", {})
        buckets[str(rate_limit_type)] = snapshot
        payload["last_updated"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("failed to persist rate_limits.json")


def _snapshot(info: Any) -> dict[str, Any]:
    """Extract a JSON-safe snapshot of one RateLimitInfo."""
    snapshot: dict[str, Any] = {
        "status": _maybe_str(getattr(info, "status", None)),
        "utilization": _maybe_float(getattr(info, "utilization", None)),
        "resets_at": _maybe_int(getattr(info, "resets_at", None)),
        "overage_status": _maybe_str(getattr(info, "overage_status", None)),
        "overage_resets_at": _maybe_int(getattr(info, "overage_resets_at", None)),
        "overage_disabled_reason": _maybe_str(
            getattr(info, "overage_disabled_reason", None)
        ),
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    return snapshot


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def default_store_path(runtime_root: Path) -> Path:
    return runtime_root / _FILENAME
