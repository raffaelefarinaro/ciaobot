"""Assemble one care measurement record from a finished schedule run.

Pure: no I/O, no engine, no git. The caller passes the run's closed outcome
(from :mod:`evals.care_eval_run`), the seed manifest (:mod:`evals.care_eval_seed`),
and the observations read from disk by the seed observers, already translated
to the oracle's symbolic bytes. Nothing here reads a reply, a transcript or a
self-report: the grade is independent only when ``observed`` is supplied.

The result goes through :func:`evals.task_eval_contract.public_record`, the
publication boundary, so no dict is published past it.

Choices made here, recorded so a later reader can see why:

* ``entry`` is always ``"schedule"``: the harness can only trigger schedules, and
  the contract enum has no value for "not run". A non-settled run is recorded
  through ``execution.status`` instead.
* Identity digests (grader, harness) hash a declared name and contract version,
  not source text, so a comment or formatting edit does not fork comparisons.
* ``environment_sha256`` stays null until a builder exists, so comparisons stay
  ``provenance_unknown``.
* Token, call and cost metrics are unknown unless a per-attempt
  :class:`care_usage.CareUsageReport` is supplied. The agent-driven run has none.
* Wall time is the caller's monotonic measurement around ``run_schedule``. It is
  complete only for a settled run; its evidence digest binds the settled job-run
  row so the number is tied to the run that produced it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any, cast

from evals.care_eval_fixtures import CARE_FIXTURES, CareFixture, grade_fixture
from evals.care_eval_run import (
    Ambiguous,
    MissingChat,
    Refused,
    ScheduleRunOutcome,
    Settled,
    Superseded,
    TimedOut,
)
from evals.care_eval_seed import SeedManifest
from evals.care_usage import CareUsageReport, contract_metrics
from ciao.curation_run import RunBudget
from evals.task_eval_contract import (
    METRIC_NAMES,
    SCHEMA,
    MeasurementRecord,
    Metric,
    Scenario,
    public_record,
)

GRADER_NAME = "care-outcome-oracle"
GRADER_CONTRACT = "1"
HARNESS_NAME = "care-measurement-harness"
HARNESS_CONTRACT = "1"

_SETTLED_STATUS = {"ok": "completed", "error": "error", "skipped": "skipped"}
_USAGE_METRICS = tuple(name for name in METRIC_NAMES if name != "wall_s")


def _sha(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity_digest(name: str, contract: str) -> str:
    return _sha({"name": name, "contract": contract})


def grader_digest() -> str:
    return _identity_digest(GRADER_NAME, GRADER_CONTRACT)


def harness_digest() -> str:
    return _identity_digest(HARNESS_NAME, HARNESS_CONTRACT)


def _fixture(scenario: Scenario) -> CareFixture:
    (match,) = [item for item in CARE_FIXTURES if item.scenario == scenario]
    return match


def workload_digest(fixture: CareFixture, budget: RunBudget) -> str:
    """Digest of what the run was asked to do: the scenario, the budget it ran under, its obligations."""
    return _sha({
        "scenario": fixture.scenario,
        "declared_budget": budget.as_dict(),
        "obligations": [[item.id, item.observation_key, _bytes_digest(item.expected_bytes)]
                        for item in fixture.obligations],
        "protected": [[item.id, item.observation_key, _bytes_digest(item.expected_bytes)]
                      for item in fixture.protected],
    })


def _observation_digest(keys: Iterable[str], observed: Mapping[str, bytes | None]) -> str:
    rows: list[list[str]] = []
    for key in sorted(set(keys) | set(observed)):
        if key not in observed:
            rows.append([key, "unknown"])
            continue
        value = observed[key]
        if value is None:
            rows.append([key, "absent"])
        elif isinstance(value, bytes):
            rows.append([key, _bytes_digest(value)])
        else:
            rows.append([key, "invalid"])
    return _sha(rows)


def _status(outcome: ScheduleRunOutcome) -> str:
    if isinstance(outcome, Settled):
        return _SETTLED_STATUS[outcome.status]
    if isinstance(outcome, TimedOut):
        return "timed_out"
    if isinstance(outcome, (Refused, Superseded)):
        return "skipped"
    if isinstance(outcome, (Ambiguous, MissingChat)):
        return "error"
    raise TypeError("unknown schedule-run outcome")


def _unknown(name: str) -> Metric:
    scope = "run" if name == "wall_s" else "all_model_calls"
    return cast(Metric, {"value": None, "status": "unknown", "scope": scope, "source": "none",
                         "evidence_sha256": None, "reason": "not_collected"})


def _wall(outcome: ScheduleRunOutcome, wall_s: float | None) -> Metric:
    if wall_s is None or not isinstance(outcome, Settled):
        return _unknown("wall_s")
    row = outcome.job_run
    evidence = _sha({"dispatch": outcome.dispatch_id,
                     "job_run": [row.started_at, row.ended_at, row.duration_ms]})
    return {"value": wall_s, "status": "complete", "scope": "run", "source": "monotonic_clock",
            "evidence_sha256": evidence, "reason": None}


def _usage(usage: CareUsageReport | None) -> dict[str, Metric]:
    if usage is None:
        return {name: _unknown(name) for name in _USAGE_METRICS}
    projected = contract_metrics(usage)
    return {name: projected[name] for name in _USAGE_METRICS}


def build_record(
    *,
    run_id: str,
    scenario: Scenario,
    outcome: ScheduleRunOutcome,
    manifest: SeedManifest,
    budget: RunBudget,
    observed: Mapping[str, bytes | None] | None,
    wall_s: float | None,
    usage: CareUsageReport | None,
    code_revision: str | None,
    dirty: bool,
) -> MeasurementRecord:
    """One validated record for one run.

    ``observed`` must be the seed observers' output for this run, or ``None``
    when nothing was observed (the grade is then ``none`` and every outcome is
    unknown). ``budget`` is the budget the run actually used, bound into the workload
    digest. ``wall_s`` is the caller's monotonic seconds around the trigger.
    """
    if manifest.scenario != scenario:
        raise ValueError("seed manifest is for another scenario")
    fixture = _fixture(scenario)
    obligation_ids = sorted(item.id for item in fixture.obligations)
    workload = workload_digest(fixture, budget)
    grader = grader_digest()
    if observed is None:
        grade: dict[str, Any] = {
            "source": "none", "run_id": None, "fixture_sha256": None, "workload_sha256": None,
            "grader_sha256": None, "observation_sha256": None,
            "outcomes": dict.fromkeys(obligation_ids, "unknown"), "safety": "unknown",
        }
    else:
        outcomes, safety = grade_fixture(fixture, observed)
        keys = [item.observation_key for item in (*fixture.obligations, *fixture.protected)]
        grade = {
            "source": "independent", "run_id": run_id, "fixture_sha256": manifest.fixture_sha256,
            "workload_sha256": workload, "grader_sha256": grader,
            "observation_sha256": _observation_digest(keys, observed),
            "outcomes": outcomes, "safety": safety,
        }
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "scenario": scenario,
        "entry": "schedule",
        "execution": {"status": _status(outcome), "code_revision": code_revision, "dirty": dirty},
        "provenance": {
            "fixture_sha256": manifest.fixture_sha256, "workload_sha256": workload,
            "environment_sha256": None, "harness_sha256": harness_digest(), "grader_sha256": grader,
        },
        "obligations": obligation_ids,
        "grade": grade,
        "metrics": {**_usage(usage), "wall_s": _wall(outcome, wall_s)},
    }
    return public_record(record)
