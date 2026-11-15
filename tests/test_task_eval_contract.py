import copy
import json
import math
import uuid
from collections import UserDict

import pytest

from ciao.task_eval_contract import (
    MAX_JSON_NUMBER,
    METRIC_NAMES,
    SCHEMA,
    comparison_eligibility,
    public_record,
    validate_record,
)


def record(run_id: str | None = None) -> dict[str, object]:
    run_id = run_id or str(uuid.uuid4())
    digest = "a" * 64
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "scenario": "nightly",
        "entry": "schedule",
        "execution": {
            "status": "completed",
            "code_revision": "b" * 40,
            "dirty": False,
        },
        "provenance": {
            "fixture_sha256": digest,
            "workload_sha256": "c" * 64,
            "environment_sha256": "d" * 64,
            "harness_sha256": "e" * 64,
            "grader_sha256": "f" * 64,
        },
        "obligations": ["o002", "o001"],
        "grade": {
            "source": "independent",
            "run_id": run_id,
            "fixture_sha256": digest,
            "workload_sha256": "c" * 64,
            "grader_sha256": "f" * 64,
            "observation_sha256": "1" * 64,
            "outcomes": {"o002": "passed", "o001": "passed"},
            "safety": "passed",
        },
        "metrics": {},
    }


def valid_record() -> dict[str, object]:
    value = record()
    value["metrics"] = {
        name: {
            "value": 0,
            "status": "complete",
            "scope": "run" if name == "wall_s" else "all_model_calls",
            "source": "monotonic_clock" if name == "wall_s" else "provider_usage",
            "evidence_sha256": "2" * 64,
            "reason": None,
        }
        for name in METRIC_NAMES
    }
    return value


def test_public_record_round_trip_is_canonical_and_detached() -> None:
    raw = valid_record()
    result = public_record(raw)
    assert result["obligations"] == ["o001", "o002"]
    assert list(result["grade"]["outcomes"]) == ["o001", "o002"]  # type: ignore[index]
    result["metrics"]["input"]["value"] = 12  # type: ignore[index]
    assert raw["metrics"]["input"]["value"] == 0  # type: ignore[index]
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda r: r.update(answer="private"), "invalid_keys"),
        (lambda r: r.update(schema="ciao-task-eval/1"), "invalid_value"),
        (lambda r: r["metrics"]["input"].update(value=True), "invalid_type"),
        (lambda r: r["metrics"]["input"].update(value=MAX_JSON_NUMBER + 1), "invalid_number"),
        (lambda r: r["grade"]["outcomes"].pop("o002"), "invalid_value"),
        (lambda r: r["execution"].update(code_revision=None), None),
    ],
)
def test_invalid_record_shapes_use_fixed_errors(mutate, code) -> None:
    raw = valid_record()
    mutate(raw)
    if code is None:
        return
    with pytest.raises(ValueError) as exc:
        validate_record(raw)
    assert str(exc.value) == code


def test_malformed_uuid_has_no_input_derived_exception_context() -> None:
    raw = valid_record()
    raw["run_id"] = "private-user-input"
    with pytest.raises(ValueError) as exc:
        validate_record(raw)
    assert str(exc.value) == "invalid_value"
    assert exc.value.__context__ is None


def test_unknown_fields_and_legacy_rows_are_not_sanitized() -> None:
    raw = valid_record()
    raw["answer"] = "secret sentinel"
    with pytest.raises(ValueError, match="^invalid_keys$"):
        public_record(raw)
    with pytest.raises(ValueError, match="^invalid_keys$"):
        validate_record({"schema": "ciao-task-eval/1", "answer": "secret"})


def test_numeric_boundaries_and_unknown_are_distinct() -> None:
    raw = valid_record()
    raw["metrics"]["input"]["value"] = MAX_JSON_NUMBER  # type: ignore[index]
    result = validate_record(raw)
    assert result["metrics"]["input"]["value"] == MAX_JSON_NUMBER  # type: ignore[index]
    raw["metrics"]["cost_usd"].update(value=None, status="unknown", reason="not_collected", evidence_sha256=None)  # type: ignore[index]
    assert validate_record(raw)["metrics"]["cost_usd"]["value"] is None  # type: ignore[index]


@pytest.mark.parametrize("name", METRIC_NAMES)
def test_metric_numbers_keep_integer_precision_and_refuse_huge_values(name: str) -> None:
    raw = valid_record()
    raw["metrics"][name]["value"] = 1 << 100_000  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid_number$"):
        validate_record(raw)
    raw["metrics"][name]["value"] = MAX_JSON_NUMBER  # type: ignore[index]
    assert validate_record(raw)["metrics"][name]["value"] == MAX_JSON_NUMBER  # type: ignore[index]


@pytest.mark.parametrize("negative", [False, True])
def test_huge_integer_rejected_without_finiteness_or_float_conversion(monkeypatch, negative: bool) -> None:
    def no_integer_finiteness(value: object) -> bool:
        assert type(value) is not int
        raise AssertionError("integer passed to math.isfinite")

    monkeypatch.setattr("ciao.task_eval_contract.math.isfinite", no_integer_finiteness)
    raw = valid_record()
    raw["metrics"]["input"]["value"] = -(1 << 100_000) if negative else 1 << 100_000  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid_number$") as exc:
        validate_record(raw)
    assert exc.value.__context__ is None


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (-1, "invalid_number"),
        (MAX_JSON_NUMBER + 1, "invalid_number"),
        (True, "invalid_type"),
        ("0", "invalid_type"),
        (1.0, "invalid_type"),
    ],
)
@pytest.mark.parametrize("name", ("input", "model_calls"))
def test_integer_metrics_reject_negative_and_non_integer_values(name: str, value: object, code: str) -> None:
    raw = valid_record()
    raw["metrics"][name]["value"] = value  # type: ignore[index]
    with pytest.raises(ValueError, match=f"^{code}$"):
        validate_record(raw)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        float(MAX_JSON_NUMBER + 1),
        math.nextafter(float(MAX_JSON_NUMBER), math.inf),
    ],
)
@pytest.mark.parametrize("name", ("cost_usd", "wall_s"))
def test_fractional_metrics_reject_nonfinite_and_out_of_range_values(name: str, value: float) -> None:
    raw = valid_record()
    raw["metrics"][name]["value"] = value  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid_number$"):
        validate_record(raw)


@pytest.mark.parametrize("name", ("cost_usd", "wall_s"))
def test_fractional_metrics_accept_valid_fractions_and_maximum(name: str) -> None:
    raw = valid_record()
    raw["metrics"][name]["value"] = 0.5  # type: ignore[index]
    assert validate_record(raw)["metrics"][name]["value"] == 0.5  # type: ignore[index]
    raw["metrics"][name]["value"] = float(MAX_JSON_NUMBER)  # type: ignore[index]
    assert validate_record(raw)["metrics"][name]["value"] == float(MAX_JSON_NUMBER)  # type: ignore[index]


def test_exact_json_types_and_key_types_are_required() -> None:
    class Text(str):
        pass

    raw = valid_record()
    raw["entry"] = Text("schedule")
    with pytest.raises(ValueError, match="^invalid_type$"):
        public_record(raw)
    raw = valid_record()
    raw[1] = "private sentinel"
    with pytest.raises(ValueError, match="^invalid_keys$"):
        public_record(raw)
    raw = valid_record()
    raw["metrics"]["input"] = UserDict(raw["metrics"]["input"])  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid_type$"):
        validate_record(raw)


@pytest.mark.parametrize("subclass", (int, float))
def test_numeric_subclasses_are_refused_without_conversion(subclass: type) -> None:
    class Integer(int):
        def __int__(self) -> int:
            raise AssertionError("int conversion")

        def __float__(self) -> float:
            raise AssertionError("float conversion")

    class Float(float):
        def __int__(self) -> int:
            raise AssertionError("int conversion")

        def __float__(self) -> float:
            raise AssertionError("float conversion")

    raw = valid_record()
    raw["metrics"]["cost_usd"]["value"] = Integer(1) if subclass is int else Float(1)  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid_type$"):
        validate_record(raw)


def test_cycles_at_schema_positions_fail_without_recursion() -> None:
    raw = valid_record()
    cyclic: list[object] = []
    cyclic.append(cyclic)
    raw["obligations"] = cyclic
    with pytest.raises(ValueError, match="^invalid_type$"):
        validate_record(raw)


@pytest.mark.parametrize("position", ("root", "execution", "obligations", "unknown"))
def test_additional_cycles_are_refused_at_schema_boundary(position: str) -> None:
    raw = valid_record()
    loop: dict[str, object] = {}
    loop["self"] = loop
    if position == "root":
        raw["execution"] = raw
        expected = "invalid_keys"
    elif position == "execution":
        raw["execution"]["status"] = loop  # type: ignore[index]
        expected = "invalid_type"
    elif position == "obligations":
        raw["obligations"] = [loop]
        expected = "invalid_type"
    else:
        raw["unknown"] = loop
        expected = "invalid_keys"
    with pytest.raises(ValueError, match=f"^{expected}$"):
        public_record(raw)


def test_non_string_hostile_key_at_expected_length_never_runs_hooks() -> None:
    calls: list[str] = []

    class HostileKey:
        armed = False

        def __hash__(self) -> int:
            if self.armed:
                calls.append("hash")
                raise AssertionError("hostile key hashed")
            return 17

        def __eq__(self, other: object) -> bool:
            calls.append("eq")
            raise AssertionError("hostile key compared")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("hostile key represented")

    for nested in (False, True):
        raw = valid_record()
        target = raw["execution"] if nested else raw
        keys = list(target)
        hostile = HostileKey()
        target.pop(keys[-1])
        target[hostile] = "attacker value"
        hostile.armed = True
        calls.clear()
        with pytest.raises(ValueError, match="^invalid_keys$") as exc:
            public_record(raw)
        assert exc.value.__context__ is None
        assert calls == []


def test_armed_string_subclass_key_is_refused_without_hooks() -> None:
    calls: list[str] = []

    class HostileString(str):
        armed = False

        def __hash__(self) -> int:
            if self.armed:
                calls.append("hash")
                raise AssertionError("string key hashed")
            return super().__hash__()

        def __eq__(self, other: object) -> bool:
            calls.append("eq")
            raise AssertionError("string key compared")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("string key represented")

    raw = valid_record()
    provenance = raw["provenance"]
    provenance.pop("grader_sha256")  # type: ignore[attr-defined]
    key = HostileString("grader_sha256")
    provenance[key] = "f" * 64  # type: ignore[index]
    key.armed = True
    with pytest.raises(ValueError, match="^invalid_keys$") as exc:
        public_record(raw)
    assert exc.value.__context__ is None
    assert calls == []


def test_dict_and_list_subclass_hooks_are_never_called() -> None:
    calls: list[str] = []

    class HostileDict(dict):
        def __iter__(self):
            calls.append("iter")
            raise AssertionError("dict iterated")

        def __getitem__(self, key: object) -> object:
            calls.append("getitem")
            raise AssertionError("dict indexed")

    class HostileList(list):
        def __iter__(self):
            calls.append("list_iter")
            raise AssertionError("list iterated")

        def __getitem__(self, key: object) -> object:
            calls.append("list_getitem")
            raise AssertionError("list indexed")

    for raw in (HostileDict(valid_record()),):
        with pytest.raises(ValueError, match="^invalid_type$") as exc:
            validate_record(raw)
        assert exc.value.__context__ is None
    raw = valid_record()
    raw["provenance"] = HostileDict(raw["provenance"])
    with pytest.raises(ValueError, match="^invalid_type$"):
        public_record(raw)
    raw = valid_record()
    raw["obligations"] = HostileList(raw["obligations"])
    with pytest.raises(ValueError, match="^invalid_type$"):
        validate_record(raw)
    assert calls == []


def test_custom_non_string_metric_selector_runs_no_hooks() -> None:
    calls: list[str] = []

    class HostileSelector:
        def __hash__(self) -> int:
            calls.append("hash")
            raise AssertionError("selector hashed")

        def __eq__(self, other: object) -> bool:
            calls.append("eq")
            raise AssertionError("selector compared")

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("selector stringified")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("selector represented")

    with pytest.raises(ValueError, match="^invalid_metric$") as exc:
        comparison_eligibility(None, None, metric=HostileSelector())  # type: ignore[arg-type]
    assert exc.value.__context__ is None
    assert calls == []


@pytest.mark.parametrize("side", ("baseline", "candidate"))
def test_nested_hostile_values_and_comparison_sides_refuse_without_hooks(side: str) -> None:
    calls: list[str] = []

    class HostileNumber:
        def __float__(self) -> float:
            calls.append("float")
            raise AssertionError("float hook")

        def __int__(self) -> int:
            calls.append("int")
            raise AssertionError("int hook")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("repr hook")

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("str hook")

    left, right = valid_record(), valid_record()
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    target = left if side == "baseline" else right
    target["metrics"]["input"]["value"] = HostileNumber()  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid_type$") as exc:
        comparison_eligibility(left, right, metric="input")
    assert exc.value.__context__ is None
    assert calls == []


@pytest.mark.parametrize("side", ("baseline", "candidate"))
def test_foreign_grade_run_binding_blocks_comparison(side: str) -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    target = left if side == "baseline" else right
    target["grade"]["run_id"] = str(uuid.uuid4())  # type: ignore[index]
    assert "grade_unbound" in comparison_eligibility(left, right, metric="input").reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fixture_sha256", "9" * 64),
        ("workload_sha256", "9" * 64),
        ("grader_sha256", "9" * 64),
        ("fixture_sha256", None),
        ("workload_sha256", None),
        ("grader_sha256", None),
    ],
)
def test_grade_provenance_binding_must_be_non_null_and_match(field: str, value: str | None) -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    left["grade"][field] = value  # type: ignore[index]
    assert "grade_unbound" in comparison_eligibility(left, right, metric="input").reasons


def test_null_provenance_and_null_grade_binding_are_not_equivalent() -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    for row in (left, right):
        row["provenance"]["fixture_sha256"] = None  # type: ignore[index]
        row["grade"]["fixture_sha256"] = None  # type: ignore[index]
    result = comparison_eligibility(left, right, metric="input")
    assert "provenance_unknown" in result.reasons
    assert "grade_unbound" in result.reasons
    assert "workload_mismatch" not in result.reasons


def test_obligation_set_and_budget_horizon_must_match_and_be_complete() -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    right["obligations"] = ["o001"]
    right["grade"]["outcomes"] = {"o001": "passed"}  # type: ignore[index]
    result = comparison_eligibility(left, right, metric="input")
    assert "workload_mismatch" in result.reasons
    left = valid_record()
    right = valid_record()
    left["scenario"] = right["scenario"] = "budget_continuation"
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    right["grade"]["outcomes"]["o002"] = "unknown"  # type: ignore[index]
    result = comparison_eligibility(left, right, metric="input")
    assert "workload_mismatch" not in result.reasons
    assert "outcome_incomplete" in result.reasons


def test_exact_simultaneous_eligibility_reasons() -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = left["run_id"]
    right["entry"] = "prompt"
    right["execution"].update(status="error", dirty=True, code_revision=None)  # type: ignore[index]
    right["provenance"].update(
        fixture_sha256="9" * 64,
        workload_sha256=None,
        environment_sha256="9" * 64,
        harness_sha256="9" * 64,
        grader_sha256="9" * 64,
    )  # type: ignore[index]
    right["grade"].update(source="self_report", run_id=None, observation_sha256=None)  # type: ignore[index]
    right["grade"]["outcomes"]["o001"] = "unknown"  # type: ignore[index]
    right["metrics"]["input"].update(
        value=None, status="unknown", reason="not_collected", evidence_sha256=None
    )  # type: ignore[index]
    result = comparison_eligibility(left, right, metric="input")
    assert result.reasons == tuple(sorted((
        "same_run", "entry_not_schedule", "execution_incomplete",
        "revision_unverifiable", "provenance_unknown", "workload_mismatch",
        "environment_mismatch", "harness_mismatch", "grader_mismatch",
        "grade_not_independent", "grade_unbound", "outcome_incomplete",
        "metric_incomplete",
    )))


def test_collection_size_boundaries_and_string_length() -> None:
    raw = valid_record()
    ids = [f"o{i:03}" for i in range(1000)]
    raw["obligations"] = ids
    raw["grade"]["outcomes"] = {key: "passed" for key in ids}  # type: ignore[index]
    assert len(validate_record(raw)["obligations"]) == 1000
    raw["obligations"].append("o000")  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="^invalid_value$"):
        validate_record(raw)
    raw = valid_record()
    raw["grade"]["outcomes"] = {f"o{i:04}": "passed" for i in range(1001)}  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid_value$"):
        validate_record(raw)
    raw = valid_record()
    raw["schema"] = "x" * 65
    with pytest.raises(ValueError, match="^invalid_value$"):
        validate_record(raw)


def test_invalid_metric_selector_is_refused_before_any_record_validation() -> None:
    class SecretMetric(str):
        def __str__(self) -> str:
            raise AssertionError("must not stringify")

        def __hash__(self) -> int:
            raise AssertionError("must not hash")

    with pytest.raises(ValueError, match="^invalid_metric$"):
        comparison_eligibility(None, None, metric=SecretMetric("private"))  # type: ignore[arg-type]


def test_turn_aggregate_is_structurally_valid_but_not_whole_run_eligible() -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    for row in (left, right):
        row["metrics"]["input"].update(scope="chat_turns", source="turn_aggregate")  # type: ignore[index]
    result = comparison_eligibility(left, right, metric="input")
    assert "metric_scope_ineligible" in result.reasons


def test_turn_and_all_call_basis_mismatch_reports_both_reasons() -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    left["metrics"]["input"].update(scope="chat_turns", source="turn_aggregate")  # type: ignore[index]
    result = comparison_eligibility(left, right, metric="input")
    assert "metric_scope_ineligible" in result.reasons
    assert "metric_basis_mismatch" in result.reasons


def test_partial_metric_is_never_comparison_eligible() -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    left["metrics"]["input"].update(
        status="partial", reason="incomplete_coverage"
    )  # type: ignore[index]
    result = comparison_eligibility(left, right, metric="input")
    assert not result.eligible
    assert "metric_incomplete" in result.reasons


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(scenario="weekly_due"), "workload_mismatch"),
        (lambda r: r["provenance"].update(fixture_sha256="9" * 64), "workload_mismatch"),
        (lambda r: r["provenance"].update(environment_sha256="9" * 64), "environment_mismatch"),
        (lambda r: r["provenance"].update(harness_sha256="9" * 64), "harness_mismatch"),
        (lambda r: r["provenance"].update(grader_sha256="9" * 64), "grader_mismatch"),
        (lambda r: r["grade"].update(source="self_report"), "grade_not_independent"),
        (lambda r: r["grade"].update(observation_sha256=None), "grade_unbound"),
        (lambda r: r["grade"]["outcomes"].update(o002="unknown"), "outcome_incomplete"),
        (lambda r: r["grade"].update(safety="failed"), "outcome_incomplete"),
        (lambda r: r.update(entry="prompt"), "entry_not_schedule"),
        (lambda r: r["execution"].update(status="timed_out"), "execution_incomplete"),
        (lambda r: r["execution"].update(dirty=True), "revision_unverifiable"),
        (lambda r: r["execution"].update(code_revision=None), "revision_unverifiable"),
    ],
)
def test_comparison_reasons_cover_identity_and_completion(change, reason) -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = str(uuid.uuid4())
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    change(right)
    result = comparison_eligibility(left, right, metric="input")
    assert not result.eligible
    assert reason in result.reasons


@pytest.mark.parametrize(
    ("name", "scope", "source"),
    [
        ("input", "run", "provider_usage"),
        ("cost_usd", "chat_turns", "turn_aggregate"),
        ("wall_s", "all_model_calls", "monotonic_clock"),
    ],
)
def test_impossible_metric_source_scope_pair_is_rejected(name: str, scope: str, source: str) -> None:
    raw = valid_record()
    raw["metrics"][name].update(scope=scope, source=source)  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid_value$"):
        validate_record(raw)


@pytest.mark.parametrize(
    ("name", "updates"),
    [
        ("input", {"status": "complete", "value": 0, "evidence_sha256": None}),
        ("input", {"status": "complete", "value": 0, "reason": "unsupported"}),
        ("input", {"status": "complete", "value": None}),
        ("input", {"status": "partial", "value": None}),
        ("input", {"status": "partial", "value": 0, "reason": None}),
        ("input", {"status": "partial", "value": 0, "evidence_sha256": None}),
        ("input", {"status": "unknown", "value": 0, "reason": "not_collected"}),
        ("input", {"status": "unknown", "value": None, "reason": None}),
    ],
)
def test_metric_value_status_consistency_matrix(name: str, updates: dict[str, object]) -> None:
    raw = valid_record()
    raw["metrics"][name].update(updates)  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid_value$"):
        validate_record(raw)


@pytest.mark.parametrize(
    ("status", "value", "evidence", "reason"),
    [
        ("complete", 0, "2" * 64, None),
        ("partial", 0, "2" * 64, "incomplete_coverage"),
        ("unknown", None, None, "not_collected"),
        ("unknown", None, "2" * 64, "unsupported"),
    ],
)
def test_valid_metric_status_consistency_matrix(status, value, evidence, reason) -> None:
    raw = valid_record()
    raw["metrics"]["input"].update(
        status=status, value=value, evidence_sha256=evidence, reason=reason
    )  # type: ignore[index]
    result = validate_record(raw)
    assert result["metrics"]["input"]["status"] == status  # type: ignore[index]


def test_duplicate_obligations_and_incomplete_grade_binding_are_refused() -> None:
    raw = valid_record()
    raw["obligations"] = ["o001", "o001"]
    with pytest.raises(ValueError, match="^invalid_value$"):
        validate_record(raw)
    raw = valid_record()
    raw["grade"]["workload_sha256"] = "9" * 64  # type: ignore[index]
    result = comparison_eligibility(raw, valid_record(), metric="input")
    assert "grade_unbound" in result.reasons


def test_extra_field_payload_is_refused_without_echoing_it() -> None:
    raw = valid_record()
    raw["note"] = "credential-shaped private sentinel"
    with pytest.raises(ValueError) as exc:
        public_record(raw)
    assert str(exc.value) == "invalid_keys"
    assert "sentinel" not in str(exc.value)


def test_eligible_pair_allows_different_revisions_and_unknown_unrequested_metric() -> None:
    baseline = valid_record()
    candidate = valid_record()
    candidate["run_id"] = str(uuid.uuid4())
    candidate["execution"]["code_revision"] = "9" * 40  # type: ignore[index]
    candidate["grade"]["run_id"] = candidate["run_id"]  # type: ignore[index]
    for row in (baseline, candidate):
        row["metrics"]["cost_usd"].update(value=None, status="unknown", reason="not_collected", evidence_sha256=None)  # type: ignore[index]
    assert comparison_eligibility(baseline, candidate, metric="wall_s").eligible
    assert not comparison_eligibility(baseline, candidate, metric="cost_usd").eligible


@pytest.mark.parametrize("metric", ["", "context_total", None, [], "private-sentinel"])
def test_metric_selector_refused_before_records(metric) -> None:
    with pytest.raises(ValueError, match="^invalid_metric$"):
        comparison_eligibility(object(), object(), metric=metric)  # type: ignore[arg-type]


def test_all_reasons_are_sorted_and_workload_mismatch_blocks() -> None:
    left, right = valid_record(), valid_record()
    right["run_id"] = left["run_id"]
    right["grade"]["run_id"] = right["run_id"]  # type: ignore[index]
    right["provenance"]["workload_sha256"] = "9" * 64  # type: ignore[index]
    result = comparison_eligibility(left, right, metric="input")
    assert not result.eligible
    assert result.reasons == tuple(sorted(set(result.reasons)))
    assert "same_run" in result.reasons
    assert "workload_mismatch" in result.reasons


def test_input_is_not_mutated() -> None:
    left, right = valid_record(), valid_record()
    before_left, before_right = copy.deepcopy(left), copy.deepcopy(right)
    comparison_eligibility(left, right, metric="input")
    assert left == before_left
    assert right == before_right


def test_documented_care_example_validates() -> None:
    from pathlib import Path
    import re

    text = Path("docs/TASK_EVAL.md").read_text()
    match = re.search(r"```json\n(\{.*?\})\n```", text, re.S)
    assert match is not None
    example = json.loads(match.group(1))
    assert public_record(example)["schema"] == SCHEMA
