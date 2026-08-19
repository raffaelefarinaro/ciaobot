from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from ciao.config import CiaoConfig
from ciao.web.app import create_app


@pytest.fixture
def client(tmp_path):
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    # DocA links to DocB via a plain relative markdown link.
    (vault / "DocA.md").write_text(
        "# Doc A\n\nLink to [DocB](./DocB.md)", encoding="utf-8"
    )
    # DocB is the target.
    (vault / "DocB.md").write_text("# Doc B\n\nTarget document.", encoding="utf-8")
    # DocC links to DocB with its own label and a heading anchor.
    (vault / "DocC.md").write_text(
        "See [the target](./DocB.md#Intro) too.", encoding="utf-8"
    )
    # DocD only *mentions* the word "DocB" in prose — no link. Must NOT count.
    (vault / "DocD.md").write_text("This paragraph talks about DocB but links nowhere.", encoding="utf-8")

    cfg = CiaoConfig(
        pwa_auth_token="test-secret",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        pwa_auth_required=False,
        vault_root=vault,
    )
    app = create_app(cfg)
    return TestClient(app)


def test_vault_backlinks_counts_only_real_links(client):
    """A relative markdown link must produce a backlink.

    Before the swap the Backlinks tab only saw `[[wikilinks]]`, so a note
    written in plain markdown showed no incoming links at all.
    """
    resp = client.get("/api/vault/backlinks?path=memory-vault/DocB.md")
    assert resp.status_code == 200
    data = resp.json()
    titles = {b["title"] for b in data["backlinks"]}
    # DocA (plain link) and DocC (labelled link with an anchor) link to DocB.
    assert titles == {"DocA", "DocC"}
    # DocD merely mentions the word and must be excluded (was a false positive).
    assert "DocD" not in titles


def test_vault_backlinks_requires_path(client):
    resp = client.get("/api/vault/backlinks?path=")
    assert resp.status_code == 200
    assert resp.json() == {"backlinks": []}


def test_vault_backlinks_resolves_duplicate_stems_by_note_location(client):
    vault = client.app.state.config.vault_root
    (vault / "a").mkdir()
    (vault / "b").mkdir()
    (vault / "a" / "Target.md").write_text("# A target", encoding="utf-8")
    (vault / "b" / "Target.md").write_text("# B target", encoding="utf-8")
    (vault / "a" / "Local.md").write_text(
        "See [Target](./Target.md).", encoding="utf-8"
    )
    (vault / "b" / "Local.md").write_text(
        "See [Target](./Target.md).", encoding="utf-8"
    )
    (vault / "Explicit.md").write_text(
        "See [Target](./a/Target.md).", encoding="utf-8"
    )
    # No such sibling at the vault root, and the stem is ambiguous, so this
    # note must not be credited as a backlink to either Target.
    (vault / "Ambiguous.md").write_text(
        "See [Target](./Target.md).", encoding="utf-8"
    )

    resp = client.get("/api/vault/backlinks?path=memory-vault/a/Target.md")

    assert resp.status_code == 200
    paths = {item["path"] for item in resp.json()["backlinks"]}
    assert paths == {
        "memory-vault/a/Local.md",
        "memory-vault/Explicit.md",
    }


def test_vault_backlinks_accepts_absolute_target_path(client):
    vault = client.app.state.config.vault_root
    target = vault / "DocB.md"

    resp = client.get("/api/vault/backlinks", params={"path": str(target)})

    assert resp.status_code == 200
    assert {item["title"] for item in resp.json()["backlinks"]} == {"DocA", "DocC"}


def test_vault_backlinks_ignores_a_leftover_wikilink(client):
    """An unconverted `[[wikilink]]` is body text, not an incoming link.

    Markdown links are the only dialect; a pre-migration vault keeps rendering
    but stops claiming edges it can no longer prove.
    """
    vault = client.app.state.config.vault_root
    (vault / "DocE.md").write_text("Old style [[DocB]].", encoding="utf-8")

    resp = client.get("/api/vault/backlinks?path=memory-vault/DocB.md")

    assert resp.status_code == 200
    assert "DocE" not in {item["title"] for item in resp.json()["backlinks"]}
