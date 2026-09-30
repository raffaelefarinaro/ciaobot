"""docs/VAULT_MIGRATION_PROMPT.md must name commands and flags that exist.

Issue #800 step 1: the document taught a path the engine refuses — hand-edited
`CLAUDE.md`, raw registry edits, `git mv` in a loop, `git add -A` as a backup —
and its own words said `ciao workspace-reroot --apply` "still exists and does all
of this atomically with a receipt and an undo". So the drift had no floor under
it: a reader who typed what the doc said could lose notes, and nothing in the
suite would notice.

Six things are pinned here, in the order a reader hits them.

* **Every `ciao` command and every `--flag` the document names is real**, and
  the flag check is **per command**, not against the union of all subcommands.
  A union check passed `ciao vault-migrate --rehearse`, which is a flag of
  `workspace-reroot`; the first pass of this file made exactly that mistake and
  the review caught it. A flag named next to a command is a claim about that
  command, so it is checked there.
* **The obsolete instructions are gone** and the managed equivalents are named.
  The `workspace-unmigrated` compatibility reader stays, because #729 says it
  must: existing installs that migrated by hand still need `--mark-migrated`.
* **The blocking notice keeps its contract** — one mandatory notice, prominent
  and non-dismissible, and not a dismissible catalog task.
* **Each managed command's rail is named**: dry-run, refusal, receipt, revision
  gate, undo — including the one remedy with no undo at all, and the flags that
  are accepted and ignored.
* **The two surfaces are told apart.** Home and `ciao os-audit` do not carry the
  same set, and a document that merges them sends a reader to the wrong place for
  the one notice that is mandatory.
* **The notices are not claimed to be catalog tasks**, because the catalog ships
  exactly one row and it is about Learnings.md.

The per-root path facts this document's ordering advice rests on are behavioural,
so they are checked against synthetic fixtures in
`tests/test_vault_migration_per_root_vaults.py` rather than asserted here.
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


def _flat() -> str:
    """The document with every whitespace run collapsed to one space.

    Prose is hard-wrapped, and a fragment asserted across a wrap is a test that
    fails when someone reflows a paragraph rather than when the contract
    changes. Fragment assertions read the flat text for that reason; the
    command/flag regexes read the real one, because backticks and fences are
    what delimit them.
    """
    return re.sub(r"\s+", " ", _doc())


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


def test_no_accepted_and_ignored_flag_is_suggested_as_protection() -> None:
    """`--force` is real on the inverse commands and does nothing there.

    `vault_migrate_links.unmigrate_links` ends in `del force`, and
    `vault_rehome.unrehome_people` takes the same argument for symmetry. A
    document that told a reader to pass `--force` to a reverse as if it bought
    a rail would be promising protection the code discards, so the document has
    to say the opposite out loud.
    """
    doc = _flat()

    for fragment in (
        "accepts `--force` and **ignores** it",
        "accepts `--force` and ignores it",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer warns that the reverse "
            f"commands accept --force and discard it: {fragment!r}"
        )
    # And the flag the first pass suggested must be gone: sync-skills' own
    # --verbose is "Accepted for script compatibility" and read nowhere.
    assert "ciao sync-skills --workspace <root> --verbose" not in _doc()
    assert "Accepted for script compatibility" not in doc, (
        "sync-skills --verbose is a no-op; the document must not offer it"
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


def test_the_document_does_not_delegate_the_migration_to_a_chat() -> None:
    """The card's own prompt forbids it: the migration is the operator's press.

    `operator_actions._detect_workspace_unmigrated`'s `chat_prompt` says "Do NOT
    pass --apply … the migration itself is mine to press." A document that hands
    itself to a chat with shell access for the apply contradicts the instruction
    the app gives about the same work, and an agent that follows the document
    rather than the card is the one that moves the vault.
    """
    doc = _flat()

    assert "hand this file to a Ciaobot chat" not in doc
    assert "shell access" not in doc, (
        "the document must not invite a chat to run the migration for the operator"
    )
    for fragment in (
        "Do this yourself, from a terminal at the install root",
        "Separate them now",
        "the migration itself is yours to press",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer points at the in-app "
            f"action and keeps the press with the operator: {fragment!r}"
        )
    # And the difference between the live button and the stopped-service CLI
    # call has to be drawn, not assumed.
    assert "with the app stopped" in doc


def test_home_and_os_audit_are_not_described_as_the_same_set() -> None:
    """One of them carries the only mandatory notice; the other does not.

    `operator_actions._DETECTORS` raises `workspace-unmigrated` and
    `vault-vocabulary`; `os_audit.audit_upgrade_notices` raises neither, and
    raises `unrehomed_people`, which has no tile at all. A document that says
    "Home and the audit report the same conditions" is wrong in both directions:
    an audit-only clean run does not clear the blocker, and Home will never offer
    the re-home notice.
    """
    doc = _flat()

    assert "they do **not** cover the same ones" in doc
    for fragment in (
        "only on Home, so an audit cannot tell you",
        "exists only in the audit, so Home will never offer it",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer distinguishes the two "
            f"surfaces where it matters: {fragment!r}"
        )
    # The step-scoped stop condition: no card ends Step 1, not the document.
    assert (
        "it does **not** mean you are finished" in doc
    ), "an absent card must not read as 'nothing else to do'"


def test_the_blocking_notice_keeps_its_mandatory_contract() -> None:
    """`workspace-unmigrated` is the one mandatory notice, and stays a tile.

    `ciao/operator_actions.py` raises it with `blocking=True` and no dismiss
    label: a catalog task would be dismissible and revision-suppressed, which is
    exactly wrong for a precondition.
    """
    doc = _flat()

    assert "**Yes**" in doc and "Mandatory?" in doc
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
    # The card can also be showing a not-yet-run or errored attempt, so the
    # detail is not always a refusal list.
    assert "the move has not run rather than naming a reason" in doc


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


def test_the_writes_without_apply_claim_is_not_made() -> None:
    """Three of the commands here write with no `--apply` at all.

    `--rehearse` writes a receipt, `--repair` reconciles the filesystem, and
    `sync-skills` mirrors, prunes and runs two receipt-gated vault migrations.
    A blanket "nothing here writes without `--apply`" is false three times over,
    and the false version is the one that surprises somebody.
    """
    doc = _flat()

    for fragment in (
        "**not** read-only and takes no `--apply`",  # sync-skills
        "**writes** — it is not one of the read-only commands",  # --repair
        "writes the receipt with the survey's result",  # --rehearse
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer says which commands write "
            f"without --apply: {fragment!r}"
        )


def test_the_per_root_vault_facts_are_stated_where_they_bite() -> None:
    """After Step 1 there is no install-root vault, and that changes each step.

    The default every vault command resolves is `CIAO_VAULT_ROOT` or
    `./memory-vault`, so the ordering advice, the per-workspace `--vault-root`,
    the per-install link receipt and the shared-layout-only re-home all follow
    from one fact. The fixture checks are in
    `tests/test_vault_migration_per_root_vaults.py`; these are the sentences a
    reader needs and a fixture cannot supply.
    """
    doc = _flat()

    for fragment in (
        "After Step 1 there is no install-root `memory-vault/` at all",
        "The receipt is **per install**",
        "This is a shared-layout remedy, so run it before Step 1",
        "passing `--workspace-name` does not change that",
        "one `vault-vocabulary.<label>-<digest>.json` per vault",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer states this per-root fact: "
            f"{fragment!r}"
        )
    # The re-home step must not be offered a per-workspace invocation.
    assert "a per-root `--vault-root` is not a supported way to use this command" in doc


def test_the_vocabulary_receipt_is_attributed_to_the_writers() -> None:
    """`vault-migrate --apply` renames and records nothing.

    The receipt is written by `vault_migration.migrate_if_needed` (which
    `sync-skills` calls) and by the tile's run button, which rewrites it after a
    fresh scan. A document that says the card clears from the CLI sends the
    reader after a card that will not go.
    """
    doc = _flat()

    assert "**The receipt is not written by `--apply`.**" in doc
    for fragment in (
        "Apply mechanical renames",
        "the card clears when one of those two runs finds an empty `unresolved` list",
        "keeps the retired categories",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer says which action writes "
            f"the vocabulary receipt: {fragment!r}"
        )


def test_the_snapshot_gitignore_caveat_is_stated() -> None:
    """The credential exclusions are only written when git init runs.

    `ensure_rollback_history` calls `_write_snapshot_gitignore` on the
    "not a repository" branch only, so an install that already had a repository
    with no commits gets its snapshot without them.
    """
    doc = _flat()

    assert "is written *only* in the \"no repository at all\" case" in doc
    for fragment in ("git init", "git show --stat HEAD", "can end up in that commit"):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer carries the snapshot "
            f"caveat: {fragment!r}"
        )


def test_the_skill_triage_paths_are_not_described_as_meeting() -> None:
    """The detector and the migration read and write different files.

    `operator_actions._detect_skill_triage_pending` reads
    `<runtime>/migration/skills-triage.md`; `workspace_reroot.write_skills_triage`
    writes `Workspace/Skill-Triage.md` in the primary workspace's vault. A
    document that implies the sheet raises the card is describing a connection
    the code does not make, and a reader would wait for a card that cannot come.
    """
    doc = _flat()

    for fragment in (
        "The two paths do not meet",
        "do not expect this card from the migration",
        "<runtime>/migration/skills-triage.md",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer keeps the two triage "
            f"paths apart: {fragment!r}"
        )


def test_the_notices_are_not_claimed_to_be_catalog_tasks() -> None:
    """None of these notices is an "After this update" task, and none is done.

    The catalog ships exactly one row, `learnings-cleanup`, which is about
    `Workspace/Learnings.md`. A reader told to look for these under "After this
    update" would find nothing; a reader told they are all finished would skip
    the one card that is blocking.
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
