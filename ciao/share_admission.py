"""Engine admission fence and attempt journal for automatic shared-note effects (#3619).

This module decides whether an engine-attested candidate may start ONE external
write right now, and records the attempt before the write. It performs no
publication itself: the effect is an injected callback, and `share_publish`
still refuses every evidence candidate until a later slice wires a real effect.

Lock order, outermost first. Every writer that can invalidate an admission
either takes the same locks in the same order or a suffix of them:

1. the in-process checkout lock, then the cross-process checkout claim
   (`shared_brain._checkout_lock` / `_checkout_claim`), as publication does;
2. the per-proposal publication lock (`share_publish._locked_proposal`);
3. the runtime registry owner (`workspaces.acquire_workspace_registry`), the
   final common lock. Admission takes it without waiting, so it never queues
   behind an unlink or archive and answers `registry_busy` with no effect;
4. the workspace evidence claim (consent state), then the queue lock only for
   the short journal write.

Registry-only writers (unlink, archive, rename, policy edits) take 3 alone.
Grant writers in `share_evidence` (confirm, revoke, withdraw, supersede) take 3
then 4. None of them waits for a checkout or proposal lock, so no writer holds
a later lock while waiting for an earlier one.

Step 4 starts with the source-eligibility check (`share_evidence.check_source_eligibility`:
withdrawn, revoked, stale generation, foreign workspace, bytes not the confirmed snapshot).
It runs once under the fence before the journal and again after the journal write,
immediately before the effect; a refusal in that gap closes the attempt as a retryable
rejection because no effect started.

The registry owner and the evidence claim stay held across the journal write,
the effect and the completion record, so once a revocation returns no write can
start under the old grant, and a write already admitted has finished or is
journaled as admitted. An admitted attempt whose acknowledgement is lost stays
`admitted`: it may have succeeded, so it is reconciled read-only and never
retried or reported as withdrawn.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

from ciao import memory_receipts, share_evidence, share_proposals as drafts, share_publish, shared_brain, shared_brain_mapping
from ciao.config import SharedBrainLink
from ciao.workspaces import (
    WorkspaceRegistryBusy,
    WorkspaceRegistryConflict,
    acquire_workspace_registry,
    run_while_claimed,
)

AdmissionCode = Literal[
    "eligible", "applied", "rejected", "indeterminate", "no_attempt",
    "stale_review", "not_candidate", "draft_corrupt", "draft_unavailable",
    "brain_unavailable", "consent_changed", "evidence_stale", "destination_changed",
    "audience_unverified", "audience_changed", "registry_busy",
    "attempt_unresolved", "already_applied", "rejected_final", "attempt_limit",
    "source_foreign", "source_withdrawn", "source_revoked", "source_generation_stale", "source_changed",
]
MAX_ADMISSION_ATTEMPTS = share_publish.MAX_ATTEMPTS
ATTEMPT_KIND = "admission"


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    code: AdmissionCode
    attempt_id: str | None = None
    receipt: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class AdmittedEffect:
    """Everything an effect may use; valid only while the fence is held."""

    attempt_id: str
    attempt: int
    workspace: str
    proposal_id: str
    revision: Any
    link: SharedBrainLink
    fields: dict[str, Any]
    row: dict[str, Any]
    target_revision: str | None
    policy_generation: int


@dataclass(frozen=True, slots=True)
class EffectResult:
    """`applied` or a definitive `rejected`. An effect that cannot say raises."""

    status: Literal["applied", "rejected"]
    retryable: bool = False
    receipt: str = ""


Effect = Callable[[AdmittedEffect], EffectResult]
# The destination audience as it is right now (a repository visibility or folder
# ACL read), or None when it cannot be read. #3615's restricted-audience
# precondition is enforced here, at the destination operation.
AudienceProbe = Callable[[SharedBrainLink, dict[str, Any]], "str | None"]


def _digest(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _refusal(code: AdmissionCode, message: str) -> AdmissionOutcome:
    return AdmissionOutcome(code, message=message)


async def admit_effect(
    config: Any, *, workspace: str, proposal_id: str, expected_revision: str,
    audience: AudienceProbe, effect: Effect | None,
) -> AdmissionOutcome:
    """Verify, journal, then run `effect` once under the admission fence.

    `effect=None` only checks eligibility (the source-eligibility check under
    admission ownership) and writes nothing.
    """
    artifact = share_publish._artifact_for(config, workspace, proposal_id)
    if artifact is None:
        return _refusal("draft_corrupt", "The queued shared-note draft could not be verified.")
    if expected_revision != artifact.get("review_token"):
        return _refusal("stale_review", "The shared-note review has changed.")
    fields = artifact["immutable"]
    path = fields.get("checkout_path")
    if (not isinstance(fields.get("evidence_authorization"), dict)
            or not isinstance(fields.get("destination_mapping"), dict)):
        return _refusal("not_candidate", "Only an attested candidate can be admitted.")
    if not isinstance(path, str) or not Path(path).is_absolute():
        return _refusal("draft_corrupt", "The recorded checkout cannot be verified.")
    try:
        async with shared_brain._checkout_lock(path):
            async with shared_brain._checkout_claim(path, config):
                async with share_publish._locked_proposal(config, workspace, fields):
                    return await run_while_claimed(
                        _admit_locked, config, workspace, fields, expected_revision, audience, effect
                    )
    except (OSError, ValueError, drafts.ShareDraftError):
        return _refusal("draft_unavailable", "Private publication state is unavailable.")


def _admit_locked(
    config: Any, workspace: str, fields: dict[str, Any], expected_revision: str,
    audience: AudienceProbe, effect: Effect | None,
) -> AdmissionOutcome:
    try:
        owner = acquire_workspace_registry(config, blocking=False)
    except WorkspaceRegistryBusy:
        return _refusal("registry_busy", "The workspace registry is busy; nothing was written.")
    except (OSError, WorkspaceRegistryConflict):
        return _refusal("draft_unavailable", "The workspace registry is unavailable.")
    with owner:
        return _admit_owned(config, owner, workspace, fields, expected_revision, audience, effect)


def _prior_attempts(publication: object) -> tuple[list[dict[str, Any]], AdmissionOutcome | None]:
    if not isinstance(publication, dict):
        return [], None
    attempts = publication.get("attempts", [])
    receipts = publication.get("receipts", [])
    if not isinstance(attempts, list) or not isinstance(receipts, list) or receipts:
        return [], _refusal("attempt_unresolved", "A publication record prevents a new attempt.")
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("kind") != ATTEMPT_KIND:
            return [], _refusal("attempt_unresolved", "A publication attempt needs reconciliation.")
        status = attempt.get("status")
        if status == "applied":
            return [], AdmissionOutcome("already_applied", attempt.get("attempt_id"), str(attempt.get("receipt", "")),
                                        "This candidate was already applied.")
        if status == "rejected" and attempt.get("retryable") is True:
            continue
        if status == "rejected":
            return [], AdmissionOutcome("rejected_final", attempt.get("attempt_id"), message="The destination refused this candidate.")
        return [], AdmissionOutcome("attempt_unresolved", attempt.get("attempt_id"),
                                    message="An admitted attempt may have succeeded; reconcile before anything else.")
    if len(attempts) >= MAX_ADMISSION_ATTEMPTS:
        return [], _refusal("attempt_limit", "The attempt limit is reached.")
    return list(attempts), None


def _admit_owned(
    config: Any, owner: Any, workspace: str, fields: dict[str, Any], expected_revision: str,
    audience: AudienceProbe, effect: Effect | None,
) -> AdmissionOutcome:
    # Everything below is re-read under the registry owner; nothing observed
    # before the locks is trusted.
    current = share_publish._artifact_for(config, workspace, fields["proposal_id"])
    if current is None or current.get("immutable") != fields:
        return _refusal("draft_corrupt", "The private share artifact changed before admission.")
    if current.get("review_token") != expected_revision:
        return _refusal("stale_review", "The shared-note review has changed.")
    attempts, blocked = _prior_attempts(current.get("publication"))
    if blocked is not None:
        return blocked
    try:
        if owner.current is None or owner.current != getattr(config, "_workspace_registry_bytes", None):
            return _refusal("brain_unavailable", "The workspace registry changed.")
        link = share_publish._active_link(config, workspace, fields)
        if link is None:
            return _refusal("brain_unavailable", "The selected shared brain link is no longer active.")
        if not shared_brain.sharing_consent_active(link):
            return _refusal("consent_changed", "Automatic sharing is no longer consented for this link.")
        revocations = shared_brain.load_sharing_revocations(config)
        if revocations.fail_closed or (link.lifetime_id, link.sharing_generation) in revocations.keys:
            return _refusal("consent_changed", "Automatic sharing was revoked for this link.")
        claim_scope = share_evidence.evidence_claim(config, workspace=workspace)
        with claim_scope as claim:
            # The source-eligibility step (#3614 snapshot and withdrawal state), read here
            # under the registry owner and the evidence claim, before anything is journaled.
            share_evidence.check_source_eligibility(
                config, workspace=workspace, authorization=fields["evidence_authorization"], claim=claim)
            guard = share_evidence.evidence_authorization_guard(
                config, workspace=workspace, authorization=fields["evidence_authorization"], claim=claim)
            with guard as row:
                return _admit_checked(
                    config, owner, workspace, fields, link, dict(row), attempts, audience, effect, expected_revision, claim)
    except share_evidence.EvidenceError as exc:
        if exc.code in share_evidence.SOURCE_REFUSALS:
            return _refusal(exc.code, str(exc))  # type: ignore[arg-type]
        if exc.code in ("private_state_unavailable", "private_state_corrupt", "workspace_lifetime_unpersisted"):
            return _refusal("draft_unavailable", "Private consent state is unavailable.")
        return _refusal("evidence_stale", "The evidence permission changed.")
    except (OSError, ValueError, WorkspaceRegistryConflict, drafts.ShareDraftError):
        return _refusal("draft_unavailable", "Private publication state is unavailable.")


def _admit_checked(
    config: Any, owner: Any, workspace: str, fields: dict[str, Any], link: SharedBrainLink,
    row: dict[str, Any], attempts: list[dict[str, Any]], audience: AudienceProbe, effect: Effect | None,
    token: str, claim: share_evidence.EvidenceClaim,
) -> AdmissionOutcome:
    mapping = fields["destination_mapping"]
    try:
        destination = mapping["destination"]
        snapshot = shared_brain_mapping.resolve_workspace_destination(
            config, workspace, link.name, note_type=destination["note_type"],
            relative_path=fields["path"], operation=destination["destination"]["operation"], owner=owner)
        if snapshot is None or asdict(snapshot) != mapping:
            return _refusal("destination_changed", "The candidate destination changed.")
        if link.kind == "git":
            # The tip is read now, not from the last fetch: an upstream edit since
            # review must refuse here, before the journal and the effect.
            fetched, _raw, _error = share_publish._fetch(Path(link.path), "origin", str(fields.get("upstream_ref")))
            if fetched != "ok":
                return _refusal("destination_changed", "The current target could not be read.")
        qualified = drafts._qualify_owned(config, workspace, link, fields["path"], fields["operation"], owner)
    except (KeyError, TypeError):
        return _refusal("draft_corrupt", "The candidate destination record is corrupt.")
    except (drafts.ShareDraftError, WorkspaceRegistryConflict, OSError, ValueError):
        return _refusal("destination_changed", "The candidate destination could not be verified.")
    ref, base_commit, _original, original_oid, fingerprint, chain, mode = qualified
    if (ref != fields.get("upstream_ref") or base_commit != fields.get("base_commit")
            or original_oid != fields.get("base_blob_oid") or fingerprint != fields.get("destination_fingerprint")
            or ("base_mode" in fields and mode != fields["base_mode"])
            or (link.kind == "folder" and chain != fields.get("folder_chain"))):
        return _refusal("destination_changed", "The current target is not the reviewed one.")
    try:
        observed = audience(link, row)
    except Exception:  # noqa: BLE001 - an unreadable audience is a refusal, whatever the probe raised
        observed = None
    if not isinstance(observed, str) or not observed:
        return _refusal("audience_unverified", "The destination audience could not be verified.")
    if observed != row.get("audience"):
        return _refusal("audience_changed", "The destination audience changed.")
    if effect is None:
        return AdmissionOutcome("eligible")
    attempt_id = "adm_" + secrets.token_urlsafe(12)
    record = {
        "kind": ATTEMPT_KIND, "attempt": len(attempts) + 1, "attempt_id": attempt_id,
        "proposal_id": fields["proposal_id"], "revision": fields["revision"],
        "destination": _digest(fields["destination_fingerprint"]), "ref": fields.get("upstream_ref"),
        "actor": "engine", "producer": str(row.get("producer", "")),
        "grant": _digest(row.get("grant_id", "")), "consent_generation": row.get("consent_generation"),
        "policy_generation": link.sharing_generation, "audience": _digest(observed),
        "target_revision": base_commit, "admitted_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "admitted",
    }
    attempts = [*attempts, record]
    queue = Path(config.workspace_vault_root(workspace)) / drafts._QUEUE
    try:
        # The journal is durable BEFORE the effect, under the queue lock dismissal
        # uses, so a dismissal either precedes this record or sees it.
        with drafts._thread_queue_lock(queue), memory_receipts.queue_lock(queue):
            visible = drafts.load_share(config, workspace=workspace, proposal_id=fields["proposal_id"])
            if (isinstance(visible, drafts.ShareRefusal) or visible.get("immutable") != fields
                    or visible.get("review_token") != token):
                return _refusal("draft_corrupt", "The share proposal is no longer active in its queue.")
            share_publish._write_state(config, workspace, fields, {"attempts": attempts})
    except (OSError, ValueError, drafts.ShareDraftError, memory_receipts.QueueLockError):
        return _refusal("draft_unavailable", "The attempt could not be recorded; nothing was written.")
    # Re-read the source after the journal write and immediately before the effect: the
    # bytes live in a private file nothing but the claim keeps stable. A refusal here
    # started no effect, so the attempt is closed as a retryable rejection.
    try:
        share_evidence.check_source_eligibility(
            config, workspace=workspace, authorization=fields["evidence_authorization"], claim=claim)
    except share_evidence.EvidenceError as exc:
        code = exc.code if exc.code in share_evidence.SOURCE_REFUSALS else "evidence_stale"
        try:
            share_publish._write_state(
                config, workspace, fields, {"attempts": [*attempts[:-1], {**record, "status": "rejected", "retryable": True}]})
        except (OSError, ValueError, drafts.ShareDraftError):
            pass  # stays `admitted`: later attempts fail closed until it is reconciled
        return _refusal(code, "The source changed before the effect; nothing was written.")  # type: ignore[arg-type]
    admitted = AdmittedEffect(
        attempt_id, record["attempt"], workspace, fields["proposal_id"], fields["revision"], link, fields,
        row, base_commit, link.sharing_generation)
    try:
        result = effect(admitted)
    except Exception:  # noqa: BLE001 - an effect that cannot say may have written
        return AdmissionOutcome("indeterminate", attempt_id, message="The effect did not report; it may have succeeded.")
    finished = dict(record)
    if result.status == "applied":
        finished.update(status="applied", receipt=result.receipt)
    else:
        finished.update(status="rejected", retryable=bool(result.retryable))
    try:
        share_publish._write_state(config, workspace, fields, {"attempts": [*attempts[:-1], finished]})
    except (OSError, ValueError, drafts.ShareDraftError):
        return AdmissionOutcome("indeterminate", attempt_id, result.receipt,
                                "The effect reported but its outcome could not be recorded.")
    if result.status == "applied":
        return AdmissionOutcome("applied", attempt_id, result.receipt)
    return AdmissionOutcome("rejected", attempt_id, message="The destination refused the write.")


def reconcile_admission(config: Any, *, workspace: str, proposal_id: str, expected_revision: str) -> AdmissionOutcome:
    """Read the journal only. Never writes, retries or reports a withdrawal."""
    artifact = share_publish._artifact_for(config, workspace, proposal_id)
    if artifact is None or artifact.get("review_token") != expected_revision:
        return _refusal("stale_review", "The shared-note review has changed.")
    publication = artifact.get("publication")
    attempts = publication.get("attempts", []) if isinstance(publication, dict) else []
    rows = [item for item in attempts if isinstance(item, dict) and item.get("kind") == ATTEMPT_KIND]
    if not rows:
        return _refusal("no_attempt", "There is no admitted attempt to reconcile.")
    last = rows[-1]
    attempt_id = last.get("attempt_id") if isinstance(last.get("attempt_id"), str) else None
    if last.get("status") == "applied":
        return AdmissionOutcome("applied", attempt_id, str(last.get("receipt", "")))
    if last.get("status") == "rejected":
        return AdmissionOutcome("rejected", attempt_id)
    return AdmissionOutcome("indeterminate", attempt_id,
                            message="The attempt was admitted and may have succeeded; it is not retried.")
