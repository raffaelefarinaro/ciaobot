"""Privacy-minimized contract for future workspace-care measurements.

This module validates declarations; it does not collect or authenticate evidence.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

SCHEMA = "ciao-care-measurement/1"
MAX_JSON_NUMBER = 2**53 - 1
METRIC_NAMES = (
    "input", "output", "cache_read", "cache_write", "model_calls", "cost_usd", "wall_s",
)
MetricName = Literal["input", "output", "cache_read", "cache_write", "model_calls", "cost_usd", "wall_s"]
Scenario = Literal["nightly", "weekly_due", "budget_continuation"]
Entry = Literal["schedule", "prompt", "delegate", "transcript"]
ExecutionStatus = Literal["completed", "timed_out", "error", "skipped"]
GradeSource = Literal["independent", "self_report", "none"]
Outcome = Literal["passed", "failed", "unknown"]
MetricStatus = Literal["complete", "partial", "unknown"]
MetricScope = Literal["all_model_calls", "chat_turns", "run"]
MetricSource = Literal["provider_usage", "turn_aggregate", "runtime_cost", "monotonic_clock", "none"]
MetricReason = Literal["not_collected", "missing_usage", "incomplete_coverage", "unsupported", "collector_error"]


class Execution(TypedDict):
    status: ExecutionStatus
    code_revision: str | None
    dirty: bool


class Provenance(TypedDict):
    fixture_sha256: str | None
    workload_sha256: str | None
    environment_sha256: str | None
    harness_sha256: str | None
    grader_sha256: str | None


class Grade(TypedDict):
    source: GradeSource
    run_id: str | None
    fixture_sha256: str | None
    workload_sha256: str | None
    grader_sha256: str | None
    observation_sha256: str | None
    outcomes: dict[str, Outcome]
    safety: Outcome


class Metric(TypedDict):
    value: int | float | None
    status: MetricStatus
    scope: MetricScope
    source: MetricSource
    evidence_sha256: str | None
    reason: MetricReason | None


class MeasurementRecord(TypedDict):
    schema: str
    run_id: str
    scenario: Scenario
    entry: Entry
    execution: Execution
    provenance: Provenance
    obligations: list[str]
    grade: Grade
    metrics: dict[str, Metric]


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...]


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_OBLIGATION = re.compile(r"o[0-9]{3}\Z")
_SCHEMA_KEYS = {
    "root": {"schema", "run_id", "scenario", "entry", "execution", "provenance", "obligations", "grade", "metrics"},
    "execution": {"status", "code_revision", "dirty"},
    "provenance": {"fixture_sha256", "workload_sha256", "environment_sha256", "harness_sha256", "grader_sha256"},
    "grade": {"source", "run_id", "fixture_sha256", "workload_sha256", "grader_sha256", "observation_sha256", "outcomes", "safety"},
    "metric": {"value", "status", "scope", "source", "evidence_sha256", "reason"},
}
_DIGEST_FIELDS = ("fixture_sha256", "workload_sha256", "environment_sha256", "harness_sha256", "grader_sha256")


def _fail(code: str) -> None:
    raise ValueError(code) from None


def _obj(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict:
        _fail("invalid_type")
    obj = cast(dict[object, object], value)
    if len(obj) != len(keys):
        _fail("invalid_keys")
    # Iterate without constructing a key set until all keys are known safe strings.
    for key in obj:
        if type(key) is not str:
            _fail("invalid_keys")
        if len(cast(str, key)) > 64:
            _fail("invalid_value")
    if set(obj) != keys:
        _fail("invalid_keys")
    return cast(dict[str, object], obj)


def _string(value: object) -> str:
    if type(value) is not str:
        _fail("invalid_type")
    result = cast(str, value)
    if len(result) > 64:
        _fail("invalid_value")
    return result


def _enum(value: object, choices: tuple[str, ...]) -> str:
    text = _string(value)
    if text not in choices:
        _fail("invalid_value")
    return text


def _digest(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    text = _string(value)
    if _HEX64.fullmatch(text) is None:
        _fail("invalid_value")
    return text


def _uuid(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    text = _string(value)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None:
        _fail("invalid_value")
    if str(parsed) != text:
        _fail("invalid_value")
    return text


def _metric(name: str, raw: object) -> Metric:
    obj = _obj(raw, _SCHEMA_KEYS["metric"])
    value, status = obj["value"], _enum(obj["status"], ("complete", "partial", "unknown"))
    scope = _enum(obj["scope"], ("all_model_calls", "chat_turns", "run"))
    source = _enum(obj["source"], ("provider_usage", "turn_aggregate", "runtime_cost", "monotonic_clock", "none"))
    evidence = _digest(obj["evidence_sha256"], nullable=True)
    reason_obj = obj["reason"]
    reason = None if reason_obj is None else _enum(reason_obj, ("not_collected", "missing_usage", "incomplete_coverage", "unsupported", "collector_error"))
    if value is not None:
        allowed_float = name in ("cost_usd", "wall_s")
        if type(value) is int:
            number = cast(int, value)
            if number < 0 or number > MAX_JSON_NUMBER:
                _fail("invalid_number")
        elif type(value) is float and allowed_float:
            number_f = cast(float, value)
            if not math.isfinite(number_f) or number_f < 0 or number_f > MAX_JSON_NUMBER:
                _fail("invalid_number")
        else:
            _fail("invalid_type")
    elif status != "unknown":
        _fail("invalid_value")
    if status == "complete":
        if value is None or evidence is None or reason is not None or source == "none":
            _fail("invalid_value")
    elif status == "partial":
        if value is None or evidence is None or reason is None:
            _fail("invalid_value")
    elif value is not None or reason is None:
        _fail("invalid_value")

    if source == "none":
        legal = status == "unknown" and (
            (name == "wall_s" and scope == "run")
            or (name == "cost_usd" and scope in ("all_model_calls", "run"))
            or (name not in ("cost_usd", "wall_s") and scope in ("all_model_calls", "chat_turns"))
        )
    elif name == "wall_s":
        legal = source == "monotonic_clock" and scope == "run"
    elif name == "cost_usd":
        legal = (source == "provider_usage" and scope == "all_model_calls") or (source == "runtime_cost" and scope == "run")
    else:
        legal = (source == "provider_usage" and scope == "all_model_calls") or (source == "turn_aggregate" and scope == "chat_turns")
    if not legal:
        _fail("invalid_value")
    return cast(Metric, {"value": value, "status": status, "scope": scope, "source": source,
                         "evidence_sha256": evidence, "reason": reason})


def validate_record(raw: object) -> MeasurementRecord:
    root = _obj(raw, _SCHEMA_KEYS["root"])
    if _enum(root["schema"], (SCHEMA,)) != SCHEMA:
        _fail("invalid_value")
    run_id = _uuid(root["run_id"])
    scenario = _enum(root["scenario"], ("nightly", "weekly_due", "budget_continuation"))
    entry = _enum(root["entry"], ("schedule", "prompt", "delegate", "transcript"))
    execution = _obj(root["execution"], _SCHEMA_KEYS["execution"])
    status = _enum(execution["status"], ("completed", "timed_out", "error", "skipped"))
    revision_raw = execution["code_revision"]
    revision = None if revision_raw is None else _string(revision_raw)
    if revision is not None and _SHA.fullmatch(revision) is None:
        _fail("invalid_value")
    dirty = execution["dirty"]
    if type(dirty) is not bool:
        _fail("invalid_type")

    provenance_obj = _obj(root["provenance"], _SCHEMA_KEYS["provenance"])
    provenance = {key: _digest(provenance_obj[key], nullable=True) for key in _DIGEST_FIELDS}
    obligations_raw = root["obligations"]
    if type(obligations_raw) is not list:
        _fail("invalid_type")
    obligations_in = cast(list[object], obligations_raw)
    if len(obligations_in) == 0 or len(obligations_in) > 1000:
        _fail("invalid_value")
    obligations: list[str] = []
    for item in obligations_in:
        text = _string(item)
        if _OBLIGATION.fullmatch(text) is None:
            _fail("invalid_value")
        obligations.append(text)
    if len(set(obligations)) != len(obligations):
        _fail("invalid_value")
    obligations.sort()

    grade_obj = _obj(root["grade"], _SCHEMA_KEYS["grade"])
    grade_source = _enum(grade_obj["source"], ("independent", "self_report", "none"))
    grade_run = _uuid(grade_obj["run_id"], nullable=True)
    grade_fixture = _digest(grade_obj["fixture_sha256"], nullable=True)
    grade_workload = _digest(grade_obj["workload_sha256"], nullable=True)
    grade_grader = _digest(grade_obj["grader_sha256"], nullable=True)
    observation = _digest(grade_obj["observation_sha256"], nullable=True)
    outcomes_raw = grade_obj["outcomes"]
    if type(outcomes_raw) is not dict:
        _fail("invalid_type")
    outcomes_obj = cast(dict[object, object], outcomes_raw)
    if len(outcomes_obj) > 1000:
        _fail("invalid_value")
    for key in outcomes_obj:
        if type(key) is not str:
            _fail("invalid_keys")
        if len(cast(str, key)) > 64:
            _fail("invalid_value")
    if set(outcomes_obj) != set(obligations):
        _fail("invalid_value")
    outcomes = {key: _enum(outcomes_obj[key], ("passed", "failed", "unknown")) for key in sorted(obligations)}
    safety = _enum(grade_obj["safety"], ("passed", "failed", "unknown"))

    metrics_obj = _obj(root["metrics"], set(METRIC_NAMES))
    metrics = {name: _metric(name, metrics_obj[name]) for name in METRIC_NAMES}
    return cast(MeasurementRecord, {
        "schema": SCHEMA, "run_id": cast(str, run_id), "scenario": scenario, "entry": entry,
        "execution": {"status": status, "code_revision": revision, "dirty": dirty},
        "provenance": cast(dict[str, object], provenance), "obligations": obligations,
        "grade": {"source": grade_source, "run_id": grade_run, "fixture_sha256": grade_fixture,
                  "workload_sha256": grade_workload, "grader_sha256": grade_grader,
                  "observation_sha256": observation, "outcomes": outcomes, "safety": safety},
        "metrics": metrics,
    })


def public_record(raw: object) -> MeasurementRecord:
    """Validate and rebuild this minimized schema; not a sanitizer for arbitrary data."""
    return validate_record(raw)


def comparison_eligibility(baseline: object, candidate: object, *, metric: MetricName) -> Eligibility:
    if type(metric) is not str or len(cast(str, metric)) > 64 or cast(str, metric) not in METRIC_NAMES:
        _fail("invalid_metric")
    left, right = validate_record(baseline), validate_record(candidate)
    reasons: set[str] = set()
    records = (left, right)
    if left["run_id"] == right["run_id"]:
        reasons.add("same_run")
    if any(row["entry"] != "schedule" for row in records):
        reasons.add("entry_not_schedule")
    if any(row["execution"]["status"] != "completed" for row in records):
        reasons.add("execution_incomplete")
    if any(row["execution"]["code_revision"] is None or row["execution"]["dirty"] for row in records):
        reasons.add("revision_unverifiable")
    provs = (left["provenance"], right["provenance"])
    if any(value is None for p in provs for value in p.values()):
        reasons.add("provenance_unknown")
    if (left["scenario"] != right["scenario"] or left["obligations"] != right["obligations"]
            or any(left["provenance"][key] != right["provenance"][key] for key in ("fixture_sha256", "workload_sha256")
                   if left["provenance"][key] is not None and right["provenance"][key] is not None)):
        reasons.add("workload_mismatch")
    left_provenance = cast(dict[str, object], left["provenance"])
    right_provenance = cast(dict[str, object], right["provenance"])
    for field, reason in (("environment_sha256", "environment_mismatch"), ("harness_sha256", "harness_mismatch"), ("grader_sha256", "grader_mismatch")):
        a, b = left_provenance[field], right_provenance[field]
        if a is not None and b is not None and a != b:
            reasons.add(reason)
    for row in records:
        grade = row["grade"]
        if grade["source"] != "independent":
            reasons.add("grade_not_independent")
        bound = (grade["observation_sha256"] is not None and grade["run_id"] is not None
                 and grade["fixture_sha256"] is not None and grade["workload_sha256"] is not None
                 and grade["grader_sha256"] is not None
                 and grade["run_id"] == row["run_id"]
                 and grade["fixture_sha256"] == row["provenance"]["fixture_sha256"]
                 and grade["workload_sha256"] == row["provenance"]["workload_sha256"]
                 and grade["grader_sha256"] == row["provenance"]["grader_sha256"])
        if not bound:
            reasons.add("grade_unbound")
        if grade["safety"] != "passed" or any(
            value != "passed" for value in cast(dict[str, str], grade["outcomes"]).values()
        ):
            reasons.add("outcome_incomplete")
    metrics = (left["metrics"][metric], right["metrics"][metric])
    if any(m["status"] != "complete" for m in metrics):
        reasons.add("metric_incomplete")
    expected_scope_source = ("run", "monotonic_clock") if metric == "wall_s" else ("all_model_calls", "provider_usage")
    if any((m["scope"], m["source"]) != expected_scope_source for m in metrics):
        reasons.add("metric_scope_ineligible")
    if metrics[0]["scope"] != metrics[1]["scope"] or metrics[0]["source"] != metrics[1]["source"]:
        reasons.add("metric_basis_mismatch")
    ordered = tuple(sorted(reasons))
    return Eligibility(not ordered, ordered)
