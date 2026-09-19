"""`?workspace=` must scope the Memory Map scan, not just its output (issue #450).

`scan_targets` reads and parses every markdown file it is handed, and that scan
is the whole cost of the graph route. Filtering afterwards meant a map of one
workspace paid for every note in the install.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ciao import vault_index
from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.web.app import create_app


def _note(path: Path, title: str, related: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rel = f"related: [{related}]\n" if related else ""
    path.write_text(
        f"---\ntype: note\ntitle: {title}\n{rel}---\n# {title}\n", encoding="utf-8"
    )


@pytest.fixture
def rerooted(tmp_path: Path) -> CiaoConfig:
    """A migrated install: one vault per workspace, so one scan target each."""
    (tmp_path / ".runtime").mkdir(parents=True, exist_ok=True)
    _note(tmp_path / "personal" / "memory-vault" / "People" / "User.md", "personal user")
    _note(
        tmp_path / "personal" / "memory-vault" / "Ideas" / "Thing.md",
        "personal thing",
        related="People/User",
    )
    _note(tmp_path / "work" / "memory-vault" / "People" / "User.md", "work user")
    receipt = tmp_path / ".runtime" / "migration" / "workspace-rooting.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    reset_reroot_cache()
    return CiaoConfig(
        pwa_auth_token="test-token",
        pwa_auth_required=False,
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            ),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )


@pytest.fixture
def scanned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records which vault roots the request actually walked."""
    roots: list[str] = []
    real = vault_index.scan_vault

    def spy(root, **kwargs):  # type: ignore[no-untyped-def]
        roots.append(str(root))
        return real(root, **kwargs)

    monkeypatch.setattr(vault_index, "scan_vault", spy)
    return roots


def test_a_scoped_graph_request_scans_only_that_workspaces_vault(
    rerooted: CiaoConfig, scanned: list[str]
) -> None:
    client = TestClient(create_app(rerooted))

    resp = client.get("/api/vault/graph?workspace=work")

    assert resp.status_code == 200
    assert scanned == [str(Path(rerooted.workspace_root) / "work" / "memory-vault")]


def test_an_unscoped_graph_request_still_scans_every_root(
    rerooted: CiaoConfig, scanned: list[str]
) -> None:
    client = TestClient(create_app(rerooted))

    resp = client.get("/api/vault/graph")

    assert resp.status_code == 200
    assert len(scanned) == 2
    assert {n["title"] for n in resp.json()["nodes"]} == {
        "personal user",
        "personal thing",
        "work user",
    }


def test_scoping_the_scan_does_not_change_the_scoped_graph(
    rerooted: CiaoConfig,
) -> None:
    """The point of the change is cost, not content.

    Every field the Memory Map reads has to come back identical to what the
    scan-everything-then-filter route produced, ids and degrees included.
    """
    client = TestClient(create_app(rerooted))

    scoped = client.get("/api/vault/graph?workspace=personal").json()

    assert scoped["workspace"] == "personal"
    assert scoped["workspaces"] == ["personal", "work"], (
        "the picker must still list every workspace the install has"
    )
    assert {n["id"] for n in scoped["nodes"]} == {
        "personal/memory-vault/People/User.md",
        "personal/memory-vault/Ideas/Thing.md",
    }
    assert {n["id"]: n["degree"] for n in scoped["nodes"]} == {
        "personal/memory-vault/People/User.md": 1,
        "personal/memory-vault/Ideas/Thing.md": 1,
    }
    assert [
        tuple(sorted((e["source"], e["target"]))) for e in scoped["edges"]
    ] == [
        (
            "personal/memory-vault/Ideas/Thing.md",
            "personal/memory-vault/People/User.md",
        )
    ]


def test_an_unknown_workspace_is_not_read_as_scan_everything(
    rerooted: CiaoConfig,
) -> None:
    """Falling back to the full target list must not widen the result.

    `?workspace=ghost` matches no target, so the scan cannot be narrowed — but
    the entry filter still has to empty the graph, or a typo in the picker
    would hand back the whole install.
    """
    client = TestClient(create_app(rerooted))

    data = client.get("/api/vault/graph?workspace=ghost").json()

    assert data["nodes"] == []
    assert data["edges"] == []


def test_a_shared_vault_install_is_still_scoped_by_the_entry_filter(
    tmp_path: Path,
) -> None:
    """Before the re-rooting one target holds every workspace: nothing to drop."""
    vault = tmp_path / "memory-vault"
    _note(vault / "personal" / "A.md", "A")
    _note(vault / "work" / "B.md", "B")
    (tmp_path / ".runtime").mkdir(parents=True, exist_ok=True)
    reset_reroot_cache()
    config = CiaoConfig(
        pwa_auth_token="test-token",
        pwa_auth_required=False,
        workspace_root=tmp_path,
        vault_root=vault,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    client = TestClient(create_app(config))

    data = client.get("/api/vault/graph?workspace=personal").json()

    assert {n["title"] for n in data["nodes"]} == {"A"}
    assert data["workspaces"] == ["personal", "work"]
