from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from ciao.config import CiaoConfig
from ciao.web.app import create_app


@pytest.fixture
def client(tmp_path):
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    # DocA links to DocB via a plain wikilink.
    (vault / "DocA.md").write_text("# Doc A\n\nLink to [[DocB]]", encoding="utf-8")
    # DocB is the target.
    (vault / "DocB.md").write_text("# Doc B\n\nTarget document.", encoding="utf-8")
    # DocC links to DocB via an aliased wikilink with a heading.
    (vault / "DocC.md").write_text("See [[DocB#Intro|the target]] too.", encoding="utf-8")
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
    resp = client.get("/api/vault/backlinks?path=memory-vault/DocB.md")
    assert resp.status_code == 200
    data = resp.json()
    titles = {b["title"] for b in data["backlinks"]}
    # DocA (plain wikilink) and DocC (aliased wikilink) link to DocB.
    assert titles == {"DocA", "DocC"}
    # DocD merely mentions the word and must be excluded (was a false positive).
    assert "DocD" not in titles


def test_vault_backlinks_requires_path(client):
    resp = client.get("/api/vault/backlinks?path=")
    assert resp.status_code == 200
    assert resp.json() == {"backlinks": []}
