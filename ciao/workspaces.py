"""Shared workspace-registry helpers.

The PWA settings routes (``ciao/web/routes_api.py``) and the MCP control
plane (``ciao/control_plane.py``) both create, update, serialize, and
persist logical workspaces. These helpers used to live inside the routes
module; they are shared here so the two surfaces cannot drift apart on
validation or serialization rules.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import tempfile
import threading
import contextvars
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, cast

from ciao.os_support.locks import lock_exclusive, lock_shared, unlock

from ciao import provider_registry
from ciao.claude_trim import coerce_claude_trim
from ciao.config import (
    CLAUDE_MODELS,
    DEFAULT_MEMORY_HANDOVER,
    DEFAULT_NEW_WORKSPACE_MEMORY_HANDOVER,
    DEFAULT_WORKSPACE_COLOR,
    SHARED_BRAIN_NAME_PATTERN,
    WorkspaceConfig,
    coerce_memory_approval,
    coerce_memory_handover,
    coerce_provider_overrides,
    coerce_workspace_color,
    merge_memory_approval,
)
from ciao import shared_brain
from ciao.model_tiers import is_tier
from ciao.provider_service import capabilities_for

WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_REGISTRY_LOCKS: dict[int, threading.RLock] = {}
_REGISTRY_LOCKS_GUARD = threading.Lock()
_ACTIVE_REGISTRY_OWNER: contextvars.ContextVar[WorkspaceRegistryOwnership | None] = (
    contextvars.ContextVar("active_workspace_registry_owner", default=None)
)


class WorkspaceRegistryChanged(OSError):
    """The on-disk registry changed under a writer holding a stale snapshot."""


class WorkspaceRegistryConflict(WorkspaceRegistryChanged):
    """The persisted registry no longer matches this transaction's snapshot."""


class WorkspaceRegistryOwnership:
    """Explicit registry lock ownership carrying the exact loaded bytes."""

    def __init__(self, runtime_root: Path, *, blocking: bool = False) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.key = str(self.runtime_root)
        self.current: bytes | None = None
        self.closed = False
        self._context = workspace_registry_file_lock(
            self.runtime_root, exclusive=True, blocking=blocking
        )

    def __enter__(self) -> WorkspaceRegistryOwnership:
        self._context.__enter__()
        self.current = (_read_registry_bytes(self.runtime_root))
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def replace(self, payload: bytes | None, *, expected: bytes | None) -> None:
        if self.closed or self.current != expected:
            raise WorkspaceRegistryConflict("workspace registry changed since it was loaded")
        path = self.runtime_root / "workspaces.json"
        if _read_registry_bytes(self.runtime_root) != expected:
            raise WorkspaceRegistryConflict("workspace registry changed on disk")
        if payload is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write_registry_bytes(path, payload)
            self.current = payload

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._context.__exit__(None, None, None)


def acquire_registry_at(runtime_root: Path, *, blocking: bool = False) -> WorkspaceRegistryOwnership:
    owner = WorkspaceRegistryOwnership(runtime_root, blocking=blocking)
    owner.__enter__()
    return owner


def acquire_workspace_registry(
    config: Any, *, blocking: bool = True,
) -> WorkspaceRegistryOwnership:
    return acquire_registry_at(Path(config.state_path).parent, blocking=blocking)


@contextmanager
def registry_owner_scope(owner: WorkspaceRegistryOwnership):
    token = _ACTIVE_REGISTRY_OWNER.set(owner)
    try:
        yield owner
    finally:
        _ACTIVE_REGISTRY_OWNER.reset(token)


def active_registry_owner(runtime_root: Path) -> WorkspaceRegistryOwnership | None:
    owner = _ACTIVE_REGISTRY_OWNER.get()
    if owner is None or owner.closed:
        return None
    return owner if owner.key == str(Path(runtime_root).resolve()) else None


@contextmanager
def workspace_registry_claim(config: Any) -> Iterator[WorkspaceRegistryOwnership]:
    owner = acquire_registry_at(Path(config.state_path).parent, blocking=False)
    try:
        yield owner
    finally:
        owner.close()


def workspace_registry_lock(config: Any) -> threading.RLock:
    """Serialize registry snapshots with initialization of legacy link IDs."""
    key = id(config)
    with _REGISTRY_LOCKS_GUARD:
        return _REGISTRY_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def workspace_registry_file_lock(
    runtime_root: Path, *, exclusive: bool = True, blocking: bool = True,
) -> Iterator[None]:
    """Interprocess barrier for readers and writers of ``workspaces.json``."""
    runtime_root.mkdir(parents=True, exist_ok=True)
    lock_root = runtime_root / "locks"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_root, 0o700)
    lock_path = lock_root / "workspaces-registry.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        (lock_exclusive if exclusive else lock_shared)(fd, blocking=blocking)
        yield
    finally:
        unlock(fd)
        os.close(fd)


def registry_rows(data: Any) -> list[dict[str, Any]]:
    """Read list and legacy name-keyed registries without dropping rows."""
    if isinstance(data, dict) and all(isinstance(value, dict) for value in data.values()):
        return [{"name": name, **value} for name, value in data.items()]
    if isinstance(data, list) and all(isinstance(row, dict) for row in data):
        return data
    raise WorkspaceRegistryConflict("workspace registry is invalid")


def authoritative_shared_links_from_bytes(raw: bytes | None) -> tuple[Path, ...] | None:
    """Strict linked roots from exact registry bytes; None when untrustworthy."""
    if raw is None:
        return ()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        items = [
            {"name": name, **value}
            for name, value in parsed.items()
            if isinstance(value, dict)
        ]
        if len(items) != len(parsed):
            return None
    elif isinstance(parsed, list):
        items = parsed
    else:
        return None
    if any(not isinstance(item, dict) for item in items):
        return None
    workspace_names: set[str] = set()
    for item in items:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or name in workspace_names:
            return None
        workspace_names.add(name)
        links = item.get("shared_brains")
        if links is None:
            continue
        if not isinstance(links, list):
            return None
        seen_names: set[str] = set()
        seen_paths: set[str] = set()
        seen_identities: set[str] = set()
        for link in links:
            if not isinstance(link, dict):
                return None
            link_path = link.get("path")
            link_name = link.get("name")
            identity = link.get("identity")
            if (
                not isinstance(link_path, str)
                or not link_path
                or not os.path.isabs(link_path)
                or any(ord(char) < 32 or ord(char) == 127 for char in link_path)
                or not isinstance(link_name, str)
                or re.fullmatch(SHARED_BRAIN_NAME_PATTERN, link_name) is None
                or not isinstance(identity, str)
                or not identity.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in identity)
            ):
                return None
            if (
                link_name in seen_names
                or link_path in seen_paths
                or identity in seen_identities
            ):
                return None
            seen_names.add(link_name)
            seen_paths.add(link_path)
            seen_identities.add(identity)
    from ciao.config import _parse_workspaces_json  # noqa: PLC0415

    workspaces = _parse_workspaces_json(text)
    if parsed and not workspaces:
        return None
    return tuple(
        Path(link.path).expanduser()
        for workspace in workspaces.values()
        for link in workspace.shared_brains
    )


def authoritative_shared_links(runtime_root: Path) -> tuple[Path, ...] | None:
    """Current linked roots, reusing an active claim when one is held."""
    owner = active_registry_owner(runtime_root)
    if owner is not None:
        return authoritative_shared_links_from_bytes(owner.current)
    path = runtime_root / "workspaces.json"
    try:
        with workspace_registry_file_lock(runtime_root, exclusive=False):
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                return ()
    except OSError:
        return None
    return authoritative_shared_links_from_bytes(raw)
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        items = [
            {"name": name, **value}
            for name, value in parsed.items()
            if isinstance(value, dict)
        ]
        if len(items) != len(parsed):
            return None
    elif isinstance(parsed, list):
        items = parsed
    else:
        return None
    if any(not isinstance(item, dict) for item in items):
        return None
    workspace_names: set[str] = set()
    for item in items:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or name in workspace_names:
            return None
        workspace_names.add(name)
        links = item.get("shared_brains")
        if links is None:
            continue
        if not isinstance(links, list):
            return None
        seen_names: set[str] = set()
        seen_paths: set[str] = set()
        seen_identities: set[str] = set()
        for link in links:
            if not isinstance(link, dict):
                return None
            link_path = link.get("path")
            link_name = link.get("name")
            identity = link.get("identity")
            if (
                not isinstance(link_path, str)
                or not link_path
                or not os.path.isabs(link_path)
                or any(ord(char) < 32 or ord(char) == 127 for char in link_path)
                or not isinstance(link_name, str)
                or re.fullmatch(SHARED_BRAIN_NAME_PATTERN, link_name) is None
                or not isinstance(identity, str)
                or not identity.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in identity)
            ):
                return None
            if (
                link_name in seen_names
                or link_path in seen_paths
                or identity in seen_identities
            ):
                return None
            seen_names.add(link_name)
            seen_paths.add(link_path)
            seen_identities.add(identity)
    from ciao.config import _parse_workspaces_json  # noqa: PLC0415

    workspaces = _parse_workspaces_json(raw)
    if parsed and not workspaces:
        return None
    return tuple(
        Path(link.path).expanduser()
        for workspace in workspaces.values()
        for link in workspace.shared_brains
    )

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
            {"name": link.name, "path": link.path, "identity": link.identity}
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
        rows.append({"path": item["path"], "name": name} if name is not None else {"path": item["path"]})
    return rows


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


def _read_registry_bytes(runtime_root: Path) -> bytes | None:
    try:
        return (Path(runtime_root) / "workspaces.json").read_bytes()
    except FileNotFoundError:
        return None


def _atomic_write_registry_bytes(path: Path, payload: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".workspaces-", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def ensure_registry_current(config: Any) -> None:
    """Refuse a transaction whose loaded registry bytes are stale."""
    if read_workspace_registry_bytes(config) != getattr(
        config, "_workspace_registry_bytes", None
    ):
        raise WorkspaceRegistryConflict("workspace registry changed since it was loaded")


def read_workspace_registry_bytes(config: Any) -> bytes | None:
    """Exact persisted registry bytes; None denotes a missing registry."""
    try:
        return _workspaces_path(config).read_bytes()
    except FileNotFoundError:
        return None



def restore_registry_entries(
    runtime_root: Path,
    prior_entries: list[dict[str, Any]] | None,
    expected_entries: list[dict[str, Any]] | None,
    *,
    owner: WorkspaceRegistryOwnership | None = None,
    fields: tuple[str, ...] | None = None,
) -> bool:
    """Restore only transaction-owned rows after validating ALL expectations.

    Every named expected row must still be present and match the selected
    fields before any registry byte is written. A mismatch returns False with
    no partial restoration, leaving concurrent edits untouched.
    """
    if expected_entries is None:
        return False
    prior_by_name = {
        str(row.get("name")): row
        for row in (prior_entries or [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    expected_by_name = {
        str(row.get("name")): row
        for row in expected_entries
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    own_owner = owner is None
    if owner is None:
        owner = acquire_registry_at(runtime_root, blocking=True)
    try:
        if owner.current is None:
            return False
        try:
            current = json.loads(owner.current)
        except (ValueError, TypeError, UnicodeDecodeError):
            return False
        if not isinstance(current, list):
            return False
        by_name = {
            str(row.get("name")): row
            for row in current
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        for name, expected_row in expected_by_name.items():
            row = by_name.get(name)
            if row is None:
                return False
            if fields is None:
                if row != expected_row:
                    return False
            elif not all(row.get(key) == expected_row.get(key) for key in fields):
                return False
        rewritten: list[dict[str, Any]] = []
        changed = False
        for row in current:
            name = str(row.get("name")) if isinstance(row, dict) else ""
            if name and name in expected_by_name:
                prior = prior_by_name.get(name)
                if prior is None:
                    changed = True
                    continue
                if fields is None:
                    restored = json.loads(json.dumps(prior))
                else:
                    restored = json.loads(json.dumps(row))
                    for key in fields:
                        if key in prior:
                            restored[key] = prior[key]
                rewritten.append(restored)
                changed = changed or restored != row
            else:
                rewritten.append(row)
        if changed:
            payload = (json.dumps(rewritten, indent=2) + "\n").encode("utf-8")
            owner.replace(payload, expected=owner.current)
    finally:
        if own_owner:
            owner.close()
    return True

def replace_workspace_registry(
    config: Any,
    payload: bytes | None,
    *,
    expected: bytes | None,
    owner: WorkspaceRegistryOwnership,
) -> None:
    """Exact-bytes CAS under explicit claim ownership."""
    if owner.closed or owner.key != str(Path(config.state_path).resolve().parent):
        raise WorkspaceRegistryConflict("workspace registry ownership is invalid")
    owner.replace(payload, expected=expected)
    if hasattr(config, "_workspace_registry_bytes"):
        config._workspace_registry_bytes = payload


def persist_workspaces(
    config: Any,
    candidate_workspaces: dict[str, Any] | None = None,
    *,
    owner: WorkspaceRegistryOwnership | None = None,
) -> None:
    persist = getattr(config, "persist_workspace_registry", None)
    if candidate_workspaces is None and callable(persist):
        if owner is None:
            persist()
        else:
            persist(owner=owner)
        return
    path = _workspaces_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
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
                {"name": link.name, "path": link.path, "identity": link.identity, "lifetime_id": link.lifetime_id}
                for link in workspace.shared_brains
            ]
    content = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    own_owner = owner is None
    if owner is None:
        owner = acquire_registry_at(path.parent, blocking=True)
    try:
        replace_workspace_registry(
            config,
            content,
            expected=getattr(config, "_workspace_registry_bytes", None),
            owner=owner,
        )
    finally:
        if own_owner:
            owner.close()
