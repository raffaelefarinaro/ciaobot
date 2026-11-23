from __future__ import annotations

import json
from pathlib import Path

from ciao import cli


def test_command_previews_applies_and_reverts_in_the_selected_workspace(
    tmp_path: Path, capsys
) -> None:
    vault = tmp_path / "vault"
    workspace = vault / "Workspace"
    workspace.mkdir(parents=True)
    queue = workspace / "Memory-Proposals.md"
    queue.write_text("- [learnings] keep this lesson _(request: request-1)_\n")
    runtime = tmp_path / ".runtime"
    args = ["learnings-queue-drain", "--vault-root", str(vault), "--runtime-root", str(runtime)]

    assert cli.main(args) == 0
    assert "1 pending" in capsys.readouterr().out
    assert queue.read_text().startswith("- [learnings]")
    assert cli.main([*args, "--apply", "--json"]) == 0
    receipt = Path(json.loads(capsys.readouterr().out)["receipts"][0])
    assert queue.read_text() == ""
    assert cli.main([*args, "--revert", str(receipt)]) == 0
    assert "pass --apply" in capsys.readouterr().out
    assert cli.main([*args, "--revert", str(receipt), "--apply"]) == 0
    assert queue.read_text() == "- [learnings] keep this lesson _(request: request-1)_\n"


def test_fresh_only_cli_applies_a_new_batch(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    workspace = vault / "Workspace"
    workspace.mkdir(parents=True)
    queue = workspace / "Memory-Proposals.md"
    queue.write_text("- [learnings] care migration\n", encoding="utf-8")
    args = [
        "learnings-queue-drain", "--vault-root", str(vault),
        "--runtime-root", str(tmp_path / ".runtime"), "--apply", "--fresh-only", "--json",
    ]

    assert cli.main(args) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is True
    assert len(result["receipts"]) == 1
    assert queue.read_text(encoding="utf-8") == ""


def test_fresh_only_requires_apply_and_cannot_be_used_for_revert(tmp_path: Path, capsys) -> None:
    args = [
        "learnings-queue-drain", "--vault-root", str(tmp_path / "vault"),
        "--runtime-root", str(tmp_path / ".runtime"), "--fresh-only",
    ]

    assert cli.main(args) == 2
    assert "requires --apply" in capsys.readouterr().err
    assert cli.main([*args, "--apply", "--revert", str(tmp_path / "receipt.json")]) == 2
    assert "cannot be used with --revert" in capsys.readouterr().err
