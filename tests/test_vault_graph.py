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


def test_vault_graph_reports_staleness_from_mtime(client, tmp_path):
    """The map's needs-review list is computed by the same detector the audit
    and daily curation consume, so all three surfaces agree."""
    import os
    import time

    vault = tmp_path / "memory-vault"
    # Fixture notes are type `note` (default 180-day horizon): push one well
    # past it while its sibling stays fresh.
    old = time.time() - 400 * 86400
    os.utime(vault / "personal" / "B.md", (old, old))

    resp = client.get("/api/vault/graph")
    assert resp.status_code == 200
    data = resp.json()

    aged = _node(data, "B")
    assert aged["stale"] is True
    assert aged["age_days"] >= 400

    fresh = _node(data, "A")
    assert fresh["stale"] is False


def test_vault_graph_frontmatter_updated_beats_old_mtime(client, tmp_path):
    """`updated:` is a deliberate re-verification claim, so it wins over an
    older file mtime (a future date also proves negative ages stay sane)."""
    import os
    import time

    vault = tmp_path / "memory-vault"
    (vault / "personal" / "Verified.md").write_text(
        "---\ntype: person\nupdated: 2099-01-01\n---\n# Verified\n",
        encoding="utf-8",
    )
    old = time.time() - 400 * 86400
    os.utime(vault / "personal" / "Verified.md", (old, old))

    resp = client.get("/api/vault/graph")
    node = _node(resp.json(), "Verified")
    assert node["stale"] is False
    assert node["age_days"] <= 0
    assert node["updated"] == "2099-01-01"


def _age(path, days):
    import os
    import time

    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_vault_graph_never_flags_what_the_review_queue_never_lists(client, tmp_path):
    """The map's "unchecked" count must equal what the queue can show."""
    vault = tmp_path / "memory-vault" / "personal"
    notes = {
        "Workspace/Proposals.md": "note",
        "journal/daily/_template.md": "note",
        "projects/completed/Done.md": "project",
        "Ideas/ops.md": "log",
        "Ideas/diary.md": "journal",
    }
    for rel, note_type in notes.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntype: {note_type}\n---\n# {path.stem}\n", encoding="utf-8")
        _age(path, 400)

    data = client.get("/api/vault/graph").json()
    for rel in notes:
        node = next(n for n in data["nodes"] if n["id"].endswith(rel))
        assert node["stale"] is False, rel
        # The age is still reported: the map shows it either way.
        assert node["age_days"] >= 400, rel


def test_vault_graph_stale_set_matches_unverified_candidates(client, tmp_path):
    from ciao.vault_review import generate_candidates

    vault = tmp_path / "memory-vault" / "personal"
    (vault / "People").mkdir()
    (vault / "People" / "Mo.md").write_text(
        "---\ntype: person\ntags: [p]\n---\n# Mo\n\nSee [A](../A.md).\n", encoding="utf-8"
    )
    _age(vault / "People" / "Mo.md", 120)
    (vault / "Workspace").mkdir()
    (vault / "Workspace" / "Queue.md").write_text("---\ntype: note\n---\n# Queue\n", encoding="utf-8")
    _age(vault / "Workspace" / "Queue.md", 400)
    _age(vault / "B.md", 400)

    graph_stale = {
        n["id"].rsplit("personal/", 1)[-1]
        for n in client.get("/api/vault/graph?workspace=personal").json()["nodes"]
        if n["stale"]
    }
    queued = {
        c.path.rsplit("personal/", 1)[-1] if "personal/" in c.path else c.path.split("memory-vault/", 1)[-1]
        for c in generate_candidates(vault, workspace="personal", max_candidates=200, write_queue=False)
        if "unverified" in c.signals
    }
    assert graph_stale == queued == {"People/Mo.md", "B.md"}


def test_keep_clears_the_graph_stale_flag(client, tmp_path):
    from ciao.vault_review import generate_candidates, record_decision

    vault = tmp_path / "memory-vault" / "personal"
    (vault / "People").mkdir()
    note = vault / "People" / "Mo.md"
    note.write_text("---\ntype: person\ntags: [p]\nupdated: 2025-01-01\n---\n# Mo\n", encoding="utf-8")

    def mo():
        data = client.get("/api/vault/graph").json()
        return next(n for n in data["nodes"] if n["title"] == "Mo")

    assert mo()["stale"] is True
    candidate = next(
        c for c in generate_candidates(vault, workspace="personal", write_queue=False)
        if c.path.endswith("People/Mo.md")
    )
    assert "unverified" in candidate.signals
    # The horizon rides on the node, so the map names the rule from the same
    # table the flag came from.
    assert mo()["threshold_days"] == 90
    record_decision(vault, candidate, disposition="keep")
    assert mo()["stale"] is False
