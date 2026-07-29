"""Native Agent Evals Harness for Ciaobot."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ciao.jsonio import read_json_dict


@dataclass(slots=True)
class EvalScenario:
    """Definition of a declarative agent eval scenario."""

    name: str
    description: str = ""
    prompt: str = ""
    expected_tools: list[str] = field(default_factory=list)
    expected_patterns: list[str] = field(default_factory=list)
    forbidden_patterns: list[str] = field(default_factory=list)
    model: str = ""
    provider: str = ""


@dataclass(slots=True)
class EvalResult:
    """Result of running one eval scenario."""

    scenario_name: str
    passed: bool
    duration_s: float
    tool_match: bool
    pattern_results: list[dict[str, Any]] = field(default_factory=list)
    output: str = ""
    error: str | None = None


def parse_eval_scenario(path: Path) -> EvalScenario | None:
    """Parse a single JSON eval scenario file."""
    if not path.is_file() or path.suffix != ".json":
        return None
    try:
        data = read_json_dict(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    if "prompt" not in data:
        return None

    return EvalScenario(
        name=data.get("name", path.stem),
        description=str(data.get("description", "")),
        prompt=str(data.get("prompt", "")),
        expected_tools=list(data.get("expected_tools", [])),
        expected_patterns=list(data.get("expected_patterns", [])),
        forbidden_patterns=list(data.get("forbidden_patterns", [])),
        model=str(data.get("model", "")),
        provider=str(data.get("provider", "")),
    )


def load_eval_scenarios(workspace: Path) -> list[EvalScenario]:
    """Discover all eval scenario JSON files in `<workspace>/evals/` and `.runtime/evals/`."""
    scenarios: list[EvalScenario] = []
    search_dirs = (workspace / "evals", workspace / ".runtime" / "evals")

    for sdir in search_dirs:
        if sdir.is_dir():
            for entry in sorted(sdir.glob("*.json"), key=lambda p: p.name):
                scenario = parse_eval_scenario(entry)
                if scenario:
                    scenarios.append(scenario)
    return scenarios


def evaluate_output(
    scenario: EvalScenario,
    output: str,
    used_tools: list[str] | None = None,
    duration_s: float = 0.0,
    error: str | None = None,
) -> EvalResult:
    """Evaluate an output string against scenario assertions."""
    if error:
        return EvalResult(
            scenario_name=scenario.name,
            passed=False,
            duration_s=duration_s,
            tool_match=False,
            output=output,
            error=error,
        )

    # Check tools
    used_tools_set = set(used_tools or [])
    tool_match = all(t in used_tools_set for t in scenario.expected_tools)

    pattern_results: list[dict[str, Any]] = []
    all_patterns_ok = True

    # Expected patterns (must match)
    for pat in scenario.expected_patterns:
        try:
            matched = bool(re.search(pat, output, re.IGNORECASE | re.MULTILINE))
        except re.error as exc:
            matched = False
            error = f"Invalid regex '{pat}': {exc}"
        pattern_results.append({"pattern": pat, "expected": True, "matched": matched})
        if not matched:
            all_patterns_ok = False

    # Forbidden patterns (must NOT match)
    for pat in scenario.forbidden_patterns:
        try:
            matched = bool(re.search(pat, output, re.IGNORECASE | re.MULTILINE))
        except re.error as exc:
            matched = True
            error = f"Invalid regex '{pat}': {exc}"
        pattern_results.append({"pattern": pat, "expected": False, "matched": not matched})
        if matched:
            all_patterns_ok = False

    passed = tool_match and all_patterns_ok and error is None

    return EvalResult(
        scenario_name=scenario.name,
        passed=passed,
        duration_s=duration_s,
        tool_match=tool_match,
        pattern_results=pattern_results,
        output=output,
        error=error,
    )


def run_eval_scenario(
    scenario: EvalScenario,
    runner_fn: Callable[[EvalScenario], tuple[str, list[str] | None]] | None = None,
) -> EvalResult:
    """Run a single eval scenario using a runner callback or default oneshot provider."""
    start_t = time.monotonic()
    if runner_fn is not None:
        try:
            output, used_tools = runner_fn(scenario)
            duration = time.monotonic() - start_t
            return evaluate_output(scenario, output, used_tools, duration)
        except Exception as exc:  # noqa: BLE001
            duration = time.monotonic() - start_t
            return evaluate_output(scenario, "", None, duration, error=f"{type(exc).__name__}: {exc}")

    # Default: call provider oneshot if runner_fn not supplied
    try:
        import asyncio
        from ciao.providers.oneshot import run_oneshot

        output = asyncio.run(
            run_oneshot(
                prompt=scenario.prompt,
                system_prompt="",
                model=scenario.model or "sonnet",
            )
        )
        duration = time.monotonic() - start_t
        return evaluate_output(scenario, output, [], duration)
    except Exception as exc:  # noqa: BLE001
        duration = time.monotonic() - start_t
        return evaluate_output(scenario, "", None, duration, error=f"{type(exc).__name__}: {exc}")


def run_eval_suite(
    workspace: Path,
    scenario_filter: str | None = None,
    runner_fn: Callable[[EvalScenario], tuple[str, list[str] | None]] | None = None,
) -> list[EvalResult]:
    """Run all discovered eval scenarios in the workspace matching optional filter."""
    scenarios = load_eval_scenarios(workspace)
    if scenario_filter:
        scenarios = [s for s in scenarios if scenario_filter.lower() in s.name.lower()]

    results: list[EvalResult] = []
    for scenario in scenarios:
        res = run_eval_scenario(scenario, runner_fn=runner_fn)
        results.append(res)
    return results


def scaffold_eval(workspace: Path, name: str) -> Path:
    """Scaffold a new JSON eval scenario file under `<workspace>/evals/<name>.json`."""
    evals_dir = workspace / "evals"
    evals_dir.mkdir(parents=True, exist_ok=True)

    target = evals_dir / f"{name}.json"
    if not target.exists():
        data = {
            "name": name,
            "description": f"Evaluation scenario for {name}",
            "prompt": "Enter prompt to test...",
            "expected_tools": [],
            "expected_patterns": [],
            "forbidden_patterns": [],
            "model": "sonnet",
        }
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return target
