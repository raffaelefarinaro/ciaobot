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
* ``budget_continuation`` is refused here for the same reason.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime

from ciao.care_eval_record import build_record
from ciao.care_eval_run import (
    BoundEngine,
    ScheduleRunRequest,
    ScheduleRunOutcome,
    Settled,
    run_schedule,
)
from ciao.care_eval_seed import (
    GUIDE_RELATIVE,
    DispatchClock,
    Layout,
    SeedManifest,
    observe,
)
from ciao.task_eval_contract import MeasurementRecord, Scenario

ORCHESTRATED: tuple[Scenario, ...] = ("nightly", "weekly_due")


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
    if scenario not in ORCHESTRATED:
        raise ValueError(f"{scenario} is not orchestrated here (see decision D1)")
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
        observed=observed,
        wall_s=wall_s,
        usage=None,
        code_revision=code_revision,
        dirty=dirty,
    )
