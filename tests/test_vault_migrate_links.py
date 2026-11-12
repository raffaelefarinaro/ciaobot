"""One-off conversion of vault `[[wikilinks]]` to relative markdown links.

The migration rewrites the prose of the user's own notes, so the tests here are
mostly about restraint: what it must *not* touch (code spans, escaped links,
`Logs/`, `Templates/`, generated files, frontmatter that YAML has to keep
parsing), and the reverse map that makes the rewrite undoable byte for byte
rather than approximately.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ciao.vault_index import (
    build_filename_index,
    _extract_body_links,
    markdown_destination,
    scan_vault,
)
from ciao.vault_migrate_links import (
    migrate_links,
    migrate_vault_links,
    peek_receipt,
    read_receipt,
    receipt_path,
    rewrite_note,
    unmigrate_links,
)

_FOO = """---
type: project
title: Foo
related:
  - "[[People/Mo]]"
  - Ideas/Thing
tags: [a]
---
# Foo

Owner is [[People/Mo]], aka [[People/Mo|Mo Salah]], history in [[People/Mo#History]].
Nothing here: [[Nowhere]].
Anchor only: [[#Section]].
Escaped: \\[[People/Mo]].
Inline code: `[[People/Mo]]`.

```
[[People/Mo]]
```
"""


def _note(vault: Path, relative: str, body: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/People/Mo.md", "---\ntype: person\n---\n# Mo\n")
    _note(vault, "personal/Ideas/Thing.md", "---\ntype: idea\n---\n# Thing\n")
    _note(vault, "personal/Projects/Foo.md", _FOO)
    _note(vault, "Root.md", "---\ntype: note\n---\n# Root\n\nSee [[People/Mo]].\n")
    # Excluded from index, lint, and search — and so from the migration.
    _note(vault, "Logs/2026-01-01.md", "# Log\n\n[[People/Mo]]\n")
    _note(vault, "Templates/note.md", "# T\n\n[[People/Mo]]\n")
    _note(vault, ".obsidian/scratch.md", "# S\n\n[[People/Mo]]\n")
    _note(vault, "INDEX.md", "# Index\n\n[[People/Mo]]\n")
    _note(vault, "personal/MEMORY.md", "# Memory\n\n[[People/Mo]]\n")
    return vault


def _snapshot(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted(vault.rglob("*.md"))
    }


def _foo(vault: Path) -> str:
    return (vault / "personal/Projects/Foo.md").read_text(encoding="utf-8")


# ---- dry run ---------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    before = _snapshot(vault)

    summary = migrate_vault_links(vault)

    assert summary["applied"] is False
    assert summary["rewrites"], "the dry run still has to report the diff"
    assert _snapshot(vault) == before


def test_a_missing_vault_is_reported_not_created(tmp_path: Path) -> None:
    summary = migrate_vault_links(tmp_path / "nope")

    assert summary["skipped"]
    assert not (tmp_path / "nope").exists()


# ---- the transformation ----------------------------------------------------


def test_a_resolved_link_is_relative_to_the_containing_note(tmp_path: Path) -> None:
    """Relative to the note's own directory, not to the vault root: a
    bundle-relative `/People/Mo.md` resolves in nothing but an OKF reader, and
    `vault_lint` skips absolute destinations, so it would also stop being
    broken-link checked."""
    vault = _vault(tmp_path)

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert "Owner is [Mo](../People/Mo.md)" in _foo(vault)


def test_a_note_at_the_vault_root_links_down_into_a_folder(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert "See [Mo](./personal/People/Mo.md)." in (
        vault / "Root.md"
    ).read_text(encoding="utf-8")


def test_an_alias_becomes_the_label(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert "aka [Mo Salah](../People/Mo.md)" in _foo(vault)


def test_an_anchor_is_dropped_and_recorded(tmp_path: Path) -> None:
    """Nothing in the viewer scrolls to a heading, so the anchor has no
    destination to survive into — but it is the one piece of information the
    rewrite destroys, so it has to be in the receipt before it can be dropped."""
    vault = _vault(tmp_path)

    summary = migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert "history in [Mo](../People/Mo.md)." in _foo(vault)
    assert [
        (item["ref"], item["anchor"]) for item in summary["anchors_dropped"]
    ] == [("People/Mo", "History")]


def test_an_unresolvable_ref_is_converted_best_effort(tmp_path: Path) -> None:
    """Leaving it as `[[Nowhere]]` would keep a second dialect alive forever. It
    was already a dead link and stays one — reported as `broken_markdown_links`,
    which is now the only broken-link bucket."""
    vault = _vault(tmp_path)

    summary = migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert "Nothing here: [Nowhere](./Nowhere.md)." in _foo(vault)
    assert [item["ref"] for item in summary["unresolved"]] == ["Nowhere"]


def test_a_pure_in_page_anchor_is_left_alone(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert "Anchor only: [[#Section]]." in _foo(vault)


def test_code_spans_and_escaped_links_are_not_rewritten(tmp_path: Path) -> None:
    """A wikilink inside a fence or behind a backslash is documentation *about*
    the syntax. Rewriting it silently edits a code sample."""
    vault = _vault(tmp_path)

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    text = _foo(vault)
    assert "Escaped: \\[[People/Mo]]." in text
    assert "Inline code: `[[People/Mo]]`." in text
    assert "```\n[[People/Mo]]\n```" in text


def test_a_destination_with_a_space_is_spelled_by_the_shared_emitter(
    tmp_path: Path,
) -> None:
    """CommonMark ends a bare destination at the first space, so `(../People/Mo
    Salah.md)` would point at `../People/Mo`. `markdown_destination` is the one
    place that decides how to spell it; the migration must not invent a second
    spelling, or the reader and the linter stop agreeing with it."""
    vault = tmp_path / "memory-vault"
    _note(vault, "People/Mo Salah.md", "---\ntype: person\n---\n# Mo Salah\n")
    note = _note(vault, "Notes/a.md", "---\ntype: note\n---\n# A\n\n[[Mo Salah]]\n")

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert f"[Mo Salah]({markdown_destination('../People/Mo Salah.md')})" in (
        note.read_text(encoding="utf-8")
    )
    assert "memory-vault/People/Mo Salah.md" in {
        entry.path.as_posix() for entry in scan_vault(vault)
    }
    assert _extract_body_links(
        note.read_text(encoding="utf-8"), "Notes/a.md"
    ) == ["People/Mo Salah"], "the migrated link has to stay a graph edge"


# ---- frontmatter -----------------------------------------------------------


def test_frontmatter_related_becomes_a_bare_ref(tmp_path: Path) -> None:
    """A markdown link in YAML is just a string, and `resolve_related` fails on
    the literal text — so the graph edge would disappear. Bare refs already
    work, and quoting/indentation must survive untouched."""
    vault = _vault(tmp_path)

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    text = _foo(vault)
    assert '  - "People/Mo"\n' in text
    assert "  - Ideas/Thing\n" in text
    assert "](" not in text.split("---\n")[1], "no markdown links may reach the YAML"


def test_the_graph_still_resolves_after_migrating_frontmatter(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    entries = {entry.path.as_posix(): entry for entry in scan_vault(vault)}
    foo = entries["memory-vault/personal/Projects/Foo.md"]
    assert "memory-vault/personal/People/Mo.md" in foo.related


def test_a_wikilink_in_a_prose_frontmatter_field_becomes_its_display_text(
    tmp_path: Path,
) -> None:
    """`description:` is rendered — `scan_vault` reads it into `Entry.description`
    and the index and PWA show it — so a wikilink left there displays as literal
    `[[People/Mo]]` once nothing parses the dialect. It cannot become a markdown
    link either (YAML would hand the literal string to `resolve_related`), so it
    reduces to the text a reader was meant to see.
    """
    vault = tmp_path / "memory-vault"
    _note(vault, "Notes/a.md", "---\ntype: note\ndescription: about [[People/Mo]]\n---\n# A\n")
    _note(vault, "People/Mo.md", "---\ntype: person\n---\n# Mo\n")

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    text = (vault / "Notes/a.md").read_text(encoding="utf-8")
    assert "description: about Mo" in text
    assert "[[" not in text


def test_an_alias_wins_over_the_path_in_a_prose_field(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "Notes/a.md",
        "---\ntype: note\ndescription: asked [[People/Mo|Mo Salah]] to help\n---\n# A\n",
    )
    _note(vault, "People/Mo.md", "---\ntype: person\n---\n# Mo\n")

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert "description: asked Mo Salah to help" in (
        vault / "Notes/a.md"
    ).read_text(encoding="utf-8")


def test_generated_files_and_excluded_dirs_are_untouched(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    # Only the two files this app regenerates, plus the excluded trees.
    # `MEMORY.md` is deliberately NOT here: it is hand-curated prose whose links
    # are real content, and skipping it left the curator's own notes as the last
    # wikilinks in the vault.
    guarded = [
        "Logs/2026-01-01.md",
        "Templates/note.md",
        ".obsidian/scratch.md",
        "INDEX.md",
    ]
    before = {name: (vault / name).read_text(encoding="utf-8") for name in guarded}

    summary = migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert {name: (vault / name).read_text(encoding="utf-8") for name in guarded} == before
    assert {item["path"] for item in summary["rewrites"]} == {
        "Root.md",
        "personal/MEMORY.md",
        "personal/Projects/Foo.md",
    }


def test_curated_memory_md_is_migrated_not_skipped(tmp_path: Path) -> None:
    """It is excluded from the index and the linter because it is not a note, but
    its links are content the user wrote and must convert with everything else."""
    vault = _vault(tmp_path)

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    text = (vault / "personal/MEMORY.md").read_text(encoding="utf-8")
    assert "[Mo](./People/Mo.md)" in text
    assert "[[" not in text


def test_a_lowercase_index_is_also_skipped(tmp_path: Path) -> None:
    """OKF names `index.md` in lowercase, so an imported bundle produces exactly
    that; a case-sensitive check would migrate a generated file."""
    vault = tmp_path / "memory-vault"
    _note(vault, "People/Mo.md", "---\ntype: person\n---\n# Mo\n")
    index = _note(vault, "index.md", "# Index\n\n[[People/Mo]]\n")

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert "[[People/Mo]]" in index.read_text(encoding="utf-8")


def test_migration_is_idempotent(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)
    after_first = _snapshot(vault)

    again = migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert again["rewrites"] == []
    assert again["files_rewritten"] == 0
    assert _snapshot(vault) == after_first


def test_a_note_with_no_wikilinks_keeps_its_mtime(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    untouched = vault / "personal/People/Mo.md"
    before = untouched.stat().st_mtime_ns

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert untouched.stat().st_mtime_ns == before


# ---- the pure core ---------------------------------------------------------


def test_rewrite_note_reports_the_line_of_each_change(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    index = build_filename_index(scan_vault(vault))

    new_text, changes = rewrite_note(_FOO, Path("personal/Projects/Foo.md"), index)

    # The `related:` item on line 5, the three links on line 11, the dead one
    # on line 12 — and nothing from the code spans below them.
    assert [change["line"] for change in changes] == [5, 11, 11, 11, 12]
    for change in changes:
        offset = change["offset"]
        assert new_text[offset : offset + len(change["to"])] == change["to"]


def test_rewrite_note_accepts_a_repo_relative_source_path(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    index = build_filename_index(scan_vault(vault))

    new_text, _ = rewrite_note(
        _FOO, Path("memory-vault/personal/Projects/Foo.md"), index
    )

    assert "[Mo](../People/Mo.md)" in new_text


# ---- reversibility ---------------------------------------------------------


def test_round_trip_is_byte_identical(tmp_path: Path) -> None:
    """The receipt records exact spans, so the undo is an inverse rather than a
    second guess at which links used to be wikilinks."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    before = _snapshot(vault)

    migrate_links(vault, runtime, apply=True)
    assert _snapshot(vault) != before
    unmigrate_links(vault, runtime, apply=True)

    assert _snapshot(vault) == before


def test_unmigrate_dry_run_writes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    migrated = _snapshot(vault)

    summary = unmigrate_links(vault, runtime)

    assert summary["restored"] == [
        "Root.md",
        "personal/MEMORY.md",
        "personal/Projects/Foo.md",
    ]
    assert _snapshot(vault) == migrated
    assert read_receipt(runtime, vault_root=vault) is not None


def test_unmigrate_needs_a_receipt(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    summary = unmigrate_links(vault, tmp_path / ".runtime", apply=True)

    assert summary["skipped"] == "no migration receipt to reverse"


def test_a_note_edited_after_the_migration_is_left_entirely_alone(tmp_path: Path) -> None:
    """A half-reverted note is worse than a skipped one, so one mismatched span
    disqualifies the whole file rather than the single edit."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    hand_edited = vault / "personal/Projects/Foo.md"
    hand_edited.write_text("# Rewritten by hand\n", encoding="utf-8")

    summary = unmigrate_links(vault, runtime, apply=True, force=True)

    assert hand_edited.read_text(encoding="utf-8") == "# Rewritten by hand\n"
    assert [item["path"] for item in summary["failed"]] == ["personal/Projects/Foo.md"]
    assert summary["restored"] == ["Root.md", "personal/MEMORY.md"]
    assert peek_receipt(runtime, vault_root=vault) is not None, "a partial revert stays revertible"


def test_a_clean_revert_drops_the_receipt(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)

    unmigrate_links(vault, runtime, apply=True)

    assert read_receipt(runtime, vault_root=vault) is None
    assert migrate_links(vault, runtime)["rewrites"], "migration is available again"


# ---- a run that could not finish -------------------------------------------


def _unwritable(vault: Path, relative: str) -> Path:
    """Make one note fail to write, the way a permission or quota problem does."""
    note = vault / relative
    note.chmod(0o444)
    return note


def test_a_partial_run_is_not_recorded_as_complete(tmp_path: Path) -> None:
    """One note that could not be written means the vault is not converted. A
    receipt saying otherwise made the migration stop short of done and report that
    it had finished: the next normal run was refused as "already migrated" while
    the note it never touched kept its wikilinks."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    blocked = _unwritable(vault, "personal/Projects/Foo.md")
    try:
        summary = migrate_links(vault, runtime, apply=True)

        assert [item["path"] for item in summary["failed"]] == ["personal/Projects/Foo.md"]
        assert summary["complete"] is False
        assert "[[People/Mo]]" in blocked.read_text(encoding="utf-8"), "still unconverted"
        # No *completed* receipt — so nothing downstream reads the vault as done.
        assert read_receipt(runtime, vault_root=vault) is None
        # But what did land is recorded, or it could never be taken back.
        recorded = peek_receipt(runtime, vault_root=vault)
        assert recorded is not None
        assert recorded["status"] == "partial"
        assert {item["path"] for item in recorded["rewrites"]} == {
            "Root.md", "personal/MEMORY.md", "personal/Projects/Foo.md",
        }
        # And the retry is a plain re-run, not something that needs --force.
        assert "skipped" not in migrate_links(vault, runtime, apply=True)
    finally:
        blocked.chmod(0o644)


def test_a_retry_after_a_partial_run_can_undo_both_batches(tmp_path: Path) -> None:
    """The reverse map has to stay usable across retries. Rotating it away on the
    second pass left the notes converted by the first with nothing to restore
    them — exactly what the map exists to promise."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    before = _snapshot(vault)
    blocked = _unwritable(vault, "personal/Projects/Foo.md")
    migrate_links(vault, runtime, apply=True)
    blocked.chmod(0o644)

    retry = migrate_links(vault, runtime, apply=True)

    assert "skipped" not in retry, "a partial run must not gate its own retry"
    assert retry["complete"] is True
    assert [item["path"] for item in retry["rewrites"]] != []
    assert read_receipt(runtime, vault_root=vault)["status"] == "migrated", "the vault is done now"
    assert unmigrate_links(vault, runtime, apply=True)["restored"] == [
        "Root.md",
        "personal/MEMORY.md",
        "personal/Projects/Foo.md",
    ]
    assert _snapshot(vault) == before, "both batches restored, byte for byte"


@pytest.mark.skipif(sys.platform == "win32", reason="chmod(0o000) cannot make a file unreadable on Windows")
def test_a_completed_receipt_is_downgraded_when_a_note_stops_being_readable(
    tmp_path: Path,
) -> None:
    """A forced pass that cannot even read a note has not verified it, so the
    vault stops counting as converted — while every entry already recorded is
    kept, because none of them was touched."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    first = read_receipt(runtime, vault_root=vault)
    unreadable = vault / "personal/MEMORY.md"
    unreadable.chmod(0o000)
    try:
        migrate_links(vault, runtime, apply=True, force=True)

        assert read_receipt(runtime, vault_root=vault) is None
        assert peek_receipt(runtime, vault_root=vault)["rewrites"] == first["rewrites"]
    finally:
        unreadable.chmod(0o644)


def test_a_receipt_written_before_status_existed_still_reads_as_complete(
    tmp_path: Path,
) -> None:
    """Installs that did the work before the field existed must not be re-nagged,
    nor have their migration re-run."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    legacy = json.loads(receipt_path(runtime).read_text(encoding="utf-8"))
    del legacy["status"]
    legacy["schema_version"] = 1
    for key in ("operation", "files", "legacy_rewrites"):
        del legacy[key]
    receipt_path(runtime).write_text(json.dumps(legacy), encoding="utf-8")

    assert read_receipt(runtime, vault_root=vault) is not None
    assert migrate_links(vault, runtime, apply=True)["skipped"] == "already migrated"


# ---- receipt and rails -----------------------------------------------------


def test_the_receipt_records_the_reverse_map(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    migrate_links(vault, runtime, apply=True)

    receipt = read_receipt(runtime, vault_root=vault)
    assert receipt is not None
    assert receipt["schema_version"] == 2
    assert receipt["migrated_at"]
    assert len(receipt["files"]) == 3
    assert {"path", "line", "offset", "from", "to"} <= set(receipt["rewrites"][0])
    assert receipt["anchors_dropped"] and receipt["unresolved"]
    assert not list(receipt_path(runtime).parent.glob("*.tmp")), (
        "the receipt is written through a .tmp sibling and replaced"
    )
    assert json.loads(receipt_path(runtime).read_text(encoding="utf-8"))["rewrites"]


def test_no_receipt_is_written_by_a_dry_run(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    migrate_links(vault, runtime)

    assert read_receipt(runtime, vault_root=vault) is None


def test_the_receipt_gates_a_second_run(tmp_path: Path) -> None:
    """Re-running would rewrite nothing (it is idempotent) but *would* overwrite
    the reverse map, and the two cannot be merged: the second pass shifts the
    offsets the first recorded."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)

    summary = migrate_links(vault, runtime, apply=True)

    assert summary["skipped"] == "already migrated"


def test_force_adds_to_the_reverse_map_instead_of_replacing_it(tmp_path: Path) -> None:
    """A second pass must not narrow what can be undone. It shifts only the
    offsets of the notes *it* rewrites, so every other note's entries stay exactly
    true and are carried into the new receipt; the earlier file is still archived
    under its own name."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    first = read_receipt(runtime, vault_root=vault)
    _note(vault, "personal/Notes/late.md", "---\ntype: note\n---\n# L\n\n[[People/Mo]]\n")

    migrate_links(vault, runtime, apply=True, force=True)

    kept = [
        path
        for path in receipt_path(runtime).parent.glob("vault-links.*.json")
        if path != receipt_path(runtime)
    ]
    assert len(kept) == 1
    assert json.loads(kept[0].read_text(encoding="utf-8"))["rewrites"] == first["rewrites"]
    merged = read_receipt(runtime, vault_root=vault)["rewrites"]
    assert "personal/Notes/late.md" in {item["path"] for item in merged}
    for item in first["rewrites"]:
        assert item in merged, "the first batch stays restorable"
    assert unmigrate_links(vault, runtime, apply=True)["restored"] == [
        "Root.md",
        "personal/MEMORY.md",
        "personal/Notes/late.md",
        "personal/Projects/Foo.md",
    ]


def test_a_note_rewritten_twice_refuses_to_supersede_live_evidence(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    receipt_before = receipt_path(runtime).read_bytes()
    root = vault / "Root.md"
    root.write_text(root.read_text(encoding="utf-8") + "\nLater: [[Ideas/Thing]].\n", encoding="utf-8")
    before = root.read_bytes()
    result = migrate_links(vault, runtime, apply=True, force=True)
    assert result["failed"]
    assert root.read_bytes() == before
    assert receipt_path(runtime).read_bytes() == receipt_before


def test_a_forced_rerun_with_nothing_to_do_keeps_the_reverse_map(tmp_path: Path) -> None:
    """Otherwise `--force` on an already-converted vault would swap a usable
    reverse map for an empty one, and leave the undo with nothing to undo."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    first = read_receipt(runtime, vault_root=vault)

    migrate_links(vault, runtime, apply=True, force=True)

    assert read_receipt(runtime, vault_root=vault) == first
    assert unmigrate_links(vault, runtime)["restored"]


def test_a_dirty_vault_is_refused_without_force(tmp_path: Path, monkeypatch) -> None:
    """`git checkout` has to stay a working undo, which it is not once the
    migration is mixed into edits the user had not committed."""
    vault = _vault(tmp_path)
    monkeypatch.setattr(
        "ciao.vault_migrate_links.vault_git_state",
        lambda root: {"is_repo": True, "head": "abc123", "dirty": True},
    )

    refused = migrate_links(vault, tmp_path / ".runtime", apply=True)
    forced = migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    assert refused["skipped"] == "vault has uncommitted changes"
    assert forced["rewrites"]
    assert read_receipt(tmp_path / ".runtime", vault_root=vault)["git_head_before"] == "abc123"


def test_a_corrupt_receipt_blocks_the_migration(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{ not json")
    before = _snapshot(vault)
    summary = migrate_links(vault, runtime, apply=True, force=True)
    assert summary["failed"]
    assert path.read_bytes() == b"{ not json"
    assert _snapshot(vault) == before


def test_a_custom_frontmatter_key_keeps_the_full_ref(tmp_path: Path) -> None:
    """Real vaults use their own keys as references — `project:`, `product:`,
    `people:` — not just `related:`. Treating those as prose reduced
    `work/products/slc` to `slc` and threw the path away, so the rule is "is the
    link the whole value", not a list of blessed key names.
    """
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "Notes/a.md",
        "---\n"
        "type: note\n"
        'project: "[[work/products/slc]]"\n'
        "people:\n"
        '  - "[[personal/People/Mo|Mo Salah]]"\n'
        "description: asked [[personal/People/Mo|Mo Salah]] to help\n"
        "---\n# A\n",
    )
    _note(vault, "work/products/slc.md", "---\ntype: product\n---\n# SLC\n")
    _note(vault, "personal/People/Mo.md", "---\ntype: person\n---\n# Mo\n")

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)

    text = (vault / "Notes/a.md").read_text(encoding="utf-8")
    # Whole-value references keep the path...
    assert 'project: "work/products/slc"' in text
    assert '- "personal/People/Mo"' in text
    # ...while prose keeps the label a reader was meant to see.
    assert "description: asked Mo Salah to help" in text
    assert "[[" not in text


def test_the_cli_does_not_claim_a_clean_vault_after_a_failed_write(
    tmp_path: Path, capsys
) -> None:
    """"No wikilinks found" was a flat lie on a retry.

    When every note carrying wikilinks fails to write, `rewrites` is empty — and
    the CLI printed the same line it uses for a genuinely clean vault, on stdout,
    while the failures went to stderr. An operator reading stdout was told the
    vault had nothing to migrate.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Mo.md").write_text(
        "---\ntype: person\n---\n# Mo\n", encoding="utf-8"
    )
    note = vault / "Root.md"
    note.write_text("---\ntype: doc\n---\nSee [[People/Mo]].\n", encoding="utf-8")
    note.chmod(0o444)
    try:
        code = cli.main(
            ["vault-migrate-links", "--apply", "--vault-root", str(vault), "--runtime-root", str(tmp_path / "runtime")]
        )
    finally:
        note.chmod(0o644)

    out = capsys.readouterr()
    assert code == 1
    assert "No wikilinks found" not in out.out
    assert "Permission denied" in out.err


_ROSTER = """---
type: project
title: UX Team
---
# UX Team

| Person | Role |
|---|---|
| [[People/Tibor\\|Tibor Kranjc]] | Lead |
| [[People/Mo]] | Design |

Outside a table: [[People/Tibor|Tibor Kranjc]].
"""


def test_a_table_cells_escaped_alias_pipe_stays_out_of_the_ref(tmp_path: Path) -> None:
    """`\\|` is the only legal alias pipe inside a markdown table.

    A bare `|` would close the cell, so a roster table has to escape it — and
    the backslash used to be read as part of the ref. `[[People/Tibor\\|Tibor]]`
    parsed as `People/Tibor\\`, resolved to nothing, and was rewritten to
    `./People/Tibor\\.md`: a path with a stray backslash, relative to the wrong
    directory, and reported as a wikilink that had *already* been dead. Every
    wikilink in a table cell in the vault hit it.
    """
    vault = tmp_path / "memory-vault"
    _note(vault, "People/Tibor.md", "---\ntype: person\n---\n# Tibor\n")
    _note(vault, "People/Mo.md", "---\ntype: person\n---\n# Mo\n")
    _note(vault, "work/projects/ux/ux-team.md", _ROSTER)

    summary = migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)
    text = (vault / "work/projects/ux/ux-team.md").read_text(encoding="utf-8")

    assert summary["unresolved"] == []
    # Same destination whether the pipe was escaped or not: the escape belongs
    # to the table, not to the link.
    assert text.count("[Tibor Kranjc](../../../People/Tibor.md)") == 2
    assert "\\.md" not in text
    assert "(./People/" not in text
    # The cell boundaries survive, so the row still renders as a row.
    assert "| [Tibor Kranjc](../../../People/Tibor.md) | Lead |" in text


def test_an_inline_flow_list_keeps_the_full_ref(tmp_path: Path) -> None:
    """A flow-sequence item is a reference, not prose.

    `related: ["[[a]]", "[[b]]"]` has two whole values on one line, and the
    old emptiness test called both of them prose — so each was replaced by its
    *display text* and the path was thrown away. Two refs that share a stem
    collapsed into the same ambiguous value, silently: the rewrite resolved, so
    nothing was reported as unresolved.
    """
    vault = tmp_path / "memory-vault"
    _note(vault, "work/products/slc.md", "---\ntype: product\n---\n# SLC\n")
    _note(vault, "work/products/legacy/slc.md", "---\ntype: product\n---\n# Old\n")
    _note(vault, "People/Mo.md", "---\ntype: person\n---\n# Mo\n")
    _note(
        vault,
        "Notes/a.md",
        '---\ntype: note\n'
        'related: ["[[work/products/slc]]", "[[work/products/legacy/slc]]"]\n'
        'people: [[[People/Mo]]]\n'
        'description: asked [[People/Mo|Mo Salah]] to help\n'
        '---\n# A\n',
    )

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)
    text = (vault / "Notes/a.md").read_text(encoding="utf-8")

    # Both paths kept, both distinguishable, quoting untouched.
    assert 'related: ["work/products/slc", "work/products/legacy/slc"]' in text
    assert "slc\", \"slc" not in text
    # An unquoted flow item is a reference too.
    assert "people: [People/Mo]" in text
    # Prose is still prose: the label, not the path.
    assert "description: asked Mo Salah to help" in text
    assert "[[" not in text


def _nested_vault(tmp_path: Path) -> Path:
    """A vault whose workspace subtree links out to shared root notes."""
    vault = tmp_path / "memory-vault"
    _note(vault, "People/Michal.md", "---\ntype: person\n---\n# Michal\n")
    _note(
        vault,
        "work/projects/cuj.md",
        "---\ntype: project\n---\n# CUJ\n\nOwner [[People/Michal]].\n",
    )
    return vault


def test_the_cli_refuses_a_root_nested_inside_the_configured_vault(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`--vault-root memory-vault/work` is not a vault, and the tool cannot tell.

    Refs resolve against a filename index built from whatever root is passed, so
    a workspace subtree makes every link into the vault's shared `People/` and
    root notes unresolvable. They were converted anyway — to `./People/Michal.md`,
    relative to a directory that has no `People/` — and reported as links that
    were already dead. The refusal covers the dry run too: the preview itself is
    what is wrong here, not just the write.
    """
    from ciao import cli

    vault = _nested_vault(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)

    code = cli.main(["vault-migrate-links", "--vault-root", str(vault / "work"), "--runtime-root", str(tmp_path / "runtime")])

    out = capsys.readouterr()
    assert code == 1
    assert "not a vault of its own" in out.err
    assert "resolves nowhere" in out.err
    # Nothing was previewed, so nothing was described as dead.
    assert "already dead" not in out.out
    assert "Would rewrite" not in out.out


def test_force_still_allows_a_nested_root(tmp_path: Path, monkeypatch, capsys) -> None:
    """The rail is a rail, not a wall — the other two are overridable too."""
    from ciao import cli

    vault = _nested_vault(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)

    code = cli.main(
        ["vault-migrate-links", "--vault-root", str(vault / "work"), "--force", "--runtime-root", str(tmp_path / "runtime")]
    )

    out = capsys.readouterr()
    assert code == 0
    assert "Would rewrite" in out.out


def test_the_vault_root_is_accepted_and_resolves_across_subtrees(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The same vault at its own root: the link resolves, nothing is unresolved."""
    from ciao import cli

    vault = _nested_vault(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)

    code = cli.main(["vault-migrate-links", "--vault-root", str(vault), "--runtime-root", str(tmp_path / "runtime")])

    out = capsys.readouterr()
    assert code == 0
    assert "../../People/Michal.md" in out.out
    assert "resolving to nothing" not in out.out


def test_an_unresolved_ref_is_not_called_a_link_that_was_already_dead(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The CLI cannot know a ref was dead before the migration touched it.

    Unresolved means "no note under the root this run was given", which is also
    what a working link looks like at a too-narrow root or behind a parse bug.
    Asserting pre-existing rot is how the tool got to label its own broken
    output as damage it had merely found.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    _note(vault, "Root.md", "---\ntype: doc\n---\n# R\n\nSee [[Nowhere]].\n")
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)

    code = cli.main(["vault-migrate-links", "--vault-root", str(vault), "--runtime-root", str(tmp_path / "runtime")])

    out = capsys.readouterr()
    assert code == 0
    assert "already dead" not in out.out
    assert "resolving to nothing" in out.out
    assert "the root is" in out.out


def test_unmigrating_from_the_wrong_root_names_the_recorded_one(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A receipt's paths are relative to the root the migration ran against.

    Reversing from anywhere else reads no file and restores nothing, and the
    failures were printed under "changed since the migration" — a cause nobody
    had established, sending the reader to look for edits that never happened.
    """
    from ciao import cli

    vault = _nested_vault(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    runtime = tmp_path / ".runtime"

    assert (
        cli.main(
            [
                "vault-migrate-links",
                "--apply",
                "--force",
                "--vault-root",
                str(vault),
                "--runtime-root",
                str(runtime),
            ]
        )
        == 0
    )
    capsys.readouterr()

    code = cli.main(
        [
            "vault-unmigrate-links",
            "--apply",
            "--vault-root",
            str(vault / "work"),
            "--runtime-root",
            str(runtime),
        ]
    )

    out = capsys.readouterr()
    assert code == 1
    assert "changed since the migration" not in out.err
    assert str(vault) in out.err
    assert "recover manually" in out.err


def test_prose_followed_by_a_comma_is_still_prose(tmp_path: Path) -> None:
    """The guard on how a flow sequence is recognised.

    Reading it off the trailing comma rather than the leading bracket looks
    equivalent and is not: the comma in `asked [[People/Mo|Mo]], then left`
    belongs to the sentence, and treating it as a value delimiter rewrites the
    sentence to carry a path where a name was meant to be read.
    """
    vault = tmp_path / "memory-vault"
    _note(vault, "People/Mo.md", "---\ntype: person\n---\n# Mo\n")
    _note(
        vault,
        "Notes/a.md",
        "---\ntype: note\n"
        "description: asked [[People/Mo|Mo Salah]], then left\n"
        "---\n# A\n",
    )

    migrate_links(vault, tmp_path / ".runtime", apply=True, force=True)
    text = (vault / "Notes/a.md").read_text(encoding="utf-8")

    assert "description: asked Mo Salah, then left" in text


def test_receipt_prepare_failure_attempts_no_note_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "A.md"
    before = b"---\ntype: note\n---\n[[Missing]]\n"
    note.write_bytes(before)
    runtime = tmp_path / "runtime"
    attempted_note_writes = []

    def deny_receipt(*args: object, **kwargs: object) -> None:
        raise PermissionError("receipt denied")

    def note_write(*args: object, **kwargs: object) -> bool:
        attempted_note_writes.append(note)
        return False

    monkeypatch.setattr(migration, "_publish_receipt", deny_receipt)
    monkeypatch.setattr(migration, "_replace_image", note_write)
    result = migrate_links(vault, runtime, apply=True)
    assert not attempted_note_writes
    assert note.read_bytes() == before
    assert not result.get("complete")


@pytest.mark.parametrize("before", [
    b"---\ntype: note\n---\n[[Missing]]\n",
    b"---\r\ntype: note\r\n---\r\n[[Missing]]\r\n",
    b"---\rtype: note\r\n---\n[[Missing]]\r",
    "\ufeff---\r\ntype: note\r\n---\r\nCafé [[Missing]]".encode(),
])
def test_exact_byte_roundtrip_crlf(tmp_path: Path, before: bytes) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "A.md"
    note.write_bytes(before)
    runtime = tmp_path / "runtime"
    assert migrate_links(vault, runtime, apply=True)["files_rewritten"] == 1
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert note.read_bytes() == before


def test_rewrite_preserves_mixed_newlines_and_exact_offsets() -> None:
    text = "---\rtype: note\rrelated: ['[[Missing]]']\r---\r\nBody [[Missing]]\r`[[code]]`\n"
    rewritten, changes = rewrite_note(text, Path("A.md"), {})
    assert rewritten == "---\rtype: note\rrelated: ['Missing']\r---\r\nBody [Missing](./Missing.md)\r`[[code]]`\n"
    assert [change["line"] for change in changes] == [3, 5]
    assert all(rewritten[c["offset"]:c["offset"] + len(c["to"])] == c["to"] for c in changes)


def test_bom_frontmatter_is_parsed_without_losing_bom() -> None:
    source = "\ufeff---\rrelated: ['[[Missing]]']\r---\rBody [[Missing]]\r"
    rewritten, changes = rewrite_note(source, Path("A.md"), {})
    assert rewritten == "\ufeff---\rrelated: ['Missing']\r---\rBody [Missing](./Missing.md)\r"
    assert [change["line"] for change in changes] == [2, 4]


def test_receipt_reader_rejects_wrong_vault_and_invalid_json(tmp_path: Path) -> None:
    from ciao.vault_migrate_links import ReceiptUnavailable

    vault = tmp_path / "vault"
    other = tmp_path / "other"
    vault.mkdir()
    other.mkdir()
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "vault_root": str(vault), "rewrites": []}))
    with pytest.raises(ReceiptUnavailable):
        peek_receipt(runtime, vault_root=other)
    path.write_text("{")
    with pytest.raises(ReceiptUnavailable):
        peek_receipt(runtime, vault_root=vault)


@pytest.mark.parametrize("stop_after", [0, 1, 2])
def test_interrupted_prepared_migration_can_retry_and_undo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_after: int) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    _note(vault, "B.md", "# B\n[[Missing]]\n")
    before = _snapshot(vault)
    runtime = tmp_path / "runtime"
    original = migration._replace_image
    original_replace = migration.os.replace
    written = []
    def track_replace(source, target):
        target_path = Path(target)
        if target_path in (vault / "A.md", vault / "B.md"):
            written.append(target_path.name)
        return original_replace(source, target)
    monkeypatch.setattr(migration.os, "replace", track_replace)
    count = 0

    def interrupt(root, image, *, undo):
        nonlocal count
        if count == stop_after:
            raise KeyboardInterrupt()
        count += 1
        return original(root, image, undo=undo)

    if stop_after < 2:
        monkeypatch.setattr(migration, "_replace_image", interrupt)
        with pytest.raises(KeyboardInterrupt):
            migrate_links(vault, runtime, apply=True)
    else:
        original_publish = migration._publish_receipt
        def interrupt_final(runtime, payload, *, archive):
            if payload["status"] == "migrated":
                raise KeyboardInterrupt()
            return original_publish(runtime, payload, archive=archive)
        monkeypatch.setattr(migration, "_publish_receipt", interrupt_final)
        with pytest.raises(KeyboardInterrupt):
            migrate_links(vault, runtime, apply=True)
        monkeypatch.setattr(migration, "_publish_receipt", original_publish)
    monkeypatch.setattr(migration, "_replace_image", original)
    assert migrate_links(vault, runtime, apply=True)["complete"]
    assert sorted(written) == ["A.md", "B.md"]
    monkeypatch.setattr(migration.os, "replace", original_replace)
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert _snapshot(vault) == before


def test_undo_cleanup_failure_is_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    before = _snapshot(vault)
    runtime = tmp_path / "runtime"
    migrate_links(vault, runtime, apply=True)
    original = Path.unlink
    def deny(path, *args, **kwargs):
        if path == receipt_path(runtime):
            raise PermissionError("denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", deny)
    assert unmigrate_links(vault, runtime, apply=True)["failed"]
    assert _snapshot(vault) == before
    monkeypatch.setattr(Path, "unlink", original)
    result = unmigrate_links(vault, runtime, apply=True)
    assert not result["failed"]
    assert result["files_restored"] == 0
    assert not receipt_path(runtime).exists()


def test_failed_receipt_replacement_preserves_previous_undo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    before = _snapshot(vault)
    runtime = tmp_path / "runtime"
    migrate_links(vault, runtime, apply=True)
    receipt_before = receipt_path(runtime).read_bytes()
    late = _note(vault, "B.md", "# B\n[[Missing]]\n")
    late_before = late.read_bytes()
    original = migration.os.replace
    def deny(source, target):
        if Path(target) == receipt_path(runtime):
            raise PermissionError("receipt replacement denied")
        return original(source, target)
    monkeypatch.setattr(migration.os, "replace", deny)
    result = migrate_links(vault, runtime, apply=True, force=True)
    assert result["failed"]
    assert receipt_path(runtime).read_bytes() == receipt_before
    assert late.read_bytes() == late_before
    monkeypatch.setattr(migration.os, "replace", original)
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert (vault / "A.md").read_bytes() == before["A.md"]


def test_final_receipt_failure_keeps_prepared_undo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    before = _snapshot(vault)
    runtime = tmp_path / "runtime"
    original = migration._publish_receipt
    def deny_final(runtime, payload, *, archive):
        if payload["status"] == "migrated":
            raise PermissionError("denied")
        return original(runtime, payload, archive=archive)
    monkeypatch.setattr(migration, "_publish_receipt", deny_final)
    result = migrate_links(vault, runtime, apply=True)
    assert result["failed"] and not result["complete"]
    assert read_receipt(runtime, vault_root=vault) is None
    monkeypatch.setattr(migration, "_publish_receipt", original)
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert _snapshot(vault) == before


@pytest.mark.parametrize("version", [1, 2])
def test_receipt_vault_identity_is_required(tmp_path: Path, version: int, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    first = tmp_path / "first"
    second = tmp_path / "second"
    _note(first, "A.md", "# A\n[[Missing]]\n")
    _note(second, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    migrate_links(first, runtime, apply=True)
    path = receipt_path(runtime)
    if version == 1:
        data = json.loads(path.read_bytes())
        data["schema_version"] = 1
        data.pop("files")
        data.pop("legacy_rewrites")
        data.pop("operation")
        path.write_text(json.dumps(data))
    evidence = path.read_bytes()
    before = _snapshot(second)
    attempts = []
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: attempts.append(True))
    assert migrate_links(second, runtime, apply=True, force=True)["failed"]
    assert unmigrate_links(second, runtime, apply=True, force=True)["failed"]
    assert not attempts and _snapshot(second) == before
    assert path.read_bytes() == evidence


def test_legacy_offsets_map_normalized_text_to_exact_boundaries(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "A.md"
    current = "é\r\n[One](./One.md)\r[Two](./Two.md)\n"
    note.write_bytes(current.encode())
    normalized = current.replace("\r\n", "\n").replace("\r", "\n")
    entries = []
    for label in ("One", "Two"):
        converted = f"[{label}](./{label}.md)"
        entries.append({"path": "A.md", "offset": normalized.index(converted), "from": f"[[{label}]]", "to": converted})
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "vault_root": str(vault), "rewrites": entries}))
    result = unmigrate_links(vault, runtime, apply=True)
    assert not result["failed"]
    assert note.read_bytes() == "é\r\n[[One]]\r[[Two]]\n".encode()


def test_symlinked_note_is_refused_before_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n[[Missing]]\n")
    try:
        (vault / "A.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    scans, reads = [], []
    original_text, original_bytes = Path.read_text, Path.read_bytes
    def track_text(self, *args, **kwargs):
        if self in (vault / "A.md", outside):
            reads.append(self)
        return original_text(self, *args, **kwargs)
    def track_bytes(self):
        if self in (vault / "A.md", outside):
            reads.append(self)
        return original_bytes(self)
    monkeypatch.setattr(Path, "read_text", track_text)
    monkeypatch.setattr(Path, "read_bytes", track_bytes)
    monkeypatch.setattr(migration, "scan_vault", lambda root: scans.append(root))
    result = migrate_links(vault, tmp_path / "runtime", apply=True)
    assert result["failed"] and not scans and not reads
    assert original_text(outside) == "# Outside\n[[Missing]]\n"


@pytest.mark.parametrize("name", [".ciaoignore", "entity-types.yaml"])
def test_symlinked_auxiliary_file_is_refused_before_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside")
    try:
        (vault / name).symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    scans, reads = [], []
    original_text, original_bytes = Path.read_text, Path.read_bytes
    def track_text(self, *args, **kwargs):
        if self in (vault / name, outside):
            reads.append(self)
        return original_text(self, *args, **kwargs)
    def track_bytes(self):
        if self in (vault / name, outside):
            reads.append(self)
        return original_bytes(self)
    monkeypatch.setattr(Path, "read_text", track_text)
    monkeypatch.setattr(Path, "read_bytes", track_bytes)
    monkeypatch.setattr(migration, "scan_vault", lambda root: scans.append(root))
    assert migrate_links(vault, tmp_path / "runtime", apply=True)["failed"]
    assert not scans and not reads
    assert original_text(outside) == "outside"


def test_busy_stable_lock_refuses_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    before = _snapshot(vault)
    runtime = tmp_path / "runtime"
    script = ("import sys; from pathlib import Path; "
              "from ciao.vault_migrate_links import _transaction_lock; "
              "lock = _transaction_lock(Path(sys.argv[1])); lock.__enter__(); "
              "print('locked', flush=True); sys.stdin.readline(); lock.__exit__(None, None, None)")
    process = subprocess.Popen([sys.executable, "-c", script, str(runtime)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "locked"
        lock = runtime / "migration" / "vault-links.lock"
        identity = lock.stat().st_ino
        original_replace = migration._replace_image
        attempts = []
        monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: attempts.append(True))
        assert migrate_links(vault, runtime, apply=True)["failed"]
        assert unmigrate_links(vault, runtime, apply=True)["failed"]
        assert not attempts and _snapshot(vault) == before
        monkeypatch.setattr(migration, "_replace_image", original_replace)
        assert process.stdin is not None
        process.stdin.write("release\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
        assert migrate_links(vault, runtime, apply=True)["complete"]
        assert lock.stat().st_ino == identity
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("corruption", ["hash", "duplicate", "root", "version"])
def test_invalid_receipts_refuse_all_mutations(tmp_path: Path, corruption: str) -> None:
    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    migrate_links(vault, runtime, apply=True)
    path = receipt_path(runtime)
    data = json.loads(path.read_bytes())
    if corruption == "hash":
        data["files"][0]["before_revision"] = "wrong"
    elif corruption == "duplicate":
        data["files"].append(data["files"][0])
    elif corruption == "root":
        data["vault_root"] = "relative"
    else:
        data["schema_version"] = 99
    path.write_text(json.dumps(data))
    evidence = path.read_bytes()
    before = _snapshot(vault)
    assert migrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert unmigrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert _snapshot(vault) == before
    assert path.read_bytes() == evidence


def test_prepared_conflict_is_not_overwritten_even_with_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    original = migration._replace_image
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(migration, "_replace_image", interrupt)
    with pytest.raises(KeyboardInterrupt):
        migrate_links(vault, runtime, apply=True)
    monkeypatch.setattr(migration, "_replace_image", original)
    note.write_bytes(b"external edit")
    assert migrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert unmigrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert note.read_bytes() == b"external edit"
    assert receipt_path(runtime).exists()


def test_legacy_undo_prepares_before_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "")
    note.write_bytes(b"# A\r\n[Missing](./Missing.md)\r\n")
    exact = note.read_bytes()
    normalized = exact.decode().replace("\r\n", "\n")
    target = "[Missing](./Missing.md)"
    entry = {"path": "A.md", "offset": normalized.index(target), "from": "[[Missing]]", "to": target}
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "vault_root": str(vault), "rewrites": [entry]}))
    receipt_before = path.read_bytes()
    original = migration._publish_receipt
    def deny(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(migration, "_publish_receipt", deny)
    assert unmigrate_links(vault, runtime, apply=True)["failed"]
    assert note.read_bytes() == exact
    assert path.read_bytes() == receipt_before
    monkeypatch.setattr(migration, "_publish_receipt", original)
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert note.read_bytes() == b"# A\r\n[[Missing]]\r\n"


def test_serialization_failure_attempts_no_note_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    before = _snapshot(vault)
    writes = []
    monkeypatch.setattr(migration.json, "dumps", lambda *args, **kwargs: (_ for _ in ()).throw(MemoryError()))
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: writes.append(True))
    result = migrate_links(vault, tmp_path / "runtime", apply=True)
    assert result["failed"]
    assert not writes
    assert _snapshot(vault) == before


@pytest.mark.parametrize("offset", [-1, 1000, "1", 0])
def test_invalid_legacy_offsets_refuse_without_writes(tmp_path: Path, offset: object) -> None:
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[Missing](./Missing.md)\n")
    before = note.read_bytes()
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    entry = {"path": "A.md", "offset": offset, "from": "[[Missing]]", "to": "[Missing](./Missing.md)"}
    path.write_text(json.dumps({"schema_version": 1, "vault_root": str(vault), "rewrites": [entry]}))
    evidence = path.read_bytes()
    assert unmigrate_links(vault, runtime, apply=True)["failed"]
    assert note.read_bytes() == before
    assert path.read_bytes() == evidence


@pytest.mark.parametrize("undo", [False, True])
def test_cli_receipt_errors_keep_existing_json_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, undo: bool) -> None:
    from ciao import cli
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    if undo:
        migrate_links(vault, runtime, apply=True)
    before = _snapshot(vault)
    def deny(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(migration, "_publish_receipt", deny)
    command = "vault-unmigrate-links" if undo else "vault-migrate-links"
    code = cli.main([command, "--apply", "--json", "--vault-root", str(vault), "--runtime-root", str(runtime)])
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert code == 1 and len(result["failed"]) == 1
    assert "no notes changed" in result["failed"][0]["error"]
    assert ("restored" if undo else "rewrites") in result
    assert "Traceback" not in output.err
    assert _snapshot(vault) == before


def test_large_images_have_no_new_arbitrary_cap(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n" + "x" * 210_000 + "\n[[Missing]]\n")
    before = note.read_bytes()
    runtime = tmp_path / "runtime"
    assert migrate_links(vault, runtime, apply=True)["complete"]
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert note.read_bytes() == before


def test_previously_interrupted_legacy_undo_refuses_guessing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    before = note.read_bytes()
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    entry = {"path": "A.md", "offset": 4, "from": "[[Missing]]", "to": "[Missing](./Missing.md)"}
    path.write_text(json.dumps({"schema_version": 1, "vault_root": str(vault), "rewrites": [entry]}))
    evidence = path.read_bytes()
    result = unmigrate_links(vault, runtime, apply=True)
    assert "recover manually" in result["failed"][0]["error"]
    assert note.read_bytes() == before
    assert path.read_bytes() == evidence


def test_interrupted_retry_allows_dirty_vault_from_its_own_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    original = migration._replace_image
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        migrate_links(vault, runtime, apply=True)
    monkeypatch.setattr(migration, "_replace_image", original)
    monkeypatch.setattr(migration, "vault_git_state", lambda root: {"is_repo": True, "head": "", "dirty": True})
    assert migrate_links(vault, runtime, apply=True)["complete"]


@pytest.mark.parametrize("name", ["vault-links.json", "vault-links.lock"])
def test_symlinked_receipt_or_lock_is_refused(tmp_path: Path, name: str) -> None:
    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    before = _snapshot(vault)
    runtime = tmp_path / "runtime"
    directory = runtime / "migration"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    try:
        (directory / name).symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert migrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert unmigrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert _snapshot(vault) == before
    assert outside.read_bytes() == b"outside"


def test_symlink_swap_before_final_replace_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    image = migration._image("A.md", note.read_text(), "# A\n[Missing](./Missing.md)\n")
    original = migration._safe_note
    calls = 0
    def swap(root, relative):
        nonlocal calls
        calls += 1
        if calls == 2:
            note.unlink()
            try:
                note.symlink_to(outside)
            except OSError:
                pytest.skip("symlinks unavailable")
        return original(root, relative)
    monkeypatch.setattr(migration, "_safe_note", swap)
    original_read = Path.read_bytes
    outside_reads = []
    def track_read(self):
        if self == note and note.is_symlink():
            outside_reads.append(self)
        return original_read(self)
    monkeypatch.setattr(Path, "read_bytes", track_read)
    with pytest.raises(OSError):
        migration._replace_image(vault, image, undo=False)
    assert not outside_reads and original_read(outside) == b"outside"
    # A swap after the final check remains outside the selected threat model.


def test_mid_write_failure_keeps_note_and_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    import stat

    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    before = note.read_bytes()
    mode = stat.S_IMODE(note.stat().st_mode)
    image = migration._image("A.md", before.decode(), "# A\n[Missing](./Missing.md)\n")
    original = migration.os.fdopen
    class FailingWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def write(self, data):
            self.stream.write(data[:3])
            raise OSError("injected mid-write failure")
    monkeypatch.setattr(migration.os, "fdopen", lambda *args, **kwargs: FailingWriter(original(*args, **kwargs)))
    with pytest.raises(OSError, match="mid-write"):
        migration._replace_image(vault, image, undo=False)
    assert note.read_bytes() == before
    assert stat.S_IMODE(note.stat().st_mode) == mode
    assert not list(vault.glob(".vault-links-*.tmp"))


def test_repeated_partial_undo_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    _note(vault, "B.md", "# B\n[[Missing]]\n")
    before = _snapshot(vault)
    runtime = tmp_path / "runtime"
    migrate_links(vault, runtime, apply=True)
    original = migration._replace_image
    count = 0
    def interrupt(root, image, *, undo):
        nonlocal count
        if count == 1:
            raise KeyboardInterrupt()
        count += 1
        return original(root, image, undo=undo)
    monkeypatch.setattr(migration, "_replace_image", interrupt)
    with pytest.raises(KeyboardInterrupt):
        unmigrate_links(vault, runtime, apply=True)
    monkeypatch.setattr(migration, "_replace_image", original)
    result = unmigrate_links(vault, runtime, apply=True)
    assert not result["failed"]
    assert result["restored"] == ["B.md"]
    assert _snapshot(vault) == before


@pytest.mark.parametrize("failure", ["write", "flush", "archive", "replace", "cleanup"])
def test_receipt_publication_failures_keep_active_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    import ciao.vault_migrate_links as migration

    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"previous active evidence")
    original_fdopen = migration.os.fdopen
    original_replace = migration.os.replace
    original_unlink = Path.unlink
    opened = 0
    class FailingStream:
        def __init__(self, stream, index):
            self.stream, self.index = stream, index
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def write(self, data):
            if (failure == "write" and self.index == 1) or (failure == "archive" and self.index == 2):
                raise OSError("injected write failure")
            return self.stream.write(data)
        def flush(self):
            if failure == "flush" and self.index == 1:
                raise OSError("injected flush failure")
            return self.stream.flush()
    def fdopen(*args, **kwargs):
        nonlocal opened
        opened += 1
        return FailingStream(original_fdopen(*args, **kwargs), opened)
    def replace(source, target):
        if failure in ("replace", "cleanup"):
            raise OSError("injected replacement failure")
        return original_replace(source, target)
    def unlink(target, *args, **kwargs):
        if failure == "cleanup" and target.name.startswith(".vault-links-"):
            raise OSError("injected cleanup failure")
        return original_unlink(target, *args, **kwargs)
    monkeypatch.setattr(migration.os, "fdopen", fdopen)
    monkeypatch.setattr(migration.os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError):
        migration._publish_receipt(runtime, {"candidate": True}, archive=True)
    assert path.read_bytes() == b"previous active evidence"


def test_cli_root_alias_uses_existing_canonical_identity(tmp_path: Path, capsys) -> None:
    from ciao import cli

    real = tmp_path / "real" / "vault"
    note = _note(real, "A.md", "# A\n[[Missing]]\n")
    before = note.read_bytes()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    selected = alias / "vault"
    assert cli._resolve_vault_root(selected) == real.resolve()
    runtime = tmp_path / "runtime"
    args = ["--apply", "--vault-root", str(selected), "--runtime-root", str(runtime)]
    assert cli.main(["vault-migrate-links", *args]) == 0
    assert json.loads(receipt_path(runtime).read_bytes())["vault_root"] == str(real.resolve())
    assert cli.main(["vault-unmigrate-links", *args]) == 0
    assert note.read_bytes() == before
    capsys.readouterr()


def test_mixed_legacy_and_new_recovery_survives_interrupted_undo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    legacy_note = _note(vault, "A.md", "")
    legacy_note.write_bytes(b"# A\r\n[Missing](./Missing.md)\r\n")
    new_note = _note(vault, "B.md", "# B\n[[Missing]]\n")
    new_before = new_note.read_bytes()
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    entry = {"path": "A.md", "offset": 4, "from": "[[Missing]]", "to": "[Missing](./Missing.md)"}
    path.write_text(json.dumps({"schema_version": 1, "status": "partial", "vault_root": str(vault), "rewrites": [entry]}))
    assert migrate_links(vault, runtime, apply=True)["complete"]
    original = migration._replace_image
    calls = 0
    def interrupt(root, image, *, undo):
        nonlocal calls
        if calls == 1:
            raise KeyboardInterrupt()
        calls += 1
        return original(root, image, undo=undo)
    monkeypatch.setattr(migration, "_replace_image", interrupt)
    with pytest.raises(KeyboardInterrupt):
        unmigrate_links(vault, runtime, apply=True)
    monkeypatch.setattr(migration, "_replace_image", original)
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert legacy_note.read_bytes() == b"# A\r\n[[Missing]]\r\n"
    assert new_note.read_bytes() == new_before


def test_serialization_failure_preserves_existing_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration

    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    migrate_links(vault, runtime, apply=True)
    late = _note(vault, "B.md", "# B\n[[Missing]]\n")
    before = late.read_bytes()
    evidence = receipt_path(runtime).read_bytes()
    monkeypatch.setattr(migration.json, "dumps", lambda *args, **kwargs: (_ for _ in ()).throw(MemoryError()))
    assert migrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert receipt_path(runtime).read_bytes() == evidence
    assert late.read_bytes() == before


@pytest.mark.parametrize("relative", ["Logs/ignored.md", "INDEX.md"])
def test_excluded_and_generated_symlinks_do_not_enter_scan(tmp_path: Path, relative: str) -> None:
    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    link = vault / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert migrate_links(vault, tmp_path / "runtime", apply=True)["complete"]
    assert outside.read_bytes() == b"outside"


def test_recorded_parent_symlink_is_refused_before_undo_read(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = _note(vault, "Folder/A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    migrate_links(vault, runtime, apply=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "A.md").write_bytes(b"outside")
    note.unlink()
    note.parent.rmdir()
    try:
        note.parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = unmigrate_links(vault, runtime, apply=True)
    assert result["failed"]
    assert (outside / "A.md").read_bytes() == b"outside"
    assert receipt_path(runtime).exists()


def test_legacy_overlapping_offsets_refuse_without_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "é\n[É](./É.md)\n")
    before = note.read_bytes()
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    entry = {"path": "A.md", "offset": 2, "from": "[[É]]", "to": "[É](./É.md)"}
    path.write_text(json.dumps({"schema_version": 1, "vault_root": str(vault), "rewrites": [entry, entry]}))
    evidence = path.read_bytes()
    writes = []
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: writes.append(True))
    assert unmigrate_links(vault, runtime, apply=True)["failed"]
    assert not writes and note.read_bytes() == before and path.read_bytes() == evidence


def test_interrupted_legacy_only_undo_retries_from_prepared_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    first = _note(vault, "A.md", "é\r\n[É](./É.md)\r\n")
    second = _note(vault, "B.md", "β\r[Deux](./Deux.md)\n")
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    entries = [{"path": name, "offset": 2, "from": f"[[{label}]]", "to": f"[{label}](./{label}.md)"} for name, label in (("A.md", "É"), ("B.md", "Deux"))]
    path.write_text(json.dumps({"schema_version": 1, "vault_root": str(vault), "rewrites": entries}))
    original = migration._replace_image
    calls = 0
    def interrupt(root, image, *, undo):
        nonlocal calls
        prepared = json.loads(path.read_bytes())
        assert prepared["schema_version"] == 2 and prepared["operation"] == "undo"
        if calls == 1:
            raise KeyboardInterrupt()
        calls += 1
        return original(root, image, undo=undo)
    monkeypatch.setattr(migration, "_replace_image", interrupt)
    with pytest.raises(KeyboardInterrupt):
        unmigrate_links(vault, runtime, apply=True)
    monkeypatch.setattr(migration, "_replace_image", original)
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert first.read_bytes() == "é\r\n[[É]]\r\n".encode()
    assert second.read_bytes() == "β\r[[Deux]]\n".encode()


def test_symlink_swap_before_first_image_read_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    note = _note(vault, "nested/A.md", "# A\n[[Missing]]\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "A.md").write_bytes(b"outside")
    image = migration._image("nested/A.md", note.read_text(), "changed")
    original = migration._safe_note
    def swap(root, relative):
        note.rename(tmp_path / "saved")
        note.parent.rmdir()
        try:
            note.parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
        return original(root, relative)
    monkeypatch.setattr(migration, "_safe_note", swap)
    original_read = Path.read_bytes
    outside_reads = []
    def track_read(self):
        if self == note or self == outside / "A.md":
            outside_reads.append(self)
        return original_read(self)
    monkeypatch.setattr(Path, "read_bytes", track_read)
    with pytest.raises(OSError):
        migration._replace_image(vault, image, undo=False)
    assert not outside_reads
    assert original_read(outside / "A.md") == b"outside"
    # Swaps after the last safety check are outside the selected threat model.


def test_process_exit_after_first_note_leaves_recoverable_journal(tmp_path: Path) -> None:
    import subprocess
    import sys

    vault = tmp_path / "vault"
    first = _note(vault, "A.md", "# A\n[[Missing]]\n")
    second = _note(vault, "B.md", "# B\n[[Missing]]\n")
    before = {p.name: p.read_bytes() for p in (first, second)}
    runtime = tmp_path / "runtime"
    script = """
import os, sys
from pathlib import Path
import ciao.vault_migrate_links as m
original = m._replace_image
calls = 0
def terminate(root, image, *, undo):
    global calls
    result = original(root, image, undo=undo)
    calls += 1
    if calls == 1:
        os._exit(73)
    return result
m._replace_image = terminate
m.migrate_links(Path(sys.argv[1]), Path(sys.argv[2]), apply=True)
"""
    process = subprocess.run([sys.executable, "-c", script, str(vault), str(runtime)], check=False)
    assert process.returncode == 73
    receipt = json.loads(receipt_path(runtime).read_bytes())
    assert receipt["status"] == "prepared" and len(receipt["files"]) == 2
    assert sum(p.read_bytes() != before[p.name] for p in (first, second)) == 1
    assert not migrate_links(vault, runtime, apply=True)["failed"]
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert {p.name: p.read_bytes() for p in (first, second)} == before


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("recorded", [None, "", "relative", "missing", "moved"])
def test_invalid_or_moved_vault_identity_refuses_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: str | None, version: int) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    before = note.read_bytes()
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    data = {"schema_version": version, "rewrites": []}
    if version == 2:
        data.update(status="prepared", operation="migrate", files=[], legacy_rewrites=[])
    if recorded is not None:
        data["vault_root"] = str(tmp_path / "missing") if recorded == "missing" else recorded
    if recorded == "moved":
        data["vault_root"] = str(vault)
        vault.rename(tmp_path / "moved")
        vault = tmp_path / "moved"
        note = vault / "A.md"
    path.write_text(json.dumps(data))
    evidence = path.read_bytes()
    writes = []
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: writes.append(True))
    assert migrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert unmigrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert not writes and note.read_bytes() == before and path.read_bytes() == evidence


def test_unreadable_receipt_refuses_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    path = receipt_path(runtime)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    before = note.read_bytes()
    original = Path.read_bytes
    def deny(self):
        if self == path:
            raise PermissionError("denied")
        return original(self)
    writes = []
    monkeypatch.setattr(Path, "read_bytes", deny)
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: writes.append(True))
    assert migrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert unmigrate_links(vault, runtime, apply=True, force=True)["failed"]
    assert not writes and note.read_bytes() == before
    assert original(path) == b"{}"


def test_prepared_retry_uses_recorded_image_when_index_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    pending = _note(vault, "B.md", "# B\n[[Target]]\n")
    runtime = tmp_path / "runtime"
    original = migration._replace_image
    calls = 0
    def interrupt(root, image, *, undo):
        nonlocal calls
        if calls == 1:
            raise KeyboardInterrupt()
        calls += 1
        return original(root, image, undo=undo)
    monkeypatch.setattr(migration, "_replace_image", interrupt)
    with pytest.raises(KeyboardInterrupt):
        migrate_links(vault, runtime, apply=True)
    planned = next(f for f in json.loads(receipt_path(runtime).read_bytes())["files"] if f["path"] == "B.md")
    _note(vault, "nested/Target.md", "# Target\n")
    monkeypatch.setattr(migration, "_replace_image", original)
    result = migrate_links(vault, runtime, apply=True)
    assert result["complete"], result
    assert pending.read_bytes() == planned["after_text"].encode()


def test_observed_junction_refuses_before_vault_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    _note(vault, "nested/A.md", "# A\n[[Missing]]\n")
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == vault / "nested" or original(self))
    scans = []
    monkeypatch.setattr(migration, "scan_vault", lambda *args: scans.append(True))
    result = migrate_links(vault, tmp_path / "runtime", apply=True)
    assert result["failed"] and not scans


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_real_windows_junction_is_refused_before_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    from ciao.os_support.links import link_dir
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    _note(outside, "A.md", "# Outside\n[[Missing]]\n")
    try:
        link_dir(outside, vault / "nested")
    except OSError:
        pytest.skip("junction creation unavailable")
    scans = []
    monkeypatch.setattr(migration, "scan_vault", lambda *args: scans.append(True))
    assert migrate_links(vault, tmp_path / "runtime", apply=True)["failed"]
    assert not scans and (outside / "A.md").read_text() == "# Outside\n[[Missing]]\n"


def test_mixed_newline_frontmatter_and_code_apply_roundtrip(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    before = "---\rtype: note\rrelated: ['[[Missing]]']\rsummary: 'see [[Missing]]'\r---\r\nBody [[Missing]]\r`[[inline]]`\n```\r[[fenced]]\r```\n".encode()
    note = _note(vault, "A.md", "")
    note.write_bytes(before)
    runtime = tmp_path / "runtime"
    result = migrate_links(vault, runtime, apply=True)
    assert result["complete"]
    after = note.read_bytes().decode()
    assert "related: ['Missing']" in after and "summary: 'see Missing'" in after
    assert "Body [Missing](./Missing.md)" in after
    assert "`[[inline]]`\n```\r[[fenced]]\r```\n" in after
    assert all(after[e["offset"]:e["offset"] + len(e["to"])] == e["to"] for e in result["rewrites"])
    assert [e["line"] for e in result["rewrites"]] == [3, 4, 6]
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert note.read_bytes() == before


def test_foreign_all_original_prepared_undo_never_attempts_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    first, second = tmp_path / "first", tmp_path / "second"
    _note(first, "A.md", "# A\n[[Missing]]\n")
    other = _note(second, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        migrate_links(first, runtime, apply=True)
    evidence = receipt_path(runtime).read_bytes()
    calls = []
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: calls.append(True))
    assert unmigrate_links(second, runtime, apply=True, force=True)["failed"]
    assert not calls and other.read_bytes() == b"# A\n[[Missing]]\n"
    assert receipt_path(runtime).read_bytes() == evidence


@pytest.mark.parametrize("call", [1, 2])
def test_observed_junction_refused_before_image_read_or_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: int) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    note = _note(vault, "nested/A.md", "# A\n[[Missing]]\n")
    image = migration._image("nested/A.md", note.read_text(), "changed")
    original = Path.is_junction
    checks = 0
    def junction(self):
        nonlocal checks
        if self == note.parent:
            checks += 1
            if checks == call:
                return True
        return original(self)
    monkeypatch.setattr(Path, "is_junction", junction)
    with pytest.raises(OSError):
        migration._replace_image(vault, image, undo=False)
    assert note.read_text() == "# A\n[[Missing]]\n"


def test_resume_then_receipt_failure_reports_recovery_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    original = migration._replace_image
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        migrate_links(vault, runtime, apply=True)
    evidence = receipt_path(runtime).read_bytes()
    monkeypatch.setattr(migration, "_replace_image", original)
    monkeypatch.setattr(migration, "_publish_receipt", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")))
    result = migrate_links(vault, runtime, apply=True)
    assert result["failed"] and "prior recovery receipt retained" in result["failed"][0]["error"]
    assert note.read_bytes() != b"# A\n[[Missing]]\n" and receipt_path(runtime).read_bytes() == evidence


def test_runtime_junction_refused_at_receipt_and_lock_seams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    directory = runtime / "migration"
    directory.mkdir(parents=True)
    path = receipt_path(runtime)
    path.write_bytes(b"outside evidence")
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == directory or original(self))
    reads = []
    original_read = Path.read_bytes
    def track(self):
        if self == path:
            reads.append(self)
        return original_read(self)
    monkeypatch.setattr(Path, "read_bytes", track)
    with pytest.raises(migration.ReceiptUnavailable):
        migration.peek_receipt(runtime, vault_root=vault)
    with pytest.raises(OSError):
        migration._publish_receipt(runtime, {}, archive=True)
    with pytest.raises(OSError):
        with migration._transaction_lock(runtime):
            pytest.fail("junction accepted")
    assert not reads and original_read(path) == b"outside evidence"
    assert not list(directory.glob(".vault-links-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_real_runtime_junction_refused(tmp_path: Path) -> None:
    import ciao.vault_migrate_links as migration
    from ciao.os_support.links import link_dir
    vault = tmp_path / "vault"
    _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence = outside / "vault-links.json"
    evidence.write_bytes(b"outside evidence")
    try:
        link_dir(outside, runtime / "migration")
    except OSError:
        pytest.skip("junction creation unavailable")
    with pytest.raises(migration.ReceiptUnavailable):
        migration.peek_receipt(runtime, vault_root=vault)
    assert migrate_links(vault, runtime, apply=True)["failed"]
    assert unmigrate_links(vault, runtime, apply=True)["failed"]
    assert evidence.read_bytes() == b"outside evidence"
    assert sorted(p.name for p in outside.iterdir()) == ["vault-links.json"]


def test_runtime_junction_observed_before_undo_cleanup_refuses_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    assert migrate_links(vault, runtime, apply=True)["complete"]
    original_replace = migration._replace_image
    original_junction = Path.is_junction
    observed = False
    def replace(root, image, *, undo):
        nonlocal observed
        result = original_replace(root, image, undo=undo)
        observed = True
        return result
    monkeypatch.setattr(migration, "_replace_image", replace)
    monkeypatch.setattr(Path, "is_junction", lambda self: (observed and self == runtime / "migration") or original_junction(self))
    result = unmigrate_links(vault, runtime, apply=True)
    assert result["failed"] and not result["receipt_removed"]
    assert receipt_path(runtime).exists() and note.read_bytes() == b"# A\n[[Missing]]\n"


def test_preflight_failure_before_resume_attempts_no_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.vault_migrate_links as migration
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    original = migration._replace_image
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        migrate_links(vault, runtime, apply=True)
    evidence = receipt_path(runtime).read_bytes()
    attempts = []
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: attempts.append(True))
    monkeypatch.setattr(migration, "_preflight", lambda root: (_ for _ in ()).throw(OSError("preflight denied")))
    result = migrate_links(vault, runtime, apply=True)
    assert result["failed"] and result["files_rewritten"] == 0 and not attempts
    assert note.read_bytes() == b"# A\n[[Missing]]\n" and receipt_path(runtime).read_bytes() == evidence
    monkeypatch.setattr(migration, "_replace_image", original)


@pytest.mark.parametrize("seam", ["planning", "publication"])
def test_post_resume_failure_reports_changes_and_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, seam: str) -> None:
    import ciao.vault_migrate_links as migration
    from ciao import cli
    vault = tmp_path / "vault"
    note = _note(vault, "A.md", "# A\n[[Missing]]\n")
    runtime = tmp_path / "runtime"
    original = migration._replace_image
    monkeypatch.setattr(migration, "_replace_image", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        migrate_links(vault, runtime, apply=True)
    evidence = receipt_path(runtime).read_bytes()
    monkeypatch.setattr(migration, "_replace_image", original)
    if seam == "planning":
        monkeypatch.setattr(migration, "migrate_vault_links", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("planning denied")))
    else:
        monkeypatch.setattr(migration, "_publish_receipt", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("publication denied")))
    args = ["vault-migrate-links", "--apply", "--vault-root", str(vault), "--runtime-root", str(runtime), "--json"]
    assert cli.main(args) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["failed"] and summary["files_rewritten"] == 1 and summary["resumed_files"] == 1
    assert summary["rewrites"] and summary["receipt_path"] == str(receipt_path(runtime))
    assert note.read_bytes() != b"# A\n[[Missing]]\n" and receipt_path(runtime).read_bytes() == evidence
    note.write_bytes(b"# A\n[[Missing]]\n")
    assert cli.main(args[:-1]) == 1
    output = capsys.readouterr()
    assert "Resumed" in output.out and str(receipt_path(runtime)) in output.out
    assert "did NOT finish" in output.out and "Failed:" in output.err


@pytest.mark.parametrize("blocked", [{"B.md"}, {"A.md", "B.md"}])
def test_fresh_apply_reports_actual_writes_and_retains_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, blocked: set[str]) -> None:
    import ciao.vault_migrate_links as migration
    from ciao import cli
    vault = tmp_path / "vault"
    first = _note(vault, "A.md", "# A\n[[Missing]]\n")
    second = _note(vault, "B.md", "# B\n[[Missing]]\n")
    originals = {p.name: p.read_bytes() for p in (first, second)}
    runtime = tmp_path / "runtime"
    original = migration._replace_image
    def refuse(root, image, *, undo):
        if not undo and image["path"] in blocked:
            raise PermissionError("denied")
        return original(root, image, undo=undo)
    monkeypatch.setattr(migration, "_replace_image", refuse)
    args = ["vault-migrate-links", "--apply", "--vault-root", str(vault), "--runtime-root", str(runtime)]
    assert cli.main([*args, "--json"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["files_rewritten"] == 2 - len(blocked)
    assert {entry["path"] for entry in summary["rewrites"]} == {"A.md", "B.md"}
    receipt = json.loads(receipt_path(runtime).read_bytes())
    assert {entry["path"] for entry in receipt["rewrites"]} == {"A.md", "B.md"}
    assert {image["path"] for image in receipt["files"]} == {"A.md", "B.md"}
    assert sum(p.read_bytes() != originals[p.name] for p in (first, second)) == 2 - len(blocked)
    monkeypatch.setattr(migration, "_replace_image", original)
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert {p.name: p.read_bytes() for p in (first, second)} == originals
    monkeypatch.setattr(migration, "_replace_image", refuse)
    assert cli.main(args) == 1
    output = capsys.readouterr()
    assert f"Rewrote {2 - len(blocked)} of 2 planned note(s)" in output.out
    assert "Failed:" in output.err and "did NOT finish" in output.out
