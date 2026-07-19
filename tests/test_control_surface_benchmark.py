from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ciao.control_surface_benchmark import (
    RunContext,
    RunResult,
    promote_decision,
    scenarios,
    summarize,
    token_count,
)
from ciao.control_surfaces import load_decision, resolve_auto_surface


def _result(
    surface: str,
    *,
    correct: bool = True,
    compliant: bool = True,
    elapsed: int = 1000,
    tokens: int = 100,
    tools: int = 1,
) -> RunResult:
    return RunResult(
        provider="claude",
        surface=surface,  # type: ignore[arg-type]
        scenario="memory_add",
        repeat=1,
        marker="marker",
        chat_id=f"chat-{surface}",
        correct=correct,
        validation="ok",
        surface_compliant=compliant,
        elapsed_ms=elapsed,
        duration_ms=elapsed,
        usage={"input_tokens": str(tokens - 10), "output_tokens": "10"},
        tokens=tokens,
        provider_tools=["tool"] * tools,
        mcp_tools=["memory_add"] if surface == "mcp" else [],
        mcp_errors=0,
        final_text="done",
    )


def test_fixed_release_suite_has_twelve_unique_scenarios() -> None:
    names = [scenario.name for scenario in scenarios()]

    assert len(names) == 12
    assert len(set(names)) == 12
    assert {"memory_add", "project_chat_create", "schedule_create", "loop_create"} <= set(names)


def test_project_chat_validator_reads_ids_from_registry_keys(tmp_path) -> None:
    marker = "project-marker"
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "web_projects.json").write_text(
        json.dumps(
            {
                "projects": {
                    "proj-created": {"name": f"Project {marker}", "workspace": "work"}
                },
                "chats": {
                    "chat-created": {
                        "project_id": "proj-created",
                        "title": f"Chat {marker}",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    scenario = next(item for item in scenarios() if item.name == "project_chat_create")
    context = RunContext(
        provider="codex",
        surface="mcp",
        repeat=1,
        marker=marker,
        server=SimpleNamespace(root=tmp_path, workspace_name="work"),
        project_id="proj-benchmark",
        project_name="Benchmark",
        chat_id="chat-benchmark",
        chat_title="Benchmark",
    )

    assert scenario.validate(context, "") == (True, "project and chat created")


def test_token_count_handles_provider_cache_semantics() -> None:
    assert token_count(
        "claude",
        {
            "input_tokens": "10",
            "cache_creation_input_tokens": "20",
            "cache_read_input_tokens": "30",
            "output_tokens": "5",
        },
    ) == 65
    assert token_count(
        "codex",
        {"input_tokens": "100", "cached_input_tokens": "80", "output_tokens": "5"},
    ) == 105


def test_summary_selects_only_eligible_arm() -> None:
    rows = [
        *[_result("legacy", correct=False) for _ in range(4)],
        *[_result("mcp", elapsed=800, tokens=80) for _ in range(4)],
    ]

    result = summarize(rows)["providers"]["claude"]

    assert result["winner"] == "mcp"
    assert result["arms"]["legacy"]["eligible"] is False
    assert result["arms"]["mcp"]["eligible"] is True


def test_summary_reports_tie_below_three_points() -> None:
    rows = [
        *[_result("legacy", elapsed=1000, tokens=100) for _ in range(4)],
        *[_result("mcp", elapsed=1000, tokens=100) for _ in range(4)],
    ]

    result = summarize(rows)["providers"]["claude"]

    assert result["winner"] == "tie"


def test_summary_excludes_blocked_pair_and_refuses_decision() -> None:
    blocked_legacy = _result("legacy")
    blocked_mcp = _result("mcp")
    blocked_mcp.provider_blocked = True
    blocked_mcp.provider_block_reason = "provider workspace is out of credits"
    evaluated_legacy = _result("legacy", elapsed=1200)
    evaluated_legacy.repeat = 2
    evaluated_mcp = _result("mcp", elapsed=800)
    evaluated_mcp.repeat = 2

    result = summarize(
        [blocked_legacy, blocked_mcp, evaluated_legacy, evaluated_mcp]
    )["providers"]["claude"]

    assert result["winner"] == "blocked"
    assert result["blocked_pairs"] == [{"scenario": "memory_add", "repeat": 1}]
    assert result["arms"]["legacy"]["evaluated_runs"] == 1
    assert result["arms"]["mcp"]["evaluated_runs"] == 1


def test_auto_surface_fails_safe_then_reads_promoted_provider(tmp_path) -> None:
    config = type("Config", (), {"state_path": tmp_path / ".runtime" / "state.json"})()
    assert resolve_auto_surface(config, "claude") == "legacy"

    rows = [
        *[_result("legacy", elapsed=2000, tokens=200) for _ in range(60)],
        *[_result("mcp", elapsed=500, tokens=50) for _ in range(60)],
    ]
    summary = summarize(rows)
    path = promote_decision(
        workspace=tmp_path,
        output=tmp_path / "report",
        summary=summary,
        results=rows,
        selected_scenarios=12,
        repeats=5,
        smoke=False,
    )

    assert path.exists()
    assert load_decision(tmp_path)["providers"]["claude"]["winner"] == "mcp"
    assert resolve_auto_surface(config, "claude") == "mcp"


def test_partial_benchmark_cannot_be_promoted(tmp_path) -> None:
    rows = [_result("legacy"), _result("mcp")]
    with pytest.raises(ValueError, match="partial benchmark"):
        promote_decision(
            workspace=tmp_path,
            output=tmp_path / "report",
            summary=summarize(rows),
            results=rows,
            selected_scenarios=3,
            repeats=1,
            smoke=True,
        )
