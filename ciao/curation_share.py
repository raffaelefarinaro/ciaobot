"""Private engine-owned capabilities for managed Workspace care."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from ciao import curation_run, share_evidence

MAX_WITNESSES_PER_ITEM = 20
MAX_CHECKPOINTS_PER_RUN = 100


def begin(config: Any, *, workspace: str, chat_id: str, token_id: str,
          dispatch_id: str, ttl_s: float) -> curation_run.Lease:
    """Acquire and privately attest the lease created by an active care dispatch."""
    vault = Path(config.workspace_vault_root(workspace))
    lease = curation_run.begin_run(vault, ttl_s=ttl_s, chat_id=chat_id)
    try:
        with share_evidence._workspace_guard(config, workspace) as directory:
            payload = {
                "workspace": workspace, "workspace_lifetime_id": share_evidence._workspace_lifetime(config, workspace),
                "chat_id": chat_id, "token_id": token_id,
                "dispatch_id": dispatch_id, "holder": lease.holder,
                "started_at": lease.started_at, "expires_at": lease.expires_at,
                "vault": str(vault.resolve()), "proof": lease.proof,
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            signature = hmac.new(share_evidence._relation_secret(config), b"care-lease\0" + encoded,
                                 hashlib.sha256).hexdigest()
            name = hashlib.sha256(lease.holder.encode("utf-8")).hexdigest() + ".json"
            data = json.dumps({"schema": share_evidence.SCHEMA, "payload": payload, "signature": signature}, sort_keys=True).encode("utf-8")
            share_evidence._write_private(directory / "care-leases" / name, data, limit=16384)
    except Exception:
        curation_run.end_run(vault, holder=lease.holder, status="error")
        raise
    return lease


def checkpoint(config: Any, manager: Any, *, workspace: str, chat_id: str,
               token_id: str, dispatch_id: str, holder: str, key: str) -> int:
    """Stage signed witness references for one completed live care item, without effects."""
    vault = Path(config.workspace_vault_root(workspace))
    name = hashlib.sha256(holder.encode("utf-8")).hexdigest() + ".json"
    with share_evidence._workspace_guard(config, workspace) as directory:
        record = share_evidence._read_json(directory / "care-leases" / name, limit=16384,
                                           code="private_state_corrupt")
        payload = record.get("payload")
        signature = record.get("signature")
        if not isinstance(payload, dict) or not isinstance(signature, str):
            return 0
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(share_evidence._relation_secret(config), b"care-lease\0" + encoded,
                            hashlib.sha256).hexdigest()
        if (not hmac.compare_digest(signature, expected) or payload.get("workspace") != workspace
            or payload.get("workspace_lifetime_id") != share_evidence._workspace_lifetime(config, workspace)
            or payload.get("chat_id") != chat_id or payload.get("token_id") != token_id
            or payload.get("dispatch_id") != dispatch_id or payload.get("holder") != holder
            or payload.get("vault") != str(vault.resolve())):
            return 0

    with curation_run._state_lock(vault):
        state = curation_run._load(curation_run.state_path(vault))
        held = curation_run._live_lease(state, curation_run._utc_now())
        if (not held or held.get("holder") != holder or held.get("chat_id") != chat_id
            or held.get("started_at") != payload.get("started_at")):
            return 0
        proof = payload.get("proof")
        if not isinstance(proof, str) or not proof:
            return 0
        proof_hash = hashlib.sha256(proof.encode("utf-8")).hexdigest()
        if "proof_hash" in held and held["proof_hash"] != proof_hash:
            return 0
        plan = held.get("plan")
        if not isinstance(plan, dict) or key not in plan.get("completed_stale_notes", []):
            return 0
        target = plan.get("stale_note_targets", {}).get(key)
        if not isinstance(target, dict):
            return 0
        relative, revision = target["relative_path"], target["revision"]
        found: list[dict[str, Any]] = []
        for project in manager.list_projects(workspace=workspace):
            witnesses = share_evidence.care_snapshot_witnesses(
                config, workspace=workspace, project_id=project.project_id,
                relative_path=relative, revision=revision,
            )
            found.extend(witnesses)
            if len(found) > MAX_WITNESSES_PER_ITEM:
                return 0
        for witness in found:
            if not curation_run._holds_live_lease_now(vault, holder=holder, clock=curation_run._utc_now):
                return 0
            item = {"workspace": workspace, "dispatch_id": dispatch_id, "holder": holder,
                    "lease_started_at": held["started_at"], "lease_proof_hash": proof_hash,
                    "work_item": key, "witness": witness}
            encoded_item = json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
            seal = hmac.new(share_evidence._relation_secret(config), b"care-outbox\0" + encoded_item,
                            hashlib.sha256).hexdigest()
            run_id = hashlib.sha256(holder.encode("utf-8")).hexdigest()
            item_name = hashlib.sha256(encoded_item).hexdigest() + ".json"
            with share_evidence._workspace_guard(config, workspace) as directory:
                outbox = directory / "care-outbox" / run_id
                if outbox.exists() and not (outbox / item_name).exists():
                    with os.scandir(outbox) as rows:
                        if sum(1 for _ in rows) >= MAX_CHECKPOINTS_PER_RUN:
                            return 0
                share_evidence._write_private(
                    outbox / item_name,
                    json.dumps({"payload": item, "signature": seal}, sort_keys=True).encode("utf-8"),
                    limit=16384,
                )
        return len(found)
