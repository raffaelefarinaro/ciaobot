"""The documented `ciao` command set must match the parsers (#1471).

`ciao-cli/SKILL.md` is the full reference; the core prompt carries a compact
synopsis of the common commands (#1464). The parsers in `ciao/agent_cli.py` (`ciao <noun> <verb>`) and
`ciao/cli.py` (top-level commands) are the truth. These tests derive the
documented set from the skill and compare it to the parsers in both
directions, so a renamed flag, a verb that never existed or an agent verb nobody
documented fails here instead of misleading a model.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import re
from pathlib import Path

import pytest

from ciao import agent_cli
from ciao import cli as operator_cli

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "ciao/stock/skills/ciao-cli/SKILL.md").read_text(encoding="utf-8")
PROMPT = (ROOT / "ciao/system_prompt.md").read_text(encoding="utf-8")

_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]*")
Leaf = tuple[str, ...]
#: Flags the parser leaves optional because the handler (not argparse) refuses
#: their absence, so the docs rightly show them as required.
_REQUIRED_BY_HANDLER: dict[Leaf, frozenset[str]] = {
    ("memory", "update"): frozenset({"--entry"}),
    ("memory-proposal-dismiss",): frozenset({"--text-file"}),
    ("memory-proposal-escalate",): frozenset({"--text-file", "--reason-file"}),
    ("memory-proposal-predict",): frozenset({"--text-file", "--reason-file"}),
    ("memory-proposal-accept",): frozenset({"--text-file"}),
    ("observation-add",): frozenset({"--about", "--source", "--text-file"}),
}
#: leaf -> (flags, required flags)
Surface = dict[Leaf, tuple[frozenset[str], frozenset[str]]]
#: leaf -> (every flag mentioned, flags shown outside any [optional] bracket)
Documented = dict[Leaf, tuple[set[str], set[str]]]


def _walk(parser: argparse.ArgumentParser, prefix: Leaf = ()) -> Surface:
    groups = [
        action.choices
        for action in parser._actions
        if isinstance(getattr(action, "choices", None), dict) and action.dest != "help"
    ]
    if not groups:
        flags = {o for a in parser._actions for o in a.option_strings if o.startswith("--") and o != "--help"}
        required = {
            o for a in parser._actions if a.required and a.option_strings
            for o in a.option_strings if o.startswith("--")
        }
        return {prefix: (frozenset(flags), frozenset(required))}
    found: Surface = {}
    for choices in groups:
        for name, sub in choices.items():
            found.update(_walk(sub, (*prefix, name)))
    return found


def agent_surface() -> Surface:
    surface = _walk(agent_cli.build_parser())
    surface.pop(("help",), None)
    return surface


def operator_surface() -> Surface:
    # Only top-level commands are documented as verbs; keep one-word leaves.
    return {leaf: v for leaf, v in _walk(operator_cli.build_parser()).items() if len(leaf) == 1}


def _split_flags(text: str) -> tuple[set[str], set[str]]:
    """Every flag in *text*, and the ones outside any ``[...]`` group."""
    every: set[str] = set()
    required: set[str] = set()
    depth = 0
    index = 0
    while index < len(text):
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth = max(0, depth - 1)
        match = _FLAG.match(text, index) if text[index] == "-" else None
        if match:
            every.add(match.group())
            if depth == 0:
                required.add(match.group())
            index = match.end()
            continue
        index += 1
    return every, required


def _match_leaf(text: str, leaves: set[Leaf]) -> Leaf | None:
    words = text.replace("`", "").split()
    best: Leaf | None = None
    for leaf in leaves:
        if tuple(words[: len(leaf)]) == leaf and (best is None or len(leaf) > len(best)):
            best = leaf
    return best


def documented_in_skill(leaves: set[Leaf]) -> Documented:
    shared = re.search(r"Shared `\.\.\.flags`[^:]*: `([^`]*)`", SKILL)
    assert shared, "the shared schedule flags paragraph is gone"
    sched_flags, _ = _split_flags(shared.group(1))
    found: Documented = {}
    for line in SKILL.splitlines():
        row = re.match(r"\| `(.+?)` \|", line)
        if not row:
            continue
        cell = row.group(1)
        leaf = _match_leaf(cell, leaves)
        assert leaf is not None, f"ciao-cli/SKILL.md documents `{cell}`, which is not a command"
        every, required = _split_flags(cell[len(" ".join(leaf)):])
        if "...flags" in cell:
            every |= sched_flags
        found[leaf] = (every, required)
    return found


def drift_problems(documented: Documented, surface: Surface, *, where: str) -> list[str]:
    problems: list[str] = []
    # `--json` is accepted everywhere as a compatibility no-op, so docs may omit it.
    for leaf, (every, required) in sorted(documented.items()):
        command = " ".join(leaf)
        flags, really_required = surface[leaf]
        for flag in sorted(every - flags - {"--json"}):
            problems.append(f"{where}: `{command}` documents {flag}, which the parser does not have")
        for flag in sorted(required - really_required - {"--json"} - _REQUIRED_BY_HANDLER.get(leaf, frozenset())):
            problems.append(f"{where}: `{command}` shows {flag} as required, but the parser makes it optional")
        for flag in sorted((really_required & flags) - required):
            problems.append(f"{where}: `{command}` shows {flag} as optional, but the parser requires it")
    return problems


def _all_leaves() -> tuple[Surface, Surface]:
    return agent_surface(), operator_surface()


def _agent_documented(agent: Surface) -> Documented:
    documented = documented_in_skill(set(agent) | set(operator_surface()))
    return {leaf: v for leaf, v in documented.items() if leaf in agent}


def _prompt_synopsis() -> list[str]:
    section = re.search(r"<ciaobot_cli_reference>(.*?)</ciaobot_cli_reference>", PROMPT, re.S)
    assert section, "the core prompt lost its <ciaobot_cli_reference> section"
    block = re.search(r"```\n(.*?)```", section.group(1), re.S)
    assert block, "the core prompt lost its compact ciao synopsis"
    return [line for line in block.group(1).splitlines() if line.strip()]


def _alternatives(word: str) -> set[str]:
    """``a|b`` names two words; ``x-add|dismiss`` names ``x-add`` and ``x-dismiss``."""
    parts = word.split("|")
    stem = parts[0].rsplit("-", 1)[0] + "-" if "-" in parts[0] else ""
    return {parts[0], *(part if "-" in part else stem + part for part in parts[1:])}


def _named_leaves(words: list[str], leaves: set[Leaf]) -> tuple[list[Leaf], int]:
    """Leaves a synopsis line names and how many of its words they use."""
    for size in (3, 2, 1):
        if len(words) < size:
            continue
        found = [
            leaf for leaf in leaves
            if len(leaf) == size and leaf[:-1] == tuple(words[: size - 1]) and leaf[-1] in _alternatives(words[size - 1])
        ]
        if found:
            return found, size
    return [], 0


def prompt_problems(lines: list[str], surface: Surface) -> list[str]:
    """Every command and flag the compact synopsis shows must exist in the parsers.

    A line naming several verbs (`task complete|delegate|link-chat`) is held to
    every one of them: each flag shown must exist on each verb named.
    """
    problems: list[str] = []
    for line in lines:
        words = line.replace("`", "").split()
        found, size = _named_leaves(words, set(surface))
        if not found:
            problems.append(f"prompt shows `{line}`, which is not a command")
            continue
        shown = set(_FLAG.findall(" ".join(words[size:]))) - {"--json"}
        for leaf in sorted(found):
            for flag in sorted(shown - surface[leaf][0]):
                problems.append(f"prompt: `{' '.join(leaf)}` is shown with {flag}, which it does not have")
    return problems


def test_core_prompt_synopsis_matches_the_parsers() -> None:
    agent, operator = _all_leaves()
    surface = {**agent, **operator}
    lines = _prompt_synopsis()
    assert prompt_problems(lines, surface) == []
    # The recall-critical commands must stay in the prompt (routing eval, #1464).
    text = "\n".join(lines)
    for command in ("vault search", "memory update", "file surface", "schedule", "task report"):
        assert command in text, command


def test_prompt_reference_names_promoted_dismissal() -> None:
    section = re.search(r"<ciaobot_cli_reference>(.*?)</ciaobot_cli_reference>", PROMPT, re.S)
    assert section, "the core prompt lost its <ciaobot_cli_reference> section"
    assert "--promoted" in section.group(1)
    assert "--days-of-week" in section.group(1)


def test_prompt_synopsis_rejects_an_unknown_flag_or_verb() -> None:
    agent, operator = _all_leaves()
    surface = {**agent, **operator}
    assert prompt_problems(["vault search QUERY [--nope N]"], surface)
    assert prompt_problems(["vault sarch QUERY"], surface)
    assert prompt_problems(["vault search QUERY [--limit N]"], surface) == []


def test_prompt_synopsis_holds_every_named_verb_to_every_flag() -> None:
    """The lines #1464 first shipped: a flag valid on only one of the verbs named."""
    agent, operator = _all_leaves()
    surface = {**agent, **operator}
    wrong_task = "task update|move|complete|delegate|add-update|link-chat ID --revision REV [--to STATUS] [--text-file F]"
    assert any("--to" in p for p in prompt_problems([wrong_task], surface))
    assert prompt_problems(["vault review list|show|keep [--candidate ID]"], surface)
    assert prompt_problems(["vault review list --candidate ID"], surface)
    assert prompt_problems(["vault review show PATH"], surface) == []
    assert prompt_problems(["task move ID --revision REV --to backlog"], surface) == []


def test_skill_tables_match_the_parsers() -> None:
    agent, operator = _all_leaves()
    surface = {**agent, **operator}
    documented = documented_in_skill(set(surface))
    assert drift_problems(documented, surface, where="ciao-cli/SKILL.md") == []
    assert sorted(set(agent) - set(documented)) == [], "agent verbs missing from ciao-cli/SKILL.md"


@pytest.mark.parametrize(
    "command",
    ["memory-proposal-add", "memory-proposal-escalate", "observation-add", "skill-draft-add", "skill-draft-approve"],
)
def test_queue_filing_commands_are_documented(command: str) -> None:
    _, operator = _all_leaves()
    assert (command,) in operator
    assert (command,) in documented_in_skill(set(operator) | set(agent_surface()))


def test_skill_documents_the_origins_input() -> None:
    row = next(line for line in SKILL.splitlines() if line.startswith("| `skill-proposal-add"))
    for word in ("origins", "learning_id", "finding", "source_revision", "summary"):
        assert word in row
    assert "non-empty `sources` list whose" not in row


def test_a_removed_flag_fails_the_comparison() -> None:
    agent, _ = _all_leaves()
    documented = _agent_documented(agent)
    assert drift_problems(documented, agent, where="x") == []
    flags, required = agent[("chat", "send")]
    broken = {**agent, ("chat", "send"): (flags - {"--prompt"}, required - {"--prompt"})}
    problems = drift_problems(documented, broken, where="x")
    assert any("--prompt" in problem for problem in problems)
    optional = {**agent, ("chat", "send"): (flags, required - {"--prompt"})}
    assert any("shows --prompt as required" in p for p in drift_problems(documented, optional, where="x"))


def test_an_undocumented_verb_fails_the_comparison() -> None:
    agent, _ = _all_leaves()
    documented = _agent_documented(agent)
    documented.pop(("task", "report"))
    assert ("task", "report") in set(agent) - set(documented)


@pytest.mark.parametrize("noun", ["chat", "file", "project", "schedule", "webhook", "workspace"])
def test_noun_help_lists_its_verbs(noun: str) -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exit_info:
        agent_cli.main([noun, "--help"])
    assert exit_info.value.code == 0
    verbs = [leaf[1] for leaf in agent_surface() if leaf[0] == noun]
    assert verbs
    for verb in verbs:
        assert re.search(rf"^\s+{re.escape(verb)}\s+\S", out.getvalue(), re.M), (noun, verb)


def test_help_prints_the_skill_and_its_reference(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_cli.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "## Calling convention" in out and "## Worked examples" in out
    assert "Operation names for telemetry" not in SKILL
