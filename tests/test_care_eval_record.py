from __future__ import annotations

import copy
import dataclasses
import hashlib
import inspect
import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ciao import care_eval_record as rec
from ciao import care_eval_seed as seed
from ciao.care_eval_fixtures import CARE_FIXTURES, CareFixture
from ciao.care_eval_run import (
    Ambiguous,
    JobRunRef,
    MissingChat,
    Refused,
    Settled,
    Superseded,
    TimedOut,
)
from ciao.care_usage import Inventory, collect, contract_metrics
from ciao.curation_run import RunBudget
from ciao.task_eval_contract import METRIC_NAMES, comparison_eligibility, public_record

DISPATCH_DATE = date(2026, 11, 24)
SCENARIOS = ("nightly", "weekly_due", "budget_continuation")
RUN_ID = str(uuid.UUID("12345678-1234-5678-1234-567812345678"))
DISPATCH_ID = "dispatch-probe-7f3a9c"
CHAT_ID = "chat-probe-41b2e0"


def fixture_for(scenario: str) -> CareFixture:
    (match,) = [item for item in CARE_FIXTURES if item.scenario == scenario]
    return match


def seeded(base: Path, scenario: str = "nightly") -> seed.SeedManifest:
    layout = seed.Layout(agent_root=base / "personal", vault_root=base / "personal" / "memory-vault")
    return seed.build_seed(scenario, layout, dispatch_date=DISPATCH_DATE)


def full_observation(scenario: str) -> dict[str, bytes | None]:
    # The values the oracle expects, as observe() would translate the real effects.
    return {item.observation_key: item.expected_bytes for item in fixture_for(scenario).obligations} | {
        item.observation_key: item.expected_bytes for item in fixture_for(scenario).protected
    }


def settled(status: str = "ok") -> Settled:
    job = JobRunRef(started_at="2026-11-24T09:00:00Z", ended_at="2026-11-24T09:03:00Z", duration_ms=180_000)
    return Settled(status=status, dispatch_id=DISPATCH_ID, chat_id=CHAT_ID, job_run=job)


def build(tmp_path: Path, scenario: str = "nightly", **overrides: Any) -> dict[str, Any]:
    manifest = seeded(tmp_path, scenario)
    fields: dict[str, Any] = {
        "run_id": RUN_ID,
        "scenario": scenario,
        "outcome": settled(),
        "manifest": manifest,
        "budget": RunBudget(),
        "observed": full_observation(scenario),
        "wall_s": 180.25,
        "usage": None,
        "code_revision": "a" * 40,
        "dirty": False,
    }
    fields.update(overrides)
    return fields


def outcome_cases() -> list[Any]:
    return [
        settled("ok"),
        settled("error"),
        settled("skipped"),
        TimedOut(dispatch_id=DISPATCH_ID),
        Refused(reason="schedule is paused or busy"),
        Ambiguous(reason="two settled rows match the dispatch"),
        Superseded(),
        MissingChat(),
    ]


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("outcome_index", range(len(outcome_cases())))
def test_every_scenario_and_outcome_validates_through_public_record(
    tmp_path: Path, scenario: str, outcome_index: int
) -> None:
    outcome = outcome_cases()[outcome_index]
    record = rec.build_record(**build(tmp_path, scenario, outcome=outcome, observed=None, wall_s=None))
    assert public_record(record) == record
    assert set(record) == {"schema", "run_id", "scenario", "entry", "execution", "provenance",
                           "obligations", "grade", "metrics"}
    assert set(record["metrics"]) == set(METRIC_NAMES)


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (settled("ok"), "completed"),
        (settled("error"), "error"),
        (settled("skipped"), "skipped"),
        (TimedOut(dispatch_id=DISPATCH_ID), "timed_out"),
        (Refused(reason="schedule is paused or busy"), "skipped"),
        (Superseded(), "skipped"),
        (Ambiguous(reason="job run log unreadable"), "error"),
        (MissingChat(), "error"),
    ],
)
def test_execution_status_maps_each_outcome(tmp_path: Path, outcome: Any, status: str) -> None:
    record = rec.build_record(**build(tmp_path, outcome=outcome))
    assert record["execution"] == {"status": status, "code_revision": "a" * 40, "dirty": False}


def test_entry_is_schedule_because_the_harness_only_triggers_schedules(tmp_path: Path) -> None:
    for index, outcome in enumerate(outcome_cases()):
        record = rec.build_record(**build(tmp_path / str(index), outcome=outcome))
        assert record["entry"] == "schedule"


def test_build_record_takes_no_reply_or_self_report_input() -> None:
    names = set(inspect.signature(rec.build_record).parameters)
    assert names.isdisjoint({"reply", "report", "grade", "self_report", "transcript", "text"})


def test_without_independent_observations_the_grade_is_none_and_unknown(tmp_path: Path) -> None:
    record = rec.build_record(**build(tmp_path, observed=None))
    grade = record["grade"]
    assert grade["source"] == "none"
    assert grade["run_id"] is None and grade["observation_sha256"] is None
    assert grade["safety"] == "unknown"
    assert set(grade["outcomes"].values()) == {"unknown"}
    assert set(grade["outcomes"]) == set(record["obligations"])


def test_observed_values_grade_independently_and_bind_run_and_workload(tmp_path: Path) -> None:
    record = rec.build_record(**build(tmp_path))
    grade = record["grade"]
    assert grade["source"] == "independent"
    assert grade["run_id"] == RUN_ID
    assert grade["fixture_sha256"] == record["provenance"]["fixture_sha256"]
    assert grade["workload_sha256"] == record["provenance"]["workload_sha256"]
    assert grade["grader_sha256"] == record["provenance"]["grader_sha256"]
    assert set(grade["outcomes"].values()) == {"passed"} and grade["safety"] == "passed"


def test_missing_observation_stays_unknown_and_explicit_absence_fails(tmp_path: Path) -> None:
    observed = full_observation("nightly")
    observed.pop("pending_proposal_disposition")
    observed["note_revision_verified"] = None
    record = rec.build_record(**build(tmp_path, observed=observed))
    outcomes = record["grade"]["outcomes"]
    assert sorted(outcomes.values()) == ["failed", "unknown"]
    assert record["grade"]["source"] == "independent"


def test_self_report_grade_is_never_eligible_for_comparison(tmp_path: Path) -> None:
    record = rec.build_record(**build(tmp_path, observed=None))
    assert record["grade"]["source"] != "independent"
    other = rec.build_record(**build(tmp_path / "other", observed=None, run_id=str(uuid.uuid4())))
    reasons = comparison_eligibility(record, other, metric="wall_s").reasons
    assert "grade_not_independent" in reasons


def test_observation_digest_is_stable_and_moves_with_one_value(tmp_path: Path) -> None:
    first = rec.build_record(**build(tmp_path / "a"))
    second = rec.build_record(**build(tmp_path / "b"))
    assert first == second
    changed = full_observation("nightly")
    changed["guide_bytes"] = b"different"
    third = rec.build_record(**build(tmp_path / "c", observed=changed))
    assert third["grade"]["observation_sha256"] != first["grade"]["observation_sha256"]


def test_workload_digest_moves_with_one_expected_byte() -> None:
    fixture = fixture_for("nightly")
    first = rec.workload_digest(fixture, RunBudget())
    assert rec.workload_digest(fixture, RunBudget()) == first
    altered_obligations = (
        dataclasses.replace(fixture.obligations[0], expected_bytes=b"fixture-note-rev-2"),
        *fixture.obligations[1:],
    )
    altered = dataclasses.replace(fixture, obligations=altered_obligations)
    assert rec.workload_digest(altered, RunBudget()) != first


def test_workload_digest_differs_between_scenarios() -> None:
    digests = {rec.workload_digest(fixture_for(name), RunBudget()) for name in SCENARIOS}
    assert len(digests) == len(SCENARIOS)


def test_workload_digest_records_the_budget_the_run_used() -> None:
    fixture = fixture_for("budget_continuation")
    assert rec.workload_digest(fixture, RunBudget(max_items=1)) != rec.workload_digest(fixture, RunBudget())


def test_the_record_binds_the_budget_it_was_built_with(tmp_path: Path) -> None:
    tight = rec.build_record(**build(tmp_path / "t", "budget_continuation", budget=RunBudget(max_items=1)))
    default = rec.build_record(**build(tmp_path / "d", "budget_continuation"))
    assert tight["provenance"]["workload_sha256"] != default["provenance"]["workload_sha256"]
    assert tight["provenance"]["workload_sha256"] == rec.workload_digest(
        fixture_for("budget_continuation"), RunBudget(max_items=1)
    )


def test_identity_digests_do_not_depend_on_source_text() -> None:
    # Declared identity strings, hashed: a comment or formatting edit must not fork comparisons.
    assert rec.grader_digest() == hashlib.sha256(
        json.dumps({"name": rec.GRADER_NAME, "contract": rec.GRADER_CONTRACT}, sort_keys=True,
                   separators=(",", ":")).encode()
    ).hexdigest()
    assert rec.harness_digest() == hashlib.sha256(
        json.dumps({"name": rec.HARNESS_NAME, "contract": rec.HARNESS_CONTRACT}, sort_keys=True,
                   separators=(",", ":")).encode()
    ).hexdigest()


def test_provenance_fields_and_environment_stays_unknown(tmp_path: Path) -> None:
    manifest = seeded(tmp_path / "m")
    record = rec.build_record(**build(tmp_path / "r", observed=None))
    provenance = record["provenance"]
    assert provenance["fixture_sha256"] == manifest.fixture_sha256
    assert provenance["environment_sha256"] is None
    assert len(provenance["harness_sha256"] or "") == 64
    assert len(provenance["grader_sha256"] or "") == 64


def test_wall_time_is_complete_only_for_a_settled_run_with_a_measured_clock(tmp_path: Path) -> None:
    complete = rec.build_record(**build(tmp_path / "s"))["metrics"]["wall_s"]
    assert complete["status"] == "complete"
    assert complete["value"] == 180.25
    assert (complete["scope"], complete["source"]) == ("run", "monotonic_clock")
    assert complete["evidence_sha256"] is not None and complete["reason"] is None

    timed_out = rec.build_record(**build(tmp_path / "t", outcome=TimedOut(dispatch_id=DISPATCH_ID)))
    assert timed_out["metrics"]["wall_s"]["status"] == "unknown"

    unmeasured = rec.build_record(**build(tmp_path / "u", wall_s=None))
    assert unmeasured["metrics"]["wall_s"] == {
        "value": None, "status": "unknown", "scope": "run", "source": "none",
        "evidence_sha256": None, "reason": "not_collected",
    }


def test_usage_is_unknown_for_the_agent_run_and_passes_a_report_through(tmp_path: Path) -> None:
    unknown = rec.build_record(**build(tmp_path / "n"))["metrics"]
    for name in ("input", "output", "cache_read", "cache_write", "model_calls", "cost_usd"):
        assert unknown[name]["status"] == "unknown"
        assert unknown[name]["reason"] == "not_collected"

    report = collect(Inventory(calls=(), proven=True), ())
    passed = rec.build_record(**build(tmp_path / "p", usage=report))["metrics"]
    expected = contract_metrics(report)
    for name in ("input", "output", "cache_read", "cache_write", "model_calls", "cost_usd"):
        assert passed[name] == expected[name]


def test_non_settled_runs_never_claim_completed_work_in_metrics(tmp_path: Path) -> None:
    record = rec.build_record(**build(tmp_path, outcome=Refused(reason="x"), wall_s=3.0))
    assert record["execution"]["status"] == "skipped"
    assert record["metrics"]["wall_s"]["status"] == "unknown"


def test_serialized_record_leaks_no_path_chat_dispatch_or_text(tmp_path: Path) -> None:
    root = tmp_path / "private-root-probe"
    record = rec.build_record(**build(root))
    text = json.dumps(record, sort_keys=True)
    for probe in (str(root), CHAT_ID, DISPATCH_ID, "2026-11-24T09:00:00Z", "Fixture note", "Prefer short"):
        assert probe not in text


def test_manifest_and_scenario_must_agree(tmp_path: Path) -> None:
    fields = build(tmp_path, "nightly")
    fields["scenario"] = "weekly_due"
    with pytest.raises(ValueError):
        rec.build_record(**fields)


def test_record_round_trips_as_public_record_without_mutation(tmp_path: Path) -> None:
    record = rec.build_record(**build(tmp_path))
    frozen = copy.deepcopy(record)
    assert public_record(record) == frozen
    assert record == frozen
