"""Tests for the pure care model-call usage projection (no provider, no I/O)."""

from __future__ import annotations

import ast
import json
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest

from evals import care_usage
from evals.care_usage import (
    PARTITIONS,
    Coverage,
    ExpectedCall,
    Inventory,
    Observation,
    Usage,
    collect,
    contract_metrics,
)
from evals.task_eval_contract import MAX_JSON_NUMBER, SCHEMA, validate_record

RUN = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
MODEL = "claude-haiku-5-5"
FULL = Usage(input=10, output=5, cache_read=2, cache_write=1, cost_usd=0.01, latency_ms=100)


def call(call_id: str = "c1", *, worker: str = "w1", partition: str = "care_worker",
         provider: str = "claude", model: str = MODEL) -> ExpectedCall:
    return ExpectedCall(RUN, worker, call_id, partition, provider, model)


def obs(call_id: str = "c1", *, attempt: int = 1, usage: Usage | None = FULL,
        worker: str = "w1", partition: str = "care_worker", provider: str = "claude",
        model: str = MODEL, outcome: str = "completed") -> Observation:
    return Observation(RUN, worker, call_id, partition, provider, model, attempt, outcome, usage)


def inv(*calls: ExpectedCall, proven: bool = True) -> Inventory:
    return Inventory(tuple(calls), proven)


def run_metric(report: object, name: str) -> Coverage:
    assert isinstance(report, care_usage.CareUsageReport)
    return report.run[name]


def part_metric(report: object, partition: str, name: str) -> Coverage:
    assert isinstance(report, care_usage.CareUsageReport)
    return report.partitions[partition][name]


# Known zero versus absent.

def test_observed_zero_is_complete_zero_and_absent_is_unknown() -> None:
    zero = Usage(input=0, output=0, cache_read=0, cache_write=0, cost_usd=0.0, latency_ms=0)
    report = collect(inv(call()), [obs(usage=zero)])
    worker_input = part_metric(report, "care_worker", "input")
    assert worker_input == Coverage(0, "complete", "provider_usage", worker_input.evidence_sha256, None)
    assert worker_input.evidence_sha256 is not None
    assert run_metric(report, "input").status == "complete"

    absent = collect(inv(call()), [obs(usage=Usage(input=None, output=5))])
    input_cov = run_metric(absent, "input")
    assert (input_cov.value, input_cov.status, input_cov.reason) == (None, "unknown", "missing_usage")
    assert run_metric(absent, "output").status == "complete"


def test_call_without_any_observation_is_unknown_not_zero() -> None:
    report = collect(inv(call()), [])
    model_calls = run_metric(report, "model_calls")
    assert (model_calls.value, model_calls.status, model_calls.reason) == (None, "unknown", "not_collected")
    assert run_metric(report, "input").value is None


# Complete empty versus unproven inventory.

def test_proven_empty_inventory_is_a_complete_zero_in_every_partition() -> None:
    report = collect(Inventory((), True), [])
    assert run_metric(report, "model_calls").value == 0
    assert run_metric(report, "model_calls").status == "complete"
    assert set(report.partitions) == set(PARTITIONS)
    for partition in PARTITIONS:
        assert part_metric(report, partition, "model_calls").status == "complete"
        assert part_metric(report, partition, "model_calls").value == 0


def test_unproven_empty_inventory_is_unknown_never_zero() -> None:
    report = collect(Inventory((), False), [])
    for name in ("model_calls", "input", "cost_usd"):
        cov = run_metric(report, name)
        assert (cov.value, cov.status, cov.reason) == (None, "unknown", "incomplete_coverage")


def test_unproven_inventory_can_never_be_complete_even_when_every_call_is_observed() -> None:
    report = collect(inv(call(), proven=False), [obs()])
    cov = run_metric(report, "input")
    assert cov.status == "partial"
    assert cov.value == 10
    assert cov.reason == "incomplete_coverage"


def test_partial_when_some_expected_calls_are_missing() -> None:
    report = collect(inv(call("c1"), call("c2")), [obs("c1")])
    cov = run_metric(report, "input")
    assert (cov.value, cov.status) == (10, "partial")
    assert cov.reason == "not_collected"


# Repeated and conflicting identities.

def test_identical_retry_telemetry_is_deduplicated_by_identity() -> None:
    report = collect(inv(call()), [obs(), obs()])
    assert report.collapsed == 1
    assert run_metric(report, "model_calls").value == 1
    assert run_metric(report, "input").value == 10
    assert run_metric(report, "input").status == "complete"


def test_conflicting_duplicates_make_the_call_incomplete() -> None:
    other = Usage(input=99, output=5, cache_read=2, cache_write=1, cost_usd=0.01, latency_ms=100)
    report = collect(inv(call()), [obs(usage=FULL), obs(usage=other)])
    assert report.conflicts == 1
    for name in ("input", "model_calls"):
        cov = run_metric(report, name)
        assert cov.status == "unknown"
        assert cov.reason == "incomplete_coverage"
        assert cov.value is None


def test_unbound_observation_is_not_summed_and_blocks_completeness() -> None:
    report = collect(inv(call("c1")), [obs("c1"), obs("ghost", usage=Usage(input=1000))])
    assert report.unbound == 1
    cov = run_metric(report, "input")
    assert cov.value == 10
    assert cov.status == "partial"
    assert cov.reason == "incomplete_coverage"
    assert part_metric(report, "care_worker", "input").status == "partial"


@pytest.mark.parametrize(
    "bad",
    [
        {"partition": "narrator_retry"},
        {"provider": "opencode"},
        {"model": "other-model"},
    ],
    ids=["wrong_partition", "wrong_provider", "wrong_model"],
)
def test_wrong_partition_provider_or_model_is_unbound_and_not_summed(bad: dict[str, str]) -> None:
    report = collect(inv(call("c1")), [obs("c1", **bad)])
    assert report.mismatched == 1
    cov = run_metric(report, "input")
    assert cov.value is None
    assert cov.status == "unknown"
    assert cov.reason == "incomplete_coverage"


# Failed and retried attempts.

def test_failed_attempt_without_usage_still_counts_as_a_model_call() -> None:
    report = collect(
        inv(call()),
        [obs(attempt=1, usage=None, outcome="failed"), obs(attempt=2, usage=Usage(input=7))],
    )
    model_calls = run_metric(report, "model_calls")
    assert (model_calls.value, model_calls.status) == (2, "complete")
    assert report.failed_attempts == 1
    input_cov = run_metric(report, "input")
    assert (input_cov.value, input_cov.status, input_cov.reason) == (None, "unknown", "missing_usage")


def test_gap_in_attempt_numbers_is_incomplete() -> None:
    report = collect(inv(call()), [obs(attempt=1), obs(attempt=3)])
    cov = run_metric(report, "model_calls")
    assert cov.status == "unknown"
    assert cov.reason == "incomplete_coverage"


def test_token_latency_and_cost_coverage_are_independent() -> None:
    report = collect(inv(call()), [obs(usage=Usage(input=10, output=5, cache_read=0,
                                                   cache_write=0, cost_usd=None, latency_ms=40))])
    assert run_metric(report, "input").status == "complete"
    cost = run_metric(report, "cost_usd")
    assert (cost.value, cost.status, cost.reason) == (None, "unknown", "missing_usage")
    assert run_metric(report, "latency_ms").status == "complete"


# Finite limits and typed input.

def test_typed_observations_reject_out_of_range_and_non_integer_values() -> None:
    with pytest.raises(ValueError):
        Usage(input=MAX_JSON_NUMBER + 1)
    with pytest.raises(ValueError):
        Usage(input=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Usage(output=-1)
    with pytest.raises(ValueError):
        Usage(cost_usd=float("nan"))
    with pytest.raises(ValueError):
        Usage(latency_ms=10**12)
    with pytest.raises(ValueError):
        obs(attempt=0)
    with pytest.raises(ValueError):
        obs(attempt=care_usage.MAX_ATTEMPT + 1)
    with pytest.raises(ValueError):
        obs(attempt=True)  # type: ignore[arg-type]


def test_inventory_and_observation_counts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(care_usage, "MAX_EXPECTED_CALLS", 2)
    with pytest.raises(ValueError, match="too_many_expected_calls"):
        Inventory((call("a"), call("b"), call("c")), True)
    monkeypatch.setattr(care_usage, "MAX_OBSERVATIONS", 1)
    with pytest.raises(ValueError, match="too_many_observations"):
        collect(inv(call()), [obs(), obs(attempt=2)])


def test_duplicate_expected_call_in_inventory_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicate_expected_call"):
        inv(call("c1"), call("c1"))


def test_sum_beyond_the_json_limit_is_collector_error_not_a_wrapped_value() -> None:
    big = Usage(input=MAX_JSON_NUMBER)
    report = collect(inv(call("c1"), call("c2")), [obs("c1", usage=big), obs("c2", usage=big)])
    cov = run_metric(report, "input")
    assert (cov.value, cov.status, cov.reason) == (None, "unknown", "collector_error")


# Identifier and output privacy.

@pytest.mark.parametrize("bad", ["w/1", "has space", "x" * 65, "line\nbreak", "../secret", ""])
def test_identifiers_that_could_carry_paths_or_text_are_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid_id"):
        ExpectedCall(RUN, bad, "c1", "care_worker", "claude", MODEL)
    with pytest.raises(ValueError, match="invalid_id"):
        ExpectedCall(RUN, "w1", bad, "care_worker", "claude", MODEL)


def test_unknown_partition_or_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="invalid_partition"):
        ExpectedCall(RUN, "w1", "c1", "everything", "claude", MODEL)
    with pytest.raises(ValueError, match="invalid_provider"):
        ExpectedCall(RUN, "w1", "c1", "care_worker", "Claude Code!", MODEL)


def test_report_carries_no_identifiers_prompts_or_paths() -> None:
    secret_calls = [
        ExpectedCall(RUN, "worker-SECRET-7", "call-SECRET-7", "care_worker", "claude", "model-SECRET"),
    ]
    observation = Observation(RUN, "worker-SECRET-7", "call-SECRET-7", "care_worker", "claude",
                              "model-SECRET", 1, "failed", None)
    report = collect(Inventory(tuple(secret_calls), True), [observation])
    text = json.dumps(asdict(report), sort_keys=True)
    for leaked in ("SECRET", RUN, "/Users/", "prompt", "transcript"):
        assert leaked not in text


# Stable provenance.

def test_provenance_is_stable_under_reordering_and_bound_to_the_inventory() -> None:
    c1, c2 = call("c1"), call("c2", partition="archive_capture")
    o1, o2 = obs("c1"), obs("c2", partition="archive_capture", usage=Usage(input=3))
    forward = collect(inv(c1, c2), [o1, o2])
    backward = collect(inv(c2, c1), [o2, o1])
    assert forward == backward

    changed_value = collect(inv(c1, c2), [o1, obs("c2", partition="archive_capture", usage=Usage(input=4))])
    assert run_metric(changed_value, "input").evidence_sha256 != run_metric(forward, "input").evidence_sha256

    changed_model = collect(inv(c1, call("c2", partition="archive_capture", model="other")), [o1])
    assert changed_model.inventory_sha256 != forward.inventory_sha256

    unproven = collect(inv(c1, c2, proven=False), [o1, o2])
    assert unproven.inventory_sha256 != forward.inventory_sha256


# Contract compatibility.

def _record(metrics: dict[str, object]) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "run_id": RUN,
        "scenario": "nightly",
        "entry": "schedule",
        "execution": {"status": "completed", "code_revision": None, "dirty": False},
        "provenance": {key: None for key in (
            "fixture_sha256", "workload_sha256", "environment_sha256", "harness_sha256", "grader_sha256")},
        "obligations": ["o001"],
        "grade": {"source": "none", "run_id": None, "fixture_sha256": None, "workload_sha256": None,
                  "grader_sha256": None, "observation_sha256": None, "outcomes": {"o001": "unknown"},
                  "safety": "unknown"},
        "metrics": metrics,
    }


def test_projection_maps_onto_a_contract_record_that_validates() -> None:
    complete = collect(Inventory((), True), [])
    validate_record(_record(contract_metrics(complete)))

    partial = collect(inv(call("c1"), call("c2")), [obs("c1"), obs("c1", attempt=2, usage=None)])
    validate_record(_record(contract_metrics(partial)))

    unknown_only = collect(inv(call()), [])
    metrics = contract_metrics(unknown_only)
    assert metrics["input"]["status"] == "unknown"
    assert metrics["input"]["source"] == "none"
    validate_record(_record(metrics))


def test_projection_module_imports_no_provider_or_task_eval_code() -> None:
    source = (Path(care_usage.__file__)).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        m for m in imported
        if m == "evals.task_eval" or m.startswith(("evals.task_eval.", "ciao.providers", "ciao.web"))
        or m in ("subprocess", "socket", "requests", "httpx")
    }
    assert forbidden == set()
