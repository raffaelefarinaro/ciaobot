from __future__ import annotations

import json
from pathlib import Path

from ciao.web.commands import expand_slash_command


def test_expand_slash_command_preserves_original_and_substitutes_arguments(
    tmp_path: Path,
) -> None:
    command = tmp_path / ".claude" / "commands" / "remember.md"
    command.parent.mkdir(parents=True)
    command.write_text(
        "---\ndescription: Save it\nargument-hint: <fact>\n---\n\n"
        "# Remember\n\nStore $ARGUMENTS durably.\n",
        encoding="utf-8",
    )

    expanded = expand_slash_command("/remember blue is preferred", tmp_path)

    assert expanded is not None
    assert "Store blue is preferred durably." in expanded
    original_line = next(
        line for line in expanded.splitlines() if line.startswith("user_input_json=")
    )
    assert json.loads(original_line.split("=", 1)[1]) == "/remember blue is preferred"


def test_expand_slash_command_ignores_unknown_command(tmp_path: Path) -> None:
    assert expand_slash_command("/ciao-definitely-missing-command arg", tmp_path) is None
    assert expand_slash_command("ordinary text", tmp_path) is None
