"""Tests for ciao/evals.py."""

from __future__ import annotations

import json
from pathlib import Path
from ciao.evals import (
    EvalScenario,
    evaluate_output,
    load_eval_scenarios,
    run_eval_scenario,
    scaffold_eval,
)


def test_evaluate_output_passing():
    scenario = EvalScenario(
        name="test_summary",
        expected_patterns=[r"summary", r"version \d+"],
        forbidden_patterns=[r"error", r"failed"],
        expected_tools=["Bash"],
    )

    output = "Here is the summary of version 2."
    res = evaluate_output(scenario, output, used_tools=["Bash", "Read"])

    assert res.passed
    assert res.tool_match
    assert len(res.pattern_results) == 4


def test_evaluate_output_failing_forbidden_pattern():
    scenario = EvalScenario(
        name="test_summary",
        forbidden_patterns=[r"fatal error"],
    )

    output = "Execution failed with fatal error in step 1."
    res = evaluate_output(scenario, output, used_tools=[])

    assert not res.passed
    assert not res.pattern_results[0]["matched"]  # expected False, but matched pattern so matched=False


def test_scaffold_and_load_evals(tmp_path: Path):
    target = scaffold_eval(tmp_path, "code_review_eval")
    assert target.is_file()

    scenarios = load_eval_scenarios(tmp_path)
    assert len(scenarios) == 1
    assert scenarios[0].name == "code_review_eval"


def test_run_eval_scenario_custom_runner():
    scenario = EvalScenario(
        name="custom_test",
        expected_patterns=[r"hello world"],
    )

    def custom_runner(sc: EvalScenario):
        return "hello world", ["CustomTool"]

    result = run_eval_scenario(scenario, runner_fn=custom_runner)
    assert result.passed
    assert result.output == "hello world"
