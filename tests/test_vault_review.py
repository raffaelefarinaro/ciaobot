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


def test_improve_link_is_no_longer_a_disposition(tmp_path: Path) -> None:
    """Nobody re-links a note by hand, so the claim had no honest caller."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    with pytest.raises(ValueError):
        record_decision(tmp_path, candidate, disposition="improve_link")
    assert "improve_link" not in review.DISPOSITIONS
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False)


def test_historical_improve_link_rows_still_suppress(tmp_path: Path) -> None:
    """A row cleared before the button was removed must not come back."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    review._append(
        tmp_path,
        {
            "candidate_id": candidate.candidate_id,
            "workspace": candidate.workspace,
            "path": candidate.path,
            "content_hash": candidate.content_hash,
            "disposition": "improve_link",
            "actor": "user",
            "status": "reviewed",
            "deferred_until": "",
        },
    )
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


def test_reopen_puts_a_kept_note_back_in_the_queue(tmp_path: Path) -> None:
    """`keep` was the one irreversible act in a reversible workflow."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    result = record_decision(tmp_path, candidate, disposition="keep")
    assert generate_candidates(tmp_path, workspace="personal", write_queue=False) == []

    # The stamp changed the note, so the live candidate id is the post-stamp one.
    cleared = review.list_cleared(tmp_path, workspace="personal")
    assert [item["path"] for item in cleared] == ["memory-vault/Ideas/Loose.md"]
    assert cleared[0]["candidate_id"] == result["candidate_id"]

    review.reopen_note(tmp_path, cleared[0]["candidate_id"], workspace="personal")
    again = generate_candidates(tmp_path, workspace="personal", write_queue=False)
    assert [c.path for c in again] == ["memory-vault/Ideas/Loose.md"]
    # And it leaves the audit trail intact: kept, then reopened.
    assert [r["disposition"] for r in review.read_ledger(tmp_path)] == ["keep", "reopen"]


def test_reopen_refuses_anything_that_was_not_kept(tmp_path: Path) -> None:
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    review.trash_note(tmp_path, candidate)
    with pytest.raises(ValueError):
        review.reopen_note(tmp_path, candidate.candidate_id, workspace="personal")
    with pytest.raises(ValueError):
        review.reopen_note(tmp_path, "0" * 24, workspace="personal")


def test_cleared_list_drops_notes_that_left_or_changed(tmp_path: Path) -> None:
    """A note already back in the queue must not also offer to be added back."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    record_decision(tmp_path, candidate, disposition="keep")
    assert len(review.list_cleared(tmp_path, workspace="personal")) == 1

    _note(tmp_path, "Ideas/Loose.md", "Edited, so it is queued again on its own.")
    assert review.list_cleared(tmp_path, workspace="personal") == []

    (tmp_path / "Ideas" / "Loose.md").unlink()
    assert review.list_cleared(tmp_path, workspace="personal") == []


def test_a_note_deleted_outside_the_workflow_is_recorded(tmp_path: Path) -> None:
    """The ledger claims to record what left the vault; it used to miss this."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    record_decision(tmp_path, candidate, disposition="keep")

    (tmp_path / "Ideas" / "Loose.md").unlink()
    generate_candidates(tmp_path, workspace="personal", write_queue=True)
    rows = [r for r in review.read_ledger(tmp_path) if r["disposition"] == "vanished"]
    assert [r["path"] for r in rows] == ["memory-vault/Ideas/Loose.md"]
    assert rows[0]["actor"] == "system"

    # Once, however often the nightly pass runs afterwards.
    generate_candidates(tmp_path, workspace="personal", write_queue=True)
    assert len([r for r in review.read_ledger(tmp_path) if r["disposition"] == "vanished"]) == 1


def test_a_read_only_listing_never_writes_a_vanished_row(tmp_path: Path) -> None:
    """A listing that appends to the ledger is not the listing it claims to be."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    record_decision(tmp_path, candidate, disposition="keep")
    (tmp_path / "Ideas" / "Loose.md").unlink()

    generate_candidates(tmp_path, workspace="personal", write_queue=False)
    assert not [r for r in review.read_ledger(tmp_path) if r["disposition"] == "vanished"]


def test_a_vanished_note_that_comes_back_is_judged_again(tmp_path: Path) -> None:
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]
    review.trash_note(tmp_path, candidate)
    # Trash already says where it went, so no `vanished` row is added for it.
    generate_candidates(tmp_path, workspace="personal", write_queue=True)
    assert not [r for r in review.read_ledger(tmp_path) if r["disposition"] == "vanished"]

    # Recreated with different bytes, so it is a different note to the ledger
    # and gets judged on its own. Restoring byte-identical content keeps the
    # trash row's suppression, which is the same content-hash contract `keep`
    # has — the UI's Restore appends its own row and does not rely on this.
    _note(tmp_path, "Ideas/Loose.md", "Written again, from scratch.")
    again = generate_candidates(tmp_path, workspace="personal", write_queue=True)
    assert [c.path for c in again] == ["memory-vault/Ideas/Loose.md"]


def test_the_queue_projection_names_its_real_source(tmp_path: Path) -> None:
    """It said "generated from the append-only ledger"; it is a vault scan."""
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    generate_candidates(tmp_path, workspace="personal", write_queue=True)
    text = review.queue_path(tmp_path).read_text(encoding="utf-8")
    assert "from a scan of the vault at" in text
    assert "generated from the append-only ledger" not in text


def test_no_retention_window_is_claimed_in_code(tmp_path: Path) -> None:
    """The constant promised a purge that never existed; the trash is kept."""
    assert not hasattr(review, "RETENTION_DAYS")


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

    assert status == "not_utf8"
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
    # Pinned: computing `today` here and letting `record_decision` recompute it
    # makes the test fail on a run that straddles UTC midnight.
    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    today = now.date().isoformat()
    path = tmp_path / "Ideas" / "fresh.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: note\nupdated: {today}\n---\nAn unlinked note.\n",
        encoding="utf-8",
    )
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    result = record_decision(tmp_path, candidate, disposition="keep", now=now)

    assert result["stamp_status"] == "already_current"
    assert result["stamped"] is False


def test_keep_distinguishes_nothing_to_stamp(tmp_path: Path) -> None:
    """The case the UI must speak up about."""
    path = tmp_path / "Ideas" / "bare.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Bare\n\nNo frontmatter here.\n", encoding="utf-8")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    result = record_decision(tmp_path, candidate, disposition="keep")

    assert result["stamp_status"] == "no_frontmatter"
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
    # 246, not some round number: the old prefix built a temp basename of
    # 1 + len(name) + 1 + 8 + 4 chars, so a 180-char stem came to 197 — well
    # under NAME_MAX (255) and passing with or without the fix. 246 makes the
    # old form 263 and the note filename itself still legal at 249.
    stem = "x" * 246
    path = tmp_path / "Ideas" / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: note\n---\nAn unlinked note.\n", encoding="utf-8")
    candidate = generate_candidates(tmp_path, workspace="personal", write_queue=False)[0]

    result = record_decision(tmp_path, candidate, disposition="keep")

    assert result["stamp_status"] == "stamped"
    assert "updated:" in path.read_text(encoding="utf-8")


def test_temp_prefix_bounds_a_multibyte_name_by_bytes() -> None:
    """NAME_MAX is a byte limit, so the temp prefix has to be cut in bytes.

    `test_keep_survives_a_very_long_note_filename` above uses an ASCII stem,
    where one character is one byte and a character-count truncation happens to
    work. A mostly non-ASCII name is the case that slipped through: 60 emoji
    plus ".md" is 243 bytes but only 63 characters, so the 64-*character* cut
    passed the whole name through and the decorated temp name still came to
    1 + 243 + 1 + 8 + 4 = 257 bytes — ENAMETOOLONG, the exact failure the
    truncation exists to remove, and one `record_decision` does not catch.

    Asserted on the prefix rather than by writing the file, because macOS
    (APFS) bounds a filename by *characters* and accepts all 257 bytes; only a
    byte-bounded filesystem such as ext4 — CI's — refuses it. A test that wrote
    the note would pass here for the wrong reason.
    """
    from ciao.vault_index import temp_prefix

    name = "\N{EARTH GLOBE EUROPE-AFRICA}" * 60 + ".md"
    assert len(name) == 63  # a 64-character cut keeps all of it...
    assert len(name.encode("utf-8")) == 243  # ...and all 243 of its bytes.

    # `NamedTemporaryFile`/`mkstemp` decorate the prefix with 8 random
    # characters and the suffix; the whole basename must fit NAME_MAX.
    decorated = len(temp_prefix(name).encode("utf-8")) + 8 + len(".tmp")

    assert decorated <= 255, f"decorated temp name is {decorated} bytes"


def test_temp_prefix_never_splits_a_character() -> None:
    """The byte cut must not leave half a character in a filename.

    Slicing the UTF-8 bytes can land mid-character; decoding that back with the
    default strict handler raises, and with errors="replace" it would write a
    U+FFFD into the name. 200 is not a multiple of 3, so a name of 3-byte
    characters (HIRAGANA LETTER A) lands the cut inside one.
    """
    from ciao.vault_index import TEMP_PREFIX_NAME_BYTES, temp_prefix

    prefix = temp_prefix("\N{HIRAGANA LETTER A}" * 80)

    assert len(prefix.encode("utf-8")) <= TEMP_PREFIX_NAME_BYTES + 2
    assert "\ufffd" not in prefix
    assert prefix.startswith(".") and prefix.endswith(".")
    # 66 whole characters is 198 bytes; the 2 bytes left in the budget are the
    # head of the 67th and are dropped rather than decoded.
    assert prefix == "." + "\N{HIRAGANA LETTER A}" * 66 + "."


def test_stamp_updated_names_every_outcome_from_one_parse() -> None:
    """One scan of the frontmatter decides both the rewrite and the status.

    `_reverify` used to ask a separate `_already_current` helper whether the
    note was already stamped, which re-implemented this scan and made the
    "already today" branch here unreachable from the only caller. Two copies of
    a parse that must agree, with one of them uncovered, is a divergence
    waiting to happen; these cases pin the single helper's whole contract.
    """
    today = "2026-09-18"

    assert review._stamp_updated("---\ntype: note\n---\nBody.\n", today) == (
        f"---\ntype: note\nupdated: {today}\n---\nBody.\n",
        "stamped",
    )
    assert review._stamp_updated(
        "---\nupdated: 2020-01-01\n---\nBody.\n", today
    ) == (f"---\nupdated: {today}\n---\nBody.\n", "stamped")
    # Quoted, and the quotes are stripped before the comparison — the rule the
    # two copies had to agree on.
    assert review._stamp_updated(f'---\nupdated: "{today}"\n---\nBody.\n', today) == (
        None,
        "already_current",
    )
    assert review._stamp_updated(f"---\nupdated: {today}\n---\nBody.\n", today) == (
        None,
        "already_current",
    )
    assert review._stamp_updated("# Bare\n\nNo frontmatter.\n", today) == (
        None,
        "no_frontmatter",
    )
    # Opened and never closed.
    assert review._stamp_updated("---\ntype: note\nBody.\n", today) == (
        None,
        "no_frontmatter",
    )


def test_keep_stamps_a_bom_prefixed_note(tmp_path: Path) -> None:
    """A BOM is not whitespace, so a BOM + "---" line is not seen as "---".

    The note has perfectly good frontmatter; reading it as unstampable told the
    user to add frontmatter it already had. `_FRONTMATTER_RE` already tolerates
    a BOM, so these paths have to agree with it.

    Driven through `_reverify`: `scan_vault` yields no entry at all for a
    BOM-prefixed note, so the queue never reaches one today. The guard is
    defensive; the bug it prevents is a false instruction to the user.
    """
    path = tmp_path / "Ideas" / "bom.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\ufeff---\ntype: note\nupdated: 2020-01-01\n---\nBody.\n", encoding="utf-8"
    )
    raw = path.read_bytes()
    candidate = review.ReviewCandidate(
        candidate_id="b" * 24,
        workspace="personal",
        path="memory-vault/Ideas/bom.md",
        content_hash=content_hash(raw),
        signals=["weak_provenance"],
        evidence={},
        priority=0.0,
    )

    _digest, status = review._reverify(tmp_path, candidate, "2026-09-18")

    assert status == "stamped"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("\ufeff")  # the BOM survives
    assert "updated: 2026-09-18" in text


def test_unreadable_and_undecodable_notes_report_distinct_statuses(
    tmp_path: Path,
) -> None:
    """One "no frontmatter" message for all three failures was wrong for two."""
    path = tmp_path / "Ideas" / "latin.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "---\ntype: note\n---\nCaf\xe9\n".encode("latin-1")
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
    assert review._reverify(tmp_path, candidate, "2026-09-18")[1] == "not_utf8"

    missing = review.ReviewCandidate(
        candidate_id="y" * 24,
        workspace="personal",
        path="memory-vault/Ideas/gone.md",
        content_hash=content_hash(b""),
        signals=["weak_provenance"],
        evidence={},
        priority=0.0,
    )
    assert review._reverify(tmp_path, missing, "2026-09-18")[1] == "unreadable"


def test_an_invalid_disposition_does_not_rewrite_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected decide must not leave a side effect behind.

    `record_decision` raises on a bad disposition, but the control plane
    regenerated the queue projection first — so a call that errored out had
    still rewritten `Workspace/Vault-Review.md`. An agent following a stale
    instruction (the curation skill named `improve_link` for a release after
    it was retired) hit exactly that.
    """
    from types import SimpleNamespace

    from ciao.control_plane import CiaoControlPlane, ControlPlaneError
    from ciao.control_plane import McpPrincipal

    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".ciao"))
    _note(tmp_path, "Ideas/Loose.md", "An unlinked note.")
    config = SimpleNamespace(
        workspace=lambda name: object() if name == "personal" else None,
        vault_root=tmp_path,
        workspace_root=tmp_path,
        agent_vault_root=lambda name: tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        state_path_parent=tmp_path / ".runtime",
    )
    plane = CiaoControlPlane(
        config,
        project_chat_manager=SimpleNamespace(
            _workspace_vault_root=lambda ws: tmp_path,
            # Attended turn: the unattended guard runs before the validation
            # under test, so the stub has to get past it.
            get_chat=lambda cid: SimpleNamespace(
                user_turn_count=1, user_turn_unattended={}
            ),
        ),
        schedule_manager=SimpleNamespace(),
    )
    principal = McpPrincipal(
        token_id="t", chat_id="c", project_id="p",
        workspace="personal", provider="claude",
    )
    queue = tmp_path / "Workspace" / "Vault-Review.md"
    assert not queue.exists()

    with pytest.raises(ControlPlaneError) as caught:
        plane.vault_review(
            principal, action="decide", candidate_id="a" * 24, disposition="improve_link"
        )

    assert caught.value.code == "vault_review_invalid"
    # The valid set is named, so an agent on a stale instruction can recover.
    assert "keep" in str(caught.value)
    assert not queue.exists()


# ── vanished rows and list_cleared cost (release review) ────────────────────


def _kept_note(tmp_path: Path) -> Path:
    note = tmp_path / "Ideas" / "Loose.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ntype: note\n---\nAn unlinked note.\n", encoding="utf-8")
    candidate = generate_candidates(tmp_path, workspace="personal")[0]
    record_decision(tmp_path, candidate, disposition="keep")
    return note


def _dispositions(tmp_path: Path) -> list[str]:
    return [str(r.get("disposition")) for r in read_ledger(tmp_path)]


def test_a_renamed_note_is_not_recorded_as_vanished(tmp_path: Path) -> None:
    """A rename leaves the old path empty; the note never left the vault.

    `candidate_id` carries the content hash, so the per-candidate guard did not
    see the stale `keep` row for the old path — and the ledger claimed the note
    "left the vault by an ordinary file deletion" while it sat there renamed.
    """
    note = _kept_note(tmp_path)
    note.rename(note.parent / "Renamed.md")

    generate_candidates(tmp_path, workspace="personal")

    assert "vanished" not in _dispositions(tmp_path)


def test_a_trashed_note_is_not_also_recorded_as_vanished(tmp_path: Path) -> None:
    """The ledger must not answer the same question twice, differently.

    Keep a note, edit it, retire it: the `trash` row lands under the new hash
    while the stale `keep` sits under the old one, and the old row produced a
    `vanished` for a note that is in the trash offering Restore.
    """
    note = _kept_note(tmp_path)
    note.write_text("---\ntype: note\n---\nEdited.\n", encoding="utf-8")
    trash_note(tmp_path, generate_candidates(tmp_path, workspace="personal")[0])

    generate_candidates(tmp_path, workspace="personal")

    dispositions = _dispositions(tmp_path)
    assert "trash" in dispositions
    assert "vanished" not in dispositions


def test_a_genuinely_deleted_note_is_still_recorded_as_vanished(
    tmp_path: Path,
) -> None:
    """The narrowing must not silence the case the row exists for."""
    note = _kept_note(tmp_path)
    note.unlink()

    generate_candidates(tmp_path, workspace="personal")

    assert "vanished" in _dispositions(tmp_path)


def test_identical_twins_suppress_the_vanished_row(tmp_path: Path) -> None:
    """Documents the content-match boundary: identical twins share one hash.

    Two byte-identical notes have the same content hash, so deleting one
    externally leaves the survivor's hash in the scan and no `vanished` row is
    recorded for the deleted twin. Telling them apart would need per-path
    revision history, which the ledger deliberately does not keep.
    """
    body = "---\ntype: note\n---\nAn unlinked note.\n"
    first = tmp_path / "Ideas" / "Loose.md"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text(body, encoding="utf-8")
    second = tmp_path / "Ideas" / "Twin.md"
    second.write_text(body, encoding="utf-8")
    for candidate in generate_candidates(tmp_path, workspace="personal"):
        record_decision(tmp_path, candidate, disposition="keep")
    first.unlink()

    generate_candidates(tmp_path, workspace="personal")

    assert "vanished" not in _dispositions(tmp_path)


def test_list_cleared_survives_an_unreadable_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreadable note drops its own row, not the whole endpoint.

    `list_cleared` runs unprotected from the GET handler and from every
    mutation's snapshot, so an unguarded read took the candidate list, the
    trash list and every mutation response down with it.
    """
    _kept_note(tmp_path)

    real = Path.read_bytes

    def boom(self: Path) -> bytes:
        if self.name == "Loose.md":
            raise OSError("permission denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", boom)

    assert review.list_cleared(tmp_path, workspace="personal") == []


def test_list_cleared_reads_only_as_far_as_the_page(tmp_path: Path) -> None:
    """Hashing every kept note ran hundreds of reads to return twenty rows."""
    for index in range(12):
        note = tmp_path / "Ideas" / f"Note{index:02d}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"---\ntype: note\n---\nNote {index}.\n", encoding="utf-8")
    for candidate in generate_candidates(tmp_path, workspace="personal", max_candidates=50):
        record_decision(tmp_path, candidate, disposition="keep")

    reads: list[str] = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self.name)
        return real(self)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "read_bytes", counting)
        rows = review.list_cleared(tmp_path, workspace="personal", limit=3)

    assert len(rows) == 3
    assert len(reads) == 3, f"read {len(reads)} notes to return 3 rows"
