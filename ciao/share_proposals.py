"""Private, immutable drafts for publishing one note to a linked shared brain.

The queue contains only a fixed summary and a random handle. Exact content and
destination details stay in owner-only runtime state until a human reviews them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

from ciao import memory_receipts, proposal_tracking, shared_brain
from ciao.config import SharedBrainLink
from ciao.git_proc import run_git_sync_bounded
from ciao.os_support.files import open_fd
from ciao.os_support.private import is_private_descriptor_strict, make_private_dir, mkstemp_private
from ciao.proposal_tracking import proposal_review_path
from ciao.redact import redact_text

KIND = "share"
MAX_BYTES = 65_536
MAX_PER_BRAIN = 10
MAX_PER_WORKSPACE = 30
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
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        make_private_dir(root)
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


def _qualify(config: Any, link: SharedBrainLink, relative_path: str) -> tuple[str, str, bytes | None, str | None]:
    parts = _safe_path(relative_path)
    state = shared_brain.brain_state(link, config)
    if state.get("problem"):
        code = "brain_changed" if state.get("problem") == "origin_changed" else "brain_unavailable"
        _refuse(code, "The selected shared brain is unavailable or has changed.")
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
    if original is not None and len(original) > MAX_BYTES:
        _refuse("too_large", "The existing note exceeds the 65,536-byte limit.")
    try:
        if original is not None:
            original.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _refuse("invalid_utf8", "The existing note is not valid UTF-8.")
    return full_ref, commit, original, original_oid


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


def _review_token(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "shr_" + hashlib.sha256(payload).hexdigest()


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


def _draft_share(
    config: Any, *, workspace: str, brain: str = "", relative_path: str,
    operation: str | None, proposed: bytes | None, request_key: str,
    request_fingerprint: str,
) -> ShareDraft:
    """Qualify and privately file one immutable shared-note proposal."""
    if operation not in (None, "create", "replace", "delete"):
        _refuse("invalid_operation", "Use create, replace, or delete.")
    if not isinstance(request_key, str) or not request_key:
        _refuse("request_conflict", "A stable internal request key is required.")
    link = _brain_for(config, workspace, brain)
    op_ref, base_commit, original, original_oid = _qualify(config, link, relative_path)
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
        _write_private(id_path, json.dumps(previous, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        vault = Path(config.workspace_vault_root(workspace))
        queue = vault / _QUEUE
        rel_queue = (PurePosixPath(workspace) / _QUEUE).as_posix()
        with _thread_queue_lock(queue), memory_receipts.queue_lock(queue):
            queue_text = queue.read_text(encoding="utf-8") if queue.exists() else "# Memory Proposals\n"
            rows = list(proposal_tracking.walk_proposal_queue(workspace, rel_queue, queue_text))
            if any(row.proposal_id == fields["proposal_id"] for row in rows):
                return ShareDraft(fields["proposal_id"], previous, proposal_review_path(workspace, fields["review_key"]))
            from ciao.memory_proposals import _dismissed_texts
            summary = f"Shared note change proposal {fields['sidecar_id']}"
            if summary in _dismissed_texts(queue):
                _refuse("request_conflict", "This share proposal has already been settled.")
            count = sum(row.bullet.kind == KIND for row in rows)
            if count >= MAX_PER_WORKSPACE:
                _refuse("capacity", "The workspace share proposal limit is reached.", limit=MAX_PER_WORKSPACE, count=count)
            brain_count = 0
            for row in rows:
                if row.bullet.kind != KIND:
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
            memory_receipts.write_queue_atomically(queue, queue_text.rstrip() + f"\n\n- [share {fields['sidecar_id']}] {summary}\n")
        return ShareDraft(fields["proposal_id"], previous, proposal_review_path(workspace, fields["review_key"]))
    sidecar_id = secrets.token_urlsafe(18)
    text = f"Shared note change proposal {sidecar_id}"
    rel_queue = (PurePosixPath(workspace) / _QUEUE).as_posix()
    proposal_id = proposal_tracking.stable_proposal_id(workspace, rel_queue, KIND, text, "", 0)
    _origin_code, listing = shared_brain._git(Path(link.path), "config", "--list", "--show-scope")
    destination_fingerprint = _origin_fingerprint(shared_brain._origin_from_listing(listing))
    if _origin_code != 0 or not destination_fingerprint:
        _refuse("brain_changed", "The shared brain destination could not be verified.")
    token_fields: dict[str, Any] = {
        "schema": _SCHEMA,
        "workspace": workspace, "proposal_id": proposal_id, "revision": 1,
        "sidecar_id": sidecar_id, "brain": link.name, "origin_identity": link.identity,
        "destination_fingerprint": destination_fingerprint,
        "upstream_ref": op_ref, "path": relative_path, "operation": operation,
        "base_blob_oid": original_oid,
        "original": original_text, "proposed": proposed_text,
        "proposed_sha256": hashlib.sha256(proposed).hexdigest() if proposed is not None else None,
        "base_commit": base_commit,
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
        count = sum(1 for entry in proposal_tracking.walk_proposal_queue(workspace, rel_queue, existing_text) if entry.bullet.kind == KIND)
        if count >= MAX_PER_WORKSPACE:
            _refuse("capacity", "The workspace share proposal limit is reached.", limit=MAX_PER_WORKSPACE, count=count)
        brain_count = 0
        for entry in proposal_tracking.walk_proposal_queue(workspace, rel_queue, existing_text):
            if entry.bullet.kind != KIND:
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
        _write_private(index_path, json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        id_path = store / (hashlib.sha256((workspace + "\0sidecar\0" + sidecar_id).encode()).hexdigest() + ".json")
        _write_private(id_path, json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        memory_receipts.write_queue_atomically(queue, existing_text.rstrip() + "\n\n" + bullet + "\n")
    return ShareDraft(proposal_id, artifact, proposal_tracking.proposal_review_path(workspace, token_fields["review_key"]))


def draft_share(
    config: Any, *, workspace: str, brain: str = "", relative_path: str,
    operation: str | None, proposed: bytes | None, request_key: str,
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
