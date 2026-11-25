"""Private, immutable drafts for publishing one note to a linked shared brain.

The queue contains only a fixed summary and a random handle. Exact content and
destination details stay in owner-only runtime state until a human reviews them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, NoReturn
from urllib.parse import urlsplit, urlunsplit

from ciao import memory_receipts, proposal_tracking, shared_brain
from ciao.config import SharedBrainLink
from ciao.git_proc import run_git_sync_bounded
from ciao.os_support import folder_io
from ciao.os_support.files import open_fd
from ciao.os_support.private import is_private_descriptor_strict, mkstemp_private
from ciao.proposal_tracking import proposal_review_path
from ciao.redact import redact_text

KIND = "share"
MAX_BYTES = 65_536
MAX_PER_BRAIN = 10
MAX_PER_WORKSPACE = 30
MAX_CANDIDATES_PER_RUN = 20
MAX_SIDECAR_BYTES = 2 * MAX_BYTES * 6 + 16_384
_SCHEMA = 1
_QUEUE = PurePosixPath("Workspace/Memory-Proposals.md")
_SAFE_COMPONENT = re.compile(r"^[^<>:\"|?*]+$")
_WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})
_QUEUE_LOCKS: dict[str, threading.RLock] = {}
_QUEUE_LOCKS_GUARD = threading.Lock()


def _thread_queue_lock(queue: Path) -> threading.RLock:
    key = str(queue.resolve())
    with _QUEUE_LOCKS_GUARD:
        return _QUEUE_LOCKS.setdefault(key, threading.RLock())


@dataclass(frozen=True, slots=True)
class ShareRefusal:
    code: str
    message: str
    eligible_brains: tuple[str, ...] = ()
    limit: int | None = None
    count: int | None = None


@dataclass(frozen=True, slots=True)
class ShareDraft:
    proposal_id: str
    artifact: dict[str, Any]
    review_path: str


class ShareDraftError(ValueError):
    def __init__(self, refusal: ShareRefusal):
        self.refusal = refusal
        super().__init__(refusal.message)


def _refuse(code: str, message: str, **context: Any) -> NoReturn:
    raise ShareDraftError(ShareRefusal(code, message, **context))


def _store_dir(config: Any) -> Path:
    return Path(config.state_path).parent / "secrets" / "share-proposals"


def _storage_safe(config: Any, workspace: str) -> Path:
    root = _store_dir(config).resolve()
    protected = [Path(config.workspace_vault_root(workspace)).resolve()]
    protected.extend(Path(config.workspace_vault_root(name)).resolve() for name in config.workspace_names())
    record = config.workspace(workspace) if hasattr(config, "workspace") else None
    protected.extend(Path(link.path).resolve() for link in (getattr(record, "shared_brains", []) or []))
    for name in config.workspace_names():
        linked = config.workspace(name) if hasattr(config, "workspace") else None
        protected.extend(Path(link.path).resolve() for link in (getattr(linked, "shared_brains", []) or []))
    export_root = getattr(config, "workspace_export_root", None)
    if export_root:
        protected.append(Path(export_root).resolve())
    from ciao.workspace_archive import archive_root
    protected.append(archive_root(config).resolve())
    if any(root == item or root.is_relative_to(item) or item.is_relative_to(root) for item in protected):
        _refuse("draft_unavailable", "Private share storage overlaps workspace data.")
    try:
        base_fd = folder_io.open_directory_nofollow(str(root.parent.parent))
        try:
            for name in ("secrets", "share-proposals"):
                child_fd, _identity = folder_io.open_or_create_private_dir(base_fd, name)
                os.close(base_fd)
                base_fd = child_fd
        finally:
            os.close(base_fd)
    except OSError:
        _refuse("draft_unavailable", "Private share storage is unavailable.")
    return root


def _write_private(path: Path, data: bytes) -> None:
    if len(data) > MAX_SIDECAR_BYTES:
        _refuse("too_large", "The private share draft exceeds its storage limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = mkstemp_private(dir=path.parent, prefix=".share-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _safe_path(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        _refuse("invalid_path", "The note path must be a repository-relative Markdown path.")
    parts = tuple(value.split("/"))
    if any(not part or part in {".", ".."} or part.startswith(".") or not _SAFE_COMPONENT.fullmatch(part) or part.endswith((".", " ")) or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED for part in parts):
        _refuse("invalid_path", "The note path contains an unsafe component.")
    if not parts[-1].lower().endswith(".md"):
        _refuse("invalid_path", "Shared notes must use a .md path.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        _refuse("invalid_path", "The note path contains a control character.")
    return parts


def _origin_fingerprint(origin: str) -> str:
    if "://" in origin:
        parsed = urlsplit(origin)
        host = (parsed.hostname or "").lower()
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme.lower(), host + port, parsed.path.rstrip("/"), "", ""))
    user_host, sep, path = origin.rpartition(":")
    if not sep:
        return ""
    host = user_host.rsplit("@", 1)[-1].lower()
    return f"{host}:{path.rstrip('/')}"


def _git_bytes(folder: Path, *args: str, limit: int, overflow_code: str = "") -> bytes:
    env = shared_brain._git_env(folder)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    try:
        result = run_git_sync_bounded(
            folder,
            *shared_brain.EXACT_BYTES,
            *shared_brain._HARDEN,
            *args,
            timeout=shared_brain.GIT_TIMEOUT_S,
            max_output=limit + 1024,
            env=env,
        )
    except OSError:
        _refuse("upstream_unavailable", "The shared brain upstream could not be read.")
    if overflow_code and (result.output_limited or (result.returncode == 0 and len(result.stdout) > limit)):
        _refuse(overflow_code, "The existing note exceeds the 65,536-byte limit.")
    if result.returncode != 0 or result.truncated or len(result.stdout) > limit:
        _refuse("upstream_unavailable", "The shared brain upstream could not be read.")
    return result.stdout


def folder_destination_fingerprint(link: SharedBrainLink) -> str:
    """A stable identity for one folder link lifetime and exact root path.

    ``st_dev``/``st_ino`` and the directory chain are checked separately at
    draft and publish time; this value binds the approval to the link record
    the user actually reviewed, so a relink (even to the same path) and a path
    move cannot reuse it.
    """
    payload = json.dumps(
        ["folder", link.lifetime_id, link.identity, link.path],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return "folder:" + hashlib.sha256(payload).hexdigest()


def _folder_chain_rows(chain: list[folder_io.DirectoryIdentity]) -> list[dict[str, int]]:
    return [{"dev": item.dev, "ino": item.ino} for item in chain]


def _qualify_folder(
    config: Any, link: SharedBrainLink, relative_path: str, operation: str | None,
) -> tuple[str, None, None, None, str, list[dict[str, int]], None]:
    """Qualify a new note in an ordinary folder; existing notes are deferred to #3626."""
    if operation not in (None, "create"):
        _refuse(
            "folder_update_unsupported",
            "Updating or deleting an existing note in an ordinary folder is not supported yet.",
        )
    parts = _safe_path(relative_path)
    state = shared_brain._brain_state_owned(link, config)
    if state.get("problem"):
        _refuse("brain_unavailable", "The selected shared folder is unavailable or has changed.")
    try:
        parent_fd, chain = folder_io.open_chain(link.path, parts)
    except FileNotFoundError:
        _refuse("invalid_path", "The note's parent folder does not exist in the shared folder.")
    except folder_io.FolderChanged:
        _refuse("unsafe_target", "The shared folder or one of its path components changed.")
    except OSError:
        _refuse("brain_unavailable", "The selected shared folder could not be read safely.")
    try:
        entry = folder_io.stat_entry(parent_fd, parts[-1])
    finally:
        os.close(parent_fd)
    if entry is not None:
        if stat.S_ISLNK(entry.st_mode):
            _refuse("unsafe_target", "The note path is a symbolic link.")
        if not stat.S_ISREG(entry.st_mode):
            _refuse("unsafe_target", "The note path is not a regular file.")
        _refuse(
            "folder_update_unsupported",
            "Updating or deleting an existing note in an ordinary folder is not supported yet.",
        )
    return "", None, None, None, folder_destination_fingerprint(link), _folder_chain_rows(chain), None


def _qualify_git(
    config: Any, link: SharedBrainLink, relative_path: str, owner: Any,
) -> tuple[str, str, bytes | None, str | None, str, None, str | None]:
    parts = _safe_path(relative_path)
    observation = shared_brain.observe_under_fence(config, link, owner)
    state = observation.state
    if state.get("problem"):
        code = "brain_changed" if state.get("problem") == "origin_changed" else "brain_unavailable"
        _refuse(code, "The selected shared brain is unavailable or has changed.")
    if len(observation.configured_urls) != 1:
        _refuse("brain_changed", "The shared brain destination is ambiguous.")
    destination_fingerprint = _origin_fingerprint(observation.configured_urls[0])
    if not destination_fingerprint or observation.origin_identity != link.identity:
        _refuse("brain_changed", "The shared brain destination changed.")
    folder = Path(link.path)
    branch = str(state.get("branch") or "")
    if not branch:
        _refuse("upstream_unavailable", "The shared brain has no checked-out branch.")
    git_code, upstream = shared_brain._git(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if git_code or not upstream.strip().startswith("origin/"):
        _refuse("upstream_unavailable", "The current branch must track origin.")
    full_ref = "refs/heads/" + upstream.strip()[len("origin/"):]
    if not full_ref.startswith("refs/heads/") or full_ref == "refs/heads/":
        _refuse("upstream_unavailable", "The tracked upstream branch is invalid.")
    tracking_ref = "refs/remotes/" + upstream.strip()
    commit = _git_bytes(folder, "rev-parse", "--verify", tracking_ref + "^{commit}", limit=128).decode("ascii", "strict").strip()
    original: bytes | None = None
    original_oid: str | None = None
    original_mode: str | None = None
    for index in range(len(parts)):
        prefix = "/".join(parts[:index + 1])
        listing = _git_bytes(folder, "ls-tree", "-z", commit, "--", ":(literal)" + prefix, limit=4096)
        row = listing.rstrip(b"\0")
        if not row:
            break
        meta, sep, raw_path = row.partition(b"\t")
        if not sep or raw_path.decode("utf-8", "strict") != prefix:
            break
        mode, _, kind_oid = meta.decode("ascii").partition(" ")
        kind, _, oid = kind_oid.partition(" ")
        if index < len(parts) - 1:
            if mode != "040000" or kind != "tree":
                _refuse("unsafe_target", "An upstream path ancestor is not a regular directory.")
        else:
            if mode not in {"100644", "100755"} or kind != "blob":
                _refuse("unsafe_target", "The upstream target is not a regular file.")
            original = _git_bytes(
                folder, "cat-file", "blob", oid,
                limit=MAX_BYTES + 1, overflow_code="too_large",
            )
            original_oid = oid
            original_mode = mode
    if original is not None and len(original) > MAX_BYTES:
        _refuse("too_large", "The existing note exceeds the 65,536-byte limit.")
    try:
        if original is not None:
            original.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _refuse("invalid_utf8", "The existing note is not valid UTF-8.")
    return full_ref, commit, original, original_oid, destination_fingerprint, None, original_mode


def _qualify(
    config: Any, link: SharedBrainLink, relative_path: str, owner: Any,
    operation: str | None = None,
) -> tuple[str, str | None, bytes | None, str | None, str, list[dict[str, int]] | None, str | None]:
    if link.kind == "folder":
        return _qualify_folder(config, link, relative_path, operation)
    return _qualify_git(config, link, relative_path, owner)


def _brain_for(config: Any, workspace: str, name: str) -> SharedBrainLink:
    record = config.workspace(workspace) if hasattr(config, "workspace") else None
    links: list[SharedBrainLink] = list(getattr(record, "shared_brains", []) or [])
    eligible = tuple(sorted(link.name for link in links))
    if name:
        for link in links:
            if link.name == name:
                return link
        _refuse("brain_unavailable", "The selected shared brain is not linked to this workspace.")
    if len(links) == 1:
        return links[0]
    if not links:
        _refuse("brain_unavailable", "This workspace has no linked shared brain.")
    _refuse("brain_required", "Choose one linked shared brain.", eligible_brains=eligible)


def _persist_link_lifetime(config: Any, workspace: str, link: SharedBrainLink, *, owner: Any = None) -> None:
    """Persist generated identities for registries written before lifetimes existed."""
    from ciao.workspaces import (
        WorkspaceRegistryConflict,
        patch_shared_brain_lifetime,
        workspace_registry_claim,
        workspace_registry_lock,
    )

    with workspace_registry_lock(config):
        record = config.workspace(workspace) if hasattr(config, "workspace") else None
        current = next((item for item in getattr(record, "shared_brains", []) or []
                        if item.name == link.name and item.path == link.path), None)
        if current is None or current.lifetime_id != link.lifetime_id:
            _refuse("brain_unavailable", "The selected shared brain link changed while drafting.")
        try:
            if owner is None:
                with workspace_registry_claim(config) as claim:
                    lifetime_id = patch_shared_brain_lifetime(config, workspace, link, owner=claim)
            else:
                lifetime_id = patch_shared_brain_lifetime(config, workspace, link, owner=owner)
        except (OSError, ValueError, WorkspaceRegistryConflict):
            _refuse("brain_unavailable", "The shared brain registry changed while drafting.")
        if lifetime_id != link.lifetime_id:
            _refuse("brain_unavailable", "The selected shared brain link changed while drafting.")


def _review_token(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "shr_" + hashlib.sha256(payload).hexdigest()


def _render_evidence(canonical: str, digest: str) -> bytes:
    """Render only verified, typed evidence as inert, deterministic Markdown data."""
    from ciao import share_evidence

    if not isinstance(canonical, str) or not isinstance(digest, str):
        _refuse("invalid_evidence", "The evidence is invalid.")
    try:
        raw = canonical.encode("utf-8")
    except UnicodeEncodeError:
        _refuse("invalid_evidence", "The evidence is invalid.")
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        _refuse("invalid_evidence", "The evidence is invalid.")
    try:
        fields = json.loads(canonical)
    except (ValueError, RecursionError):
        _refuse("invalid_evidence", "The evidence is invalid.")
    if (
        not isinstance(fields, dict) or not fields
        or any(key not in share_evidence.FIELDS or not isinstance(value, str)
               for key, value in fields.items())
    ):
        _refuse("invalid_evidence", "The evidence is invalid.")
    if any(re.search(r"(?:https?|file)://", value, re.IGNORECASE) for value in fields.values()):
        _refuse("disclosure_prohibited", "The evidence contains a link that cannot be shared automatically.")
    try:
        if share_evidence._canonical(fields) != raw:
            _refuse("invalid_evidence", "The evidence is invalid.")
    except UnicodeEncodeError:
        _refuse("invalid_evidence", "The evidence is invalid.")
    text = json.dumps(fields, ensure_ascii=True, sort_keys=True, indent=2)
    for char in "`<>&":
        text = text.replace(char, f"\\u{ord(char):04x}")
    result = ("```json\n" + text + "\n```\n").encode("utf-8")
    if len(result) > MAX_BYTES:
        _refuse("too_large", "The proposed note exceeds the 65,536-byte limit.")
    return result


def _compose_candidate(base: bytes | None, rendered: bytes, destination: Any) -> bytes:
    if destination.kind == "collection":
        return rendered
    if destination.kind != "section" or destination.operation != "append" or base is None:
        _refuse("invalid_operation", "This mapped destination cannot be composed safely.")
    try:
        text = base.decode("utf-8", "strict")
        fact = rendered.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _refuse("invalid_utf8", "The note content is not valid UTF-8.")
    lines = text.splitlines(keepends=True)
    # Fenced code could contain headings; refuse ambiguity rather than editing it.
    if any(re.match(r"^\s{0,3}(`{3,}|~{3,})", line) for line in lines):
        _refuse("ambiguous_destination", "The mapped section cannot be located safely.")
    headings = [(i, match) for i, line in enumerate(lines)
                if (match := re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*\r?\n?$", line))]
    targets = [(i, match) for i, match in headings if match.group(2) == destination.section]
    if len(targets) != 1:
        _refuse("ambiguous_destination", "The mapped section cannot be located safely.")
    start, match = targets[0]
    end = next((i for i, item in headings if i > start and len(item.group(1)) <= len(match.group(1))), len(lines))
    before = "".join(lines[:end])
    after = "".join(lines[end:])
    if before and not before.endswith("\n"):
        before += "\n"
    if before and not before.endswith("\n\n") and not before.endswith("\r\n\r\n"):
        before += "\n"
    return (before + fact.rstrip("\n") + "\n\n" + after).encode("utf-8")


def _load_sidecar(path: Path) -> dict[str, Any]:
    try:
        fd = open_fd(path, os.O_RDONLY, follow_symlinks=False)
        try:
            if not is_private_descriptor_strict(fd):
                raise ValueError("share artifact is not private")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_SIDECAR_BYTES + 1)
                if len(raw) > MAX_SIDECAR_BYTES:
                    raise ValueError("share artifact exceeds its limit")
        finally:
            os.close(fd)
        data = json.loads(raw.decode("utf-8", "strict"))
        if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
            raise ValueError
        fields = data.get("immutable")
        if not isinstance(fields, dict) or fields.get("schema") != _SCHEMA or data.get("review_token") != _review_token(fields):
            raise ValueError
        return data
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        _refuse("draft_corrupt", "The private share draft is missing or corrupt.")


def share_dismissal_block(
    config: Any, *, workspace: str, bullet: Any, proposal_id: str = ""
) -> str | None:
    """Return a reason a share row cannot be dismissed, reading canonical state only.

    Callers must hold the proposal queue lock while checking and removing the row;
    this sidecar-only read deliberately does not acquire either queue lock.
    """
    if getattr(bullet, "kind", None) != KIND:
        return None
    sidecar_id = getattr(bullet, "target", "")
    if not isinstance(sidecar_id, str) or not sidecar_id:
        return "The shared-note publication state is unavailable."
    path = _store_dir(config) / (
        hashlib.sha256((workspace + "\0sidecar\0" + sidecar_id).encode()).hexdigest() + ".json"
    )
    try:
        data = _load_sidecar(path)
    except ShareDraftError:
        return "The shared-note publication state is unavailable."
    fields = data.get("immutable")
    expected_proposal_id = proposal_tracking.stable_proposal_id(
        workspace, (PurePosixPath(workspace) / _QUEUE).as_posix(), KIND,
        getattr(bullet, "text", ""), "", 0,
    )
    if (
        not isinstance(fields, dict)
        or fields.get("workspace") != workspace
        or fields.get("sidecar_id") != sidecar_id
        or fields.get("proposal_id") != expected_proposal_id
        or (proposal_id and fields.get("proposal_id") != proposal_id)
        or getattr(bullet, "text", "") != f"Shared note change proposal {sidecar_id}"
    ):
        return "The shared-note publication state does not match this proposal."
    if "publication" not in data:
        return None
    publication = data.get("publication")
    if not isinstance(publication, dict):
        return "The shared-note publication state is indeterminate."
    if "attempts" not in publication or "receipts" not in publication:
        return "The shared-note publication state is indeterminate."
    attempts = publication.get("attempts", [])
    receipts = publication.get("receipts", [])
    if not isinstance(attempts, list) or not isinstance(receipts, list):
        return "The shared-note publication state is indeterminate."
    if receipts:
        return "A shared-note publication is already confirmed."
    for attempt in attempts:
        if not isinstance(attempt, dict):
            return "A shared-note publication attempt needs reconciliation."
        common = (
            type(attempt.get("attempt")) is int
            and attempt.get("attempt", 0) >= 1
            and attempt.get("proposal_id") == fields.get("proposal_id")
            and attempt.get("revision") == fields.get("revision")
            and attempt.get("destination_fingerprint") == fields.get("destination_fingerprint")
        )
        if attempt.get("kind") == "folder":
            # A rejected folder attempt wrote nothing (either the destination
            # already existed, or the no-clobber publish failed before the
            # link). It is safe to dismiss, unlike an unresolved attempt.
            if (
                not common
                or attempt.get("status") != "rejected"
                or attempt.get("retryable") not in (True, False)
                or attempt.get("path") != fields.get("path")
                or not isinstance(attempt.get("token"), str)
                or not attempt.get("token")
                or attempt.get("staging_name") != f".ciao-share-{attempt.get('token')}.d"
                or not isinstance(attempt.get("staging_root"), str)
                or not os.path.isabs(str(attempt.get("staging_root")))
            ):
                return "A shared-note publication attempt needs reconciliation."
            continue
        if (
            not common
            or attempt.get("status") != "rejected"
            or attempt.get("retryable") is not False
            or not isinstance(attempt.get("commit_oid"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", attempt.get("commit_oid", ""))
            or not isinstance(attempt.get("parent_oid"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", attempt.get("parent_oid", ""))
            or attempt.get("ref") != fields.get("upstream_ref")
        ):
            return "A shared-note publication attempt needs reconciliation."
    return None


def _qualify_owned(
    config: Any, workspace: str, link: SharedBrainLink, relative_path: str,
    operation: str | None, owner: Any,
) -> tuple[str, str | None, bytes | None, str | None, str, list[dict[str, int]] | None, str | None]:
    from ciao.workspaces import (
        ensure_registry_current,
        persisted_checkout_links,
        persisted_shared_links,
        WorkspaceRegistryConflict,
    )

    ensure_registry_current(config)
    if owner.current is None:
        raise WorkspaceRegistryConflict("workspace registry is missing")
    if link.kind == "folder":
        if not any(
            name == workspace
            and row.get("name") == link.name
            and row.get("path") == link.path
            and row.get("identity") == link.identity
            and row.get("kind") == "folder"
            and row.get("lifetime_id") == link.lifetime_id
            for name, row in persisted_shared_links(owner.current, link.path)
        ):
            raise WorkspaceRegistryConflict("the shared folder link changed")
    else:
        persisted_checkout_links(owner.current, link.path)
        _persist_link_lifetime(config, workspace, link, owner=owner)
    return _qualify(config, link, relative_path, owner, operation)


def _draft_share(
    config: Any, *, workspace: str, brain: str = "", relative_path: str,
    operation: str | None, proposed: bytes | None, request_key: str,
    request_fingerprint: str, retiring_proposal_id: str | None = None,
    publish_guard: Callable[[], AbstractContextManager[object]] | None = None,
    checkout_claimed: bool = False,
    registry_owner: Any = None,
    expected_qualification: tuple[Any, ...] | None = None,
    candidate_binding: dict[str, Any] | None = None,
) -> ShareDraft:
    """Qualify and privately file one immutable shared-note proposal."""
    if operation not in (None, "create", "replace", "delete"):
        _refuse("invalid_operation", "Use create, replace, or delete.")
    if not isinstance(request_key, str) or not request_key:
        _refuse("request_conflict", "A stable internal request key is required.")
    if candidate_binding is not None and (
        registry_owner is None or not checkout_claimed or expected_qualification is None
        or not isinstance(candidate_binding.get("authorization"), dict)
    ):
        _refuse("invalid_evidence", "The candidate requires owned destination and evidence claims.")
    link = _brain_for(config, workspace, brain)
    from ciao.workspaces import workspace_registry_claim, WorkspaceRegistryBusy, WorkspaceRegistryConflict

    claim = (
        nullcontext()
        if checkout_claimed or link.kind != "git"
        else shared_brain.checkout_claims_nowait([link.path], config)
    )
    try:
        registry = nullcontext(registry_owner) if registry_owner is not None else workspace_registry_claim(config)
        with claim, registry as owner:
            qualified = _qualify_owned(config, workspace, link, relative_path, operation, owner)
            if expected_qualification is not None and qualified != expected_qualification:
                _refuse("changed_upstream", "The reviewed note changed while composing; draft it again.")
            (
                op_ref,
                base_commit,
                original,
                original_oid,
                destination_fingerprint,
                folder_chain,
                original_mode,
            ) = qualified
    except (WorkspaceRegistryBusy, WorkspaceRegistryConflict):
        _refuse("brain_unavailable", "The shared brain changed while drafting.")
    operation = operation or ("replace" if original is not None else "create")
    if operation == "create" and original is not None or operation in ("replace", "delete") and original is None:
        _refuse("invalid_operation", "The requested operation does not match the upstream note.")
    if operation == "delete":
        if proposed is not None:
            _refuse("invalid_operation", "A deletion has no proposed content.")
    elif not isinstance(proposed, bytes):
        _refuse("invalid_operation", "Create and replace require exact bytes.")
    if proposed is not None and len(proposed) > MAX_BYTES:
        _refuse("too_large", "The proposed note exceeds the 65,536-byte limit.")
    try:
        original_text = original.decode("utf-8", "strict") if original is not None else None
        proposed_text = proposed.decode("utf-8", "strict") if proposed is not None else None
    except UnicodeDecodeError:
        _refuse("invalid_utf8", "The note content is not valid UTF-8.")
    home = str(Path.home())
    for content in (original_text, proposed_text):
        if content is not None:
            if redact_text(content) != content:
                _refuse("secret_detected", "The note contains credential-shaped text.")
            if home in content or str(Path(config.workspace_root)) in content:
                _refuse("private_path_mentioned", "The note mentions a private local path.")
    store = _storage_safe(config, workspace)
    key_hash = hashlib.sha256((workspace + "\0" + request_key).encode()).hexdigest()
    index_path = store / (key_hash + ".json")
    if index_path.exists():
        previous = _load_sidecar(index_path)
        if previous.get("request_fingerprint") != request_fingerprint:
            _refuse("request_conflict", "This request key was already used for different content.")
        fields = previous["immutable"]
        id_path = store / (hashlib.sha256((workspace + "\0sidecar\0" + fields["sidecar_id"]).encode()).hexdigest() + ".json")
        vault = Path(config.workspace_vault_root(workspace))
        queue = vault / _QUEUE
        rel_queue = (PurePosixPath(workspace) / _QUEUE).as_posix()
        with _thread_queue_lock(queue), memory_receipts.queue_lock(queue):
            queue_text = queue.read_text(encoding="utf-8") if queue.exists() else "# Memory Proposals\n"
            rows = list(proposal_tracking.walk_proposal_queue(workspace, rel_queue, queue_text))
            visible = any(row.proposal_id == fields["proposal_id"] for row in rows)
            if id_path.exists():
                canonical = _load_sidecar(id_path)
                canonical_fields = canonical["immutable"]
                if (
                    canonical_fields.get("workspace") != workspace
                    or canonical_fields.get("sidecar_id") != fields["sidecar_id"]
                    or canonical_fields.get("proposal_id") != fields["proposal_id"]
                ):
                    _refuse("draft_corrupt", "The canonical share artifact does not match its request index.")
            else:
                _refuse(
                    "draft_corrupt",
                    "The canonical share artifact is missing; request replay cannot safely recover it.",
                )
            canonical_fields = canonical["immutable"]
            if visible:
                return ShareDraft(fields["proposal_id"], canonical, proposal_review_path(workspace, canonical_fields["review_key"]))
            from ciao.memory_proposals import _dismissed_texts
            summary = f"Shared note change proposal {fields['sidecar_id']}"
            if summary in _dismissed_texts(queue):
                _refuse("request_conflict", "This share proposal has already been settled.")
            publication = canonical.get("publication", {})
            if not isinstance(publication, dict):
                _refuse("draft_corrupt", "The canonical share publication state is corrupt.")
            attempts = publication.get("attempts", [])
            receipts = publication.get("receipts", [])
            if not isinstance(attempts, list) or not isinstance(receipts, list):
                _refuse("draft_corrupt", "The canonical share publication state is corrupt.")
            if attempts or receipts:
                _refuse("request_conflict", "This share proposal already has a publication attempt or receipt.")
            count = sum(
                row.bullet.kind == KIND and row.proposal_id != retiring_proposal_id
                for row in rows
            )
            if count >= MAX_PER_WORKSPACE:
                _refuse("capacity", "The workspace share proposal limit is reached.", limit=MAX_PER_WORKSPACE, count=count)
            brain_count = 0
            for row in rows:
                if row.bullet.kind != KIND or row.proposal_id == retiring_proposal_id:
                    continue
                prior_path = store / (hashlib.sha256((workspace + "\0sidecar\0" + row.bullet.target).encode()).hexdigest() + ".json")
                try:
                    prior = _load_sidecar(prior_path)
                except ShareDraftError:
                    _refuse("draft_corrupt", "A queued share proposal has a missing or corrupt private artifact.")
                if prior["immutable"].get("brain") == fields["brain"]:
                    brain_count += 1
            if brain_count >= MAX_PER_BRAIN:
                _refuse("capacity", "The shared brain share proposal limit is reached.", limit=MAX_PER_BRAIN, count=brain_count)
            memory_receipts.write_queue_atomically(
                queue,
                queue_text.rstrip() + f"\n\n- [share {fields['sidecar_id']}] {summary}\n",
                publish_guard=publish_guard,
            )
        return ShareDraft(
            fields["proposal_id"], canonical,
            proposal_review_path(workspace, canonical_fields["review_key"]),
        )
    sidecar_id = secrets.token_urlsafe(18)
    text = f"Shared note change proposal {sidecar_id}"
    rel_queue = (PurePosixPath(workspace) / _QUEUE).as_posix()
    proposal_id = proposal_tracking.stable_proposal_id(workspace, rel_queue, KIND, text, "", 0)
    token_fields: dict[str, Any] = {
        "schema": _SCHEMA,
        "workspace": workspace, "proposal_id": proposal_id, "revision": 1,
        "link_lifetime_id": link.lifetime_id, "link_kind": link.kind,
        "sidecar_id": sidecar_id, "brain": link.name, "origin_identity": link.identity,
        "checkout_path": str(Path(link.path).resolve()),
        "destination_fingerprint": destination_fingerprint,
        "upstream_ref": op_ref, "path": relative_path, "operation": operation,
        "base_blob_oid": original_oid,
        "base_mode": original_mode,
        "original": original_text, "proposed": proposed_text,
        "proposed_sha256": hashlib.sha256(proposed).hexdigest() if proposed is not None else None,
        "base_commit": base_commit,
    }
    if link.kind == "folder":
        token_fields["folder_chain"] = folder_chain
    if candidate_binding is not None:
        token_fields["evidence_authorization"] = candidate_binding["authorization"]
        token_fields["destination_mapping"] = {
            key: value for key, value in candidate_binding.items() if key != "authorization"
        }
    token_fields["review_key"] = proposal_tracking.proposal_review_key(workspace, KIND, text, "", sidecar_id, "")
    token = _review_token(token_fields)
    artifact = {"schema": _SCHEMA, "immutable": token_fields, "review_token": token, "request_fingerprint": request_fingerprint, "request_key_hash": key_hash}
    vault = Path(config.workspace_vault_root(workspace))
    queue = vault / _QUEUE
    with memory_receipts.queue_lock(queue):
        try:
            existing_text = queue.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing_text = "# Memory Proposals\n"
        queued = list(proposal_tracking.walk_proposal_queue(workspace, rel_queue, existing_text))
        count = sum(
            1 for entry in queued
            if entry.bullet.kind == KIND and entry.proposal_id != retiring_proposal_id
        )
        if count >= MAX_PER_WORKSPACE:
            _refuse("capacity", "The workspace share proposal limit is reached.", limit=MAX_PER_WORKSPACE, count=count)
        brain_count = 0
        for entry in proposal_tracking.walk_proposal_queue(workspace, rel_queue, existing_text):
            if entry.bullet.kind != KIND or entry.proposal_id == retiring_proposal_id:
                continue
            prior_path = store / (hashlib.sha256((workspace + "\0sidecar\0" + entry.bullet.target).encode()).hexdigest() + ".json")
            try:
                prior = _load_sidecar(prior_path)
            except ShareDraftError:
                _refuse("draft_corrupt", "A queued share proposal has a missing or corrupt private artifact.")
            if prior["immutable"].get("brain") == link.name:
                brain_count += 1
        if brain_count >= MAX_PER_BRAIN:
            _refuse("capacity", "The shared brain share proposal limit is reached.", limit=MAX_PER_BRAIN, count=brain_count)
        # The ID payload is visible; no path, content or content hash is.
        bullet = f"- [share {sidecar_id}] {text}"
        id_path = store / (hashlib.sha256((workspace + "\0sidecar\0" + sidecar_id).encode()).hexdigest() + ".json")
        _write_private(id_path, json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        _write_private(index_path, json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        try:
            memory_receipts.write_queue_atomically(
                queue, existing_text.rstrip() + "\n\n" + bullet + "\n",
                publish_guard=publish_guard,
            )
        except ShareDraftError:
            index_path.unlink(missing_ok=True)
            id_path.unlink(missing_ok=True)
            raise
    return ShareDraft(proposal_id, artifact, proposal_tracking.proposal_review_path(workspace, token_fields["review_key"]))


def draft_share(
    config: Any, *, workspace: str, brain: str = "", relative_path: str,
    operation: str | None, proposed: bytes | None, request_key: str,
    retiring_proposal_id: str | None = None,
    publish_guard: Callable[[], AbstractContextManager[object]] | None = None,
    checkout_claimed: bool = False,
) -> ShareDraft | ShareRefusal:
    """Return a draft or its safe, typed refusal without exposing input data."""
    try:
        if not isinstance(request_key, str) or not request_key:
            _refuse("request_conflict", "A stable internal request key is required.")
        if operation not in (None, "create", "replace", "delete"):
            _refuse("invalid_operation", "Use create, replace, or delete.")
        if proposed is not None and not isinstance(proposed, bytes):
            _refuse("invalid_operation", "Create and replace require exact bytes.")
        if operation == "delete" and proposed is not None or operation != "delete" and proposed is None:
            _refuse("invalid_operation", "Delete requires no content; create and replace require exact bytes.")
        request_fingerprint = _request_fingerprint(brain, relative_path, operation, proposed)
        store = _storage_safe(config, workspace)
        queue = Path(config.workspace_vault_root(workspace)) / _QUEUE
        key_hash = hashlib.sha256((workspace + "\0" + request_key).encode()).hexdigest()
        index_path = store / (key_hash + ".json")
        rel_queue = (PurePosixPath(workspace) / _QUEUE).as_posix()
        # This lock covers idempotency lookup, recovery, capacity, persistence,
        # and append. A visible request is returned before touching Git again.
        with _thread_queue_lock(queue), memory_receipts.queue_lock(queue):
            if index_path.exists():
                previous = _load_sidecar(index_path)
                fields = previous["immutable"]
                if previous.get("request_fingerprint") != request_fingerprint:
                    _refuse("request_conflict", "This request key was already used for different content.")
                from ciao.memory_proposals import _dismissed_texts
                summary = f"Shared note change proposal {fields['sidecar_id']}"
                if summary in _dismissed_texts(queue):
                    _refuse("request_conflict", "This share proposal has already been settled.")
                visible = load_share(config, workspace=workspace, proposal_id=str(fields["proposal_id"]))
                if isinstance(visible, dict):
                    return ShareDraft(fields["proposal_id"], visible, proposal_review_path(workspace, fields["review_key"]))
                queue_text = queue.read_text(encoding="utf-8") if queue.exists() else ""
                has_row = any(
                    row.proposal_id == fields["proposal_id"]
                    for row in proposal_tracking.walk_proposal_queue(workspace, rel_queue, queue_text)
                )
                if has_row:
                    _refuse("draft_corrupt", "The visible share proposal does not match its private artifact.")
            return _draft_share(
                config,
                workspace=workspace,
                brain=brain,
                relative_path=relative_path,
                operation=operation,
                proposed=proposed,
                request_key=request_key,
                request_fingerprint=request_fingerprint,
                retiring_proposal_id=retiring_proposal_id,
                publish_guard=publish_guard,
                checkout_claimed=checkout_claimed,
            )
    except ShareDraftError as exc:
        return exc.refusal
    except memory_receipts.QueueLockError:
        return ShareRefusal("draft_unavailable", "The proposal queue is currently unavailable.")
    except OSError:
        return ShareRefusal("draft_unavailable", "Private share storage or the proposal queue is unavailable.")


def _request_fingerprint(
    brain: str, relative_path: str,
    operation: str | None, proposed: bytes | None,
) -> str:
    proposed_identity = None if proposed is None else hashlib.sha256(proposed).hexdigest()
    payload = json.dumps(
        [brain, relative_path, operation, proposed_identity],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def draft_evidence_candidates(config: Any, *, workspace: str, relation: Any) -> list[ShareDraft | ShareRefusal]:
    """File bounded, individually reviewed candidates from engine-attested evidence."""
    from ciao import share_evidence, shared_brain_mapping
    from ciao.workspaces import workspace_registry_claim, WorkspaceRegistryBusy, WorkspaceRegistryConflict

    if not share_evidence._relation_is_sealed(config, relation) or relation.kind != "memory_checkpoint":
        share_evidence._refuse("invalid_relation", "A durable engine-attested source is required.")
    if relation.workspace != workspace:
        return []
    record = config.workspace(workspace)
    links = list(getattr(record, "shared_brains", []) or [])
    results: list[ShareDraft | ShareRefusal] = []
    for link in links:
        if len(results) >= MAX_CANDIDATES_PER_RUN:
            break
        mapping = getattr(link, "guide_mapping", None)
        grant = next((item for item in getattr(mapping, "grants", ()) if item.note_type == "decision"), None)
        if grant is None:
            continue
        try:
            with shared_brain.checkout_claims_nowait([link.path], config), workspace_registry_claim(config) as owner:
                authorizations = share_evidence.mint_evidence_authorizations(
                    config, workspace=workspace, brain_name=link.name, relation=relation,
                )
                for authorization in authorizations[:MAX_CANDIDATES_PER_RUN - len(results)]:
                    with share_evidence.evidence_authorization_guard(config, workspace=workspace, authorization=authorization) as row:
                        identity = {key: row.get(key) for key in (
                            "project_id", "source_id", "entry_id", "entry_revision", "snapshot_id",
                            "digest", "consent_generation", "audience", "field_mask",
                        )}
                        identity.update(workspace=workspace, workspace_lifetime=relation.workspace_lifetime_id,
                                        brain_lifetime=link.lifetime_id)
                        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        opaque = hmac.new(share_evidence._relation_secret(config), b"share-candidate-path\0" + encoded, hashlib.sha256).hexdigest()
                        relative_path = f"{grant.path}/{opaque}.md" if grant.kind == "collection" else ""
                        snapshot = shared_brain_mapping.resolve_workspace_destination(
                            config, workspace, link.name, note_type="decision", relative_path=relative_path, owner=owner,
                        )
                        if snapshot is None:
                            continue
                        destination = snapshot.destination.destination
                        operation = "replace" if destination.operation == "append" else destination.operation
                        if operation not in ("create", "replace"):
                            continue
                        captured = _qualify_owned(config, workspace, link, destination.path, operation, owner)
                        proposed = _compose_candidate(captured[2], _render_evidence(row["canonical_bytes"], row["digest"]), destination)
                        identity.update(destination=asdict(snapshot), operation=operation,
                                        base_commit=captured[1], base_blob=captured[3], base_mode=captured[6],
                                        base_digest=hashlib.sha256(captured[2]).hexdigest() if captured[2] is not None else None,
                                        result_digest=hashlib.sha256(proposed).hexdigest())
                        request = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        fingerprint = hashlib.sha256(request.encode("utf-8")).hexdigest()
                        queue = Path(config.workspace_vault_root(workspace)) / _QUEUE
                        lock = _thread_queue_lock(queue)
                        if not lock.acquire(blocking=False):
                            _refuse("draft_unavailable", "The proposal queue is currently unavailable.")
                        try:
                            with memory_receipts.queue_lock(queue, timeout_s=0):
                                if not shared_brain_mapping.revalidate_snapshot(config, snapshot, owner=owner):
                                    _refuse("brain_unavailable", "The reviewed destination mapping changed.")
                                store = _storage_safe(config, workspace)
                                parent_fd = folder_io.open_directory_nofollow(str(store))
                                try:
                                    run_fd, _ = folder_io.open_or_create_private_dir(parent_fd, "candidate-runs")
                                    os.close(run_fd)
                                finally:
                                    os.close(parent_fd)
                                run_id = hmac.new(share_evidence._relation_secret(config),
                                    (workspace + "\0" + relation.workspace_lifetime_id + "\0" + relation.relation_id).encode(),
                                    hashlib.sha256).hexdigest()
                                ledger_path = store / "candidate-runs" / (run_id + ".json")
                                ledger = share_evidence._read_json(ledger_path, limit=8192, code="private_state_corrupt")
                                fingerprints = ledger.get("fingerprints", [])
                                if not isinstance(fingerprints, list) or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in fingerprints) or len(fingerprints) > MAX_CANDIDATES_PER_RUN:
                                    _refuse("draft_corrupt", "The private candidate run is corrupt.")
                                if fingerprint not in fingerprints:
                                    if len(fingerprints) >= MAX_CANDIDATES_PER_RUN:
                                        _refuse("capacity", "The candidate intake limit for this run is reached.", limit=MAX_CANDIDATES_PER_RUN, count=len(fingerprints))
                                    fingerprints.append(fingerprint)
                                    share_evidence._write_private(ledger_path, json.dumps({"schema": share_evidence.SCHEMA, "fingerprints": fingerprints}).encode(), limit=8192)
                                draft = _draft_share(
                                    config, workspace=workspace, brain=link.name, relative_path=destination.path,
                                    operation=operation, proposed=proposed, request_key="candidate:" + fingerprint,
                                    request_fingerprint=fingerprint, checkout_claimed=True, registry_owner=owner,
                                    expected_qualification=captured,
                                    candidate_binding={"authorization": authorization, **asdict(snapshot)},
                                )
                                artifact = draft.artifact
                                provenance = artifact.setdefault("producer_provenance", [])
                                if not isinstance(provenance, list):
                                    _refuse("draft_corrupt", "The private candidate provenance is corrupt.")
                                if authorization not in provenance:
                                    provenance.append(authorization)
                                    sidecar_id = artifact["immutable"]["sidecar_id"]
                                    sidecar = _store_dir(config) / (hashlib.sha256((workspace + "\0sidecar\0" + sidecar_id).encode()).hexdigest() + ".json")
                                    _write_private(sidecar, json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                                results.append(draft)
                        finally:
                            lock.release()
        except ShareDraftError as exc:
            results.append(exc.refusal)
        except (OSError, WorkspaceRegistryBusy, WorkspaceRegistryConflict, memory_receipts.QueueLockError):
            results.append(ShareRefusal("draft_unavailable", "The candidate could not be filed safely."))
    return results


def load_share(config: Any, *, workspace: str, proposal_id: str) -> dict[str, Any] | ShareRefusal:
    """Load a draft only when its queue row independently names the same artifact."""
    try:
        store = _store_dir(config)
        queue = Path(config.workspace_vault_root(workspace)) / _QUEUE
        with _thread_queue_lock(queue), memory_receipts.queue_lock(queue):
            text = queue.read_text(encoding="utf-8")
        entry = next((item for item in proposal_tracking.walk_proposal_queue(workspace, (PurePosixPath(workspace) / _QUEUE).as_posix(), text) if item.proposal_id == proposal_id), None)
        if entry is None or entry.bullet.kind != KIND:
            return ShareRefusal("draft_corrupt", "The queued share draft could not be verified.")
        sidecar_id = entry.bullet.target
        data = _load_sidecar(store / (hashlib.sha256((workspace + "\0sidecar\0" + sidecar_id).encode()).hexdigest() + ".json"))
        fields = data["immutable"]
        if fields.get("proposal_id") != proposal_id or fields.get("workspace") != workspace or fields.get("sidecar_id") != sidecar_id or entry.bullet.text != f"Shared note change proposal {sidecar_id}":
            return ShareRefusal("draft_corrupt", "The queued share draft could not be verified.")
        return data
    except ShareDraftError as exc:
        return exc.refusal
    except (OSError, UnicodeError):
        return ShareRefusal("draft_corrupt", "The queued share draft could not be verified.")
