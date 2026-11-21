"""The derived outcome of a schedule's last care run, for the chat that ran it.

Pure: reads one ``last_run`` mapping from the curation state and the chat id the
schedule row names. It returns an outcome only when the stored run was taken by
that chat and carries the derived counts (``run_id``, ``failed`` and
``counts_complete`` are written by ``curation-end`` and absent otherwise). A run
that another chat owns, or a run with no counts, is ``None``: nothing is
inferred from timestamps, prose or a newer schedule row.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_REASONS = 10
MAX_REASON_CHARS = 200

_COUNT_FIELDS = ("planned", "completed", "deferred", "failed")


def outcome_for_chat(last_run: Mapping[str, Any], chat_id: str) -> dict[str, Any] | None:
    """The bounded outcome of ``last_run`` when it belongs to ``chat_id``, else None."""
    if not chat_id or not last_run or last_run.get("chat_id") != chat_id:
        return None
    if "failed" not in last_run or "counts_complete" not in last_run or "run_id" not in last_run:
        return None
    reasons = [str(reason)[:MAX_REASON_CHARS] for reason in last_run.get("reasons", [])]
    return {
        "run_id": str(last_run["run_id"]),
        **{field: int(last_run.get(field, 0)) for field in _COUNT_FIELDS},
        "counts_complete": bool(last_run["counts_complete"]),
        "reasons": reasons[:MAX_REASONS],
    }
