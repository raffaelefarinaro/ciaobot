"""Two-workspace isolation of the share-evidence routes against real stores (#3614, #3646)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ciao import share_evidence as se
from tests.test_share_evidence import Config, client, config, headers  # noqa: F401

FOREIGN_TERMS = ("Borealis", "proj-borealis", "WORK-SECRET", "acme/work", "brain-work")
HOME_DOC = (
    "## Decision\nDecision: HOME-FIRST\nReason: home reason\nEffective date: 2026-11-21\n\n"
    "## Decision\nDecision: HOME-SECOND\nReason: home reason\nEffective date: 2026-11-22\n"
)
WORK_DOC = "## Decision\nDecision: WORK-SECRET-DECISION\nReason: WORK-SECRET-REASON\nEffective date: 2026-11-23\n"


class TwoWorkspaceManager:
    """Projects with distinct ids and names per workspace; lookups by id cross workspaces."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.projects = {
            "proj-atlas": SimpleNamespace(project_id="proj-atlas", name="Atlas", workspace="home"),
            "proj-borealis": SimpleNamespace(project_id="proj-borealis", name="Borealis Launch", workspace="work"),
        }

    def list_projects(self, workspace: str) -> list[SimpleNamespace]:
        return [p for p in self.projects.values() if p.workspace == workspace]

    def get_project(self, project_id: str) -> SimpleNamespace | None:
        return self.projects.get(project_id)

    def active_project_vault_dir(self, project_id: str) -> Path | None:
        project = self.projects.get(project_id)
        return self.config.project_dirs[project.workspace] if project else None


@pytest.fixture
def world(client: TestClient) -> TestClient:
    config = client.app.state.config
    client.app.state.project_chat_manager = TwoWorkspaceManager(config)
    (config.project_dirs["home"] / se.DOCUMENT_NAME).write_text(HOME_DOC, encoding="utf-8")
    (config.project_dirs["work"] / se.DOCUMENT_NAME).write_text(WORK_DOC, encoding="utf-8")
    return client


def get(client: TestClient, path: str, **kw: object):
    return client.get(path, headers=headers(), **kw)


def post(client: TestClient, path: str, body: dict, who: dict | None = None):
    return client.post(path, headers=headers(who), json=body)


def preview_body(project: str, source: str, grant_type: str, **extra: object) -> dict:
    return {
        "brain": "notes", "project_id": project, "source_id": source, "grant_type": grant_type,
        "field_mask": ["decision", "effective_date"], "producers": ["memory_extraction"], **extra,
    }


def work_grant(client: TestClient) -> dict:
    """A confirmed snapshot in the work workspace, made through the real routes."""
    prev = post(client, "/api/workspaces/work/share-evidence/preview", preview_body(
        "proj-borealis", se.DOCUMENT_SOURCE, "snapshot", entry_index=0,
    ))
    assert prev.status_code == 200, prev.text
    done = post(client, "/api/workspaces/work/share-evidence/confirm", {"preview_token": prev.json()["preview_token"], "confirm": True})
    assert done.status_code == 200, done.text
    return done.json()["grant"]


def assert_no_foreign(payload: object, extra: tuple[str, ...] = ()) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    for term in (*FOREIGN_TERMS, *extra):
        assert term not in text, term


def store_text(config: Config, workspace: str) -> str:
    root = se._workspace_dir(config, workspace)
    return "\n".join(p.read_text(errors="replace") for p in sorted(root.rglob("*")) if p.is_file())


def test_foreign_workspace_never_leaks_through_listing_read_preview_or_mutation(world: TestClient) -> None:
    config = world.app.state.config
    grant = work_grant(world)
    gid = grant["grant_id"]
    assert "Borealis" in json.dumps(get(world, "/api/workspaces/work/share-evidence").json())  # control

    listing = get(world, "/api/workspaces/home/share-evidence")
    assert listing.status_code == 200
    assert [p["project_id"] for p in listing.json()["projects"]] == ["proj-atlas"]
    assert listing.json()["grants"] == []
    assert gid not in listing.text
    assert_no_foreign(listing.text, (gid,))

    direct = get(world, f"/api/workspaces/home/share-evidence/grants/{gid}")
    assert direct.status_code == 404
    # The request names the foreign id, so this checks the error does not echo or enrich it.
    assert_no_foreign(direct.text, (gid, "WORK-SECRET-REASON"))

    # Preview for a foreign project, source or entry resolves nothing in this workspace.
    foreign_project = post(world, "/api/workspaces/home/share-evidence/preview", preview_body(
        "proj-borealis", se.DOCUMENT_SOURCE, "snapshot", entry_index=0))
    assert foreign_project.status_code == 400
    assert foreign_project.json()["code"] == "invalid_project"
    assert_no_foreign(foreign_project.text)

    # A real home preview carries only home content, never the work document.
    home_prev = post(world, "/api/workspaces/home/share-evidence/preview", preview_body(
        "proj-atlas", se.DOCUMENT_SOURCE, "snapshot", entry_index=1))
    assert home_prev.status_code == 200, home_prev.text
    assert "HOME-SECOND" in home_prev.text
    assert_no_foreign(home_prev.text)

    # Mutations by grant id, decision save and token replay are refused without touching work.
    before = store_text(config, "work")
    for action in ("revoke", "withdraw"):
        refused = post(world, f"/api/workspaces/home/share-evidence/grants/{gid}/{action}", {})
        assert refused.status_code in {400, 404}, action
        assert_no_foreign(refused.text, (gid,))  # the error must not echo the foreign id
    saved = post(world, "/api/workspaces/home/share-evidence/decisions", {
        "project_id": "proj-borealis", "decision": "Injected", "effective_date": "2026-11-24",
        "disclosure_token": "0" * 64,
    })
    assert saved.status_code == 400
    assert_no_foreign(saved.text)
    assert store_text(config, "work") == before
    assert get(world, f"/api/workspaces/work/share-evidence/grants/{gid}").json()["grant"]["status"] == "active"

    # Audit: the grant history and the home store on disk never mention work.
    confirmed = post(world, "/api/workspaces/home/share-evidence/confirm",
                     {"preview_token": home_prev.json()["preview_token"], "confirm": True})
    assert confirmed.status_code == 200
    detail = get(world, f"/api/workspaces/home/share-evidence/grants/{confirmed.json()['grant']['grant_id']}")
    assert detail.json()["grant"]["history"]
    assert_no_foreign(detail.text, (gid,))
    assert_no_foreign(store_text(config, "home"), (gid, "WORK-SECRET-REASON"))
    assert "HOME-SECOND" not in store_text(config, "work")


def test_tampered_preview_is_refused_or_cannot_widen_the_snapshot(world: TestClient) -> None:
    config = world.app.state.config
    prev = post(world, "/api/workspaces/home/share-evidence/preview", preview_body(
        "proj-atlas", se.DOCUMENT_SOURCE, "snapshot", entry_index=1, field_mask=["decision"]))
    token = prev.json()["preview_token"]
    flipped = token[:-1] + ("0" if token[-1] != "0" else "1")
    bad = post(world, "/api/workspaces/home/share-evidence/confirm", {"preview_token": flipped, "confirm": True})
    assert bad.status_code in {400, 409}
    assert get(world, "/api/workspaces/home/share-evidence").json()["grants"] == []

    # Fields sent beside the token are ignored: the grant is exactly what was previewed.
    done = post(world, "/api/workspaces/home/share-evidence/confirm", {
        "preview_token": token, "confirm": True, "field_mask": ["decision", "reason"],
        "entry_index": 0, "source_id": "stream:proj-atlas", "project_id": "proj-borealis",
    })
    assert done.status_code == 200, done.text
    grant = done.json()["grant"]
    assert grant["field_mask"] == ["decision"]
    assert grant["source_id"] == se.DOCUMENT_SOURCE and grant["project_id"] == "proj-atlas"
    stored = se._load_state(config, "home")["grants"][grant["grant_id"]]
    evidence = se._read_json(
        se._evidence_dir(config, "home") / (stored["evidence_key"] + ".json"),
        limit=se.MAX_EVIDENCE_BYTES, code="evidence_corrupt",
    )
    assert evidence["canonical_bytes"] == '{"decision":"HOME-SECOND"}'
    # The same token cannot be replayed.
    replay = post(world, "/api/workspaces/home/share-evidence/confirm", {"preview_token": token, "confirm": True})
    assert replay.status_code in {400, 409}


def test_workspace_switch_between_preview_and_confirm_writes_nothing(world: TestClient) -> None:
    config = world.app.state.config
    prev = post(world, "/api/workspaces/home/share-evidence/preview", preview_body(
        "proj-atlas", se.DOCUMENT_SOURCE, "snapshot", entry_index=0))
    token = prev.json()["preview_token"]
    work_before = store_text(config, "work")
    switched = post(world, "/api/workspaces/work/share-evidence/confirm", {"preview_token": token, "confirm": True})
    assert switched.status_code in {400, 404, 409}
    assert_no_foreign(switched.text)
    assert get(world, "/api/workspaces/work/share-evidence").json()["grants"] == []
    assert get(world, "/api/workspaces/home/share-evidence").json()["grants"] == []
    assert store_text(config, "work") == work_before
    # The token still belongs to home and confirms there.
    assert post(world, "/api/workspaces/home/share-evidence/confirm", {"preview_token": token, "confirm": True}).status_code == 200


# Durability, not isolation: kept here because it uses the same two-workspace routes.
def test_snapshot_bytes_survive_a_child_process_editing_and_deleting_the_source(world: TestClient) -> None:
    config = world.app.state.config
    prev = post(world, "/api/workspaces/home/share-evidence/preview", preview_body(
        "proj-atlas", se.DOCUMENT_SOURCE, "snapshot", entry_index=1))
    grant = post(world, "/api/workspaces/home/share-evidence/confirm",
                 {"preview_token": prev.json()["preview_token"], "confirm": True}).json()["grant"]
    stored = se._load_state(config, "home")["grants"][grant["grant_id"]]
    path = se._evidence_dir(config, "home") / (stored["evidence_key"] + ".json")
    expected = '{"decision":"HOME-SECOND","effective_date":"2026-11-22"}'
    document = config.project_dirs["home"] / se.DOCUMENT_NAME
    script = (
        "import sys; from pathlib import Path; p = Path(sys.argv[1]); "
        "p.write_text('## Decision\\nDecision: CHILD-EDIT\\nEffective date: 2027-01-01\\n'); "
        "assert 'CHILD-EDIT' in p.read_text(); p.unlink()"
    )
    subprocess.run([sys.executable, "-c", script, str(document)], check=True)
    assert not document.exists()
    evidence = se._read_json(path, limit=se.MAX_EVIDENCE_BYTES, code="evidence_corrupt")
    assert evidence["canonical_bytes"] == expected
    after = get(world, f"/api/workspaces/home/share-evidence/grants/{grant['grant_id']}")
    assert after.status_code == 200
    assert after.json()["grant"]["field_mask"] == ["decision", "effective_date"]
    assert "CHILD-EDIT" not in store_text(config, "home")
