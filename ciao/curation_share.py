"""Private engine-owned capabilities for managed Workspace care."""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

from ciao import curation_run, share_evidence

MAX_CHECKPOINTS_PER_RUN = 100
CARE_BATCH_WIDTH = 20


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
    if (not isinstance(payload, dict) or not isinstance(signature, str) or len(signature) != 64
            or any(char not in "0123456789abcdef" for char in signature)):
        return None
    expected = _seal(config, domain, payload)["signature"]
    return payload if hmac.compare_digest(signature, expected) else None


def _write_record(path: Path, record: dict[str, Any]) -> None:
    share_evidence._write_private(path, json.dumps(record, sort_keys=True).encode("utf-8"), limit=65536)


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
        record = share_evidence._read_json(pending, limit=65536, code="private_state_corrupt")
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
            root = _private_dir(directory, "care-outbox")
            outbox = _private_dir(root, name[:-5]) if root is not None else None
            if outbox is not None:
                with os.scandir(outbox) as rows:
                    has_items = next(rows, None) is not None
                if has_items:
                    ready = _private_dir(directory, "care-ready", create=True)
                    assert ready is not None
                    _write_record(ready / name, record)
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
        run_id = hashlib.sha256(holder.encode("utf-8")).hexdigest()
        marker_name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        with share_evidence._workspace_guard(config, workspace) as directory:
            root = _private_dir(directory, "care-item-checkpoints")
            run_dir = _private_dir(root, run_id) if root is not None else None
            if run_dir is not None:
                existing = share_evidence._read_json(run_dir / marker_name, limit=65536, code="private_state_corrupt")
                if existing:
                    marker = _verified(config, b"care-item\0", existing)
                    if (marker is None or marker.get("workspace") != workspace or marker.get("holder") != holder
                        or marker.get("dispatch_id") != dispatch_id or marker.get("work_item") != key
                        or marker.get("lease_started_at") != held.get("started_at")
                        or marker.get("lease_proof_hash") != proof_hash or marker.get("source_revision") != revision
                        or not isinstance(marker.get("entries"), list)):
                        raise curation_run.CurationStateError("the care item checkpoint is invalid")
                    return len(marker["entries"])
        found: list[dict[str, Any]] = []
        for project in manager.list_projects(workspace=workspace):
            witnesses = share_evidence.care_snapshot_witnesses(
                config, workspace=workspace, project_id=project.project_id,
                relative_path=relative, revision=revision,
            )
            found.extend(witnesses)
            if len(found) > MAX_CHECKPOINTS_PER_RUN:
                return 0
        entries: list[dict[str, str]] = []
        for witness in found:
            if not curation_run._holds_live_lease_now(vault, holder=holder, clock=curation_run._utc_now):
                return 0
            item = {"workspace": workspace, "dispatch_id": dispatch_id, "holder": holder,
                    "lease_started_at": held["started_at"], "lease_proof_hash": proof_hash,
                    "work_item": key, "witness": witness}
            encoded_item = json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
            seal = hmac.new(share_evidence._relation_secret(config), b"care-outbox\0" + encoded_item,
                            hashlib.sha256).hexdigest()
            item_name = hashlib.sha256(encoded_item).hexdigest() + ".json"
            with share_evidence._workspace_guard(config, workspace) as directory:
                root = _private_dir(directory, "care-outbox", create=True)
                assert root is not None
                outbox = _private_dir(root, run_id, create=True)
                assert outbox is not None
                if outbox.exists() and not (outbox / item_name).exists():
                    with os.scandir(outbox) as rows:
                        if sum(1 for _ in itertools.islice(rows, MAX_CHECKPOINTS_PER_RUN)) >= MAX_CHECKPOINTS_PER_RUN:
                            return 0
                share_evidence._write_private(
                    outbox / item_name,
                    json.dumps({"schema": share_evidence.SCHEMA, "payload": item, "signature": seal}, sort_keys=True).encode("utf-8"),
                    limit=16384,
                )
            entries.append({"name": item_name, "witness_signature": witness["witness_signature"]})
        marker = {"workspace": workspace, "holder": holder, "dispatch_id": dispatch_id,
                  "lease_started_at": held["started_at"], "lease_proof_hash": proof_hash,
                  "work_item": key, "source_revision": revision, "entries": sorted(entries, key=lambda row: row["name"])}
        with share_evidence._workspace_guard(config, workspace) as directory:
            root = _private_dir(directory, "care-item-checkpoints", create=True)
            assert root is not None
            run_dir = _private_dir(root, run_id, create=True)
            assert run_dir is not None
            _write_record(run_dir / marker_name, _seal(config, b"care-item\0", marker))
        return len(found)


def _settlement_manifest(config: Any, directory: Path, *, workspace: str, holder: str,
                         dispatch_id: str, held: dict[str, Any], proof_hash: str) -> list[dict[str, str]]:
    run_id = hashlib.sha256(holder.encode("utf-8")).hexdigest()
    plan = held.get("plan")
    keys = plan.get("completed_stale_notes", []) if isinstance(plan, dict) else []
    target_rows = plan.get("stale_note_targets", {}) if isinstance(plan, dict) else {}
    marker_root = _private_dir(directory, "care-item-checkpoints")
    markers = _private_dir(marker_root, run_id) if marker_root is not None else None
    root = _private_dir(directory, "care-outbox")
    outbox = _private_dir(root, run_id) if root is not None else None
    entries: list[dict[str, str]] = []
    for key in keys:
        if markers is None:
            raise curation_run.CurationStateError("a completed care item has no private checkpoint")
        marker_name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        marker = _verified(config, b"care-item\0", share_evidence._read_json(
            markers / marker_name, limit=65536, code="private_state_corrupt"))
        if (marker is None or marker.get("workspace") != workspace or marker.get("holder") != holder
            or marker.get("dispatch_id") != dispatch_id or marker.get("work_item") != key
            or marker.get("lease_started_at") != held.get("started_at")
            or marker.get("lease_proof_hash") != proof_hash
            or marker.get("source_revision") != target_rows.get(key, {}).get("revision")
            or not isinstance(marker.get("entries"), list)):
            raise curation_run.CurationStateError("the private care checkpoint is invalid")
        for entry in marker["entries"]:
            if (not isinstance(entry, dict) or set(entry) != {"name", "witness_signature"}
                or not isinstance(entry["name"], str) or not isinstance(entry["witness_signature"], str)
                or len(entry["name"]) != 69 or not entry["name"].endswith(".json")
                or any(c not in "0123456789abcdef" for c in entry["name"][:-5]) or outbox is None):
                raise curation_run.CurationStateError("the private care checkpoint is invalid")
            record = share_evidence._read_json(outbox / entry["name"], limit=16384, code="private_state_corrupt")
            item = _verified(config, b"care-outbox\0", record)
            encoded = json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8") if item else b""
            if (item is None or hashlib.sha256(encoded).hexdigest() + ".json" != entry["name"]
                or item.get("workspace") != workspace or item.get("holder") != holder
                or item.get("dispatch_id") != dispatch_id or item.get("work_item") != key
                or item.get("lease_started_at") != held.get("started_at") or item.get("lease_proof_hash") != proof_hash
                or not isinstance(item.get("witness"), dict)
                or item["witness"].get("witness_signature") != entry["witness_signature"]):
                raise curation_run.CurationStateError("the private care outbox is invalid")
            entries.append(entry)
            if len(entries) > MAX_CHECKPOINTS_PER_RUN:
                raise curation_run.CurationStateError("the private care outbox exceeds its limit")
    actual: list[str] = []
    if outbox is not None:
        with os.scandir(outbox) as rows:
            actual = [row.name for row in itertools.islice(rows, MAX_CHECKPOINTS_PER_RUN + 1)]
    names = [entry["name"] for entry in entries]
    if len(names) != len(set(names)) or sorted(names) != sorted(actual):
        raise curation_run.CurationStateError("the private care outbox is incomplete or altered")
    return sorted(entries, key=lambda entry: entry["name"])


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
            payload["manifest_version"] = 1
            payload["batch_width"] = CARE_BATCH_WIDTH
            payload["outbox_manifest"] = _settlement_manifest(
                config, directory, workspace=workspace, holder=holder, dispatch_id=dispatch_id,
                held=held, proof_hash=proof_hash,
            ) if summary["status"] == "ok" else []
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


def settled_witnesses(config: Any, *, workspace: str, holder: str, claim: share_evidence.EvidenceClaim | None = None) -> list[dict[str, Any]]:
    """Read only references whose run settled and whose source grant is still current."""
    run_id = hashlib.sha256(holder.encode("utf-8")).hexdigest()
    with share_evidence._claim_scope(config, workspace, claim) as owned:
        directory = owned.directory
        receipts = _private_dir(directory, "care-settlements")
        if receipts is None:
            return []
        receipt = share_evidence._read_json(receipts / (run_id + ".json"),
                                            limit=65536, code="private_state_corrupt")
        settlement = _verified(config, b"care-settlement\0", receipt)
        if (settlement is None or settlement.get("workspace") != workspace
            or settlement.get("workspace_lifetime_id") != share_evidence._workspace_lifetime(config, workspace)
            or settlement.get("holder") != holder or settlement.get("status") != "ok"):
            return []
        manifest = settlement.get("outbox_manifest")
        if (settlement.get("manifest_version") != 1 or settlement.get("batch_width") != CARE_BATCH_WIDTH
            or not isinstance(manifest, list) or len(manifest) > MAX_CHECKPOINTS_PER_RUN
            or any(not isinstance(entry, dict) or set(entry) != {"name", "witness_signature"}
                   or not isinstance(entry["name"], str) or not isinstance(entry["witness_signature"], str)
                   for entry in manifest)):
            return []
        manifest_names = [entry["name"] for entry in manifest]
        if manifest_names != sorted(set(manifest_names)):
            return []
        root = _private_dir(directory, "care-outbox")
        if root is None:
            return []
        outbox = _private_dir(root, run_id)
        if outbox is None:
            return []
        with os.scandir(outbox) as rows:
            names = []
            for row in rows:
                names.append(row.name)
                if len(names) > MAX_CHECKPOINTS_PER_RUN:
                    break
        if len(names) > MAX_CHECKPOINTS_PER_RUN:
            return []
        if sorted(names) != manifest_names:
            return []
        items: list[dict[str, Any]] = []
        for ordinal, entry in enumerate(manifest):
            name = entry["name"]
            if len(name) != 69 or not name.endswith(".json") or any(c not in "0123456789abcdef" for c in name[:-5]):
                return []
            record = share_evidence._read_json(outbox / name, limit=16384, code="private_state_corrupt")
            item = _verified(config, b"care-outbox\0", record)
            if item is not None:
                encoded = json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if name != hashlib.sha256(encoded).hexdigest() + ".json":
                    return []
            if (item is None or item.get("workspace") != workspace or item.get("holder") != holder
                or item.get("dispatch_id") != settlement.get("dispatch_id")
                or item.get("lease_started_at") != settlement.get("lease_started_at")
                or item.get("lease_proof_hash") != settlement.get("lease_proof_hash")
                or not isinstance(item.get("witness"), dict)
                or item["witness"].get("witness_signature") != entry["witness_signature"]):
                return []
            items.append({**item, "batch_index": ordinal // CARE_BATCH_WIDTH})
        eligible: list[dict[str, Any]] = []
        for item in items:
            witness = item["witness"]
            witnesses = share_evidence.care_snapshot_witnesses(
                config, workspace=workspace, project_id=witness.get("project_id", ""),
                relative_path=witness.get("source_path", ""), revision=witness.get("source_revision", ""),
                claim=owned,
            )
            if witness in witnesses:
                eligible.append(item)
        return eligible


def dispatch_ready(config: Any, *, workspace: str) -> None:
    """Retry a bounded batch of settled care work through private candidate intake."""
    from ciao import share_proposals  # noqa: PLC0415

    with share_evidence._workspace_guard(config, workspace) as directory:
        ready = _private_dir(directory, "care-ready")
        if ready is None:
            return
        with os.scandir(ready) as rows:
            names = [row.name for row in itertools.islice(rows, MAX_CHECKPOINTS_PER_RUN)]
    for name in names:
        if len(name) != 69 or not name.endswith(".json") or any(c not in "0123456789abcdef" for c in name[:-5]):
            continue
        with share_evidence._workspace_guard(config, workspace) as directory:
            ready = _private_dir(directory, "care-ready")
            if ready is None:
                return
            record = share_evidence._read_json(ready / name, limit=65536, code="private_state_corrupt")
            payload = _verified(config, b"care-settlement\0", record)
        if (payload is None or payload.get("workspace") != workspace
            or payload.get("workspace_lifetime_id") != share_evidence._workspace_lifetime(config, workspace)
            or payload.get("status") != "ok" or not isinstance(payload.get("holder"), str)
            or hashlib.sha256(payload["holder"].encode("utf-8")).hexdigest() + ".json" != name):
            continue
        holder = payload["holder"]
        manifest = payload.get("outbox_manifest")
        if not isinstance(manifest, list) or len(manifest) > MAX_CHECKPOINTS_PER_RUN:
            continue
        manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with share_evidence._workspace_guard(config, workspace) as directory:
            progress_dir = _private_dir(directory, "care-progress", create=True)
            assert progress_dir is not None
            progress_record = share_evidence._read_json(progress_dir / name, limit=65536, code="private_state_corrupt")
            progress = _verified(config, b"care-progress\0", progress_record) if progress_record else None
        if progress_record and (progress is None or progress.get("workspace") != workspace
            or progress.get("holder") != holder or progress.get("manifest_hash") != manifest_hash):
            continue
        processed = progress.get("processed", []) if progress else []
        if (not isinstance(processed, list) or len(processed) > MAX_CHECKPOINTS_PER_RUN
            or any(not isinstance(item, str) for item in processed) or len(processed) != len(set(processed))):
            continue
        signatures = {entry.get("witness_signature") for entry in manifest if isinstance(entry, dict)}
        if any(signature not in signatures for signature in processed):
            continue
        items = settled_witnesses(config, workspace=workspace, holder=holder)
        if not items:
            continue
        pending_items = [item for item in items if item["witness"]["witness_signature"] not in processed]
        if not pending_items:
            with share_evidence._workspace_guard(config, workspace) as directory:
                ready = _private_dir(directory, "care-ready")
                if ready is not None:
                    (ready / name).unlink(missing_ok=True)
            continue
        batch = pending_items[0]["batch_index"]
        for item in pending_items:
            if item["batch_index"] != batch:
                break
            relation = share_evidence.recover_care_source_relation(
                config, workspace=workspace, holder=holder,
                witness_signature=item["witness"]["witness_signature"],
            )
            drafts = share_proposals.draft_evidence_candidates(config, workspace=workspace, relation=relation)
            if not drafts or any(isinstance(draft, share_proposals.ShareRefusal)
                                 and draft.code != "already_settled" for draft in drafts):
                break
            processed.append(item["witness"]["witness_signature"])
            with share_evidence._workspace_guard(config, workspace) as directory:
                progress_dir = _private_dir(directory, "care-progress", create=True)
                assert progress_dir is not None
                _write_record(progress_dir / name, _seal(config, b"care-progress\0", {
                    "workspace": workspace, "holder": holder, "manifest_hash": manifest_hash,
                    "processed": processed,
                }))
        if all(item["witness"]["witness_signature"] in processed for item in items):
            with share_evidence._workspace_guard(config, workspace) as directory:
                ready = _private_dir(directory, "care-ready")
                if ready is not None:
                    (ready / name).unlink(missing_ok=True)
