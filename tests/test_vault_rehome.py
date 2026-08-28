"""Re-homing person notes a global curation run filed in the wrong workspace.

Two things are being defended here. The first is restraint: only a note whose
tags already name another workspace may move, and the 53-note judgement backlog
this was measured against has to reach a review queue without a single file
changing place. The second is that a move takes the note's *edges* with it — in
both link dialects, and in the moved note's own outbound links — because a
migration that relocates 42 notes and breaks 70 links has made the vault worse.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.vault_rehome import (
    detect_misfiled_people,
    peek_receipt,
    plan_rehome,
    read_receipt,
    receipt_path,
    rehome_people,
    rehome_vault_people,
    resolve_role_workspaces,
    unrehome_people,
    unrehome_vault_people,
    vault_workspaces,
)


def _note(vault: Path, relative: str, body: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _person(tags: str) -> str:
    return f"---\ntype: person\ntags: {tags}\n---\n# Someone\n"


def _vault(tmp_path: Path) -> Path:
    """A two-workspace vault holding one of each bucket, in both link dialects."""
    vault = tmp_path / "memory-vault"
    # Work-tagged, sitting in personal/: the mechanical case.
    _note(
        vault,
        "personal/People/Mo.md",
        "---\n"
        "type: person\n"
        "tags: [person, colleague]\n"
        "related:\n"
        "  - work/projects/alpha\n"
        "---\n"
        "# Mo\n\n"
        "Runs [Alpha](../../work/projects/alpha.md).\n",
    )
    # No workspace-naming tag: judgement.
    _note(vault, "personal/People/Ida.md", _person("[person]"))
    # Personal-tagged and already personal: not a candidate at all.
    _note(vault, "personal/People/Alba.md", _person("[person, friend]"))
    # Inbound references, wikilink dialect and markdown dialect.
    _note(
        vault,
        "personal/Projects/Foo.md",
        "---\n"
        "type: project\n"
        "related:\n"
        "  - personal/People/Mo\n"
        "---\n"
        "# Foo\n\n"
        "Owner [[personal/People/Mo|Mo]], also [Mo](../People/Mo.md).\n"
        "Code: `[[personal/People/Mo]]`, escaped \\[[personal/People/Mo]].\n",
    )
    _note(
        vault,
        "work/projects/alpha.md",
        "---\ntype: project\n---\n# Alpha\n\n"
        "Lead [Mo](../../personal/People/Mo.md) / [[personal/People/Mo]]\n",
    )
    # Excluded from the index, the linter and the search — and so from this.
    _note(vault, "Logs/2026-01-01.md", "# Log\n\n[[personal/People/Mo]]\n")
    return vault


def _snapshot(vault: Path) -> dict[str, str]:
    return {
        path.relative_to(vault).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(vault.rglob("*.md"))
    }


def _paths(items: list[dict]) -> list[str]:
    return [item["path"] for item in items]


def _proposals(vault: Path, workspace: str) -> str:
    path = vault / workspace / "Workspace" / "Memory-Proposals.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# ---- bucketing -------------------------------------------------------------


def test_a_work_tagged_person_in_personal_is_mechanical(tmp_path: Path) -> None:
    """The tags already say where it belongs, so re-filing carries no judgement —
    the same bar `vault_migration` uses for applying an aliased type rename."""
    vault = _vault(tmp_path)

    candidates = {c.path: c for c in detect_misfiled_people(vault)}

    assert candidates["personal/People/Mo.md"].bucket == "mechanical"
    assert candidates["personal/People/Mo.md"].destination == "work/People/Mo.md"


def test_an_untagged_person_is_never_mechanical(tmp_path: Path) -> None:
    """53 of the 95 notes on the reference vault carry no workspace signal at all.
    Guessing a relationship from a filename is how a migration moves someone's
    family into the work vault."""
    vault = _vault(tmp_path)

    candidates = {c.path: c for c in detect_misfiled_people(vault)}

    assert candidates["personal/People/Ida.md"].bucket == "needs_judgement"
    assert candidates["personal/People/Ida.md"].reason == "no tag names a workspace"


def test_a_correctly_filed_person_is_not_a_candidate(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    assert "personal/People/Alba.md" not in [
        c.path for c in detect_misfiled_people(vault)
    ]


def test_conflicting_tags_go_to_judgement(tmp_path: Path) -> None:
    """`colleague` plus `friend` is a real question about a real person, not a
    tie for the migration to break."""
    vault = _vault(tmp_path)
    _note(vault, "personal/People/Sam.md", _person("[person, colleague, friend]"))

    candidate = next(
        c for c in detect_misfiled_people(vault) if c.path == "personal/People/Sam.md"
    )

    assert candidate.bucket == "needs_judgement"
    assert "both personal and work" in candidate.reason


def test_a_tag_naming_no_registered_workspace_is_not_a_candidate(tmp_path: Path) -> None:
    """With only one workspace there is nowhere to move to, so `colleague` is not
    a signal — inventing a `work/` directory is worse than leaving the note."""
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/People/Mo.md", _person("[person, colleague]"))

    assert detect_misfiled_people(vault) == []


def test_roles_bind_to_names_not_the_other_way_round() -> None:
    """Workspace names are the user's. An install with neither `personal` nor
    `work` must be inert rather than moving notes into a name it made up."""
    assert resolve_role_workspaces(["personal", "work"]) == {
        "personal": "personal",
        "work": "work",
    }
    assert resolve_role_workspaces(["Work", "home"]) == {
        "work": "Work",
        "personal": "home",
    }
    assert resolve_role_workspaces(["clientA", "clientB"]) == {}


def test_the_operators_own_note_is_never_a_candidate(tmp_path: Path) -> None:
    """`People/User.md` is the operator's identity note, named by memory_proposals
    as the canonical home for durable identity facts, so it must never be
    proposed for re-homing in any bucket. A generic accept would move it out of
    the primary workspace and break identity resolution."""
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/People/User.md", _person("[person]"))
    _note(vault, "personal/People/Peter.md", _person("[person]"))

    candidates = {c.path: c for c in detect_misfiled_people(vault)}

    assert "personal/People/User.md" not in candidates
    assert candidates["personal/People/Peter.md"].bucket == "needs_judgement"


def test_the_operator_note_exclusion_is_case_insensitive(tmp_path: Path) -> None:
    """A vault written on a case-insensitive filesystem can hold the note under any
    casing, so the exclusion casefolds rather than matching one spelling."""
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/People/user.md", _person("[person]"))
    _note(vault, "work/People/USER.md", _person("[person]"))

    assert detect_misfiled_people(vault) == []


def test_a_note_only_containing_user_in_its_name_still_moves(tmp_path: Path) -> None:
    """The exclusion is an exact filename match, not a substring match: a note
    about a real person whose name happens to contain the word is still a
    contact."""
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/People/User-Group-Lead.md", _person("[person, colleague]"))
    _note(vault, "work/projects/alpha.md", "---\ntype: project\n---\n# Alpha\n")

    candidates = {c.path: c for c in detect_misfiled_people(vault)}

    assert candidates["personal/People/User-Group-Lead.md"].bucket == "mechanical"


def test_note_type_folders_are_not_mistaken_for_workspaces(tmp_path: Path) -> None:
    """A legacy single-root vault has `People/` at the top level; treating that as
    a workspace would make every person note a candidate for moving into a
    directory named after a note type."""
    vault = tmp_path / "memory-vault"
    _note(vault, "People/Mo.md", _person("[person, colleague]"))
    _note(vault, "Logs/x.md", "# L\n")

    assert vault_workspaces(vault) == []
    assert detect_misfiled_people(vault) == []


# ---- the move and its edges ------------------------------------------------


def test_a_move_rewrites_inbound_wikilinks(tmp_path: Path) -> None:
    """A wikilink naming the old path resolves to nothing once the note moves, so
    the vault would lose the edge it was moved to make workspace-internal."""
    vault = _vault(tmp_path)

    rehome_vault_people(vault, apply=True)

    foo = (vault / "personal/Projects/Foo.md").read_text(encoding="utf-8")
    assert "[[work/People/Mo|Mo]]" in foo
    assert "[[personal/People/Mo|Mo]]" not in foo
    assert "[[work/People/Mo]]" in (
        vault / "work/projects/alpha.md"
    ).read_text(encoding="utf-8")


def test_a_move_rewrites_inbound_relative_markdown_links(tmp_path: Path) -> None:
    """A relative destination is measured from the linking note's directory, so
    both ends of every edge have to be recomputed — `../People/Mo.md` from
    `personal/Projects/` is `../../work/People/Mo.md` after the move."""
    vault = _vault(tmp_path)

    rehome_vault_people(vault, apply=True)

    assert "[Mo](../../work/People/Mo.md)" in (
        vault / "personal/Projects/Foo.md"
    ).read_text(encoding="utf-8")
    # Same target, different linking note, therefore a different relative path.
    assert "[Mo](../People/Mo.md)" in (
        vault / "work/projects/alpha.md"
    ).read_text(encoding="utf-8")


def test_the_moved_notes_own_outbound_links_are_recomputed(tmp_path: Path) -> None:
    """The failure that is easy to miss: nothing pointed *at* the moved note here,
    the note itself moved, and every relative path it holds is now measured from
    a different directory."""
    vault = _vault(tmp_path)

    rehome_vault_people(vault, apply=True)

    assert "[Alpha](../projects/alpha.md)" in (
        vault / "work/People/Mo.md"
    ).read_text(encoding="utf-8")


def test_frontmatter_related_refs_are_updated(tmp_path: Path) -> None:
    """`related:` is half the graph; a bare ref left naming the old path is a
    broken edge that `vault_lint` cannot even see."""
    vault = _vault(tmp_path)

    rehome_vault_people(vault, apply=True)

    assert "  - work/People/Mo\n" in (
        vault / "personal/Projects/Foo.md"
    ).read_text(encoding="utf-8")


def test_a_frontmatter_wikilink_ref_is_updated(tmp_path: Path) -> None:
    """A vault mid-conversion still has wikilinked frontmatter refs, and they are
    references rather than prose, so the path survives the rewrite."""
    vault = _vault(tmp_path)
    _note(
        vault,
        "work/journal/daily.md",
        "---\ntype: journal\npeople:\n  - \"[[personal/People/Mo|Mo]]\"\n---\n# Day\n",
    )

    rehome_vault_people(vault, apply=True)

    assert '- "[[work/People/Mo|Mo]]"' in (
        vault / "work/journal/daily.md"
    ).read_text(encoding="utf-8")


def test_code_spans_and_escapes_are_left_alone(tmp_path: Path) -> None:
    """A wikilink inside backticks is documentation *about* the syntax. The skip
    rules are imported rather than re-derived precisely so this module cannot
    develop a second opinion about what counts as code."""
    vault = _vault(tmp_path)

    rehome_vault_people(vault, apply=True)

    foo = (vault / "personal/Projects/Foo.md").read_text(encoding="utf-8")
    assert "`[[personal/People/Mo]]`" in foo
    assert "\\[[personal/People/Mo]]" in foo


def test_excluded_directories_are_not_touched(tmp_path: Path) -> None:
    """`Logs/` is outside the index, the linter and search; an archived chat
    transcript is a record of what was said, not an edge to maintain."""
    vault = _vault(tmp_path)

    rehome_vault_people(vault, apply=True)

    assert "[[personal/People/Mo]]" in (
        vault / "Logs/2026-01-01.md"
    ).read_text(encoding="utf-8")


def test_a_destination_collision_is_reported_not_merged(tmp_path: Path) -> None:
    """Two notes with the same filename in both trees is a content decision.
    Overwriting one of them would silently delete a note."""
    vault = _vault(tmp_path)
    _note(vault, "work/People/Mo.md", _person("[person, colleague]"))
    keep = (vault / "work/People/Mo.md").read_text(encoding="utf-8")

    summary = rehome_vault_people(vault, apply=True)

    assert _paths(summary["conflicts"]) == ["personal/People/Mo.md"]
    assert summary["moves"] == []
    assert (vault / "personal/People/Mo.md").is_file()
    assert (vault / "work/People/Mo.md").read_text(encoding="utf-8") == keep


def test_two_candidates_racing_for_one_destination_are_both_refused(tmp_path: Path) -> None:
    """`personal/People/Mo.md` and `home/People/Mo.md`, both tagged `colleague`,
    both resolve to `work/People/Mo.md`. Nothing is there yet, so the on-disk
    guard passes for *both*: letting them through meant the apply pass ran
    `replace()` twice and the second note ate the first, with two reported
    successes, an empty `failed`, and a receipt that would then unrehome the
    survivor's content onto the loser's path."""
    vault = _vault(tmp_path)
    _note(
        vault,
        "home/People/Mo.md",
        "---\ntype: person\ntags: [person, colleague]\n---\n# Mo\n\nThe other Mo.\n",
    )
    before = {
        path: (vault / path).read_text(encoding="utf-8")
        for path in ("personal/People/Mo.md", "home/People/Mo.md")
    }

    summary = rehome_vault_people(vault, apply=True)

    assert _paths(summary["conflicts"]) == ["home/People/Mo.md", "personal/People/Mo.md"]
    for conflict in summary["conflicts"]:
        assert "same destination" in conflict["error"]
    # Refused at plan time, so it is a conflict and not an apply failure.
    assert summary["moves"] == []
    assert summary["failed"] == []
    assert not (vault / "work/People/Mo.md").exists()
    for path, text in before.items():
        assert (vault / path).read_text(encoding="utf-8") == text


# ---- the review queue ------------------------------------------------------


def test_judgement_cases_are_proposed_but_not_moved(tmp_path: Path) -> None:
    """The whole point of the split: a note with no workspace signal reaches the
    promote/dismiss surface the user already reviews, and stays on disk exactly
    where it was."""
    vault = _vault(tmp_path)

    summary = rehome_vault_people(vault, apply=True)

    assert (vault / "personal/People/Ida.md").is_file()
    assert not (vault / "work/People/Ida.md").exists()
    queue = _proposals(vault, "personal")
    assert "personal/People/Ida.md" in queue
    assert "_(from: vault-rehome)_" in queue
    assert summary["proposals"]


def test_the_queue_lives_in_the_workspace_holding_the_note(tmp_path: Path) -> None:
    """`ciao memory-proposal-dismiss` removes an entry in a *workspace's* queue,
    and the curator who can answer "is this person work or personal" is the one
    reading that workspace."""
    vault = _vault(tmp_path)

    rehome_vault_people(vault, apply=True)

    assert _proposals(vault, "personal")
    assert not _proposals(vault, "work")


def test_the_queue_is_not_re_appended_on_a_second_run(tmp_path: Path) -> None:
    """A weekly routine appending the same 53 entries every week turns the review
    queue into a place nobody reads."""
    vault = _vault(tmp_path)
    rehome_vault_people(vault, apply=True)
    first = _proposals(vault, "personal")

    rehome_vault_people(vault, apply=True)

    assert _proposals(vault, "personal") == first


# ---- dry run ---------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """Not the moves, not the link rewrites, and not the review queue: a preview
    that files 53 proposals is not a preview."""
    vault = _vault(tmp_path)
    before = _snapshot(vault)

    summary = rehome_vault_people(vault)

    assert summary["applied"] is False
    assert summary["moves"], "the dry run still has to report the plan"
    assert summary["rewrites"], "including the references it would rewrite"
    assert _snapshot(vault) == before


def test_a_missing_vault_is_reported_not_created(tmp_path: Path) -> None:
    summary = rehome_vault_people(tmp_path / "nope")

    assert summary["skipped"]
    assert not (tmp_path / "nope").exists()


def test_a_second_run_finds_nothing_left_to_do(tmp_path: Path) -> None:
    """Idempotency comes from the state on disk, not a flag: the note is no longer
    under the old workspace, so it is no longer a candidate."""
    vault = _vault(tmp_path)
    rehome_vault_people(vault, apply=True)
    after_first = _snapshot(vault)

    second = rehome_vault_people(vault, apply=True)

    assert second["moves"] == []
    assert second["rewrites"] == []
    assert _snapshot(vault) == after_first


# ---- round trip ------------------------------------------------------------


def test_round_trip_is_byte_identical(tmp_path: Path) -> None:
    """Rewriting a user's notes is only defensible if it is exactly undoable —
    which is why the receipt is a reverse map and not a description."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    before = _snapshot(vault)

    rehome_people(vault, runtime, apply=True)
    unrehome_people(vault, runtime, apply=True)

    after = _snapshot(vault)
    # The review queue is the user's to resolve and is deliberately not withdrawn.
    after.pop("personal/Workspace/Memory-Proposals.md", None)
    assert after == before


def test_unrehome_is_a_dry_run_by_default(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    rehome_people(vault, runtime, apply=True)
    migrated = _snapshot(vault)

    summary = unrehome_people(vault, runtime)

    assert summary["moves_reverted"]
    assert _snapshot(vault) == migrated
    assert read_receipt(runtime) is not None


def test_unrehome_needs_the_receipt(tmp_path: Path) -> None:
    """Re-deriving "which people used to be filed personally" from tags would drag
    back notes the user filed by hand in the meantime."""
    vault = _vault(tmp_path)

    summary = unrehome_people(vault, tmp_path / ".runtime", apply=True)

    assert summary["skipped"] == "no migration receipt to reverse"


def test_unrehome_verifies_each_span_and_is_all_or_nothing(tmp_path: Path) -> None:
    """A note edited after the migration must come back whole or not at all: a
    file half in one dialect and half in the other is worse than a skipped one."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    rehome_people(vault, runtime, apply=True)
    foo = vault / "personal/Projects/Foo.md"
    edited = foo.read_text(encoding="utf-8").replace("[[work/People/Mo|Mo]]", "[[Nobody]]")
    foo.write_text(edited, encoding="utf-8")

    summary = unrehome_people(vault, runtime, apply=True)

    assert "personal/Projects/Foo.md" not in summary["restored"]
    assert any("changed since rehoming" in item["error"] for item in summary["failed"])
    # Untouched, including the spans that *did* still match.
    assert foo.read_text(encoding="utf-8") == edited
    # Another file's restore is unaffected — the unit is the file, not the run.
    assert "work/projects/alpha.md" in summary["restored"]


def test_a_note_whose_text_could_not_be_restored_is_not_moved_back(tmp_path: Path) -> None:
    """Moving it back while its own links still point at the new location would
    leave a broken note in the original place and nothing to detect it."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    rehome_people(vault, runtime, apply=True)
    moved = vault / "work/People/Mo.md"
    moved.write_text(
        moved.read_text(encoding="utf-8").replace("../projects/alpha.md", "./nope.md"),
        encoding="utf-8",
    )

    summary = unrehome_people(vault, runtime, apply=True)

    assert summary["moves_reverted"] == []
    assert moved.is_file()
    assert not (vault / "personal/People/Mo.md").exists()


def test_the_receipt_is_kept_when_the_revert_was_partial(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    rehome_people(vault, runtime, apply=True)
    foo = vault / "personal/Projects/Foo.md"
    foo.write_text("---\ntype: project\n---\n# Foo\n", encoding="utf-8")

    unrehome_people(vault, runtime, apply=True)

    assert read_receipt(runtime) is not None, "a partial revert has to stay revertible"


# ---- receipt and rails -----------------------------------------------------


def test_the_receipt_records_the_reverse_map(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    rehome_people(vault, runtime, apply=True)

    receipt = read_receipt(runtime)
    assert receipt is not None
    assert receipt["schema_version"] == 1
    assert receipt["rehomed_at"]
    assert receipt["moves"] == [
        {"from": "personal/People/Mo.md", "to": "work/People/Mo.md"}
    ]
    assert {"path", "line", "offset", "from", "to"} <= set(receipt["rewrites"][0])
    # Spans are recorded against the path the note ends up at, because that is
    # where unrehoming reads them back from.
    assert "work/People/Mo.md" in {item["path"] for item in receipt["rewrites"]}
    assert receipt["needs_judgement"]
    assert not list(receipt_path(runtime).parent.glob("*.tmp")), (
        "the receipt is written through a .tmp sibling and replaced"
    )
    assert json.loads(receipt_path(runtime).read_text(encoding="utf-8"))["moves"]


def test_no_receipt_is_written_by_a_dry_run(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    rehome_people(vault, runtime)

    assert read_receipt(runtime) is None


def test_the_receipt_gates_a_second_run(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    rehome_people(vault, runtime, apply=True)

    summary = rehome_people(vault, runtime, apply=True)

    assert summary["skipped"] == "already migrated"


def test_force_adds_to_the_reverse_map_instead_of_replacing_it(tmp_path: Path) -> None:
    """A second pass must not narrow what can be undone. It shifts only the text
    of the notes *it* rewrites and moves only the notes *it* moves, so every other
    entry stays exactly true and is carried into the new receipt; the earlier file
    is still archived under its own name."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    before = _snapshot(vault)
    rehome_people(vault, runtime, apply=True)
    first = read_receipt(runtime)
    _note(vault, "personal/People/Late.md", _person("[person, customer]"))

    rehome_people(vault, runtime, apply=True, force=True)

    kept = [
        path
        for path in receipt_path(runtime).parent.glob("vault-rehome.*.json")
        if path != receipt_path(runtime)
    ]
    assert len(kept) == 1
    assert json.loads(kept[0].read_text(encoding="utf-8"))["moves"] == first["moves"]
    merged = read_receipt(runtime)
    assert merged["moves"] == [
        {"from": "personal/People/Mo.md", "to": "work/People/Mo.md"},
        {"from": "personal/People/Late.md", "to": "work/People/Late.md"},
    ], "the first batch stays reversible"
    for entry in first["rewrites"]:
        assert entry in merged["rewrites"]

    unrehome_people(vault, runtime, apply=True)
    after = _snapshot(vault)
    after.pop("personal/Workspace/Memory-Proposals.md", None)
    after.pop("personal/People/Late.md", None)
    assert after == before, "both batches restored, byte for byte"


def test_a_forced_rerun_with_nothing_to_do_keeps_the_reverse_map(tmp_path: Path) -> None:
    """Otherwise `--force` on an already-rehomed vault would swap a usable reverse
    map for an empty one, and leave the undo with nothing to undo."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    rehome_people(vault, runtime, apply=True)
    first = read_receipt(runtime)

    rehome_people(vault, runtime, apply=True, force=True)

    assert read_receipt(runtime) == first
    assert unrehome_people(vault, runtime)["moves_reverted"]


# ---- a run that could not finish -------------------------------------------


def _block_moves(vault: Path) -> Path:
    """Make the move fail the way a permission or quota problem does.

    The destination workspace is left unwritable, so the link rewrites all land
    and only `source.replace(destination)` fails — the shape that matters,
    because it is the one that leaves work on disk with nothing recording it.
    """
    work = vault / "work"
    work.chmod(0o555)
    return work


def test_a_partial_run_is_not_recorded_as_complete(tmp_path: Path) -> None:
    """One note that could not be moved means the vault is not re-homed. A receipt
    saying otherwise made the migration stop short of done and report that it had
    finished: the next normal run was refused as "already migrated" while the note
    it never moved stayed misfiled — with every reference to it already repointed
    at a path it is not at, so the vault was left worse than before the run."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    blocked = _block_moves(vault)
    try:
        summary = rehome_people(vault, runtime, apply=True)

        assert _paths(summary["failed"]) == ["personal/People/Mo.md"]
        assert summary["moves"] == []
        assert summary["complete"] is False
        assert (vault / "personal/People/Mo.md").is_file(), "still misfiled"
        # No *completed* receipt, so nothing reads the vault as re-homed.
        assert read_receipt(runtime) is None
        # But what did land is recorded, or it could never be taken back.
        recorded = peek_receipt(runtime)
        assert recorded is not None
        assert recorded["status"] == "partial"
        assert recorded["moves"] == []
        assert "work/People/Mo.md" in (vault / "personal/Projects/Foo.md").read_text(
            encoding="utf-8"
        ), "the references were repointed even though the move failed"
        assert {entry["path"] for entry in recorded["rewrites"]} == {
            "personal/Projects/Foo.md",
            "work/projects/alpha.md",
        }
        # The note's own links were recomputed for a directory it never reached,
        # so that one write is taken back rather than recorded.
        assert "](../../work/projects/alpha.md)" in (
            vault / "personal/People/Mo.md"
        ).read_text(encoding="utf-8")
        # And the retry is a plain re-run, not something that needs --force.
        assert "skipped" not in rehome_people(vault, runtime, apply=True)
        # That retry failed the same way and wrote nothing new, so it is not
        # recorded: re-running a failing migration must not bury the one usable
        # reverse map under a pile of timestamped copies of itself.
        assert [
            path
            for path in receipt_path(runtime).parent.glob("vault-rehome.*.json")
            if path != receipt_path(runtime)
        ] == []
    finally:
        blocked.chmod(0o755)


def test_a_partial_run_is_undoable_on_its_own(tmp_path: Path) -> None:
    """A run that could not finish still has to be an exact inverse of what it did
    write, or the receipt it now leaves behind is no better than the missing one.
    Two ways that failed: the spans of the note whose move failed were keyed to
    the destination it never reached, so the undo reported a mismatch it invented,
    and the completed-only reader refused the receipt outright."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    before = _snapshot(vault)
    blocked = _block_moves(vault)
    try:
        rehome_people(vault, runtime, apply=True)
    finally:
        blocked.chmod(0o755)

    summary = unrehome_people(vault, runtime, apply=True)

    assert summary["failed"] == []
    assert summary["restored"] == [
        "personal/Projects/Foo.md",
        "work/projects/alpha.md",
    ]
    after = _snapshot(vault)
    after.pop("personal/Workspace/Memory-Proposals.md", None)
    assert after == before


def test_a_retry_after_a_partial_run_can_undo_both_batches(tmp_path: Path) -> None:
    """The reverse map has to stay usable across retries. Rotating it away on the
    second pass left the references the first pass rewrote with nothing to restore
    them — the retry moves the note and records only that, so the undo put the note
    back and left every link pointing at the workspace it no longer lives in."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    before = _snapshot(vault)
    blocked = _block_moves(vault)
    try:
        first = rehome_people(vault, runtime, apply=True)
    finally:
        blocked.chmod(0o755)
    assert first["failed"], "the fixture has to actually fail the first move"

    retry = rehome_people(vault, runtime, apply=True)

    assert "skipped" not in retry, "a partial run must not gate its own retry"
    assert retry["complete"] is True
    assert retry["moves"] == [{"from": "personal/People/Mo.md", "to": "work/People/Mo.md"}]
    assert read_receipt(runtime)["status"] == "migrated", "the vault is done now"
    # The first pass's rewrites survive, re-keyed to where the retry's move put
    # the note: a move does not touch a byte, so the offsets are still exact.
    assert {entry["path"] for entry in read_receipt(runtime)["rewrites"]} == {
        "work/People/Mo.md",
        "personal/Projects/Foo.md",
        "work/projects/alpha.md",
    }

    unrehome_people(vault, runtime, apply=True)

    after = _snapshot(vault)
    after.pop("personal/Workspace/Memory-Proposals.md", None)
    assert after == before, "both batches restored, byte for byte"


def test_a_rewrite_that_cannot_be_taken_back_is_still_mapped_to_the_real_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Reverting the own-link rewrite of a failed move is the preferred outcome,
    but it is a write and writes fail. When it does, the spans must be re-keyed to
    where the note actually sits: pointing them at the destination it never reached
    would leave the one file whose links disagree with its location unrestorable."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    before = _snapshot(vault)
    real = Path.write_text

    def refuse_the_rollback(self, data, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name == "Mo.md" and "../../work/projects/alpha.md" in data:
            raise OSError("Read-only file system")
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse_the_rollback)
    blocked = _block_moves(vault)
    try:
        summary = rehome_people(vault, runtime, apply=True)
    finally:
        blocked.chmod(0o755)
        monkeypatch.undo()

    assert any(
        "its own links were left rewritten" in item["error"]
        for item in summary["failed"]
    ), "a note whose links disagree with its location needs a human, so it is reported"
    assert "personal/People/Mo.md" in {
        entry["path"] for entry in peek_receipt(runtime)["rewrites"]
    }

    unrehome_people(vault, runtime, apply=True)

    after = _snapshot(vault)
    after.pop("personal/Workspace/Memory-Proposals.md", None)
    assert after == before, "the degraded branch is still an exact inverse"


def test_a_partial_receipt_never_reads_as_a_re_homed_vault(tmp_path: Path) -> None:
    """The accessor split, stated on its own: the completed-only reader is what
    every "has this vault been re-homed?" surface must ask, and the raw one is for
    the undo and the carry-forward, which need the map whatever its status."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    blocked = _block_moves(vault)
    try:
        rehome_people(vault, runtime, apply=True)
    finally:
        blocked.chmod(0o755)

    assert read_receipt(runtime) is None
    assert peek_receipt(runtime)["status"] == "partial"


def test_a_receipt_written_before_status_existed_still_reads_as_complete(
    tmp_path: Path,
) -> None:
    """Installs that did the work before the field existed must not have their
    migration re-run, and must still be able to reverse it — the completed-only
    reader used to answer None for those, which took the undo away from exactly
    the installs holding the largest reverse maps."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    rehome_people(vault, runtime, apply=True)
    legacy = json.loads(receipt_path(runtime).read_text(encoding="utf-8"))
    del legacy["status"]
    receipt_path(runtime).write_text(json.dumps(legacy), encoding="utf-8")

    assert read_receipt(runtime) is not None
    assert rehome_people(vault, runtime, apply=True)["skipped"] == "already migrated"
    assert unrehome_people(vault, runtime, apply=True)["moves_reverted"]


def test_a_dirty_vault_is_refused_without_force(tmp_path: Path, monkeypatch) -> None:
    """`git checkout` has to stay a working undo, which it is not once the moves
    are mixed into edits the user had not committed."""
    vault = _vault(tmp_path)
    monkeypatch.setattr(
        "ciao.vault_rehome.vault_git_state",
        lambda root, touched=(): {
            "is_repo": True,
            "head": "abc123",
            "dirty": True,
            "dirty_paths": ["personal/People/Mo.md"],
        },
    )

    refused = rehome_people(vault, tmp_path / ".runtime", apply=True)
    forced = rehome_people(vault, tmp_path / ".runtime", apply=True, force=True)

    assert refused["skipped"] == "vault has uncommitted changes"
    assert forced["moves"]
    assert read_receipt(tmp_path / ".runtime")["git_head_before"] == "abc123"


def test_a_dry_run_is_never_blocked_by_a_refusal(tmp_path: Path, monkeypatch) -> None:
    """Both refusals protect a *write*. Gating the preview meant the only way to
    see what would happen to a dirty vault was to pass the flag that skips the
    checks — exactly backwards."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    rehome_people(vault, runtime, apply=True)  # leaves a receipt
    monkeypatch.setattr(
        "ciao.vault_rehome.vault_git_state",
        lambda root, touched=(): {"is_repo": True, "head": "abc123", "dirty": True},
    )
    _note(vault, "personal/People/Late.md", _person("[person, customer]"))

    summary = rehome_people(vault, runtime)

    assert "skipped" not in summary
    assert _paths(summary["mechanical"]) == ["personal/People/Late.md"]


def test_the_inverse_is_never_gated_on_a_dirty_vault(tmp_path: Path, monkeypatch) -> None:
    """The re-homing is what makes the vault dirty, so gating the reverse on
    cleanliness made recovery impossible in exactly the state it exists for."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    rehome_people(vault, runtime, apply=True)
    monkeypatch.setattr(
        "ciao.vault_rehome.vault_git_state",
        lambda root, touched=(): {"is_repo": True, "head": "abc123", "dirty": True},
    )

    summary = unrehome_people(vault, runtime, apply=True)

    assert "skipped" not in summary
    assert summary["moves_reverted"]
    assert (vault / "personal/People/Mo.md").is_file()


def test_the_dirty_check_only_looks_at_files_the_plan_would_touch(
    tmp_path: Path, monkeypatch
) -> None:
    """An untracked chat transcript under `Logs/` once blocked an unrelated
    migration. The rail keeps `git checkout` an undo for the *rewritten* files;
    anything else being dirty is none of its business."""
    vault = _vault(tmp_path)
    porcelain = ""  # rebound below; the fake reports whatever it currently holds

    def fake_run_git(root: Path, *args: str) -> tuple[int, str]:
        if args == ("rev-parse", "--is-inside-work-tree"):
            return 0, "true\n"
        if args[0] == "rev-parse":
            return 0, "abc123\n"
        return 0, porcelain

    monkeypatch.setattr("ciao.vault_rehome._run_git", fake_run_git)
    monkeypatch.setattr("ciao.vault_rehome.shutil.which", lambda name: "/usr/bin/git")

    # Dirt in a note the plan *does* rewrite refuses...
    porcelain = " M personal/Projects/Foo.md\n"
    blocked = rehome_people(vault, tmp_path / "other-runtime", apply=True)
    # ...an untracked log folder and an edit to a note the plan never opens do not.
    porcelain = "?? Logs/2026-01-02/\n M personal/People/Alba.md\n"
    unrelated = rehome_people(vault, tmp_path / ".runtime", apply=True)

    assert "skipped" not in unrelated
    assert unrelated["moves"]
    assert blocked["skipped"] == "vault has uncommitted changes"
    assert blocked["git"]["dirty_paths"] == ["personal/Projects/Foo.md"]


def test_a_corrupt_receipt_does_not_block_the_migration(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    receipt_path(runtime).parent.mkdir(parents=True, exist_ok=True)
    receipt_path(runtime).write_text("{ not json", encoding="utf-8")

    summary = rehome_people(vault, runtime, apply=True)

    assert summary["moves"]
    assert read_receipt(runtime) is not None


def test_unrehome_ignores_a_receipt_with_nothing_recorded(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    summary = unrehome_vault_people(vault, {"moves": [], "rewrites": []}, apply=True)

    assert summary["skipped"]


def test_the_plan_names_only_the_files_it_would_write(tmp_path: Path) -> None:
    """The git rail is scoped to this set, so a file appearing here that the
    migration does not actually rewrite would resurrect the whole-subtree bug."""
    vault = _vault(tmp_path)

    plan = plan_rehome(vault)

    assert sorted(plan["files"]) == [
        "personal/People/Mo.md",
        "personal/Projects/Foo.md",
        "work/projects/alpha.md",
    ]


# ---- only an applied run writes a receipt ----------------------------------


def test_a_dry_run_writes_no_receipt(tmp_path: Path) -> None:
    """The receipt is the record of an APPLIED migration, and the whole detection
    side now reads its mere presence as "the re-home ran". A preview that left a
    file behind would tell every surface the work was done."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    before = _snapshot(vault)

    rehome_people(vault, runtime, apply=False)

    assert not receipt_path(runtime).exists()
    assert read_receipt(runtime) is None
    assert _snapshot(vault) == before


def test_an_applied_run_writes_a_migrated_receipt(tmp_path: Path) -> None:
    """`migrated` is the only status the receipt can carry, so `read_receipt`
    and `peek_receipt` agree on every receipt this code writes."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"

    rehome_people(vault, runtime, apply=True)

    payload = json.loads(receipt_path(runtime).read_text(encoding="utf-8"))
    assert payload["status"] == "migrated"
    assert read_receipt(runtime) is not None
    assert peek_receipt(runtime) == payload


# -- an existing linked counterpart settles the question ----------------------
#
# A person can genuinely be both — a friend who is also a colleague — and the
# vault's answer for that is two notes, one per workspace, cross-linked. Tags
# naming two workspaces is the one case the tag rules refuse to decide, so
# without this the second half of a deliberate split sat in the review queue
# permanently, offering to move a note on top of its own counterpart.


def _counterpart_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    # Friend AND colleague, with the work half already filed and cross-linked.
    _note(
        vault,
        "personal/People/Oliver.md",
        "---\n"
        "type: person\n"
        "aliases:\n"
        "  - Oliver Akermann\n"
        "tags: [person, friend, colleague]\n"
        "related:\n"
        '  - "work/People/Oliver-Akermann"\n'
        '  - "People/Sara"\n'
        "---\n"
        "# Oliver Akermann\n",
    )
    _note(vault, "work/People/Oliver-Akermann.md", _person("[person, colleague]"))
    _note(vault, "personal/People/Sara.md", _person("[person, friend]"))
    return vault


def test_a_linked_counterpart_takes_the_note_out_of_the_queue(tmp_path: Path) -> None:
    vault = _counterpart_vault(tmp_path)

    paths = [c.path for c in detect_misfiled_people(vault)]

    assert "personal/People/Oliver.md" not in paths


def test_the_counterpart_must_be_named_by_an_alias_not_by_name_shape(tmp_path: Path) -> None:
    """Two people who share a name are not one person.

    Measured on a real vault: `personal/People/Ipek.md` (Raffa's partner) and
    `work/People/Ipek-Kahraman-Scandit.md` (a Scandit colleague), whose note says
    "the name collision in the vault is intentional — do not merge". A rule that
    matched a longer stem extending a shorter one would have silently dropped a
    genuine queue row on the strength of a shared first name.
    """
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "personal/People/Ipek.md",
        "---\n"
        "type: person\n"
        "tags: [person, friend, colleague]\n"
        "related:\n"
        '  - "work/People/Ipek-Kahraman-Scandit"\n'
        "---\n"
        "# Ipek\n",
    )
    _note(vault, "work/People/Ipek-Kahraman-Scandit.md", _person("[person, colleague]"))

    paths = [c.path for c in detect_misfiled_people(vault)]

    assert "personal/People/Ipek.md" in paths


def test_a_link_to_someone_else_over_there_is_not_a_counterpart(tmp_path: Path) -> None:
    """Oliver's own note links to David Blazevic. Linking *into* the other
    workspace is not the same as having a note *of yourself* there."""
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "personal/People/Nadia.md",
        "---\n"
        "type: person\n"
        "tags: [person, friend, colleague]\n"
        "related:\n"
        '  - "work/People/David-Blazevic"\n'
        "---\n"
        "# Nadia\n",
    )
    _note(vault, "work/People/David-Blazevic.md", _person("[person, colleague]"))

    paths = [c.path for c in detect_misfiled_people(vault)]

    assert "personal/People/Nadia.md" in paths


def test_the_counterpart_rule_also_covers_an_untagged_note(tmp_path: Path) -> None:
    """The untagged bucket proposed moving the note to its counterpart's
    workspace — i.e. on top of the note it is already linked to."""
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "personal/People/Rui.md",
        "---\n"
        "type: person\n"
        "tags: [person]\n"
        "related:\n"
        '  - "work/People/Rui"\n'
        "---\n"
        "# Rui\n",
    )
    _note(vault, "work/People/Rui.md", _person("[person, colleague]"))

    paths = [c.path for c in detect_misfiled_people(vault)]

    assert "personal/People/Rui.md" not in paths


def test_an_inline_related_list_is_read_too(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "personal/People/Bea.md",
        "---\n"
        "type: person\n"
        "tags: [person, friend, colleague]\n"
        'related: ["work/People/Bea", "People/Sara"]\n'
        "---\n"
        "# Bea\n",
    )
    _note(vault, "work/People/Bea.md", _person("[person, colleague]"))

    paths = [c.path for c in detect_misfiled_people(vault)]

    assert "personal/People/Bea.md" not in paths


def test_a_mutual_link_identifies_one_person_when_the_names_cannot(tmp_path: Path) -> None:
    """`Ipek` and `Ipek-Kahraman-Scandit` are one person — she joined the company
    — but the work note carries a disambiguating suffix no real alias contains.
    Both notes naming each other is an identity claim coincidence cannot produce.
    """
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "personal/People/Ipek.md",
        "---\n"
        "type: person\n"
        "tags: [person]\n"
        "related:\n"
        '  - "work/People/Ipek-Kahraman-Scandit"\n'
        "---\n"
        "# Ipek\n",
    )
    _note(
        vault,
        "work/People/Ipek-Kahraman-Scandit.md",
        "---\n"
        "type: person\n"
        "tags: [person, scandit]\n"
        "related:\n"
        '  - "personal/People/Ipek"\n'
        "---\n"
        "# Ipek Kahraman\n",
    )

    paths = [c.path for c in detect_misfiled_people(vault)]

    assert "personal/People/Ipek.md" not in paths
    assert "work/People/Ipek-Kahraman-Scandit.md" not in paths


def test_one_sided_link_between_different_people_is_still_queued(tmp_path: Path) -> None:
    """The disambiguation case: a note may point at a same-named stranger to say
    "not this one". Only a link BACK makes it an identity claim."""
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "personal/People/Ipek.md",
        "---\n"
        "type: person\n"
        "tags: [person]\n"
        "related:\n"
        '  - "work/People/Ipek-Kahraman-Scandit"\n'
        "---\n"
        "# Ipek\n",
    )
    _note(vault, "work/People/Ipek-Kahraman-Scandit.md", _person("[person, scandit]"))

    paths = [c.path for c in detect_misfiled_people(vault)]

    assert "personal/People/Ipek.md" in paths


def test_the_legacy_unprefixed_form_counts_as_a_link_back(tmp_path: Path) -> None:
    """Pre-migration notes name the other half unprefixed (`People/Oliver`), and
    after the split that can only mean the other root."""
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "personal/People/Vik.md",
        "---\n"
        "type: person\n"
        "tags: [person]\n"
        "related:\n"
        '  - "work/People/Vik-Long-Suffix"\n'
        "---\n"
        "# Vik\n",
    )
    _note(
        vault,
        "work/People/Vik-Long-Suffix.md",
        "---\n"
        "type: person\n"
        "tags: [person, colleague]\n"
        "related:\n"
        '  - "People/Vik"\n'
        "---\n"
        "# Vik\n",
    )

    paths = [c.path for c in detect_misfiled_people(vault)]

    assert "personal/People/Vik.md" not in paths


def test_a_link_back_to_a_different_person_is_not_a_link_back(tmp_path: Path) -> None:
    """The far note has plenty of `related` refs into this workspace that are not
    this note. "Points back at me" has to mean me."""
    vault = tmp_path / "memory-vault"
    _note(
        vault,
        "personal/People/Ada.md",
        "---\n"
        "type: person\n"
        "tags: [person]\n"
        "related:\n"
        '  - "work/People/Ada-Long-Suffix"\n'
        "---\n"
        "# Ada\n",
    )
    _note(
        vault,
        "work/People/Ada-Long-Suffix.md",
        "---\n"
        "type: person\n"
        "tags: [person, colleague]\n"
        "related:\n"
        '  - "personal/People/Someone-Else"\n'
        "---\n"
        "# Ada Long\n",
    )
    _note(vault, "personal/People/Someone-Else.md", _person("[person, friend]"))

    paths = [c.path for c in detect_misfiled_people(vault)]

    assert "personal/People/Ada.md" in paths


def test_the_cli_does_not_claim_a_clean_vault_when_every_move_failed(
    tmp_path: Path, capsys
) -> None:
    """"No tag-obvious misfiled people" was a lie, and the receipt went unmentioned.

    `mechanical` names what the run FOUND; `moves` names what it managed to
    move. With the destination unwritable, `moves` is empty — and the CLI printed
    the clean-vault line on stdout while stderr listed the failures for the very
    notes it had just found. The receipt line was keyed on `moves` too, so a
    receipt was written and never mentioned.
    """
    from ciao import cli

    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    destination = vault / "work" / "People"
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o555)
    try:
        code = cli.main([
            "vault-rehome", "--apply",
            "--vault-root", str(vault),
            "--runtime-root", str(runtime),
            "--workspace-name", "personal",
            "--workspace-name", "work",
        ])
    finally:
        destination.chmod(0o755)

    out = capsys.readouterr()
    assert code == 1
    assert "No tag-obvious misfiled people" not in out.out
    assert "Moved nothing" in out.out
    assert "did NOT finish" in out.out


def test_a_table_cells_escaped_alias_pipe_survives_a_move(tmp_path: Path) -> None:
    """A repointed wikilink has to keep the pipe spelling it arrived with.

    Inside a table cell the alias pipe is `\\|`, because a bare one closes the
    cell. Re-emitting the repointed link with a bare `|` would break the row —
    and this went unnoticed while `_parse_wikilink` read the backslash as part
    of the ref, because resolution then failed and the link was skipped
    entirely: a roster table's edges were silently left pointing at the old
    path.
    """
    vault = _vault(tmp_path)
    _note(
        vault,
        "personal/Projects/Roster.md",
        "---\ntype: project\n---\n"
        "# Roster\n\n"
        "| Person | Role |\n|---|---|\n"
        "| [[personal/People/Mo\\|Mo]] | Lead |\n",
    )

    rehome_vault_people(vault, apply=True)

    text = (vault / "personal/Projects/Roster.md").read_text(encoding="utf-8")
    # Repointed at the new path, and still one cell rather than two.
    assert "| [[work/People/Mo\\|Mo]] | Lead |" in text
    assert "[[personal/People/Mo" not in text
