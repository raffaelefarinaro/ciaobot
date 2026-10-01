"""The vault-vocabulary card's run button, and the receipt it has to write.

The card reads the INSTALL-WIDE view (`vault_migration.read_receipt` with no
vault named), which is assembled from the per-vault **keyed** receipts and
consults the pre-keying unkeyed file only on a single-vault install. The button
wrote that unkeyed file, so on a re-rooted install its write was made and then
not read: the press appeared to work, the card survived it, and nothing failed
(#814). Nothing in the suite caught it because the layout it was written against
— one vault — is the one layout where the install-wide view reads it back.

These pin the writer and the reader against each other:

* one keyed receipt per vault, for the vault that press actually re-migrated, and
  no receipt of another root touched;
* a vault whose own receipt is already complete is not re-walked, so a press
  neither mutates a finished root nor re-stamps the record of its original
  migration;
* the summary reports the re-read install-wide view, so a root still waiting on
  its first receipt is named rather than rounded into "the install is done".
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.operator_actions import DetectionContext, detect_actions, run_action
from ciao.vault_migration import migrate_if_needed, read_receipt, receipt_path

# A retired stock category that needs a decision: nothing maps onto it, so the
# migration reports it and leaves the note alone.
UNRESOLVED = "---\ntype: frobnicate\n---\n# Odd\n"
# An alias with a canonical target, so the migration renames it mechanically.
ALIASED = '---\ntype: "project-log"\n---\n# Plan\n'
JOURNAL = "---\ntype: journal\n---\n# Plan\n"


def _note(vault: Path, relative: str, body: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _install(tmp_path: Path, *, rerooted: bool) -> tuple[CiaoConfig, Path]:
    """A two-workspace install in one layout or the other, plus its runtime root.

    ``_vault_count`` — which decides how many receipts the install-wide view
    waits for — counts the agent roots ``agent_roots_for`` reads off
    ``<runtime>/workspaces.json`` behind the re-rooting receipt, so both files
    are part of the fixture rather than decoration. Before the re-rooting that
    is one agent root and one shared vault; after it, one root and one vault per
    workspace.
    """
    runtime = tmp_path / ".runtime"
    (runtime / "migration").mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        json.dumps([{"name": "personal"}, {"name": "work"}]), encoding="utf-8"
    )
    if rerooted:
        (runtime / "migration" / "workspace-rooting.json").write_text(
            json.dumps({"status": "migrated"}), encoding="utf-8"
        )
    reset_reroot_cache()
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    return config, runtime


def _context(config: CiaoConfig, runtime: Path) -> DetectionContext:
    return DetectionContext(config=config, runtime_dir=runtime)


def _card(context: DetectionContext) -> bool:
    return "vault-vocabulary" in [action.id for action in detect_actions(context)]


async def test_the_press_writes_the_receipt_the_reader_is_built_from(tmp_path: Path) -> None:
    """The #814 repro, as an assertion: one root's fix must settle what the card reads.

    Both roots have a receipt, so the install-wide view answers and the card is
    up. The press used to write ``vault-vocabulary.json`` — the pre-keying name,
    which that view consults only when the install has exactly one vault — so the
    file landed, the card read nothing, and the tile stayed. The press must
    rewrite the vault it re-migrated, on the key the reader resolves.
    """
    config, runtime = _install(tmp_path, rerooted=True)
    personal = config.agent_vault_root("personal")
    work = config.agent_vault_root("work")
    _note(personal, "projects/odd.md", UNRESOLVED)
    _note(work, "projects/plan.md", JOURNAL)
    migrate_if_needed(personal, runtime)
    migrate_if_needed(work, runtime)
    # An alias written since that root's migration — the case the button exists
    # for, and the one the install-time migration will not come back for.
    _note(personal, "projects/plan.md", ALIASED)
    context = _context(config, runtime)
    assert _card(context)

    result, _summary = await run_action("vault-vocabulary", context)

    assert result["renamed"] == 1
    assert (personal / "projects/plan.md").read_text(encoding="utf-8") == JOURNAL
    # The receipt for the vault that was re-migrated names that vault, and the
    # install-wide read now answers from it.
    receipt = read_receipt(runtime, personal)
    assert receipt is not None
    assert receipt["vault_root"] == str(personal)
    assert read_receipt(runtime) is not None
    # The pre-keying file is never written by a press: nothing on a re-rooted
    # install reads it, so writing it would be a record nobody can use.
    assert not (runtime / "migration" / "vault-vocabulary.json").exists()


async def test_a_legacy_single_vault_receipt_still_clears_the_card(tmp_path: Path) -> None:
    """An install upgrading into per-vault keying: the unkeyed receipt still raises
    the card, and one press turns it into the keyed receipt the reader wants.

    Nothing here may depend on the legacy file surviving as the answer — it is
    kept, untouched, because it is the record of the migration that ran before
    keying existed — but the card clearing must come from the vault's own keyed
    receipt, on any install.
    """
    config, runtime = _install(tmp_path, rerooted=False)
    vault = config.vault_root
    assert vault == tmp_path / "memory-vault"
    _note(vault, "personal/plan.md", ALIASED)
    odd = _note(vault, "personal/odd.md", UNRESOLVED)
    legacy = receipt_path(runtime)  # the pre-keying name: no vault in it
    legacy.parent.mkdir(parents=True, exist_ok=True)
    seeded = {"schema_version": 2, "renamed": [], "unresolved": {"frobnicate": [str(odd)]}}
    legacy.write_text(json.dumps(seeded), encoding="utf-8")
    context = _context(config, runtime)
    assert _card(context), "the pre-keying receipt is the card's evidence on a one-vault install"
    assert read_receipt(runtime, vault) is None, "a pre-keying receipt claims no vault"

    result, summary = await run_action("vault-vocabulary", context)

    assert result["renamed"] == 1
    assert "still need a decision: frobnicate" in summary
    assert _card(context), "a type nobody can resolve mechanically keeps the card"
    # The press wrote the keyed receipt and left the legacy file exactly as it
    # found it — it is evidence of an earlier pass, not this one's answer.
    assert legacy.read_text(encoding="utf-8") == json.dumps(seeded)
    keyed = read_receipt(runtime, vault)
    assert keyed is not None and keyed["vault_root"] == str(vault)

    # The categorisation decision the card asked for, made by hand. The next press
    # re-scans the vault, so the card clears off a receipt the reader can use.
    odd.write_text(JOURNAL, encoding="utf-8")
    _result, summary = await run_action("vault-vocabulary", context)

    assert "Nothing left to decide on" in summary
    assert not _card(context)
    assert read_receipt(runtime) is not None

    # And the verdict does not lean on the file the pre-keying reader consults:
    # with it gone, the keyed receipt is the whole of the answer.
    legacy.unlink()
    assert not _card(context)


async def test_a_root_already_migrated_is_neither_walked_nor_re_stamped(tmp_path: Path) -> None:
    """One root reported, one root finished: the press acts on the first only.

    The card's evidence is the install-wide view, which merges both roots' types,
    so a press that walked every vault would re-stamp a finished root's receipt —
    replacing the record of what its own migration renamed with a no-op — and pay
    a full vault walk for a root with nothing to migrate.
    """
    config, runtime = _install(tmp_path, rerooted=True)
    personal = config.agent_vault_root("personal")
    work = config.agent_vault_root("work")
    _note(personal, "projects/odd.md", UNRESOLVED)
    _note(work, "projects/done.md", JOURNAL)  # this root migrated cleanly
    migrate_if_needed(personal, runtime)
    migrate_if_needed(work, runtime)
    late = _note(work, "projects/late.md", ALIASED)  # written after work's receipt
    work_receipt = receipt_path(runtime, work)
    work_before = work_receipt.read_text(encoding="utf-8")
    context = _context(config, runtime)
    assert _card(context)

    result, summary = await run_action("vault-vocabulary", context)

    assert result["vaults"] == [str(personal)], "the finished root is not this card's work"
    assert result["renamed"] == 0
    assert late.read_text(encoding="utf-8") == ALIASED, "a completed root was re-migrated"
    assert work_receipt.read_text(encoding="utf-8") == work_before, (
        "a completed root's receipt was rewritten, losing the record of its migration"
    )
    assert "still need a decision: frobnicate" in summary
    assert _card(context)


async def test_both_reported_roots_are_migrated_and_the_card_clears(tmp_path: Path) -> None:
    """Every root the view names gets its own receipt from one press.

    The unresolved types are merged across roots by the install-wide view, so a
    press that fixed only one root would leave the card up with nothing left to
    do on the vault it had already finished — and the press is the only place a
    categorisation decision gets written back.
    """
    config, runtime = _install(tmp_path, rerooted=True)
    personal = config.agent_vault_root("personal")
    work = config.agent_vault_root("work")
    _note(personal, "projects/odd.md", UNRESOLVED)
    _note(work, "projects/odd.md", UNRESOLVED)
    migrate_if_needed(personal, runtime)
    migrate_if_needed(work, runtime)
    _note(work, "projects/plan.md", ALIASED)  # written since that root's migration
    context = _context(config, runtime)
    assert _card(context)

    result, summary = await run_action("vault-vocabulary", context)

    assert sorted(result["vaults"]) == sorted([str(personal), str(work)])
    assert result["renamed"] == 1
    assert result["uncovered"] == []
    assert (work / "projects/plan.md").read_text(encoding="utf-8") == JOURNAL
    assert "still need a decision: frobnicate" in summary

    for vault in (personal, work):
        (vault / "projects/odd.md").write_text(JOURNAL, encoding="utf-8")
    result, summary = await run_action("vault-vocabulary", context)

    assert result["unresolved"] == []
    assert "Nothing left to decide on" in summary
    assert not _card(context)
    assert read_receipt(runtime)["vaults"] == sorted([str(personal), str(work)])


async def test_a_leftover_shared_vault_is_not_the_vault_the_press_migrates(tmp_path: Path) -> None:
    """The #814 repro's exact shape, where `config.vault_root` still resolves.

    When the re-rooting left `<install>/memory-vault` behind as an empty shell,
    the old press found a directory there, migrated it, and wrote the unkeyed
    receipt for a vault no reader counts — so the install-wide view stayed None
    and the card stayed. A press works from the vaults the install actually has,
    which after the re-rooting are one per agent root.
    """
    config, runtime = _install(tmp_path, rerooted=True)
    config.vault_root.mkdir(parents=True)  # the leftover shell
    personal = config.agent_vault_root("personal")
    work = config.agent_vault_root("work")
    _note(personal, "projects/odd.md", UNRESOLVED)
    _note(work, "projects/odd.md", UNRESOLVED)
    migrate_if_needed(personal, runtime)
    migrate_if_needed(work, runtime)
    context = _context(config, runtime)
    assert _card(context)

    result, _summary = await run_action("vault-vocabulary", context)

    # The write this press used to make: the leftover shell is not a vault of this
    # install, so nothing is recorded for it, and no unkeyed file appears at all.
    assert read_receipt(runtime, config.vault_root) is None, (
        "the leftover shared directory was migrated as if it were a vault"
    )
    assert not (runtime / "migration" / "vault-vocabulary.json").exists()
    assert sorted(result["vaults"]) == sorted([str(personal), str(work)])
    assert read_receipt(runtime) is not None


async def test_a_root_without_a_vault_is_named_rather_than_swallowed(tmp_path: Path) -> None:
    """One root migrated, one root that has no vault yet: the press says so.

    The install-wide view answers None while any vault is uncovered, so a press
    that settles one root of two has fixed one root. Reporting that as a finished
    migration would be a claim the receipt does not support — and the summary is
    the only place the operator learns which root is still outstanding.
    """
    config, runtime = _install(tmp_path, rerooted=True)
    personal = config.agent_vault_root("personal")
    work = config.agent_vault_root("work")  # never created: the vault is not there
    _note(personal, "projects/odd.md", UNRESOLVED)
    migrate_if_needed(personal, runtime)
    context = _context(config, runtime)

    result, summary = await run_action("vault-vocabulary", context)

    assert result["vaults"] == [str(personal)]
    assert result["uncovered"] == [str(work)]
    assert f"1 vault(s) still have no migration receipt: {work}" in summary
    assert "Nothing left to decide on" not in summary
    # No receipt for a vault that does not exist, so the real one is migrated
    # when it appears; and the install view is still unsettled, as it must be.
    assert read_receipt(runtime, work) is None
    assert read_receipt(runtime) is None


async def test_a_press_that_can_reach_no_vault_is_a_failure(tmp_path: Path) -> None:
    """A remedy that did nothing must not be reported as a run.

    The route turns a raised error into a tile carrying the reason, which is the
    one way the operator learns the button cannot act here — as opposed to a
    summary claiming a clean install.
    """
    config, runtime = _install(tmp_path, rerooted=False)
    config.vault_root.write_text("not a directory", encoding="utf-8")
    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {"frobnicate": ["x.md"]}}), encoding="utf-8"
    )
    context = _context(config, runtime)
    assert _card(context)

    try:
        await run_action("vault-vocabulary", context)
    except RuntimeError as exc:
        assert "vault root does not exist" in str(exc)
    else:  # pragma: no cover - the raise is the assertion
        raise AssertionError("a run that migrated nothing must not report success")