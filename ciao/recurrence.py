"""Pure recurrence verdict for a repeated procedure (#3238-A).

A procedure recurs when several *independent* completions of it are on
record. This module decides that from evidence the caller has already
resolved; it does no I/O, takes no locks and never raises on odd input.

The caller builds ``resolved`` from the engine's lookup of persisted
completions. A ``None`` entry, or a missing key, means the completion id was
invented, unreadable or belongs to another workspace.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

RECURRENCE_THRESHOLD = 3


@dataclass(frozen=True, slots=True)
class Outcome:
    """One persisted completion and the task it belongs to."""

    completion_id: str
    task_id: str


@dataclass(frozen=True, slots=True)
class RecurrenceVerdict:
    """Result of :func:`classify_recurrence`.

    ``excluded`` holds ``(completion_id, reason)`` pairs in first-appearance
    order. ``reasons`` names why the status was reached.
    """

    status: Literal["below", "met", "unavailable"]
    supported: tuple[Outcome, ...]
    excluded: tuple[tuple[str, str], ...]
    reasons: tuple[str, ...]


def classify_recurrence(
    claimed_ids: Sequence[str],
    resolved: Mapping[str, Outcome | None],
    *,
    threshold: int = RECURRENCE_THRESHOLD,
) -> RecurrenceVerdict:
    """Count independent outcomes among the claimed completion ids.

    Exact repeats fold to one. Completions sharing a task fold to the first
    one seen (``same_task``), because a retry or replay of one task is one
    outcome. ``unavailable`` means no claimed id resolved to a valid outcome,
    so a failed lookup is never read as ``below``.
    """
    if threshold < 1:
        raise ValueError(f"threshold must be at least 1, got {threshold}")

    supported: list[Outcome] = []
    excluded: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    seen_tasks: set[str] = set()
    any_resolved = False

    for item in claimed_ids:
        if not isinstance(item, str):
            excluded.append((f"<{type(item).__name__}>", "invalid"))
            continue
        if item in seen_ids:
            continue
        seen_ids.add(item)
        if not item.strip():
            excluded.append((item, "blank"))
            continue

        outcome = resolved.get(item)
        if outcome is None:
            excluded.append((item, "unsupported"))
            continue
        if (
            not isinstance(outcome, Outcome)
            or not isinstance(outcome.task_id, str)
            or not outcome.task_id.strip()
        ):
            excluded.append((item, "invalid"))
            continue

        any_resolved = True
        if outcome.task_id in seen_tasks:
            excluded.append((item, "same_task"))
            continue
        seen_tasks.add(outcome.task_id)
        supported.append(outcome)

    if not any_resolved:
        status: Literal["below", "met", "unavailable"] = "unavailable"
        reasons: tuple[str, ...] = ("no_resolved_claims",)
    elif len(supported) >= threshold:
        status = "met"
        reasons = ("threshold_met",)
    else:
        status = "below"
        reasons = ("below_threshold",)

    return RecurrenceVerdict(
        status=status,
        supported=tuple(supported),
        excluded=tuple(excluded),
        reasons=reasons,
    )
