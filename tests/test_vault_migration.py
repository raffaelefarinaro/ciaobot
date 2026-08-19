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

from ciao.vault_migration import (
    migrate_if_needed,
    migrate_vault_vocabulary,
    read_receipt,
    receipt_path,
)


def _note(vault: Path, relative: str, body: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/a.md", "---\ntype: doc\ntitle: A\ntags: [x]\n---\n# A\n\nbody\n")
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
    assert {change["from"] for change in summary["planned"]} == {"doc", "project-log"}
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
        "---\ntype: document\ntitle: A\ntags: [x]\n---\n# A\n\nbody\n"
    )


def test_apply_handles_a_quoted_value(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_vault_vocabulary(vault, apply=True)

    assert "type: log" in (vault / "personal/b.md").read_text(encoding="utf-8")


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
    note = _note(vault, "personal/a.md", "---\ntype: doc\n---\n# A\n")
    summary = migrate_vault_vocabulary(vault)  # plan against `doc`
    assert summary["planned"]

    note.write_text("---\ntype: reference\n---\n# A\n", encoding="utf-8")
    from ciao.vault_migration import _retype_frontmatter

    assert _retype_frontmatter(
        note.read_text(encoding="utf-8"), expect="doc", replacement="document"
    ) is None


def test_frontmatter_less_note_is_not_rewritten() -> None:
    from ciao.vault_migration import _retype_frontmatter

    assert _retype_frontmatter("# Just a heading\n", expect="doc", replacement="document") is None


# ---- the one-off gate ------------------------------------------------------


def test_receipt_makes_it_run_once(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    first = migrate_if_needed(vault, runtime)
    second = migrate_if_needed(vault, runtime)

    assert len(first["renamed"]) == 2
    assert second["skipped"] == "already migrated"
    assert receipt_path(runtime).is_file()


def test_receipt_records_what_was_left_for_the_user(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    migrate_if_needed(vault, runtime)

    receipt = read_receipt(runtime)
    assert receipt is not None
    assert receipt["schema_version"] == 1
    assert "frobnicate" in receipt["unresolved"]


def test_no_receipt_is_left_when_there_is_no_vault_yet(tmp_path: Path) -> None:
    """Bootstrap: the setup wizard has not created the vault. Leaving a receipt
    here would mark the real vault as migrated before it existed."""
    runtime = tmp_path / ".runtime"

    migrate_if_needed(tmp_path / "nope", runtime)

    assert read_receipt(runtime) is None


def test_a_corrupt_receipt_does_not_block_the_migration(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    receipt_path(runtime).parent.mkdir(parents=True, exist_ok=True)
    receipt_path(runtime).write_text("{ not json", encoding="utf-8")

    summary = migrate_if_needed(vault, runtime)

    assert len(summary["renamed"]) == 2
    assert json.loads(receipt_path(runtime).read_text(encoding="utf-8"))["renamed"]
