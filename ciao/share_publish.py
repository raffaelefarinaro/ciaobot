"""Bounded, review-bound publication of a shared-note proposal."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ciao import memory_receipts, share_proposals as drafts, shared_brain
from ciao.config import SharedBrainLink
from ciao.git_proc import run_git_sync_bounded
from ciao.os_support import folder_io
from ciao.os_support.files import open_fd
from ciao.os_support.locks import await_exclusive
from ciao.os_support.private import is_private_descriptor_strict, make_private_dir
from ciao.redact import redact_text
from ciao.workspaces import run_while_claimed

OutcomeCode = Literal[
    "published", "already_published", "changed_upstream", "brain_changed",
    "push_refused", "indeterminate", "stale_review", "draft_corrupt",
    "brain_unavailable", "upstream_unavailable", "draft_unavailable",
    "reconcile_after_unlink", "unsafe_target", "folder_update_unsupported",
    "folder_unverifiable", "manual_export_required",
]
MAX_ATTEMPTS = 3
PUSH_TIMEOUT_S = 20.0
MOUNT_STAGING_DIR = ".ciao-share-staging"
_OID = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    outcome: OutcomeCode
    commit_oid: str | None = None
    receipt_id: str | None = None
    message: str = ""


def _safe_git(
    folder: Path, *args: str, limit: int = 16_384,
    timeout_s: float = shared_brain.FETCH_TIMEOUT_S,
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    child = shared_brain._git_env(folder)
    child["GIT_NO_REPLACE_OBJECTS"] = "1"
    child["GIT_NO_LAZY_FETCH"] = "1"
    if env:
        child.update(env)
    try:
        result = run_git_sync_bounded(
            folder, *shared_brain.EXACT_BYTES, *shared_brain._HARDEN, *args,
            timeout=timeout_s, max_output=limit,
            env=child,
        )
    except OSError:
        return 127, b"", b"git could not be started"
    return result.returncode, result.stdout, result.stderr[:limit]


def _network_git(
    folder: Path, *args: str, limit: int = 4096,
    timeout_s: float = shared_brain.FETCH_TIMEOUT_S,
) -> tuple[int, bytes, bytes]:
    """The only injectable seam for bounded remote I/O in isolated Git tests."""
    return _safe_git(folder, *args, limit=limit, timeout_s=timeout_s)


def _hash_blob(folder: Path, content: bytes) -> str | None:
    child = shared_brain._git_env(folder)
    child["GIT_NO_REPLACE_OBJECTS"] = "1"
    child["GIT_NO_LAZY_FETCH"] = "1"
    try:
        proc = subprocess.run(
            ["git", *shared_brain.EXACT_BYTES, *shared_brain._HARDEN, "-C", str(folder), "hash-object", "-w", "--no-filters", "--stdin"],
            input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child, timeout=shared_brain.GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = proc.stdout.decode("ascii", "strict").strip() if proc.returncode == 0 else ""
    return value if _OID.fullmatch(value) else None


def _text(value: bytes) -> str:
    return value.decode("ascii", "strict").strip()


def _artifact_for(config: Any, workspace: str, proposal_id: str) -> dict[str, Any] | None:
    store = drafts._store_dir(config)
    if not store.is_dir():
        return None
    for path in store.glob("*.json"):
        try:
            data = drafts._load_sidecar(path)
        except drafts.ShareDraftError:
            continue
        fields = data["immutable"]
        if fields.get("workspace") == workspace and fields.get("proposal_id") == proposal_id:
            canonical = store / (hashlib.sha256((workspace + "\0sidecar\0" + fields["sidecar_id"]).encode()).hexdigest() + ".json")
            try:
                return drafts._load_sidecar(canonical)
            except drafts.ShareDraftError:
                return None
    return None


def has_publication_attempt(config: Any, *, workspace: str, proposal_id: str, expected_revision: str) -> bool:
    """Whether this exact reviewed artifact has a persisted attempt to reconcile."""
    artifact = _artifact_for(config, workspace, proposal_id)
    if artifact is None or artifact.get("review_token") != expected_revision:
        return False
    publication = artifact.get("publication")
    attempts = publication.get("attempts", []) if isinstance(publication, dict) else []
    return isinstance(attempts, list) and bool(attempts)


def can_retry_publication(
    config: Any, *, workspace: str, proposal_id: str, expected_revision: str,
) -> bool:
    """Whether every persisted attempt was definitively rejected as retryable."""
    artifact = _artifact_for(config, workspace, proposal_id)
    if artifact is None or artifact.get("review_token") != expected_revision:
        return False
    fields = artifact.get("immutable")
    publication = artifact.get("publication")
    if not isinstance(fields, dict) or not isinstance(publication, dict):
        return False
    attempts = publication.get("attempts")
    receipts = publication.get("receipts")
    if (
        not isinstance(attempts, list) or not attempts
        or len(attempts) >= MAX_ATTEMPTS
        or not isinstance(receipts, list) or receipts
    ):
        return False
    if fields.get("link_kind") == "folder":
        for index, attempt in enumerate(attempts, start=1):
            if (
                not isinstance(attempt, dict)
                or attempt.get("kind") != "folder"
                or type(attempt.get("attempt")) is not int
                or attempt.get("attempt") != index
                or attempt.get("proposal_id") != fields.get("proposal_id")
                or attempt.get("revision") != fields.get("revision")
                or attempt.get("destination_fingerprint") != fields.get("destination_fingerprint")
                or attempt.get("path") != fields.get("path")
                or not isinstance(attempt.get("token"), str)
                or not attempt.get("token")
                or attempt.get("status") != "rejected"
                or attempt.get("retryable") is not True
            ):
                return False
        return True
    for index, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, dict):
            return False
        commit_oid = attempt.get("commit_oid")
        parent_oid = attempt.get("parent_oid")
        if (
            type(attempt.get("attempt")) is not int or attempt["attempt"] != index
            or not isinstance(commit_oid, str) or not _OID.fullmatch(commit_oid)
            or not isinstance(parent_oid, str) or not _OID.fullmatch(parent_oid)
            or attempt.get("proposal_id") != fields.get("proposal_id")
            or attempt.get("revision") != fields.get("revision")
            or attempt.get("destination_fingerprint") != fields.get("destination_fingerprint")
            or attempt.get("ref") != fields.get("upstream_ref")
            or attempt.get("status") != "rejected"
            or attempt.get("retryable") is not True
        ):
            return False
    return True


def refresh_legacy_draft(
    config: Any, *, workspace: str, proposal_id: str, expected_revision: str,
) -> drafts.ShareDraft | drafts.ShareRefusal | None:
    """Re-draft an unattempted #3575 review against the same still-linked checkout.

    The replacement gets a new token and queue row. The old token never consents
    to a publish, and a changed checkout or origin cannot be retargeted.
    """
    artifact = _artifact_for(config, workspace, proposal_id)
    if artifact is None or artifact.get("review_token") != expected_revision:
        return None
    fields = artifact["immutable"]
    publication = artifact.get("publication")
    attempts = publication.get("attempts", []) if isinstance(publication, dict) else []
    if "link_lifetime_id" in fields or (isinstance(attempts, list) and attempts):
        return None
    from ciao.workspaces import workspace_registry_lock

    queue = Path(config.workspace_vault_root(workspace)) / drafts._QUEUE
    request_key = "legacy-refresh:" + str(fields.get("sidecar_id", ""))
    key_hash = hashlib.sha256((workspace + "\0" + request_key).encode()).hexdigest()
    index_path = drafts._store_dir(config) / (key_hash + ".json")
    with drafts._thread_queue_lock(queue), drafts.memory_receipts.queue_lock(queue):
        # The initial read above only routes obvious non-legacy requests. The
        # canonical sidecar and exact old row must be re-read under the queue
        # lock: publication reserves its attempt under that same lock.
        rel_path = (Path(workspace) / drafts._QUEUE).as_posix()
        try:
            queue_text = queue.read_text(encoding="utf-8")
            current = drafts._load_sidecar(
                drafts._store_dir(config) / (
                    hashlib.sha256((workspace + "\0sidecar\0" + str(fields.get("sidecar_id", ""))).encode()).hexdigest() + ".json"
                )
            )
        except (OSError, drafts.ShareDraftError):
            return drafts.ShareRefusal("draft_corrupt", "The legacy review state changed; refresh it before replacing.")
        current_fields = current.get("immutable")
        if current_fields != fields or current.get("review_token") != expected_revision:
            return drafts.ShareRefusal("stale_review", "The legacy review changed; refresh it before replacing.")
        old_row = next((entry for entry in drafts.proposal_tracking.walk_proposal_queue(workspace, rel_path, queue_text)
                        if entry.proposal_id == proposal_id and entry.bullet.kind == "share"), None)
        if old_row is None:
            return drafts.ShareRefusal("legacy_missing", "The old shared-note review is no longer queued.")
        block = drafts.share_dismissal_block(
            config, workspace=workspace, bullet=old_row.bullet, proposal_id=proposal_id
        )
        if block:
            return drafts.ShareRefusal("publication_pending", block)
        publication_now = current.get("publication")
        if publication_now is not None:
            if (not isinstance(publication_now, dict)
                    or not isinstance(publication_now.get("attempts"), list)
                    or not isinstance(publication_now.get("receipts"), list)
                    or publication_now.get("attempts")
                    or publication_now.get("receipts")):
                return drafts.ShareRefusal(
                    "publication_pending", "Publication state changed; reconcile it before replacing this review."
                )
        with workspace_registry_lock(config):
            link = _active_legacy_link(config, workspace, fields)
            if link is None:
                return drafts.ShareRefusal("brain_unavailable", "The original shared-brain checkout is no longer linked.")
            proposed_text = fields.get("proposed")
            proposed = proposed_text.encode("utf-8") if isinstance(proposed_text, str) else None
            fingerprint = drafts._request_fingerprint(
                str(fields.get("brain", "")), str(fields.get("path", "")),
                str(fields.get("operation", "")), proposed,
            )
            if index_path.exists():
                replacement_artifact = drafts._load_sidecar(index_path)
                if replacement_artifact.get("request_fingerprint") != fingerprint:
                    return drafts.ShareRefusal("request_conflict", "The legacy review replacement key is already in use.")
                replacement_fields = replacement_artifact["immutable"]
                visible = drafts.load_share(
                    config, workspace=workspace,
                    proposal_id=str(replacement_fields.get("proposal_id", "")),
                )
                if isinstance(visible, dict):
                    replacement = drafts.ShareDraft(
                        str(replacement_fields["proposal_id"]), visible,
                        drafts.proposal_review_path(workspace, str(replacement_fields["review_key"])),
                    )
                else:
                    from ciao.memory_proposals import _dismissed_texts

                    summary = f"Shared note change proposal {replacement_fields['sidecar_id']}"
                    if summary in _dismissed_texts(queue):
                        if not _retire_legacy_queue_row(config, workspace, proposal_id, fields):
                            return drafts.ShareRefusal("publication_pending", "The old review could not be safely retired.")
                        return drafts.ShareRefusal("legacy_replaced", "The replacement proposal was already settled; the old review was removed.")
                    # The process may have stopped after writing the private
                    # idempotency artifact but before appending its queue row.
                    # draft_share's recovery path restores that exact row.
                    recovered = drafts.draft_share(
                        config, workspace=workspace, brain=str(fields.get("brain", "")),
                        relative_path=str(fields.get("path", "")),
                        operation=str(fields.get("operation", "")), proposed=proposed,
                        request_key=request_key, retiring_proposal_id=proposal_id,
                    )
                    if isinstance(recovered, drafts.ShareDraft):
                        replacement = recovered
                    else:
                        return recovered
            else:
                created = drafts.draft_share(
                    config, workspace=workspace, brain=str(fields.get("brain", "")),
                    relative_path=str(fields.get("path", "")),
                    operation=str(fields.get("operation", "")), proposed=proposed,
                    request_key=request_key, retiring_proposal_id=proposal_id,
                )
                if isinstance(created, drafts.ShareDraft):
                    replacement = created
                else:
                    return created
            if isinstance(replacement, drafts.ShareDraft):
                if not _retire_legacy_queue_row(config, workspace, proposal_id, fields):
                    return drafts.ShareRefusal("publication_pending", "The old review could not be safely retired.")
            return replacement


def _active_legacy_link(config: Any, workspace: str, fields: dict[str, Any]) -> SharedBrainLink | None:
    record = config.workspace(workspace) if hasattr(config, "workspace") else None
    links = getattr(record, "shared_brains", []) or []
    if not isinstance(links, list):
        return None
    for candidate in links:
        if not isinstance(candidate, SharedBrainLink):
            continue
        if (candidate.name == fields.get("brain")
                and candidate.path == fields.get("checkout_path")
                and candidate.identity == fields.get("origin_identity")):
            return candidate
    return None


def _retire_legacy_queue_row(config: Any, workspace: str, proposal_id: str, fields: dict[str, Any]) -> bool:
    """Remove only the exact legacy queue row after its replacement is durable."""
    from ciao.web.proposal_service import ShareDismissalRefused, _rewrite_queue_single

    queue = Path(config.workspace_vault_root(workspace)) / drafts._QUEUE
    text = queue.read_text(encoding="utf-8") if queue.exists() else ""
    rel_path = (Path(workspace) / drafts._QUEUE).as_posix()
    summary = f"Shared note change proposal {fields.get('sidecar_id', '')}"
    row = next((item for item in drafts.proposal_tracking.walk_proposal_queue(workspace, rel_path, text)
                if item.proposal_id == proposal_id and item.bullet.kind == "share"
                and item.bullet.text == summary), None)
    if row is None:
        return False
    vault = Path(config.workspace_vault_root(workspace))
    try:
        return _rewrite_queue_single(
            queue, row.line, row.raw, row.bullet.text, "share", False, workspace, vault,
            config=config, proposal_id=proposal_id,
        )
    except ShareDismissalRefused:
        return False


def _active_link(config: Any, workspace: str, fields: dict[str, Any]) -> SharedBrainLink | None:
    from ciao.workspaces import (
        ensure_registry_current,
        persisted_checkout_links,
        persisted_shared_links,
        WorkspaceRegistryConflict,
    )
    kind = fields.get("link_kind", "git")
    try:
        ensure_registry_current(config)
        raw = config._workspace_registry_bytes
        if raw is None:
            return None
        if kind == "folder":
            matches = persisted_shared_links(raw, fields["checkout_path"])
            if not any(
                name == workspace and row.get("name") == fields.get("brain")
                and row.get("identity") == fields.get("origin_identity")
                and row.get("kind") == "folder"
                and row.get("lifetime_id") == fields.get("link_lifetime_id")
                for name, row in matches
            ):
                return None
        elif not any(
            name == workspace and row.get("name") == fields.get("brain")
            and row.get("identity") == fields.get("origin_identity")
            and row.get("lifetime_id") == fields.get("link_lifetime_id")
            for name, row in persisted_checkout_links(raw, fields["checkout_path"])
        ):
            return None
    except (WorkspaceRegistryConflict, OSError, ValueError):
        return None
    record = config.workspace(workspace) if hasattr(config, "workspace") else None
    links = getattr(record, "shared_brains", []) or []
    if not isinstance(links, list):
        return None
    for candidate in links:
        if not isinstance(candidate, SharedBrainLink):
            continue
        if (candidate.name == fields.get("brain")
                and candidate.path == fields.get("checkout_path")
                and getattr(candidate, "lifetime_id", None) == fields.get("link_lifetime_id")
                and getattr(candidate, "kind", "git") == kind):
            return candidate
    return None


def _receipt_id(fields: dict[str, Any], commit: str) -> str:
    value = "\0".join((fields["workspace"], fields["proposal_id"], str(fields["revision"]), fields["destination_fingerprint"], commit))
    return "shrpub_" + hashlib.sha256(value.encode()).hexdigest()


def _folder_receipt_id(fields: dict[str, Any], token: str) -> str:
    """A deterministic identity for one folder create attempt, not its bytes alone."""
    value = "\0".join((
        fields["workspace"], fields["proposal_id"], str(fields["revision"]),
        fields["destination_fingerprint"], "folder", token,
    ))
    return "shrpub_" + hashlib.sha256(value.encode()).hexdigest()


def _valid_receipt(fields: dict[str, Any], receipt: dict[str, Any]) -> bool:
    common = (
        receipt.get("workspace") == fields.get("workspace")
        and receipt.get("proposal_id") == fields.get("proposal_id")
        and receipt.get("revision") == fields.get("revision")
        and receipt.get("destination_fingerprint") == fields.get("destination_fingerprint")
        and receipt.get("operation") == fields.get("operation")
    )
    if not common:
        return False
    if fields.get("link_kind") == "folder":
        token = receipt.get("attempt_token")
        proposed = fields.get("proposed")
        if (
            not isinstance(token, str) or not token
            or receipt.get("id") != _folder_receipt_id(fields, token)
            or fields.get("operation") != "create"
            or not isinstance(proposed, str)
            or receipt.get("content_sha256") != fields.get("proposed_sha256")
            or receipt.get("content_bytes") != len(proposed.encode("utf-8"))
            or type(receipt.get("file_dev")) is not int
            or type(receipt.get("file_ino")) is not int
            or receipt.get("path") != fields.get("path")
            or receipt.get("brain") != fields.get("brain")
            or receipt.get("identity") != fields.get("origin_identity")
            or receipt.get("link_lifetime_id") != fields.get("link_lifetime_id")
            or receipt.get("scope") != "historical_local_write"
            or receipt.get("local_only") is not True
            or receipt.get("sync_claimed") is not False
        ):
            return False
    else:
        commit = receipt.get("commit_oid")
        if not isinstance(commit, str) or _OID.fullmatch(commit) is None:
            return False
        if receipt.get("id") != _receipt_id(fields, commit):
            return False
    # #3575 receipts predate destination display metadata. The immutable
    # artifact and deterministic receipt ID still bind them to this path.
    return all(
        receipt.get(key) == fields.get(field)
        for key, field in (
            ("path", "path"), ("brain", "brain"), ("identity", "origin_identity"),
            ("link_lifetime_id", "link_lifetime_id"),
        )
        if key in receipt
    )


def _receipt_with_metadata(fields: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    """Project destination metadata omitted by early #3575 receipt records."""
    enriched = dict(receipt)
    enriched.setdefault("brain", fields.get("brain", ""))
    enriched.setdefault("identity", fields.get("origin_identity", ""))
    enriched.setdefault("path", fields.get("path", ""))
    upstream_ref = fields.get("upstream_ref")
    if isinstance(upstream_ref, str):
        enriched.setdefault("branch", upstream_ref.removeprefix("refs/heads/"))
    if isinstance(fields.get("link_lifetime_id"), str):
        enriched.setdefault("link_lifetime_id", fields["link_lifetime_id"])
    if fields.get("link_kind") == "folder":
        enriched.setdefault("link_kind", "folder")
        enriched.setdefault("undoable", False)
    else:
        enriched.setdefault("link_kind", "git")
        enriched.setdefault("undoable", True)
    return enriched


def is_share_receipt_undoable(receipt: dict[str, Any]) -> bool:
    """A folder create has no conditional reverse; only Git receipts are undoable."""
    return (
        receipt.get("undoable", True) is not False
        and receipt.get("link_kind", "git") != "folder"
        and isinstance(receipt.get("commit_oid"), str)
        and bool(receipt.get("commit_oid"))
    )


def _write_state(config: Any, workspace: str, fields: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    store = drafts._store_dir(config)
    digest = hashlib.sha256((workspace + "\0sidecar\0" + fields["sidecar_id"]).encode()).hexdigest()
    path = store / (digest + ".json")
    data = drafts._load_sidecar(path)
    if data["immutable"] != fields:
        raise ValueError("share artifact changed")
    publication = data.get("publication")
    if not isinstance(publication, dict):
        publication = {"attempts": [], "receipts": []}
    for key, value in update.items():
        if key == "attempts":
            publication[key] = value
        elif key == "receipt":
            receipt = value
            rows = publication.setdefault("receipts", [])
            for index, row in enumerate(rows):
                if isinstance(row, dict) and row.get("id") == receipt["id"]:
                    rows[index] = {**row, **receipt}
                    break
            else:
                rows.append(receipt)
    data["publication"] = publication
    drafts._write_private(path, json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return data


@asynccontextmanager
async def _locked_proposal(config: Any, workspace: str, fields: dict[str, Any]):
    lock_root = drafts._store_dir(config) / "locks"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(lock_root)
    key = "\0".join((workspace, fields["proposal_id"], str(fields["revision"]), fields["destination_fingerprint"]))
    path = lock_root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    fd = open_fd(path, os.O_CREAT | os.O_RDWR, 0o600, follow_symlinks=False)
    try:
        if not is_private_descriptor_strict(fd):
            raise OSError("share publication lock is not private")
        await await_exclusive(fd)
        yield
    finally:
        os.close(fd)


def _verify_destination(
    config: Any, folder: Path, link: SharedBrainLink, fields: dict[str, Any], *,
    allow_legacy_lifetime: bool = False,
) -> tuple[str, str] | None:
    if link.kind != "git":
        return None
    if fields.get("link_lifetime_id") != getattr(link, "lifetime_id", None) and not (
        allow_legacy_lifetime and "link_lifetime_id" not in fields
    ):
        return None
    if shared_brain.path_problem(config, link.path):
        return None
    from ciao.workspaces import (
        WorkspaceRegistryConflict,
        ensure_registry_current,
        persisted_checkout_links,
        workspace_registry_claim,
    )

    try:
        with workspace_registry_claim(config) as owner:
            ensure_registry_current(config)
            if owner.current is None:
                return None
            if not any(
                row.get("name") == link.name
                and row.get("identity") == link.identity
                and row.get("lifetime_id", link.lifetime_id) == link.lifetime_id
                for _workspace, row in persisted_checkout_links(owner.current, link.path)
            ):
                return None
            observation = shared_brain.observe_under_fence(config, link, owner)
    except (OSError, ValueError, WorkspaceRegistryConflict):
        return None
    if observation.state.get("problem"):
        return None
    configured = list(observation.configured_urls)
    if len(configured) != 1:
        return None
    raw_origin = configured[0]
    try:
        if shared_brain.canonical_identity(raw_origin) != fields["origin_identity"]:
            return None
    except ValueError:
        return None
    effective: list[str] = []
    for urls in (observation.fetch_urls, observation.push_urls):
        if len(urls) != 1:
            return None
        url = urls[0]
        try:
            if shared_brain.canonical_identity(url) != fields["origin_identity"]:
                return None
        except ValueError:
            return None
        effective.append(drafts._origin_fingerprint(url))
    if effective[0] != effective[1] or effective[0] != fields.get("destination_fingerprint"):
        return None
    ref = fields.get("upstream_ref")
    if not isinstance(ref, str) or not ref.startswith("refs/heads/") or ref in {"refs/heads/", "refs/heads/HEAD"}:
        return None
    code, _, _ = _safe_git(folder, "check-ref-format", ref, limit=256)
    if code:
        return None
    code, upstream, _ = _safe_git(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", limit=256)
    if code or upstream.decode("utf-8", "strict").strip() != "origin/" + ref.removeprefix("refs/heads/"):
        return None
    # Pass the verified remote name to Git, never a URL that could carry credentials in argv.
    return "origin", ref


def _fetch(folder: Path, origin: str, ref: str) -> tuple[str, bytes | None, bytes | None]:
    tracking = "refs/remotes/origin/" + ref.removeprefix("refs/heads/")
    code, _, err = _network_git(folder, "fetch", "--no-tags", "--no-recurse-submodules", origin, f"{ref}:{tracking}", limit=4096)
    if code:
        return "unavailable", None, err
    code, out, _ = _safe_git(folder, "rev-parse", "--verify", tracking + "^{commit}", limit=256)
    if code:
        return "unavailable", None, None
    return "ok", out, None


def _blob(folder: Path, commit: str, path: str) -> tuple[str, str | None, bytes | None, str | None]:
    parts = path.split("/")
    mode = kind = oid = ""
    for index in range(len(parts)):
        prefix = "/".join(parts[: index + 1])
        code, listing, _ = _safe_git(folder, "ls-tree", "-z", commit, "--", ":(literal)" + prefix, limit=8192)
        if code:
            return "unavailable", None, None, None
        row = listing.rstrip(b"\0")
        if not row:
            return "absent", None, None, None
        meta, sep, raw_path = row.partition(b"\t")
        try:
            if not sep or raw_path.decode("utf-8", "strict") != prefix:
                return "unsafe", None, None, None
            mode, kind, oid = meta.decode("ascii").split()
        except (UnicodeError, ValueError):
            return "unsafe", None, None, None
        if index < len(parts) - 1:
            if mode != "040000" or kind != "tree":
                return "unsafe", None, None, None
        elif kind != "blob" or mode not in {"100644", "100755"} or not _OID.fullmatch(oid):
            return "unsafe", None, None, None
    code, content, _ = _safe_git(folder, "cat-file", "blob", oid, limit=drafts.MAX_BYTES + 1)
    if code or len(content) > drafts.MAX_BYTES:
        return "unsafe", None, None, None
    return "blob", oid, content, mode


def _tree_has_attributes(folder: Path, commit: str) -> bool:
    code, listing, _ = _safe_git(folder, "ls-tree", "-r", "--name-only", "-z", commit, limit=1_000_000)
    if code:
        return True
    return any(Path(item.decode("utf-8", "replace")).name == ".gitattributes" for item in listing.split(b"\0") if item)


def _prepare_commit(folder: Path, parent: str, fields: dict[str, Any], attempt: int) -> tuple[str | None, str | None]:
    if _tree_has_attributes(folder, parent):
        return None, "unsupported_attributes"
    op = fields["operation"]
    path = fields["path"]
    mode = "100644"
    if fields.get("base_blob_oid"):
        state, _, _, existing_mode = _blob(folder, parent, path)
        if state != "blob" or existing_mode is None:
            return None, "changed_upstream"
        mode = existing_mode
    with tempfile.TemporaryDirectory(prefix="ciao-share-index-") as tmp:
        index = Path(tmp) / "index"
        env = {"GIT_INDEX_FILE": str(index)}
        code, _, err = _safe_git(folder, "read-tree", parent, env=env)
        if code:
            return None, redact_text(err.decode("utf-8", "replace"))[:300]
        if op == "delete":
            code, _, err = _safe_git(folder, "update-index", "--force-remove", "--", path, env=env)
            if code:
                return None, "target_changed"
        else:
            content = fields["proposed"].encode("utf-8")
            oid = _hash_blob(folder, content)
            if oid is None:
                return None, "object_write_failed"
            code, _, err = _safe_git(folder, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}", env=env)
            if code:
                return None, "target_changed"
        code, tree_raw, err = _safe_git(folder, "write-tree", env=env, limit=256)
        if code:
            return None, redact_text(err.decode("utf-8", "replace"))[:300]
        tree = _text(tree_raw)
        trailer = f"Ciaobot-Share-Proposal: {fields['proposal_id']}"
        message = f"Publish shared note proposal {fields['proposal_id']}\n\n{trailer}\n"
        code, commit_raw, err = _safe_git(folder, "commit-tree", tree, "-p", parent, "-m", message, limit=256)
        if code:
            return None, redact_text(err.decode("utf-8", "replace"))[:300]
        commit = _text(commit_raw)
        return (commit, None) if _OID.fullmatch(commit) else (None, "commit_failed")


def _verify_commit(folder: Path, commit: str, parent: str, fields: dict[str, Any]) -> bool:
    if not _OID.fullmatch(commit):
        return False
    code, actual_parent, _ = _safe_git(folder, "rev-parse", "--verify", commit + "^", limit=256)
    code2, message, _ = _safe_git(folder, "show", "-s", "--format=%B", commit, limit=2048)
    if code or code2 or _text(actual_parent) != parent or f"Ciaobot-Share-Proposal: {fields['proposal_id']}".encode() not in message:
        return False
    code, changed, _ = _safe_git(folder, "diff-tree", "--no-commit-id", "--name-status", "-r", "-z", parent, commit, limit=4096)
    expected_status = {"create": b"A", "replace": b"M", "delete": b"D"}[fields["operation"]]
    if code or changed.split(b"\0")[:2] != [expected_status, fields["path"].encode("utf-8")]:
        return False
    state, oid, content, mode = _blob(folder, commit, fields["path"])
    if fields["operation"] == "delete":
        return state == "absent"
    base_state, _base_oid, _base_content, base_mode = _blob(folder, parent, fields["path"])
    expected_mode = base_mode if base_state == "blob" else "100644"
    if state != "blob" or mode != expected_mode:
        return False
    return state == "blob" and content == fields["proposed"].encode("utf-8") and oid is not None


def _is_direct_child(folder: Path, commit: str, tip: str) -> bool:
    """Prove the commit has exactly one parent and it is the reviewed tip."""
    if not _OID.fullmatch(commit) or not _OID.fullmatch(tip):
        return False
    code, out, _ = _safe_git(folder, "rev-list", "--parents", "-n", "1", commit, limit=256)
    return code == 0 and _text(out).split(" ") == [commit, tip]


def _reachable(folder: Path, commit: str, tracking: str, fields: dict[str, Any]) -> bool:
    code, _, _ = _safe_git(folder, "merge-base", "--is-ancestor", commit, tracking, limit=256)
    code2, parent_raw, _ = _safe_git(folder, "rev-parse", "--verify", commit + "^", limit=256)
    if code or code2:
        return False
    parent = _text(parent_raw)
    return _verify_commit(folder, commit, parent, fields)


def _porcelain_rejection(stdout: bytes, ref: str, commit: str) -> Literal["retryable", "refused"] | None:
    """Read only Git's porcelain status for the exact frozen ref; diagnostics are untrusted."""
    for raw_line in stdout.splitlines():
        try:
            flag, target, status = (part.decode("utf-8", "strict") for part in raw_line.split(b"\t", 2))
        except UnicodeDecodeError:
            continue
        except ValueError:
            continue
        source, separator, destination = target.rpartition(":")
        if flag != "!" or not separator or source != commit or destination != ref:
            continue
        if status == "[rejected]" or status.startswith("[rejected] "):
            return "retryable"
        if status == "[remote rejected]" or status.startswith("[remote rejected] "):
            return "refused"
    return None


def _folder_chain_matches(
    recorded: Any, chain: list[folder_io.DirectoryIdentity],
) -> bool:
    if not isinstance(recorded, list) or len(recorded) != len(chain):
        return False
    for row, current in zip(recorded, chain):
        if (
            not isinstance(row, dict)
            or type(row.get("dev")) is not int
            or type(row.get("ino")) is not int
            or row["dev"] != current.dev
            or row["ino"] != current.ino
        ):
            return False
    return True


@contextmanager
def _folder_target(fields: dict[str, Any]):
    """Open the reviewed folder chain and verify it is still the same directories."""
    path = fields.get("checkout_path")
    relative = fields.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(relative, str):
        raise folder_io.FolderChanged("the reviewed folder path is invalid")
    parts = tuple(relative.split("/"))
    parent_fd, chain = folder_io.open_chain(path, parts)
    try:
        if not _folder_chain_matches(fields.get("folder_chain"), chain):
            raise folder_io.FolderChanged("the shared folder path changed after review")
        yield parent_fd, chain
    finally:
        os.close(parent_fd)


def folder_manual_surface(fields: dict[str, Any]) -> dict[str, Any]:
    """Where and against which version a person applies a folder update by hand.

    Read-only: it opens the reviewed chain, stats and reads the note, and
    never stages, links, renames or deletes. The engine does not apply an
    update or delete to an existing note in an ordinary folder, because no
    write there can atomically test the version it was reviewed against while
    an external editor or cloud client is also writing. Seeing the file now at
    the reviewed version, already changed or already applied does not make the
    change an engine publication; there is never a receipt for it.
    """
    root = str(fields.get("checkout_path") or "")
    relative = str(fields.get("path") or "")
    operation = str(fields.get("operation") or "")
    reviewed = fields.get("base_blob_oid")
    proposed = fields.get("proposed")
    state = "unavailable"
    current: str | None = None
    try:
        with _folder_target(fields) as (parent_fd, _chain):
            name = relative.rsplit("/", 1)[-1]
            entry = folder_io.stat_entry(parent_fd, name)
            if entry is None:
                state = "applied" if operation == "delete" else "changed"
            elif stat.S_ISREG(entry.st_mode) and not folder_io.is_dataless(entry):
                data, _opened = folder_io.read_regular(parent_fd, name, drafts.MAX_BYTES + 1)
                if len(data) <= drafts.MAX_BYTES:
                    current = drafts.folder_version(data)
                    if operation == "replace" and isinstance(proposed, str) and data == proposed.encode("utf-8"):
                        state = "applied"
                    else:
                        state = "ready" if current == reviewed else "changed"
    except (folder_io.FolderChanged, OSError):
        state = "unavailable"
        current = None
    return {
        "root": root,
        "path": relative,
        "absolute_path": str(Path(root) / relative),
        "operation": operation,
        "reviewed_version": reviewed,
        "current_version": current,
        "state": state,
        "reason": "folder_update_unsupported",
    }


def _folder_parts(fields: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    path = fields.get("checkout_path")
    relative = fields.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(relative, str):
        raise folder_io.FolderChanged("the reviewed folder path is invalid")
    return path, tuple(relative.split("/"))


def _revalidate_visible_chain(
    fields: dict[str, Any], chain: list[folder_io.DirectoryIdentity],
) -> None:
    path, parts = _folder_parts(fields)
    folder_io.revalidate_chain(path, parts, chain)


def _folder_staging_root(
    config: Any, workspace: str, fields: dict[str, Any], parent_fd: int,
) -> str:
    """Pick or create trusted staging on the destination filesystem.

    Engine state is used when it is already on the destination volume. A
    mounted folder on another volume gets a private staging directory at that
    volume's root, but only when it is outside every linked/synced tree and can
    be opened without modifying an existing object. Otherwise the caller
    offers manual export.
    """
    store = drafts._storage_safe(config, workspace)
    store_fd = folder_io.open_directory_nofollow(str(store))
    try:
        engine_root = store / "staging"
        try:
            engine_fd, _identity = folder_io.open_or_create_private_dir(store_fd, "staging")
        except FileExistsError:
            raise
        try:
            if folder_io.same_mount(engine_fd, parent_fd):
                return str(engine_root)
        finally:
            os.close(engine_fd)
    finally:
        os.close(store_fd)
    volume_root = _folder_volume_root(fields, parent_fd)
    candidate = Path(volume_root) / MOUNT_STAGING_DIR
    if not _folder_staging_path_safe(config, candidate):
        raise folder_io.UnsupportedFilesystem(
            errno.EXDEV, "no trusted staging directory outside synced trees on this volume"
        )
    volume_fd = folder_io.open_directory_nofollow(volume_root)
    try:
        if not folder_io.same_mount(volume_fd, parent_fd):
            raise folder_io.UnsupportedFilesystem(
                errno.EXDEV, "staging is not on the destination filesystem"
            )
        child_fd, _identity = folder_io.open_or_create_private_dir(
            volume_fd, MOUNT_STAGING_DIR
        )
        try:
            if not folder_io.same_mount(child_fd, parent_fd):
                raise folder_io.UnsupportedFilesystem(
                    errno.EXDEV, "staging is not on the destination filesystem"
                )
        finally:
            os.close(child_fd)
    finally:
        os.close(volume_fd)
    return str(candidate)

def _folder_volume_root(fields: dict[str, Any], parent_fd: int) -> str:
    """The mount root containing the linked folder, by device identity."""
    checkout = fields.get("checkout_path")
    if not isinstance(checkout, str) or not Path(checkout).is_absolute():
        raise folder_io.UnsupportedFilesystem(errno.EINVAL, "the linked folder path is invalid")
    device = os.fstat(parent_fd).st_dev
    candidate = Path(checkout)
    while True:
        parent = candidate.parent
        if parent == candidate:
            break
        try:
            parent_device = os.stat(parent).st_dev
        except OSError as exc:
            raise folder_io.UnsupportedFilesystem(
                errno.EXDEV, "the destination mount root cannot be inspected"
            ) from exc
        if parent_device != device:
            break
        candidate = parent
    return str(candidate)


def _folder_staging_path_safe(config: Any, candidate: Path) -> bool:
    """Reject staging inside the install, a vault, a shared brain, or export/archive."""
    try:
        target = Path(os.path.realpath(candidate))
    except OSError:
        return False
    protected = [Path(config.workspace_root), Path(config.state_path).parent]
    for name in config.workspace_names():
        try:
            protected.append(Path(config.workspace_vault_root(name)))
        except ValueError:
            continue
        record = config.workspace(name) if hasattr(config, "workspace") else None
        protected.extend(Path(link.path) for link in (getattr(record, "shared_brains", []) or []))
    export_root = getattr(config, "workspace_export_root", None)
    if export_root:
        protected.append(Path(export_root))
    from ciao.workspace_archive import archive_root

    protected.append(archive_root(config))
    for root in protected:
        try:
            root = Path(os.path.realpath(root))
        except OSError:
            return False
        if target == root or target.is_relative_to(root):
            return False
    return True


def _folder_same_mount(staging_root: str, parent_fd: int) -> bool:
    """Hard links need staging and the destination on one filesystem."""
    return os.stat(staging_root).st_dev == os.fstat(parent_fd).st_dev


def _open_folder_staging(
    staging_root: str, attempt: dict[str, Any],
) -> int:
    name = attempt.get("staging_name")
    dev = attempt.get("staging_dev")
    ino = attempt.get("staging_ino")
    if not isinstance(name, str) or type(dev) is not int or type(ino) is not int:
        raise folder_io.FolderChanged("the staging directory identity is missing")
    return folder_io.open_staging_dir(staging_root, name, expected_dev=dev, expected_ino=ino)


def _cleanup_folder_staging(attempt: dict[str, Any]) -> None:
    """Best-effort staging cleanup that never removes a replaced or external file."""
    staging_root = attempt.get("staging_root")
    if not isinstance(staging_root, str) or not Path(staging_root).is_absolute():
        return
    try:
        staging_fd = _open_folder_staging(staging_root, attempt)
    except OSError:
        return
    try:
        name = str(attempt["staging_name"])
        dev = attempt.get("temp_dev")
        ino = attempt.get("temp_ino")
        token = attempt.get("token")
        folder_io.discard_staging(
            staging_fd, name, staging_root,
            expected_dev=dev if type(dev) is int else None,
            expected_ino=ino if type(ino) is int else None,
            token=token if isinstance(token, str) else None,
        )
    finally:
        os.close(staging_fd)

def _folder_verify_link(config: Any, link: SharedBrainLink, fields: dict[str, Any]) -> bool:
    if link.kind != "folder" or fields.get("link_kind") != "folder":
        return False
    if getattr(link, "lifetime_id", None) != fields.get("link_lifetime_id"):
        return False
    if link.identity != fields.get("origin_identity"):
        return False
    if os.path.realpath(link.path) != link.path:
        return False
    if drafts.folder_destination_fingerprint(link) != fields.get("destination_fingerprint"):
        return False
    state = shared_brain._brain_state_owned(link, config)
    return not state.get("problem")


def _folder_entry_matches(
    parent_fd: int, fields: dict[str, Any], attempt: dict[str, Any],
    proposed: bytes, *, staging_fd: int | None = None,
) -> tuple[str, os.stat_result | None]:
    """Classify the destination against the live staged inode and operation tag.

    ``match`` returns the ``fstat`` of the descriptor that was actually read,
    so the caller never re-resolves the mutable final name for the receipt.
    """
    name = str(fields["path"]).rsplit("/", 1)[-1]
    owned_staging = staging_fd is None
    if staging_fd is None:
        staging_root = attempt.get("staging_root")
        if not isinstance(staging_root, str) or not Path(staging_root).is_absolute():
            return "indeterminate", None
        try:
            staging_fd = _open_folder_staging(staging_root, attempt)
        except OSError:
            return "indeterminate", None
    try:
        try:
            staged = os.stat("note", dir_fd=staging_fd, follow_symlinks=False)
        except OSError:
            return "indeterminate", None
        expected_dev = attempt.get("temp_dev")
        expected_ino = attempt.get("temp_ino")
        if (
            not stat.S_ISREG(staged.st_mode)
            or (type(expected_dev) is int and staged.st_dev != expected_dev)
            or (type(expected_ino) is int and staged.st_ino != expected_ino)
        ):
            return "indeterminate", None
        try:
            fd = folder_io.open_regular(parent_fd, name)
        except FileNotFoundError:
            return "absent", None
        except OSError:
            return "indeterminate", None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                return "conflict", None
            if opened.st_dev != staged.st_dev or opened.st_ino != staged.st_ino:
                return "conflict", None
            try:
                tag = folder_io.read_operation_tag(fd)
            except folder_io.UnsupportedFilesystem:
                return "unsupported", None
            except OSError:
                return "indeterminate", None
            if tag != attempt.get("token"):
                return "indeterminate", None
            if folder_io.read_all(fd, len(proposed) + 1) != proposed:
                return "conflict", None
            return "match", opened
        finally:
            os.close(fd)
    finally:
        if owned_staging:
            os.close(staging_fd)


def _folder_attempt_fields(fields: dict[str, Any], attempt: Any) -> bool:
    token = attempt.get("token") if isinstance(attempt, dict) else None
    return bool(
        isinstance(attempt, dict)
        and attempt.get("kind") == "folder"
        and type(attempt.get("attempt")) is int
        and attempt.get("attempt", 0) >= 1
        and attempt.get("proposal_id") == fields.get("proposal_id")
        and attempt.get("revision") == fields.get("revision")
        and attempt.get("destination_fingerprint") == fields.get("destination_fingerprint")
        and attempt.get("path") == fields.get("path")
        and isinstance(token, str)
        and bool(token)
        and attempt.get("staging_name") == f".ciao-share-{token}.d"
        and isinstance(attempt.get("staging_root"), str)
        and Path(str(attempt.get("staging_root"))).is_absolute()
    )


def _make_folder_receipt(
    fields: dict[str, Any], token: str, proposed: bytes, entry: os.stat_result,
) -> dict[str, Any]:
    return {
        "id": _folder_receipt_id(fields, token),
        "workspace": fields["workspace"], "ts": datetime.now(UTC).isoformat(),
        "proposal_id": fields["proposal_id"], "revision": fields["revision"],
        "brain": fields["brain"], "identity": fields["origin_identity"],
        "path": fields["path"], "operation": "create",
        "destination_fingerprint": fields["destination_fingerprint"],
        "link_lifetime_id": fields.get("link_lifetime_id"),
        "link_kind": "folder", "attempt_token": token,
        "content_sha256": hashlib.sha256(proposed).hexdigest(),
        "content_bytes": len(proposed),
        "file_dev": entry.st_dev, "file_ino": entry.st_ino,
        "commit_oid": None, "published_blob_oid": None,
        "base_commit": None, "base_blob_oid": None,
        "scope": "historical_local_write", "local_only": True, "sync_claimed": False,
        "undoable": False,
    }


def _write_folder_attempts(
    config: Any, workspace: str, fields: dict[str, Any], artifact: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> PublishOutcome | None:
    """Persist an attempt (or its resolution) under the same fence as dismissal."""
    queue = Path(config.workspace_vault_root(workspace)) / drafts._QUEUE
    try:
        with drafts._thread_queue_lock(queue), memory_receipts.queue_lock(queue):
            visible = drafts.load_share(config, workspace=workspace, proposal_id=fields["proposal_id"])
            if (
                isinstance(visible, drafts.ShareRefusal)
                or visible.get("review_token") != artifact.get("review_token")
                or visible.get("immutable") != fields
            ):
                return PublishOutcome("draft_corrupt", message="The share proposal is no longer active in its queue.")
            try:
                _write_state(config, workspace, fields, {"attempts": attempts})
            except (OSError, ValueError, drafts.ShareDraftError):
                return PublishOutcome("draft_unavailable", message="The publication attempt could not be recorded safely.")
    except memory_receipts.QueueLockError:
        return PublishOutcome("draft_unavailable", message="The share proposal queue could not be locked safely.")
    return None


def _folder_status_attempt(
    attempts: list[dict[str, Any]], index: int, *, retryable: bool, reason: str,
) -> list[dict[str, Any]]:
    updated = [dict(attempt) for attempt in attempts]
    updated[index]["status"] = "rejected"
    updated[index]["retryable"] = retryable
    updated[index]["reason"] = reason
    return updated


def _folder_advance_attempt(
    attempts: list[dict[str, Any]], index: int, status: str,
) -> list[dict[str, Any]]:
    updated = [dict(attempt) for attempt in attempts]
    updated[index]["status"] = status
    updated[index].pop("retryable", None)
    updated[index].pop("reason", None)
    return updated


def _publish_folder_locked(
    config: Any, workspace: str, link: SharedBrainLink, fields: dict[str, Any],
    artifact: dict[str, Any],
) -> PublishOutcome:
    """Stage in engine state, link one verified inode, and receipt only after flush."""
    current = _artifact_for(config, workspace, fields["proposal_id"])
    if current is None or current.get("immutable") != fields:
        return PublishOutcome("draft_corrupt", message="The private share artifact changed during publication.")
    publication = current.get("publication")
    if isinstance(publication, dict):
        for receipt in publication.get("receipts", []):
            if isinstance(receipt, dict) and receipt.get("revision") == fields.get("revision") and _valid_receipt(fields, receipt):
                return PublishOutcome("already_published", None, receipt.get("id"))
    if not _folder_verify_link(config, link, fields):
        return PublishOutcome("brain_changed", message="The shared folder destination changed after review.")
    if fields.get("operation") != "create" or not isinstance(fields.get("proposed"), str):
        return PublishOutcome(
            "folder_update_unsupported",
            message="Updating or deleting an existing note in an ordinary folder is not supported yet; apply the reviewed change by hand.",
        )
    proposed = str(fields["proposed"]).encode("utf-8")
    if hashlib.sha256(proposed).hexdigest() != fields.get("proposed_sha256"):
        return PublishOutcome("draft_corrupt", message="The reviewed note bytes could not be verified.")
    attempts = current.get("publication", {}).get("attempts", [])
    if not isinstance(attempts, list):
        return PublishOutcome("draft_corrupt", message="The recorded publication attempts are unreadable.")
    try:
        target = _folder_target(fields)
        parent_fd, chain = target.__enter__()
    except folder_io.FolderChanged:
        return PublishOutcome("unsafe_target", message="The shared folder path changed after review.")
    except FileNotFoundError:
        return PublishOutcome("brain_unavailable", message="The shared folder is not available.")
    except OSError:
        return PublishOutcome("brain_unavailable", message="The shared folder could not be opened safely.")
    try:
        final_name = str(fields["path"]).rsplit("/", 1)[-1]
        # Resolve every attempt before starting a new one. Only a durably
        # flushed attempt can become a receipt; pending, linked-but-unflushed
        # and ambiguous attempts stay indeterminate.
        for index, attempt in enumerate(attempts):
            if not _folder_attempt_fields(fields, attempt):
                return PublishOutcome("indeterminate", message="A recorded publication attempt is malformed.")
            if attempt.get("status") == "rejected":
                if attempt.get("retryable") is True:
                    if folder_io.stat_entry(parent_fd, final_name) is not None:
                        return PublishOutcome("changed_upstream", message="The reviewed note destination now exists; nothing was overwritten.")
                    continue
                if attempt.get("reason") == "unsupported_tag":
                    return PublishOutcome("manual_export_required", message="This volume cannot carry the operation tag; export the note manually.")
                return PublishOutcome("changed_upstream", message="The reviewed note destination already existed; nothing was overwritten.")
            resolution, verified_entry = _folder_entry_matches(
                parent_fd, fields, attempt, proposed,
            )
            if resolution == "match" and attempt.get("status") == "flushed" and verified_entry is not None:
                receipt = _make_folder_receipt(fields, str(attempt["token"]), proposed, verified_entry)
                try:
                    _write_state(config, workspace, fields, {"receipt": receipt})
                except (OSError, ValueError, drafts.ShareDraftError):
                    return PublishOutcome("indeterminate", message="The publish was confirmed but its receipt needs recovery.")
                _cleanup_folder_staging(attempt)
                return PublishOutcome("published", None, receipt["id"])
            if resolution == "conflict":
                if attempt.get("status") != "flushed":
                    # A pending/linked attempt has no proof that the link was
                    # not ours-then-replaced; retain it and the staging inode.
                    return PublishOutcome("indeterminate", message="The destination was replaced after an unproven link; reconcile before retrying.")
                updated = _folder_status_attempt(attempts, index, retryable=False, reason="conflict")
                failure = _write_folder_attempts(config, workspace, fields, artifact, updated)
                if failure is not None:
                    return failure
                _cleanup_folder_staging(attempt)
                return PublishOutcome("changed_upstream", message="The destination changed after review; nothing was overwritten.")
            if resolution == "unsupported":
                return PublishOutcome("folder_unverifiable", message="This volume cannot prove the completed write, so nothing was claimed.")
            return PublishOutcome("indeterminate", message="A recorded create cannot be proven flushed and complete; reconcile before retrying.")
        if len(attempts) >= MAX_ATTEMPTS:
            return PublishOutcome("indeterminate", message="No further folder publication attempt is allowed without re-review.")
        # Capability checks gate only a NEW attempt. Existing attempts were
        # reconciled above even when this volume can no longer start one.
        try:
            staging_root = _folder_staging_root(config, workspace, fields, parent_fd)
            same_mount = _folder_same_mount(staging_root, parent_fd)
        except OSError:
            return PublishOutcome(
                "manual_export_required",
                message="No trusted staging directory is available on the destination filesystem; export the note manually.",
            )
        if not same_mount:
            return PublishOutcome(
                "manual_export_required",
                message="No trusted staging directory is available on the destination filesystem; export the note manually.",
            )
        if not folder_io.verified_hardlink_support(staging_root):
            return PublishOutcome(
                "manual_export_required",
                message="This filesystem cannot hard-link from trusted staging; export the note manually.",
            )
        if not folder_io.verified_rename_observability(staging_root):
            return PublishOutcome(
                "manual_export_required",
                message="This volume does not expose a verifiable move-away guard; export the note manually.",
            )
        if folder_io.stat_entry(parent_fd, final_name) is not None:
            return PublishOutcome("changed_upstream", message="The reviewed note destination now exists; nothing was overwritten.")
        if folder_io.has_cloud_placeholder(parent_fd, final_name):
            return PublishOutcome("unsafe_target", message="A cloud placeholder now stands in for this note; nothing was written.")
        token = secrets.token_urlsafe(24)
        staging_name = f".ciao-share-{token}.d"
        new_attempt: dict[str, Any] = {
            "attempt": len(attempts) + 1, "kind": "folder", "status": "pending",
            "token": token, "staging_name": staging_name,
            "staging_root": staging_root,
            "proposal_id": fields["proposal_id"], "revision": fields["revision"],
            "destination_fingerprint": fields["destination_fingerprint"],
            "path": fields["path"],
        }
        # The durable intent exists before the destination is touched.
        updated = [*attempts, new_attempt]
        failure = _write_folder_attempts(config, workspace, fields, artifact, updated)
        if failure is not None:
            return failure
        try:
            staging_fd, staging_identity = folder_io.create_staging_dir(staging_root, staging_name)
        except FileExistsError:
            rejected = _folder_status_attempt(updated, len(updated) - 1, retryable=True, reason="staging_exists")
            _write_folder_attempts(config, workspace, fields, artifact, rejected)
            return PublishOutcome("draft_unavailable", message="A private staging name already exists; retry the review.")
        except OSError:
            rejected = _folder_status_attempt(updated, len(updated) - 1, retryable=True, reason="staging_unavailable")
            _write_folder_attempts(config, workspace, fields, artifact, rejected)
            return PublishOutcome("draft_unavailable", message="The note could not be staged safely; nothing was published.")
        try:
            new_attempt["staging_dev"] = staging_identity.dev
            new_attempt["staging_ino"] = staging_identity.ino
            failure = _write_folder_attempts(config, workspace, fields, artifact, updated)
            if failure is not None:
                folder_io.discard_staging(staging_fd, staging_name, staging_root)
                return failure
            try:
                temp_dev, temp_ino = folder_io.create_staged_file(staging_fd, "note", proposed, token)
            except folder_io.UnsupportedFilesystem:
                folder_io.discard_staging(staging_fd, staging_name, staging_root)
                rejected = _folder_status_attempt(updated, len(updated) - 1, retryable=False, reason="unsupported_tag")
                _write_folder_attempts(config, workspace, fields, artifact, rejected)
                return PublishOutcome("manual_export_required", message="This volume cannot carry the operation tag that proves a completed write; export the note manually.")
            except OSError:
                folder_io.discard_staging(staging_fd, staging_name, staging_root)
                rejected = _folder_status_attempt(updated, len(updated) - 1, retryable=True, reason="staged_write_failed")
                _write_folder_attempts(config, workspace, fields, artifact, rejected)
                return PublishOutcome("draft_unavailable", message="The note could not be staged safely; nothing was published.")
            new_attempt["temp_dev"] = temp_dev
            new_attempt["temp_ino"] = temp_ino
            failure = _write_folder_attempts(config, workspace, fields, artifact, updated)
            if failure is not None:
                folder_io.discard_staging(
                    staging_fd, staging_name, staging_root,
                    expected_dev=temp_dev, expected_ino=temp_ino, token=token,
                )
                return failure
            try:
                ancestor_before = folder_io.ancestor_stamps(parent_fd)
            except OSError:
                return PublishOutcome("indeterminate", message="The approved folder path cannot be proven stable; reconcile before retrying.")
            try:
                _revalidate_visible_chain(fields, chain)
            except folder_io.FolderChanged:
                folder_io.discard_staging(
                    staging_fd, staging_name, staging_root,
                    expected_dev=temp_dev, expected_ino=temp_ino, token=token,
                )
                return PublishOutcome("indeterminate", message="The approved folder path changed before the write; nothing was published.")
            try:
                folder_io.link_no_clobber(
                    staging_fd, "note", parent_fd, final_name,
                    expected_dev=temp_dev, expected_ino=temp_ino, token=token,
                )
            except folder_io.UnsupportedFilesystem:
                folder_io.discard_staging(
                    staging_fd, staging_name, staging_root,
                    expected_dev=temp_dev, expected_ino=temp_ino, token=token,
                )
                rejected = _folder_status_attempt(updated, len(updated) - 1, retryable=False, reason="unsupported_mount")
                _write_folder_attempts(config, workspace, fields, artifact, rejected)
                return PublishOutcome("manual_export_required", message="This folder cannot be hard-linked from private staging; export the note manually.")
            except folder_io.FolderChanged:
                return PublishOutcome("indeterminate", message="The staged note changed before linking; reconcile before retrying.")
            except OSError:
                # No post-syscall errno is a portable proof that the link did
                # not complete on an unverified volume. Keep the proposal and
                # staging evidence; only reconciliation may resolve it.
                return PublishOutcome("indeterminate", message="The filesystem did not confirm the link; reconcile before retrying.")
            try:
                ancestor_after = folder_io.ancestor_stamps(parent_fd)
            except OSError:
                return PublishOutcome("indeterminate", message="The approved folder path cannot be proven stable; reconcile before retrying.")
            if ancestor_after != ancestor_before:
                return PublishOutcome("indeterminate", message="The approved folder path changed during the write; reconcile before retrying.")
            linked = _folder_advance_attempt(updated, len(updated) - 1, "linked")
            failure = _write_folder_attempts(config, workspace, fields, artifact, linked)
            if failure is not None:
                return PublishOutcome("indeterminate", message="The link succeeded but its durable state could not be recorded; reconcile before retrying.")
            try:
                folder_io.fsync_directory(parent_fd)
            except OSError:
                return PublishOutcome("indeterminate", message="The created note could not be flushed to stable storage; reconcile before retrying.")
            flushed = _folder_advance_attempt(linked, len(linked) - 1, "flushed")
            failure = _write_folder_attempts(config, workspace, fields, artifact, flushed)
            if failure is not None:
                return PublishOutcome("indeterminate", message="The directory flush succeeded but its durable state could not be recorded; reconcile before retrying.")
            try:
                _revalidate_visible_chain(fields, chain)
            except folder_io.FolderChanged:
                return PublishOutcome("indeterminate", message="The approved folder path changed after the write; reconcile before retrying.")
            resolution, verified_entry = _folder_entry_matches(
                parent_fd, fields, new_attempt, proposed,
                staging_fd=staging_fd,
            )
            if resolution == "unsupported":
                return PublishOutcome("folder_unverifiable", message="This volume cannot prove the completed write, so nothing was claimed.")
            if resolution != "match" or verified_entry is None:
                if resolution == "conflict":
                    rejected = _folder_status_attempt(flushed, len(flushed) - 1, retryable=False, reason="conflict")
                    failure = _write_folder_attempts(config, workspace, fields, artifact, rejected)
                    if failure is not None:
                        return failure
                    folder_io.discard_staging(
                        staging_fd, staging_name, staging_root,
                        expected_dev=temp_dev, expected_ino=temp_ino, token=token,
                    )
                    return PublishOutcome("changed_upstream", message="The destination changed while publishing; nothing was overwritten.")
                return PublishOutcome("indeterminate", message="The created note cannot be proven complete; reconcile before retrying.")
            receipt = _make_folder_receipt(fields, token, proposed, verified_entry)
            try:
                _write_state(config, workspace, fields, {"receipt": receipt})
            except (OSError, ValueError, drafts.ShareDraftError):
                return PublishOutcome("indeterminate", message="The publish was confirmed but its receipt needs recovery.")
            folder_io.discard_staging(
                staging_fd, staging_name, staging_root,
                expected_dev=temp_dev, expected_ino=temp_ino, token=token,
            )
            return PublishOutcome("published", None, receipt["id"])
        finally:
            os.close(staging_fd)
    finally:
        target.__exit__(None, None, None)


def _reconcile_folder_locked(
    config: Any, workspace: str, link: SharedBrainLink | None, fields: dict[str, Any],
    artifact: dict[str, Any],
) -> PublishOutcome:
    """Confirm only a durably flushed attempt from its live staged inode and tag."""
    current = _artifact_for(config, workspace, fields["proposal_id"])
    if current is None or current.get("immutable") != fields:
        return PublishOutcome("draft_corrupt", message="The private share artifact changed during reconciliation.")
    artifact = current
    publication = artifact.get("publication")
    attempts = publication.get("attempts", []) if isinstance(publication, dict) else []
    if not isinstance(attempts, list) or not attempts:
        return PublishOutcome("indeterminate", message="There is no recorded folder publication attempt to reconcile.")
    proposed = fields.get("proposed")
    if not isinstance(proposed, str) or fields.get("operation") != "create":
        return PublishOutcome("indeterminate", message="The recorded folder publication is unsupported.")
    payload = proposed.encode("utf-8")
    try:
        target = _folder_target(fields)
        parent_fd, _chain = target.__enter__()
    except folder_io.FolderChanged:
        return PublishOutcome("indeterminate", message="The recorded folder destination changed.")
    except (FileNotFoundError, OSError):
        return PublishOutcome("indeterminate", message="The recorded folder destination is unavailable.")
    try:
        final_name = str(fields["path"]).rsplit("/", 1)[-1]
        for index, attempt in enumerate(attempts):
            if not _folder_attempt_fields(fields, attempt):
                return PublishOutcome("indeterminate", message="A recorded publication attempt is malformed.")
            if attempt.get("status") == "rejected":
                if attempt.get("retryable") is True:
                    continue
                if attempt.get("reason") == "unsupported_tag":
                    return PublishOutcome("manual_export_required", message="This volume cannot prove a completed write; export the note manually.")
                return PublishOutcome("changed_upstream", message="The reviewed destination already existed; nothing was overwritten.")
            resolution, verified_entry = _folder_entry_matches(
                parent_fd, fields, attempt, payload,
            )
            if resolution == "match" and attempt.get("status") == "flushed" and verified_entry is not None:
                receipt = _make_folder_receipt(fields, str(attempt["token"]), payload, verified_entry)
                try:
                    _write_state(config, workspace, fields, {"receipt": receipt})
                except (OSError, ValueError, drafts.ShareDraftError):
                    return PublishOutcome("indeterminate", message="The publish was confirmed but its receipt needs recovery.")
                _cleanup_folder_staging(attempt)
                return PublishOutcome("published", None, receipt["id"])
            if resolution == "conflict":
                if attempt.get("status") != "flushed":
                    return PublishOutcome("indeterminate", message="The destination was replaced after an unproven link; reconcile before retrying.")
                updated = _folder_status_attempt(attempts, index, retryable=False, reason="conflict")
                _write_folder_attempts(config, workspace, fields, artifact, updated)
                _cleanup_folder_staging(attempt)
                return PublishOutcome("changed_upstream", message="The destination changed after review; nothing was overwritten.")
            if resolution == "unsupported":
                return PublishOutcome("folder_unverifiable", message="This volume cannot prove the completed write, so nothing was claimed.")
            return PublishOutcome("indeterminate", message="No recorded folder create can be proven flushed and complete.")
        return PublishOutcome("indeterminate", message="No recorded folder create can be proven flushed and complete.")
    finally:
        target.__exit__(None, None, None)

async def publish_share(config: Any, *, workspace: str, proposal_id: str, expected_revision: str) -> PublishOutcome:
    """Publish the reviewed artifact once; reconcile every persisted attempt first."""
    artifact = _artifact_for(config, workspace, proposal_id)
    if artifact is None:
        return PublishOutcome("draft_corrupt", message="The queued shared-note draft could not be verified.")
    fields = artifact["immutable"]
    if expected_revision != artifact.get("review_token"):
        return PublishOutcome("stale_review", message="The shared-note review has changed.")
    if "evidence_authorization" in fields:
        return PublishOutcome("draft_unavailable", message="This candidate is awaiting publication fencing.")
    publication_raw = artifact.get("publication")
    publication: dict[str, Any] = publication_raw if isinstance(publication_raw, dict) else {}
    receipts = publication.get("receipts", [])
    if isinstance(receipts, list):
        for receipt in receipts:
            if isinstance(receipt, dict) and receipt.get("revision") == fields.get("revision") and _valid_receipt(fields, receipt):
                return PublishOutcome("already_published", receipt.get("commit_oid"), receipt.get("id"))
    visible = drafts.load_share(config, workspace=workspace, proposal_id=proposal_id)
    if isinstance(visible, drafts.ShareRefusal):
        prior_publication = artifact.get("publication")
        if isinstance(prior_publication, dict) and prior_publication.get("attempts"):
            return await reconcile_share(config, workspace=workspace, proposal_id=proposal_id, expected_revision=expected_revision)
        refusal_code: OutcomeCode = "draft_unavailable" if visible.code == "draft_unavailable" else "draft_corrupt"
        return PublishOutcome(refusal_code, message=visible.message)
    if visible.get("review_token") != expected_revision or visible.get("immutable") != fields:
        return PublishOutcome("stale_review", message="The shared-note review has changed.")
    link_record = config.workspace(workspace) if hasattr(config, "workspace") else None
    link = next((item for item in getattr(link_record, "shared_brains", []) or [] if item.name == fields.get("brain")), None)
    if link is None:
        prior_publication = artifact.get("publication")
        if isinstance(prior_publication, dict) and prior_publication.get("attempts"):
            return await reconcile_share(config, workspace=workspace, proposal_id=proposal_id, expected_revision=expected_revision)
        return PublishOutcome("brain_unavailable", message="The selected shared brain is no longer linked.")
    expected_kind = fields.get("link_kind", "git")
    if getattr(link, "kind", "git") != expected_kind:
        if publication.get("attempts"):
            return await reconcile_share(config, workspace=workspace, proposal_id=proposal_id, expected_revision=expected_revision)
        return PublishOutcome("brain_changed", message="The selected shared brain is linked with a different storage kind.")
    if getattr(link, "lifetime_id", None) != fields.get("link_lifetime_id"):
        if publication.get("attempts"):
            return await reconcile_share(config, workspace=workspace, proposal_id=proposal_id, expected_revision=expected_revision)
        return PublishOutcome("brain_unavailable", message="The selected shared brain link lifetime has changed.")
    checkout_lock = shared_brain._checkout_lock(link.path)
    try:
        async with checkout_lock:
            async with shared_brain._checkout_claim(link.path, config):
                active = _active_link(config, workspace, fields)
                if active is None:
                    result = PublishOutcome("reconcile_after_unlink" if has_publication_attempt(
                        config, workspace=workspace, proposal_id=proposal_id,
                        expected_revision=expected_revision,
                    ) else "brain_unavailable", message="The selected shared brain link is no longer active.")
                else:
                    async with _locked_proposal(config, workspace, fields):
                        if active.kind == "folder":
                            result = await run_while_claimed(
                                _publish_folder_locked, config, workspace, active, fields, artifact
                            )
                        else:
                            result = await run_while_claimed(
                                _publish_locked, config, workspace, Path(active.path), active, fields, artifact
                            )
    except (OSError, ValueError, drafts.ShareDraftError):
        return PublishOutcome("draft_unavailable", message="Private publication state is unavailable.")
    if result.outcome == "reconcile_after_unlink":
        return await reconcile_share(config, workspace=workspace, proposal_id=proposal_id,
                                     expected_revision=expected_revision)
    if result.outcome == "published" and link.kind == "git":
        try:
            await shared_brain.update_brain(link, config)
        except Exception:
            pass
    return result


async def reconcile_share(config: Any, *, workspace: str, proposal_id: str, expected_revision: str) -> PublishOutcome:
    """Confirm persisted attempts without issuing another push; safe after settlement or unlink."""
    artifact = _artifact_for(config, workspace, proposal_id)
    if artifact is None or expected_revision != artifact.get("review_token"):
        return PublishOutcome("stale_review", message="The shared-note review has changed.")
    fields = artifact["immutable"]
    publication = artifact.get("publication")
    attempts = publication.get("attempts", []) if isinstance(publication, dict) else []
    if not attempts:
        return PublishOutcome("indeterminate", message="There is no recorded publish attempt to reconcile.")
    for receipt in publication.get("receipts", []) if isinstance(publication, dict) else []:
        if isinstance(receipt, dict) and receipt.get("revision") == fields.get("revision") and _valid_receipt(fields, receipt):
            return PublishOutcome("already_published", receipt.get("commit_oid"), receipt.get("id"))
    path = fields.get("checkout_path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        return PublishOutcome("indeterminate", message="The recorded checkout cannot be verified.")
    if fields.get("link_kind") == "folder":
        checkout_lock = shared_brain._checkout_lock(path)
        try:
            async with checkout_lock:
                async with shared_brain._checkout_claim(path, config):
                    async with _locked_proposal(config, workspace, fields):
                        return await run_while_claimed(
                            _reconcile_folder_locked, config, workspace, None, fields, artifact
                        )
        except (OSError, ValueError, drafts.ShareDraftError):
            return PublishOutcome("indeterminate", message="The recorded folder publication could not be reconciled.")
    link = SharedBrainLink(
        name=str(fields.get("brain", "")), path=path,
        identity=str(fields.get("origin_identity", "")),
        lifetime_id=str(fields.get("link_lifetime_id", "")),
    )
    checkout_lock = shared_brain._checkout_lock(path)
    try:
        async with checkout_lock:
            async with shared_brain._checkout_claim(path, config):
                from ciao.workspaces import persisted_checkout_links, WorkspaceRegistryConflict
                registry_path = Path(config.state_path).parent / "workspaces.json"
                try:
                    persisted_checkout_links(registry_path.read_bytes(), path)
                except (OSError, ValueError, WorkspaceRegistryConflict):
                    return PublishOutcome("indeterminate", message="The recorded checkout cannot be verified.")
                async with _locked_proposal(config, workspace, fields):
                    result = await run_while_claimed(_reconcile_locked, config, workspace, Path(path), link, fields)
    except (OSError, ValueError, drafts.ShareDraftError):
        return PublishOutcome("indeterminate", message="The recorded publish attempt could not be reconciled.")
    return result


def _reconcile_locked(config: Any, workspace: str, folder: Path, link: SharedBrainLink, fields: dict[str, Any]) -> PublishOutcome:
    destination = _verify_destination(config, folder, link, fields, allow_legacy_lifetime=True)
    if destination is None:
        return PublishOutcome("indeterminate", message="The recorded destination cannot be verified.")
    origin, ref = destination
    tracking = "refs/remotes/origin/" + ref.removeprefix("refs/heads/")
    artifact = _artifact_for(config, workspace, fields["proposal_id"])
    if artifact is None or artifact.get("immutable") != fields:
        return PublishOutcome("draft_corrupt", message="The private share artifact changed during reconciliation.")
    attempts = artifact.get("publication", {}).get("attempts", [])
    for attempt in attempts:
        if not isinstance(attempt, dict) or not isinstance(attempt.get("commit_oid"), str):
            return PublishOutcome("indeterminate", message="A recorded publication attempt is malformed.")
        commit = attempt["commit_oid"]
        fetched, _raw, _error = _fetch(folder, origin, ref)
        if fetched != "ok":
            return PublishOutcome("indeterminate", message="A recorded publish attempt could not be reconciled.")
        if _reachable(folder, commit, tracking, fields):
            receipt = _make_receipt(folder, fields, commit)
            try:
                _write_state(config, workspace, fields, {"receipt": receipt})
            except (OSError, ValueError, drafts.ShareDraftError):
                return PublishOutcome("indeterminate", commit, message="The publish was confirmed but its receipt needs recovery.")
            return PublishOutcome("published", commit, receipt["id"])
        if attempt.get("status") == "rejected":
            if attempt.get("retryable") is True:
                continue
            return PublishOutcome("push_refused", message="The shared brain rejected the recorded publish attempt.")
        return PublishOutcome("indeterminate", message="A recorded publish attempt is unresolved; reconcile before retrying.")
    return PublishOutcome("indeterminate", message="No recorded commit is reachable on the approved branch.")


def _publish_locked(config: Any, workspace: str, folder: Path, link: SharedBrainLink, fields: dict[str, Any], artifact: dict[str, Any]) -> PublishOutcome:
    current = _artifact_for(config, workspace, fields["proposal_id"])
    if current is None or current.get("immutable") != fields:
        return PublishOutcome("draft_corrupt", message="The private share artifact changed during publication.")
    artifact = current
    publication = artifact.get("publication")
    if isinstance(publication, dict):
        for receipt in publication.get("receipts", []):
            if isinstance(receipt, dict) and receipt.get("revision") == fields.get("revision") and _valid_receipt(fields, receipt):
                return PublishOutcome("already_published", receipt.get("commit_oid"), receipt.get("id"))
    destination = _verify_destination(config, folder, link, fields)
    if destination is None:
        return PublishOutcome("brain_changed", message="The shared brain destination changed after review.")
    origin, ref = destination
    code, _out, _err = _safe_git(folder, "config", "--get", "core.repositoryformatversion", limit=256)
    if code:
        return PublishOutcome("brain_changed", message="The shared brain checkout could not be verified.")
    attempts = artifact.get("publication", {}).get("attempts", [])
    if not isinstance(attempts, list):
        attempts = []
    tracking = "refs/remotes/origin/" + ref.removeprefix("refs/heads/")
    for attempt in attempts:
        if not isinstance(attempt, dict) or not isinstance(attempt.get("commit_oid"), str):
            return PublishOutcome("indeterminate", message="A recorded publication attempt needs reconciliation.")
        commit = attempt["commit_oid"]
        fetched, _raw, _error = _fetch(folder, origin, ref)
        if fetched != "ok":
            return PublishOutcome("indeterminate", message="A prior publish attempt could not be reconciled.")
        code, tracking_raw, _ = _safe_git(folder, "rev-parse", "--verify", tracking + "^{commit}", limit=256)
        if code:
            return PublishOutcome("indeterminate", message="A prior publish attempt could not be reconciled.")
        if _reachable(folder, commit, tracking, fields):
            receipt = _make_receipt(folder, fields, commit)
            try:
                _write_state(config, workspace, fields, {"receipt": receipt})
            except (OSError, ValueError, drafts.ShareDraftError):
                return PublishOutcome("indeterminate", commit, message="The publish was confirmed but its receipt needs recovery.")
            return PublishOutcome("published", commit, receipt["id"])
        if attempt.get("status") == "rejected":
            if attempt.get("retryable") is True:
                continue
            return PublishOutcome("push_refused", message="The shared brain rejected the recorded publish attempt.")
        return PublishOutcome("indeterminate", message="A recorded publish attempt is unresolved; reconcile before retrying.")
    for number in range(len(attempts), MAX_ATTEMPTS):
        fetched, _raw, fetch_error = _fetch(folder, origin, ref)
        if fetched != "ok":
            return PublishOutcome("upstream_unavailable", message="The shared brain upstream could not be read.")
        code, tip_raw, _ = _safe_git(folder, "rev-parse", "--verify", tracking + "^{commit}", limit=256)
        if code:
            return PublishOutcome("upstream_unavailable", message="The shared brain upstream could not be read.")
        tip = _text(tip_raw)
        base_state, base_oid, _base_content, mode = _blob(folder, tip, fields["path"])
        expected_oid = fields.get("base_blob_oid")
        if (expected_oid is None and base_state != "absent") or (
            expected_oid is not None
            and (base_state != "blob" or base_oid != expected_oid
                 or ("base_mode" in fields and mode != fields["base_mode"]))
        ):
            return PublishOutcome("changed_upstream", message="The reviewed note changed upstream; draft it again.")
        commit, error = _prepare_commit(folder, tip, fields, number + 1)
        if commit is None:
            return PublishOutcome("changed_upstream" if error == "changed_upstream" else "push_refused", message="The reviewed note could not be prepared safely.")
        if not _verify_commit(folder, commit, tip, fields):
            return PublishOutcome("push_refused", message="The prepared commit did not match the reviewed note.")
        record = {
            "attempt": number + 1, "commit_oid": commit, "parent_oid": tip,
            "proposal_id": fields["proposal_id"], "revision": fields["revision"],
            "destination_fingerprint": fields["destination_fingerprint"],
            "ref": ref, "status": "pending",
        }
        attempts = [*attempts, record]
        queue = Path(config.workspace_vault_root(workspace)) / drafts._QUEUE
        # Persist the reservation under the same queue lock used by dismissal.
        # Dismissal checks this pending attempt after the lock is released.
        try:
            with drafts._thread_queue_lock(queue), memory_receipts.queue_lock(queue):
                visible = drafts.load_share(config, workspace=workspace, proposal_id=fields["proposal_id"])
                if (
                    isinstance(visible, drafts.ShareRefusal)
                    or visible.get("review_token") != artifact.get("review_token")
                    or visible.get("immutable") != fields
                ):
                    return PublishOutcome("draft_corrupt", message="The share proposal is no longer active in its queue.")
                try:
                    _write_state(config, workspace, fields, {"attempts": attempts})
                except (OSError, ValueError, drafts.ShareDraftError):
                    return PublishOutcome("draft_unavailable", message="The publication attempt could not be recorded safely.")
        except memory_receipts.QueueLockError:
            return PublishOutcome("draft_unavailable", message="The share proposal queue could not be locked safely.")
        if not _is_direct_child(folder, commit, tip):
            return PublishOutcome("push_refused", message="The prepared commit is not a direct child of the reviewed tip.")
        # Expected-old-tip update: the remote ref moves only if it still equals
        # the tip this commit was built on. Never an unconditional force.
        code, push_stdout, stderr = _network_git(
            folder, "push", "--porcelain", f"--force-with-lease={ref}:{tip}", origin, f"{commit}:{ref}",
            limit=4096, timeout_s=PUSH_TIMEOUT_S,
        )
        fetched, _raw, _fetch_error = _fetch(folder, origin, ref)
        if fetched != "ok":
            return PublishOutcome("indeterminate", commit, message="The push response could not be reconciled.")
        if not _reachable(folder, commit, tracking, fields):
            detail = redact_text(stderr.decode("utf-8", "replace"))[:300]
            rejection = _porcelain_rejection(push_stdout, ref, commit) if code != 0 else None
            if rejection is not None:
                retryable = rejection == "retryable"
                try:
                    attempts = _set_attempt_status(config, workspace, fields, attempts, len(attempts) - 1, retryable=retryable)
                except (OSError, ValueError, drafts.ShareDraftError):
                    return PublishOutcome("indeterminate", commit, message="The push rejection could not be recorded safely.")
                if retryable:
                    continue
                return PublishOutcome("push_refused", message=detail or "The shared brain refused the publish.")
            return PublishOutcome("indeterminate", commit, message="The push response is unresolved; reconcile before retrying.")
        receipt = _make_receipt(folder, fields, commit)
        try:
            _write_state(config, workspace, fields, {"receipt": receipt})
        except (OSError, ValueError, drafts.ShareDraftError):
            return PublishOutcome("indeterminate", commit, message="The publish succeeded but the receipt needs recovery.")
        return PublishOutcome("published", commit, receipt["id"])
    return PublishOutcome("indeterminate", message="The upstream changed repeatedly; reconcile before retrying.")


def _make_receipt(folder: Path, fields: dict[str, Any], commit: str) -> dict[str, Any]:
    oid: str | None = None
    if fields["operation"] != "delete":
        state, oid, _content, _mode = _blob(folder, commit, fields["path"])
        if state != "blob":
            raise ValueError("published blob could not be verified")
    receipt = {
        "id": _receipt_id(fields, commit), "workspace": fields["workspace"],
        "ts": datetime.now(UTC).isoformat(),
        "proposal_id": fields["proposal_id"], "revision": fields["revision"],
        "brain": fields["brain"], "identity": fields["origin_identity"],
        "branch": fields["upstream_ref"].removeprefix("refs/heads/"),
        "path": fields["path"], "operation": fields["operation"],
        "destination_fingerprint": fields["destination_fingerprint"],
        "base_commit": fields["base_commit"],
        "base_blob_oid": fields["base_blob_oid"],
        "commit_oid": commit, "published_blob_oid": oid,
    }
    if isinstance(fields.get("link_lifetime_id"), str):
        receipt["link_lifetime_id"] = fields["link_lifetime_id"]
    return receipt


def _set_attempt_status(
    config: Any, workspace: str, fields: dict[str, Any], attempts: list[dict[str, Any]],
    index: int, *, retryable: bool,
) -> list[dict[str, Any]]:
    updated = [dict(attempt) for attempt in attempts]
    updated[index]["status"] = "rejected"
    updated[index]["retryable"] = retryable
    _write_state(config, workspace, fields, {"attempts": updated})
    return updated


def list_share_receipt_details(
    config: Any, *, workspace: str | None = None,
) -> list[dict[str, Any]]:
    """Read canonical, validated publication receipts and their immutable drafts once."""
    store = drafts._store_dir(config)
    rows: dict[str, dict[str, Any]] = {}
    if not store.is_dir():
        return []
    for path in store.glob("*.json"):
        try:
            artifact = drafts._load_sidecar(path)
        except (drafts.ShareDraftError, OSError, ValueError):
            continue
        fields = artifact.get("immutable")
        if not isinstance(fields, dict):
            continue
        owner = fields.get("workspace")
        sidecar_id = fields.get("sidecar_id")
        required_strings = [
            "workspace", "sidecar_id", "proposal_id", "destination_fingerprint",
            "operation", "brain", "origin_identity", "path",
        ]
        if fields.get("link_kind") != "folder":
            required_strings.append("upstream_ref")
        if (
            any(not isinstance(fields.get(key), str) or not fields.get(key) for key in required_strings)
            or type(fields.get("revision")) is not int
            or fields.get("revision", 0) < 1
            or (workspace is not None and owner != workspace)
        ):
            continue
        canonical_name = hashlib.sha256(
            (str(owner) + "\0sidecar\0" + str(sidecar_id)).encode()
        ).hexdigest() + ".json"
        # Request-key index files carry the same document, but only this
        # deterministic path is authoritative for publication state.
        if path.name != canonical_name:
            continue
        publication = artifact.get("publication")
        if not isinstance(publication, dict):
            continue
        receipts = publication.get("receipts")
        if not isinstance(receipts, list):
            continue
        for receipt in receipts:
            try:
                if (
                    not isinstance(receipt, dict)
                    or not isinstance(receipt.get("id"), str)
                    or receipt.get("workspace") != owner
                    or not _valid_receipt(fields, receipt)
                ):
                    continue
                enriched = _receipt_with_metadata(fields, receipt)
            except (KeyError, TypeError, ValueError):
                continue
            rows[str(receipt["id"])] = {
                "receipt": enriched,
                "immutable": dict(fields),
            }
    return sorted(
        rows.values(),
        key=lambda detail: (
            str(detail["receipt"].get("ts", "")),
            str(detail["receipt"].get("id", "")),
        ),
        reverse=True,
    )


def list_share_receipts(config: Any, *, workspace: str) -> list[dict[str, Any]]:
    """List confirmed publication receipts for one workspace only."""
    return [
        detail["receipt"]
        for detail in list_share_receipt_details(config, workspace=workspace)
    ]


def get_share_receipt(
    config: Any, *, workspace: str, proposal_id: str, expected_revision: str,
) -> dict[str, Any] | None:
    """Look up one confirmed receipt using its workspace and exact review token."""
    artifact = _artifact_for(config, workspace, proposal_id)
    if artifact is None or artifact.get("review_token") != expected_revision:
        return None
    publication = artifact.get("publication")
    if not isinstance(publication, dict):
        return None
    revision = artifact["immutable"].get("revision")
    for receipt in publication.get("receipts", []):
        if isinstance(receipt, dict) and receipt.get("workspace") == workspace and receipt.get("proposal_id") == proposal_id and receipt.get("revision") == revision and _valid_receipt(artifact["immutable"], receipt):
            return _receipt_with_metadata(artifact["immutable"], receipt)
    return None


def get_share_receipt_detail(
    config: Any, *, workspace: str, receipt_id: str,
) -> dict[str, Any] | None:
    """Return one private receipt and its immutable review artifact by id."""
    return next(
        (
            detail for detail in list_share_receipt_details(config, workspace=workspace)
            if detail["receipt"].get("id") == receipt_id
        ),
        None,
    )


async def draft_share_reversal(
    config: Any, *, workspace: str, receipt_id: str,
) -> dict[str, str] | drafts.ShareRefusal:
    """Queue an inverse proposal only while the published destination is intact."""
    detail = await asyncio.to_thread(
        get_share_receipt_detail, config, workspace=workspace, receipt_id=receipt_id
    )
    if detail is None:
        return drafts.ShareRefusal("receipt_unavailable", "The shared-note receipt could not be verified.")
    receipt, fields = detail["receipt"], detail["immutable"]
    if fields.get("link_kind") == "folder":
        return drafts.ShareRefusal(
            "undo_unsupported",
            "A note published into an ordinary folder cannot be undone or deleted by Ciaobot; edit it in the folder instead.",
        )
    record = config.workspace(workspace) if hasattr(config, "workspace") else None
    link = next((item for item in getattr(record, "shared_brains", []) or [] if item.name == fields.get("brain")), None)
    if link is None or getattr(link, "lifetime_id", None) != fields.get("link_lifetime_id"):
        return drafts.ShareRefusal("brain_changed", "The original shared-brain link lifetime is no longer active.")
    path = Path(link.path)
    try:
        async with shared_brain._checkout_lock(link.path):
            async with shared_brain._checkout_claim(link.path, config):
                active = _active_link(config, workspace, fields)
                if active is None:
                    return drafts.ShareRefusal("brain_changed", "The original shared-brain link lifetime is no longer active.")
                def verify_and_draft() -> dict[str, str] | drafts.ShareRefusal:
                    destination = _verify_destination(config, path, active, fields)
                    if destination is None:
                        return drafts.ShareRefusal("brain_changed", "The original shared-brain destination changed.")
                    origin, ref = destination
                    fetched, _raw, _error = _fetch(path, origin, ref)
                    if fetched != "ok":
                        return drafts.ShareRefusal("upstream_unavailable", "The published shared note could not be verified.")
                    tracking = "refs/remotes/origin/" + ref.removeprefix("refs/heads/")
                    code, tip_raw, _ = _safe_git(path, "rev-parse", "--verify", tracking + "^{commit}", limit=256)
                    if code:
                        return drafts.ShareRefusal("upstream_unavailable", "The published shared note could not be verified.")
                    tip = _text(tip_raw)
                    state, oid, _content, _mode = _blob(path, tip, fields["path"])
                    expected_oid = receipt.get("published_blob_oid")
                    if expected_oid is None:
                        if state != "absent":
                            return drafts.ShareRefusal("changed_upstream", "The destination changed after publication; undo was refused.")
                    elif state != "blob" or oid != expected_oid:
                        return drafts.ShareRefusal("changed_upstream", "The destination changed after publication; undo was refused.")
                    prior = fields.get("original")
                    if fields["operation"] == "create":
                        inverse, proposed = "delete", None
                    elif fields["operation"] == "replace":
                        inverse, proposed = "replace", str(prior).encode("utf-8")
                    elif fields["operation"] == "delete":
                        inverse, proposed = "create", str(prior).encode("utf-8")
                    else:
                        return drafts.ShareRefusal("receipt_unavailable", "The shared-note receipt has an unsupported operation.")
                    drafted = drafts.draft_share(
                        config, workspace=workspace, brain=str(fields["brain"]),
                        relative_path=str(fields["path"]), operation=inverse,
                        proposed=proposed, request_key="undo:" + receipt_id,
                        checkout_claimed=True,
                    )
                    if isinstance(drafted, drafts.ShareRefusal):
                        return drafted
                    return {"workspace": workspace, "review_path": drafted.review_path}
                return await run_while_claimed(verify_and_draft)
    except (OSError, ValueError, drafts.ShareDraftError):
        return drafts.ShareRefusal("draft_unavailable", "The shared-note reversal could not be queued.")
