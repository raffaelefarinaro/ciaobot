"""docs/VAULT_MIGRATION_PROMPT.md must name commands and flags that exist.

The document is reachable from the CLI, every `ciao` command it names is a real
subcommand, and a flag named next to a command is one of that command's own
options (checked per command, not against the union of all subcommands).
Prose is deliberately not pinned here.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from ciao import cli


REPO = Path(__file__).resolve().parents[1]
PROMPT_DOC = REPO / "docs" / "VAULT_MIGRATION_PROMPT.md"

#: `ciao <subcommand> …` as it appears in the document, up to the end of the
#: inline code span, so the flags inside the same span are attributable to that
#: command. A flag mentioned in prose away from its command ("use `--apply`") is
#: not attributable and is not checked against one; the command-specific check
#: below reads the fenced command blocks and the inline spans together.
_COMMAND_SPAN_RE = re.compile(r"`ciao ([a-z][a-z0-9]*(?:-[a-z0-9]+)+)([^`]*)`")
_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]+)")
#: Same, over the fenced ```bash blocks, so a command and its flags are paired
#: on one line the way an operator would type them.
_BASH_LINE_RE = re.compile(r"^ciao ([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\s*(.*)$")

#: Flags the document names that belong to no subcommand. Each is another
#: tool's, in the sentence that shows the command using it, so the check has to
#: let it through rather than read it as an invented ``ciao`` option.
_NON_CLI_FLAGS = frozenset(
    {
        "--add",      # the `git add -A` the document tells readers NOT to run
        "--follow",   # the `git log --follow` history-preservation note
        "--include",  # the `grep -rn … --include=…` shape of the user's own scripts
    }
)


def _doc() -> str:
    return PROMPT_DOC.read_text(encoding="utf-8")


def _bash_lines() -> list[str]:
    return [
        line
        for block in re.findall(r"```bash (.*?)```", _doc(), re.DOTALL)
        for line in block.splitlines()
    ]


def _subcommands() -> argparse._SubParsersAction:
    action = next(
        a for a in cli.build_parser()._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    assert action.choices, "ciao.cli registers no subcommands"
    return action


def test_the_prompt_doc_exists_and_is_still_reachable() -> None:
    """Existing installs were pointed here by name; the reader must survive.

    `ciao/cli.py` names this file twice: in the `--mark-migrated` refusal and in
    that flag's help. #729 says the document is not to be deleted, so the path
    and both references are part of its contract.
    """
    assert PROMPT_DOC.is_file(), f"{PROMPT_DOC} is gone; #729 keeps this reader"
    cli_source = (REPO / "ciao" / "cli.py").read_text(encoding="utf-8")
    assert cli_source.count("docs/VAULT_MIGRATION_PROMPT.md") >= 2, (
        "ciao/cli.py no longer points an operator at docs/VAULT_MIGRATION_PROMPT.md; "
        "the --mark-migrated refusal is how an already-migrated install is recorded"
    )


def test_every_command_the_prompt_doc_names_is_registered() -> None:
    named = sorted({m.group(1) for m in _COMMAND_SPAN_RE.finditer(_doc())})

    missing = [name for name in named if name not in _subcommands().choices]

    assert named, "the regex found no ciao command; the document has been restructured"
    assert missing == [], (
        "docs/VAULT_MIGRATION_PROMPT.md names commands the CLI does not register: "
        f"{missing}"
    )


def test_every_flag_beside_a_command_is_that_commands_own_option() -> None:
    """Per command, not against the union of every subcommand.

    The union check is the trap this replaced: it passed
    `ciao vault-migrate --rehearse`, which is a real flag of a real command and
    an imaginary flag of that one. A flag typed next to a command is a claim
    about that command, so it is checked there — and a flag that exists nowhere
    is reported as its own case, because "you meant another command" and "you
    invented this" are different bugs.
    """
    subcommands = _subcommands().choices
    checked: list[tuple[str, str]] = []

    for line in _bash_lines():
        match = _BASH_LINE_RE.match(line)
        if match is None:
            continue
        command, rest = match.group(1), match.group(2)
        assert command in subcommands, (
            f"the fenced example runs `ciao {command}`, which is not a subcommand"
        )
        for flag in _FLAG_RE.findall(rest):
            checked.append((command, flag))

    for match in _COMMAND_SPAN_RE.finditer(_doc()):
        command, rest = match.group(1), match.group(2)
        if command not in subcommands:
            continue  # its own test's failure, not this one's
        for flag in _FLAG_RE.findall(rest):
            checked.append((command, flag))

    assert checked, "the regexes found no command/flag pair; the document changed shape"

    unknown_flag: list[str] = []
    wrong_command: list[str] = []
    for command, flag in checked:
        if flag in _NON_CLI_FLAGS:
            continue
        if flag in subcommands[command]._option_string_actions:
            continue
        owner = [name for name, p in subcommands.items() if flag in p._option_string_actions]
        (unknown_flag if not owner else wrong_command).append(
            f"ciao {command} {flag}"
            + (f" (it is a flag of {owner[0]})" if owner else " (no command has it)")
        )

    assert wrong_command == [], (
        "docs/VAULT_MIGRATION_PROMPT.md puts a flag next to a command that does "
        f"not define it: {wrong_command}"
    )
    assert unknown_flag == [], (
        "docs/VAULT_MIGRATION_PROMPT.md names options no ciao subcommand "
        f"defines: {unknown_flag}"
    )

