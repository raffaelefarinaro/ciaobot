from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient
from ciao.config import CiaoConfig
from ciao.web.app import create_app


@pytest.fixture
def client(tmp_path):
    vault = tmp_path / "memory-vault"
    (vault / "personal").mkdir(parents=True)

    # A relates to B via frontmatter `related:` and a body markdown link also
    # points at B, so deleting B should touch A's frontmatter and body.
    (vault / "personal" / "A.md").write_text(
        "---\n"
        "type: note\n"
        "related:\n"
        "  - B\n"
        "---\n"
        "# A\n\nSee [B](./B.md) for context.\n",
        encoding="utf-8",
    )
    (vault / "personal" / "B.md").write_text(
        "---\ntype: note\n---\n# B\n",
        encoding="utf-8",
    )
    (vault / "personal" / "Unrelated.md").write_text(
        "---\ntype: note\n---\n# Unrelated\n\nNothing to see here.\n",
        encoding="utf-8",
    )

    cfg = CiaoConfig(
        pwa_auth_token="test-secret",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        pwa_auth_required=False,
        vault_root=vault,
    )
    app = create_app(cfg)
    return TestClient(app), vault


def test_delete_note_removes_file_and_cleans_backlinks(client):
    """Deleting a note must strip the relative markdown links pointing at it.

    Only wikilinks were stripped before, so a delete left every markdown link
    to the note looking live while its target was gone.
    """
    c, vault = client
    resp = c.delete("/api/vault/note?path=memory-vault/personal/B.md")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["edited_backlinks"] == ["memory-vault/personal/A.md"]

    assert not (vault / "personal" / "B.md").exists()
    a_text = (vault / "personal" / "A.md").read_text(encoding="utf-8")
    assert "related:" not in a_text
    assert "See B for context." in a_text
    unrelated_text = (vault / "personal" / "Unrelated.md").read_text(encoding="utf-8")
    assert unrelated_text == "---\ntype: note\n---\n# Unrelated\n\nNothing to see here.\n"
    # the staged cleanup must not leave temp litter behind
    assert list(vault.rglob("*.tmp")) == []


def test_delete_note_cleanup_failure_returns_500_and_keeps_vault_intact(client, monkeypatch):
    """A failed backlink cleanup must not delete the target or half-strip notes.

    strip_references used to write notes one-by-one with no error handling at
    all: a mid-loop failure surfaced as a bare 500 with earlier notes already
    rewritten and the target still alive. The staged cleanup rolls everything
    back, and the route reports the failure instead of deleting.
    """
    c, vault = client

    def boom(*args, **kwargs):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr("ciao.web.routes_api.strip_references", boom)
    a_before = (vault / "personal" / "A.md").read_text(encoding="utf-8")
    b_before = (vault / "personal" / "B.md").read_text(encoding="utf-8")

    resp = c.delete("/api/vault/note?path=memory-vault/personal/B.md")

    assert resp.status_code == 500
    assert "error" in resp.json()
    # nothing changed: references intact, target still on disk
    assert (vault / "personal" / "A.md").read_text(encoding="utf-8") == a_before
    assert (vault / "personal" / "B.md").read_text(encoding="utf-8") == b_before


def test_delete_note_unwritable_dir_aborts_before_touching_backlinks(client):
    """An unwritable target directory must fail BEFORE any backlink is rewritten.

    The unlink used to run after cleanup, so an unwritable folder returned 500
    only after live references had already been stripped out of other notes.
    A pre-flight probe in the target directory now aborts first, keeping both
    the target and every reference to it. Skipped as root, where directory
    permissions do not block writes.
    """
    if os.getuid() == 0:
        pytest.skip("chmod-based writability probe is meaningless as root")
    c, vault = client
    personal = vault / "personal"
    a_before = (personal / "A.md").read_text(encoding="utf-8")
    b_before = (personal / "B.md").read_text(encoding="utf-8")
    personal.chmod(0o555)
    try:
        resp = c.delete("/api/vault/note?path=memory-vault/personal/B.md")
        assert resp.status_code == 500
        assert "error" in resp.json()
    finally:
        personal.chmod(0o755)

    # the vault is untouched: target alive, references intact
    assert (personal / "B.md").exists()
    assert (personal / "B.md").read_text(encoding="utf-8") == b_before
    assert (personal / "A.md").read_text(encoding="utf-8") == a_before
    assert "[B](./B.md)" in a_before


def test_delete_note_missing_path_400(client):
    c, _vault = client
    resp = c.delete("/api/vault/note")
    assert resp.status_code == 400


def test_delete_note_not_found_404(client):
    c, _vault = client
    resp = c.delete("/api/vault/note?path=memory-vault/personal/Nope.md")
    assert resp.status_code == 404


def test_delete_note_rejects_non_vault_prefix(client):
    c, _vault = client
    resp = c.delete("/api/vault/note?path=some/other/file.md")
    assert resp.status_code == 400


def test_delete_note_rejects_non_markdown_extension(client):
    c, _vault = client
    resp = c.delete("/api/vault/note?path=memory-vault/personal/A.md.bak")
    assert resp.status_code in (400, 404, 415)


def test_delete_note_rejects_path_traversal_outside_vault(client):
    c, _vault = client
    resp = c.delete("/api/vault/note?path=memory-vault/../../etc/passwd")
    assert resp.status_code in (400, 404, 415)
