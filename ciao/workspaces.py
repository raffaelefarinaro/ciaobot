"""Shared workspace-registry helpers.

The PWA settings routes (``ciao/web/routes_api.py``) and the MCP control
plane (``ciao/control_plane.py``) both create, update, serialize, and
persist logical workspaces. These helpers used to live inside the routes
module; they are shared here so the two surfaces cannot drift apart on
validation or serialization rules.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar, cast

from ciao import provider_registry
from ciao.claude_trim import coerce_claude_trim
from ciao.config import (
    CLAUDE_MODELS,
    DEFAULT_MEMORY_HANDOVER,
    DEFAULT_NEW_WORKSPACE_MEMORY_HANDOVER,
    DEFAULT_WORKSPACE_COLOR,
    SharedBrainLink,
    WorkspaceConfig,
    coerce_memory_approval,
    coerce_memory_handover,
    coerce_provider_overrides,
    coerce_workspace_color,
    merge_memory_approval,
    shared_brain_row,
)
from ciao import shared_brain
from ciao.model_tiers import is_tier
from ciao.provider_service import capabilities_for
from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import is_private_descriptor_strict, make_private_dir

WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_REGISTRY_LOCKS: dict[int, threading.RLock] = {}
_REGISTRY_LOCKS_GUARD = threading.Lock()
_REGISTRY_CLAIMS: dict[str, threading.Lock] = {}


_T = TypeVar("_T")


async def run_while_claimed(function: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """A cancelled request retains its claims until the filesystem worker stops."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


class WorkspaceRegistryConflict(RuntimeError):
    """The persisted registry no longer matches this process's snapshot."""


class WorkspaceRegistryBusy(WorkspaceRegistryConflict):
    """Another process currently owns the workspace registry transaction."""


class WorkspaceRegistryOwnership:
    """Explicit descriptor ownership; safe to pass across async task threads."""

    def __init__(self, key: str, local_lock: threading.Lock, fd: int) -> None:
        self.key = key
        self.local_lock = local_lock
        self.fd = fd
        self.closed = False
        self.path = Path(key) / "workspaces.json"
        self.current = _read_registry_bytes(self.path)

    def __enter__(self) -> WorkspaceRegistryOwnership:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def replace(self, payload: bytes | None, *, expected: bytes | None) -> None:
        if self.closed or self.current != expected or _read_registry_bytes(self.path) != expected:
            raise WorkspaceRegistryConflict("workspace registry changed since it was loaded")
        path = self.path
        if payload is None:
            path.unlink(missing_ok=True)
            self.current = None
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".workspaces-", suffix=".tmp", dir=path.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            replace_file(temp, path)
            self.current = payload
        finally:
            temp.unlink(missing_ok=True)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            unlock(self.fd)
        finally:
            os.close(self.fd)
            self.local_lock.release()


def acquire_workspace_registry(config: Any, *, blocking: bool = False) -> WorkspaceRegistryOwnership:
    """Acquire explicit stable lock ownership; caller closes the returned owner."""
    return acquire_registry_at(Path(config.state_path).parent, blocking=blocking)


def acquire_registry_at(runtime_root: Path, *, blocking: bool = False) -> WorkspaceRegistryOwnership:
    key = str(runtime_root.resolve())
    with _REGISTRY_LOCKS_GUARD:
        local_lock = _REGISTRY_CLAIMS.setdefault(key, threading.Lock())
    if not local_lock.acquire(blocking=blocking):
        raise WorkspaceRegistryBusy("workspace registry is busy")
    fd = None
    try:
        root = Path(key)
        lock_root = root / "locks"
        lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        make_private_dir(lock_root)
        path = lock_root / "workspaces-registry.lock"
        fd = open_fd(path, os.O_CREAT | os.O_RDWR, 0o600, follow_symlinks=False)
        if not is_private_descriptor_strict(fd):
            raise OSError("workspace registry lock is not private")
        try:
            lock_exclusive(fd, blocking=blocking)
        except BlockingIOError as exc:
            raise WorkspaceRegistryBusy("workspace registry is busy") from exc
        return WorkspaceRegistryOwnership(key, local_lock, fd)
    except BaseException:
        try:
            local_lock.release()
        finally:
            if fd is not None:
                os.close(fd)
        raise


@contextmanager
def workspace_registry_claim(config: Any):
    """Synchronous convenience claim; do not span awaits with this context."""
    owner = acquire_workspace_registry(config)
    try:
        yield owner
    finally:
        owner.close()


def ensure_registry_current(config: Any) -> None:
    if read_workspace_registry_bytes(config) != getattr(config, "_workspace_registry_bytes", None):
        raise WorkspaceRegistryConflict("workspace registry changed since it was loaded")


def read_workspace_registry_bytes(config: Any) -> bytes | None:
    """Read exact persisted registry bytes; None denotes a missing registry."""
    return _read_registry_bytes(_workspaces_path(config))


def _read_registry_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def replace_workspace_registry(
    config: Any, payload: bytes, *, expected: bytes | None,
    owner: WorkspaceRegistryOwnership,
) -> None:
    """Compare and atomically replace the registry under its interprocess claim."""
    if owner.closed or owner.key != str(Path(config.state_path).resolve().parent):
        raise WorkspaceRegistryConflict("workspace registry ownership is invalid")
    owner.replace(payload, expected=expected)


def registry_rows(data: Any) -> list[dict[str, Any]]:
    """Read list and legacy name-keyed registries, retaining nested link records."""
    if isinstance(data, dict) and all(isinstance(value, dict) for value in data.values()):
        return [{"name": name, **value} for name, value in data.items()]
    if isinstance(data, list) and all(isinstance(row, dict) for row in data):
        return data
    raise WorkspaceRegistryConflict("workspace registry is invalid")


def persisted_checkout_links(raw: bytes, path: str) -> list[tuple[str, dict[str, Any]]]:
    """Collect current Git links, refusing any folder or unknown kind at this checkout."""
    matches = []
    canonical = os.path.realpath(path)
    try:
        rows = registry_rows(json.loads(raw))
    except ValueError as exc:
        raise WorkspaceRegistryConflict("workspace registry is unreadable") from exc
    for row in rows:
        links = row.get("shared_brains", [])
        if not isinstance(links, list):
            raise WorkspaceRegistryConflict("workspace shared brains are unreadable")
        for candidate in links:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("path"), str):
                raise WorkspaceRegistryConflict("workspace shared brain path is unreadable")
            if os.path.realpath(candidate["path"]) != canonical:
                continue
            if candidate.get("kind", "git") != "git":
                raise WorkspaceRegistryConflict("the checkout is linked with another storage kind")
            matches.append((row.get("name", ""), candidate))
    return matches


def patch_shared_brain_lifetime(config: Any, workspace: str, link: Any, *, owner: WorkspaceRegistryOwnership) -> str:
    """Set only the matching persisted Git row's missing lifetime; reuse a peer token."""
    raw = read_workspace_registry_bytes(config)
    if raw is None:
        raise WorkspaceRegistryConflict("workspace registry is missing")
    data = json.loads(raw)
    rows = [row for row in registry_rows(data) if row.get("name") == workspace]
    if len(rows) != 1 or not isinstance(rows[0].get("shared_brains"), list):
        raise WorkspaceRegistryConflict("workspace registry entry is missing or ambiguous")
    links = rows[0]["shared_brains"]
    matches = [item for item in links if isinstance(item, dict) and item.get("name") == link.name]
    if len(matches) != 1:
        raise WorkspaceRegistryConflict("shared brain link is missing or ambiguous")
    item = matches[0]
    if (item.get("path"), item.get("kind", "git"), item.get("identity")) != (link.path, "git", link.identity):
        raise WorkspaceRegistryConflict("shared brain link changed")
    existing = item.get("lifetime_id")
    if isinstance(existing, str) and existing:
        return existing
    if raw != getattr(config, "_workspace_registry_bytes", None):
        raise WorkspaceRegistryConflict("workspace registry changed before lifetime initialization")
    item["lifetime_id"] = link.lifetime_id
    payload = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    replace_workspace_registry(config, payload, expected=raw, owner=owner)
    config._workspace_registry_bytes = payload
    return str(link.lifetime_id)


def workspace_registry_lock(config: Any) -> threading.RLock:
    """Serialize registry snapshots with initialization of legacy link IDs.

    This is the in-process reentrant lock. Cross-process writers use
    ``acquire_workspace_registry`` and ``replace_workspace_registry`` for the
    authoritative claim plus byte-exact compare-and-swap. Shared-brain policy
    writes take both that registry transaction and the cross-process checkout
    claim.
    """
    key = id(config)
    with _REGISTRY_LOCKS_GUARD:
        return _REGISTRY_LOCKS.setdefault(key, threading.RLock())

# Every selectable provider is a runtime provider, so the registry is the only
# source for labels.
_WORKSPACE_PROVIDER_LABELS = {
    item.id: item.label for item in provider_registry.descriptors()
}


def workspace_provider_options(config: Any) -> list[dict[str, str]]:
    values = list(provider_registry.provider_ids())
    return [
        {"value": value, "label": _WORKSPACE_PROVIDER_LABELS[value]}
        for value in values
    ]


def workspace_provider_values(config: Any) -> set[str]:
    return {option["value"] for option in workspace_provider_options(config)}


def agent_root_for(config: Any, workspace: str) -> Path:
    """The agent root that owns one workspace's own assets, and what to do
    with a name that does not resolve.

    Returns the root for a registered workspace, or the install root for an
    empty or unknown name. An unknown name is NOT an error here: the caller is
    usually a read that should degrade to the whole install rather than 400,
    and every root this returns is one the app already owns. Callers that
    mutate must confirm the name separately — see
    :func:`resolve_workspace_name`.
    """
    name = str(workspace or "").strip()
    if name and config.workspace(name):
        try:
            return Path(config.agent_root(name))
        except ValueError:
            # A registered name that cannot be a folder (it came from a
            # hand-edited registry) falls back to the install root rather
            # than raising out of a GET.
            pass
    return Path(config.workspace_root)


def resolve_workspace_name(config: Any, workspace: str, *, required: bool = True) -> str:
    """The canonical registered workspace name, or raise ``ValueError``.

    For a write: an unregistered or empty name is refused rather than
    silently redirected to the install root, so a stale or hostile
    ``workspace`` field can never file an asset somewhere the user did not
    ask for.
    """
    name = str(workspace or "").strip()
    if name and config.workspace(name):
        return name
    if required:
        raise ValueError(f"unknown workspace '{workspace or ''}'")
    primary: str = config.primary_workspace()
    return primary


def workspace_to_dict(workspace: WorkspaceConfig, config: Any) -> dict:
    try:
        color = coerce_workspace_color(getattr(workspace, "color", DEFAULT_WORKSPACE_COLOR))
    except ValueError:
        color = DEFAULT_WORKSPACE_COLOR
    try:
        memory_handover = coerce_memory_handover(workspace.memory_handover)
    except ValueError:
        memory_handover = DEFAULT_MEMORY_HANDOVER
    try:
        memory_approval = coerce_memory_approval(workspace.memory_approval)
    except ValueError:
        memory_approval = {}
    try:
        provider_overrides = coerce_provider_overrides(workspace.provider_overrides)
    except ValueError:
        provider_overrides = {}
    # A stored value naming a removed backend (e.g. pre-refactor "ollama") is
    # reported as the provider that actually runs the workspace. The PWA
    # renders this into a <select> limited to the provider options, so an
    # unregistered value would show a blank dropdown and be rejected on save.
    provider = config.default_provider_for_workspace(getattr(workspace, "name", None))
    return {
        "name": getattr(workspace, "name", ""),
        "vault_root": getattr(workspace, "vault_root", ""),
        "default_provider": provider,
        "disallowed_tools": (
            list(cast(Iterable[Any], getattr(workspace, "disallowed_tools", None)))
            if getattr(workspace, "disallowed_tools", None) is not None
            else None
        ),
        "claude_trim": dict(getattr(workspace, "claude_trim", None) or {}),
        "color": color,
        "memory_handover": memory_handover,
        "memory_approval": memory_approval,
        "provider_overrides": provider_overrides,
        "shared_brains": [
            {"name": link.name, "path": link.path, "identity": link.identity, "kind": link.kind}
            for link in getattr(workspace, "shared_brains", [])
        ],
    }


_SHARED_BRAINS_SHAPE = "shared_brains must be a list of {path, name}"


def _shared_brain_rows(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise ValueError(_SHARED_BRAINS_SHAPE)
    rows: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError(_SHARED_BRAINS_SHAPE)
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError(_SHARED_BRAINS_SHAPE)
        kind = item.get("kind")
        if "kind" in item and not isinstance(kind, str):
            raise ValueError(_SHARED_BRAINS_SHAPE)
        row = {"path": item["path"]}
        if name is not None:
            row["name"] = name
        if "kind" in item:
            row["kind"] = kind
        rows.append(row)
    return rows


def requested_brain_rows(raw: object) -> list[dict[str, str]]:
    rows = _shared_brain_rows(raw)
    if len(rows) > shared_brain.MAX_BRAINS_PER_WORKSPACE:
        raise ValueError(f"A workspace can link at most {shared_brain.MAX_BRAINS_PER_WORKSPACE} shared brains")
    paths: set[str] = set()
    for row in rows:
        value = row["path"]
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("Shared brain path contains a control character")
        # The path is exact: a trailing or leading space can name a different
        # existing folder, and trimming it would link the wrong one.
        path = os.path.expanduser(value)
        if not os.path.isabs(path):
            raise ValueError("Shared brain folder path must be absolute")
        canonical = os.path.realpath(path)
        if canonical in paths:
            raise ValueError(f"Shared brain folder is listed twice: {canonical}")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in canonical):
            raise ValueError("Shared brain path cannot be represented unambiguously")
        paths.add(canonical)
        row["path"] = canonical
    return rows


def check_requested_brain_kinds(
    owner: WorkspaceRegistryOwnership, requested: list[dict[str, str]], existing: list[SharedBrainLink],
) -> None:
    if any(os.path.realpath(row["path"]) != row["path"] for row in requested):
        raise WorkspaceRegistryConflict("shared brain path changed while acquiring checkout claims")
    if owner.current is None:
        return
    retained = {os.path.realpath(link.path): link.kind for link in existing}
    kinds = {row["path"]: row.get("kind", retained.get(row["path"], "git")) for row in requested}
    for workspace in registry_rows(json.loads(owner.current)):
        links = workspace.get("shared_brains", [])
        if not isinstance(links, list):
            raise WorkspaceRegistryConflict("workspace shared brains are unreadable")
        for link in links:
            if not isinstance(link, dict) or not isinstance(link.get("path"), str):
                raise WorkspaceRegistryConflict("workspace shared brain path is unreadable")
            path = os.path.realpath(link["path"])
            if path in kinds and kinds[path] != link.get("kind", "git"):
                raise ValueError("shared brain kind changed; unlink and add it again to change kind")


def parse_disallowed_tools_value(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw.strip().lower() == "default":
            return None
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    raise ValueError("disallowed_tools must be a list, comma-separated string, or null")


def vault_root_owner(config: Any, target: Path) -> str | None:
    """Name of the configured workspace that already owns ``target``, if any.

    Ownership is overlap, not equality. A target that is an *ancestor* of
    another workspace's vault — or that sits inside one — leaves two registry
    entries pointing into the same notes, so chats and vault writes in either
    workspace can read and rewrite the other workspace's data.

    One nesting is legitimate: the shared vault root itself. On installs where
    setup pointed a workspace at ``CIAO_VAULT_ROOT`` every standard per-workspace folder
    lives inside that workspace's vault by design, so counting it as a conflict
    would refuse every new workspace on those installs.
    """
    shared_root = getattr(config, "vault_root", None)
    for raw_name in config.workspace_names():
        configured_name = str(raw_name)
        try:
            configured_root = Path(config.workspace_vault_root(configured_name))
        except ValueError:
            continue
        if configured_root == target or configured_root.is_relative_to(target):
            return configured_name
        if target.is_relative_to(configured_root) and configured_root != shared_root:
            return configured_name
    return None


def workspace_from_request(
    data: dict,
    *,
    config: Any,
    existing: WorkspaceConfig | None = None,
) -> WorkspaceConfig:
    name = str(data.get("name", existing.name if existing else "")).strip()
    if not WORKSPACE_NAME_RE.match(name):
        raise ValueError("workspace name must use letters, numbers, dashes, or underscores")
    if existing is None:
        for configured_name in config.workspace_names():
            if configured_name.casefold() == name.casefold():
                raise ValueError(
                    f"workspace name conflicts with existing workspace "
                    f"'{configured_name}'"
                )
        target_root = config.canonical_workspace_vault_root(name)
        owner = vault_root_owner(config, Path(target_root))
        if owner is not None:
            raise ValueError(
                f"workspace vault folder is already owned by '{owner}'"
            )
    if "default_provider" in data:
        requested_provider = str(data["default_provider"]).strip() or "claude"
    else:
        # A save that leaves the provider untouched must not be rejected
        # because the stored value names a removed backend (pre-refactor
        # "ollama"); fall back to the provider that actually runs the
        # workspace.
        requested_provider = config.default_provider_for_workspace(
            existing.name if existing else None
        )
    available_providers = workspace_provider_values(config)
    if requested_provider not in available_providers:
        allowed = ", ".join(sorted(available_providers))
        raise ValueError(f"default_provider must be one of: {allowed}")
    provider = requested_provider
    if "disallowed_tools" in data:
        disallowed_tools = parse_disallowed_tools_value(data.get("disallowed_tools"))
    elif existing is not None:
        disallowed_tools = existing.disallowed_tools
    else:
        disallowed_tools = None
    if "claude_trim" in data:
        claude_trim = coerce_claude_trim(data.get("claude_trim"))
    elif existing is not None:
        claude_trim = dict(existing.claude_trim)
    else:
        claude_trim = {}
    if "color" in data:
        color = coerce_workspace_color(data.get("color"))
    elif existing is not None:
        try:
            color = coerce_workspace_color(existing.color)
        except ValueError:
            color = DEFAULT_WORKSPACE_COLOR
    else:
        color = DEFAULT_WORKSPACE_COLOR
    if "memory_handover" in data:
        memory_handover = coerce_memory_handover(data.get("memory_handover"))
    elif existing is not None:
        memory_handover = existing.memory_handover
    else:
        memory_handover = DEFAULT_NEW_WORKSPACE_MEMORY_HANDOVER
    if "memory_approval" in data:
        # A partial save: each named kind is set or, with null, reset to inherited.
        # Validated whole before anything is returned, so a bad entry changes nothing.
        memory_approval = merge_memory_approval(
            existing.memory_approval if existing is not None else {},
            data.get("memory_approval"),
        )
    elif existing is not None:
        memory_approval = dict(existing.memory_approval)
    else:
        memory_approval = {}
    if "provider_overrides" in data:
        provider_overrides = coerce_provider_overrides(data.get("provider_overrides"))
        _check_override_models(provider_overrides)
    elif existing is not None:
        provider_overrides = existing.provider_overrides
    else:
        provider_overrides = {}
    if "shared_brains" in data:
        # Whole-list replacement; git runs only for rows that are new.
        shared_brains = shared_brain.resolve_links(
            config,
            name,
            _shared_brain_rows(data.get("shared_brains")),
            list(getattr(existing, "shared_brains", [])) if existing is not None else [],
        )
    elif existing is not None:
        shared_brains = list(getattr(existing, "shared_brains", []))
    else:
        shared_brains = []
    return WorkspaceConfig(
        name=name,
        # Vault locations are not an editable Settings field. Updating a
        # workspace must preserve setup-created/external roots exactly; a new
        # user-named workspace always receives its standard folder beneath the
        # configured vault. Accepting request-body paths here allowed `/` and
        # `..` to turn one authenticated save into filesystem-wide writes and
        # scans.
        vault_root=(
            existing.vault_root
            if existing is not None
            else config.stored_workspace_vault_root(name)
        ),
        default_provider=provider,
        disallowed_tools=disallowed_tools,
        claude_trim=claude_trim,
        color=color,
        memory_handover=memory_handover,
        shared_brains=shared_brains,
        memory_approval=memory_approval,
        provider_overrides=provider_overrides,
    )


def _check_override_models(overrides: dict[str, dict[str, str]]) -> None:
    """Reject a model override a new chat could not start with.

    Mirrors ``ProjectChatManager._validate_configured_model``: an invalid saved
    Claude model would make every new chat in the workspace fail at creation.
    Dynamic-catalog providers (opencode) are exempt; their catalog is async.
    """
    for provider, fields in overrides.items():
        if capabilities_for(provider).dynamic_models:
            continue
        for field_name in ("model", "insights_model"):
            value = fields.get(field_name)
            if value and not (is_tier(value) or value in CLAUDE_MODELS):
                raise ValueError(
                    f"provider_overrides.{provider}.{field_name}: unknown model '{value}'"
                )


def _workspaces_path(config: Any) -> Path:
    return Path(config.state_path).resolve().parent / "workspaces.json"


def persist_workspaces(
    config: Any, candidate_workspaces: dict[str, Any] | None = None, *, owner: WorkspaceRegistryOwnership | None = None
) -> None:
    persist = getattr(config, "persist_workspace_registry", None)
    if candidate_workspaces is None and callable(persist):
        if owner is None:
            persist()
        else:
            persist(owner=owner)
        return
    workspaces = candidate_workspaces if candidate_workspaces is not None else config.workspaces
    payload = [workspace_to_dict(workspace, config) for workspace in workspaces.values()]
    for row, workspace in zip(payload, workspaces.values()):
        if candidate_workspaces is not None:
            row["default_provider"] = workspace.default_provider
            if not workspace.claude_trim:
                row.pop("claude_trim", None)
            if workspace.memory_handover == DEFAULT_MEMORY_HANDOVER:
                row.pop("memory_handover", None)
            if not workspace.memory_approval:
                row.pop("memory_approval", None)
            if not workspace.provider_overrides:
                row.pop("provider_overrides", None)
            if not workspace.shared_brains:
                row.pop("shared_brains", None)
        if workspace.shared_brains:
            row["shared_brains"] = [
                shared_brain_row(link) for link in workspace.shared_brains
            ]
    expected = getattr(config, "_workspace_registry_bytes", None)
    content = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    own_owner = owner is None
    if owner is None:
        owner = acquire_workspace_registry(config)
    try:
        replace_workspace_registry(config, content, expected=expected, owner=owner)
        config._workspace_registry_bytes = content
    finally:
        if own_owner:
            owner.close()
