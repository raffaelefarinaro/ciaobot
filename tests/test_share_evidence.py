"""Private structured decision evidence and the permission to disclose it (#3614)."""

from __future__ import annotations

import dataclasses
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import curation_run, share_evidence as se
from ciao.config import CiaoConfig, SharedBrainLink, WorkspaceConfig
from ciao.mcp_server import AgentSessionRegistry
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import memory_pass, routes_api
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.project_chats import ProjectChatManager


class Config:
    def __init__(self, root: Path) -> None:
        self.workspace_root = root / "install"
        self.state_path = self.workspace_root / ".runtime" / "state.json"
        self.vaults = {"home": root / "vault-home", "work": root / "vault-work"}
        for vault in self.vaults.values():
            vault.mkdir(parents=True)
        self.links: dict[str, list[SharedBrainLink]] = {
            "home": [SharedBrainLink(name="notes", path=str(root / "brain-home"), identity="github.com/acme/home")],
            "work": [SharedBrainLink(name="notes", path=str(root / "brain-work"), identity="github.com/acme/work")],
        }
        self.lifetimes = {
            "home": "wsl_home_lifetime_0001",
            "work": "wsl_work_lifetime_0001",
        }
        self.lifetime_persisted = {"home": True, "work": True}
        for name in self.vaults:
            folder = self.vaults[name] / "projects" / "active" / "alpha"
            folder.mkdir(parents=True)
        self.project_dirs = {name: self.vaults[name] / "projects" / "active" / "alpha" for name in self.vaults}

    def workspace_names(self) -> list[str]:
        return list(self.vaults)

    def workspace(self, name: str | None) -> SimpleNamespace | None:
        if name not in self.links:
            return None
        return SimpleNamespace(
            shared_brains=self.links[name],
            lifetime_id=self.lifetimes[name],
            _lifetime_persisted=self.lifetime_persisted[name],
        )

    def workspace_vault_root(self, name: str) -> Path:
        return self.vaults[name]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(tmp_path)


def record(config: Config, decision: str, *, day: str = "2026-11-20", reason: str = "", workspace: str = "home") -> dict:
    return se.record_operator_decision(
        config, workspace=workspace, project_id="proj-alpha",
        decision=decision, reason=reason, effective_date=day,
        session="operator-one",
    )


def live_grant(
    config: Config,
    *,
    source_id: str = "stream:proj-alpha",
    grant_type: str = "standing",
    field_mask: list[str] | None = None,
    producers: list[str] | None = None,
    entry_index: int | None = None,
    workspace: str = "home",
    session: str = "operator-one",
) -> dict:
    preview = se.preview(
        config, workspace=workspace, brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs[workspace],
        source_id=source_id, grant_type=grant_type,
        field_mask=field_mask or ["decision", "effective_date"],
        producers=producers or ["memory_extraction", "nightly_curation"],
        entry_index=entry_index, session=session,
    )
    return se.confirm(
        config, workspace=workspace, token=preview["preview_token"], session=session
    )


def assert_eligibility_blocked(config: Config, *, workspace: str = "home") -> None:
    with pytest.raises(se.EvidenceError) as raised:
        se.eligible_entries(
            config, workspace=workspace, brain_name="notes", relation=None,  # type: ignore[arg-type]
        )
    assert raised.value.code == "invalid_relation"


def test_existing_install_starts_in_review_with_no_evidence(config: Config) -> None:
    assert se.workspace_mode(config, workspace="home") == se.MODE_REVIEW
    assert se.list_grants(config, workspace="home") == []
    assert (config.state_path.parent / "secrets" / "share-evidence").exists() is False


def test_standing_grant_records_scope_and_blocks_eligibility_until_integration(config: Config) -> None:
    first = record(config, "Use the old vendor")
    with pytest.raises(se.EvidenceError):
        # A snapshot grant cannot be issued for the engine-owned stream.
        live_grant(config, grant_type="snapshot")

    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
        source_id="stream:proj-alpha", grant_type="standing",
        field_mask=["decision", "effective_date"],
        producers=["memory_extraction", "nightly_curation"],
        session="operator-one",
    )
    assert preview["preview"]["future_items_without_review"] is True
    assert "When automatic sharing becomes available" in preview["preview"]["summary"]
    assert preview["preview"]["excluded_fields"] == ["reason"]
    sample_fields = {row["field"] for sample in preview["preview"]["fields"] for row in sample}
    assert sample_fields == {"decision", "effective_date"}
    confirmed = se.confirm(
        config, workspace="home", token=preview["preview_token"], session="operator-one"
    )
    grant = confirmed["grant"]
    assert grant["type"] == "standing"
    assert grant["start_revision"] == 1
    assert grant["consent_generation"] == 1
    assert grant["audience"] == "github.com/acme/home"
    assert grant["status"] == "active"
    assert grant["linked"] is True
    assert grant["brain"]["link_fingerprint"]
    assert se.workspace_mode(config, workspace="home") == se.MODE_AUTOMATIC

    second = record(config, "Use the new vendor", reason="cheaper")
    third = record(config, "Keep the migration", reason="already tested")
    assert second["entry"]["revision"] == 2
    assert third["entry"]["revision"] == 3
    # The grant scope is recorded, but no caller-selected relation can resolve it
    # until a real producer integration supplies an engine-verified context.
    assert_eligibility_blocked(config)
    for producer in se.PRODUCERS:
        assert producer in grant["producers"]
    assert first["entry"]["entry_id"] != second["entry"]["entry_id"]

    with pytest.raises(se.EvidenceError):
        se.grant_detail(config, workspace="home", grant_id="grn_" + "0" * 24)

    # A private note and a generic file write are not the engine's trusted event.
    (config.project_dirs["home"] / "note.md").write_text(
        "## Decision\nDecision: leaked\nEffective date: 2026-11-20\n", encoding="utf-8"
    )
    assert se.stream_for_project(
        config, workspace="home", project_id="proj-alpha"
    )["entry_count"] == 3


def test_individual_snapshot_survives_external_edit_and_deletion(config: Config) -> None:
    document = config.project_dirs["home"] / se.DOCUMENT_NAME
    document.write_text(
        "## Decision\nDecision: Ship the beta\nReason: internal\nEffective date: 2026-11-21\n\n"
        "## Decision\nDecision: Delay the next beta\nReason: stability\nEffective date: 2026-11-22\n",
        encoding="utf-8",
    )
    sources = se.list_sources(
        config, workspace="home", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
    )
    assert [row["kind"] for row in sources] == ["document"]
    confirmed = live_grant(
        config, source_id=se.DOCUMENT_SOURCE, grant_type="snapshot",
        field_mask=["decision", "effective_date"], entry_index=1,
    )
    assert confirmed["grant"]["type"] == "snapshot"
    assert confirmed["grant"]["status"] == "active"
    expected = '{"decision":"Delay the next beta","effective_date":"2026-11-22"}'
    state = se._load_state(config, "home")
    stored_grant = state["grants"][confirmed["grant"]["grant_id"]]
    evidence_path = se._evidence_dir(config, "home") / (stored_grant["evidence_key"] + ".json")
    evidence = se._read_json(evidence_path, limit=se.MAX_EVIDENCE_BYTES, code="evidence_corrupt")
    assert evidence["canonical_bytes"] == expected
    assert evidence["project_id"] == "proj-alpha"

    # A separate process changes and then deletes the original document.
    document.write_text("## Decision\nDecision: Changed later\nEffective date: 2026-12-01\n", encoding="utf-8")
    evidence = se._read_json(evidence_path, limit=se.MAX_EVIDENCE_BYTES, code="evidence_corrupt")
    assert evidence["canonical_bytes"] == expected
    document.unlink()
    evidence = se._read_json(evidence_path, limit=se.MAX_EVIDENCE_BYTES, code="evidence_corrupt")
    assert evidence["canonical_bytes"] == expected
    assert_eligibility_blocked(config)

    withdrawn = se.withdraw(
        config, workspace="home", grant_id=confirmed["grant"]["grant_id"], reason="superseded"
    )
    assert withdrawn["grant"]["withdrawn_at"]
    assert withdrawn["grant"]["status"] == "withdrawn"


def test_revoke_removes_old_generation_and_requires_a_fresh_grant(config: Config) -> None:
    record(config, "Baseline")
    grant = live_grant(config)["grant"]
    record(config, "Later decision")
    assert se.workspace_mode(config, workspace="home") == se.MODE_AUTOMATIC

    result = se.revoke(config, workspace="home", grant_id=grant["grant_id"])
    assert result["fresh_grant_required"] is True
    assert result["grant"]["revoked_generation"] == 1
    assert result["grant"]["active"] is False
    assert result["grant"]["status"] == "revoked"
    assert se.workspace_mode(config, workspace="home") == se.MODE_REVIEW
    assert_eligibility_blocked(config)

    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
        source_id="stream:proj-alpha", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    fresh = se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")
    assert fresh["grant"]["consent_generation"] == 1
    assert fresh["grant"]["grant_id"] != grant["grant_id"]


def test_stale_session_mismatched_and_reused_previews_are_rejected(config: Config) -> None:
    record(config, "Baseline")
    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
        source_id="stream:proj-alpha", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    with pytest.raises(se.EvidenceError, match="another operator session"):
        se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-two")

    record(config, "Authored while previewing")
    with pytest.raises(se.EvidenceError, match="stream changed"):
        se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")

    preview2 = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
        source_id="stream:proj-alpha", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    se.confirm(config, workspace="home", token=preview2["preview_token"], session="operator-one")
    with pytest.raises(se.EvidenceError, match="already confirmed"):
        se.confirm(config, workspace="home", token=preview2["preview_token"], session="operator-one")


def test_preview_expires(config: Config) -> None:
    record(config, "Baseline")
    moment = datetime(2026, 11, 25, tzinfo=UTC)
    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
        source_id="stream:proj-alpha", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"],
        session="operator-one", now=moment,
    )
    with pytest.raises(se.EvidenceError, match="expired"):
        se.confirm(
            config, workspace="home", token=preview["preview_token"],
            session="operator-one", now=moment + timedelta(minutes=16),
        )


def test_workspace_isolation_and_relinks_convey_no_grant(config: Config) -> None:
    record(config, "Home baseline")
    grant = live_grant(config)["grant"]
    record(config, "Home later")
    assert se.workspace_mode(config, workspace="home") == se.MODE_AUTOMATIC

    # Another workspace has its own empty store: grant ids and entries do not cross.
    assert se.list_grants(config, workspace="work") == []
    assert_eligibility_blocked(config, workspace="work")
    with pytest.raises(se.EvidenceError, match="not available"):
        se.grant_detail(config, workspace="work", grant_id=grant["grant_id"])
    with pytest.raises(se.EvidenceError, match="not available"):
        se.revoke(config, workspace="work", grant_id=grant["grant_id"])

    # Copying private state between workspace stores is refused, never adopted.
    home_store = se._workspace_dir(config, "home")
    work_store = se._workspace_dir(config, "work")
    work_store.mkdir(parents=True, exist_ok=True)
    (work_store / "state.json").write_bytes((home_store / "state.json").read_bytes())
    (work_store / "state.json").chmod(0o600)
    with pytest.raises(se.EvidenceError, match="another workspace"):
        se.list_grants(config, workspace="work")
    (work_store / "state.json").unlink()

    # Re-linking the folder mints a new lifetime; the old grant no longer resolves.
    config.links["home"] = [
        SharedBrainLink(name="notes", path=config.links["home"][0].path, identity="github.com/acme/home")
    ]
    assert se.workspace_mode(config, workspace="home") == se.MODE_REVIEW
    assert se.list_grants(config, workspace="home")[0]["status"] == "unlinked"
    assert_eligibility_blocked(config)


def test_preview_token_is_bound_to_its_workspace(config: Config) -> None:
    record(config, "Baseline")
    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
        source_id="stream:proj-alpha", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    with pytest.raises(se.EvidenceError, match="not available"):
        se.confirm(config, workspace="work", token=preview["preview_token"], session="operator-one")
    with pytest.raises(se.EvidenceError, match="not available"):
        se.preview_target(config, workspace="work", token=preview["preview_token"], session="operator-one")
    # The home token is still intact for its own workspace.
    assert se.preview_target(
        config, workspace="home", token=preview["preview_token"], session="operator-one"
    )["project_id"] == "proj-alpha"


def test_only_the_declared_decision_document_is_a_snapshot_source(config: Config) -> None:
    (config.project_dirs["home"] / "Note.md").write_text(
        "## Decision\nDecision: Not a source\nEffective date: 2026-11-20\n", encoding="utf-8"
    )
    assert se.list_sources(
        config, workspace="home", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
    ) == []
    with pytest.raises(se.EvidenceError, match="listed sources"):
        se.preview(
            config, workspace="home", brain_name="notes", project_id="proj-alpha",
            project_name="Alpha", project_dir=config.project_dirs["home"],
            source_id="document:Note.md", grant_type="snapshot",
            field_mask=["decision"], producers=["memory_extraction"],
            entry_index=0, session="operator-one",
        )


def test_foreign_project_folder_is_refused(config: Config) -> None:
    record(config, "Baseline")
    with pytest.raises(se.EvidenceError, match="outside this workspace"):
        se.preview(
            config, workspace="home", brain_name="notes", project_id="proj-alpha",
            project_name="Alpha", project_dir=config.project_dirs["work"],
            source_id="stream:proj-alpha", grant_type="standing",
            field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
        )
    document = config.project_dirs["home"] / se.DOCUMENT_NAME
    document.write_text("## Decision\nDecision: Fine\nEffective date: 2026-11-20\n", encoding="utf-8")
    with pytest.raises(se.EvidenceError, match="outside this workspace"):
        se.list_sources(
            config, workspace="home", project_id="proj-alpha",
            project_name="Alpha", project_dir=config.project_dirs["work"],
        )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"decision": ""}, "Decision is required"),
        ({"decision": "x" * 2001}, "longer than"),
        ({"effective_date": "2026-02-30"}, "real calendar date"),
        ({"effective_date": "20-11-2026"}, "YYYY-MM-DD"),
        ({"reason": "password=hunter2"}, "credential-shaped"),
        ({"decision": "line\u0000break"}, "control character"),
    ],
)
def test_recorded_fields_are_bounded_and_validated(config: Config, kwargs: dict, message: str) -> None:
    values = {"decision": "Ship it", "reason": "", "effective_date": "2026-11-20", **kwargs}
    with pytest.raises(se.EvidenceError, match=message):
        se.record_decision(
            config, workspace="home", project_id="proj-alpha",
            decision=values["decision"], reason=values["reason"],
            effective_date=values["effective_date"],
        )
    assert se.list_grants(config, workspace="home") == []


@pytest.mark.parametrize(
    "field_mask, producers, message",
    [
        ([], ["memory_extraction"], "at least one field"),
        (["decision", "transcript"], ["memory_extraction"], "listed decision fields"),
        (["decision"], [], "at least one producer"),
        (["decision"], ["generic_agent"], "listed producers"),
    ],
)
def test_preview_requires_an_approved_field_mask_and_producer(
    config: Config, field_mask: list[str], producers: list[str], message: str
) -> None:
    record(config, "Baseline")
    with pytest.raises(se.EvidenceError, match=message):
        se.preview(
            config, workspace="home", brain_name="notes", project_id="proj-alpha",
            project_name="Alpha", project_dir=config.project_dirs["home"],
            source_id="stream:proj-alpha", grant_type="standing",
            field_mask=field_mask, producers=producers, session="operator-one",
        )


def test_superseded_stream_entry_stops_resolving(config: Config) -> None:
    record(config, "Baseline")
    grant = live_grant(config)["grant"]
    entry = record(config, "Wrong decision")["entry"]
    superseded = se.supersede_entry(
        config, workspace="home", project_id="proj-alpha", entry_id=entry["entry_id"]
    )
    assert superseded["superseded"] is True
    assert se.stream_for_project(config, workspace="home", project_id="proj-alpha")["entry_count"] == 2
    assert se.workspace_mode(config, workspace="home") == se.MODE_AUTOMATIC
    assert_eligibility_blocked(config)
    se.revoke(config, workspace="home", grant_id=grant["grant_id"])


def test_private_state_is_owner_only(config: Config) -> None:
    record(config, "Baseline")
    live_grant(config)
    root = config.state_path.parent / "secrets" / "share-evidence"
    assert root.stat().st_mode & 0o077 == 0
    assert root.parent.stat().st_mode & 0o077 == 0
    for path in root.rglob("*.json"):
        assert path.stat().st_mode & 0o077 == 0
    raw = (se._workspace_dir(config, "home") / "state.json").read_text()
    assert "canonical_bytes" not in raw
    assert all("evidence_key" not in item for item in se.list_grants(config, workspace="home"))


# ---------------------------------------------------------------------------
# Routes


class FakePCM:
    def __init__(self, config: Config) -> None:
        self.config = config

    def list_projects(self, workspace: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(project_id="proj-alpha", name="Alpha", workspace=workspace)]

    def get_project(self, project_id: str) -> SimpleNamespace | None:
        if project_id != "proj-alpha":
            return None
        return SimpleNamespace(project_id="proj-alpha", name="Alpha", workspace="home")

    def active_project_vault_dir(self, project_id: str) -> Path | None:
        return self.config.project_dirs["home"] if project_id == "proj-alpha" else None


@pytest.fixture
def client(config: Config) -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/workspaces/{name}/share-evidence", routes_api.share_evidence_state),
            Route("/api/workspaces/{name}/share-evidence/decisions", routes_api.share_evidence_record_decision, methods=["POST"]),
            Route("/api/workspaces/{name}/share-evidence/preview", routes_api.share_evidence_preview, methods=["POST"]),
            Route("/api/workspaces/{name}/share-evidence/confirm", routes_api.share_evidence_confirm, methods=["POST"]),
            Route("/api/workspaces/{name}/share-evidence/grants/{grant_id}", routes_api.share_evidence_grant),
            Route("/api/workspaces/{name}/share-evidence/grants/{grant_id}/revoke", routes_api.share_evidence_revoke, methods=["POST"]),
            Route("/api/workspaces/{name}/share-evidence/grants/{grant_id}/withdraw", routes_api.share_evidence_withdraw, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.project_chat_manager = FakePCM(config)
    return TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 50000))


def headers(payload: dict | None = None) -> dict[str, str]:
    signer = URLSafeTimedSerializer("test-secret")
    return {
        "Cookie": f"{SESSION_COOKIE}={signer.dumps(payload or {'user': 'owner'})}",
        "Origin": "http://localhost:8443",
    }


def test_routes_require_a_session_and_scope_every_action(client: TestClient) -> None:
    assert client.get("/api/workspaces/home/share-evidence").status_code == 401
    denied_origin = client.post(
        "/api/workspaces/home/share-evidence/decisions",
        headers={"Cookie": headers()["Cookie"], "Origin": "http://evil.example"},
        json={"project_id": "proj-alpha", "decision": "x", "effective_date": "2026-11-20"},
    )
    assert denied_origin.status_code == 403
    get = client.get("/api/workspaces/home/share-evidence", headers=headers())
    assert get.status_code == 200
    body = get.json()
    assert body["chat_evidence"] == {"available": False, "reason": "protected_authority_unavailable"}
    assert body["mode"] == "review"
    assert body["fields"] == ["decision", "reason", "effective_date"]
    assert body["producers"] == ["memory_extraction", "nightly_curation"]
    assert body["brains"] == [{"name": "notes", "identity": "github.com/acme/home"}]
    assert body["projects"][0]["sources"] == []
    assert body["grants"] == []

    recorded = client.post(
        "/api/workspaces/home/share-evidence/decisions",
        headers=headers(),
        json={"project_id": "proj-alpha", "decision": "Use the new vendor", "reason": "cheaper", "effective_date": "2026-11-20", "disclosure_token": body["disclosure_tokens"]["proj-alpha"]},
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["entry"]["decision"] == "Use the new vendor"

    preview = client.post(
        "/api/workspaces/home/share-evidence/preview",
        headers=headers(),
        json={
            "brain": "notes", "project_id": "proj-alpha", "source_id": "stream:proj-alpha",
            "grant_type": "standing", "field_mask": ["decision"], "producers": ["memory_extraction"],
        },
    )
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]
    assert preview.json()["preview"]["recipient"]["audience"] == "github.com/acme/home"

    missing_confirm = client.post(
        "/api/workspaces/home/share-evidence/confirm",
        headers=headers(),
        json={"preview_token": token},
    )
    assert missing_confirm.status_code == 400
    confirmed = client.post(
        "/api/workspaces/home/share-evidence/confirm",
        headers=headers(),
        json={"preview_token": token, "confirm": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    grant_id = confirmed.json()["grant"]["grant_id"]
    assert confirmed.json()["mode"] == "automatic"

    listed = client.get("/api/workspaces/home/share-evidence", headers=headers()).json()
    assert [g["grant_id"] for g in listed["grants"]] == [grant_id]
    assert "canonical_bytes" not in listed["grants"][0]
    assert "entry_id" not in listed["grants"][0]

    detail = client.get(f"/api/workspaces/home/share-evidence/grants/{grant_id}", headers=headers())
    assert detail.status_code == 200
    assert detail.json()["grant"]["field_mask"] == ["decision"]

    revoked = client.post(
        f"/api/workspaces/home/share-evidence/grants/{grant_id}/revoke", headers=headers(), json={}
    )
    assert revoked.status_code == 200
    assert revoked.json()["grant"]["revoked_generation"] == 1
    assert revoked.json()["fresh_grant_required"] is True


def test_decision_save_refuses_changed_disclosure_and_foreign_token(client: TestClient, config: Config) -> None:
    def state(workspace: str = "home") -> dict:
        return client.get(f"/api/workspaces/{workspace}/share-evidence", headers=headers()).json()

    def save(token: str) -> object:
        return client.post("/api/workspaces/home/share-evidence/decisions", headers=headers(), json={
            "project_id": "proj-alpha", "decision": "A", "effective_date": "2026-11-20",
            "disclosure_token": token,
        })

    record(config, "Baseline")
    old = state()["disclosure_tokens"]["proj-alpha"]
    foreign = state("work")["disclosure_tokens"]["proj-alpha"]
    assert save(foreign).status_code == 409
    grant = live_grant(config)["grant"]
    assert save(old).status_code == 409
    assert se.stream_for_project(config, workspace="home", project_id="proj-alpha")["revision"] == 1
    fresh = state()["disclosure_tokens"]["proj-alpha"]
    se.revoke(config, workspace="home", grant_id=grant["grant_id"])
    assert save(fresh).status_code == 409
    assert save(old).status_code == 409  # Grant and revoke must not restore an old token.
    assert se.stream_for_project(config, workspace="home", project_id="proj-alpha")["revision"] == 1
    assert save(state()["disclosure_tokens"]["proj-alpha"]).status_code == 200


def test_routes_refuse_a_foreign_workspace_and_invalid_preview_session(client: TestClient) -> None:
    assert client.get("/api/workspaces/nope/share-evidence", headers=headers()).status_code == 404
    record(client.app.state.config, "Baseline")
    preview = client.post(
        "/api/workspaces/home/share-evidence/preview",
        headers=headers(),
        json={
            "brain": "notes", "project_id": "proj-alpha", "source_id": "stream:proj-alpha",
            "grant_type": "standing", "field_mask": ["decision"], "producers": ["memory_extraction"],
        },
    )
    token = preview.json()["preview_token"]
    other = {'user': 'owner', 'device': 'second-session'}
    mismatch = client.post(
        "/api/workspaces/home/share-evidence/confirm",
        headers=headers(other),
        json={"preview_token": token, "confirm": True},
    )
    # A second signed cookie is a different session value.
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "preview_session_mismatch"

    foreign = client.post(
        "/api/workspaces/work/share-evidence/decisions",
        headers=headers(),
        json={"project_id": "proj-alpha", "decision": "Wrong workspace", "effective_date": "2026-11-20"},
    )
    assert foreign.status_code == 400
    assert foreign.json()["code"] == "invalid_project"


# ---------------------------------------------------------------------------
# Review regressions: trusted producer context, workspace lifetime, collisions,
# malformed state and cross-process atomicity.


def _second_project(config: Config) -> Path:
    folder = config.vaults["home"] / "projects" / "active" / "beta"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_eligibility_is_blocked_even_with_a_live_grant_and_later_entries(config: Config) -> None:
    record(config, "Baseline")
    live_grant(config)
    record(config, "Later decision")
    assert se.workspace_mode(config, workspace="home") == se.MODE_AUTOMATIC
    assert_eligibility_blocked(config)
def test_unpersisted_lifetime_is_refused_before_any_evidence(config: Config) -> None:
    config.lifetime_persisted["home"] = False
    with pytest.raises(se.EvidenceError, match="not durable"):
        se.workspace_mode(config, workspace="home")
    assert not (config.state_path.parent / "secrets" / "share-evidence").exists()


def test_delete_and_recreate_workspace_starts_without_consent(config: Config) -> None:
    record(config, "Baseline")
    live_grant(config)
    record(config, "Later decision")
    assert se.workspace_mode(config, workspace="home") == se.MODE_AUTOMATIC

    # A new registered workspace instance under the same name has a new lifetime.
    config.lifetimes["home"] = "wsl_recreated_lifetime_0002"
    assert se.list_grants(config, workspace="home") == []
    assert se.workspace_mode(config, workspace="home") == se.MODE_REVIEW
    assert not se._workspace_dir(config, "home").exists()
    assert_eligibility_blocked(config)


def test_unlinked_brain_does_not_report_automatic(config: Config) -> None:
    record(config, "Baseline")
    live_grant(config)
    assert se.workspace_mode(config, workspace="home") == se.MODE_AUTOMATIC
    config.links["home"] = []
    assert se.workspace_mode(config, workspace="home") == se.MODE_REVIEW
    assert se.list_grants(config, workspace="home")[0]["status"] == "unlinked"
    assert_eligibility_blocked(config)


def test_identical_snapshots_in_two_projects_keep_separate_evidence(config: Config) -> None:
    decision_doc = "## Decision\nDecision: Same words\nReason: same\nEffective date: 2026-11-20\n"
    (config.project_dirs["home"] / se.DOCUMENT_NAME).write_text(decision_doc, encoding="utf-8")
    beta = _second_project(config)
    (beta / se.DOCUMENT_NAME).write_text(decision_doc, encoding="utf-8")
    alpha_grant = live_grant(
        config, source_id=se.DOCUMENT_SOURCE, grant_type="snapshot", entry_index=0,
    )["grant"]
    beta_preview = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-beta",
        project_name="Beta", project_dir=beta,
        source_id=se.DOCUMENT_SOURCE, grant_type="snapshot",
        field_mask=["decision", "effective_date"], producers=["memory_extraction"],
        entry_index=0, session="operator-one",
    )
    beta_grant = se.confirm(
        config, workspace="home", token=beta_preview["preview_token"], session="operator-one"
    )["grant"]
    assert alpha_grant["grant_id"] != beta_grant["grant_id"]

    state = se._load_state(config, "home")
    alpha_state = state["grants"][alpha_grant["grant_id"]]
    beta_state = state["grants"][beta_grant["grant_id"]]
    alpha_evidence = se._read_json(
        se._evidence_dir(config, "home") / (alpha_state["evidence_key"] + ".json"),
        limit=se.MAX_EVIDENCE_BYTES, code="evidence_corrupt",
    )
    beta_evidence = se._read_json(
        se._evidence_dir(config, "home") / (beta_state["evidence_key"] + ".json"),
        limit=se.MAX_EVIDENCE_BYTES, code="evidence_corrupt",
    )
    assert alpha_evidence["canonical_bytes"] == beta_evidence["canonical_bytes"]
    assert alpha_evidence["digest"] == beta_evidence["digest"]
    assert alpha_evidence["project_id"] == "proj-alpha"
    assert beta_evidence["project_id"] == "proj-beta"
    assert alpha_state["evidence_key"] != beta_state["evidence_key"]
    evidence_files = list((se._workspace_dir(config, "home") / "evidence").glob("*.json"))
    assert len(evidence_files) == 2
    assert_eligibility_blocked(config)


def test_malformed_private_state_fails_closed(config: Config) -> None:
    record(config, "Baseline")
    state = se._state_path(config, "home")
    state.write_text("{not json", encoding="utf-8")
    state.chmod(0o600)
    for call in (
        lambda: se.list_grants(config, workspace="home"),
        lambda: se.workspace_mode(config, workspace="home"),
        lambda: se.record_decision(
            config, workspace="home", project_id="proj-alpha",
            decision="After corruption", reason="", effective_date="2026-11-20",
        ),
    ):
        with pytest.raises(se.EvidenceError, match="unreadable"):
            call()
    state.write_text(json.dumps({"schema": 1, "workspace": "home"}), encoding="utf-8")
    with pytest.raises(se.EvidenceError, match="another workspace"):
        se.list_grants(config, workspace="home")


def test_crash_between_grant_and_token_write_cannot_replay(config: Config) -> None:
    record(config, "Baseline")
    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
        source_id="stream:proj-alpha", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")
    # Simulate the old two-file seam: the grant landed but token consumption was lost.
    state = se._read_json(se._state_path(config, "home"), limit=se.MAX_STATE_BYTES, code="private_state_corrupt")
    hash_ = se._token_hash(preview["preview_token"])
    state["previews"][hash_]["used_at"] = None
    se._write_private(
        se._state_path(config, "home"),
        json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        limit=se.MAX_STATE_BYTES,
    )
    with pytest.raises(se.EvidenceError, match="already confirmed"):
        se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")
    assert len(se.list_grants(config, workspace="home")) == 1


def test_two_processes_confirm_one_preview_exactly_once(config: Config, tmp_path: Path) -> None:
    import subprocess
    import sys
    import time

    record(config, "Baseline")
    config.links["home"] = [SharedBrainLink(
        name="notes", path=config.links["home"][0].path,
        identity="github.com/acme/home", lifetime_id="brain_lifetime_fixed01",
    )]
    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=config.project_dirs["home"],
        source_id="stream:proj-alpha", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    repo = Path(__file__).resolve().parents[1]
    script = tmp_path / "confirm_once.py"
    go = tmp_path / "go"
    script.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from ciao import share_evidence as se\n"
        "from ciao.config import SharedBrainLink\n"
        "class C:\n"
        "    def __init__(self, state):\n"
        "        self.state_path = Path(state)\n"
        "    def workspace_names(self):\n"
        "        return ['home']\n"
        "    def workspace(self, name):\n"
        "        if name != 'home':\n"
        "            return None\n"
        "        return SimpleNamespace(lifetime_id='wsl_home_lifetime_0001', _lifetime_persisted=True, shared_brains=[SharedBrainLink(name='notes', path='/tmp/notes', identity='github.com/acme/home', lifetime_id='brain_lifetime_fixed01')])\n"
        "while not Path(sys.argv[2]).exists():\n"
        "    time.sleep(0.01)\n"
        "try:\n"
        "    se.confirm(C(sys.argv[1]), workspace='home', token=sys.argv[3], session='operator-one')\n"
        "except Exception as exc:\n"
        "    print('ERR:' + str(getattr(exc, 'code', '')))\n"
        "else:\n"
        "    print('OK')\n",
        encoding="utf-8",
    )
    env = {"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    processes = [
        subprocess.Popen(
            [sys.executable, str(script), str(config.state_path), str(go), preview["preview_token"]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for _ in range(2)
    ]
    time.sleep(0.2)
    go.write_text("go", encoding="utf-8")
    outputs = []
    for process in processes:
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0, err
        outputs.append(out.strip())
    assert sorted(outputs) == ["ERR:preview_used", "OK"]
    assert len(se.list_grants(config, workspace="home")) == 1


def test_two_processes_revoke_and_record_without_losing_either_write(
    config: Config, tmp_path: Path
) -> None:
    import subprocess
    import sys
    import time

    record(config, "Baseline")
    grant = live_grant(config)["grant"]
    repo = Path(__file__).resolve().parents[1]
    script = tmp_path / "mutate_once.py"
    go = tmp_path / "go"
    script.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from ciao import share_evidence as se\n"
        "from ciao.config import SharedBrainLink\n"
        "class C:\n"
        "    def __init__(self, state):\n"
        "        self.state_path = Path(state)\n"
        "    def workspace_names(self):\n"
        "        return ['home']\n"
        "    def workspace(self, name):\n"
        "        if name != 'home':\n"
        "            return None\n"
        "        return SimpleNamespace(lifetime_id='wsl_home_lifetime_0001', _lifetime_persisted=True, shared_brains=[SharedBrainLink(name='notes', path='/tmp/notes', identity='github.com/acme/home', lifetime_id='brain_lifetime_fixed01')])\n"
        "while not Path(sys.argv[2]).exists():\n"
        "    time.sleep(0.01)\n"
        "config = C(sys.argv[1])\n"
        "if sys.argv[3] == 'revoke':\n"
        "    se.revoke(config, workspace='home', grant_id=sys.argv[4])\n"
        "else:\n"
        "    se.record_decision(config, workspace='home', project_id='proj-alpha', decision='Concurrent entry', reason='', effective_date='2026-11-21')\n"
        "print('OK')\n",
        encoding="utf-8",
    )
    env = {"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    processes = [
        subprocess.Popen(
            [sys.executable, str(script), str(config.state_path), str(go), action, grant["grant_id"]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for action in ("revoke", "record")
    ]
    time.sleep(0.2)
    go.write_text("go", encoding="utf-8")
    for process in processes:
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0, err
        assert out.strip() == "OK"
    stored = se.grant_detail(config, workspace="home", grant_id=grant["grant_id"])
    assert stored["active"] is False
    assert stored["revoked_generation"] == 1
    assert se.stream_for_project(config, workspace="home", project_id="proj-alpha")["entry_count"] == 2
    assert_eligibility_blocked(config)


def test_legacy_registry_lifetime_is_persisted_before_evidence_survives_restart(
    tmp_path: Path,
) -> None:
    from ciao.config import CiaoConfig

    vault = tmp_path / "vault"
    project = vault / "projects" / "active" / "alpha"
    project.mkdir(parents=True)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text(
        json.dumps([{"name": "home", "vault_root": str(vault)}]), encoding="utf-8"
    )
    env = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(runtime),
    }
    config = CiaoConfig.from_env(env)
    link = SharedBrainLink(
        name="notes", path=str(tmp_path / "brain"), identity="github.com/acme/home",
        lifetime_id="brain_lifetime_fixed01",
    )
    config.workspace("home").shared_brains = [link]
    assert config.workspace("home")._lifetime_persisted is False

    se.record_decision(
        config, workspace="home", project_id="proj-alpha",
        decision="Persisted baseline", reason="", effective_date="2026-11-20",
    )
    rows = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    lifetime = next(row for row in rows if row["name"] == "home")["lifetime_id"]
    assert lifetime
    assert config.workspace("home")._lifetime_persisted is True

    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id="proj-alpha",
        project_name="Alpha", project_dir=project,
        source_id="stream:proj-alpha", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    grant = se.confirm(
        config, workspace="home", token=preview["preview_token"], session="operator-one"
    )["grant"]

    restarted = CiaoConfig.from_env(env)
    assert restarted.workspace("home").lifetime_id == lifetime
    assert restarted.workspace("home")._lifetime_persisted is True
    grants = se.list_grants(restarted, workspace="home")
    assert [row["grant_id"] for row in grants] == [grant["grant_id"]]
    assert grants[0]["status"] == "active"
    assert grants[0]["linked"] is True
    assert se.workspace_mode(restarted, workspace="home") == se.MODE_AUTOMATIC
    assert_eligibility_blocked(restarted)


def test_project_manager_binding_refuses_a_foreign_object() -> None:
    with pytest.raises(TypeError, match="engine's own"):
        se.bind_project_manager(object())


def _memory_manager(tmp_path: Path) -> tuple[CiaoConfig, ProjectChatManager, object, object]:
    install = tmp_path / "real-install"
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=install,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "home": WorkspaceConfig(name="home", vault_root="memory-vault/home"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    manager = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, install, config.media_root),
        transcript_store=TranscriptStore(runtime, install / "transcripts"),
        path=runtime / "web_projects.json",
    )
    project = manager.create_project("Alpha", workspace="home")
    memory_project = manager.create_project("Memory", workspace="home")
    memory_project.kind = memory_pass.MEMORY_PROJECT_KIND
    source = manager.create_chat(project.project_id, title="Source conversation")
    archive = config.chats_roots()[0] / source.chat_id / "claude" / "2026-11-20.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("## Turn 1\nUser: hello\n", encoding="utf-8")
    source.archived = True
    source.archive_path = str(archive)
    pass_chat = manager.create_chat(
        memory_project.project_id,
        helper={
            "kind": memory_pass.MEMORY_PASS_KIND,
            "source_chat_id": source.chat_id,
            "archive_path": str(archive),
            "state": "done",
            "archive_policy": "when_clean",
        },
    )
    return config, manager, project, source


async def test_real_memory_pass_completion_checkpoints_before_archival(tmp_path: Path) -> None:
    config, manager, project, source = _memory_manager(tmp_path)
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    project.vault_folder = "alpha"
    project_dir = Path(config.workspace_vault_root("home")) / "projects" / "active" / "alpha"
    project_dir.mkdir(parents=True)
    config.workspace("home").shared_brains = [SharedBrainLink(
        name="notes", path=str(tmp_path / "brain"), identity="github.com/acme/home",
        lifetime_id="brain_lifetime_fixed01",
    )]
    se.record_decision(
        config, workspace="home", project_id=project.project_id,
        decision="Baseline", reason="", effective_date="2026-11-20",
    )
    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id=project.project_id,
        project_name="Alpha", project_dir=project_dir,
        source_id=f"stream:{project.project_id}", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")
    se.record_operator_decision(
        config, workspace="home", project_id=project.project_id,
        decision="Synthetic decision", reason="", effective_date="2026-11-21",
        session="operator-one",
    )
    registry = AgentSessionRegistry()
    manager._mcp_service = SimpleNamespace(registry=registry)
    pass_chat = next(chat for chat in manager.list_chats() if (chat.helper or {}).get("kind") == "memory_pass")
    pass_chat.helper["state"] = "running"
    pass_chat.last_response = "Updated synthetic memory."
    pass_chat.last_response_status = "success"
    registry.issue(chat_id=pass_chat.chat_id, project_id=pass_chat.project_id, workspace="home", provider="claude")

    try:
        assert await manager._memory_pass.on_turn_finished(pass_chat.chat_id)
        if manager._detached_tasks:
            await asyncio.gather(*list(manager._detached_tasks))
        assert pass_chat.archived
        assert registry.principal_for_chat(pass_chat.chat_id) is None
        se._reset_project_manager_for_tests()
        restarted = ProjectChatManager(
            config,
            state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
            transcript_store=TranscriptStore(config.state_path.parent, config.workspace_root / "transcripts"),
            path=config.state_path.parent / "web_projects.json",
        )
        se.bind_project_manager(restarted)
        relation = se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=pass_chat.chat_id)
        assert relation.project_id == project.project_id
        assert relation.relation_id == pass_chat.chat_id
        assert source.chat_id == pass_chat.helper["source_chat_id"]
        rows = se.eligible_entries(config, workspace="home", brain_name="notes", relation=relation)
        assert [row["canonical_bytes"] for row in rows] == ['{"decision":"Synthetic decision"}']
        assert se.eligible_entries(config, workspace="work", brain_name="notes", relation=relation) == []
        se.revoke(config, workspace="home", grant_id=rows[0]["grant_id"])
        assert se.eligible_entries(config, workspace="home", brain_name="notes", relation=relation) == []
    finally:
        se._reset_project_manager_for_tests()


def test_done_without_checkpoint_cannot_recover_source_after_restart(tmp_path: Path) -> None:
    config, manager, _, _ = _memory_manager(tmp_path)
    pass_chat = next(chat for chat in manager.list_chats() if (chat.helper or {}).get("kind") == "memory_pass")
    pass_chat.last_response = "Updated synthetic memory."
    pass_chat.last_response_status = "success"
    manager._save()
    restarted = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(config.state_path.parent, config.workspace_root / "transcripts"),
        path=config.state_path.parent / "web_projects.json",
    )
    se._reset_project_manager_for_tests()
    se.bind_project_manager(restarted)
    try:
        with pytest.raises(se.EvidenceError, match="checkpoint is unavailable"):
            se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=pass_chat.chat_id)
    finally:
        se._reset_project_manager_for_tests()


def test_memory_completion_attestation_is_persisted_without_browser_exposure(tmp_path: Path) -> None:
    config, manager, _, _ = _memory_manager(tmp_path)
    chat = next(item for item in manager.list_chats() if (item.helper or {}).get("kind") == "memory_pass")
    chat.memory_completion_attestation = {"payload": {"source": "private"}, "signature": "signed"}
    manager._save()
    restarted = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(config.state_path.parent, config.workspace_root / "transcripts"),
        path=config.state_path.parent / "web_projects.json",
    )
    restored = restarted.get_chat(chat.chat_id)
    assert restored.memory_completion_attestation == chat.memory_completion_attestation
    assert "memory_completion_attestation" not in restored.to_dict()


def test_process_killed_after_done_recovers_completed_pass(tmp_path: Path) -> None:
    config, manager, project, _ = _memory_manager(tmp_path)
    config.workspace("home").shared_brains = [SharedBrainLink("notes", str(tmp_path / "brain"), "team")]
    se._workspace_lifetime(config, "home")
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    registry = AgentSessionRegistry()
    manager._mcp_service = SimpleNamespace(registry=registry)
    chat = next(item for item in manager.list_chats() if (item.helper or {}).get("kind") == "memory_pass")
    chat.helper["state"] = "running"
    chat.last_response = "Completed synthetic pass"
    chat.last_response_status = "success"
    manager._save()
    registry.issue(chat_id=chat.chat_id, project_id=chat.project_id, workspace="home", provider="claude")
    pid = os.fork()
    if pid == 0:
        original = manager._memory_pass._settle_success

        def kill_after_done(item, source_id, outcome):
            original(item, source_id, outcome)
            os.kill(os.getpid(), signal.SIGKILL)

        manager._memory_pass._settle_success = kill_after_done
        asyncio.run(manager._memory_pass.on_turn_finished(chat.chat_id))
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    restarted = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(config.state_path.parent, config.workspace_root / "transcripts"),
        path=config.state_path.parent / "web_projects.json",
    )
    se._reset_project_manager_for_tests()
    se.bind_project_manager(restarted)
    try:
        async def resume_and_wait():
            await restarted.resume_memory_passes()
            if restarted._detached_tasks:
                await asyncio.gather(*list(restarted._detached_tasks))

        asyncio.run(resume_and_wait())
        relation = se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=chat.chat_id)
        assert relation.project_id == project.project_id
    finally:
        se._reset_project_manager_for_tests()


async def test_checkpoint_survives_interruption_before_pass_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, manager, project, _ = _memory_manager(tmp_path)
    config.workspace("home").shared_brains = [SharedBrainLink("notes", str(tmp_path / "brain"), "team")]
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    registry = AgentSessionRegistry()
    manager._mcp_service = SimpleNamespace(registry=registry)
    chat = next(item for item in manager.list_chats() if (item.helper or {}).get("kind") == "memory_pass")
    chat.helper["state"] = "running"
    chat.last_response = "Updated synthetic memory."
    chat.last_response_status = "success"
    registry.issue(chat_id=chat.chat_id, project_id=chat.project_id, workspace="home", provider="claude")

    async def interrupted(_chat):
        raise RuntimeError("simulated interruption before archive")

    monkeypatch.setattr(manager._memory_pass, "_archive_finished", interrupted)
    try:
        with pytest.raises(RuntimeError, match="simulated interruption"):
            await manager._memory_pass.on_turn_finished(chat.chat_id)
        restarted = ProjectChatManager(
            config,
            state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
            transcript_store=TranscriptStore(config.state_path.parent, config.workspace_root / "transcripts"),
            path=config.state_path.parent / "web_projects.json",
        )
        se._reset_project_manager_for_tests()
        se.bind_project_manager(restarted)
        await restarted.resume_memory_passes()
        if restarted._detached_tasks:
            await asyncio.gather(*list(restarted._detached_tasks))
        assert restarted.get_chat(chat.chat_id).archived
        relation = se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=chat.chat_id)
        assert relation.project_id == project.project_id
    finally:
        se._reset_project_manager_for_tests()


def test_completed_memory_checkpoint_survives_registry_restart_and_revocation(tmp_path: Path) -> None:
    config, manager, project, source = _memory_manager(tmp_path)
    project.vault_folder = "alpha"
    project_dir = Path(config.workspace_vault_root("home")) / "projects" / "active" / "alpha"
    project_dir.mkdir(parents=True)
    config.workspace("home").shared_brains = [SharedBrainLink(
        name="notes", path=str(tmp_path / "brain"), identity="github.com/acme/home",
        lifetime_id="brain_lifetime_fixed01",
    )]
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    chat = next(item for item in manager.list_chats() if (item.helper or {}).get("kind") == memory_pass.MEMORY_PASS_KIND)
    chat.last_response = "Completed the pass"
    chat.last_response_status = "success"
    registry = AgentSessionRegistry()
    registry.issue(chat_id=chat.chat_id, project_id=chat.project_id, workspace="home", provider="claude")
    se.record_operator_decision(config, workspace="home", project_id=project.project_id, decision="Baseline", reason="", effective_date="2026-11-20", session="operator-one")
    preview = se.preview(config, workspace="home", brain_name="notes", project_id=project.project_id, project_name="Alpha", project_dir=project_dir, source_id=f"stream:{project.project_id}", grant_type="standing", field_mask=["decision"], producers=["memory_extraction"], session="operator-one")
    grant = se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")["grant"]
    se.record_operator_decision(config, workspace="home", project_id=project.project_id, decision="Later", reason="", effective_date="2026-11-21", session="operator-one")
    manager._save()
    se.checkpoint_completed_memory_pass(config, registry, workspace="home", pass_chat_id=chat.chat_id)
    registry.revoke_chat(chat.chat_id)
    restarted = ProjectChatManager(
        config, state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(config.state_path.parent, config.workspace_root / "transcripts"),
        path=config.state_path.parent / "web_projects.json",
    )
    se._reset_project_manager_for_tests()
    se.bind_project_manager(restarted)
    chat = restarted.get_chat(chat.chat_id)
    assert chat is not None
    relation = se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=chat.chat_id)
    assert relation.project_id == project.project_id
    assert relation.source_id == f"stream:{project.project_id}"
    assert [row["canonical_bytes"] for row in se.eligible_entries(config, workspace="home", brain_name="notes", relation=relation)] == ['{"decision":"Later"}']
    authorizations = se.mint_evidence_authorizations(config, workspace="home", brain_name="notes", relation=relation)
    assert len(authorizations) == 1
    authorization = authorizations[0]
    later = se.record_operator_decision(config, workspace="home", project_id=project.project_id, decision="Superseded", reason="", effective_date="2026-11-22", session="operator-one")
    newer = se.mint_evidence_authorizations(config, workspace="home", brain_name="notes", relation=relation)
    superseded = next(item for item in newer if item["payload"]["row"]["entry_id"] == later["entry"]["entry_id"])
    se.supersede_entry(config, workspace="home", project_id=project.project_id, entry_id=later["entry"]["entry_id"])
    with pytest.raises(se.EvidenceError):
        with se.evidence_authorization_guard(config, workspace="home", authorization=superseded):
            pass
    with patch.object(se, "_now", return_value=datetime.now(UTC) + timedelta(hours=2)):
        assert se.eligible_entries(config, workspace="home", brain_name="notes", relation=relation) == []
        with se.evidence_authorization_guard(config, workspace="home", authorization=authorization) as durable:
            assert durable["entry_revision"] == 2
    document = project_dir / se.DOCUMENT_NAME
    document.write_text("## Decision\nDecision: Snapshot\nEffective date: 2026-11-20\n", encoding="utf-8")
    snapshot = se.preview(config, workspace="home", brain_name="notes", project_id=project.project_id, project_name="Alpha", project_dir=project_dir, source_id=se.DOCUMENT_SOURCE, grant_type="snapshot", field_mask=["decision"], producers=["memory_extraction"], entry_index=0, session="operator-one")
    snapshot_grant = se.confirm(config, workspace="home", token=snapshot["preview_token"], session="operator-one")["grant"]
    snapshot_relation = se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=chat.chat_id, source_id=se.DOCUMENT_SOURCE)
    snapshot_authorization = se.mint_evidence_authorizations(config, workspace="home", brain_name="notes", relation=snapshot_relation)[0]
    document.unlink()
    with se.evidence_authorization_guard(config, workspace="home", authorization=snapshot_authorization) as snapshot_row:
        assert snapshot_row["canonical_bytes"] == '{"decision":"Snapshot"}'
        assert snapshot_row["entry_revision"] is None
    se.withdraw(config, workspace="home", grant_id=snapshot_grant["grant_id"], reason="withdrawn")
    with pytest.raises(se.EvidenceError):
        with se.evidence_authorization_guard(config, workspace="home", authorization=snapshot_authorization):
            pass
    with pytest.raises(se.EvidenceError):
        with se.evidence_authorization_guard(config, workspace="work", authorization=authorization):
            pass
    forged = json.loads(json.dumps(authorization))
    forged["payload"]["row"]["canonical_bytes"] = '{"decision":"forged"}'
    with pytest.raises(se.EvidenceError):
        with se.evidence_authorization_guard(config, workspace="home", authorization=forged):
            pass
    entered = threading.Event()
    finished = threading.Event()
    def revoke() -> None:
        entered.set()
        se.revoke(config, workspace="home", grant_id=grant["grant_id"])
        finished.set()
    with se.evidence_authorization_guard(config, workspace="home", authorization=authorization) as admitted:
        assert admitted["entry_revision"] == 2
        assert admitted["audience"] == "github.com/acme/home"
        thread = threading.Thread(target=revoke)
        thread.start()
        assert entered.wait(2)
        assert not finished.wait(0.05)
        lock_path = se._workspace_dir(config, "home") / ".state.lock"
        marker = tmp_path / "child-acquired"
        child = subprocess.Popen([sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'r+'); fcntl.flock(f, fcntl.LOCK_EX); open(sys.argv[2], 'w').close()", str(lock_path), str(marker)])
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=0.2)
        assert not marker.exists()
    assert child.wait(timeout=5) == 0
    assert marker.exists()
    thread.join(2)
    assert finished.is_set()
    with pytest.raises(se.EvidenceError):
        with se.evidence_authorization_guard(config, workspace="home", authorization=authorization):
            pass
    assert se.eligible_entries(config, workspace="home", brain_name="notes", relation=relation) == []
    with pytest.raises(se.EvidenceError):
        se.recover_memory_pass_source_relation(config, workspace="work", pass_chat_id=chat.chat_id)
    chat.last_response = "Changed"
    with pytest.raises(se.EvidenceError):
        se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=chat.chat_id)
    chat.last_response = "Completed the pass"
    archive = Path(source.archive_path)
    original = archive.read_bytes()
    archive.write_bytes(b"changed")
    with pytest.raises(se.EvidenceError):
        se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=chat.chat_id)
    archive.write_bytes(original)
    se.revoke_memory_pass_checkpoint(config, workspace="home", pass_chat_id=chat.chat_id)
    with pytest.raises(se.EvidenceError):
        se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=chat.chat_id)


def test_atomic_memory_attestation_recovers_after_done_without_live_principal(tmp_path: Path) -> None:
    config, manager, _project, _source = _memory_manager(tmp_path)
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    chat = next(item for item in manager.list_chats() if (item.helper or {}).get("kind") == memory_pass.MEMORY_PASS_KIND)
    chat.helper["state"] = "running"
    chat.last_response_status = "success"
    chat.last_response = "settled response"
    registry = AgentSessionRegistry()
    registry.issue(chat_id=chat.chat_id, project_id=chat.project_id, workspace="home", provider="claude")
    attestation = se.prepare_memory_completion(config, registry, workspace="home", pass_chat_id=chat.chat_id)
    with pytest.raises(se.EvidenceError):
        se._verified_memory_checkpoint(config, "home", chat.chat_id, checkpoint=attestation)
    chat.helper["state"] = "done"
    manager._save()
    restarted = ProjectChatManager(config, state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(config.state_path.parent, config.workspace_root / "transcripts"), path=config.state_path.parent / "web_projects.json")
    se._reset_project_manager_for_tests()
    se.bind_project_manager(restarted)
    se._verified_memory_checkpoint(config, "home", chat.chat_id, checkpoint=attestation)
    se._store_memory_checkpoint(config, "home", chat.chat_id, attestation)
    assert se.recover_memory_pass_source_relation(config, workspace="home", pass_chat_id=chat.chat_id).relation_id == chat.chat_id


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_care_witness_binds_exact_preview_revision_and_revocation(tmp_path: Path, newline: str) -> None:
    config, manager, project, _source = _memory_manager(tmp_path)
    project.vault_folder = "alpha"
    directory = Path(config.workspace_vault_root("home")) / "projects" / "active" / "alpha"
    directory.mkdir(parents=True)
    config.workspace("home").shared_brains = [SharedBrainLink(name="notes", path=str(tmp_path / "brain"), identity="team", lifetime_id="brain_lifetime_fixed01")]
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    document = directory / se.DOCUMENT_NAME
    text = newline.join(["## Decision", "Decision: Launch", "Effective date: 2026-11-20", ""])
    document.write_bytes(text.encode("utf-8"))
    relative = document.relative_to(config.workspace_vault_root("home")).as_posix()
    note = curation_run._ScannedNote(rendered=relative, relative=relative, title="Decisions", stale=True,
        exempt=False, age_days=120, threshold_days=90, last_verified=None)
    items, _, _ = curation_run._stale_note_items(
        vault_root=config.workspace_vault_root("home"), scanned=[note], today=datetime(2026, 11, 25).date(), scan_unavailable=False,
    )
    assert len(items) == 1 and items[0].target is not None
    revision = items[0].target.revision
    preview = se.preview(config, workspace="home", brain_name="notes", project_id=project.project_id,
        project_name="Alpha", project_dir=directory, source_id=se.DOCUMENT_SOURCE, grant_type="snapshot",
        field_mask=["decision"], producers=["nightly_curation"], entry_index=0, session="operator-one")
    subprocess.run([sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')", str(document), text + "changed"], check=True)
    grant = se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")["grant"]
    rows = se.care_snapshot_witnesses(config, workspace="home", project_id=project.project_id, relative_path=relative, revision=revision)
    assert len(rows) == 1 and rows[0]["grant_id"] == grant["grant_id"]
    assert rows[0]["entry_index"] == 0
    changed_items, _, _ = curation_run._stale_note_items(
        vault_root=config.workspace_vault_root("home"), scanned=[note], today=datetime(2026, 11, 25).date(), scan_unavailable=False,
    )
    assert len(changed_items) == 1 and changed_items[0].target is not None
    assert changed_items[0].target.revision != revision
    assert se.care_snapshot_witnesses(config, workspace="home", project_id=project.project_id, relative_path=relative, revision=changed_items[0].target.revision) == []
    assert se.care_snapshot_witnesses(config, workspace="work", project_id=project.project_id, relative_path=relative, revision=revision) == []
    old_created = project.created_at
    project.created_at = "2099-01-01T00:00:00Z"
    assert se.care_snapshot_witnesses(config, workspace="home", project_id=project.project_id, relative_path=relative, revision=revision) == []
    project.created_at = old_created
    old_link = config.workspace("home").shared_brains[0]
    config.workspace("home").shared_brains = [dataclasses.replace(old_link, lifetime_id="brain_lifetime_new0001")]
    assert se.care_snapshot_witnesses(config, workspace="home", project_id=project.project_id, relative_path=relative, revision=revision) == []
    config.workspace("home").shared_brains = [old_link]
    with se._workspace_guard(config, "home"):
        state = se._load_state(config, "home")
        saved = state["grants"][grant["grant_id"]]["care_witness"]
        state["grants"][grant["grant_id"]]["care_witness"] = None
        se._save_state(config, "home", state)
    assert se.care_snapshot_witnesses(config, workspace="home", project_id=project.project_id, relative_path=relative, revision=revision) == []
    with se._workspace_guard(config, "home"):
        state = se._load_state(config, "home")
        state["grants"][grant["grant_id"]]["care_witness"] = saved
        se._save_state(config, "home", state)
    se.revoke(config, workspace="home", grant_id=grant["grant_id"])
    assert se.care_snapshot_witnesses(config, workspace="home", project_id=project.project_id, relative_path=relative, revision=revision) == []


@pytest.mark.parametrize("cycle", [False, True])
@pytest.mark.parametrize("crash_point", ["before_state", "before_receipt", "before_ready"])
def test_care_checkpoint_stages_only_completed_exact_witness_and_replays_once(
    tmp_path: Path, cycle: bool, crash_point: str
) -> None:
    from ciao import curation_cycle, curation_share

    config, manager, project, _source = _memory_manager(tmp_path)
    project.vault_folder = "alpha"
    vault = Path(config.workspace_vault_root("home"))
    directory = vault / "projects" / "active" / "alpha"
    directory.mkdir(parents=True)
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    if cycle:
        state = curation_cycle.CycleState(
            cycle_id="c2-" + "0" * 24,
            prior_marker=curation_cycle.MarkerIdentity(kind="absent", sha256=None),
            required={}, optional={}, carried=(), legacy_historical={}, daily={},
            lease={}, last_run={}, pending=None,
        )
        curation_run.state_path(vault).write_text(curation_cycle.encode_state(state), encoding="utf-8")
    config.workspace("home").shared_brains = [SharedBrainLink(
        name="notes", path=str(tmp_path / "brain"), identity="team", lifetime_id="brain_lifetime_fixed01")]
    config.persist_workspace_registry()
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    document = directory / se.DOCUMENT_NAME
    text = "## Decision\nDecision: Launch\nEffective date: 2026-11-20\n"
    document.write_text(text, encoding="utf-8")
    preview = se.preview(config, workspace="home", brain_name="notes", project_id=project.project_id,
        project_name="Alpha", project_dir=directory, source_id=se.DOCUMENT_SOURCE, grant_type="snapshot",
        field_mask=["decision"], producers=["nightly_curation"], entry_index=0, session="operator-one")
    grant = se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")["grant"]
    relative = document.relative_to(vault).as_posix()
    key = curation_run.item_key(curation_run.PASS_STALE_NOTE, relative)
    target = curation_run.StaleNoteTarget(relative_path=relative, revision=se._digest(text.encode()))
    item = curation_run.WorklistItem(pass_id=curation_run.PASS_STALE_NOTE, label="note", reason="due", keys=(key,), target=target)
    lease = curation_share.begin(config, workspace="home", chat_id="care", token_id="token", dispatch_id="dispatch", ttl_s=600)
    curation_run.record_plan(vault, holder=lease.holder, plan=curation_run.lease_plan(
        curation_run.RunPlan(planned=(item,), deferred=(), budget=curation_run.RunBudget())))
    args = dict(workspace="home", chat_id="care", token_id="token", dispatch_id="dispatch", holder=lease.holder, key=key)
    assert curation_share.checkpoint(config, manager, **args) == 0
    subprocess.run([
        sys.executable, "-c",
        "import sys; from pathlib import Path; from ciao.curation_run import record_done; "
        "record_done(Path(sys.argv[1]), [sys.argv[3]], holder=sys.argv[2])",
        str(vault), lease.holder, key,
    ], check=True)
    assert curation_share.checkpoint(config, manager, **{**args, "dispatch_id": "foreign"}) == 0
    assert curation_share.checkpoint(config, manager, **args) == 1
    assert curation_share.checkpoint(config, manager, **args) == 1
    manager._save()
    restarted = ProjectChatManager(
        config, state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(config.state_path.parent, config.workspace_root / "transcripts"),
        path=config.state_path.parent / "web_projects.json",
    )
    se._reset_project_manager_for_tests()
    se.bind_project_manager(restarted)
    from ciao.control_plane import AgentPrincipal, CiaoControlPlane, ControlPlaneError

    plane = CiaoControlPlane(config, project_chat_manager=restarted, schedule_manager=SimpleNamespace())
    principal = AgentPrincipal(token_id="token", chat_id="care", project_id=project.project_id,
                               workspace="home", provider="claude")
    with pytest.raises(ControlPlaneError, match="live managed"):
        plane.managed_care_checkpoint(principal, lease.holder, key)
    records = list((config.state_path.parent / "secrets" / "share-evidence").rglob("care-outbox/*/*.json"))
    assert len(records) == 1
    assert "Launch" not in records[0].read_text()
    assert not (tmp_path / "brain").exists()
    capabilities = list((config.state_path.parent / "secrets" / "share-evidence").rglob("care-leases/*.json"))
    original = capabilities[0].read_text()
    tampered = json.loads(original)
    tampered["payload"]["chat_id"] = "foreign"
    capabilities[0].write_text(json.dumps(tampered))
    assert curation_share.checkpoint(config, manager, **args) == 0
    capabilities[0].write_text(original)
    assert curation_share.settled_witnesses(config, workspace="home", holder=lease.holder) == []
    script = "\n".join([
        "import json, os, sys",
        "from pathlib import Path",
        "from ciao.config import CiaoConfig, WorkspaceConfig, shared_brains_from_rows",
        "from ciao import curation_share",
        "root, vault, lifetime, holder, key, crash_point = sys.argv[1:]",
        "root = Path(root)",
        "config = CiaoConfig(pwa_auth_token='t', workspace_root=root, state_path=root / '.runtime/state.json', media_root=root / '.runtime/media', workspaces={'home': WorkspaceConfig(name='home', vault_root=vault, lifetime_id=lifetime, _lifetime_persisted=True)})",
        "raw = (root / '.runtime/workspaces.json').read_bytes()",
        "row = next(row for row in json.loads(raw) if row['name'] == 'home')",
        "config.workspace('home').shared_brains = shared_brains_from_rows(row.get('shared_brains'))",
        "config._workspace_registry_bytes = raw",
        "write = curation_share._write_record",
        "def interrupted(path, record):",
        "    if path.parent.name == 'care-settlements' and crash_point == 'before_receipt': os._exit(42)",
        "    if path.parent.name == 'care-ready' and crash_point == 'before_ready': os._exit(44)",
        "    write(path, record)",
        "curation_share._write_record = interrupted",
        "if crash_point == 'before_state':",
        "    from ciao import curation_run",
        "    store = curation_run._store",
        "    def before_state(path, state):",
        "        if not state.lease: os._exit(43)",
        "        store(path, state)",
        "    curation_run._store = before_state",
        "curation_share.end(config, workspace='home', chat_id='care', token_id='token', dispatch_id='dispatch', holder=holder, status='ok', planned=1, completed=1, deferred=0, reasons=[], live_keys=frozenset({key}), failed=0, counts_complete=True, dispatch_active=lambda: True)",
    ])
    crash = subprocess.run([sys.executable, "-c", script, str(config.workspace_root), str(vault),
                            config.workspace("home").lifetime_id, lease.holder, key,
                            crash_point], check=False)
    assert crash.returncode == {"before_state": 43, "before_receipt": 42, "before_ready": 44}[crash_point]
    if crash_point == "before_state":
        with se.evidence_claim(config, workspace="home") as claim:
            replacement = curation_run.begin_run(
                vault, holder="replacement", chat_id="care", now=datetime.now(UTC) + timedelta(days=1),
                before_acquire=lambda state: curation_share.recover(config, "home", state, claim),
            )
    else:
        replacement = curation_share.begin(config, workspace="home", chat_id="care", token_id="token",
                                           dispatch_id="replacement", ttl_s=600)
    receipts = list((config.state_path.parent / "secrets" / "share-evidence").rglob("care-settlements/*.json"))
    assert len(receipts) == (0 if crash_point == "before_state" else 1)
    if receipts:
        manifest = json.loads(receipts[0].read_text())["payload"]
        assert manifest["manifest_version"] == 1 and manifest["batch_width"] == 20
        assert len(manifest["outbox_manifest"]) == 1
        original = records[0].read_bytes()
        records[0].unlink()
        assert curation_share.settled_witnesses(config, workspace="home", holder=lease.holder) == []
        curation_share.dispatch_ready(config, workspace="home")
        assert list((config.state_path.parent / "secrets" / "share-evidence").rglob("care-ready/*.json"))
        records[0].write_bytes(original)
        records[0].chmod(0o600)
        extra = records[0].with_name("f" * 64 + ".json")
        extra.write_bytes(original)
        extra.chmod(0o600)
        assert curation_share.settled_witnesses(config, workspace="home", holder=lease.holder) == []
        extra.unlink()
    assert len(curation_share.settled_witnesses(config, workspace="home", holder=lease.holder)) == (0 if crash_point == "before_state" else 1)
    if crash_point != "before_state":
        with se.evidence_claim(config, workspace="home") as claim:
            settled = curation_share.settled_witnesses(config, workspace="home", holder=lease.holder, claim=claim)
            signature = settled[0]["witness"]["witness_signature"]
            relation = se.recover_care_source_relation(config, workspace="home", holder=lease.holder, witness_signature=signature, claim=claim)
            assert relation.batch_index == settled[0]["batch_index"] == 0
            with pytest.raises(se.EvidenceError):
                se.mint_evidence_authorizations(config, workspace="home", brain_name="notes", relation=dataclasses.replace(relation, batch_index=1), claim=claim)
            auths = se.mint_evidence_authorizations(config, workspace="home", brain_name="notes", relation=relation, claim=claim)
            assert len(auths) == 1
            with se.evidence_authorization_guard(config, workspace="home", authorization=auths[0], claim=claim) as row:
                assert row["canonical_bytes"] == '{"decision":"Launch"}'
                assert row["producer"] == "nightly_curation"
                assert row["batch_index"] == 0
            original_link = config.workspace("home").shared_brains[0]
            config.workspace("home").shared_brains = [dataclasses.replace(original_link, lifetime_id="brain_lifetime_new0001")]
            with pytest.raises(se.EvidenceError):
                with se.evidence_authorization_guard(config, workspace="home", authorization=auths[0], claim=claim):
                    pass
            config.workspace("home").shared_brains = [original_link]
            with pytest.raises(se.EvidenceError):
                se.recover_care_source_relation(config, workspace="home", holder=lease.holder, witness_signature="0" * 64, claim=claim)
        obtained = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []
        def reader() -> None:
            try:
                async def ordered() -> None:
                    from ciao import shared_brain
                    from ciao.workspaces import acquire_workspace_registry
                    path = str(tmp_path / "brain")
                    async with shared_brain._checkout_lock(path), shared_brain._checkout_claim(path, config):
                        owner = acquire_workspace_registry(config)
                        try:
                            with se.evidence_claim(config, workspace="home") as claim:
                                assert len(curation_share.settled_witnesses(config, workspace="home", holder=lease.holder, claim=claim)) == 1
                                foreign_thread_errors: list[str] = []
                                def check_foreign_thread() -> None:
                                    try:
                                        curation_share.settled_witnesses(config, workspace="home", holder=lease.holder, claim=claim)
                                    except se.EvidenceError as exc:
                                        foreign_thread_errors.append(exc.code)
                                foreign_thread = threading.Thread(target=check_foreign_thread)
                                foreign_thread.start()
                                foreign_thread.join(2)
                                assert foreign_thread_errors == ["invalid_evidence_claim"]
                                with pytest.raises(se.EvidenceError):
                                    curation_share.settled_witnesses(config, workspace="work", holder=lease.holder, claim=claim)
                                object.__setattr__(claim, "workspace", "work")
                                with pytest.raises(se.EvidenceError):
                                    curation_share.settled_witnesses(config, workspace="work", holder=lease.holder, claim=claim)
                                object.__setattr__(claim, "workspace", "home")
                                with pytest.raises(se.EvidenceError, match="existing evidence claim"):
                                    curation_share.settled_witnesses(config, workspace="home", holder=lease.holder)
                                obtained.set()
                                assert release.wait(5)
                            with pytest.raises(se.EvidenceError):
                                curation_share.settled_witnesses(config, workspace="home", holder=lease.holder, claim=claim)
                        finally:
                            owner.close()
                asyncio.run(ordered())
            except BaseException as exc:
                errors.append(exc)
                obtained.set()
        def revoke() -> None:
            se.revoke(config, workspace="home", grant_id=grant["grant_id"])
            finished.set()
        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        assert obtained.wait(3), "nested settlement lookup deadlocked"
        assert not errors
        revoker = threading.Thread(target=revoke, daemon=True)
        revoker.start()
        assert not finished.wait(0.1)
        ready = tmp_path / "revoker-ready"
        script = "from pathlib import Path; import sys; from ciao.config import CiaoConfig, WorkspaceConfig; from ciao import share_evidence as se; root=Path(sys.argv[1]); cfg=CiaoConfig(pwa_auth_token='t',workspace_root=root,state_path=root / '.runtime/state.json',media_root=root / '.runtime/media',workspaces={'home': WorkspaceConfig(name='home',vault_root=sys.argv[2],lifetime_id=sys.argv[3],_lifetime_persisted=True)}); Path(sys.argv[5]).touch(); se.revoke(cfg,workspace='home',grant_id=sys.argv[4])"
        child = subprocess.Popen([sys.executable, "-c", script, str(config.workspace_root), str(vault), config.workspace("home").lifetime_id, grant["grant_id"], str(ready)])
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=0.2)
        release.set()
        reader_thread.join(3)
        revoker.join(3)
        assert not reader_thread.is_alive() and not revoker.is_alive()
        assert not errors and finished.is_set()
        assert child.wait(timeout=10) == 0
        with pytest.raises(se.EvidenceError):
            with se.evidence_authorization_guard(config, workspace="home", authorization=auths[0]):
                pass
    se.revoke(config, workspace="home", grant_id=grant["grant_id"])
    assert curation_share.settled_witnesses(config, workspace="home", holder=lease.holder) == []
    assert curation_share.checkpoint(config, manager, **args) == 0
    curation_run.end_run(vault, holder=replacement.holder)


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_document_revision_matches_planner_bytes(tmp_path: Path, newline: str) -> None:
    from ciao.memory_receipts import content_revision

    document = tmp_path / "Decisions.md"
    document.write_bytes(newline.join(["## Decision", "Decision: Launch", "Effective date: 2026-11-20", ""]).encode())
    entries, revision = se._read_document(document)
    assert entries[0]["decision"] == "Launch"
    assert revision == content_revision(document.read_bytes().decode("utf-8"))


def test_care_batch_indices_do_not_shift_when_earlier_witness_is_withdrawn(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import curation_share
    import hashlib

    holder = "synthetic-holder"
    run_id = hashlib.sha256(holder.encode()).hexdigest()
    witnesses = []
    with se.evidence_claim(config, workspace="home") as claim:
        receipts = curation_share._private_dir(claim.directory, "care-settlements", create=True)
        root = curation_share._private_dir(claim.directory, "care-outbox", create=True)
        assert receipts is not None and root is not None
        outbox = curation_share._private_dir(root, run_id, create=True)
        assert outbox is not None
        manifest = []
        for index in range(21):
            witness = {"project_id": "proj-alpha", "source_path": "p/Decisions.md", "source_revision": str(index), "witness_signature": f"{index:064x}"}
            item = {"workspace": "home", "holder": holder, "dispatch_id": "dispatch", "lease_started_at": "start", "lease_proof_hash": "proof", "witness": witness}
            encoded = json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
            name = hashlib.sha256(encoded).hexdigest() + ".json"
            curation_share._write_record(outbox / name, curation_share._seal(config, b"care-outbox\0", item))
            manifest.append({"name": name, "witness_signature": witness["witness_signature"],
                             "destination": {"status": "not_mapped", "brain": "notes"}})
            witnesses.append(witness)
        manifest.sort(key=lambda row: row["name"])
        settlement = {"workspace": "home", "workspace_lifetime_id": config.lifetimes["home"], "holder": holder, "status": "ok", "dispatch_id": "dispatch", "lease_started_at": "start", "lease_proof_hash": "proof", "manifest_version": 1, "batch_width": 20, "outbox_manifest": manifest}
        curation_share._write_record(receipts / (run_id + ".json"), curation_share._seal(config, b"care-settlement\0", settlement))
    active = {w["witness_signature"] for w in witnesses}
    monkeypatch.setattr(se, "care_snapshot_witnesses", lambda _config, **kw: [w for w in witnesses if w["witness_signature"] in active and w["source_revision"] == kw["revision"]])
    before = curation_share.settled_witnesses(config, workspace="home", holder=holder)
    assert len(before) == 21 and before[-1]["batch_index"] == 1
    active.remove(before[0]["witness"]["witness_signature"])
    after = curation_share.settled_witnesses(config, workspace="home", holder=holder)
    assert len(after) == 20 and after[-1]["batch_index"] == 1
    (outbox / manifest[0]["name"]).unlink()
    assert curation_share.settled_witnesses(config, workspace="home", holder=holder) == []


def test_care_private_storage_rejects_a_symlink(tmp_path: Path) -> None:
    from ciao import curation_share

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (tmp_path / "care-outbox").symlink_to(outside, target_is_directory=True)
    with pytest.raises(se.EvidenceError, match="unsafe"):
        curation_share._private_dir(tmp_path, "care-outbox", create=True)


def test_care_acquires_evidence_before_lease_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl
    from contextlib import contextmanager
    from ciao import curation_share, memory_receipts

    config, manager, _project, _source = _memory_manager(tmp_path)
    vault = Path(config.workspace_vault_root("home"))
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    original = se.evidence_claim
    checks: list[str] = []

    @contextmanager
    def checked(config, *, workspace):
        path = memory_receipts.lock_path_for(str(curation_run.state_path(vault).resolve()))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        checks.append(workspace)
        with original(config, workspace=workspace) as claim:
            yield claim

    monkeypatch.setattr(se, "evidence_claim", checked)
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    lease = curation_share.begin(config, workspace="home", chat_id="care", token_id="token", dispatch_id="dispatch", ttl_s=600)
    relative = "People/Example.md"
    key = curation_run.item_key(curation_run.PASS_STALE_NOTE, relative)
    item = curation_run.WorklistItem(pass_id=curation_run.PASS_STALE_NOTE, label="note", reason="due", keys=(key,),
                                     target=curation_run.StaleNoteTarget(relative_path=relative, revision="a" * 64))
    curation_run.record_plan(vault, holder=lease.holder, plan=curation_run.lease_plan(
        curation_run.RunPlan(planned=(item,), deferred=(), budget=curation_run.RunBudget())))
    curation_run.record_done(vault, [key], holder=lease.holder)
    assert curation_share.checkpoint(config, manager, workspace="home", chat_id="care", token_id="token",
                                     dispatch_id="dispatch", holder=lease.holder, key=key) == 0
    curation_share.end(config, workspace="home", chat_id="care", token_id="token", dispatch_id="dispatch",
                       holder=lease.holder, status="ok", planned=0, completed=0, deferred=0,
                       reasons=[], live_keys=frozenset(), failed=0, counts_complete=True, dispatch_active=lambda: True)
    assert len(checks) >= 2


def test_real_memory_pass_archive_proves_positive_stream_eligibility(
    tmp_path: Path,
) -> None:
    config, manager, project, source = _memory_manager(tmp_path)
    project.vault_folder = "alpha"
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    registry = AgentSessionRegistry()
    project_dir = Path(config.workspace_vault_root("home")) / "projects" / "active" / "alpha"
    project_dir.mkdir(parents=True)
    config.workspace("home").shared_brains = [SharedBrainLink(
        name="notes", path=str(tmp_path / "brain"), identity="github.com/acme/home",
        lifetime_id="brain_lifetime_fixed01",
    )]
    se.record_decision(
        config, workspace="home", project_id=project.project_id,
        decision="Baseline", reason="", effective_date="2026-11-20",
    )
    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id=project.project_id,
        project_name="Alpha", project_dir=project_dir,
        source_id=f"stream:{project.project_id}", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")
    pass_chat = next(
        item for item in manager.list_chats()
        if memory_pass.MEMORY_PASS_KIND
        == (item.helper or {}).get("kind")
    )
    registry.issue(
        chat_id=pass_chat.chat_id, project_id=pass_chat.project_id,
        workspace="home", provider="claude",
    )
    pass_chat.helper["state"] = "running"
    with pytest.raises(se.EvidenceError, match="not complete"):
        se.memory_pass_source_relation(
            config, registry, workspace="home", pass_chat_id=pass_chat.chat_id,
        )
    pass_chat.helper["state"] = "done"
    with pytest.raises(se.EvidenceError, match="managed pass"):
        se.memory_pass_source_relation(
            config, AgentSessionRegistry(), workspace="home", pass_chat_id=pass_chat.chat_id,
        )
    relation = se.memory_pass_source_relation(
        config, registry, workspace="home", pass_chat_id=pass_chat.chat_id,
    )
    se.record_operator_decision(
        config, workspace="home", project_id=project.project_id,
        decision="Later decision", reason="", effective_date="2026-11-21",
        session="operator-one",
    )
    rows = se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=relation,
    )
    assert [row["canonical_bytes"] for row in rows] == ['{"decision":"Later decision"}']
    assert rows[0]["producer"] == "memory_extraction"
    config.workspace("home").shared_brains.append(SharedBrainLink(
        name="notes-two", path=str(tmp_path / "brain-two"),
        identity="github.com/acme/home-two", lifetime_id="brain_lifetime_fixed02",
    ))
    second_preview = se.preview(
        config, workspace="home", brain_name="notes-two", project_id=project.project_id,
        project_name="Alpha", project_dir=project_dir,
        source_id=f"stream:{project.project_id}", grant_type="standing",
        field_mask=["reason"], producers=["memory_extraction"], session="operator-one",
    )
    se.confirm(
        config, workspace="home", token=second_preview["preview_token"], session="operator-one"
    )
    se.record_operator_decision(
        config, workspace="home", project_id=project.project_id,
        decision="Second decision", reason="because", effective_date="2026-11-21",
        session="operator-one",
    )
    first_bytes = [
        row["canonical_bytes"] for row in se.eligible_entries(
            config, workspace="home", brain_name="notes", relation=relation,
        )
    ]
    second_bytes = [
        row["canonical_bytes"] for row in se.eligible_entries(
            config, workspace="home", brain_name="notes-two", relation=relation,
        )
    ]
    assert first_bytes == [
        '{"decision":"Later decision"}', '{"decision":"Second decision"}'
    ]
    assert second_bytes == ['{"reason":"because"}']
    (project_dir / se.DOCUMENT_NAME).write_text(
        "## Decision\nDecision: Snapshot decision\nEffective date: 2026-11-21\n",
        encoding="utf-8",
    )
    snapshot_preview = se.preview(
        config, workspace="home", brain_name="notes", project_id=project.project_id,
        project_name="Alpha", project_dir=project_dir,
        source_id=se.DOCUMENT_SOURCE, grant_type="snapshot",
        field_mask=["decision", "effective_date"], producers=["memory_extraction"],
        entry_index=0, session="operator-one",
    )
    se.confirm(
        config, workspace="home", token=snapshot_preview["preview_token"], session="operator-one"
    )
    (project_dir / se.DOCUMENT_NAME).unlink()
    document_relation = se.memory_pass_source_relation(
        config, registry, workspace="home", pass_chat_id=pass_chat.chat_id,
        source_id=se.DOCUMENT_SOURCE,
    )
    snapshot_rows = se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=document_relation,
    )
    assert [row["canonical_bytes"] for row in snapshot_rows] == [
        '{"decision":"Snapshot decision","effective_date":"2026-11-21"}'
    ]
    assert snapshot_rows[0]["grant_type"] == "snapshot"
    (project_dir / se.DOCUMENT_NAME).write_text(
        "## Decision\nDecision: Replacement\nEffective date: 2026-12-01\n",
        encoding="utf-8",
    )
    assert [row["canonical_bytes"] for row in se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=document_relation,
    )] == ['{"decision":"Snapshot decision","effective_date":"2026-11-21"}']
    assert rows[0]["relation_kind"] == "memory_pass_chat"
    assert rows[0]["relation_id"] == pass_chat.chat_id

    # A retargeted relation, a foreign workspace and an expired context resolve nothing.
    with pytest.raises(se.EvidenceError, match="not engine-attested"):
        se.eligible_entries(
            config, workspace="home", brain_name="notes",
            relation=dataclasses.replace(relation, project_id="proj-foreign"),
        )
    assert se.eligible_entries(
        config, workspace="work", brain_name="notes", relation=relation,
    ) == []
    assert se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=relation,
        now=datetime.now(UTC) + timedelta(hours=2),
    ) == []
    # A forged dataclass with no seal is refused, not silently empty.
    forged = dataclasses.replace(relation, seal=b"")
    with pytest.raises(se.EvidenceError, match="not engine-attested"):
        se.eligible_entries(config, workspace="home", brain_name="notes", relation=forged)

    # Revocation and workspace lifetime both invalidate the positive source.
    se.revoke(config, workspace="home", grant_id=rows[0]["grant_id"])
    assert se.eligible_entries(config, workspace="home", brain_name="notes", relation=relation) == []
    config.workspace("home").lifetime_id = "wsl_memory_recreated_0002"
    assert se.eligible_entries(config, workspace="home", brain_name="notes", relation=relation) == []


def test_curation_lease_work_item_cannot_authorize_unattributed_snapshot(
    config: Config, tmp_path: Path
) -> None:
    document = config.project_dirs["home"] / se.DOCUMENT_NAME
    document.write_text(
        "## Decision\nDecision: Ship the beta\nEffective date: 2026-11-21\n", encoding="utf-8"
    )
    grant = live_grant(
        config, source_id=se.DOCUMENT_SOURCE, grant_type="snapshot",
        field_mask=["decision", "effective_date"], entry_index=0,
    )["grant"]
    vault = config.vaults["home"]
    from ciao import curation_cycle as cc
    from ciao.web.project_chats import ProjectInfo

    real_config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=config.state_path,
        media_root=tmp_path / "media",
        workspaces={"home": WorkspaceConfig(name="home", vault_root=str(vault))},
    )
    manager = ProjectChatManager(
        real_config,
        state_store=StateStore(real_config.state_path, tmp_path, real_config.media_root),
        transcript_store=TranscriptStore(tmp_path / ".runtime", tmp_path / "transcripts"),
        path=tmp_path / ".runtime" / "web_projects.json",
    )
    manager._projects["proj-alpha"] = ProjectInfo(
        project_id="proj-alpha", name="Alpha", workspace="home", vault_folder="alpha"
    )
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    moment = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    cycle = cc.CycleState(
        cycle_id="c2-" + "0" * 24,
        prior_marker=cc.MarkerIdentity(kind="absent", sha256=None),
        required={}, optional={}, carried=(), legacy_historical={}, daily={},
        lease={}, last_run={}, pending=None,
    )
    vault.joinpath("Workspace").mkdir(parents=True, exist_ok=True)
    curation_run.state_path(vault).write_text(cc.encode_state(cycle), encoding="utf-8")
    lease = curation_run.begin_run(
        vault, holder="curator", ttl_s=3600, chat_id="curation-chat", now=moment
    )
    registry = AgentSessionRegistry()
    token, _principal = registry.issue(
        chat_id="curation-chat", project_id="proj-general",
        workspace="home", provider="claude",
    )
    readme = config.project_dirs["home"] / "README.md"
    readme.write_text("# Alpha\n", encoding="utf-8")
    # The plan names Decisions.md; README is present but must not stand in.
    relative = document.relative_to(vault).as_posix()
    key = curation_run.item_key(curation_run.PASS_STALE_NOTE, relative)
    curation_run.record_plan(
        vault, holder=lease.holder,
        plan={"planned": [[curation_run.PASS_STALE_NOTE, key]], "blocked": [],
              "blocked_overflow": 0, "deferred_count": 0},
        now=moment,
    )
    relation = se.curation_source_relation(
        config, registry, vault, workspace="home", project_id="proj-alpha",
        source_id=se.DOCUMENT_SOURCE, lease=lease, agent_token=token, now=moment,
    )
    rows = se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=relation, now=moment,
    )
    assert rows == []
    # A later process can load the private capability from the public holder.
    capability = curation_run.load_lease_capability(vault, lease.holder, now=moment)
    assert capability is not None and capability.proof == lease.proof
    assert "proof" not in lease.as_dict()
    capability_file = curation_run._capability_path(vault, lease.holder)
    assert capability_file.stat().st_mode & 0o077 == 0
    with pytest.raises(se.EvidenceError, match="curation lease proof"):
        se.curation_source_relation(
            config, registry, vault, workspace="home", project_id="proj-alpha",
            source_id=se.DOCUMENT_SOURCE, agent_token=token,
            lease=lease.holder, now=moment,
        )
    with pytest.raises(se.EvidenceError, match="curation lease proof"):
        se.curation_source_relation(
            config, registry, vault, workspace="home", project_id="proj-alpha",
            source_id=se.DOCUMENT_SOURCE, agent_token=token,
            lease=dataclasses.replace(lease, proof="wrong"), now=moment,
        )
    with pytest.raises(se.EvidenceError, match="curation lease proof"):
        se.curation_source_relation(
            config, registry, vault, workspace="home", project_id="proj-alpha",
            source_id=se.DOCUMENT_SOURCE, agent_token=token,
            lease=dataclasses.replace(lease, proof_hash="0" * 64), now=moment,
        )
    wrong_token, _ = registry.issue(
        chat_id="other-chat", project_id="proj-alpha",
        workspace="home", provider="claude",
    )
    with pytest.raises(se.EvidenceError, match="lease-owning agent"):
        se.curation_source_relation(
            config, registry, vault, workspace="home", project_id="proj-alpha",
            source_id=se.DOCUMENT_SOURCE, lease=lease,
            agent_token=wrong_token, now=moment,
        )
    assert grant["status"] == "active"
    assert relation.producer == "nightly_curation"
    assert relation.evidence == key
    document.unlink()
    # The confirmed snapshot remains unauthorized without fact attribution.
    deleted_relation = se.curation_source_relation(
        config, registry, vault, workspace="home", project_id="proj-alpha",
        source_id=se.DOCUMENT_SOURCE, lease=lease, agent_token=token, now=moment,
    )
    assert se.eligible_entries(
        config, workspace="home", brain_name="notes",
        relation=deleted_relation, now=moment,
    ) == []
    # A later replacement document is not substituted into the same snapshot.
    document.write_text(
        "## Decision\nDecision: Replacement\nEffective date: 2026-12-01\n", encoding="utf-8"
    )
    assert se.eligible_entries(
        config, workspace="home", brain_name="notes",
        relation=deleted_relation, now=moment,
    ) == []
    curation_run.end_run(vault, holder=lease.holder, now=moment)
    assert curation_run.load_lease_capability(vault, lease.holder, now=moment) is None
    assert se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=relation, now=moment,
    ) == []
    assert se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=relation,
        now=moment + timedelta(hours=2),
    ) == []
    config.lifetimes["home"] = "wsl_curation_recreated_0002"
    assert se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=relation, now=moment,
    ) == []
    with pytest.raises(se.EvidenceError, match="not engine-attested"):
        se.eligible_entries(
            config, workspace="home", brain_name="notes",
            relation=dataclasses.replace(relation, source_id="stream:proj-alpha"),
        )
    # A lease plan for a different project is not proof for this one.
    beta = _second_project(config)
    (beta / se.DOCUMENT_NAME).write_text(
        "## Decision\nDecision: Other\nEffective date: 2026-11-21\n", encoding="utf-8"
    )
    beta_key = curation_run.item_key(
        curation_run.PASS_STALE_NOTE,
        (beta / se.DOCUMENT_NAME).relative_to(vault).as_posix(),
    )
    other_lease = curation_run.begin_run(
        vault, holder="other", ttl_s=3600, chat_id="curation-chat",
        now=moment + timedelta(hours=3),
    )
    curation_run.record_plan(
        vault, holder=other_lease.holder,
        plan={"planned": [[curation_run.PASS_STALE_NOTE, beta_key]],
              "blocked": [], "blocked_overflow": 0, "deferred_count": 0},
        now=moment + timedelta(hours=3),
    )
    with pytest.raises(se.EvidenceError, match="does not include"):
        se.curation_source_relation(
            config, registry, vault, workspace="home", project_id="proj-alpha",
            source_id=se.DOCUMENT_SOURCE, lease=other_lease, agent_token=token,
            now=moment + timedelta(hours=3),
        )


def test_authenticated_route_decision_is_positive_and_forgery_is_refused(tmp_path: Path) -> None:
    config, manager, project, source = _memory_manager(tmp_path)
    project.vault_folder = "alpha"
    project_dir = Path(config.workspace_vault_root("home")) / "projects" / "active" / "alpha"
    project_dir.mkdir(parents=True)
    config.workspace("home").shared_brains = [SharedBrainLink(
        name="notes", path=str(tmp_path / "brain"), identity="github.com/acme/home",
        lifetime_id="brain_lifetime_fixed01",
    )]
    se._reset_project_manager_for_tests()
    se.bind_project_manager(manager)
    pass_chat = next(
        item for item in manager.list_chats()
        if memory_pass.MEMORY_PASS_KIND == (item.helper or {}).get("kind")
    )
    registry = AgentSessionRegistry()
    registry.issue(
        chat_id=pass_chat.chat_id, project_id=pass_chat.project_id,
        workspace="home", provider="claude",
    )
    se.record_operator_decision(
        config, workspace="home", project_id=project.project_id,
        decision="Baseline", reason="", effective_date="2026-11-20",
        session="operator-one",
    )
    preview = se.preview(
        config, workspace="home", brain_name="notes", project_id=project.project_id,
        project_name="Alpha", project_dir=project_dir,
        source_id=f"stream:{project.project_id}", grant_type="standing",
        field_mask=["decision"], producers=["memory_extraction"], session="operator-one",
    )
    se.confirm(config, workspace="home", token=preview["preview_token"], session="operator-one")

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route(
            "/api/workspaces/{name}/share-evidence/decisions",
            routes_api.share_evidence_record_decision, methods=["POST"],
        )],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.project_chat_manager = manager
    client = TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 50000))
    session = headers()["Cookie"].split("=", 1)[1]
    _, tokens = se.disclosure_state(config, workspace="home", projects=[project.project_id], session=session)
    response = client.post(
        "/api/workspaces/home/share-evidence/decisions",
        headers=headers(),
        json={
            "project_id": project.project_id,
            "decision": "Route-authored decision",
            "reason": "",
            "effective_date": "2026-11-21",
            "disclosure_token": tokens[project.project_id],
        },
    )
    assert response.status_code == 200, response.text

    relation = se.memory_pass_source_relation(
        config, registry, workspace="home", pass_chat_id=pass_chat.chat_id,
    )
    rows = se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=relation,
    )
    assert [row["canonical_bytes"] for row in rows] == ['{"decision":"Route-authored decision"}']
    entry_id = rows[0]["entry_id"]

    # Editing the stored event text without its engine signature makes it inert.
    state = se._load_state(config, "home")
    stream = state["streams"][project.project_id]
    entry = next(item for item in stream["entries"] if item["entry_id"] == entry_id)
    assert entry["provenance"]["operator_event"]["signature"]
    entry["decision"] = "Forged replacement"
    se._write_private(
        se._state_path(config, "home"),
        json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        limit=se.MAX_STATE_BYTES,
    )
    assert se.eligible_entries(
        config, workspace="home", brain_name="notes", relation=relation,
    ) == []


def test_v1_legacy_lease_has_no_cross_process_capability(tmp_path: Path) -> None:
    vault = tmp_path / "legacy-vault"
    (vault / "Workspace").mkdir(parents=True)
    lease = curation_run.begin_run(vault, holder="legacy", ttl_s=3600)
    persisted = json.loads(curation_run.state_path(vault).read_text(encoding="utf-8"))
    assert "proof_hash" not in persisted["lease"]
    assert curation_run.load_lease_capability(vault, lease.holder) is None


def test_curation_cli_publishes_only_a_private_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import date
    from types import SimpleNamespace

    from ciao import curation_cycle as cc
    from ciao import curation_run
    from ciao import cli
    from ciao.memory_tool import ensure_regions

    root = tmp_path / "install"
    vault = root / "memory-vault" / "home"
    (vault / "Workspace").mkdir(parents=True)
    (root / ".runtime").mkdir(parents=True)
    (root / ".runtime" / "workspaces.json").write_text(
        json.dumps([{"name": "home", "vault_root": str(vault)}]), encoding="utf-8"
    )
    guide = root / "CLAUDE.md"
    guide.write_text("# Workspace\n", encoding="utf-8", newline="")
    ensure_regions(guide)
    (vault / curation_run.CURATION_LOG_RELATIVE).write_text(
        f"---\nlast_full_pass: {date.today().isoformat()}\n---\n\n# Curation log\n",
        encoding="utf-8", newline="",
    )
    notes = vault / "Journals"
    notes.mkdir()
    for index in range(3):
        (notes / f"Note{index}.md").write_text(
            "---\ntype: recipe-book\n---\n# Note\n", encoding="utf-8", newline=""
        )
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(workspace=str(root)),
    )
    cycle = cc.CycleState(
        cycle_id="c2-" + "1" * 24,
        prior_marker=cc.MarkerIdentity(kind="absent", sha256=None),
        required={}, optional={}, carried=(), legacy_historical={}, daily={},
        lease={}, last_run={}, pending=None,
    )
    curation_run.state_path(vault).write_text(cc.encode_state(cycle), encoding="utf-8")

    capsys.readouterr()
    code = cli.main(["curation-begin", "--vault-root", str(vault), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    holder = payload["lease"]["holder"]
    assert "proof" not in payload["lease"]
    assert "proof_hash" not in payload["lease"]
    capability = curation_run.load_lease_capability(vault, holder)
    assert capability is not None
    assert capability.proof
    assert capability.proof not in captured.out + captured.err
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo)
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys; from pathlib import Path; from ciao import curation_run; "
         "lease = curation_run.load_lease_capability(Path(sys.argv[1]), sys.argv[2]); "
         "print('OK' if lease is not None and lease.proof else 'NO')",
         str(vault), holder],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "OK"
    assert capability.proof not in child.stdout + child.stderr
