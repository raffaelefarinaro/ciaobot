from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from ciao.config import CiaoConfig
from ciao.web.app import create_app


@pytest.fixture
def client(tmp_path):
    vault = tmp_path / "memory-vault"
    (vault / "personal").mkdir(parents=True)
    (vault / "work").mkdir(parents=True)

    # A relates to B via frontmatter `related:` and to C (a different
    # workspace) via an inline relative markdown link.
    (vault / "personal" / "A.md").write_text(
        "---\n"
        "type: note\n"
        "related: [B]\n"
        "description: Note A.\n"
        "---\n"
        "# A\n\nSee [C](../work/C.md) too.\n",
        encoding="utf-8",
    )
    (vault / "personal" / "B.md").write_text(
        "---\n"
        "type: note\n"
        "description: Note B, the target.\n"
        "---\n"
        "# B\n",
        encoding="utf-8",
    )
    (vault / "work" / "C.md").write_text(
        "---\n"
        "type: note\n"
        "description: Note C in Work.\n"
        "---\n"
        "# C\n",
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
    return TestClient(app)


def _node(data, title):
    return next(n for n in data["nodes"] if n["title"] == title)


def test_vault_graph_unscoped_covers_all_workspaces(client):
    resp = client.get("/api/vault/graph")
    assert resp.status_code == 200
    data = resp.json()

    assert data["workspace"] == "all"
    assert data["workspaces"] == ["personal", "work"]
    titles = {n["title"] for n in data["nodes"]}
    assert {"A", "B", "C"} <= titles

    edge_pairs = {tuple(sorted((e["source"], e["target"]))) for e in data["edges"]}
    assert ("memory-vault/personal/A.md", "memory-vault/personal/B.md") in edge_pairs
    assert ("memory-vault/personal/A.md", "memory-vault/work/C.md") in edge_pairs


def test_vault_graph_workspace_filter_drops_cross_workspace_edges(client):
    resp = client.get("/api/vault/graph?workspace=personal")
    assert resp.status_code == 200
    data = resp.json()

    assert data["workspace"] == "personal"
    titles = {n["title"] for n in data["nodes"]}
    assert titles == {"A", "B"}

    edge_pairs = {tuple(sorted((e["source"], e["target"]))) for e in data["edges"]}
    assert edge_pairs == {("memory-vault/personal/A.md", "memory-vault/personal/B.md")}


def test_vault_graph_surfaces_description_and_degree(client):
    resp = client.get("/api/vault/graph")
    data = resp.json()

    b = _node(data, "B")
    assert b["description"] == "Note B, the target."
    assert b["degree"] == 1

    a = _node(data, "A")
    assert a["description"] == "Note A."
    assert a["degree"] == 2


def test_vault_graph_reports_note_mtimes(client):
    """The Memory Map seeds its local view from the most recently written note,
    so every node has to carry a usable timestamp."""
    resp = client.get("/api/vault/graph")
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    assert nodes, "expected the fixture vault to produce nodes"
    for node in nodes:
        assert isinstance(node["mtime"], (int, float))
        assert node["mtime"] > 0, f"{node['title']} has no mtime"


def test_vault_graph_survives_a_note_that_cannot_be_stat_ed(client, tmp_path):
    """A note indexed but unreadable (deleted between scan and stat, broken
    symlink) must degrade to mtime 0 rather than failing the whole request."""
    vault = tmp_path / "memory-vault"
    (vault / "personal" / "Ghost.md").write_text(
        "---\ntype: note\ndescription: Vanishes.\n---\n# Ghost\n", encoding="utf-8"
    )
    # Replace the file with a dangling symlink: still indexed by name, but
    # stat() on it raises.
    (vault / "personal" / "Ghost.md").unlink()
    (vault / "personal" / "Ghost.md").symlink_to(vault / "personal" / "nope.md")

    resp = client.get("/api/vault/graph")
    assert resp.status_code == 200
    titles = {n["title"] for n in resp.json()["nodes"]}
    assert "Ghost" not in titles or _node(resp.json(), "Ghost")["mtime"] == 0.0
