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
