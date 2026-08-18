from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.cli import main
from ciao.eval_runner import ChatObservation
from ciao.release_evidence import (
    compare_summaries,
    collect_inventory,
    diff_inventories,
    load_release_suite,
    normalize_usage_metrics,
    parse_release_suite,
    run_release_evidence,
    summarize_values,
)


def _observation(scenario: str, *, provider: str, memory: bool = False) -> ChatObservation:
    return ChatObservation(
        scenario=scenario,
        selected_model="sonnet",
        effective_model="provider-model",
        final_text="workspace project chat written updates on Fridays RELEASE_MEMORY_OK RELEASE_MEMORY_REPEAT_OK RELEASE_CAPABILITIES_OK RELEASE_SUBAGENT_OK Hook",
        error="",
        elapsed_ms=120,
        provider_duration_ms=80,
        usage=(
            {
                "input_tokens": "100",
                "output_tokens": "20",
                "cache_creation_input_tokens": "30",
                "cache_read_input_tokens": "70",
                "context_pct": "12.0%",
            }
            if provider == "claude"
            else {
                "input_tokens": "100",
                "cached_input_tokens": "70",
                "output_tokens": "20",
                "context_window": "1000",
                "context_pct": "12.0%",
            }
        ),
        tokens=120,
        provider_tools=(),
        mcp_tools=("vault_search", "projects_list", "agent")
        if memory
        else ("projects_list", "agent"),
        mcp_errors=0,
        mcp_result_paths=("personal/Workspace/People/Ada.md",)
        if memory
        else (),
        mcp_tool_durations_ms=(4, 8),
    )


def test_release_suite_fixture_is_schema_v2() -> None:
    suite = load_release_suite(Path("evals/release.json"))

    assert suite.schema_version == 2
    assert suite.providers == ("claude", "codex", "opencode")
    assert len(suite.scenarios) == 3
    assert len(suite.scenarios[1].turns) == 2
    assert suite.scenarios[1].vault_files[0][0].endswith("Ada.md")


def test_release_suite_rejects_duplicate_provider() -> None:
    value = {
        "schema_version": 2,
        "name": "bad",
        "defaults": {"providers": ["claude", "claude"], "model": "sonnet", "surface": "mcp"},
        "scenarios": [],
    }

    with pytest.raises(ValueError, match="duplicate provider"):
        parse_release_suite(value)


def test_provider_usage_metrics_keep_cache_semantics_separate() -> None:
    claude = normalize_usage_metrics(
        "claude",
        {
            "input_tokens": "10",
            "cache_creation_input_tokens": "20",
            "cache_read_input_tokens": "30",
            "context_pct": "25.0%",
        },
    )
    codex = normalize_usage_metrics(
        "codex",
        {"input_tokens": "100", "cached_input_tokens": "40", "context_window": "1000"},
    )

    assert claude["cache_read_share"] == 0.6
    assert "cached_input_share" not in claude
    assert codex["cached_input_share"] == 0.4
    assert codex["context_window_tokens"] == 1000


def test_summary_statistics_are_deterministic() -> None:
    summary = summarize_values([1, 2, 3, 4, 5])

    assert summary == {"count": 5, "min": 1.0, "median": 3.0, "p95": 5.0, "max": 5.0}


def test_inventory_diff_reports_public_structural_changes(tmp_path: Path) -> None:
    skill = tmp_path / "ciao" / "stock" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\nDemo\n", encoding="utf-8")
    (tmp_path / "ciao").mkdir(exist_ok=True)
    (tmp_path / "ciao" / "mcp_server.py").write_text(
        '        @tool(name="vault_search", annotations=_READ)\n'
        '        async def vault_search():\n'
        '            return {}\n',
        encoding="utf-8",
    )
    before = collect_inventory(tmp_path)
    skill.write_text("---\nname: demo\n---\nChanged\n", encoding="utf-8")
    (tmp_path / "ciao" / "mcp_server.py").write_text(
        '        @tool(name="vault_search", annotations=_WRITE)\n'
        '        async def vault_search():\n'
        '            return {}\n',
        encoding="utf-8",
    )
    after = collect_inventory(tmp_path)
    diff = diff_inventories(before, after)

    assert diff["categories"]["skills"]["changed"]
    assert diff["categories"]["mcp_tools"]["changed"]


def test_release_evidence_runs_provider_mode_matrix_without_publishing_outputs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "release-evidence" / "v1.0.0"

    def fake_runner(scenario, provider, mode, **_kwargs):
        memory = scenario.name == "vault-recall-and-persistence"
        return [
            _observation(
                f"{scenario.name}:{index}",
                provider=provider,
                memory=memory,
            )
            for index, _turn in enumerate(scenario.turns)
        ]

    result = run_release_evidence(
        suite_path=Path("evals/release.json"),
        workspace=workspace,
        output=output,
        version="1.0.0",
        repeats=3,
        mode_runner=fake_runner,
        require_complete=True,
    )

    assert result.complete is True
    # 3 scenarios x 3 providers (claude, codex, opencode) x 3 modes (cold, warm, restart).
    assert len(result.summary["groups"]) == 27
    assert (output / "REPORT.md").is_file()
    public_text = (output / "REPORT.md").read_text(encoding="utf-8")
    assert "written updates" not in public_text
    assert "RELEASE_MEMORY_OK" not in public_text
    payload = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert all("output" not in row for row in payload["groups"])


def test_compare_summaries_flags_quality_and_advisory_regressions() -> None:
    baseline = {
        "groups": [
            {
                "scenario": "one",
                "provider": "claude",
                "mode": "cold",
                "repetitions": 3,
                "passed": 3,
                "elapsed_ms": {"median": 100},
                "cache_read_share": {"median": 0.8},
            }
        ]
    }
    current = {
        "groups": [
            {
                "scenario": "one",
                "provider": "claude",
                "mode": "cold",
                "repetitions": 3,
                "passed": 2,
                "elapsed_ms": {"median": 120},
                "cache_read_share": {"median": 0.6},
            }
        ]
    }

    comparison = compare_summaries(baseline, current)
    kinds = {flag["kind"] for flag in comparison["flags"]}
    assert "quality_regression" in kinds
    assert "advisory_regression" in kinds
    assert "cache_regression" in kinds


def test_compare_summaries_compares_pass_rate_across_repeat_counts() -> None:
    """Different --repeats values must not produce pass-count false regressions."""

    def run(baseline_passed: int, baseline_repeats: int, current_passed: int, current_repeats: int):
        baseline = {
            "groups": [
                {
                    "scenario": "one",
                    "provider": "claude",
                    "mode": "cold",
                    "repetitions": baseline_repeats,
                    "passed": baseline_passed,
                    "elapsed_ms": {"median": 100},
                    "cache_read_share": {"median": 0.5},
                }
            ]
        }
        current = {
            "groups": [
                {
                    "scenario": "one",
                    "provider": "claude",
                    "mode": "cold",
                    "repetitions": current_repeats,
                    "passed": current_passed,
                    "elapsed_ms": {"median": 100},
                    "cache_read_share": {"median": 0.5},
                }
            ]
        }
        return compare_summaries(baseline, current)

    # 3/3 vs 2/2 same rate -> no quality regression.
    assert all(
        flag["kind"] != "quality_regression"
        for flag in run(3, 3, 2, 2)["flags"]
    )
    # 2/2 vs 1/3 strictly worse -> quality regression.
    assert any(
        flag["kind"] == "quality_regression"
        for flag in run(2, 2, 1, 3)["flags"]
    )
    # 3/3 vs 2/2 same rate -> no quality regression.
    assert all(
        flag["kind"] != "quality_regression"
        for flag in run(3, 3, 2, 2)["flags"]
    )
    # 2/3 baseline vs 2/2 current is an improvement (1.0 > 0.667) -> no regression.
    assert all(
        flag["kind"] != "quality_regression"
        for flag in run(2, 3, 2, 2)["flags"]
    )
    # 3/3 baseline vs 1/2 current is a regression (0.5 < 1.0) -> quality_regression.
    assert any(
        flag["kind"] == "quality_regression"
        for flag in run(3, 3, 1, 2)["flags"]
    )


def test_compare_summaries_uses_provider_appropriate_cache_metric() -> None:
    """A real Codex cache-hit drop must surface even when cache_read_share is null."""

    baseline = {
        "groups": [
            {
                "scenario": "one",
                "provider": "codex",
                "mode": "cold",
                "repetitions": 3,
                "passed": 3,
                "elapsed_ms": {"median": 100},
                "cache_read_share": {"median": None},
                "cached_input_share": {"median": 0.5},
            }
        ]
    }
    current = {
        "groups": [
            {
                "scenario": "one",
                "provider": "codex",
                "mode": "cold",
                "repetitions": 3,
                "passed": 3,
                "elapsed_ms": {"median": 100},
                "cache_read_share": {"median": None},
                "cached_input_share": {"median": 0.2},
            }
        ]
    }

    kinds = {flag["kind"] for flag in compare_summaries(baseline, current)["flags"]}
    assert "cache_regression" in kinds

    # Claude rows that lack cache_read_share by accident still find the metric.
    claude_baseline = dict(baseline["groups"][0], provider="claude", cache_read_share={"median": 0.8}, cached_input_share={"median": None})
    claude_current = dict(current["groups"][0], provider="claude", cache_read_share={"median": 0.6}, cached_input_share={"median": None})
    claude_kinds = {
        flag["kind"]
        for flag in compare_summaries(
            {"groups": [claude_baseline]}, {"groups": [claude_current]}
        )["flags"]
    }
    assert "cache_regression" in claude_kinds


def test_eval_compare_cli_is_advisory(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps({"groups": []}), encoding="utf-8")
    current.write_text(json.dumps({"groups": []}), encoding="utf-8")

    assert main(
        [
            "eval",
            "compare",
            "--baseline",
            str(baseline),
            "--current",
            str(current),
            "--json",
        ]
    ) == 0


def test_external_vault_mode_redacts_result_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "private-vault"
    external.mkdir()
    (external / "Private.md").write_text("private", encoding="utf-8")

    def fake_runner(scenario, provider, mode, **_kwargs):
        return [_observation(scenario.name, provider=provider, memory=True)] * len(scenario.turns)

    result = run_release_evidence(
        suite_path=Path("evals/release.json"),
        workspace=workspace,
        output=tmp_path / "out",
        version="1.0.0",
        modes=("cold",),
        repeats=1,
        external_vault=external,
        mode_runner=fake_runner,
        require_complete=False,
    )

    public_paths = " ".join(
        str(row.get("source_paths")) for row in result.summary["groups"]
    )
    assert "memory-vault/personal/Workspace/People/Ada.md" not in public_paths
    assert "source-" in public_paths


def test_current_inventory_uses_to_ref_not_worktree(tmp_path: Path) -> None:
    """`run_release_evidence` must inventory the requested ref, not dirty files.

    Reproduces the P1 Codex finding: when the workspace contains
    untracked or ignored files (e.g. ``__pycache__`` or ``.pyc``) the
    inventory was capturing them, polluting ``changes.json`` and
    reporting machine-local noise as part of the release.
    """
    import subprocess

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=workspace, check=True)
    # Tracked skills directory at v1.
    tracked = workspace / "ciao" / "stock" / "skills" / "demo" / "SKILL.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("---\nname: demo\n---\nv1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v1"], cwd=workspace, check=True)
    initial_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True, stdout=subprocess.PIPE, check=True
    ).stdout.strip()
    # Commit v2.
    tracked.write_text("---\nname: demo\n---\nv2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v2"], cwd=workspace, check=True)
    head_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True, stdout=subprocess.PIPE, check=True
    ).stdout.strip()
    # Pollute the worktree with untracked and ignored files.
    (workspace / "ciao" / "stock" / "skills" / "demo" / "stale.pyc").write_bytes(b"\x00\x01")
    (workspace / "untracked_dir").mkdir()
    (workspace / "untracked_dir" / "noise.md").write_text("dirty", encoding="utf-8")

    def fake_runner(scenario, provider, mode, **_kwargs):
        return [_observation(scenario.name, provider=provider)] * len(scenario.turns)

    result = run_release_evidence(
        suite_path=Path("evals/release.json"),
        workspace=workspace,
        output=tmp_path / "out",
        version="1.0.0",
        modes=("cold",),
        repeats=1,
        from_ref=initial_ref,
        to_ref=head_ref,
        mode_runner=fake_runner,
        require_complete=False,
    )

    skills = result.changes["categories"]["skills"]
    assert skills["changed"]
    changed_paths = {entry["after"]["path"] for entry in skills["changed"]}
    assert "ciao/stock/skills/demo/SKILL.md" in changed_paths
    assert "stale.pyc" not in " ".join(changed_paths)
    assert "untracked_dir/noise.md" not in str(result.changes)


def test_memory_leakage_undefined_without_negative_assertions(tmp_path: Path) -> None:
    """A turn without ``output_not_contains``/``output_not_regex`` must report None.

    Reproduces the P2 Codex finding: ``all(passed for passed in [])`` is
    ``True``, so the empty negative-assertion case used to be reported
    as ``0.0`` (clean leakage), falsely claiming the scenario was
    measured. Comparisons then could suppress or create
    ``memory_leakage_regression`` flags on the basis of a measurement
    that never happened.
    """
    from ciao.evals import EvalAssertionResult, EvalResult
    from ciao.release_evidence import _public_assertions

    # No negative assertions at all: leakage must be None, not 0.0.
    result_no_neg = EvalResult(
        scenario_name="capabilities",
        passed=True,
        duration_s=0.1,
        assertion_results=(),
        normalized_tools=(),
        output="",
    )
    _, memory_none = _public_assertions(result_no_neg, (), ())
    assert memory_none["memory_leakage"] is None

    # Negative assertion that passed: leakage must be 0.0.
    result_clean = EvalResult(
        scenario_name="vault",
        passed=True,
        duration_s=0.1,
        assertion_results=(
            EvalAssertionResult(kind="output_not_contains", expected="VAULT_PRIVATE", passed=True),
        ),
        normalized_tools=(),
        output="",
    )
    _, memory_clean = _public_assertions(result_clean, (), ())
    assert memory_clean["memory_leakage"] == 0.0

    # Negative assertion that failed: leakage must be 1.0.
    result_leaked = EvalResult(
        scenario_name="vault",
        passed=True,
        duration_s=0.1,
        assertion_results=(
            EvalAssertionResult(kind="output_not_contains", expected="VAULT_PRIVATE", passed=False),
        ),
        normalized_tools=(),
        output="",
    )
    _, memory_leaked = _public_assertions(result_leaked, (), ())
    assert memory_leaked["memory_leakage"] == 1.0
