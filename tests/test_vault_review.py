from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.vault_review import (
    delete_permanently,
    generate_candidates,
    read_ledger,
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
