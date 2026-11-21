"""Whether tonight's Workspace care has observed work, decided without a model.

The nightly care run is a model turn. On a quiet vault that turn only learns
that there is nothing to do, and costs a full prompt to say so. This module
is the read-only answer to "is there anything to do", computed from the same
worklist ``curation-begin`` would plan. It takes no lease, writes nothing,
reads no clock and never touches the scheduler.

The rules are applied in order, and each one fails toward the run that already
happens today: only :class:`Empty` lets a caller skip the chat. Every other
verdict prints or exits non-zero, which the schedule treats as "run the chat as
before". So the verdict can remove a no-op night, never hide a busy, unreadable
or interrupted one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from ciao.curation_run import (
    SKIPPED_PASS_NOTE_PREFIXES,
    SKIPPED_PASS_NOTES,
    RunPlan,
    Worklist,
)

REASON_EMPTY = "no observed care work this run"
REASON_DUE = "care work is due"
REASON_BUSY = "a run holds the lease"
REASON_STATE_UNREADABLE = "curation state is unreadable; the care verdict is unavailable"
REASON_CATEGORY_SCAN = "the category scan did not complete; the care verdict is unavailable"
REASON_PASS_SKIPPED = "a worklist pass did not run this night; the care verdict is unavailable"


@dataclass(frozen=True, slots=True)
class Empty:
    """Nothing observed to do: the night needs no chat."""

    kind: ClassVar[str] = "empty"
    reason: str = REASON_EMPTY


@dataclass(frozen=True, slots=True)
class Due:
    """Observed work exists. ``planned_count`` is the work this run would take.

    Category candidates count once each. A candidate that is already queued is
    still counted, so for those the figure is an upper bound.
    """

    planned_count: int
    kind: ClassVar[str] = "due"
    reason: str = REASON_DUE


@dataclass(frozen=True, slots=True)
class Busy:
    """A live run holds the vault's lease. Never reported as empty."""

    kind: ClassVar[str] = "busy"
    reason: str = REASON_BUSY


@dataclass(frozen=True, slots=True)
class Unavailable:
    """The verdict cannot be trusted. ``reason`` is one of the fixed REASON_ strings."""

    reason: str
    kind: ClassVar[str] = "unavailable"


Verdict = Empty | Due | Busy | Unavailable


def verdict_payload(verdict: Verdict) -> dict[str, Any]:
    """The verdict as the JSON object ``ciao curation-due --json`` prints."""
    payload: dict[str, Any] = {"verdict": verdict.kind, "reason": verdict.reason}
    if isinstance(verdict, Due):
        payload["planned_count"] = verdict.planned_count
    return payload


def _pass_skipped(worklist: Worklist) -> bool:
    """True when a pass did not run, or the stale-note scan did not complete.

    Matched on the exact note strings the builder emits, not on prose. Notes
    that report work deferred to a later run are not skips and do not count.
    """
    coverage = worklist.note_coverage
    if coverage is not None and coverage.status == "unavailable":
        return True
    return any(
        note in SKIPPED_PASS_NOTES or note.startswith(SKIPPED_PASS_NOTE_PREFIXES)
        for note in worklist.notes
    )


def care_due_verdict(
    worklist: Worklist | None,
    *,
    plan: RunPlan | None,
    lease: Mapping[str, Any] | None,
    category_candidates: Sequence[Mapping[str, Any]] | None,
    state_error: str = "",
) -> Verdict:
    """Decide whether tonight's care run has observed work.

    ``worklist`` is the remaining worklist ``curation-begin`` would plan, and
    ``plan`` is its budgeted plan. ``lease`` is the live lease, or None.
    ``category_candidates`` is the read-only category scan, where None means the
    scan failed. ``worklist`` and ``plan`` may be None only when the state could
    not be read, which ``state_error`` names.
    """
    if state_error or worklist is None or plan is None:
        return Unavailable(REASON_STATE_UNREADABLE)
    if lease:
        return Busy()
    if category_candidates is None:
        return Unavailable(REASON_CATEGORY_SCAN)
    # The category pass files its rows under the lease, so a candidate the
    # plan does not see yet is work the run would do. The scan is conservative
    # here: a candidate already queued also shows up as a proposal row, which
    # the worklist plans anyway.
    if category_candidates:
        return Due(plan.planned_count + len(category_candidates))
    if _pass_skipped(worklist):
        return Unavailable(REASON_PASS_SKIPPED)
    if worklist.empty and not plan.blocked:
        return Empty()
    # Budget-deferred work and blocked oversize logs are observed work too:
    # the run has to be told about them, so they are not a quiet night.
    return Due(plan.planned_count)
