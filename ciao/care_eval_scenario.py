"""One care scenario run, end to end, against an engine the caller owns.

``run_scenario`` sequences the pieces the other care modules provide: it checks
that the seed is on disk, triggers the system schedule through
:func:`ciao.care_eval_run.run_schedule`, observes the files the run left behind
only when the run settled, grades them, and assembles the record through
:func:`ciao.care_eval_record.build_record`.

It never boots, stops or installs an engine (the caller owns that and passes a
``BoundEngine`` from ``bind_isolated``), never calls a model, and leaves the
schedule paused as the driver does. Nothing here is wired into the nightly
schedule.

Deviations from the first brief, decided here:

* The seed is not built inside this function. ``care_eval_boot.boot_care``
  writes it into the overlay before the engine copies it into the workspace,
  and ``build_seed`` refuses to overwrite a file. This function verifies that
  the booted layout carries exactly the manifest's files instead.
* The run holder id is not read. The obligations of ``nightly`` and
  ``weekly_due`` are never effect counts, so nothing consumes it yet. It is
  needed only when ``budget_continuation`` is orchestrated (decision D1).
* ``budget_continuation`` is not a single run. ``run_continuation`` drives two
  attributable attempts (decision D1): attempt 1 must settle incomplete, its
  cursor is observed twice and must agree, and attempt 2 must resume from it.
  Only attempt 2 is graded; attempt 1 is never published. ``run_scenario``
  refuses the scenario and points there.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ciao.care_eval_record import build_record
from ciao.care_eval_run import (
    BoundEngine,
    ScheduleRunOutcome,
    ScheduleRunRequest,
    Settled,
    run_schedule,
)
from ciao.care_eval_seed import (
    GUIDE_RELATIVE,
    DispatchClock,
    LastRun,
    Layout,
    SeedManifest,
    observe,
    observe_cursor,
    observe_effects,
    observe_last_run,
)
from ciao.curation_run import RunBudget
from ciao.task_eval_contract import MeasurementRecord, Scenario

ORCHESTRATED: tuple[Scenario, ...] = ("nightly", "weekly_due")

# What ``budget_continuation`` runs under: one item per attempt, so attempt 1 takes one
# key and defers the rest and attempt 2 has something to resume. The harness hands it to
# ``ciao curation-begin`` as a flag through a shim on the engine's PATH
# (``task_eval.IsolatedEngine.boot``); no vault file or product setting carries it.
CONTINUATION_BUDGET = RunBudget(max_items=1)

REASON_ATTEMPT1_NOT_SETTLED = "attempt 1 did not settle"
REASON_ATTEMPT1_COMPLETED = "attempt 1 completed; no work was left for attempt 2"
REASON_NO_CURSOR_PROGRESS = "attempt 1 recorded no cursor progress"
REASON_CURSOR_UNSTABLE = "cursor is unreadable or two reads disagree"
REASON_ATTEMPT2_NOT_SETTLED = "attempt 2 did not settle"
REASON_ATTEMPT2_SAME_RUN = "attempt 2 shares attempt 1's run id"
REASON_ATTEMPT2_NO_START = "attempt 2 start time is unreadable"
REASON_ATTEMPT2_NO_PROGRESS = "attempt 2 recorded no new cursor work"
REASON_ATTEMPT2_RESTARTED = "attempt 2 restarted from scratch"
REASON_RESUME_UNVERIFIED = "attempt 2 resume could not be verified"


@dataclass(frozen=True)
class Unavailable:
    """The scenario has no successful measurement; ``reason`` is one of the fixed REASON_ strings."""

    reason: str


ContinuationResult = MeasurementRecord | Unavailable


@dataclass(frozen=True)
class _Cursor:
    done: frozenset[str]
    last_run: LastRun


def _verify_seed(layout: Layout, manifest: SeedManifest) -> None:
    for entry in manifest.files:
        root = layout.agent_root if entry.path == GUIDE_RELATIVE else layout.vault_root
        target = root / entry.path
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise ValueError("seed on disk does not match the manifest") from exc
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ValueError("seed on disk does not match the manifest")


def _dispatch_clock(outcome: Settled) -> DispatchClock | None:
    try:
        return DispatchClock(started_at=datetime.fromisoformat(outcome.job_run.started_at))
    except ValueError:
        return None


def run_scenario(
    scenario: Scenario,
    bound: BoundEngine,
    layout: Layout,
    manifest: SeedManifest,
    *,
    schedule_id: str,
    workspace: str,
    deadline_s: float,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    code_revision: str | None,
    dirty: bool,
    run_id: str | None = None,
) -> MeasurementRecord:
    """Trigger one system schedule for ``scenario`` and return its measurement record.

    ``clock`` must be monotonic; it supplies both the driver's deadline and the
    wall time, which is reported only for a settled run. Observations are taken
    only for a settled run whose start time parses; otherwise the grade is
    ``none`` and every outcome is unknown.
    """
    if scenario == "budget_continuation":
        raise ValueError("budget_continuation takes two attempts; use run_continuation (decision D1)")
    if scenario not in ORCHESTRATED:
        raise ValueError(f"{scenario} is not orchestrated here")
    if manifest.scenario != scenario:
        raise ValueError("seed manifest is for another scenario")
    _verify_seed(layout, manifest)

    request = ScheduleRunRequest(
        schedule_id=schedule_id, workspace=workspace, deadline_s=deadline_s, poll_s=poll_s
    )
    started = clock()
    outcome: ScheduleRunOutcome = run_schedule(bound, request, clock=clock, sleep=sleep)
    wall_s = clock() - started

    observed = None
    if isinstance(outcome, Settled):
        dispatch = _dispatch_clock(outcome)
        if dispatch is not None:
            observed = observe(scenario, layout, manifest, dispatch)

    return build_record(
        run_id=run_id or str(uuid.uuid4()),
        scenario=scenario,
        outcome=outcome,
        manifest=manifest,
        budget=RunBudget(),
        observed=observed,
        wall_s=wall_s,
        usage=None,
        code_revision=code_revision,
        dirty=dirty,
    )


def _attempt(
    bound: BoundEngine,
    request: ScheduleRunRequest,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[ScheduleRunOutcome, float]:
    started = clock()
    outcome = run_schedule(bound, request, clock=clock, sleep=sleep)
    return outcome, clock() - started


def _read_cursor(vault: Path) -> _Cursor | None:
    observation = observe_cursor(vault)
    last_run = observe_last_run(vault)
    if observation.status != "ok" or last_run is None or last_run.run_id is None:
        return None
    return _Cursor(done=observation.done_keys, last_run=last_run)


def _stable_cursor(vault: Path) -> _Cursor | None:
    """The cursor, read twice; None unless both reads are readable and agree."""
    first = _read_cursor(vault)
    if first is None or _read_cursor(vault) != first:
        return None
    return first


def run_continuation(
    bound: BoundEngine,
    layout: Layout,
    manifest: SeedManifest,
    *,
    schedule_id: str,
    workspace: str,
    deadline_s: float,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    code_revision: str | None,
    dirty: bool,
    run_id: str | None = None,
) -> ContinuationResult:
    """Two attributable attempts of ``budget_continuation``; grade only attempt 2.

    Attempt 1 must settle with work left and a readable, stable cursor. Attempt 2
    must settle, record new cursor work, leave the attempt-1 keys untouched, and
    carry its own run id. Any failed check returns ``Unavailable`` with a fixed
    reason and nothing is graded. Attempt 1 is never returned or published.
    """
    if manifest.scenario != "budget_continuation":
        raise ValueError("seed manifest is for another scenario")
    _verify_seed(layout, manifest)
    vault = Path(layout.vault_root)
    request = ScheduleRunRequest(
        schedule_id=schedule_id, workspace=workspace, deadline_s=deadline_s, poll_s=poll_s
    )

    first_outcome, _ = _attempt(bound, request, clock, sleep)
    if not isinstance(first_outcome, Settled):
        return Unavailable(REASON_ATTEMPT1_NOT_SETTLED)
    first = _stable_cursor(vault)
    if first is None:
        return Unavailable(REASON_CURSOR_UNSTABLE)
    if first.last_run.deferred <= 0:
        return Unavailable(REASON_ATTEMPT1_COMPLETED)
    if not first.done:
        return Unavailable(REASON_NO_CURSOR_PROGRESS)

    second_outcome, wall_s = _attempt(bound, request, clock, sleep)
    if not isinstance(second_outcome, Settled):
        return Unavailable(REASON_ATTEMPT2_NOT_SETTLED)
    after = _stable_cursor(vault)
    if after is None:
        return Unavailable(REASON_CURSOR_UNSTABLE)
    if after.last_run.run_id == first.last_run.run_id:
        return Unavailable(REASON_ATTEMPT2_SAME_RUN)
    if not after.done - first.done:
        return Unavailable(REASON_ATTEMPT2_NO_PROGRESS)
    holder = after.last_run.run_id
    if holder is None:
        return Unavailable(REASON_CURSOR_UNSTABLE)
    effects = observe_effects(vault, holder, sorted(first.done))
    if any(count is None for count in effects.values()):
        return Unavailable(REASON_RESUME_UNVERIFIED)
    if any(count for count in effects.values()):
        return Unavailable(REASON_ATTEMPT2_RESTARTED)

    dispatch = _dispatch_clock(second_outcome)
    if dispatch is None:
        return Unavailable(REASON_ATTEMPT2_NO_START)
    return build_record(
        run_id=run_id or str(uuid.uuid4()),
        scenario="budget_continuation",
        outcome=second_outcome,
        manifest=manifest,
        budget=CONTINUATION_BUDGET,
        observed=observe("budget_continuation", layout, manifest, dispatch),
        wall_s=wall_s,
        usage=None,
        code_revision=code_revision,
        dirty=dirty,
    )
