"""The documented `ciao` command set must match the parsers (#1471).

The complete `ciao help` reference is the full documentation; the core prompt
carries a compact synopsis of the common commands (#1464). The parsers in
`ciao/agent_cli.py` (`ciao <noun> <verb>`) and `ciao/cli.py` (top-level
commands) are the truth. These tests derive the documented set from the
complete reference and compare it to the parsers in both directions, so a
renamed flag, a verb that never existed or an agent verb nobody documented
fails here instead of misleading a model.
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
SKILL = agent_cli._help_text()
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
        assert leaf is not None, f"the complete CLI reference documents `{cell}`, which is not a command"
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
    assert prompt_problems(["note create|related --type T"], surface)
    assert prompt_problems(["note related PATH [--limit N]"], surface) == []
    assert prompt_problems(["task move ID --revision REV --to backlog"], surface) == []


def test_skill_tables_match_the_parsers() -> None:
    agent, operator = _all_leaves()
    surface = {**agent, **operator}
    documented = documented_in_skill(set(surface))
    assert drift_problems(documented, surface, where="complete CLI reference") == []
    assert sorted(set(agent) - set(documented)) == [], "agent verbs missing from the complete CLI reference"


@pytest.mark.parametrize(
    "command",
    ["proposal-check"],
)
def test_queue_filing_commands_are_documented(command: str) -> None:
    _, operator = _all_leaves()
    assert (command,) in operator
    assert (command,) in documented_in_skill(set(operator) | set(agent_surface()))


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


def test_the_care_prompt_and_its_references_name_only_real_files_and_commands() -> None:
    """Every `references/…` file and `ciao …` command the unattended care run is told to use exists."""
    import json

    root = ROOT / "ciao/stock/skills/ciao-capabilities/references/memory-operations"
    schedules = json.loads((ROOT / "ciao/stock/schedules.json").read_text(encoding="utf-8"))
    prompt = next(e["prompt"] for e in schedules["schedules"] if e["schedule_id"] == "system-memory-curation")
    texts = [prompt, *(p.read_text(encoding="utf-8") for p in sorted((root / "references").glob("*.md")))]
    agent, operator = _all_leaves()
    leaves = set(agent) | set(operator)
    problems: list[str] = []
    for text in texts:
        for path in re.findall(r"`(references/[a-z0-9-]+\.md)`", text):
            if not (root / path).is_file():
                problems.append(f"missing file {path}")
        for first, second in re.findall(r"`ciao ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", text):
            if (first,) in leaves or (second and (first, second) in leaves):
                continue
            problems.append(f"unknown command ciao {first} {second}".strip())
    assert not problems, sorted(set(problems))


_BARE_PROCEDURE = re.compile(
    r"`((?:curation-[a-z0-9-]+|drain-legacy-queues|unsure-and-proposals|writing)\.md)`"
)


def test_the_care_run_names_only_procedure_files_that_ship() -> None:
    """The orchestrator and the guide name step files by bare name; each must exist."""
    import json

    root = ROOT / "ciao/stock/skills/ciao-capabilities/references/memory-operations"
    schedules = json.loads((ROOT / "ciao/stock/schedules.json").read_text(encoding="utf-8"))
    prompt = next(e["prompt"] for e in schedules["schedules"] if e["schedule_id"] == "system-memory-curation")
    texts = [prompt, (root / "guide.md").read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8") for p in sorted((root / "references").glob("*.md"))]
    missing = {n for text in texts for n in _BARE_PROCEDURE.findall(text) if not (root / "references" / n).is_file()}
    assert not missing, sorted(missing)


def _preflight_snippet() -> str:
    import json

    schedules = json.loads((ROOT / "ciao/stock/schedules.json").read_text(encoding="utf-8"))
    prompt = next(e["prompt"] for e in schedules["schedules"] if e["schedule_id"] == "system-memory-curation")
    match = re.search(r"Run once: `(.+?)`\. If it prints anything", prompt, re.S)
    assert match, "the preflight snippet changed shape"
    return match.group(1)


def test_care_preflight_checks_every_command_the_care_run_uses() -> None:
    """The preflight lists real subcommands and key flags and covers every command a step names."""
    import json

    root = ROOT / "ciao/stock/skills/ciao-capabilities/references/memory-operations"
    snippet = _preflight_snippet()
    schedules = json.loads((ROOT / "ciao/stock/schedules.json").read_text(encoding="utf-8"))
    prompt = next(e["prompt"] for e in schedules["schedules"] if e["schedule_id"] == "system-memory-curation")
    listed = [tuple(item.split()) for item in re.findall(r"chk ciao ([a-z -]+?);", snippet)]
    agent, operator = _all_leaves()
    leaves = set(agent) | set(operator)
    assert listed and set(listed) <= leaves, sorted(set(listed) - leaves)
    assert 'grep -q -- $f || echo "missing: $f"' in snippet
    for flag, command in (
        ("--with-vault", "memory-audit"), ("--write", "vault-index"), ("--json", "decisions-digest"),
        ("--completed-since", "task list"),
    ):
        assert f"has {flag} ciao {command}" in snippet, flag
    steps = [p.read_text(encoding="utf-8") for p in sorted((root / "references").glob("*.md"))]
    text = "\n".join([prompt, *(s for s in steps if s.startswith("# Step "))])
    named = {
        (first, second) if (first, second) in leaves else (first,)
        for first, second in re.findall(r"`ciao ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", text)
    }
    # learnings-cleanup is only reported to the person, never run by a step.
    unchecked = named - set(listed) - {("learnings-cleanup",)}
    assert not unchecked, sorted(unchecked)


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_care_preflight_runs_the_same_in_bash_and_zsh(shell: str, tmp_path: Path) -> None:
    """zsh does not word-split a variable, so a `for c in "a b"; do ciao $c` loop reports every command missing there."""
    import shutil
    import subprocess

    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} is not installed")
    fake = tmp_path / "ciao"
    fake.write_text(
        '#!/bin/sh\n'
        'if [ "$1 $2" = "note related" ] && [ -n "$CIAO_FAKE_MISSING" ]; then exit 2; fi\n'
        'echo "usage: ciao $* --with-vault --write --json --completed-since"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    ok = subprocess.run([exe, "-c", _preflight_snippet()], env=env, capture_output=True, text=True, check=False)
    assert ok.stdout == "" and ok.returncode == 0, (shell, ok.stdout, ok.stderr)
    broken = subprocess.run(
        [exe, "-c", _preflight_snippet()], env={**env, "CIAO_FAKE_MISSING": "1"}, capture_output=True, text=True, check=False
    )
    assert broken.stdout.strip() == "missing: ciao note related", (shell, broken.stdout)
