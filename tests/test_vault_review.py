from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import ciao.vault_review as review

from ciao.fts_search import EXCLUDED_VAULT_DIRS
from ciao.vault_index import scan_vault
from ciao.vault_lint import run_validation
from ciao.vault_review import (
    content_hash,
    delete_permanently,
    generate_candidates,
    read_ledger,
    record_decision,
    restore_note,
    trash_note,
)


def _note(root: Path, name: str, body: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: note\n---\n" + body, encoding="utf-8")


def test_candidates_are_deterministic_and_explain_orphans(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "An unlinked note.")
    candidates = generate_candidates(tmp_path, workspace="personal")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.path == "memory-vault/People/A.md"
    assert "unlinked" in candidate.signals
    assert candidate.evidence["backlinks"] == []
    assert candidate.candidate_id
    assert generate_candidates(tmp_path, workspace="personal")[0].candidate_id == candidate.candidate_id


def test_keep_is_hash_scoped_and_trash_restore_is_exact(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal")[0]

    trashed = trash_note(tmp_path, candidate)
    assert trashed["original_path"] == candidate.path
    assert not (tmp_path / "People/A.md").exists()
    assert (tmp_path / "Workspace" / ".vault-trash" / f"{candidate.candidate_id}.md").is_file()

    restored = restore_note(tmp_path, candidate.candidate_id)
    assert restored["content_hash"] == candidate.content_hash
    assert (tmp_path / "People/A.md").read_text(encoding="utf-8").endswith("An unlinked note.")
    assert read_ledger(tmp_path)[-1]["disposition"] == "restore"


def test_permanent_delete_requires_trash_and_exact_confirmation(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal")[0]
    trash_note(tmp_path, candidate)
    with pytest.raises(ValueError, match="confirmation"):
        delete_permanently(tmp_path, candidate.candidate_id, confirm="wrong")
    delete_permanently(tmp_path, candidate.candidate_id, confirm=candidate.candidate_id)
    assert not list((tmp_path / "Workspace" / ".vault-trash").glob("*"))
    assert json.loads((tmp_path / "Workspace" / "Vault-Review.jsonl").read_text().splitlines()[-1])["status"] == "deleted"


def test_restore_keeps_backlinks_until_permanent_delete(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "The canonical note.")
    _note(tmp_path, "People/B.md", "See [A](A.md).")
    candidates = generate_candidates(tmp_path, workspace="personal", max_candidates=50)
    candidate = next(item for item in candidates if item.path.endswith("/A.md"))

    trash_note(tmp_path, candidate)
    assert "See [A](A.md)." in (tmp_path / "People" / "B.md").read_text(encoding="utf-8")
    restore_note(tmp_path, candidate.candidate_id)
    assert "See [A](A.md)." in (tmp_path / "People" / "B.md").read_text(encoding="utf-8")

    trash_note(tmp_path, candidate)
    delete_permanently(tmp_path, candidate.candidate_id, confirm=candidate.candidate_id)
    assert "See [A](A.md)." not in (tmp_path / "People" / "B.md").read_text(encoding="utf-8")


def test_trashed_notes_leave_the_index_and_the_graph(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal")[0]
    trash_note(tmp_path, candidate)

    trashed = tmp_path / "Workspace" / ".vault-trash" / f"{candidate.candidate_id}.md"
    assert trashed.is_file()
    # Still on disk so restore works, but no longer part of the vault: a
    # "trashed" note that stayed searchable and drawn in the Memory Map under
    # an opaque hash filename is a note the user cannot find or remove.
    assert scan_vault(tmp_path, workspace="personal") == []
    assert ".vault-trash" in EXCLUDED_VAULT_DIRS


def test_the_queue_projection_is_not_itself_a_note(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "An unlinked note.")
    generate_candidates(tmp_path, workspace="personal")
    queue = tmp_path / "Workspace" / "Vault-Review.md"
    assert queue.is_file()

    scanned = {str(entry.path) for entry in scan_vault(tmp_path, workspace="personal")}
    assert "memory-vault/Workspace/Vault-Review.md" not in scanned
    assert "Workspace/Vault-Review.md" not in run_validation(tmp_path).get("orphans", [])


def test_a_read_only_listing_does_not_write_the_queue(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "An unlinked note.")
    generate_candidates(tmp_path, workspace="personal", write_queue=False)
    assert not (tmp_path / "Workspace" / "Vault-Review.md").exists()


def test_candidate_generation_serializes_workspace_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _note(tmp_path, "People/A.md", "An unlinked note.")
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    original = review._generate_candidates

    def blocked(*args: object, **kwargs: object):
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            started.set()
            assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(review, "_generate_candidates", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(generate_candidates, tmp_path, workspace="personal")
        assert started.wait(2)
        second = pool.submit(generate_candidates, tmp_path, workspace="personal")
        assert not second.done()
        release.set()
        first.result()
        second.result()


def test_archive_is_not_an_accepted_disposition(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal")[0]
    # Nothing archived a note, so accepting the word only suppressed the
    # candidate for good while the note stayed exactly where it was.
    with pytest.raises(ValueError, match="unsupported"):
        record_decision(tmp_path, candidate, "archive")
    assert read_ledger(tmp_path) == []


def test_a_hub_note_does_not_outrank_an_orphan(tmp_path: Path) -> None:
    _note(tmp_path, "People/Orphan.md", "Nothing points here.")
    for name in ("W", "X", "Y", "Z"):
        _note(tmp_path, f"People/{name}.md", f"Note {name}.")
    (tmp_path / "People" / "Hub.md").write_text(
        "---\ntype: note\nrelated: [People/W, People/X, People/Y, People/Z]\n---\nA hub.",
        encoding="utf-8",
    )

    by_path = {item.path: item for item in generate_candidates(tmp_path, workspace="personal", max_candidates=50)}
    hub = by_path["memory-vault/People/Hub.md"]
    orphan = by_path["memory-vault/People/Orphan.md"]
    assert hub.evidence["bridge"] is True
    assert hub.priority < orphan.priority


def test_permanent_delete_keeps_backlinks_when_the_folder_is_read_only(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "The canonical note.")
    _note(tmp_path, "People/B.md", "See [A](A.md).")
    candidates = generate_candidates(tmp_path, workspace="personal", max_candidates=50)
    candidate = next(item for item in candidates if item.path.endswith("/A.md"))
    trash_note(tmp_path, candidate)

    folder = tmp_path / "People"
    original_mode = folder.stat().st_mode
    folder.chmod(0o500)
    try:
        with pytest.raises(ValueError, match="cannot delete"):
            delete_permanently(tmp_path, candidate.candidate_id, confirm=candidate.candidate_id)
    finally:
        folder.chmod(original_mode)

    # The note is still recoverable AND every link to it still resolves: the
    # deletion has to succeed before any backlink is rewritten, because those
    # rewrites are what no rollback can undo.
    assert "See [A](A.md)." in (tmp_path / "People" / "B.md").read_text(encoding="utf-8")
    assert (tmp_path / "Workspace" / ".vault-trash" / f"{candidate.candidate_id}.md").is_file()
    assert read_ledger(tmp_path)[-1]["disposition"] == "trash"


def test_permanent_delete_restores_backlinks_when_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _note(tmp_path, "People/A.md", "The canonical note.")
    _note(tmp_path, "People/B.md", "See [A](A.md).")
    candidate = next(
        item for item in generate_candidates(tmp_path, workspace="personal", max_candidates=50)
        if item.path.endswith("/A.md")
    )
    trash_note(tmp_path, candidate)

    def fail_audit(*args: object, **kwargs: object) -> None:
        raise OSError("ledger is read-only")

    monkeypatch.setattr("ciao.vault_review._append", fail_audit)
    with pytest.raises(ValueError, match="audit failed"):
        delete_permanently(tmp_path, candidate.candidate_id, confirm=candidate.candidate_id)

    assert "See [A](A.md)." in (tmp_path / "People" / "B.md").read_text(encoding="utf-8")
    assert (tmp_path / "Workspace" / ".vault-trash" / f"{candidate.candidate_id}.md").is_file()


def test_list_trashed_is_scoped_and_tracks_restore(tmp_path: Path) -> None:
    _note(tmp_path, "People/A.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal")[0]

    assert review.list_trashed(tmp_path, workspace="personal") == []

    trash_note(tmp_path, candidate)
    (listed,) = review.list_trashed(tmp_path, workspace="personal")
    assert listed["candidate_id"] == candidate.candidate_id
    assert listed["original_path"] == candidate.path
    assert listed["content_hash"] == candidate.content_hash
    assert listed["trashed_at"]
    # Another workspace's trash view stays empty.
    assert review.list_trashed(tmp_path, workspace="work") == []

    restore_note(tmp_path, candidate.candidate_id)
    assert review.list_trashed(tmp_path, workspace="personal") == []


def test_list_trashed_skips_malformed_sidecars(tmp_path: Path) -> None:
    trash = tmp_path / "Workspace" / ".vault-trash"
    trash.mkdir(parents=True, exist_ok=True)
    (trash / "not-a-candidate.json").write_text("{}", encoding="utf-8")
    (trash / "0123456789abcdef01234567.json").write_text("not json", encoding="utf-8")
    assert review.list_trashed(tmp_path, workspace="personal") == []


def test_an_earlier_unattended_turn_does_not_block_a_later_attended_trash(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal

    _note(tmp_path, "People/A.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal")[0]

    # Turn 7 is in flight (`user_turn_count` is already bumped past it); turn 3
    # ran unattended long ago. Only turn 7 decides whether this call is
    # attended — reading the highest key instead meant one scheduled turn
    # refused every later attended trash in the chat, forever.
    chat = SimpleNamespace(user_turn_count=8, user_turn_unattended={"3": True, "7": True})
    plane = CiaoControlPlane(
        SimpleNamespace(workspace=lambda name: object(), workspace_vault_root=lambda name: tmp_path),
        project_chat_manager=SimpleNamespace(get_chat=lambda chat_id: chat),
        schedule_manager=SimpleNamespace(),
    )
    principal = McpPrincipal(
        token_id="token-1", chat_id="chat-1", project_id="project-1",
        workspace="personal", provider="opencode",
    )

    with pytest.raises(ControlPlaneError) as unattended:
        plane.vault_review(principal, "trash", candidate_id=candidate.candidate_id)
    assert unattended.value.code == "unattended_forbidden"

    chat.user_turn_unattended.pop("7")
    assert plane.vault_review(principal, "trash", candidate_id=candidate.candidate_id)["ok"]
    assert not (tmp_path / "People" / "A.md").exists()


def test_lookup_notes_need_more_than_unlinked_to_be_offered_for_retirement(tmp_path: Path) -> None:
    """Nothing links to a person note by design, so `unlinked` alone is not a finding.

    One real vault had 41 of its 50 candidates as `People/*.md` flagged by that
    signal on its own, in a queue whose terminal action is deletion.
    """
    (tmp_path / "People").mkdir(parents=True)
    (tmp_path / "People" / "Quiet.md").write_text(
        "---\ntype: person\ntags: [person]\nupdated: 2026-01-01\n---\n# Quiet\n\nA colleague.",
        encoding="utf-8",
    )
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False) == []

    # A second, independent signal still surfaces the same note.
    (tmp_path / "People" / "Quiet.md").write_text(
        "---\ntype: person\ntags: [person]\nupdated: 2026-01-01\n---\n# Quiet\n\nSuperseded by someone else.",
        encoding="utf-8",
    )
    candidates = generate_candidates(tmp_path, workspace="personal", write_queue=False)
    assert [c.path for c in candidates] == ["memory-vault/People/Quiet.md"]
    assert set(candidates[0].signals) == {"unlinked", "superseded_language"}


def test_templates_are_never_retirement_candidates(tmp_path: Path) -> None:
    (tmp_path / "journal").mkdir(parents=True)
    (tmp_path / "journal" / "_template.md").write_text(
        "---\ntype: note\n---\n# Daily\n\nReplaced by the day's entry.", encoding="utf-8"
    )
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False) == []


def test_link_fixed_clears_the_row_until_the_note_changes(tmp_path: Path) -> None:
    """`improve_link` used to write a ledger row and leave the row in place."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    record_decision(tmp_path, candidate, disposition="improve_link")
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False) == []

    # Editing the note re-raises it, exactly as `keep` does.
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note, now rewritten.")
    again = generate_candidates(tmp_path, workspace="personal", write_queue=False)
    assert [c.path for c in again] == ["memory-vault/Ideas/Loose.md"]


def test_candidates_carry_an_excerpt_for_the_row(tmp_path: Path) -> None:
    _note(tmp_path, "Ideas/Loose.md", "# Loose\n\nThe first line of prose.\n\n- and a bullet")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    excerpt = candidate.evidence["excerpt"]
    # Frontmatter and the H1 are already on the row; the prose is what is new.
    assert excerpt.startswith("The first line of prose.")
    assert "---" not in excerpt and "# Loose" not in excerpt
    assert len(excerpt) <= review.EXCERPT_CHARS + 2


def test_long_excerpts_stop_on_a_word_boundary(tmp_path: Path) -> None:
    _note(tmp_path, "Ideas/Long.md", "word " * 400)
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    excerpt = candidate.evidence["excerpt"]
    assert excerpt.endswith(" …")
    assert len(excerpt) <= review.EXCERPT_CHARS + 2


def test_excerpt_is_plain_text_not_markdown(tmp_path: Path) -> None:
    """The row renders the excerpt as text, so the syntax is noise in it."""
    _note(
        tmp_path,
        "Ideas/Loose.md",
        "# Loose\n\nBuilt with [Slidev](https://sli.dev) and `npm`.\n\n"
        "## Methodology\n\n- **Source**: BigQuery\n",
    )
    excerpt = generate_candidates(
        tmp_path, workspace="personal", write_queue=False
    )[0].evidence["excerpt"]
    assert excerpt == "Built with Slidev and npm. Methodology Source: BigQuery"


def test_superseded_language_must_be_about_this_note(tmp_path: Path) -> None:
    """Mentioning supersession is not the same as announcing your own.

    The decision archive whose job is to record superseded decisions was the
    clearest casualty: a hub, so it arrived ranked most disposable of all.
    """
    (tmp_path / "projects").mkdir(parents=True)
    (tmp_path / "projects" / "Decisions.md").write_text(
        "---\ntype: project\ntags: [project]\nupdated: 2026-01-01\n---\n"
        "# Decisions\n\nThe live decision log.\n\n"
        "## Archive\n\nSuperseded decisions are kept here; each was replaced by a newer one.\n",
        encoding="utf-8",
    )
    # The same words at the top, where a note speaks about itself, still count.
    (tmp_path / "projects" / "Old.md").write_text(
        "---\ntype: project\ntags: [project]\nupdated: 2026-01-01\n"
        "description: Superseded by the new plan.\n---\n# Old\n\nNothing here.\n",
        encoding="utf-8",
    )
    # Both are unlinked in a two-note vault; only one claims to be superseded.
    signals = {
        c.path: set(c.signals)
        for c in generate_candidates(tmp_path, workspace="personal", write_queue=False)
    }
    assert "superseded_language" not in signals["memory-vault/projects/Decisions.md"]
    assert "superseded_language" in signals["memory-vault/projects/Old.md"]


def test_an_active_status_outranks_supersession_wording(tmp_path: Path) -> None:
    (tmp_path / "projects").mkdir(parents=True)
    (tmp_path / "projects" / "Wedding.md").write_text(
        "---\ntype: project\nstatus: active\ntags: [project]\nupdated: 2026-01-01\n---\n"
        "# Wedding\n\nThe guest table moved to `guests.csv`.\n",
        encoding="utf-8",
    )
    # Still unlinked, as the only note in its vault — but not superseded.
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    assert candidate.signals == ("unlinked",)


def test_journal_entries_are_never_superseded(tmp_path: Path) -> None:
    """Nothing replaces a given day, and a log's prose is full of the wording."""
    (tmp_path / "journal" / "daily").mkdir(parents=True)
    (tmp_path / "journal" / "daily" / "2026-07-24.md").write_text(
        "---\ntags: [daily-log]\ndate: 2026-07-24\n---\n"
        "# 2026-07-24 (Friday)\n\nOOO today. All open items moved to Monday.\n",
        encoding="utf-8",
    )
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False) == []


def test_defer_is_no_longer_a_disposition(tmp_path: Path) -> None:
    """Snoozing is gone: an untouched row already stays and asks again."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    with pytest.raises(ValueError):
        record_decision(tmp_path, candidate, disposition="defer")
    assert "defer" not in review.DISPOSITIONS
    # Leaving it alone keeps it in the queue, which is what Later approximated.
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False)


def test_keep_is_permanent_until_the_note_is_edited(tmp_path: Path) -> None:
    """The distinction Later used to blur: keep is not "ask me next time"."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    record_decision(tmp_path, candidate, disposition="keep")
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False) == []

    _note(tmp_path, "Ideas/Loose.md", "An unlinked note, edited.")
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False)


def test_still_true_stamps_the_note_as_verified_today(tmp_path: Path) -> None:
    """The button said it re-verified a note; it used to only silence the row."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    note = tmp_path / "Ideas" / "Loose.md"
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    row = record_decision(
        tmp_path, candidate, disposition="keep", now=datetime(2026, 9, 18, tzinfo=UTC)
    )
    text = note.read_text(encoding="utf-8")
    assert "updated: 2026-09-18" in text
    assert text.endswith("An unlinked note.")  # body untouched

    # The ledger names the note as it now stands, so the row it silences is the
    # stamped one — not a version that no longer exists on disk.
    assert row["content_hash"] == content_hash(note.read_bytes())
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False) == []


def test_still_true_replaces_an_existing_updated_date(tmp_path: Path) -> None:
    note = tmp_path / "Ideas" / "Dated.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: note\nupdated: 2020-01-01\ntags: [idea]\n---\n# Dated\n\nBody.\n",
        encoding="utf-8",
    )
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    record_decision(
        tmp_path, candidate, disposition="keep", now=datetime(2026, 9, 18, tzinfo=UTC)
    )
    text = note.read_text(encoding="utf-8")
    assert "updated: 2026-09-18" in text
    assert "2020-01-01" not in text
    # Neighbouring keys and their order survive byte for byte.
    assert "type: note\nupdated: 2026-09-18\ntags: [idea]" in text


def test_still_true_leaves_a_note_without_frontmatter_alone(tmp_path: Path) -> None:
    """Verifying a note may clear the row; it may not restructure the file."""
    note = tmp_path / "Ideas" / "Bare.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Bare\n\nNo frontmatter here.\n", encoding="utf-8")
    candidates = generate_candidates(tmp_path, workspace="personal", write_queue=False)
    if not candidates:  # a note with no frontmatter may not be scanned at all
        return
    record_decision(tmp_path, candidates[0], disposition="keep")
    assert note.read_text(encoding="utf-8") == "# Bare\n\nNo frontmatter here.\n"


def test_still_true_refuses_a_note_that_changed_under_it(tmp_path: Path) -> None:
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    _note(tmp_path, "Ideas/Loose.md", "Edited since the queue was built.")
    with pytest.raises(ValueError):
        record_decision(tmp_path, candidate, disposition="keep")


def test_improve_link_does_not_claim_a_verification(tmp_path: Path) -> None:
    """Re-linking a note elsewhere says nothing about whether its facts hold."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    note = tmp_path / "Ideas" / "Loose.md"
    before = note.read_text(encoding="utf-8")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    record_decision(tmp_path, candidate, disposition="improve_link")
    assert note.read_text(encoding="utf-8") == before


# ── release review fixes ─────────────────────────────────────────────────────


def test_keep_preserves_the_note_file_mode(tmp_path: Path) -> None:
    """A temp file lands at 0600; os.replace would tighten the note silently.

    `vault_index.apply_edits` carries the old mode over for exactly this
    reason. On a vault shared over a group-readable mount, or synced by
    another user or daemon, one click of "Still true" otherwise makes the
    note unreadable to it.
    """
    import os
    import stat

    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    note = tmp_path / "Ideas" / "Loose.md"
    os.chmod(note, 0o644)
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    result = record_decision(tmp_path, candidate, disposition="keep")

    assert result["stamped"] is True
    assert stat.S_IMODE(note.stat().st_mode) == 0o644


def test_keep_leaves_no_stray_temp_file_name_in_the_vault(tmp_path: Path) -> None:
    """The temp file is hidden and suffixed, so a crash leaves an obvious artifact."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    record_decision(tmp_path, candidate, disposition="keep")

    leftovers = [p.name for p in (tmp_path / "Ideas").iterdir()]
    assert leftovers == ["Loose.md"]


def test_keep_reports_when_there_was_no_frontmatter_to_stamp(
    tmp_path: Path,
) -> None:
    """A note with no frontmatter is the archetypal weak_provenance candidate.

    The row still clears — the user said the note is fine — but the caller must
    not be told a date was written, or the button promises a stamp that
    `memory-audit` and the Memory Map badge go on contradicting.
    """
    path = tmp_path / "Ideas" / "loose.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Loose thought\n\nSomething I wrote once.\n", encoding="utf-8")
    before = path.read_bytes()

    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    assert "weak_provenance" in candidate.signals

    result = record_decision(tmp_path, candidate, disposition="keep")

    assert result["stamped"] is False
    assert path.read_bytes() == before


def test_reverify_refuses_to_rewrite_a_note_that_is_not_utf8(tmp_path: Path) -> None:
    """errors="replace" on a read is fine; writing the result back is not.

    Driven through `_reverify` directly: `scan_vault` never yields a note it
    cannot decode, so this guard is defensive — but the rewrite it prevents
    would replace every undecodable byte with U+FFFD in the user's own file.
    """
    path = tmp_path / "Ideas" / "latin.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "---\ntype: note\n---\nCaf\xe9 notes\n".encode("latin-1")
    path.write_bytes(raw)

    candidate = review.ReviewCandidate(
        candidate_id="x" * 24,
        workspace="personal",
        path="memory-vault/Ideas/latin.md",
        content_hash=content_hash(raw),
        signals=["weak_provenance"],
        evidence={},
        priority=0.0,
    )

    digest, status = review._reverify(tmp_path, candidate, "2026-09-18")

    assert status == "not_stampable"
    assert digest == candidate.content_hash
    assert path.read_bytes() == raw  # not rewritten with U+FFFD


def test_keep_preserves_crlf_line_endings(tmp_path: Path) -> None:
    """The contract is that every other byte survives."""
    path = tmp_path / "Ideas" / "crlf.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"---\r\ntype: note\r\ntitle: X\r\n---\r\nBody\r\n")

    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    record_decision(tmp_path, candidate, disposition="keep")

    body = path.read_bytes()
    assert b"updated:" in body
    assert b"\n" not in body.replace(b"\r\n", b"")  # no bare LF anywhere


def test_keep_returns_the_candidate_id_the_caller_asked_about(
    tmp_path: Path,
) -> None:
    """`candidate_id` is recomputed from the post-stamp hash, so it changes."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    result = record_decision(tmp_path, candidate, disposition="keep")

    assert result["previous_candidate_id"] == candidate.candidate_id
    assert result["candidate_id"] != candidate.candidate_id


def test_lookup_type_filter_survives_a_capitalised_type(tmp_path: Path) -> None:
    """The filter guards a queue whose terminal action is deletion.

    `type: Person` must not slip past it on spelling alone — that is the
    41-of-50 `People/*.md` case the filter exists for.
    """
    path = tmp_path / "People" / "A.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Provenance present, so `unlinked` is the sole signal — which is exactly
    # the case the lookup-type filter is supposed to drop.
    path.write_text(
        "---\ntype: Person\nupdated: 2026-09-18\ntags: [people]\n---\n"
        "An unlinked note.\n",
        encoding="utf-8",
    )

    assert generate_candidates(tmp_path, workspace="personal", write_queue=False) == []


def test_record_type_filter_follows_type_aliases(tmp_path: Path) -> None:
    """`hackathon-log` aliases to journal, which is never superseded."""
    from ciao.vault_index import canonical_type

    if canonical_type("hackathon-log") != "journal":
        pytest.skip("alias not configured in this vault vocabulary")
    path = tmp_path / "Journal" / "2026-07-24.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: hackathon-log\nupdated: 2026-07-24\ntags: [a]\n---\n"
        "All open items moved to Monday.\n",
        encoding="utf-8",
    )

    candidates = generate_candidates(tmp_path, workspace="personal", write_queue=False)
    assert all("superseded_language" not in c.signals for c in candidates)


def test_keep_reports_a_note_that_was_already_verified_today(
    tmp_path: Path,
) -> None:
    """"Already current" is a success, not the half-working case.

    Collapsing it into the same `stamped: False` as "no frontmatter" would make
    the UI warn about a note that is perfectly stamped.
    """
    today = datetime.now(UTC).date().isoformat()
    path = tmp_path / "Ideas" / "fresh.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: note\nupdated: {today}\n---\nAn unlinked note.\n",
        encoding="utf-8",
    )
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    result = record_decision(tmp_path, candidate, disposition="keep")

    assert result["stamp_status"] == "already_current"
    assert result["stamped"] is False


def test_keep_distinguishes_nothing_to_stamp(tmp_path: Path) -> None:
    """The case the UI must speak up about."""
    path = tmp_path / "Ideas" / "bare.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Bare\n\nNo frontmatter here.\n", encoding="utf-8")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    result = record_decision(tmp_path, candidate, disposition="keep")

    assert result["stamp_status"] == "not_stampable"
    assert result["stamped"] is False


def test_an_orphaned_analysis_note_is_still_queued(tmp_path: Path) -> None:
    """Alias resolution must not widen the lookup-type exemption.

    `analysis` aliases to `reference`, which is in `_LOOKUP_TYPES` — but the
    exemption's rationale ("nothing links to a person or a bookmark as a matter
    of course") does not hold for an analysis document, and dropping it would
    silently remove a real finding from the queue.
    """
    path = tmp_path / "Resources" / "Q3 analysis.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: analysis\nupdated: 2026-09-18\ntags: [q3]\n---\nOrphaned.\n",
        encoding="utf-8",
    )

    candidates = generate_candidates(tmp_path, workspace="personal", write_queue=False)
    assert [c.path for c in candidates] == ["memory-vault/Resources/Q3 analysis.md"]
    assert list(candidates[0].signals) == ["unlinked"]


def test_keep_survives_a_very_long_note_filename(tmp_path: Path) -> None:
    """The temp prefix is truncated, so it cannot push past NAME_MAX.

    `record_decision` does not catch OSError and the MCP layer only catches
    ValueError, so an ENAMETOOLONG here surfaced as an unhandled exception.
    """
    stem = "x" * 180
    path = tmp_path / "Ideas" / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: note\n---\nAn unlinked note.\n", encoding="utf-8")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    result = record_decision(tmp_path, candidate, disposition="keep")

    assert result["stamp_status"] == "stamped"
    assert "updated:" in path.read_text(encoding="utf-8")
