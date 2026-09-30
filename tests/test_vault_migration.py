"""One-off migration of an existing vault onto the canonical `type:` set.

`vault-lint` starts reporting non-canonical types on upgrade, and `os-audit`
exits 1 on them, so shipping the check without this hands every existing install
a permanently unhealthy audit. The migration applies only *aliased* renames — a
substitution with a named target and no judgement in it — and reports a type with
no canonical equivalent instead of guessing a category for the user.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao import entity_types
from ciao.vault_index import scan_vault
from ciao.vault_migration import (
    RECEIPT_NAME,
    migrate_if_needed,
    migrate_vault_vocabulary,
    read_receipt,
    receipt_path,
    retain_retired_stock_types,
    retain_retired_stock_types_if_needed,
    retired_receipt_path,
)


def _note(vault: Path, relative: str, body: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/a.md", "---\ntype: discussion-prep\ntitle: A\ntags: [x]\n---\n# A\n\nbody\n")
    _note(vault, "personal/b.md", '---\ntype: "project-log"\n---\n# B\n')
    _note(vault, "personal/c.md", "---\ntype: frobnicate\n---\n# C\n")
    _note(vault, "personal/d.md", "---\ntype: person\n---\n# D\n")
    return vault


# ---- dry run ---------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    before = (vault / "personal/a.md").read_text(encoding="utf-8")

    summary = migrate_vault_vocabulary(vault)

    assert summary["applied"] is False
    assert summary["renamed"] == []
    assert {change["from"] for change in summary["planned"]} == {"discussion-prep", "project-log"}
    assert (vault / "personal/a.md").read_text(encoding="utf-8") == before


def test_a_missing_vault_is_reported_not_created(tmp_path: Path) -> None:
    summary = migrate_vault_vocabulary(tmp_path / "nope")

    assert summary["skipped"]
    assert not (tmp_path / "nope").exists()


# ---- apply -----------------------------------------------------------------


def test_apply_rewrites_only_the_type_line(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_vault_vocabulary(vault, apply=True)

    assert (vault / "personal/a.md").read_text(encoding="utf-8") == (
        "---\ntype: note\ntitle: A\ntags: [x]\n---\n# A\n\nbody\n"
    )


def test_apply_handles_a_quoted_value(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_vault_vocabulary(vault, apply=True)

    assert "type: journal" in (vault / "personal/b.md").read_text(encoding="utf-8")


def test_a_type_with_no_alias_is_reported_and_left_alone(tmp_path: Path) -> None:
    """Choosing a category is the user's call; guessing would bury a real
    decision inside a migration."""
    vault = _vault(tmp_path)

    summary = migrate_vault_vocabulary(vault, apply=True)

    assert list(summary["unresolved"]) == ["frobnicate"]
    assert "type: frobnicate" in (vault / "personal/c.md").read_text(encoding="utf-8")


def test_canonical_notes_are_untouched(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    before = (vault / "personal/d.md").read_text(encoding="utf-8")

    migrate_vault_vocabulary(vault, apply=True)

    assert (vault / "personal/d.md").read_text(encoding="utf-8") == before


def test_migration_is_idempotent(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    migrate_vault_vocabulary(vault, apply=True)

    again = migrate_vault_vocabulary(vault, apply=True)

    assert again["renamed"] == []
    assert again["failed"] == []


def test_a_hand_edit_racing_the_migration_is_not_clobbered(tmp_path: Path) -> None:
    """The rewrite only fires when the line still holds the value it planned to
    replace, so a concurrent edit fails loudly instead of being overwritten."""
    vault = tmp_path / "memory-vault"
    note = _note(vault, "personal/a.md", "---\ntype: discussion-prep\n---\n# A\n")
    summary = migrate_vault_vocabulary(vault)  # plan against `doc`
    assert summary["planned"]

    note.write_text("---\ntype: reference\n---\n# A\n", encoding="utf-8")
    from ciao.vault_migration import _retype_frontmatter

    assert _retype_frontmatter(
        note.read_text(encoding="utf-8"), expect="discussion-prep", replacement="note"
    ) is None


def test_frontmatter_less_note_is_not_rewritten() -> None:
    from ciao.vault_migration import _retype_frontmatter

    assert _retype_frontmatter("# Just a heading\n", expect="discussion-prep", replacement="note") is None


# ---- the one-off gate ------------------------------------------------------


def test_receipt_makes_it_run_once(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    first = migrate_if_needed(vault, runtime)
    second = migrate_if_needed(vault, runtime)

    assert len(first["renamed"]) == 2
    assert second["skipped"] == "already migrated"
    assert receipt_path(runtime, vault).is_file()


def test_each_vault_migrates_once_behind_one_runtime_root(tmp_path: Path) -> None:
    """Two agent roots, one baked `CIAO_RUNTIME_ROOT` — the launchd layout.

    The receipt was keyed on the runtime root while the unit being migrated is a
    vault, so `main.py`'s per-root `update_skills` loop wrote the receipt on the
    first root and every later root short-circuited as "already migrated". The
    second workspace's vault then kept its legacy `type:` values forever, and
    `vault-lint`/`os-audit` failed for it permanently with no path to a fix.
    """
    personal = _vault(tmp_path / "personal")
    work = _vault(tmp_path / "work")
    runtime = tmp_path / ".runtime"

    first = migrate_if_needed(personal, runtime)
    second = migrate_if_needed(work, runtime)

    assert len(first["renamed"]) == 2
    assert "skipped" not in second, "the second workspace's vault was never migrated"
    assert len(second["renamed"]) == 2
    assert "type: note" in (work / "personal/a.md").read_text(encoding="utf-8")
    # One receipt per vault, so neither root rescans on the next boot.
    assert migrate_if_needed(personal, runtime)["skipped"] == "already migrated"
    assert migrate_if_needed(work, runtime)["skipped"] == "already migrated"
    assert receipt_path(runtime, personal) != receipt_path(runtime, work)


def test_a_pre_keying_receipt_does_not_claim_a_vault(tmp_path: Path) -> None:
    """Upgrading from the runtime-keyed receipt still migrates every vault.

    The old payload does not record which vault it covered, so it cannot claim
    one; trusting it would leave exactly the un-migrated vault this keying
    exists to reach. The cost is one idempotent rescan per vault.
    """
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    legacy = receipt_path(runtime)  # no vault named: the pre-keying path
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"schema_version": 1, "renamed": [], "unresolved": {}}), encoding="utf-8"
    )

    summary = migrate_if_needed(vault, runtime)

    assert len(summary["renamed"]) == 2


def test_the_install_view_waits_for_every_vault(tmp_path: Path) -> None:
    """`sync_skills` gates its per-root loop on the UNKEYED read, so that read
    must not report a migrated install until every vault is done. The first
    root's receipt closing it is what stranded the second workspace's vault.
    """
    runtime = tmp_path / ".runtime"
    (runtime / "migration").mkdir(parents=True)
    # A re-rooted, two-workspace install: `agent_roots_for` reads exactly these
    # two files, so this is the layout that gives the install two vaults.
    (runtime / "workspaces.json").write_text(
        json.dumps([{"name": "personal"}, {"name": "work"}]), encoding="utf-8"
    )
    (runtime / "migration" / "workspace-rooting.json").write_text(
        json.dumps({"status": "migrated"}), encoding="utf-8"
    )
    personal = _vault(tmp_path / "personal")
    work = _vault(tmp_path / "work")
    # The pre-keying receipt names no vault, so here it accounts for neither.
    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {}}), encoding="utf-8"
    )

    assert read_receipt(runtime) is None

    migrate_if_needed(personal, runtime)
    assert read_receipt(runtime) is None, "one root's receipt closed the gate on the other"

    migrate_if_needed(work, runtime)
    view = read_receipt(runtime)
    assert view is not None
    assert view["vaults"] == sorted([str(personal), str(work)])
    assert "frobnicate" in view["unresolved"]


def test_a_lone_pre_keying_receipt_still_covers_a_single_vault_install(tmp_path: Path) -> None:
    """One vault means there is nothing else the unkeyed receipt can be about.

    The `vault-vocabulary` operator tile reads it to report the types the
    migration declined to guess, so ignoring it here would silently drop that
    report on every install that upgrades into per-vault keying.
    """
    runtime = tmp_path / ".runtime"
    (runtime / "migration").mkdir(parents=True)
    (runtime / "migration" / RECEIPT_NAME).write_text(
        json.dumps({"renamed": [], "unresolved": {"log": ["x.md"]}}), encoding="utf-8"
    )

    view = read_receipt(runtime)

    assert view is not None
    assert view["unresolved"] == {"log": ["x.md"]}


def test_receipt_records_what_was_left_for_the_user(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    migrate_if_needed(vault, runtime)

    receipt = read_receipt(runtime, vault)
    assert receipt is not None
    assert receipt["schema_version"] == 2
    assert receipt["vault_root"] == str(vault)
    assert "frobnicate" in receipt["unresolved"]


def test_no_receipt_is_left_when_there_is_no_vault_yet(tmp_path: Path) -> None:
    """Bootstrap: the setup wizard has not created the vault. Leaving a receipt
    here would mark the real vault as migrated before it existed."""
    runtime = tmp_path / ".runtime"

    migrate_if_needed(tmp_path / "nope", runtime)

    assert read_receipt(runtime, tmp_path / "nope") is None


def test_a_corrupt_receipt_does_not_block_the_migration(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    path = receipt_path(runtime, vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    summary = migrate_if_needed(vault, runtime)

    assert len(summary["renamed"]) == 2
    assert json.loads(path.read_text(encoding="utf-8"))["renamed"]


# ---- retired stock categories ----------------------------------------------


def _retired_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/notes/plan.md", "---\ntype: plan\n---\n# Plan\n")
    _note(vault, "personal/notes/ref.md", "---\ntype: reference\n---\n# Ref\n")
    _note(vault, "personal/notes/p.md", "---\ntype: person\n---\n# P\n")
    return vault


def test_retire_dry_run_reports_and_writes_nothing(tmp_path: Path) -> None:
    vault = _retired_vault(tmp_path)

    summary = retain_retired_stock_types(vault)

    assert summary["retained"] == ["document", "reference"]
    assert "plan" in summary["covers"]
    assert not (vault / "entity-types.yaml").exists()


def test_retire_apply_keeps_used_types_as_custom_and_rewrites_no_note(tmp_path: Path) -> None:
    vault = _retired_vault(tmp_path)
    before = {p: p.read_text(encoding="utf-8") for p in vault.rglob("*.md")}

    summary = retain_retired_stock_types(vault, apply=True)

    assert summary["retained"] == ["document", "reference"]
    assert {p: p.read_text(encoding="utf-8") for p in vault.rglob("*.md")} == before
    entity_types.clear_entity_types_cache()
    registry = entity_types.load_entity_types(vault)
    assert registry.get("document") is not None and not registry.get("document").builtin
    # The alias travels with the category, so `plan` is no longer drift.
    assert registry.aliases()["plan"] == "document"
    assert registry.get("product") is None, "an unused retired type is not copied"
    drift = migrate_vault_vocabulary(vault)["unresolved"]
    assert drift == {}


def test_retire_is_idempotent_and_leaves_a_vault_definition_alone(tmp_path: Path) -> None:
    vault = _retired_vault(tmp_path)
    (vault / "entity-types.yaml").write_text(
        "- id: reference\n  label: Sources\n  kind: note\n  folder: sources\n",
        encoding="utf-8",
    )

    first = retain_retired_stock_types(vault, apply=True)
    entity_types.clear_entity_types_cache()
    second = retain_retired_stock_types(vault, apply=True)

    assert first["retained"] == ["document"]
    assert second["retained"] == []
    assert entity_types.load_entity_types(vault).get("reference").label == "Sources"


def test_retire_refuses_a_write_the_loader_would_drop(tmp_path: Path) -> None:
    vault = _retired_vault(tmp_path)
    # A vault category that already owns `doc`: the retired `document` alias list
    # would collide with it, and a colliding file is dropped whole by the loader.
    (vault / "entity-types.yaml").write_text(
        "- id: doc\n  label: Doc\n  kind: note\n  folder: Docs\n", encoding="utf-8"
    )

    summary = retain_retired_stock_types(vault, apply=True)

    assert summary["retained"] == [] and "failed" in summary
    assert "document" not in (vault / "entity-types.yaml").read_text(encoding="utf-8")


def test_retire_receipt_makes_the_upgrade_step_run_once(tmp_path: Path) -> None:
    vault = _retired_vault(tmp_path)
    runtime = tmp_path / ".runtime"

    first = retain_retired_stock_types_if_needed(vault, runtime)
    assert first["retained"] == ["document", "reference"]
    assert retired_receipt_path(runtime, vault).is_file()

    _note(vault, "personal/notes/x.md", "---\ntype: product\n---\n# X\n")
    second = retain_retired_stock_types_if_needed(vault, runtime)
    assert second["skipped"] == "already checked"
    entity_types.clear_entity_types_cache()
    assert entity_types.load_entity_types(vault).get("product") is None


def test_retire_leaves_no_receipt_for_a_missing_vault(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"

    summary = retain_retired_stock_types_if_needed(tmp_path / "nope", runtime)

    assert summary["skipped"]
    assert not (runtime / "migration").exists()


def test_retire_leaves_an_old_disabled_row_alone_and_complete(tmp_path: Path) -> None:
    vault = _retired_vault(tmp_path)
    (vault / "entity-types.yaml").write_text("- id: document\n  enabled: false\n", encoding="utf-8")

    summary = retain_retired_stock_types(vault, apply=True)

    # The vault already owns `document`, switched off on purpose; only the rest is kept.
    assert summary["retained"] == ["reference"]
    entity_types.clear_entity_types_cache()
    assert entity_types.load_entity_types(vault).get("document").enabled is False


def test_retire_records_a_refused_write_so_it_is_not_retried_every_boot(tmp_path: Path) -> None:
    vault = _retired_vault(tmp_path)
    (vault / "entity-types.yaml").write_text(
        "- id: doc\n  label: Doc\n  kind: note\n  folder: Docs\n", encoding="utf-8"
    )
    runtime = tmp_path / ".runtime"

    first = retain_retired_stock_types_if_needed(vault, runtime)
    second = retain_retired_stock_types_if_needed(vault, runtime)

    assert "failed" in first
    assert second["skipped"] == "already checked"
    assert json.loads(retired_receipt_path(runtime, vault).read_text())["failed"]


def test_retire_counts_untyped_notes_in_a_retired_folder(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/Documents/Plan.md", "# Plan\n")
    _note(vault, "personal/notes/p.md", "---\ntype: person\n---\n# P\n")

    summary = retain_retired_stock_types(vault, apply=True)

    assert summary["retained"] == ["document"]
    entity_types.clear_entity_types_cache()
    assert {e.path.name: e.type for e in scan_vault(vault)}["Plan.md"] == "document"
