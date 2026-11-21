"""The derived outcome of a schedule's last care run, for the chat that ran it.

Pure: reads one ``last_run`` mapping from the curation state, the chat id the
schedule row names as its latest run, and the row's dispatch time. It returns an
outcome only when all of these hold:

- the schedule is a Workspace care routine (:func:`is_care_schedule_id`), the
  same rule the PWA applies before it asks;
- the stored run carries the derived counts (``run_id``, ``failed`` and
  ``counts_complete`` are written by ``curation-end``, absent otherwise);
- the stored run names that chat in ``chat_id``;
- the stored run finished at or after the row's dispatch time, so an outcome
  from an earlier run is never shown for a later dispatch.

Trust boundary: the ``chat_id`` binding is taken from the agent's
``CIAO_CHAT_ID`` environment and ``--holder`` is free text. Any process that can
run ``ciao curation-begin``/``curation-end`` against the vault can therefore
claim another chat's id, and the dispatch-time bound only stops an outcome from
*before* the run. A run by another chat that finishes after the scheduled run
started can still overwrite it. That is accepted: whoever can write the vault
is already trusted with it. What this view yields is a status (counts, reasons
and whether the counts are complete), never the content of any note.

Nothing is inferred from prose, from a newer schedule row or from timestamps
other than the two named above. A missing dispatch time is ``None``, not a
guess.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

MAX_REASONS = 10
MAX_REASON_CHARS = 200
MAX_RUN_ID_CHARS = 200

_COUNT_FIELDS = ("planned", "completed", "deferred", "failed")
_CARE_ROUTINE = "system-memory-curation"


def is_care_schedule_id(schedule_id: str) -> bool:
    """Whether a schedule id is the nightly care routine or one of its per-workspace copies.

    The PWA's ``isCareSchedule`` applies the same rule; keep the two in step.
    """
    if schedule_id == _CARE_ROUTINE:
        return True
    prefix = f"{_CARE_ROUTINE}@"
    return schedule_id.startswith(prefix) and len(schedule_id) > len(prefix)


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def outcome_for_chat(
    last_run: Mapping[str, Any],
    chat_id: str,
    *,
    dispatched_at: datetime | None,
) -> dict[str, Any] | None:
    """The bounded outcome of ``last_run`` when it belongs to ``chat_id`` and is not older than the dispatch."""
    if not chat_id or not last_run or last_run.get("chat_id") != chat_id:
        return None
    if "failed" not in last_run or "counts_complete" not in last_run or "run_id" not in last_run:
        return None
    if dispatched_at is None:
        return None
    finished = _instant(last_run.get("finished_at"))
    if finished is None or finished < dispatched_at:
        return None
    reasons = [str(reason)[:MAX_REASON_CHARS] for reason in last_run.get("reasons", [])]
    return {
        "run_id": str(last_run["run_id"])[:MAX_RUN_ID_CHARS],
        **{field: int(last_run.get(field, 0)) for field in _COUNT_FIELDS},
        "counts_complete": bool(last_run["counts_complete"]),
        "reasons": reasons[:MAX_REASONS],
    }
