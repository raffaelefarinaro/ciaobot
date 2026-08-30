from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.fts_search import EXCLUDED_VAULT_DIRS
from ciao.vault_index import scan_vault
from ciao.vault_lint import run_validation
from ciao.vault_review import (
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
