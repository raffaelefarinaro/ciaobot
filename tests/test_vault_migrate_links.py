"""One-off conversion of vault `[[wikilinks]]` to relative markdown links.

The migration rewrites the prose of the user's own notes, so the tests here are
mostly about restraint: what it must *not* touch (code spans, escaped links,
`Logs/`, `Templates/`, generated files, frontmatter that YAML has to keep
parsing), and the reverse map that makes the rewrite undoable byte for byte
rather than approximately.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.vault_index import (
    _build_filename_index,
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
    unmigrate_vault_links,
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


def _snapshot(vault: Path) -> dict[str, str]:
    return {
        path.relative_to(vault).as_posix(): path.read_text(encoding="utf-8")
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

    migrate_vault_links(vault, apply=True)

    assert "Owner is [Mo](../People/Mo.md)" in _foo(vault)


def test_a_note_at_the_vault_root_links_down_into_a_folder(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_vault_links(vault, apply=True)

    assert "See [Mo](./personal/People/Mo.md)." in (
        vault / "Root.md"
    ).read_text(encoding="utf-8")


def test_an_alias_becomes_the_label(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_vault_links(vault, apply=True)

    assert "aka [Mo Salah](../People/Mo.md)" in _foo(vault)


def test_an_anchor_is_dropped_and_recorded(tmp_path: Path) -> None:
    """Nothing in the viewer scrolls to a heading, so the anchor has no
    destination to survive into — but it is the one piece of information the
    rewrite destroys, so it has to be in the receipt before it can be dropped."""
    vault = _vault(tmp_path)

    summary = migrate_vault_links(vault, apply=True)

    assert "history in [Mo](../People/Mo.md)." in _foo(vault)
    assert [
        (item["ref"], item["anchor"]) for item in summary["anchors_dropped"]
    ] == [("People/Mo", "History")]


def test_an_unresolvable_ref_is_converted_best_effort(tmp_path: Path) -> None:
    """Leaving it as `[[Nowhere]]` would keep a second dialect alive forever. It
    was already a dead link and stays one — reported as `broken_markdown_links`,
    which is now the only broken-link bucket."""
    vault = _vault(tmp_path)

    summary = migrate_vault_links(vault, apply=True)

    assert "Nothing here: [Nowhere](./Nowhere.md)." in _foo(vault)
    assert [item["ref"] for item in summary["unresolved"]] == ["Nowhere"]


def test_a_pure_in_page_anchor_is_left_alone(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_vault_links(vault, apply=True)

    assert "Anchor only: [[#Section]]." in _foo(vault)


def test_code_spans_and_escaped_links_are_not_rewritten(tmp_path: Path) -> None:
    """A wikilink inside a fence or behind a backslash is documentation *about*
    the syntax. Rewriting it silently edits a code sample."""
    vault = _vault(tmp_path)

    migrate_vault_links(vault, apply=True)

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

    migrate_vault_links(vault, apply=True)

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
    """A markdown link in YAML is just a string, and `_resolve_related` fails on
    the literal text — so the graph edge would disappear. Bare refs already
    work, and quoting/indentation must survive untouched."""
    vault = _vault(tmp_path)

    migrate_vault_links(vault, apply=True)

    text = _foo(vault)
    assert '  - "People/Mo"\n' in text
    assert "  - Ideas/Thing\n" in text
    assert "](" not in text.split("---\n")[1], "no markdown links may reach the YAML"


def test_the_graph_still_resolves_after_migrating_frontmatter(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    migrate_vault_links(vault, apply=True)

    entries = {entry.path.as_posix(): entry for entry in scan_vault(vault)}
    foo = entries["memory-vault/personal/Projects/Foo.md"]
    assert "memory-vault/personal/People/Mo.md" in foo.related


def test_a_wikilink_in_a_prose_frontmatter_field_becomes_its_display_text(
    tmp_path: Path,
) -> None:
    """`description:` is rendered — `scan_vault` reads it into `Entry.description`
    and the index and PWA show it — so a wikilink left there displays as literal
    `[[People/Mo]]` once nothing parses the dialect. It cannot become a markdown
    link either (YAML would hand the literal string to `_resolve_related`), so it
    reduces to the text a reader was meant to see.
    """
    vault = tmp_path / "memory-vault"
    _note(vault, "Notes/a.md", "---\ntype: note\ndescription: about [[People/Mo]]\n---\n# A\n")
    _note(vault, "People/Mo.md", "---\ntype: person\n---\n# Mo\n")

    migrate_vault_links(vault, apply=True)

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

    migrate_vault_links(vault, apply=True)

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

    summary = migrate_vault_links(vault, apply=True)

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

    migrate_vault_links(vault, apply=True)

    text = (vault / "personal/MEMORY.md").read_text(encoding="utf-8")
    assert "[Mo](./People/Mo.md)" in text
    assert "[[" not in text


def test_a_lowercase_index_is_also_skipped(tmp_path: Path) -> None:
    """OKF names `index.md` in lowercase, so an imported bundle produces exactly
    that; a case-sensitive check would migrate a generated file."""
    vault = tmp_path / "memory-vault"
    _note(vault, "People/Mo.md", "---\ntype: person\n---\n# Mo\n")
    index = _note(vault, "index.md", "# Index\n\n[[People/Mo]]\n")

    migrate_vault_links(vault, apply=True)

    assert "[[People/Mo]]" in index.read_text(encoding="utf-8")


def test_migration_is_idempotent(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    migrate_vault_links(vault, apply=True)
    after_first = _snapshot(vault)

    again = migrate_vault_links(vault, apply=True)

    assert again["rewrites"] == []
    assert again["files_rewritten"] == 0
    assert _snapshot(vault) == after_first


def test_a_note_with_no_wikilinks_keeps_its_mtime(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    untouched = vault / "personal/People/Mo.md"
    before = untouched.stat().st_mtime_ns

    migrate_vault_links(vault, apply=True)

    assert untouched.stat().st_mtime_ns == before


# ---- the pure core ---------------------------------------------------------


def test_rewrite_note_reports_the_line_of_each_change(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    index = _build_filename_index(scan_vault(vault))

    new_text, changes = rewrite_note(_FOO, Path("personal/Projects/Foo.md"), index)

    # The `related:` item on line 5, the three links on line 11, the dead one
    # on line 12 — and nothing from the code spans below them.
    assert [change["line"] for change in changes] == [5, 11, 11, 11, 12]
    for change in changes:
        offset = change["offset"]
        assert new_text[offset : offset + len(change["to"])] == change["to"]


def test_rewrite_note_accepts_a_repo_relative_source_path(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    index = _build_filename_index(scan_vault(vault))

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
    assert read_receipt(runtime) is not None


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
    assert read_receipt(runtime) is not None, "a partial revert stays revertible"


def test_a_clean_revert_drops_the_receipt(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)

    unmigrate_links(vault, runtime, apply=True)

    assert read_receipt(runtime) is None
    assert migrate_links(vault, runtime)["rewrites"], "migration is available again"


def test_unmigrate_ignores_a_receipt_with_nothing_recorded(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    summary = unmigrate_vault_links(vault, {"rewrites": []}, apply=True)

    assert summary["skipped"]


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
        assert read_receipt(runtime) is None
        # But what did land is recorded, or it could never be taken back.
        recorded = peek_receipt(runtime)
        assert recorded is not None
        assert recorded["status"] == "partial"
        assert {item["path"] for item in recorded["rewrites"]} == {
            "Root.md",
            "personal/MEMORY.md",
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
    assert read_receipt(runtime)["status"] == "migrated", "the vault is done now"
    assert unmigrate_links(vault, runtime, apply=True)["restored"] == [
        "Root.md",
        "personal/MEMORY.md",
        "personal/Projects/Foo.md",
    ]
    assert _snapshot(vault) == before, "both batches restored, byte for byte"


def test_a_completed_receipt_is_downgraded_when_a_note_stops_being_readable(
    tmp_path: Path,
) -> None:
    """A forced pass that cannot even read a note has not verified it, so the
    vault stops counting as converted — while every entry already recorded is
    kept, because none of them was touched."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    first = read_receipt(runtime)
    unreadable = vault / "personal/MEMORY.md"
    unreadable.chmod(0o000)
    try:
        migrate_links(vault, runtime, apply=True, force=True)

        assert read_receipt(runtime) is None
        assert peek_receipt(runtime)["rewrites"] == first["rewrites"]
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
    receipt_path(runtime).write_text(json.dumps(legacy), encoding="utf-8")

    assert read_receipt(runtime) is not None
    assert migrate_links(vault, runtime, apply=True)["skipped"] == "already migrated"


# ---- receipt and rails -----------------------------------------------------


def test_the_receipt_records_the_reverse_map(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    migrate_links(vault, runtime, apply=True)

    receipt = read_receipt(runtime)
    assert receipt is not None
    assert receipt["schema_version"] == 1
    assert receipt["migrated_at"]
    assert receipt["files_rewritten"] == 3
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

    assert read_receipt(runtime) is None


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
    first = read_receipt(runtime)
    _note(vault, "personal/Notes/late.md", "---\ntype: note\n---\n# L\n\n[[People/Mo]]\n")

    migrate_links(vault, runtime, apply=True, force=True)

    kept = [
        path
        for path in receipt_path(runtime).parent.glob("vault-links.*.json")
        if path != receipt_path(runtime)
    ]
    assert len(kept) == 1
    assert json.loads(kept[0].read_text(encoding="utf-8"))["rewrites"] == first["rewrites"]
    merged = read_receipt(runtime)["rewrites"]
    assert "personal/Notes/late.md" in {item["path"] for item in merged}
    for item in first["rewrites"]:
        assert item in merged, "the first batch stays restorable"
    assert unmigrate_links(vault, runtime, apply=True)["restored"] == [
        "Root.md",
        "personal/MEMORY.md",
        "personal/Notes/late.md",
        "personal/Projects/Foo.md",
    ]


def test_a_note_rewritten_twice_keeps_only_the_live_entries(tmp_path: Path) -> None:
    """The carry-forward is per note, not wholesale. A note that gained a wikilink
    after the first pass was hand-edited, which already invalidated its recorded
    offsets — and one stale span disqualifies the *whole* file in the undo, so
    keeping them would take the second pass's good entries down too."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    root = vault / "Root.md"
    root.write_text(
        root.read_text(encoding="utf-8") + "\nLater: [[Ideas/Thing]].\n", encoding="utf-8"
    )

    migrate_links(vault, runtime, apply=True, force=True)

    entries = read_receipt(runtime)["rewrites"]
    assert [item["from"] for item in entries if item["path"] == "Root.md"] == [
        "[[Ideas/Thing]]"
    ]
    # Untouched notes keep every entry they had.
    assert {item["path"] for item in entries} >= {"personal/MEMORY.md", "personal/Projects/Foo.md"}
    # And the file that was rewritten twice still reverses cleanly for that pass.
    unmigrate_links(vault, runtime, apply=True)
    assert "[[Ideas/Thing]]" in root.read_text(encoding="utf-8")


def test_a_forced_rerun_with_nothing_to_do_keeps_the_reverse_map(tmp_path: Path) -> None:
    """Otherwise `--force` on an already-converted vault would swap a usable
    reverse map for an empty one, and leave the undo with nothing to undo."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    migrate_links(vault, runtime, apply=True)
    first = read_receipt(runtime)

    migrate_links(vault, runtime, apply=True, force=True)

    assert read_receipt(runtime) == first
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
    assert read_receipt(tmp_path / ".runtime")["git_head_before"] == "abc123"


def test_a_corrupt_receipt_does_not_block_the_migration(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    receipt_path(runtime).parent.mkdir(parents=True, exist_ok=True)
    receipt_path(runtime).write_text("{ not json", encoding="utf-8")

    summary = migrate_links(vault, runtime, apply=True)

    assert summary["rewrites"]
    assert read_receipt(runtime) is not None


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

    migrate_vault_links(vault, apply=True)

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
            ["vault-migrate-links", "--apply", "--vault-root", str(vault)]
        )
    finally:
        note.chmod(0o644)

    out = capsys.readouterr()
    assert code == 1
    assert "No wikilinks found" not in out.out
    assert "failed to write" in out.out
    assert "Permission denied" in out.err
