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
    assert suite.providers == ("claude", "codex")
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
    assert len(result.summary["groups"]) == 18
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
