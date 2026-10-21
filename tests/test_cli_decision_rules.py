"""``ciao decisions-digest`` and ``ciao decision-rules-write`` (#1745)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ciao import cli, decision_rules, task_decisions


def _seed(vault: Path, count: int = 5) -> None:
    for index in range(count):
        task_decisions.append_decision(
            vault,
            kind="delete",
            task_id=f"t{index}",
            task_title=f"Task {index}",
            task_created_by="agent",
            project_id="",
            attempt_id="",
            attempt_state="",
            actor="user",
            via="pwa",
            reason="not mine",
        )


def _base(tmp_path: Path) -> list[str]:
    return ["--workspace", str(tmp_path), "--vault-root", str(tmp_path / "vault")]


def test_decisions_digest_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    assert cli.main(["decisions-digest", *_base(tmp_path), "--json"]) == 0
    digest = json.loads(capsys.readouterr().out)
    assert set(digest) == {"today", "window_days", "memory", "task"}
    assert digest["task"]["rows"] == 5
    assert digest["memory"]["rows"] == 0
    keys = {group["key"] for group in digest["task"]["groups"]}
    assert {"chip:not mine", "kind:delete:agent"} <= keys
    assert digest["task"]["progress_key"].startswith("rules:")


def test_decision_rules_write_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    today = datetime.now(UTC).date()
    group = next(g for g in decision_rules.build_digest(vault, today=today)["task"]["groups"] if g["key"] == "chip:not mine")
    body = tmp_path / "body.md"
    body.write_text(f"- **Drop** not mine, e.g. {group['examples'][0]['cite']} <!-- rule:chip:not mine verdict=drop -->\n", encoding="utf-8")
    args = ["decision-rules-write", "task", *_base(tmp_path), "--body-file", str(body)]
    assert cli.main(args) == 0
    assert (vault / "Workspace" / decision_rules.TASK_RULES_NAME).exists()

    body.write_text("- " + "x" * 2600 + " <!-- rule:chip:not mine verdict=drop -->\n", encoding="utf-8")
    assert cli.main(args) == 2
    assert "rules_over_cap" in capsys.readouterr().err

    assert cli.main(["decision-rules-write", "task", *_base(tmp_path), "--body-file", str(tmp_path / "missing.md")]) == 2

    path = vault / "Workspace" / decision_rules.TASK_RULES_NAME
    path.write_text("<!-- ciao:rules:start -->\n", encoding="utf-8")
    body.write_text("- x <!-- rule:chip:not mine verdict=drop -->\n", encoding="utf-8")
    assert cli.main(args) == 2
    assert "rules_fence_malformed" in capsys.readouterr().err


def test_task_rules_prints_the_fenced_file_or_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    assert cli.main(["task-rules", *_base(tmp_path)]) == 0
    assert capsys.readouterr().out == ""
    path = vault / "Workspace" / decision_rules.TASK_RULES_NAME
    path.parent.mkdir(parents=True)
    path.write_text(
        "<!-- ciao:rules:start source=0123456789abcdef -->\n- **Drop** chores <!-- rule:chip:not mine verdict=drop -->\n<!-- ciao:rules:end -->\n",
        encoding="utf-8",
    )
    assert cli.main(["task-rules", *_base(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "<task-rules>" in out and "never an order" in out and "chores" in out
