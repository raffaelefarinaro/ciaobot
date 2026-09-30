"""docs/VAULT_MIGRATION_PROMPT.md must name commands that exist, and only them.

Issue #800 step 1: the document taught a path the engine refuses — hand-edited
`CLAUDE.md`, raw registry edits, `git mv` in a loop, `git add -A` as a backup —
and its own words said `ciao workspace-reroot --apply` "still exists and does all
of this atomically with a receipt and an undo". So the drift had no floor under
it: a reader who typed what the doc said could lose notes, and nothing in the
suite would notice.

Three things are pinned here, in the order a reader hits them.

* **Every `ciao` command the doc names is a registered subcommand**, and every
  `--flag` it names is a real option of some subcommand. A typo'd or invented
  flag is the failure mode a prose document has and code does not, so it gets
  checked against the real parser rather than trusted.
* **The obsolete instructions are gone** and the managed equivalents are named,
  including the ones the plan's evidence table singled out. The
  `workspace-unmigrated` compatibility reader stays, because #729 says it must:
  existing installs that migrated by hand still need `--mark-migrated`.
* **The claims that would mislead a reader are stated**: the blocking notice is
  the one mandatory thing, the rest are optional, and the notices are operator
  tiles rather than "After this update" catalog tasks. The catalog shipped one
  row (`learnings-cleanup`), which is about Learnings.md and unrelated to layout
  — so a doc that claimed these were offered there would be wrong, and a doc
  that claimed they were all completed would be wronger.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from ciao import cli


REPO = Path(__file__).resolve().parents[1]
PROMPT_DOC = REPO / "docs" / "VAULT_MIGRATION_PROMPT.md"

#: `ciao <subcommand>` and nothing else. A flag-only mention is matched by
#: ``--[a-z]``, a path is ``/``-separated, and the surrounding prose never puts
#: a bare word after the program name without a hyphen.
_COMMAND_RE = re.compile(r"\bciao ([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b")
_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]+)")

#: Flags the document names that belong to no subcommand. Each is another
#: tool's, in the sentence that shows the command using it, so the check has to
#: let it through rather than read it as an invented ``ciao`` option.
_NON_CLI_FLAGS = frozenset(
    {
        "--add",     # the `git add -A` the document tells readers NOT to run
        "--follow",  # the `git log --follow` history-preservation note
        "--include",  # the `grep -rn … --include=…` shape of the user's own scripts
    }
)


def _doc() -> str:
    return PROMPT_DOC.read_text(encoding="utf-8")


def _flat() -> str:
    """The document with every whitespace run collapsed to one space.

    Prose is hard-wrapped, and a fragment asserted across a wrap is a test that
    fails when someone reflows a paragraph rather than when the contract
    changes. Fragment assertions read the flat text for that reason; the
    command/flag regexes read the real one, because backticks and fences are
    what delimit them.
    """
    return re.sub(r"\s+", " ", _doc())


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
    named = sorted(set(_COMMAND_RE.findall(_doc())))

    missing = [name for name in named if name not in _subcommands().choices]

    assert named, "the regex found no ciao command; the document has been restructured"
    assert missing == [], (
        "docs/VAULT_MIGRATION_PROMPT.md names commands the CLI does not register: "
        f"{missing}"
    )


def test_every_flag_the_prompt_doc_names_is_a_real_option() -> None:
    """A flag is checked against the whole CLI, not against one subcommand.

    The document explains one flag per sentence and names a given flag under
    several commands, so pinning each to a specific subparser would assert the
    prose's layout rather than its accuracy. What must hold is that no flag is
    invented: an unknown one is a usage error at best and a wrong file edited at
    worst.
    """
    subcommands = _subcommands()
    real: set[str] = set(_NON_CLI_FLAGS)
    for parser in subcommands.choices.values():
        real |= set(parser._option_string_actions)

    named = sorted(set(_FLAG_RE.findall(_doc())))

    missing = [flag for flag in named if flag not in real]

    assert named, "the regex found no flag; the document has been restructured"
    assert missing == [], (
        "docs/VAULT_MIGRATION_PROMPT.md names options no ciao subcommand defines: "
        f"{missing}"
    )


def test_the_managed_entry_points_are_all_named() -> None:
    """Every remedy the plan's evidence table names has to be in the document.

    These are the five commands #800 sent the document to, their two undo
    counterparts, and the read-only checks it already leaned on. A remedy that
    exists but is not written down is the same gap the rewrite was for, one
    level up.
    """
    doc = _flat()

    missing = [
        command
        for command in (
            "workspace-reroot",
            "vault-relocate",
            "vault-migrate-links",
            "vault-unmigrate-links",
            "vault-rehome",
            "vault-unrehome",
            "vault-migrate",
            "vault-lint",
            "workspace-census",
            "sync-skills",
            "os-audit",
        )
        if f"ciao {command}" not in doc
    ]

    assert missing == [], (
        "docs/VAULT_MIGRATION_PROMPT.md no longer names these managed entry "
        f"points: {missing}"
    )


def test_the_obsolete_hand_migration_instructions_are_gone() -> None:
    """The manual half is what #800 came to delete from this document.

    `git add -A` as a backup instruction, `git mv` and `mv` as the migration
    mechanism, a hand-written `AGENTS.md` symlink, a hand-edited registry, and a
    hand-edited `MEMORY.md` each taught a step the engine now performs, performs
    better, or refuses to let anyone perform. Leaving any of them in place is
    how a reader ends up re-deriving a move the receipt already records.
    """
    doc = _flat()

    for fragment in (
        "git add -A && git commit",
        "mkdir -p W && git mv",
        "ln -s CLAUDE.md AGENTS.md",
        "git mv memory-vault/W W/memory-vault",
        "git mv memory-vault/Logs Logs",
    ):
        assert fragment not in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md still teaches the manual move "
            f"{fragment!r}; the managed commands do it"
        )
    # Staging the whole tree is discussed only as the thing not to do.
    assert "git add -A" in doc, (
        "the document should still say why `git add -A` is not the backup step"
    )


def test_the_blocking_notice_keeps_its_mandatory_contract() -> None:
    """`workspace-unmigrated` is the one mandatory notice, and stays a tile.

    `ciao/operator_actions.py` raises it with `blocking=True` and no dismiss
    label: a catalog task would be dismissible and revision-suppressed, which is
    exactly wrong for a precondition. The document has to say which one is
    mandatory, because "read the housekeeping strip" alone does not tell a reader
    which card they may decline.
    """
    doc = _flat()

    assert "**Exactly one of them is mandatory.**" in doc
    assert "`workspace-unmigrated`" in doc
    for fragment in (
        "a precondition this install cannot get past on its own",
        "prominent, permanent and retryable rather than dismissible",
        "must not become one",  # the catalog section's own contrast
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer states the blocking "
            f"contract: {fragment!r}"
        )
    # `--mark-migrated` is the compatibility path for an install already moved
    # by hand. #729 forbids removing it, so the document must still teach it.
    assert "ciao workspace-reroot --mark-migrated" in doc
    assert "origin: hand" in doc, (
        "the doc should say --mark-migrated records `origin: hand`, so the "
        "receipt does not claim a migration that never ran"
    )


def test_the_dry_run_apply_receipt_and_undo_contract_is_stated() -> None:
    """Each managed command's rail is named, not implied.

    A dry-run-by-default command that says "use `--apply`" without saying what
    the receipt is, or that has no undo at all, is how a reader ends up unable
    to reverse what they ran. One assertion per contract, across the commands
    that own it.
    """
    doc = _flat()

    for fragment in (
        "Dry-run by default",  # vault-relocate, vault-migrate-links, vault-rehome
        "refuses outright",  # workspace-reroot --apply
        "all-or-nothing",
        "It is idempotent",  # --repair, sync-skills
        "the exact `git mv` in reverse",  # vault-relocate's receipt
        "an exact reverse map",  # vault-rehome's receipt
        "inverse rather than a re-derivation",  # vault-unmigrate-links
        "byte-identical tree",  # workspace-reroot --undo
        "there is no `vault-unmigrate`",  # the one remedy without an undo
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer states this contract: "
            f"{fragment!r}"
        )


def test_the_notices_are_not_claimed_to_be_catalog_tasks() -> None:
    """None of these notices is an "After this update" task, and none is done.

    The catalog ships exactly one row, `learnings-cleanup`, which is about
    `Workspace/Learnings.md`. A reader told to look for these under "After this
    update" would find nothing; a reader told they are all finished would skip
    the one card that is blocking. Both are the failure #800's evidence table was
    written to prevent, so the document states the boundary in both directions.
    """
    doc = _flat()

    assert "**None of the migration notices in this document are catalog tasks today.**" in doc
    assert "learnings-cleanup" in doc, (
        "name the one shipped catalog row so 'unrelated to layout' is checkable"
    )
    assert "unrelated to layout" in doc
    assert "do not assume any of these are offered there" in doc
    # The audit is the diagnostic interface and keeps reporting a finding even
    # when the matching card was dismissed; the document must not promise the
    # two agree.
    assert "reports the findings independently" in doc
