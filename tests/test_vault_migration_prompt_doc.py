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
* **The two surfaces are told apart**, including where they overlap: Home and
  `ciao os-audit` do not carry the same set, a document that merges them sends a
  reader to the wrong place for the one notice that is mandatory, and since #833
  the one notice on *both* has to be described as two different things — a
  dismissible Home task and a report the operator cannot silence.
* **Exactly one notice is claimed to be a catalog task**, and the document says
  what completes it. `unrehomed-people` (#833) is it, because it is the only
  migration remedy whose receipt a completion check can read; the rest are tiles,
  and a document that offered them under "After this update" would send a reader
  looking for cards that do not exist.

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


def test_reversibility_is_not_described_as_an_overridable_rail() -> None:
    """`--force` buys three things, and the receipt is not one of them.

    The second version of the document listed reversibility as the first of
    "three rails, and `--force` overrides all three", which reads as though
    `--force` could trade the reverse map away. It cannot: the overridable rails
    are the existing receipt, the dirty tree and the nested vault root, and the
    reverse map is a property of every run rather than a check any flag turns
    off. A reader who believed the earlier wording would either distrust the undo
    or reach for `--force` expecting it to mean something it does not.
    """
    doc = _flat()

    assert "Reversibility is a fourth thing and is **not** one of them" in doc
    assert "**Reversibility is not a rail and `--force` cannot touch it.**" in doc
    # The three overridable rails, named as the implementation names them.
    for rail in (
        "**An existing receipt**",
        "**A dirty vault git tree.**",
        "**A `--vault-root` nested inside the configured vault.**",
    ):
        assert rail in doc, (
            f"docs/VAULT_MIGRATION_PROMPT.md no longer lists this overridable rail: {rail!r}"
        )
    # Only the nesting rail gates the preview; saying otherwise sends a reader
    # to `--force` to look at a dirty vault, which the code allows without it.
    assert "the only rail that gates the *preview*" in doc
    # And the earlier heading, which bundled reversibility in, must be gone.
    assert "Three rails, and `--force` overrides all three" not in doc


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
    # The live button runs against a live server, which is the one hazard the
    # CLI path removes by asking for the app to be stopped. `apply` writes the
    # chat state file and its own docstring says a live server "would both read
    # a path that no longer exists and overwrite the handover flags from its
    # in-memory copy", so a reader pressing the button deserves the caution.
    assert "One caution about the live button" in doc
    assert "Let the run finish before you touch anything else" in doc
    assert "do not start another operation that writes chats until it has reported back" in doc


def test_home_and_os_audit_are_not_described_as_the_same_set() -> None:
    """One of them carries the only mandatory notice; the other does not.

    `operator_actions._DETECTORS` raises `workspace-unmigrated` and
    `vault-vocabulary`; `os_audit.audit_upgrade_notices` raises neither. A
    document that says "Home and the audit report the same conditions" is wrong
    in both directions: an audit-only clean run does not clear the blocker.

    `unrehomed_people` used to be the third case — audit-only, with no Home
    surface at all — and since #833 it has both, which is why it now needs the
    *asymmetry* stated instead: the card is dismissible and the report is not, so
    a document that called the two interchangeable would send a reader who hid
    the card to `ciao os-audit` and back out again for nothing.
    """
    doc = _flat()

    assert "they do **not** cover the same ones" in doc
    for fragment in (
        "only on Home, so an audit cannot tell you",
        "the two are **not** the same thing",
        "dismissing the Home card leaves it exactly where it was",
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
        "The moves are a shared-layout remedy, so run them before Step 1",
        "passing `--workspace-name` does not change that",
        "one `vault-vocabulary.<label>-<digest>.json` per vault",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer states this per-root fact: "
            f"{fragment!r}"
        )
    # The re-home step must not be offered a per-workspace invocation for the
    # moves — the no-op run that clears the notice is a different thing, and the
    # document has to keep the two apart.
    assert "a per-root `--vault-root` is not a supported way to move anything" in doc
    assert "What *is* left after Step 1 is the receipt" in doc


def test_the_vocabulary_card_clears_through_its_own_keyed_receipt() -> None:
    """The card has exactly one remedy, and the document must name it correctly.

    #814 fixed the writer: the run button used to write the **unkeyed**
    `vault-vocabulary.json`, which the install-wide reader in `_install_receipt`
    consults only when the install has exactly one vault, so on a re-rooted
    install the press wrote a receipt and the card read nothing. Two facts are
    still true and one is now false, and the document has to say which is which:
    `--apply` writes no receipt at all; `sync-skills` is gated on the install-wide
    receipt being absent while `migrate_if_needed` skips any vault that already
    has a keyed one, so it never re-scans; and the button **does** clear the
    card, by rewriting each reported vault's own keyed receipt. A document still
    promising nothing clears it sends the reader after a card that goes away on
    the next press.
    """
    doc = _flat()

    for fragment in (
        "`--apply` writes no receipt",
        "**rewrites that vault's own keyed receipt**",
        "the button is how a decision gets recorded",
        "does not write the pre-keying unkeyed `vault-vocabulary.json`",
        "does not touch a vault whose receipt is already complete",
        "it does not re-scan, so a note written afterwards with a retired type",
        "not as a status light somebody",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer states this receipt fact: "
            f"{fragment!r}"
        )
    # The claim #800's correction installed, and the one #814 made false.
    assert "the card does not clear from a re-scan" not in doc
    assert "do not expect the card to clear because you resolved the types" not in doc
    assert "the button's write is written and then not read" not in doc
    assert "not a way to make it notice a retired type" in doc, (
        "sync-skills is receipt-gated and does not re-scan; the document has to "
        "say so rather than leave the earlier 're-scan' wording standing"
    )


def test_the_snapshot_exclusions_apply_to_both_snapshot_branches() -> None:
    """The credential exclusions are written on every path that snapshots.

    `ensure_rollback_history` used to call `_write_snapshot_gitignore` on the
    "not a repository" branch only, so an install that already had a repository
    with no commits got its snapshot with no exclusions at all. The document must
    not tell a reader the old caveat still holds.
    """
    doc = _flat()

    assert "Both snapshotting branches first get a `.gitignore`" in doc, (
        "the document must state that both snapshotting branches get the exclusions"
    )
    for fragment in (".env", "secrets/", "cannot be committed into the snapshot", "additive"):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer carries the snapshot "
            f"exclusion statement: {fragment!r}"
        )
    # The old caveat is now false and must be gone, not merely softened.
    for gone in (
        "is written *only* in the \"no repository at all\" case",
        "git show --stat HEAD",
    ):
        assert gone not in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md still carries the withdrawn snapshot "
            f"caveat: {gone!r}"
        )


def test_the_snapshot_unstages_the_excluded_paths_and_says_it_does() -> None:
    """The document must state that a PRE-STAGED secret is left out too.

    A `.gitignore` never unstages, so an owner who ran `git add .env` after
    `git init` still got `.env` committed into the snapshot by the version that
    wrote the exclusions on both branches. `ensure_rollback_history` now also
    runs `git rm --cached` over `_SNAPSHOT_IGNORES`, which is invisible from the
    outside in the one way that matters: it does not touch the working tree. A
    reader who has staged their `.env` needs to know it will be dropped from the
    commit and still be on disk afterwards, or they will refuse to run the
    migration at all.
    """
    doc = _flat()

    for fragment in (
        "`.gitignore` does not unstage anything",
        "dropped from git's **staging area** before the snapshot is taken",
        "you had already run `git add` on would go into the commit anyway",
        "Nothing is deleted from disk",
        "every other file you had staged is still in it",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md does not tell the reader that a "
            f"pre-staged secret is unstaged for the snapshot: {fragment!r}"
        )


def test_the_snapshot_verifies_itself_and_says_so() -> None:
    """The document must state that the unstage is verified, and what it does.

    The unstage is a best effort: an owner's `.gitignore` can put a credential
    back on the very next `git add -A` (a `!.env` after our entry, or a
    `sub/.gitignore` saying `!.env`, which nothing reads).
    `ensure_rollback_history` re-reads the index before committing and returns
    `unstage_failed` if anything is still staged. A reader who has a `!` line
    needs to know the run refuses and leaves their files alone, or they will read
    the refusal as damage.
    """
    doc = _flat()

    for fragment in (
        "That is not taken on trust",
        "`!.env` placed after them",
        "containing `!.env`, which applies to everything under `sub/`",
        "the staging area is read back immediately before the commit",
        "if anything in `_SNAPSHOT_IGNORES` is still staged the migration refuses",
        "`unstage_failed`",
        "makes no commit at all",
        "Your files are still on disk either way",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md does not tell the reader that the "
            f"snapshot is verified and refuses when it cannot be proven clean: {fragment!r}"
        )


def test_unrehomed_people_is_documented_as_a_receipt_check() -> None:
    """The notice reports the absence of a receipt, not a scan of the vault.

    `os_audit.audit_upgrade_notices` raises it from `read_receipt(runtime_dir)`
    alone, and only when `len(names) > 1` — it never calls
    `detect_misfiled_people`. So it can be present when there is nothing to move,
    and Step 1 does not clear it (the re-rooting writes no re-home receipt). A
    document that called it "person notes may be filed in the wrong workspace"
    and pointed straight at the remedy would send a reader to a command that
    finds nothing and leaves the notice up.
    """
    doc = _flat()

    assert "**`unrehomed_people` is a receipt check, not a scan" in doc
    for fragment in (
        "there is **no completed re-home receipt**",
        "it does not walk the vault, so it cannot tell you whether anything is actually misfiled",
        "it is **not** cleared by Step 1",
        "the re-rooting writes no re-home receipt",
        "an honest no-op run writes one",
        "status: migrated",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer explains how this notice "
            f"behaves: {fragment!r}"
        )
    # And the no-op run has to name the root it applies to, or it cannot be run.
    assert (
        "ciao vault-rehome --vault-root <install>/<workspace>/memory-vault --apply" in doc
    ), "the clearing run must be spelled out, since the default vault root is gone after Step 1"
    # The table row is a receipt condition, not a claim about the notes.
    assert "| `unrehomed_people` — no re-home has been recorded |" in doc
    # "informational, not a defect" is the answer for an operator who declines it.
    assert "informational, not a defect" in doc


def test_the_home_task_is_narrower_than_the_audit_notice() -> None:
    """The card needs a move to offer; the notice only needs a missing receipt.

    #833's first pass gave the card the notice's condition, which offered it on
    every fresh install that never needed the migration — a receipt's absence is
    free on a clean install. A reader told the two are the same question would
    wait for a card that is correctly never offered, and one told the card always
    appears would look for it on a separated install where the command cannot act.
    """
    doc = _flat()

    assert "only when `ciao vault-rehome`'s own plan finds a tag-obvious note" in doc
    for fragment in (
        "A card is an offer to do work, so it needs a reason to exist rather than a receipt's absence",
        "Untagged contacts are normal in any vault and are never counted as evidence",
        "a separated install gets **no task at all**",
        "the notice keeps reporting",
        "it is never how one comes to be offered",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer keeps the broad notice and "
            f"the narrower card apart: {fragment!r}"
        )
    # And the table says so in the Home column rather than only in prose.
    assert (
        "Yes, as an **After this update** task (`unrehomed-people`), dismissible, "
        "and only when there is something to move" in doc
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


def test_exactly_one_notice_is_claimed_to_be_a_catalog_task() -> None:
    """One migration notice is an "After this update" task: `unrehomed_people`.

    `unrehomed-people` (#833) is the only one whose remedy writes a receipt a
    completion check can read, so it is the only one that may be catalogued. The
    rest are tiles: a *view of the machine*, recomputed on every render, whose
    only "completion evidence" is the condition's absence — a tautology dressed
    as a check. A reader told to look for those under "After this update" would
    find nothing; a reader told they were all finished would skip the one card
    that is blocking.
    """
    doc = _flat()

    assert (
        "**Only one migration notice in this document is a catalog task, and it is "
        "the one whose remedy writes a receipt.**" in doc
    ), "the catalog claim must name the one row that is a task, and why"
    for fragment in (
        "`unrehomed-people`",
        "prompts/unrehomed-people-1.md",
        "install-scoped",
        "not** a `partial` one",
        "not a run that was refused",
        "not a file nobody could read",
    ):
        assert fragment in doc, (
            "docs/VAULT_MIGRATION_PROMPT.md no longer states what completes the "
            f"catalogued notice: {fragment!r}"
        )
    assert "learnings-cleanup" in doc, (
        "name the other shipped catalog row so 'unrelated to layout' is checkable"
    )
    assert "unrelated to layout" in doc
    assert "do not assume any of these are offered there" in doc
    # The audit is the diagnostic interface and keeps reporting a finding even
    # when the matching card was dismissed; the document must not promise the
    # two agree.
    assert "reports the findings independently" in doc
