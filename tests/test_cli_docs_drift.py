"""The documented `ciao` command set must match the parsers (#1471).

The core prompt's synopsis and `ciao-cli/SKILL.md` both list commands and
flags; the parsers in `ciao/agent_cli.py` (`ciao <noun> <verb>`) and
`ciao/cli.py` (top-level commands) are the truth. These tests derive the
documented set from the two documents and compare it to the parsers in both
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


def documented_in_prompt(leaves: set[Leaf]) -> Documented:
    block = PROMPT.split("## Ciaobot command line", 1)[1].split("```", 2)[1]
    sched_text = ""
    lines: list[str] = []
    in_sched = False
    for raw in block.splitlines():
        if raw.startswith("<sched>"):
            in_sched = True
        if in_sched:
            sched_text += " " + raw
        elif raw.strip():
            lines.append(raw)
    sched_flags, _ = _split_flags(sched_text)
    found: Documented = {}
    for line in lines:
        leaf = _match_leaf(line, leaves)
        assert leaf is not None, f"core synopsis documents `{line.strip()}`, which is not an agent command"
        every, required = _split_flags(line[len(" ".join(leaf)):])
        if "<sched>" in line:
            every |= sched_flags
        found[leaf] = (every, required)
    return found


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


def test_prompt_synopsis_matches_the_agent_parser() -> None:
    agent, _ = _all_leaves()
    documented = documented_in_prompt(set(agent))
    assert drift_problems(documented, agent, where="system_prompt.md") == []
    assert sorted(set(agent) - set(documented)) == [], "agent verbs missing from the core synopsis"


def test_skill_tables_match_the_parsers() -> None:
    agent, operator = _all_leaves()
    surface = {**agent, **operator}
    documented = documented_in_skill(set(surface))
    assert drift_problems(documented, surface, where="ciao-cli/SKILL.md") == []
    assert sorted(set(agent) - set(documented)) == [], "agent verbs missing from ciao-cli/SKILL.md"


@pytest.mark.parametrize(
    "command",
    ["memory-proposal-add", "observation-add", "skill-draft-add", "skill-draft-approve"],
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
    documented = documented_in_prompt(set(agent))
    assert drift_problems(documented, agent, where="x") == []
    flags, required = agent[("chat", "send")]
    broken = {**agent, ("chat", "send"): (flags - {"--prompt"}, required - {"--prompt"})}
    problems = drift_problems(documented, broken, where="x")
    assert any("--prompt" in problem for problem in problems)
    optional = {**agent, ("chat", "send"): (flags, required - {"--prompt"})}
    assert any("shows --prompt as required" in p for p in drift_problems(documented, optional, where="x"))


def test_an_undocumented_verb_fails_the_comparison() -> None:
    agent, _ = _all_leaves()
    documented = documented_in_prompt(set(agent))
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
