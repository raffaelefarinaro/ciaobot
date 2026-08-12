"""Acceptance tests for the declarative eval CLI and incremental reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ciao import evals
from ciao.cli import main
from ciao.eval_runner import ChatObservation
from ciao.evals import (
    EvalRunOverrides,
    EvalSchemaError,
    load_eval_suite,
    resolve_routing,
    run_eval_suite,
    scaffold_eval,
)


def _suite_data(*names: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "cli-smoke",
        "defaults": {
            "provider": "claude",
            "model": "sonnet",
            "surface": "mcp",
        },
        "scenarios": [
            {
                "name": name,
                "description": f"Exercise {name}.",
                "target": {"kind": "skill", "name": "demo"},
                "prompt": f"Run {name}.",
                "assertions": {
                    "output_contains": ["done"],
                    "output_not_contains": [],
                    "output_regex": [],
                    "output_not_regex": [],
                    "required_tools": [],
                    "forbidden_tools": [],
                },
            }
            for name in names
        ],
    }


def _write_suite(tmp_path: Path, *names: str) -> Path:
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(_suite_data(*names)), encoding="utf-8")
    return path


def _observation(
    scenario: str,
    *,
    text: str = "done",
    error: str = "",
) -> ChatObservation:
    return ChatObservation(
        scenario=scenario,
        selected_model="requested-model",
        effective_model="effective-model",
        final_text=text,
        error=error,
        elapsed_ms=125,
        provider_duration_ms=80,
        usage={"input_tokens": "3", "output_tokens": "4"},
        tokens=7,
        provider_tools=("Web.Run",),
        mcp_tools=("mcp__vault__read",),
        mcp_errors=0,
    )


def _cli_args(tmp_path: Path, suite: Path) -> list[str]:
    return [
        "eval",
        "--suite",
        str(suite),
        "--workspace",
        str(tmp_path / "workspace"),
        "--output",
        str(tmp_path / "reports"),
    ]


def test_eval_cli_returns_zero_when_every_scenario_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _write_suite(tmp_path, "alpha")
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(
        evals,
        "run_eval_scenario",
        lambda scenario, **_kwargs: _observation(scenario.name),
    )

    assert main(_cli_args(tmp_path, suite)) == 0


def test_eval_cli_returns_one_when_a_scenario_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _write_suite(tmp_path, "alpha")
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(
        evals,
        "run_eval_scenario",
        lambda scenario, **_kwargs: _observation(scenario.name, text="not finished"),
    )

    assert main(_cli_args(tmp_path, suite)) == 1


@pytest.mark.parametrize("name_filter", ["missing", "ALPHA"])
def test_eval_cli_returns_two_for_invalid_or_empty_selection(
    tmp_path: Path,
    name_filter: str,
) -> None:
    suite = _write_suite(tmp_path, "alpha")
    (tmp_path / "workspace").mkdir()

    assert main([*_cli_args(tmp_path, suite), "--filter", name_filter]) == 2


def test_invalid_suite_starts_no_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = tmp_path / "invalid.json"
    suite.write_text("{invalid", encoding="utf-8")
    (tmp_path / "workspace").mkdir()
    calls = 0

    def fail_if_called(*_args: object, **_kwargs: object) -> ChatObservation:
        nonlocal calls
        calls += 1
        raise AssertionError("scenario runner must not be called")

    monkeypatch.setattr(evals, "run_eval_scenario", fail_if_called)

    assert main(_cli_args(tmp_path, suite)) == 2
    assert calls == 0
    assert not (tmp_path / "reports").exists()


def test_report_is_written_after_each_completed_scenario(tmp_path: Path) -> None:
    suite = _write_suite(tmp_path, "alpha", "beta")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "reports"
    calls = 0

    def runner(scenario: Any, **_kwargs: object) -> ChatObservation:
        nonlocal calls
        if calls == 1:
            payload = json.loads((output / "results.json").read_text())
            assert [row["status"] for row in payload["scenarios"]] == [
                "passed",
                "pending",
            ]
            assert (output / "REPORT.md").is_file()
        calls += 1
        return _observation(scenario.name)

    result = run_eval_suite(
        suite,
        workspace,
        output,
        scenario_runner=runner,
    )

    assert result.exit_code == 0
    assert calls == 2


def test_cli_forwards_overrides_and_timeouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _write_suite(tmp_path, "alpha")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seen: dict[str, object] = {}

    def runner(scenario: Any, **kwargs: object) -> ChatObservation:
        seen.update(kwargs)
        return _observation(scenario.name)

    monkeypatch.setattr(evals, "run_eval_scenario", runner)
    code = main(
        [
            *_cli_args(tmp_path, suite),
            "--provider",
            "codex",
            "--model",
            "gpt-test",
            "--turn-timeout",
            "12.5",
            "--startup-timeout",
            "4",
        ]
    )

    assert code == 0
    assert seen["overrides"] == EvalRunOverrides(
        provider="codex",
        model="gpt-test",
    )
    assert seen["turn_timeout_s"] == 12.5
    assert seen["startup_timeout_s"] == 4.0


@pytest.mark.parametrize("flag", ["--turn-timeout", "--startup-timeout"])
@pytest.mark.parametrize("value", ["nan", "inf", "+inf", "-inf"])
def test_cli_rejects_non_finite_timeouts_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    value: str,
) -> None:
    suite = _write_suite(tmp_path, "alpha")
    (tmp_path / "workspace").mkdir()
    calls = 0

    def fail_if_called(*_args: object, **_kwargs: object) -> ChatObservation:
        nonlocal calls
        calls += 1
        raise AssertionError("scenario runner must not be called")

    monkeypatch.setattr(evals, "run_eval_scenario", fail_if_called)

    assert main([*_cli_args(tmp_path, suite), f"{flag}={value}"]) == 2
    assert calls == 0
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize("model", ["", "   "])
def test_cli_rejects_empty_model_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    suite = _write_suite(tmp_path, "alpha")
    (tmp_path / "workspace").mkdir()
    calls = 0

    def fail_if_called(*_args: object, **_kwargs: object) -> ChatObservation:
        nonlocal calls
        calls += 1
        raise AssertionError("scenario runner must not be called")

    monkeypatch.setattr(evals, "run_eval_scenario", fail_if_called)

    assert main([*_cli_args(tmp_path, suite), "--model", model]) == 2
    assert calls == 0
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize("model", ["", "\t"])
def test_direct_routing_rejects_empty_model_override(
    tmp_path: Path,
    model: str,
) -> None:
    scenario = load_eval_suite(_write_suite(tmp_path, "alpha")).scenarios[0]

    with pytest.raises(EvalSchemaError, match="model override must not be empty"):
        resolve_routing(
            evals.EvalDefaults("claude", "sonnet", "mcp"),
            scenario,
            EvalRunOverrides(model=model),
        )


def test_report_records_execution_errors_and_full_observation_shape(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path, "alpha", "beta")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "reports"

    def runner(scenario: Any, **_kwargs: object) -> ChatObservation:
        if scenario.name == "alpha":
            raise RuntimeError("provider unavailable")
        return _observation(scenario.name)

    result = run_eval_suite(
        suite,
        workspace,
        output,
        scenario_runner=runner,
    )
    payload = json.loads((output / "results.json").read_text())

    assert result.exit_code == 1
    assert payload["suite"] == {
        "name": "cli-smoke",
        "schema_version": 1,
        "source": str(suite.resolve()),
    }
    first, second = payload["scenarios"]
    assert first["status"] == "failed"
    assert first["error"] == "provider unavailable"
    assert second["status"] == "passed"
    assert second["selected_model"] == "requested-model"
    assert second["effective_model"] == "effective-model"
    assert second["provider"] == "claude"
    assert second["surface"] == "mcp"
    assert second["normalized_tools"] == ["web.run", "vault.read"]
    assert second["raw_usage"] == {"input_tokens": "3", "output_tokens": "4"}
    assert second["token_total"] == 7
    assert second["elapsed_ms"] == 125
    assert second["provider_duration_ms"] == 80
    assert second["assertions"][0] == {
        "expected": "done",
        "kind": "output_contains",
        "passed": True,
    }
    assert list(output.glob(".*.tmp")) == []


def test_interrupt_is_reported_without_swallowing_interrupt(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path, "alpha", "beta")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "reports"

    def runner(*_args: object, **_kwargs: object) -> ChatObservation:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_eval_suite(
            suite,
            workspace,
            output,
            scenario_runner=runner,
        )

    payload = json.loads((output / "results.json").read_text())
    assert [row["status"] for row in payload["scenarios"]] == [
        "interrupted",
        "pending",
    ]


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (KeyboardInterrupt(), KeyboardInterrupt),
        (SystemExit(23), SystemExit),
    ],
)
def test_interrupt_is_not_masked_by_report_failure(
    tmp_path: Path,
    raised: BaseException,
    expected: type[BaseException],
) -> None:
    suite = _write_suite(tmp_path, "alpha")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def runner(*_args: object, **_kwargs: object) -> ChatObservation:
        raise raised

    def failing_writer(*_args: object, **_kwargs: object) -> None:
        raise OSError("report disk unavailable")

    with pytest.raises(expected) as caught:
        run_eval_suite(
            suite,
            workspace,
            tmp_path / "reports",
            scenario_runner=runner,
            report_writer=failing_writer,
        )

    if isinstance(raised, SystemExit):
        assert caught.value.code == 23


def test_each_scenario_uses_and_cleans_a_distinct_temporary_workspace(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path, "alpha", "beta")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_file = workspace / "keep.txt"
    source_file.write_text("unchanged", encoding="utf-8")
    isolated_roots: list[Path] = []

    def runner(scenario: Any, **kwargs: object) -> ChatObservation:
        isolated = Path(str(kwargs["isolated_root"]))
        isolated_roots.append(isolated)
        (isolated / "created.txt").write_text(scenario.name, encoding="utf-8")
        return _observation(scenario.name)

    run_eval_suite(
        suite,
        workspace,
        tmp_path / "reports",
        scenario_runner=runner,
    )

    assert len(set(isolated_roots)) == 2
    assert all(not root.exists() for root in isolated_roots)
    assert source_file.read_text(encoding="utf-8") == "unchanged"


def test_scenario_display_names_never_form_temporary_paths(tmp_path: Path) -> None:
    names = ("slash/name", "line\nbreak", "x" * 1000)
    suite = _write_suite(tmp_path, *names)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seen: list[str] = []

    def runner(scenario: Any, **_kwargs: object) -> ChatObservation:
        seen.append(scenario.name)
        return _observation(scenario.name)

    result = run_eval_suite(
        suite,
        workspace,
        tmp_path / "reports",
        scenario_runner=runner,
    )

    assert result.exit_code == 0
    assert seen == list(names)


def test_scaffold_refuses_to_overwrite_existing_suite(tmp_path: Path) -> None:
    first = scaffold_eval(tmp_path, "my-eval")
    original = first.read_text(encoding="utf-8")
    parsed = evals.load_eval_suite(first)

    with pytest.raises(FileExistsError):
        scaffold_eval(tmp_path, "my-eval")

    assert parsed.schema_version == 1
    assert parsed.name == "my-eval"
    assert len(parsed.scenarios) == 1
    assert first.read_text(encoding="utf-8") == original


def test_scaffold_rejects_evals_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "evals").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes workspace"):
        scaffold_eval(workspace, "safe-name")

    assert list(outside.iterdir()) == []


def test_scaffold_failure_never_publishes_partial_file_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_writer = evals._write_scaffold_temporary

    def interrupted_writer(path: Path, _text: str) -> None:
        path.write_text("{partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(evals, "_write_scaffold_temporary", interrupted_writer)
    with pytest.raises(OSError, match="disk full"):
        scaffold_eval(tmp_path, "retryable")

    final = tmp_path / "evals" / "retryable.json"
    assert not final.exists()
    assert list(final.parent.glob(".retryable.json.*.tmp")) == []

    monkeypatch.setattr(evals, "_write_scaffold_temporary", original_writer)
    created = scaffold_eval(tmp_path, "retryable")
    assert load_eval_suite(created).name == "retryable"


@pytest.mark.parametrize("name", ["../escape", "two words", "", ".hidden"])
def test_scaffold_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="Invalid eval name"):
        scaffold_eval(tmp_path, name)


def test_filter_is_case_sensitive_substring_and_preserves_suite_order(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path, "zeta-smoke", "alpha", "beta-smoke")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seen: list[str] = []

    def runner(scenario: Any, **_kwargs: object) -> ChatObservation:
        seen.append(scenario.name)
        return _observation(scenario.name)

    result = run_eval_suite(
        suite,
        workspace,
        tmp_path / "reports",
        name_filter="smoke",
        scenario_runner=runner,
    )

    assert result.exit_code == 0
    assert seen == ["zeta-smoke", "beta-smoke"]


def test_markdown_report_escapes_schema_valid_display_values(
    tmp_path: Path,
) -> None:
    data = _suite_data("bad|row\n# injected\\tail")
    data["name"] = "suite\n# forged"
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps(data), encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "reports"

    def runner(*_args: object, **_kwargs: object) -> ChatObservation:
        raise RuntimeError("first line\n- fake item | tail\\")

    run_eval_suite(
        suite,
        workspace,
        output,
        scenario_runner=runner,
    )
    report = (output / "REPORT.md").read_text(encoding="utf-8")

    assert report.count("\n# ") == 0
    assert "bad&#124;row / &#35; injected&#92;tail" in report
    assert "first line / &#45; fake item &#124; tail&#92;" in report


def test_atomic_report_write_cleans_temp_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(_temporary: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(evals, "_replace_atomic_target", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        evals._atomic_write(tmp_path / "results.json", "{}")

    assert list(tmp_path.glob(".results.json.*.tmp")) == []


def test_atomic_report_write_cleans_temp_after_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(path: Path, _text: str) -> None:
        path.write_text("{partial", encoding="utf-8")
        raise OSError("write failed")

    monkeypatch.setattr(evals, "_write_atomic_temporary", fail_write)

    with pytest.raises(OSError, match="write failed"):
        evals._atomic_write(tmp_path / "results.json", "{}")

    assert not (tmp_path / "results.json").exists()
    assert list(tmp_path.glob(".results.json.*.tmp")) == []


def test_cli_returns_one_when_report_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _write_suite(tmp_path, "alpha")
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(
        evals,
        "run_eval_scenario",
        lambda scenario, **_kwargs: _observation(scenario.name),
    )

    def fail_replace(_temporary: Path, _target: Path) -> None:
        raise OSError("report disk unavailable")

    monkeypatch.setattr(evals, "_replace_atomic_target", fail_replace)

    assert main(_cli_args(tmp_path, suite)) == 1
