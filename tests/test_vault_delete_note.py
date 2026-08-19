from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from ciao.config import CiaoConfig
from ciao.web.app import create_app


@pytest.fixture
def client(tmp_path):
    vault = tmp_path / "memory-vault"
    (vault / "personal").mkdir(parents=True)

    # A relates to B via frontmatter `related:` and a body wikilink also
    # points at B, so deleting B should touch A's frontmatter and body.
    (vault / "personal" / "A.md").write_text(
        "---\n"
        "type: note\n"
        "related:\n"
        "  - B\n"
        "---\n"
        "# A\n\nSee [[B]] for context.\n",
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
