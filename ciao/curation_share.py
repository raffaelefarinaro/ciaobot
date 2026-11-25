"""Private engine-owned capabilities for managed Workspace care."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

from ciao import curation_run, share_evidence

MAX_WITNESSES_PER_ITEM = 20
MAX_CHECKPOINTS_PER_RUN = 100


def _private_dir(parent: Path, name: str, *, create: bool = False) -> Path | None:
    path = parent / name
    try:
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise share_evidence.EvidenceError("private_state_unsafe", "The care storage directory is unsafe.")
    return path


def _seal(config: Any, domain: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(share_evidence._relation_secret(config), domain + encoded, hashlib.sha256).hexdigest()
    return {"schema": share_evidence.SCHEMA, "payload": payload, "signature": signature}


def _verified(config: Any, domain: bytes, record: dict[str, Any]) -> dict[str, Any] | None:
    payload, signature = record.get("payload"), record.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        return None
    expected = _seal(config, domain, payload)["signature"]
    return payload if hmac.compare_digest(signature, expected) else None


def _write_record(path: Path, record: dict[str, Any]) -> None:
    share_evidence._write_private(path, json.dumps(record, sort_keys=True).encode("utf-8"), limit=16384)


def _capability(config: Any, *, workspace: str, chat_id: str, token_id: str,
                dispatch_id: str, holder: str, vault: Path) -> dict[str, Any] | None:
    name = hashlib.sha256(holder.encode("utf-8")).hexdigest() + ".json"
    with share_evidence._workspace_guard(config, workspace) as directory:
        leases = _private_dir(directory, "care-leases")
        if leases is None:
            return None
        record = share_evidence._read_json(leases / name, limit=16384,
                                           code="private_state_corrupt")
        payload = _verified(config, b"care-lease\0", record)
        if (payload is None or payload.get("workspace") != workspace
            or payload.get("workspace_lifetime_id") != share_evidence._workspace_lifetime(config, workspace)
            or payload.get("chat_id") != chat_id or payload.get("token_id") != token_id
            or payload.get("dispatch_id") != dispatch_id or payload.get("holder") != holder
            or payload.get("vault") != str(vault.resolve())):
            return None
        return payload


def recover(config: Any, workspace: str, state: curation_run.CurationState) -> None:
    """Settle an interrupted private receipt before a later lease can overwrite last_run.

    The caller holds the curation state lock. An intent with no matching committed
    run is discarded; it can never authorize a candidate.
    """
    pending_path = share_evidence._workspace_dir(config, workspace) / "care-settlement-pending.json"
    try:
        pending_path.lstat()
    except FileNotFoundError:
        return
    with share_evidence._workspace_guard(config, workspace) as directory:
        pending = directory / "care-settlement-pending.json"
        record = share_evidence._read_json(pending, limit=16384, code="private_state_corrupt")
        if not record:
            return
        payload = _verified(config, b"care-settlement\0", record)
        if (payload is None or payload.get("workspace") != workspace
            or payload.get("workspace_lifetime_id") != share_evidence._workspace_lifetime(config, workspace)):
            raise share_evidence.EvidenceError("private_state_corrupt", "The care settlement is invalid.")
        last = state.last_run
        name = hashlib.sha256(str(payload["holder"]).encode("utf-8")).hexdigest() + ".json"
        if (last.get("run_id") == payload.get("holder") and last.get("chat_id") == payload.get("chat_id")
            and last.get("status") == payload.get("status") and payload.get("status") == "ok"):
            receipts = _private_dir(directory, "care-settlements", create=True)
            assert receipts is not None
            _write_record(receipts / name, record)
        leases = _private_dir(directory, "care-leases")
        if leases is not None:
            (leases / name).unlink(missing_ok=True)
        pending.unlink()
        if hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def begin(config: Any, *, workspace: str, chat_id: str, token_id: str,
          dispatch_id: str, ttl_s: float) -> curation_run.Lease:
    """Acquire and privately attest the lease created by an active care dispatch."""
    vault = Path(config.workspace_vault_root(workspace))
    lease = curation_run.begin_run(
        vault, ttl_s=ttl_s, chat_id=chat_id,
        before_acquire=lambda state: recover(config, workspace, state),
    )
    try:
        with share_evidence._workspace_guard(config, workspace) as directory:
            payload = {
                "workspace": workspace, "workspace_lifetime_id": share_evidence._workspace_lifetime(config, workspace),
                "chat_id": chat_id, "token_id": token_id,
                "dispatch_id": dispatch_id, "holder": lease.holder,
                "started_at": lease.started_at, "expires_at": lease.expires_at,
                "vault": str(vault.resolve()), "proof": lease.proof,
            }
            name = hashlib.sha256(lease.holder.encode("utf-8")).hexdigest() + ".json"
            leases = _private_dir(directory, "care-leases", create=True)
            assert leases is not None
            _write_record(leases / name, _seal(config, b"care-lease\0", payload))
    except Exception:
        curation_run.end_run(vault, holder=lease.holder, status="error")
        raise
    return lease


def checkpoint(config: Any, manager: Any, *, workspace: str, chat_id: str,
               token_id: str, dispatch_id: str, holder: str, key: str) -> int:
    """Stage signed witness references for one completed live care item, without effects."""
    vault = Path(config.workspace_vault_root(workspace))
    payload = _capability(config, workspace=workspace, chat_id=chat_id, token_id=token_id,
                          dispatch_id=dispatch_id, holder=holder, vault=vault)
    if payload is None:
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
                root = _private_dir(directory, "care-outbox", create=True)
                assert root is not None
                outbox = _private_dir(root, run_id, create=True)
                assert outbox is not None
                if outbox.exists() and not (outbox / item_name).exists():
                    with os.scandir(outbox) as rows:
                        if sum(1 for _ in rows) >= MAX_CHECKPOINTS_PER_RUN:
                            return 0
                share_evidence._write_private(
                    outbox / item_name,
                    json.dumps({"schema": share_evidence.SCHEMA, "payload": item, "signature": seal}, sort_keys=True).encode("utf-8"),
                    limit=16384,
                )
        return len(found)


def end(config: Any, *, workspace: str, chat_id: str, token_id: str, dispatch_id: str,
        holder: str, status: str, planned: int, completed: int, deferred: int,
        reasons: list[str], live_keys: frozenset[str], failed: int,
        counts_complete: bool, dispatch_active: Callable[[], bool]) -> dict[str, Any]:
    """Settle one engine-owned lease with a recoverable private commit receipt."""
    vault = Path(config.workspace_vault_root(workspace))
    capability = _capability(config, workspace=workspace, chat_id=chat_id, token_id=token_id,
                             dispatch_id=dispatch_id, holder=holder, vault=vault)
    if capability is None:
        raise curation_run.CurationBusy("the managed care capability is unavailable")
    proof = capability.get("proof")
    if not isinstance(proof, str) or not proof:
        raise curation_run.CurationBusy("the managed care capability is invalid")
    proof_hash = hashlib.sha256(proof.encode("utf-8")).hexdigest()

    def prepare(state: curation_run.CurationState, summary: dict[str, Any]) -> None:
        held = state.lease
        if (not dispatch_active() or held.get("holder") != holder or held.get("chat_id") != chat_id
            or held.get("started_at") != capability.get("started_at")
            or ("proof_hash" in held and held["proof_hash"] != proof_hash)):
            raise curation_run.CurationBusy("the managed care lease no longer matches its capability")
        payload = {
            "workspace": workspace, "workspace_lifetime_id": capability["workspace_lifetime_id"],
            "chat_id": chat_id, "dispatch_id": dispatch_id, "holder": holder,
            "lease_started_at": held["started_at"], "lease_proof_hash": proof_hash,
            "status": summary["status"],
        }
        with share_evidence._workspace_guard(config, workspace) as directory:
            pending = directory / "care-settlement-pending.json"
            if pending.exists():
                raise curation_run.CurationStateError("a care settlement needs recovery")
            _write_record(pending, _seal(config, b"care-settlement\0", payload))

    def commit(state: curation_run.CurationState, summary: dict[str, Any]) -> None:
        recover(config, workspace, state)

    return curation_run.end_run(
        vault, holder=holder, status=status, planned=planned, completed=completed,
        deferred=deferred, reasons=reasons, live_keys=live_keys,
        advance_marker=status == "ok", failed=failed, counts_complete=counts_complete,
        before_settlement=prepare, after_settlement=commit,
    )


def settled_witnesses(config: Any, *, workspace: str, holder: str) -> list[dict[str, Any]]:
    """Read only references whose run settled and whose source grant is still current."""
    run_id = hashlib.sha256(holder.encode("utf-8")).hexdigest()
    with share_evidence._workspace_guard(config, workspace) as directory:
        receipts = _private_dir(directory, "care-settlements")
        if receipts is None:
            return []
        receipt = share_evidence._read_json(receipts / (run_id + ".json"),
                                            limit=16384, code="private_state_corrupt")
        settlement = _verified(config, b"care-settlement\0", receipt)
        if (settlement is None or settlement.get("workspace") != workspace
            or settlement.get("workspace_lifetime_id") != share_evidence._workspace_lifetime(config, workspace)
            or settlement.get("holder") != holder or settlement.get("status") != "ok"):
            return []
        root = _private_dir(directory, "care-outbox")
        if root is None:
            return []
        outbox = _private_dir(root, run_id)
        if outbox is None:
            return []
        with os.scandir(outbox) as rows:
            names = [row.name for row in rows]
        if len(names) > MAX_CHECKPOINTS_PER_RUN:
            return []
        items: list[dict[str, Any]] = []
        for name in names:
            if len(name) != 69 or not name.endswith(".json") or any(c not in "0123456789abcdef" for c in name[:-5]):
                return []
            record = share_evidence._read_json(outbox / name, limit=16384, code="private_state_corrupt")
            item = _verified(config, b"care-outbox\0", record)
            if (item is None or item.get("workspace") != workspace or item.get("holder") != holder
                or item.get("dispatch_id") != settlement.get("dispatch_id")
                or item.get("lease_started_at") != settlement.get("lease_started_at")
                or item.get("lease_proof_hash") != settlement.get("lease_proof_hash")
                or not isinstance(item.get("witness"), dict)):
                return []
            items.append(item)
    eligible: list[dict[str, Any]] = []
    for item in items:
        witness = item["witness"]
        witnesses = share_evidence.care_snapshot_witnesses(
            config, workspace=workspace, project_id=witness.get("project_id", ""),
            relative_path=witness.get("source_path", ""), revision=witness.get("source_revision", ""),
        )
        if witness in witnesses:
            eligible.append(item)
    return eligible
