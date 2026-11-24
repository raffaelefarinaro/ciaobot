"""Bounded, review-bound publication of a shared-note proposal."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, TypeVar

from ciao import share_proposals as drafts, shared_brain
from ciao.config import SharedBrainLink
from ciao.git_proc import run_git_sync_bounded
from ciao.os_support.files import open_fd
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import is_private_descriptor_strict, make_private_dir
from ciao.redact import redact_text

OutcomeCode = Literal[
    "published", "already_published", "changed_upstream", "brain_changed",
    "push_refused", "indeterminate", "stale_review", "draft_corrupt",
    "brain_unavailable", "upstream_unavailable", "draft_unavailable", "reconcile_after_unlink",
]
MAX_ATTEMPTS = 3
_OID = re.compile(r"^[0-9a-f]{40,64}$")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    outcome: OutcomeCode
    commit_oid: str | None = None
    receipt_id: str | None = None
    message: str = ""


def _safe_git(folder: Path, *args: str, limit: int = 16_384, env: dict[str, str] | None = None) -> tuple[int, bytes, bytes]:
    child = shared_brain._git_env(folder)
    child["GIT_NO_REPLACE_OBJECTS"] = "1"
    child["GIT_NO_LAZY_FETCH"] = "1"
    if env:
        child.update(env)
    try:
        result = run_git_sync_bounded(
            folder, *shared_brain.EXACT_BYTES, *shared_brain._HARDEN, *args,
            timeout=shared_brain.FETCH_TIMEOUT_S, max_output=limit,
            env=child,
        )
    except OSError:
        return 127, b"", b"git could not be started"
    return result.returncode, result.stdout, result.stderr[:limit]


def _network_git(folder: Path, *args: str, limit: int = 4096) -> tuple[int, bytes, bytes]:
    """The only injectable seam for bounded remote I/O in isolated Git tests."""
    return _safe_git(folder, *args, limit=limit)


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
                return data
    return None


def has_publication_attempt(config: Any, *, workspace: str, proposal_id: str, expected_revision: str) -> bool:
    """Whether this exact reviewed artifact has a persisted attempt to reconcile."""
    artifact = _artifact_for(config, workspace, proposal_id)
    if artifact is None or artifact.get("review_token") != expected_revision:
        return False
    publication = artifact.get("publication")
    attempts = publication.get("attempts", []) if isinstance(publication, dict) else []
    return isinstance(attempts, list) and bool(attempts)


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
    record = config.workspace(workspace) if hasattr(config, "workspace") else None
    links = getattr(record, "shared_brains", []) or []
    if not isinstance(links, list):
        return None
    for candidate in links:
        if not isinstance(candidate, SharedBrainLink):
            continue
        if (candidate.name == fields.get("brain")
                and candidate.path == fields.get("checkout_path")
                and getattr(candidate, "lifetime_id", None) == fields.get("link_lifetime_id")):
            return candidate
    return None


def _receipt_id(fields: dict[str, Any], commit: str) -> str:
    value = "\0".join((fields["workspace"], fields["proposal_id"], str(fields["revision"]), fields["destination_fingerprint"], commit))
    return "shrpub_" + hashlib.sha256(value.encode()).hexdigest()


def _valid_receipt(fields: dict[str, Any], receipt: dict[str, Any]) -> bool:
    commit = receipt.get("commit_oid")
    required_matches = (
        isinstance(commit, str) and _OID.fullmatch(commit) is not None
        and receipt.get("id") == _receipt_id(fields, commit)
        and receipt.get("workspace") == fields.get("workspace")
        and receipt.get("proposal_id") == fields.get("proposal_id")
        and receipt.get("revision") == fields.get("revision")
        and receipt.get("destination_fingerprint") == fields.get("destination_fingerprint")
        and receipt.get("operation") == fields.get("operation")
    )
    if not required_matches:
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
    return enriched


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


@contextmanager
def _proposal_lock(config: Any, workspace: str, fields: dict[str, Any]) -> Iterator[None]:
    lock_root = drafts._store_dir(config) / "locks"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(lock_root)
    key = "\0".join((workspace, fields["proposal_id"], str(fields["revision"]), fields["destination_fingerprint"]))
    path = lock_root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    fd = open_fd(path, os.O_CREAT | os.O_RDWR, 0o600, follow_symlinks=False)
    try:
        if not is_private_descriptor_strict(fd):
            raise OSError("share publication lock is not private")
        lock_exclusive(fd)
        yield
    finally:
        try:
            unlock(fd)
        finally:
            os.close(fd)


@asynccontextmanager
async def _locked_proposal(config: Any, workspace: str, fields: dict[str, Any]):
    """Acquire the cross-process proposal lock without blocking the event loop."""
    loop = asyncio.get_running_loop()
    context = _proposal_lock(config, workspace, fields)
    acquire = loop.run_in_executor(None, context.__enter__)
    try:
        await asyncio.shield(acquire)
    except asyncio.CancelledError:
        await acquire
        await asyncio.shield(loop.run_in_executor(None, context.__exit__, None, None, None))
        raise
    try:
        yield
    finally:
        await asyncio.shield(loop.run_in_executor(None, context.__exit__, None, None, None))


async def _run_locked(function: Callable[..., _T], *args: Any) -> _T:
    """Keep locks held until a synchronous Git transaction finishes on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _verify_destination(
    config: Any, folder: Path, link: SharedBrainLink, fields: dict[str, Any], *,
    allow_legacy_lifetime: bool = False,
) -> tuple[str, str] | None:
    if fields.get("link_lifetime_id") != getattr(link, "lifetime_id", None) and not (
        allow_legacy_lifetime and "link_lifetime_id" not in fields
    ):
        return None
    state = shared_brain.brain_state(link, config)
    if state.get("problem") or state.get("identity") != fields.get("origin_identity"):
        return None
    code, out, _ = _safe_git(folder, "config", "--get-all", "remote.origin.url")
    configured = out.decode("utf-8", "strict").splitlines() if code == 0 else []
    if len(configured) != 1:
        return None
    raw_origin = configured[0].strip()
    try:
        if shared_brain.canonical_identity(raw_origin) != fields["origin_identity"]:
            return None
    except ValueError:
        return None
    effective: list[str] = []
    for args in (("remote", "get-url", "--all", "origin"), ("remote", "get-url", "--push", "--all", "origin")):
        result, data, _ = _safe_git(folder, *args)
        if result or not data:
            return None
        urls = data.decode("utf-8", "strict").splitlines()
        if len(urls) != 1:
            return None
        url = urls[0].strip()
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


async def publish_share(config: Any, *, workspace: str, proposal_id: str, expected_revision: str) -> PublishOutcome:
    """Publish the reviewed artifact once; reconcile every persisted attempt first."""
    artifact = _artifact_for(config, workspace, proposal_id)
    if artifact is None:
        return PublishOutcome("draft_corrupt", message="The queued shared-note draft could not be verified.")
    fields = artifact["immutable"]
    if expected_revision != artifact.get("review_token"):
        return PublishOutcome("stale_review", message="The shared-note review has changed.")
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
    if getattr(link, "lifetime_id", None) != fields.get("link_lifetime_id"):
        if publication.get("attempts"):
            return await reconcile_share(config, workspace=workspace, proposal_id=proposal_id, expected_revision=expected_revision)
        return PublishOutcome("brain_unavailable", message="The selected shared brain link lifetime has changed.")
    folder = Path(link.path)
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
                        result = await _run_locked(_publish_locked, config, workspace, Path(active.path), active, fields, artifact)
    except (OSError, ValueError, drafts.ShareDraftError):
        return PublishOutcome("draft_unavailable", message="Private publication state is unavailable.")
    if result.outcome == "reconcile_after_unlink":
        return await reconcile_share(config, workspace=workspace, proposal_id=proposal_id,
                                     expected_revision=expected_revision)
    if result.outcome == "published":
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
    link = SharedBrainLink(
        name=str(fields.get("brain", "")), path=path,
        identity=str(fields.get("origin_identity", "")),
        lifetime_id=str(fields.get("link_lifetime_id", "")),
    )
    checkout_lock = shared_brain._checkout_lock(path)
    try:
        async with checkout_lock:
            async with shared_brain._checkout_claim(path, config):
                async with _locked_proposal(config, workspace, fields):
                    result = await _run_locked(_reconcile_locked, config, workspace, Path(path), link, fields)
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
    state = shared_brain.brain_state(link, config)
    if state.get("problem"):
        return PublishOutcome("brain_changed", message="The shared brain is unavailable or has changed.")
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
        visible = drafts.load_share(config, workspace=workspace, proposal_id=fields["proposal_id"])
        if isinstance(visible, drafts.ShareRefusal) or visible.get("review_token") != artifact.get("review_token"):
            return PublishOutcome("draft_corrupt", message="The share proposal is no longer active in its queue.")
        fetched, _raw, fetch_error = _fetch(folder, origin, ref)
        if fetched != "ok":
            return PublishOutcome("upstream_unavailable", message="The shared brain upstream could not be read.")
        code, tip_raw, _ = _safe_git(folder, "rev-parse", "--verify", tracking + "^{commit}", limit=256)
        if code:
            return PublishOutcome("upstream_unavailable", message="The shared brain upstream could not be read.")
        tip = _text(tip_raw)
        base_state, base_oid, _base_content, _mode = _blob(folder, tip, fields["path"])
        expected_oid = fields.get("base_blob_oid")
        if (expected_oid is None and base_state != "absent") or (expected_oid is not None and (base_state != "blob" or base_oid != expected_oid)):
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
        try:
            _write_state(config, workspace, fields, {"attempts": attempts})
        except (OSError, ValueError, drafts.ShareDraftError):
            return PublishOutcome("draft_unavailable", message="The publication attempt could not be recorded safely.")
        code, push_stdout, stderr = _network_git(folder, "push", "--porcelain", origin, f"{commit}:{ref}", limit=4096)
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
        required_strings = (
            "workspace", "sidecar_id", "proposal_id", "destination_fingerprint",
            "operation", "brain", "origin_identity", "path", "upstream_ref",
        )
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
                    )
                    if isinstance(drafted, drafts.ShareRefusal):
                        return drafted
                    return {"workspace": workspace, "review_path": drafted.review_path}
                return await asyncio.to_thread(verify_and_draft)
    except (OSError, ValueError, drafts.ShareDraftError):
        return drafts.ShareRefusal("draft_unavailable", "The shared-note reversal could not be queued.")
