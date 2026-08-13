"""Tests for strict, declarative eval suite parsing and evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ciao.evals import (
    EvalAssertions,
    EvalScenario,
    EvalSchemaError,
    EvalTarget,
    evaluate_output,
    load_eval_suite,
    normalize_tool_identifier,
)


def _suite_data() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "core-agent-smoke",
        "defaults": {
            "provider": "claude",
            "model": "sonnet",
            "surface": "mcp",
        },
        "scenarios": [
            {
                "name": "researcher-uses-web",
                "description": "The researcher cites an official source.",
                "target": {"kind": "subagent", "name": "researcher"},
                "prompt": "Find the current Python release.",
                "provider": "codex",
                "model": "gpt-5",
                "surface": "legacy",
                "assertions": {
                    "output_contains": ["Python"],
                    "output_not_contains": ["I cannot"],
                    "output_regex": [r"https://"],
                    "output_not_regex": [r"(?i)unverified"],
                    "required_tools": ["web.run"],
                    "forbidden_tools": ["shell.exec"],
                },
            }
        ],
    }


def _write_suite(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _assert_schema_error(tmp_path: Path, data: object, expected: str) -> None:
    with pytest.raises(EvalSchemaError, match=expected):
        load_eval_suite(_write_suite(tmp_path, data))


def test_loads_strict_schema_version_one(tmp_path: Path):
    suite = load_eval_suite(_write_suite(tmp_path, _suite_data()))

    assert suite.schema_version == 1
    assert suite.name == "core-agent-smoke"
    assert suite.defaults.provider == "claude"
    assert suite.defaults.model == "sonnet"
    assert suite.defaults.surface == "mcp"
    assert len(suite.scenarios) == 1
    scenario = suite.scenarios[0]
    assert scenario.target == EvalTarget(kind="subagent", name="researcher")
    assert scenario.provider == "codex"
    assert scenario.model == "gpt-5"
    assert scenario.surface == "legacy"
    assert scenario.assertions.required_tools == ("web.run",)


def test_rejects_unknown_top_level_field_with_json_path(tmp_path: Path):
    data = _suite_data()
    data["extra"] = True
    _assert_schema_error(tmp_path, data, r"\$\.extra: unknown field")


def test_rejects_unsupported_schema_version(tmp_path: Path):
    data = _suite_data()
    data["schema_version"] = 2
    _assert_schema_error(tmp_path, data, r"\$\.schema_version: unsupported schema version 2")


def test_rejects_boolean_schema_version_even_though_bool_is_an_int(tmp_path: Path):
    data = _suite_data()
    data["schema_version"] = True
    _assert_schema_error(tmp_path, data, r"\$\.schema_version: expected integer")


def test_rejects_empty_scenario_list(tmp_path: Path):
    data = _suite_data()
    data["scenarios"] = []
    _assert_schema_error(tmp_path, data, r"\$\.scenarios: must contain at least one scenario")


def test_rejects_duplicate_scenario_names(tmp_path: Path):
    data = _suite_data()
    data["scenarios"].append(dict(data["scenarios"][0]))
    _assert_schema_error(tmp_path, data, r"\$\.scenarios\[1\]\.name: duplicate scenario name")


def test_rejects_invalid_provider(tmp_path: Path):
    data = _suite_data()
    data["scenarios"][0]["provider"] = "openrouter"
    _assert_schema_error(
        tmp_path,
        data,
        r"\$\.scenarios\[0\]\.provider: expected one of: claude, codex",
    )


def test_rejects_invalid_regex_during_parse(tmp_path: Path):
    data = _suite_data()
    data["scenarios"][0]["assertions"]["output_regex"] = ["["]
    _assert_schema_error(
        tmp_path,
        data,
        r"\$\.scenarios\[0\]\.assertions\.output_regex\[0\]: invalid regex",
    )


def test_rejects_scenario_without_assertions(tmp_path: Path):
    data = _suite_data()
    data["scenarios"][0]["assertions"] = {
        "output_contains": [],
        "output_not_contains": [],
        "output_regex": [],
        "output_not_regex": [],
        "required_tools": [],
        "forbidden_tools": [],
    }
    _assert_schema_error(
        tmp_path,
        data,
        r"\$\.scenarios\[0\]\.assertions: must contain at least one assertion",
    )


def test_rejects_target_without_name(tmp_path: Path):
    data = _suite_data()
    data["scenarios"][0]["target"]["name"] = " \t"
    _assert_schema_error(
        tmp_path,
        data,
        r"\$\.scenarios\[0\]\.target\.name: must not be empty",
    )


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda data: data["defaults"].update({"unexpected": "value"}),
            r"\$\.defaults\.unexpected: unknown field",
        ),
        (
            lambda data: data["scenarios"][0].update({"unexpected": "value"}),
            r"\$\.scenarios\[0\]\.unexpected: unknown field",
        ),
        (
            lambda data: data["scenarios"][0]["target"].update({"unexpected": "value"}),
            r"\$\.scenarios\[0\]\.target\.unexpected: unknown field",
        ),
        (
            lambda data: data["scenarios"][0]["assertions"].update(
                {"unexpected": ["value"]}
            ),
            r"\$\.scenarios\[0\]\.assertions\.unexpected: unknown field",
        ),
    ],
)
def test_rejects_unknown_nested_fields_with_json_path(
    tmp_path: Path,
    mutate: Any,
    expected: str,
):
    data = _suite_data()
    mutate(data)
    _assert_schema_error(tmp_path, data, expected)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda data: data.update({"name": 4}), r"\$\.name: expected string"),
        (
            lambda data: data.update({"defaults": []}),
            r"\$\.defaults: expected object",
        ),
        (
            lambda data: data["defaults"].update({"model": False}),
            r"\$\.defaults\.model: expected string",
        ),
        (
            lambda data: data.update({"scenarios": {}}),
            r"\$\.scenarios: expected array",
        ),
        (
            lambda data: data["scenarios"][0].update({"description": None}),
            r"\$\.scenarios\[0\]\.description: expected string",
        ),
        (
            lambda data: data["scenarios"][0].update({"prompt": ["hello"]}),
            r"\$\.scenarios\[0\]\.prompt: expected string",
        ),
        (
            lambda data: data["scenarios"][0]["assertions"].update(
                {"required_tools": "web.run"}
            ),
            r"\$\.scenarios\[0\]\.assertions\.required_tools: expected array",
        ),
        (
            lambda data: data["scenarios"][0]["assertions"].update(
                {"output_contains": ["Python", 3]}
            ),
            r"\$\.scenarios\[0\]\.assertions\.output_contains\[1\]: expected string",
        ),
    ],
)
def test_rejects_wrong_types_with_json_path(
    tmp_path: Path,
    mutate: Any,
    expected: str,
):
    data = _suite_data()
    mutate(data)
    _assert_schema_error(tmp_path, data, expected)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda data: data.pop("name"), r"\$\.name: missing required field"),
        (
            lambda data: data["defaults"].pop("surface"),
            r"\$\.defaults\.surface: missing required field",
        ),
        (
            lambda data: data["scenarios"][0].pop("prompt"),
            r"\$\.scenarios\[0\]\.prompt: missing required field",
        ),
        (
            lambda data: data["scenarios"][0]["target"].pop("kind"),
            r"\$\.scenarios\[0\]\.target\.kind: missing required field",
        ),
        (
            lambda data: data["scenarios"][0].pop("assertions"),
            r"\$\.scenarios\[0\]\.assertions: missing required field",
        ),
    ],
)
def test_rejects_missing_required_fields_with_json_path(
    tmp_path: Path,
    mutate: Any,
    expected: str,
):
    data = _suite_data()
    mutate(data)
    _assert_schema_error(tmp_path, data, expected)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda data: data.update({"name": "\n"}), r"\$\.name: must not be empty"),
        (
            lambda data: data["scenarios"][0].update({"name": " "}),
            r"\$\.scenarios\[0\]\.name: must not be empty",
        ),
        (
            lambda data: data["scenarios"][0].update({"prompt": "\t"}),
            r"\$\.scenarios\[0\]\.prompt: must not be empty",
        ),
        (
            lambda data: data["defaults"].update({"model": " "}),
            r"\$\.defaults\.model: must not be empty",
        ),
    ],
)
def test_rejects_empty_required_strings(tmp_path: Path, mutate: Any, expected: str):
    data = _suite_data()
    mutate(data)
    _assert_schema_error(tmp_path, data, expected)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda data: data["defaults"].update({"surface": "api"}),
            r"\$\.defaults\.surface: expected one of: legacy, mcp",
        ),
        (
            lambda data: data["scenarios"][0]["target"].update({"kind": "agent"}),
            r"\$\.scenarios\[0\]\.target\.kind: expected one of: skill, subagent",
        ),
    ],
)
def test_rejects_unsupported_enum_values(tmp_path: Path, mutate: Any, expected: str):
    data = _suite_data()
    mutate(data)
    _assert_schema_error(tmp_path, data, expected)


def test_scenario_routing_overrides_are_optional(tmp_path: Path):
    data = _suite_data()
    scenario = data["scenarios"][0]
    scenario.pop("provider")
    scenario.pop("model")
    scenario.pop("surface")

    parsed = load_eval_suite(_write_suite(tmp_path, data)).scenarios[0]

    assert parsed.provider is None
    assert parsed.model is None
    assert parsed.surface is None


def test_contains_checks_are_case_sensitive():
    scenario = EvalScenario(
        name="case-sensitive",
        description="",
        target=EvalTarget(kind="skill", name="example"),
        prompt="Run it.",
        provider=None,
        model=None,
        surface=None,
        assertions=EvalAssertions(output_contains=("Python",)),
    )

    result = evaluate_output(scenario, "python", used_tools=[])

    assert not result.passed
    assert result.assertion_results[0].kind == "output_contains"
    assert not result.assertion_results[0].passed


def test_regex_checks_use_patterns_validated_during_parse(tmp_path: Path):
    scenario = load_eval_suite(_write_suite(tmp_path, _suite_data())).scenarios[0]

    result = evaluate_output(
        scenario,
        "Python docs: https://python.org",
        used_tools=["web.run"],
    )

    assert result.passed
    assert all(assertion.passed for assertion in result.assertion_results)


def test_tool_matching_uses_normalized_exact_identifiers():
    scenario = EvalScenario(
        name="tools",
        description="",
        target=EvalTarget(kind="skill", name="example"),
        prompt="Run it.",
        provider=None,
        model=None,
        surface=None,
        assertions=EvalAssertions(
            required_tools=("web.run",),
            forbidden_tools=("shell.exec",),
        ),
    )

    exact = evaluate_output(scenario, "done", used_tools=[" MCP__WEB__RUN "])
    suffix_only = evaluate_output(scenario, "done", used_tools=["other.web.run"])
    forbidden = evaluate_output(scenario, "done", used_tools=["shell.exec"])

    assert exact.passed
    assert not suffix_only.passed
    assert not forbidden.passed
    assert exact.normalized_tools == ("web.run",)
    assert normalize_tool_identifier("mcp__Web__Run") == "web.run"
    assert normalize_tool_identifier(" Bash ") == "bash"


def test_execution_error_fails_without_evaluating_assertions():
    scenario = EvalScenario(
        name="error",
        description="",
        target=EvalTarget(kind="skill", name="example"),
        prompt="Run it.",
        provider=None,
        model=None,
        surface=None,
        assertions=EvalAssertions(output_contains=("done",)),
    )

    result = evaluate_output(
        scenario,
        "",
        used_tools=[],
        duration_s=0.5,
        error="TimeoutError: timed out",
    )

    assert not result.passed
    assert result.error == "TimeoutError: timed out"
    assert result.assertion_results == ()
    assert result.duration_s == 0.5


def test_empty_execution_error_runs_assertions():
    scenario = EvalScenario(
        name="empty-error",
        description="",
        target=EvalTarget(kind="skill", name="example"),
        prompt="Run it.",
        provider=None,
        model=None,
        surface=None,
        assertions=EvalAssertions(output_contains=("done",)),
    )

    result = evaluate_output(scenario, "done", used_tools=[], error="")

    assert result.passed
    assert result.error is None
    assert result.assertion_results[0].passed


def test_direct_scenario_without_assertions_fails_closed():
    scenario = EvalScenario(
        name="vacuous",
        description="",
        target=EvalTarget(kind="skill", name="example"),
        prompt="Run it.",
        provider=None,
        model=None,
        surface=None,
        assertions=EvalAssertions(),
    )

    result = evaluate_output(scenario, "anything", used_tools=[])

    assert not result.passed
    assert result.error == "Scenario has no effective assertions"
    assert result.assertion_results == ()


def test_direct_scenario_with_invalid_regex_fails_deterministically():
    scenario = EvalScenario(
        name="invalid-regex",
        description="",
        target=EvalTarget(kind="skill", name="example"),
        prompt="Run it.",
        provider=None,
        model=None,
        surface=None,
        assertions=EvalAssertions(output_regex=("[",)),
    )

    result = evaluate_output(scenario, "anything", used_tools=[])

    assert not result.passed
    assert result.error is not None
    assert result.error.startswith("Invalid regex at assertions.output_regex[0]:")
    assert result.assertion_results == ()
