"""Configuration loading for the Ciaobot server."""

from __future__ import annotations

import logging
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# The one direction the dependency runs: `entity_types` imports no `ciao` module,
# so the stock workspace-evidence set below can be derived from it (see
# `_WORKSPACE_EVIDENCE_DIRS`) without a cycle.
from ciao.entity_types import stock_entity_type_registry
from ciao.claude_trim import (
    absorb_legacy_claude_ai_connectors,
    claude_settings_layer,
    load_claude_trim,
    trim_tool_denies,
)
from ciao.models import THINKING_LEVELS, BridgeMode
from ciao.os_support.paths import resolve_path
from ciao.opencode_cli import OpencodeSettings

if TYPE_CHECKING:
    from ciao.provider_registry import ProviderDescriptor


# The self-hosted n8n MCP used to be denied by default in a workspace literally
# named ``personal``. Ciaobot no longer ships an opinion about it: n8n is
# project-scoped in ``.mcp.json``, so it exists only where someone configured it
# deliberately, and *which* workspaces should see it is a per-workspace
# preference — which is exactly what the "Extra disallowed tools" field is for.
#
# Keying it on a name meant any other private workspace went unprotected, and
# universalising the deny instead gave users a documented escape hatch that did
# not work: clearing the field sends null, which restores the defaults, and the
# value that does clear them drops the harness denies too.


_REROOTED_CACHE: dict[str, bool] = {}

# Runtime-root file recording that the retired ``CIAO_WORKSPACES`` variable
# was imported into ``workspaces.json``. Its presence makes the import one-shot.
LEGACY_WORKSPACES_IMPORT_MARKER = "workspaces-env-imported.json"

# Keys ``from_env`` has injected into ``os.environ`` from a workspace ``.env``.
#
# The export itself is load-bearing and must stay: `.mcp.json` carries
# credentials as `${NAME}` placeholders that the provider CLI resolves from the
# environment it inherits from this process, so a Notion/n8n/Airtable server
# gets its token only because the workspace `.env` reached `os.environ`. What
# was missing is a way to put the process back: `load_dotenv` sets keys it has
# never seen and nothing restores them, so a call that every caller treats as a
# read permanently changed its caller's environment.
_EXPORTED_DOTENV_KEYS: set[str] = set()

# Variables a workspace ``.env`` can no longer set. The runtime root is
# ``<workspace>/.runtime`` unless the PROCESS environment names another one (a
# service definition, test isolation); a ``CIAO_RUNTIME_ROOT`` line in ``.env``
# is dropped wherever the file is read, so no reader can disagree about where
# the runtime lives. The legacy-env-ignored tile reports a non-default one.
DOTENV_IGNORED_KEYS: frozenset[str] = frozenset({"CIAO_RUNTIME_ROOT"})


def without_ignored_dotenv_keys(values: Mapping[str, str | None]) -> dict[str, str]:
    """``values`` read from a workspace ``.env``, minus :data:`DOTENV_IGNORED_KEYS`."""
    return {
        key: value
        for key, value in values.items()
        if key and value is not None and key not in DOTENV_IGNORED_KEYS
    }


def _workspace_env(source: Any) -> str:
    """``CIAO_WORKSPACE`` as a usable path, or "" — never raw whitespace.

    Read through one helper because three separate decisions keyed off it and
    only some of them stripped. ``export CIAO_WORKSPACE=" "`` (a stray space in
    a shell profile) then skipped LaunchAgent discovery because the value was
    truthy, cleared ``bootstrap_mode`` for the same reason, and finally
    resolved ``Path("  ")`` to the CURRENT DIRECTORY — so a bare CLI run
    created ``.runtime`` and minted a session secret under whatever directory
    the operator happened to be standing in.
    """
    return str(source.get("CIAO_WORKSPACE", "") or "").strip()


def _overlay_workspace_env(
    base: Mapping[str, str],
    *,
    workspace: str,
    runtime_root: str | None = None,
) -> dict[str, str]:
    """``base`` with the workspace's ``.env`` overlaid and the workspace pinned.

    The one merge for both workspace sources (the pointer file the app owns and
    the service definition install discovery reads). The workspace's ``.env`` is
    what the running server reads; a bare-shell invocation must see the same
    values. The workspace is pinned absolutely so the file's relative
    ``CIAO_WORKSPACE=.`` cannot rebase onto the shell cwd — pinned even when
    that workspace has no ``.env``: a source naming a directory means a server
    is installed there, and falling through to a freshly manufactured bootstrap
    root beside it is the failure discovery exists to prevent. Precedence
    matches ``load_dotenv``: the caller's environment wins over the file.

    The pin is the merge's final word, not the overlay's: this is entered for an
    *empty* ``CIAO_WORKSPACE`` as well as an unset one (``export
    CIAO_WORKSPACE=`` in a shell profile), and the caller's environment wins the
    merge, so an empty string would otherwise beat the pin. The result was the
    hybrid this pinning exists to prevent — the installed workspace's auth and
    provider settings applied to a freshly manufactured bootstrap root, because
    ``bootstrap_mode`` still saw no workspace.
    """
    from dotenv import dotenv_values

    try:
        overlay: dict[str, str] = without_ignored_dotenv_keys(
            dotenv_values(Path(workspace) / ".env")
        )
    except OSError:
        overlay = {}
    merged: dict[str, str] = {**overlay, **base, "CIAO_WORKSPACE": workspace}
    if runtime_root is not None:
        merged["CIAO_RUNTIME_ROOT"] = runtime_root
    return merged


def installed_workspace_env(base: Mapping[str, str]) -> dict[str, str]:
    """``base`` with the installed server's workspace and its ``.env`` applied.

    The one answer to "which install is this shell talking to": a bare-shell
    invocation (`ciao health get` from any directory) has no CIAO_WORKSPACE, and
    the workspace the installed server's service definition points at is the
    install the operator actually has — not a fresh bootstrap workspace
    manufactured beside it. ``base`` naming a workspace already is that answer,
    and it is returned untouched.

    App mode answers differently: when this engine is the one Ciaobot for Mac
    spawned, the app-owned workspace pointer is the only source, and the
    LaunchAgent plist is never read at all. Before a takeover that plist still
    names the old engine's workspace, and a bundled engine adopting it would
    collide with the old engine's port and lock; after it, nothing names a
    workspace, so an absent or unusable pointer is bootstrap and the setup
    wizard runs.

    Read-only, like the ``export=False`` branch of :meth:`CiaoConfig.from_env`:
    the discovered ``.env`` is overlaid into a private mapping rather than loaded
    into ``os.environ``, because ``load_dotenv`` sets keys it has never seen and
    nothing restores them, so a read-only diagnostic would change its caller's
    environment. No file is created and no secret is minted.
    """
    if _workspace_env(base):
        return dict(base)
    from ciao import package_version

    if package_version.running_from_app_bundle():
        from ciao import app_engine

        pointer = app_engine.read_pointer()
        if pointer is None:
            # Nothing names a workspace in app mode: bootstrap, never the
            # LaunchAgent the takeover is removing (or has removed).
            return dict(base)
        return _overlay_workspace_env(
            base,
            workspace=str(pointer.workspace),
            runtime_root=str(pointer.runtime_root),
        )
    # The platform branch, the definition it reads and the staleness guard all
    # live in `ciao.workspace_discovery`; imported here so the seam stays the
    # one place that knows the platform and the patch target the existing tests
    # install (`ciao.macos_service.discover_runtime`) is still reached.
    from ciao.workspace_discovery import discover_workspace

    discovered = discover_workspace(base)
    if discovered is None:
        return dict(base)
    return _overlay_workspace_env(base, workspace=str(discovered))


def reset_exported_dotenv() -> None:
    """Drop every key ``from_env`` injected from a workspace ``.env``.

    Only keys this module actually added are removed — a value the process
    already had is left exactly as it was, so this can never clear a variable
    the operator exported themselves.

    Tests use it to stop one fixture's workspace bleeding into the next. It
    matters more than tidiness: a leaked RELATIVE path (`.env` used to carry
    `CIAO_RUNTIME_ROOT=.runtime`) resolves against whatever cwd the next
    caller happens to have — which is how a later CLI run wrote its outcome
    log into the repo checkout instead of its own workspace.
    """
    for key in sorted(_EXPORTED_DOTENV_KEYS):
        os.environ.pop(key, None)
    _EXPORTED_DOTENV_KEYS.clear()


def reset_reroot_cache() -> None:
    """Forget whether an install has re-rooted.

    Called by the migration after it writes its receipt, and by tests that build
    several installs in one process. Without it, the first install's answer would
    be reused for a later one that has a different runtime directory only if the
    path matched, so this is mostly a same-path correctness hook.
    """
    _REROOTED_CACHE.clear()


def agent_roots_for(workspace_root: Path, runtime_root: Path) -> list[tuple[Path, str]]:
    """Every agent root, from explicit paths. See ``CiaoConfig.agent_root_targets``.

    The standalone form, for `ciao setup`, which builds paths before any config
    exists. Reads the registry off disk rather than through ``CiaoConfig``, and
    deliberately does NOT apply the bootstrap fallback: setup runs on installs
    that have no registry yet, and manufacturing two workspace names there would
    scaffold two roots for an install that has none.
    """
    from ciao.workspace_reroot import read_receipt, registry_file  # noqa: PLC0415

    workspace_root = Path(workspace_root)
    if read_receipt(Path(runtime_root)) is None:
        return [(workspace_root, "")]
    try:
        entries = json.loads(registry_file(Path(runtime_root)).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return [(workspace_root, "")]
    names = [
        str(e.get("name", "")).strip()
        for e in entries
        if isinstance(e, dict) and str(e.get("name", "")).strip()
    ]
    if not names:
        return [(workspace_root, "")]
    return [(workspace_root / name, name) for name in sorted(names)]


def logs_root_for(workspace_root: Path, vault_root: Path, runtime_root: Path) -> Path:
    """Where the derived transcript archive lives, from explicit paths.

    The same receipt gate ``CiaoConfig.logs_root`` uses, exposed as a function so
    a CLI holding only paths cannot end up with a second copy of the rule. One
    definition of "has this install re-rooted" is the whole point of the seam.
    """
    from ciao.workspace_reroot import read_receipt  # noqa: PLC0415

    if read_receipt(Path(runtime_root)) is not None:
        return Path(workspace_root) / "Logs"
    return Path(vault_root) / "Logs"


def logs_roots_for(workspace_root: Path, vault_root: Path, runtime_root: Path) -> list[Path]:
    """Every transcript-archive Logs root, from explicit paths: each agent root's
    <root>/Logs (registry order from agent_roots_for), then the shared
    logs_root_for(...). Pre-reroot: [<vault>/Logs]. De-duplicated, order kept."""
    from ciao.workspace_reroot import read_receipt  # noqa: PLC0415

    shared = logs_root_for(workspace_root, vault_root, runtime_root)
    if read_receipt(Path(runtime_root)) is None:
        return [shared]
    roots = [root / "Logs" for root, name in agent_roots_for(workspace_root, runtime_root) if name]
    return list(dict.fromkeys([*roots, shared]))


def _clean_relative_path(raw: str) -> str:
    """Normalize a safe relative path so equivalent spellings resolve alike.

    ``research``, ``research/`` and ``./research`` all name one segment; without
    this they took different branches and two of them put the vault outside the
    vault root. The free-text "Vault name" field in Settings invites exactly
    that trailing slash.

    Absolute paths are preserved for setup-created external vaults. Relative
    paths may never contain ``..``: a workspace registry is trusted
    configuration, but resolving a malformed value outside ``workspace_root``
    would turn one bad setting into filesystem-wide reads and writes.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    # Setup uses "." deliberately when the selected notes folder is both the
    # operational workspace and the vault. It is a safe, exact location (the
    # resolved workspace root), not an empty or traversal path.
    if cleaned in {".", "./"}:
        return "."
    path = Path(cleaned)
    if path.is_absolute():
        return str(path)
    parts = [part for part in path.parts if part not in {".", ""}]
    if ".." in parts:
        raise ValueError("relative vault_root must not contain '..'")
    return Path(*parts).as_posix() if parts else ""


def _is_agent_root(path: Path) -> bool:
    """Whether a folder is a workspace's agent root, which now holds its own Logs/."""
    return (
        (path / "AGENTS.md").exists()
        or (path / "CLAUDE.md").exists()
        or (path / ".claude").is_dir()
    )


def _has_logs(path: Path) -> bool:
    """A Logs/ folder counts as vault evidence only outside an agent root."""
    return (path / "Logs").is_dir() and not _is_agent_root(path)


def _looks_like_vault(path: Path) -> bool:
    """Whether a directory is plausibly a workspace vault, not a stray folder."""
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    return any(
        (
            (path / "MEMORY.md").is_file(),
            (path / "INDEX.md").is_file(),
            (path / "Workspace").is_dir(),
            (path / "projects").is_dir(),
            _has_logs(path),
        )
    )


def _vault_evidence_score(path: Path) -> int:
    """Rank vault evidence when both legacy and standard locations exist."""
    if not _looks_like_vault(path):
        return 0
    score = 0
    score += 8 if (path / "MEMORY.md").is_file() else 0
    score += 4 if (path / "INDEX.md").is_file() else 0
    score += 4 if (path / "projects").is_dir() else 0
    score += 2 if _has_logs(path) else 0
    score += 1 if (path / "Workspace").is_dir() else 0
    return score


# Accent presets for the PWA. Missing/unknown values resolve to amber (Ciao
# orange; the old pink default read as an error). Only accents shift; canvas
# tokens stay stable.
WORKSPACE_COLOR_IDS = (
    "amber", "pink", "cyan", "emerald", "violet", "blue", "teal", "lime", "fuchsia",
)
DEFAULT_WORKSPACE_COLOR = "amber"


def coerce_workspace_color(raw: object) -> str:
    """Normalize a workspace accent id. Empty/missing → amber."""
    if raw is None:
        return DEFAULT_WORKSPACE_COLOR
    cleaned = str(raw).strip().lower()
    if not cleaned:
        return DEFAULT_WORKSPACE_COLOR
    if cleaned in WORKSPACE_COLOR_IDS:
        return cleaned
    raise ValueError(
        f"color must be one of: {', '.join(WORKSPACE_COLOR_IDS)}"
    )


# Execution (permission) modes a provider default may pin. ``manual`` is the
# PWA-facing name for the BridgeMode ``normal`` (ask for every action);
# ``plan`` stays chat-only and is not a settings default.
PERMISSION_MODES: tuple[str, ...] = ("manual", "auto", "bypass")

MEMORY_HANDOVERS: tuple[str, ...] = ("off", "dismiss", "all")
DEFAULT_MEMORY_HANDOVER = "off"
# New user-created workspaces start with predicted memory suggestions enabled.
# Keep the registry/load default above conservative so older workspaces whose
# files omit this field remain unchanged.
DEFAULT_NEW_WORKSPACE_MEMORY_HANDOVER = "all"


def coerce_memory_handover(raw: object) -> str:
    """Normalize a workspace handover. Missing/empty -> "off"; anything else unknown raises ValueError."""
    if raw is None:
        return DEFAULT_MEMORY_HANDOVER
    cleaned = str(raw).strip()
    if not cleaned:
        return DEFAULT_MEMORY_HANDOVER
    if cleaned in MEMORY_HANDOVERS:
        return cleaned
    raise ValueError(f"memory_handover must be one of: {', '.join(MEMORY_HANDOVERS)}")


# Per-kind acceptance by Workspace care. Sparse: a kind with no key inherits
# ``memory_handover``. Only acceptance is per kind; dismissal stays on the
# legacy setting. A kind is added here only with its own guarded apply path.
MEMORY_APPROVAL_KINDS: tuple[str, ...] = ("memory", "profile")
MEMORY_APPROVAL_MODES: tuple[str, ...] = ("auto", "approve")


def coerce_memory_approval(raw: object) -> dict[str, str]:
    """Validate a stored per-kind approval map. None or {} -> {}; anything unknown raises ValueError."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("memory_approval must be an object of kind to mode")
    out: dict[str, str] = {}
    for kind, mode in raw.items():
        if kind not in MEMORY_APPROVAL_KINDS:
            raise ValueError(f"memory_approval kind must be one of: {', '.join(MEMORY_APPROVAL_KINDS)}")
        if mode not in MEMORY_APPROVAL_MODES:
            raise ValueError(f"memory_approval mode must be one of: {', '.join(MEMORY_APPROVAL_MODES)}")
        out[str(kind)] = str(mode)
    return out


def merge_memory_approval(current: Mapping[str, str], patch: object) -> dict[str, str]:
    """Apply a partial save to the per-kind map. Validates the whole patch before returning.

    Each key sets that kind's mode; a ``null`` value resets that kind to inherited.
    Keys not named in the patch are kept. A whole-field ``null`` is not a patch.
    """
    if not isinstance(patch, dict):
        raise ValueError("memory_approval must be an object of kind to mode or null per kind")
    merged = dict(current)
    for kind, mode in patch.items():
        if kind not in MEMORY_APPROVAL_KINDS:
            raise ValueError(f"memory_approval kind must be one of: {', '.join(MEMORY_APPROVAL_KINDS)}")
        if mode is None:
            merged.pop(kind, None)
        elif mode in MEMORY_APPROVAL_MODES:
            merged[kind] = mode
        else:
            raise ValueError(f"memory_approval mode must be one of: {', '.join(MEMORY_APPROVAL_MODES)}")
    return merged


PROVIDER_OVERRIDE_FIELDS: tuple[str, ...] = (
    "model",
    "thinking",
    "permission_mode",
    "insights_model",
)


_FREE_TEXT_OVERRIDE_FIELDS = ("model", "insights_model")
_OVERRIDE_ID_MAX_CHARS = 200
_OVERRIDE_ID_RE = re.compile(r"[A-Za-z0-9._:/@\[\]-]+")


def _clip(value: object) -> str:
    """A repr of an untrusted key, cut short so it cannot flood an error or a log line."""
    text = repr(value)
    return text if len(text) <= 64 else text[:61] + "..."


def coerce_provider_overrides(raw: object) -> dict[str, dict[str, str]]:
    """Normalize a workspace's provider overrides. None -> {}; unknown provider/key/value raises ValueError."""
    from ciao import provider_registry

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("provider_overrides must be an object")
    known = provider_registry.provider_ids()
    for provider in raw:
        if provider not in known:
            raise ValueError(f"provider_overrides: unknown provider {_clip(provider)}")
    cleaned: dict[str, dict[str, str]] = {}
    for provider in known:
        if provider not in raw:
            continue
        entry = raw[provider]
        if not isinstance(entry, dict):
            raise ValueError(f"provider_overrides.{provider} must be an object")
        for key in entry:
            if key not in PROVIDER_OVERRIDE_FIELDS:
                raise ValueError(f"provider_overrides.{provider}: unknown key {_clip(key)}")
        fields: dict[str, str] = {}
        for key in PROVIDER_OVERRIDE_FIELDS:
            if key not in entry:
                continue
            value = entry[key]
            label = f"provider_overrides.{provider}.{key}"
            if not isinstance(value, str):
                raise ValueError(f"{label} must be a string")
            value = value.strip()
            if not value:
                continue
            if key in _FREE_TEXT_OVERRIDE_FIELDS and (
                len(value) > _OVERRIDE_ID_MAX_CHARS
                or not _OVERRIDE_ID_RE.fullmatch(value)
            ):
                # The value is never echoed: it may be hand-edited junk that
                # ends up in a log line or an error toast.
                raise ValueError(
                    f"{label} must be at most {_OVERRIDE_ID_MAX_CHARS} characters "
                    "of letters, digits and . _ : / @ [ ] -"
                )
            if key == "permission_mode" and value not in PERMISSION_MODES:
                raise ValueError(
                    f"{label} must be one of: {', '.join(PERMISSION_MODES)}"
                )
            if key == "thinking":
                levels = THINKING_LEVELS.get(provider, ())
                if value not in levels:
                    raise ValueError(f"{label} must be one of: {', '.join(levels)}")
            fields[key] = value
        if fields:
            cleaned[provider] = fields
    return cleaned


#: Local name of a shared brain: a-z, 0-9 and "-", 1 to 32 characters.
SHARED_BRAIN_NAME_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$"


@dataclass(frozen=True, slots=True)
class SharedBrainLink:
    """One linked shared brain: a Git checkout or ordinary folder a workspace reads."""

    name: str  # stable local name, unique per workspace
    path: str  # canonical absolute path of the Git checkout or ordinary folder
    identity: str  # Git origin identity or canonical folder identity, unique per workspace
    lifetime_id: str = field(default_factory=lambda: secrets.token_urlsafe(24), compare=False)
    kind: str = "git"


@dataclass(slots=True)
class WorkspaceConfig:
    """Config for one logical chat workspace."""

    name: str
    vault_root: str
    default_provider: str = "claude"
    # Stable engine lifetime for this registered workspace. A delete and
    # recreate (or an archive restore) gets a fresh one, so private consent
    # keyed by it is never inherited by a different workspace instance.
    lifetime_id: str = field(default_factory=lambda: secrets.token_urlsafe(24), compare=False)
    # True only once this lifetime was written to the workspace registry. A
    # generated fallback is not authority until the registry CAS accepts it.
    _lifetime_persisted: bool = field(default=False, repr=False, compare=False)
    # Extra tools to deny on top of the trim catalog. None, [] and 'none' all mean no extras.
    disallowed_tools: list[str] | None = None
    # PWA accent preset id. Defaults to Ciao orange (amber).
    color: str = DEFAULT_WORKSPACE_COLOR
    # Whether Workspace care may act on its own [memory]/[profile] verdicts
    # (off, dismiss, all). Operator-only: never set from an agent surface.
    memory_handover: str = DEFAULT_MEMORY_HANDOVER
    # Operator-only: the workspace's shared brains (empty = none), in the order the operator gave.
    shared_brains: list[SharedBrainLink] = field(default_factory=list)
    # Sparse per-kind acceptance (MEMORY_APPROVAL_KINDS -> MEMORY_APPROVAL_MODES).
    # A kind with no key inherits memory_handover. Operator-only, like the above.
    memory_approval: dict[str, str] = field(default_factory=dict)
    # Operator-only per-provider overrides of the Providers defaults for new
    # chats and schedules in this workspace. Empty/missing = engine default.
    # Never set from an agent surface.
    provider_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    # Sparse per-workspace Claude context-trim overrides (ciao/claude_trim.py); only keys that differ from the catalog default.
    claude_trim: dict[str, bool] = field(default_factory=dict)


def _coerce_workspace_disallowed(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return _parse_disallowed_tools(raw)
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return None


def _has_control(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def shared_brains_from_rows(raw: object) -> list[SharedBrainLink]:
    """Shared-brain rows from a registry or archive, with bad rows dropped.

    A hand-edited file must not smuggle in a brain the setting could never have
    saved: a row needs a valid name, an absolute path with no control characters
    and a non-empty identity, and no two rows may share a name, path or
    identity. Overlap with the install or a vault is checked where the config is
    built (``CiaoConfig``), because only it knows those roots.
    """
    if not isinstance(raw, list):
        return []
    log = logging.getLogger(__name__)
    links: list[SharedBrainLink] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        name, path, identity = row.get("name"), row.get("path"), row.get("identity")
        kind = row.get("kind", "git")
        lifetime_id = row.get("lifetime_id")
        if not (isinstance(name, str) and isinstance(path, str) and isinstance(identity, str)):
            continue
        sound = (
            re.fullmatch(SHARED_BRAIN_NAME_PATTERN, name) is not None
            and os.path.isabs(path)
            and not _has_control(path)
            and not _has_control(identity)
            and bool(identity.strip())
            and kind in ("git", "folder")
        )
        clash = {f"name:{name}", f"path:{path}", f"id:{identity}"} & seen
        if not sound or clash:
            log.warning("Dropping an invalid shared brain row %r", name)
            continue
        seen |= {f"name:{name}", f"path:{path}", f"id:{identity}"}
        links.append(SharedBrainLink(
            name=name, path=path, identity=identity,
            lifetime_id=lifetime_id if isinstance(lifetime_id, str) and lifetime_id else secrets.token_urlsafe(24),
            kind=kind,
        ))
    return links


def _workspace_from_mapping(data: dict) -> WorkspaceConfig | None:
    name = str(data.get("name", "")).strip()
    if not name:
        return None
    vault_root = str(data.get("vault_root", name)).strip() or name
    try:
        color = coerce_workspace_color(data.get("color"))
    except ValueError:
        color = DEFAULT_WORKSPACE_COLOR
    try:
        memory_handover = coerce_memory_handover(data.get("memory_handover"))
    except ValueError:
        memory_handover = DEFAULT_MEMORY_HANDOVER
    try:
        memory_approval = coerce_memory_approval(data.get("memory_approval"))
    except ValueError as exc:
        logging.getLogger(__name__).warning(
            "Ignoring invalid memory_approval for workspace %r: %s", name, exc
        )
        memory_approval = {}
    try:
        provider_overrides = coerce_provider_overrides(data.get("provider_overrides"))
    except ValueError as exc:
        logging.getLogger(__name__).warning(
            "Ignoring invalid provider_overrides for workspace %r: %s", name, exc
        )
        provider_overrides = {}
    raw_lifetime = data.get("lifetime_id")
    persisted_lifetime = isinstance(raw_lifetime, str) and bool(raw_lifetime)
    lifetime_id: str = str(raw_lifetime) if persisted_lifetime else secrets.token_urlsafe(24)
    return WorkspaceConfig(
        name=name,
        vault_root=vault_root,
        lifetime_id=lifetime_id,
        _lifetime_persisted=persisted_lifetime,
        default_provider=str(data.get("default_provider", "claude")).strip() or "claude",
        disallowed_tools=_coerce_workspace_disallowed(data.get("disallowed_tools")),
        claude_trim=absorb_legacy_claude_ai_connectors(
            data, load_claude_trim(data.get("claude_trim"))
        ),
        color=color,
        memory_handover=memory_handover,
        shared_brains=shared_brains_from_rows(data.get("shared_brains")),
        memory_approval=memory_approval,
        provider_overrides=provider_overrides,
    )


def _parse_workspaces_json(raw: str) -> dict[str, WorkspaceConfig]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logging.getLogger(__name__).warning("Workspace registry is not valid JSON")
        return {}
    items: list[dict]
    if isinstance(parsed, dict):
        items = [
            {"name": name, **value}
            for name, value in parsed.items()
            if isinstance(value, dict)
        ]
    elif isinstance(parsed, list):
        items = [item for item in parsed if isinstance(item, dict)]
    else:
        return {}
    out: dict[str, WorkspaceConfig] = {}
    for item in items:
        workspace = _workspace_from_mapping(item)
        if workspace is not None:
            out[workspace.name] = workspace
    return out


# A vault directory is a workspace when it CONTAINS one of these, not when it is
# one. `memory-vault/personal/People/` makes `personal` a workspace;
# `memory-vault/People/` is a note folder in a single-workspace vault and must
# not become a workspace called "People". The nesting is what separates them.
#
# The entity categories' own folders come from the shipped registry
# (`entity_types.stock_entity_type_registry()`), so a category is added here the
# same way it is added to the indexer and the linter. The three extras are the
# names a containment test needs that a category's single folder cannot express:
# the agent's own `Workspace/`, the `journal/` folder, and the lower-case
# `projects` (the historical name of `Projects/`). A containment test is not a
# type mapping, which is also why the rest of the folders are absent —
# `Documents/` is a category folder that is deliberately not evidence of a
# workspace, and no entry could say otherwise.
#
# Stock-ONLY, and by construction: this runs in `CiaoConfig.__post_init__` while
# the workspace registry is still being bootstrapped, so the
# `<vault>/entity-types.yaml` that could answer a better question is itself
# inside a directory this scan has not found yet. Reading one here would be the
# chicken-and-egg the bootstrap exists to avoid, so an install that configured
# nothing gets exactly the set it always got, and one that did is discovered by
# the registry it already has rather than by this.
_WORKSPACE_EVIDENCE_EXTRA_DIRS: frozenset[str] = frozenset({"Workspace", "journal", "projects"})

_WORKSPACE_EVIDENCE_DIRS: frozenset[str] = (
    frozenset(stock_entity_type_registry().entity_folders()) | _WORKSPACE_EVIDENCE_EXTRA_DIRS
)


def _looks_like_workspace_dir(path: Path) -> bool:
    """Whether ``path`` is a workspace folder inside a shared vault."""
    if not path.is_dir() or path.name.startswith("."):
        return False
    if (path / "MEMORY.md").is_file():
        return True
    try:
        return any(
            child.is_dir() and child.name in _WORKSPACE_EVIDENCE_DIRS
            for child in path.iterdir()
        )
    except OSError:
        return False


def _bootstrap_registry(vault_root: Path | None = None) -> dict[str, WorkspaceConfig]:
    """The registry an install gets before it has one, read off the vault.

    This is the bootstrap default, not a fallback — nothing else seeds a registry
    for an install that skipped ``ciao setup``, so returning nothing would yield
    zero workspaces. It used to manufacture BOTH ``personal`` and ``work``
    unconditionally, which was harmless while every workspace shared one vault
    directory and is not harmless now:

    - The re-rooting plan refuses when a registered workspace has no vault
      directory, so a phantom ``work`` entry left such an install permanently
      unable to migrate while the blocking gate kept telling it to.
    - Hardcoding ONE instead, as the work order proposed, moves the same problem:
      an install whose vault really does hold ``personal/`` and ``work/`` would
      have ``work`` unregistered, and the plan refuses on an unregistered vault
      directory. Also stuck, from the other side.

    Neither guess is right because the answer is on disk, so read it: one
    workspace per vault directory that looks like one, and ``personal`` when none
    do (a fresh or single-workspace vault). The evidence test is deliberately
    nested — see ``_WORKSPACE_EVIDENCE_DIRS`` — so a vault whose notes sit
    directly under ``People/`` yields one workspace, not a workspace per folder.

    The four ``*_PERSONAL`` / ``*_WORK`` environment variables that fed the
    two-entry shape (default model and extra disallowed tools, one pair each)
    went with it: they could only ever describe those two hardcoded names, and an
    install that wants per-workspace settings puts them in ``workspaces.json``,
    which is the supported path and works for any name.
    """
    names: list[str] = []
    if vault_root is not None and Path(vault_root).is_dir():
        names = sorted(
            child.name
            for child in Path(vault_root).iterdir()
            if _looks_like_workspace_dir(child)
        )
    if not names:
        names = ["personal"]
    return {
        name: WorkspaceConfig(
            name=name,
            vault_root=name,
            default_provider="claude",
        )
        for name in names
    }


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_disallowed_tools(raw: str) -> list[str] | None:
    """Parse a CSV denylist. Empty/missing → None, ``"none"`` → ``[]`` — both mean no extras since trims moved to claude_trim.

    The None vs []-empty distinction is kept for callers, but the harness
    trims now live in ``claude_trim``, so neither value drops a default deny.
    """
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.lower() == "none":
        return []
    return _split_csv(cleaned)


def _bootstrap_workspace(source: Mapping[str, str]) -> Path:
    raw = source.get("CIAO_BOOTSTRAP_WORKSPACE", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    home = Path(source.get("HOME", str(Path.home()))).expanduser()
    return (home / ".ciao" / "bootstrap").resolve()


def _read_or_create_secret(path: Path) -> str:
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8", newline="")
    return token


# The exit code that asks `scripts/run-ciao.sh` (and `ciao run`) to restart in
# place. Fixed: the shell loop hardcodes the same number.
RESTART_EXIT_CODE = 75

# The workspace name used when a chat or project names none.
DEFAULT_WORKSPACE_NAME = "personal"

# Upload size caps for chat attachments.
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024

# Anthropic model aliases offered in the picker; the first is the default.
CLAUDE_MODELS = ("opus", "sonnet", "haiku", "fable")


@dataclass(slots=True)
class CiaoConfig:
    """Environment-backed configuration."""

    pwa_auth_token: str
    workspace_root: Path
    state_path: Path
    media_root: Path
    # Developer controls and the source checkout for developer-only
    # deploy/restart workflows. Both are Settings (`app_settings.json`), read at
    # startup and overlaid live by `AppSettingsStore.apply_to_config`.
    dev_mode: bool = False
    app_repo: Path | None = None
    bootstrap_mode: bool = False
    vault_root: Path = Path("memory-vault")
    claude_default_model: str = CLAUDE_MODELS[0]
    # Per-workspace default models and tool denylists live on the WorkspaceConfig
    # in this registry, set through `workspaces.json`. The former top-level
    # `*_personal` / `*_work` pairs are gone: they existed only to furnish the
    # two-entry bootstrap registry, and that now returns one workspace.
    workspaces: dict[str, WorkspaceConfig] = field(default_factory=dict)
    _workspace_registry_changed: bool = field(
        init=False, default=False, repr=False
    )
    _workspace_registry_bytes: bytes | None = field(default=None, repr=False, compare=False)
    # Raw value of the retired ``CIAO_WORKSPACES`` variable, if an install
    # still sets it. Never used as a workspace source; server startup imports
    # it into ``.runtime/workspaces.json`` once and then ignores it.
    legacy_workspaces_env: str = field(default="", repr=False)
    # Raw values of the retired `.env` server variables
    # (`app_settings.LEGACY_ENV_SETTINGS`) this install still sets. Never read
    # as settings; server startup imports them into Settings once.
    legacy_env_settings: dict[str, str] = field(default_factory=dict, repr=False)
    claude_mode: BridgeMode = "auto"
    # Per-provider default execution (permission) mode for new chats, set from
    # the PWA Settings → Providers tab (runtime settings store). A missing
    # entry uses ``claude_mode`` for every provider.
    provider_default_modes: dict[str, str] = field(default_factory=dict)
    # Per-provider default model for new chats, set from the PWA Settings →
    # Models tab (runtime settings store). A missing entry uses the provider's
    # own catalog default.
    provider_default_models: dict[str, str] = field(default_factory=dict)
    # Per-provider default thinking level for new chats, set from the PWA
    # Settings → Providers tab. A missing entry uses the provider's own default.
    provider_default_thinking: dict[str, str] = field(default_factory=dict)
    # Per-provider Session insights model (memory pass, chat titles, schedule
    # attention check), set on each provider's card in Settings → Providers. A
    # missing entry uses the source chat's model.
    provider_insights_models: dict[str, str] = field(default_factory=dict)
    pwa_port: int = 8443
    # The server binds all interfaces by default so the PWA is reachable over
    # LAN and Tailscale; the dashboard password + login rate limit are the
    # access control. Settings → Access & devices → Network access sets "127.0.0.1"
    # for loopback-only (`app_settings.DEFAULT_PWA_HOST` is this default).
    pwa_host: str = "0.0.0.0"
    # Root log level name (Settings → Advanced → Developer).
    log_level: str = "info"
    # Per-provider default model for new chats, set from the PWA Settings →
    # Models tab. Empty means the provider's own default applies.
    opencode: OpencodeSettings = field(default_factory=OpencodeSettings)
    # Claude model for the review-queue folds that run without a chat behind
    # them. Chat-driven calls use :func:`ciao.insights.resolve_insights_model`.
    insights_model: str = "sonnet"

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).expanduser().resolve()
        self.state_path = Path(self.state_path).expanduser().resolve()
        self.media_root = Path(self.media_root).expanduser().resolve()
        vault_root = Path(self.vault_root).expanduser()
        if not vault_root.is_absolute():
            vault_root = self.workspace_root / vault_root
        self.vault_root = vault_root.resolve()
        if not self.workspaces:
            self.workspaces = _bootstrap_registry(self.vault_root)
        self._workspace_registry_changed = self._normalize_workspace_vault_roots()
        self._note_unsafe_shared_brains()

    def _note_unsafe_shared_brains(self) -> None:
        """Log shared brains that point into the install, a vault or the runtime dir.

        They are kept, not dropped: ``brain_state`` reports them as
        ``problem: outside_install`` and the update loop skips them, so the
        operator sees them and can remove them in Settings.
        """
        from ciao.shared_brain import overlaps_protected  # noqa: PLC0415

        for workspace in self.workspaces.values():
            for link in getattr(workspace, "shared_brains", None) or []:
                if overlaps_protected(self, link.path):
                    logging.getLogger(__name__).warning(
                        "Shared brain %r of workspace %r points into the install or a vault; "
                        "it is kept and reported, not updated",
                        link.name,
                        workspace.name,
                    )

    def workspace(self, name: str | None) -> WorkspaceConfig | None:
        if not name:
            return None
        return self.workspaces.get(name)

    def workspace_names(self) -> list[str]:
        return list(self.workspaces.keys())

    def primary_workspace(self) -> str:
        """The workspace to use when a caller has no better idea.

        Prefers one literally named ``personal`` for continuity with installs
        that predate a configurable registry, then falls back to whatever is
        registered first. Callers must not hardcode ``"personal"`` themselves:
        workspace names are the user's, and an install may have none by that
        name at all.
        """
        if "personal" in self.workspaces:
            return "personal"
        names = self.workspace_names()
        return names[0] if names else ""

    def workspace_vault_root(self, workspace: str | None) -> Path:
        """Absolute vault directory for one logical workspace.

        Pure: the answer depends only on the registry, never on what happens to
        exist on disk right now. An earlier version chose between two candidate
        paths by probing the filesystem on every call, which meant an install's
        vault silently relocated the moment the other candidate appeared —
        stranding everything written at the first one. Legacy layouts are
        reconciled once, at load (see ``_normalize_workspace_vault_roots``).

        Registered ``vault_root`` shapes:

        - absolute — preserved setup/external roots and pinned legacy vaults.
        - ``.`` — existing-folder setup where the operational workspace itself
          is the vault.
        - one path segment — a legacy ambiguous value, normalized and persisted
          at load.
        - several segments (normally ``memory-vault/clientA``) — the standard
          named workspace path, resolved against ``workspace_root`` so it is not
          nested twice.
        """
        name = workspace or ""
        workspace_config = self.workspace(name)
        raw_root = (workspace_config.vault_root if workspace_config else name) or name
        return self._resolve_vault_root(raw_root)

    def canonical_workspace_vault_root(self, workspace: str) -> Path:
        """Standard location for a user-named workspace's notes, in the layout
        this install is actually in.

        Two layouts, one question. Shared: a folder per workspace under the one
        vault (``memory-vault/<name>``). Per-root, after the re-rooting: the
        whole vault of that workspace's agent root — which is what
        :meth:`agent_vault_root` already derives from the same receipt.

        Answering "shared" unconditionally made this a claim about the past.
        The vault-location check compares the resolved vault against this and
        raised "The personal vault is not in its standard folder" on a correctly
        migrated install, for every workspace, permanently — with a chat prompt
        telling the operator to move the vault back to where the migration had
        just moved it from. A standard location that disagrees with the layout
        is worse than no check at all.
        """
        name = _clean_relative_path(workspace)
        if not name or len(Path(name).parts) != 1:
            raise ValueError("workspace name must identify one vault folder")
        if self.agent_root(name) != self.workspace_root:
            # Derived directly, not through agent_vault_root: that resolver
            # follows the registry, and a registered nonstandard root must not
            # become the canonical destination merely by being registered.
            return self.agent_root(name) / self.vault_root.name
        candidate = self.vault_root / name
        if candidate.is_symlink():
            raise ValueError("workspace vault folder must not be a symlink")
        resolved = candidate.resolve()
        if resolved.parent != self.vault_root:
            raise ValueError("workspace vault folder must stay inside the vault root")
        if resolved.exists() and not resolved.is_dir():
            raise ValueError("workspace vault folder must be a directory")
        return resolved

    def stored_workspace_vault_root(self, workspace: str) -> str:
        """Portable registry value for a workspace's standard vault folder."""
        root = self.canonical_workspace_vault_root(workspace)
        try:
            return root.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return str(root)

    def agent_root(self, name: str) -> Path:
        """Per-workspace directory that will hold that workspace's agent assets.

        The future home of a workspace's own ``CLAUDE.md``, ``.mcp.json``,
        ``.claude/`` assets, and ``memory-vault/``. The derivation is
        ``workspace_root / name``, but the per-workspace subdirectory only
        exists after the re-rooting release, so every workspace still resolves
        to ``workspace_root`` itself for now. A later phase flips the return to
        the derived path; this method is the single seam for that change.
        """
        name = _clean_relative_path(name)
        if not name or len(Path(name).parts) != 1 or any(
            sep in name for sep in ("/", "\\")
        ):
            raise ValueError("workspace name must identify one folder")
        # Flipped per install by the re-rooting migration, not by a release date.
        # Gating on the migration's own receipt is what makes the change atomic:
        # an install that has not re-rooted keeps resolving to workspace_root, so
        # a half-flipped state cannot exist. Deleting the legacy filters before
        # this returns a real subdirectory would leave no filter over a still
        # prefixed index, which is fail-open and strictly worse than today.
        if self._rerooted():
            return self.workspace_root / name
        return self.workspace_root

    def agent_vault_root(self, name: str) -> Path:
        """The vault whose ``INDEX.md`` this workspace's agent root owns.

        Distinct from :meth:`workspace_vault_root`, and the difference matters:

        ``workspace_vault_root`` is where a workspace's NOTES live. Before the
        re-rooting that is a subtree of one shared vault
        (``memory-vault/<name>``); after it, the whole vault of that root.

        This is where the aggregate FILES about those notes live — ``INDEX.md``
        and ``VOCABULARY.md``. Before the re-rooting there is exactly one such
        pair for the whole install, so every workspace resolves to the shared
        vault root; after it, each root owns its own pair. Derived from
        :meth:`agent_root`, so it flips on the same receipt and cannot disagree
        with it.

        Written as ``agent_root / <vault dir name>`` because that is literally
        what the migration produces: ``workspace_reroot.plan`` moves each vault
        to ``<name>/<vault dir name>``. A registered per-root workspace is the
        exception: its registered root is its vault (notes ownership), so an
        adopted folder such as ``work/brain`` or ``.`` owns its own aggregates.
        """
        if self.adopted_workspace_vault(name):
            return self.workspace_vault_root(name)
        return self.agent_root(name) / self.vault_root.name

    def adopted_workspace_vault(self, name: str) -> bool:
        """Whether a re-rooted workspace's registered vault is an explicit adoption.

        The registry is the evidence: a registered root that is neither the
        per-root standard (``<agent root>/<vault folder name>``) nor the
        superseded pre-re-rooting shared subfolder (``<vault root>/<name>``)
        was chosen by setup or by the operator, such as ``work/brain``, a
        nested clone vault, ``.`` or an absolute external folder. Ownership
        (:meth:`agent_vault_root`) and the relocation offer
        (``migration_notices.relocation_state``) both read this, so the
        folder a workspace owns is never the folder it is told to leave.
        Always ``False`` before the re-rooting and for an unregistered name.
        """
        if not self._rerooted() or self.workspace(name) is None:
            return False
        registered = self.workspace_vault_root(name)
        return registered not in (
            self.vault_root / name,
            self.agent_root(name) / self.vault_root.name,
        )

    @property
    def logs_root(self) -> Path:
        """The shared logs root. See :func:`logs_root_for`.

        Before the re-rooting it is the only archive, ``<vault>/Logs``. After it,
        ``<install>/Logs`` holds what predates per-workspace archives and could not
        be attributed to a workspace: loose files, ``CLI/``, ``Telegram/``,
        ``Meetings/``, and chat ids not in the registry. New chat archives go to
        :meth:`chats_root` for their workspace. Readers scan :meth:`logs_roots`.

        Receipt-gated through the same ``_rerooted`` check as
        :meth:`agent_root`, so the two can never disagree about which layout the
        install is on.
        """
        return (
            self.workspace_root / "Logs" if self._rerooted() else self.vault_root / "Logs"
        )

    def chats_root(self, workspace: str) -> Path:
        """Where a chat of *workspace* archives: ``<agent_root(workspace)>/Logs/Chats``
        after the re-rooting; ``self.logs_root / "Chats"`` before it, and for
        workspace ``""`` (a chat with no workspace has no agent root).

        ``agent_root``'s ``ValueError`` propagates for a malformed name. Callers pass
        registered names only.
        """
        if not workspace or not self._rerooted():
            return self.logs_root / "Chats"
        return self.agent_root(workspace) / "Logs" / "Chats"

    def logs_roots(self) -> list[Path]:
        """Every archive Logs root a reader must scan: each agent root's ``Logs``
        after the re-rooting, then the shared ``logs_root``. Before the re-rooting,
        ``[logs_root]``. De-duplicated, order kept.
        """
        if not self._rerooted():
            return [self.logs_root]
        roots = [root / "Logs" for root, _ in self.agent_root_targets()]
        return list(dict.fromkeys([*roots, self.logs_root]))

    def chats_roots(self) -> list[Path]:
        """``root / "Chats"`` for every root in :meth:`logs_roots`."""
        return [root / "Chats" for root in self.logs_roots()]

    def agent_root_targets(self) -> list[tuple[Path, str]]:
        """Every agent root in this install, as ``(root, workspace name)``.

        One target before the re-rooting — the install root itself, unnamed,
        because that is where the single set of provider assets lives. One per
        workspace afterwards, because each root then owns its own ``AGENTS.md``,
        ``.claude/``, ``skills/`` and mirrors. The install root is intentionally
        absent after that transition: ``workspace_reroot`` moves the shared
        guide into the primary root, and a root-level guide is no longer an
        agent asset to rename or sync.

        The seam for anything inspecting or listing agent assets. Reading
        ``workspace_root`` directly still finds the install root's stale
        ``.claude/`` after the migration, whose links point at a catalog that
        moved — which is why the health panel reported every custom skill as
        broken on a correctly migrated install.
        """
        if not self._rerooted():
            return [(self.workspace_root, "")]
        return [
            (self.agent_root(name), name) for name in self.workspace_names() if name
        ]

    def vault_scan_targets(self) -> list[tuple[Path, str, Path]]:
        """Every vault in this install, as ``(root, workspace, path prefix)``.

        One target before the re-rooting — the shared vault, unstamped, so the
        first-path-segment inference labels the workspaces exactly as it does
        today. One target PER ROOT afterwards, each stamped with its workspace
        and rendering paths under ``<name>/memory-vault`` so two roots holding a
        note of the same name do not render the same path.

        This is the seam for anything that means "all the notes in this install":
        reading ``vault_root`` directly gives a directory that does not exist
        after the migration, which is why the Memory Map came back empty.
        """
        if not self._rerooted():
            return [(self.vault_root, "", Path("memory-vault"))]
        targets: list[tuple[Path, str, Path]] = []
        for name in self.workspace_names():
            if not name:
                continue
            root = self.agent_vault_root(name)
            targets.append((root, name, Path(name) / root.name))
        return targets

    def _rerooted(self) -> bool:
        """Whether this install has completed the per-workspace re-rooting.

        Cached in a module-level map keyed by runtime directory, because
        ``CiaoConfig`` uses slots and ``agent_root`` is called on hot paths: a
        stat per call would be wasteful for a value that changes exactly once in
        an install's life. ``reset_reroot_cache`` clears it, which the migration
        itself calls after writing its receipt.
        """
        key = str(self.state_path.parent)
        cached = _REROOTED_CACHE.get(key)
        if cached is None:
            from ciao.workspace_reroot import read_receipt

            cached = read_receipt(self.state_path.parent) is not None
            _REROOTED_CACHE[key] = cached
        return cached

    def _resolve_vault_root(self, raw_root: str) -> Path:
        cleaned = _clean_relative_path(raw_root)
        if not cleaned:
            raise ValueError("vault_root must not be empty")
        root = Path(cleaned).expanduser()
        if root.is_absolute():
            resolved = resolve_path(root)
            if resolved == Path(resolved.anchor):
                raise ValueError("vault_root must not be the filesystem root")
            # Absolute compatibility roots are canonicalized when the registry
            # loads. If that pinned path (or one of its descendants below the
            # already-resolved workspace root) later becomes a symlink, do not
            # silently redirect agent reads and writes to a different vault.
            if resolved != root:
                raise ValueError("workspace vault path must not contain symlinks")
            return resolved
        if len(root.parts) == 1:
            candidate = self.vault_root / root
            if candidate.is_symlink():
                raise ValueError("workspace vault folder must not be a symlink")
            resolved = resolve_path(candidate)
            if resolved.parent != self.vault_root:
                raise ValueError(
                    "workspace vault folder must stay inside the vault root"
                )
            return resolved
        candidate = self.workspace_root / root
        resolved = resolve_path(candidate)
        if resolved == Path(resolved.anchor):
            raise ValueError("vault_root must not be the filesystem root")
        if resolved != candidate:
            raise ValueError("workspace vault path must not contain symlinks")
        if not resolved.is_relative_to(self.workspace_root):
            raise ValueError("relative vault_root must stay inside workspace_root")
        return resolved

    def _normalize_workspace_vault_roots(self) -> bool:
        """Pin a legacy vault's location into the registry, once.

        Before vault nesting applied to every workspace, a workspace named
        anything but ``personal``/``work`` kept its vault beside ``memory-vault/``
        rather than inside it. Such an install must not appear to lose its data,
        but it also must not have its location re-decided on every call. So the
        one-segment ``vault_root`` is rewritten to the absolute legacy path here:
        resolution downstream stays pure, and the change is visible in
        ``.runtime/workspaces.json`` the next time it is written.

        The legacy directory has to actually look like a vault. Gating on mere
        existence would capture an unrelated sibling — naming a workspace after
        a ``clients/`` or ``skills/`` folder that already sits in the workspace
        root would silently adopt it, and the agent would then write memory into
        someone's document folder.
        """
        changed = False
        for name, workspace_config in list(self.workspaces.items()):
            try:
                raw_root = _clean_relative_path(workspace_config.vault_root or name)
                if not raw_root:
                    raise ValueError("vault_root must not be empty")
                canonical = self.canonical_workspace_vault_root(name)
            except ValueError:
                logger.warning(
                    "Workspace %s has an unsafe vault_root %r; using its "
                    "standard folder under %s",
                    name,
                    workspace_config.vault_root,
                    self.vault_root,
                )
                try:
                    canonical = self.canonical_workspace_vault_root(name)
                except ValueError:
                    continue
                workspace_config.vault_root = self.stored_workspace_vault_root(name)
                changed = True
                continue
            root = Path(raw_root).expanduser()
            if root.is_absolute():
                absolute = root.resolve()
                if absolute == Path(absolute.anchor):
                    workspace_config.vault_root = (
                        self.stored_workspace_vault_root(name)
                    )
                    changed = True
                    continue
                # Preserve a setup-selected symlink by pinning its current
                # target. Resolution can then fail closed if the pinned path
                # itself is replaced by a symlink after startup/restart.
                if workspace_config.vault_root != str(absolute):
                    workspace_config.vault_root = str(absolute)
                    changed = True
                # Backward compatibility for an older/manual sibling move:
                # when the pinned source disappeared and the standard folder
                # now has vault evidence, follow the completed move. The
                # current guided migration updates the registry explicitly.
                if (
                    absolute != self.vault_root
                    and absolute.parent == self.workspace_root
                    and not absolute.exists()
                    and _looks_like_vault(canonical)
                ):
                    workspace_config.vault_root = self.stored_workspace_vault_root(name)
                    changed = True
                continue
            if len(root.parts) != 1:
                candidate = self.workspace_root / root
                absolute = candidate.resolve()
                if absolute == Path(absolute.anchor):
                    workspace_config.vault_root = (
                        self.stored_workspace_vault_root(name)
                    )
                    changed = True
                    continue
                # A relative setup path may run through the configured vault
                # alias (for example memory-vault -> an external notes
                # directory). Pin the alias's current target exactly once;
                # later resolver calls can then reject any replacement without
                # breaking an intentional symlink that existed at setup time.
                if absolute != candidate:
                    workspace_config.vault_root = str(absolute)
                    changed = True
                continue
            legacy = (self.workspace_root / root).resolve()
            if legacy == self.vault_root:
                # A one-segment value equal to CIAO_VAULT_ROOT is the
                # setup-era "this workspace owns the whole vault" shape. Pin
                # it absolutely; leaving it as ``memory-vault`` would make the
                # generic one-segment resolver nest it a second time.
                stored = str(legacy)
                if workspace_config.vault_root != stored:
                    workspace_config.vault_root = stored
                    changed = True
                continue
            if legacy == canonical:
                continue
            if (
                _looks_like_vault(legacy)
                and _vault_evidence_score(legacy)
                >= _vault_evidence_score(canonical)
            ):
                workspace_config.vault_root = str(legacy)
                changed = True
                logger.info(
                    "Workspace %s keeps its vault at the pre-nesting location "
                    "%s; pinned in the registry until an interactive migration "
                    "moves it under %s.",
                    name,
                    legacy,
                    self.vault_root,
                )
                continue

            # New workspaces and already-nested legacy workspaces get an
            # explicit registry path. That removes the old ambiguous
            # one-segment shape, so later filesystem changes cannot relocate
            # the workspace.
            stored = self.stored_workspace_vault_root(name)
            if workspace_config.vault_root != stored:
                workspace_config.vault_root = stored
                changed = True
        return changed

    def persist_workspace_registry(self, *, owner: Any | None = None) -> None:
        """Atomically persist the live registry if its loaded snapshot is current."""
        from ciao.workspaces import (
            replace_workspace_registry,
            acquire_workspace_registry,
            workspace_registry_lock,
        )  # noqa: PLC0415

        with workspace_registry_lock(self):
            payload = self._workspace_registry_payload()
            own_owner = owner is None
            if owner is None:
                owner = acquire_workspace_registry(self)
            try:
                replace_workspace_registry(
                    self, payload, expected=self._workspace_registry_bytes, owner=owner
                )
                self._workspace_registry_bytes = payload
                self._workspace_registry_changed = False
                for workspace in self.workspaces.values():
                    workspace._lifetime_persisted = True
            finally:
                if own_owner:
                    owner.close()

    def _workspace_registry_payload(self) -> bytes:
        """Serialize the in-memory registry without writing it."""
        payload: list[dict[str, Any]] = []
        for workspace in self.workspaces.values():
            row: dict[str, Any] = {
                "name": workspace.name,
                "vault_root": workspace.vault_root,
                "lifetime_id": workspace.lifetime_id,
                # Persist the effective provider, not a stale registry value
                # left behind after a provider was removed.  The route layer
                # already serializes this way; keeping the config registry
                # consistent prevents the next restart from resurrecting the
                # invalid id (#292).
                "default_provider": self.default_provider_for_workspace(
                    workspace.name
                ),
                "disallowed_tools": workspace.disallowed_tools,
                "color": workspace.color,
            }
            # Sparse: a workspace on the catalog defaults keeps no key.
            if workspace.claude_trim:
                row["claude_trim"] = workspace.claude_trim
            if workspace.memory_handover != DEFAULT_MEMORY_HANDOVER:
                row["memory_handover"] = workspace.memory_handover
            if workspace.shared_brains:
                row["shared_brains"] = [
                    {"name": link.name, "path": link.path, "identity": link.identity, "lifetime_id": link.lifetime_id, "kind": link.kind}
                    for link in workspace.shared_brains
                ]
            if workspace.memory_approval:
                row["memory_approval"] = dict(workspace.memory_approval)
            if workspace.provider_overrides:
                row["provider_overrides"] = workspace.provider_overrides
            payload.append(row)
        return (json.dumps(payload, indent=2) + "\n").encode("utf-8")

    def _archived_workspace_names(self) -> set[str]:
        """Casefolded names recorded in ``<install>/.archived-workspaces/*/archive.json``."""
        from ciao.vault_index import ARCHIVED_WORKSPACES_DIR  # noqa: PLC0415

        root = Path(self.workspace_root) / ARCHIVED_WORKSPACES_DIR
        names: set[str] = set()
        try:
            folders = list(root.iterdir()) if root.is_dir() else []
        except OSError:
            return names
        for folder in folders:
            try:
                data = json.loads((folder / "archive.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("name"):
                names.add(str(data["name"]).casefold())
        return names

    def import_legacy_workspaces_env(self, *, owner: Any = None) -> list[str]:
        """Import the retired ``CIAO_WORKSPACES`` variable into the registry, once.

        ``CIAO_WORKSPACES`` used to override ``.runtime/workspaces.json`` on
        every start, so a workspace created, edited or archived in Settings
        could silently revert on the next restart. The registry is now the only
        source. An install that still sets the variable gets its workspaces
        copied into the registry the first time a server starts on this
        release:

        - no registry file (or one with no valid entries): the variable's
          workspaces become the registry, replacing the in-memory bootstrap
          list, because the variable was this install's effective list;
        - a registry file with entries: only workspaces it does not already
          define are appended; existing entries are never overwritten.

        A marker file in the runtime root records that the import ran, so a
        later start never re-imports: a workspace removed in Settings stays
        removed even while the variable is still in ``.env``. Every start that
        still sees the variable logs a warning saying it is ignored.

        Returns the names of the imported workspaces. The caller must hold the
        workspace instance lock, since this writes the registry.
        """
        raw = self.legacy_workspaces_env.strip()
        if not raw:
            return []
        from ciao.workspaces import workspace_registry_claim, ensure_registry_current
        if owner is None:
            with workspace_registry_claim(self) as claim:
                return self.import_legacy_workspaces_env(owner=claim)
        ensure_registry_current(self)
        runtime_root = self.state_path.parent
        marker = runtime_root / LEGACY_WORKSPACES_IMPORT_MARKER
        if marker.exists():
            logger.warning(
                "CIAO_WORKSPACES is set but no longer read; workspaces are "
                "managed in Settings and stored in %s. Remove it from .env.",
                runtime_root / "workspaces.json",
            )
            return []
        legacy = _parse_workspaces_json(raw)
        # Overrides can carry a permission mode and are operator-set in
        # Settings only; an environment import never brings them in. The same
        # holds for Claude context trims.
        for imported_workspace in legacy.values():
            imported_workspace.provider_overrides = {}
            imported_workspace.claude_trim = {}
        # A workspace archived in Settings left the registry on purpose; the
        # variable must not bring it back while its folder sits in the archive.
        archived = self._archived_workspace_names()
        skipped = sorted(n for n in legacy if n.casefold() in archived)
        if skipped:
            logger.warning(
                "CIAO_WORKSPACES names archived workspace(s) %s; not re-registering "
                "them. Restore them from Settings instead.",
                ", ".join(skipped),
            )
            legacy = {n: w for n, w in legacy.items() if n.casefold() not in archived}
        on_disk = _parse_workspaces_json((self._workspace_registry_bytes or b"").decode("utf-8"))
        previous = self.workspaces
        if on_disk:
            imported = {n: w for n, w in legacy.items() if n not in self.workspaces}
            self.workspaces = {**self.workspaces, **imported}
        else:
            imported = dict(legacy)
            if imported:
                self.workspaces = dict(imported)
        if imported:
            self._normalize_workspace_vault_roots()
            try:
                self.persist_workspace_registry(owner=owner)
            except BaseException:
                self.workspaces = previous
                raise
        runtime_root.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "imported_at": datetime.now(UTC).isoformat(),
                    "imported": list(imported),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8", newline="",
        )
        logger.warning(
            "CIAO_WORKSPACES is no longer read; imported %s into %s once. "
            "Workspaces are managed in Settings now; remove CIAO_WORKSPACES "
            "from .env.",
            ", ".join(imported) if imported else "no new workspaces",
            runtime_root / "workspaces.json",
        )
        return list(imported)

    def default_model_for_workspace(
        self, workspace: str | None, provider: str | None = None
    ) -> str:
        """Pick the new-chat / new-schedule default for a workspace.

        Order: this workspace's override for the effective provider, then the
        provider's operator default (``provider_default_models``, surfaced in
        the PWA Settings → Providers tab), then the provider's own catalog
        default. The override is keyed by the effective provider, never by the
        workspace's default provider, so a chat opened on another provider
        never inherits a model that belongs to this workspace's own provider.

        ``provider``, when given, is the provider the chat/schedule will
        actually run on. It can differ from the workspace's own
        ``default_provider`` (an explicit provider override); in that case
        resolve against that provider's own operator default instead of the
        workspace's default-provider model.
        """
        from ciao import provider_registry

        workspace_config = self.workspace(workspace)
        workspace_provider = (
            workspace_config.default_provider if workspace_config else None
        )
        effective_provider = provider or workspace_provider
        if effective_provider:
            descriptor = provider_registry.get(effective_provider)
            if descriptor is not None:
                override = self.workspace_override(
                    workspace, effective_provider, "model"
                )
                if override:
                    return override
                # The operator's explicit pick comes first, whatever the
                # provider. Checking `default_model_config_key` before this
                # meant Claude — the one provider that has such a key — always
                # returned its env-backed value, so a Claude default chosen in
                # Settings → Providers was stored, echoed back by the UI, and
                # never actually used.
                operator_default = self._operator_default_model(descriptor)
                if operator_default:
                    return operator_default
                if descriptor.default_model_config_key:
                    return str(getattr(self, descriptor.default_model_config_key, "") or "")
                # No operator default and no env-backed key: "use that provider
                # account's current catalog default" — the provider resolves it
                # and the chat records the effective model.
                return descriptor.default_model
        return self.claude_default_model

    def _operator_default_model(self, descriptor: "ProviderDescriptor") -> str:
        """The operator-settable default model for a provider, or ``""`` when unset.

        ``provider_default_models`` is what Settings → Providers writes for every
        provider, so it is the authority. The per-provider settings dataclass is
        consulted only as a fallback, for providers that mirror the same value
        there (``app_settings`` copies it across for those).
        """
        selected = (self.provider_default_models or {}).get(descriptor.id, "")
        if selected:
            return str(selected)
        if not descriptor.default_model_settings_attr:
            return ""
        settings = getattr(self, descriptor.default_model_settings_attr, None)
        return getattr(settings, "default_model", "") if settings else ""

    def workspace_override(
        self, workspace: str | None, provider: str, field_name: str
    ) -> str:
        """This workspace's override of one provider default, or "" when it has none."""
        workspace_config = self.workspace(workspace)
        if workspace_config is None:
            return ""
        return (workspace_config.provider_overrides.get(provider) or {}).get(
            field_name, ""
        )

    def default_thinking_for_provider(
        self, provider: str, workspace: str | None = None
    ) -> str:
        """Default thinking level for new chats on ``provider``: workspace override, then engine."""
        return self.workspace_override(workspace, provider, "thinking") or (
            self.provider_default_thinking or {}
        ).get(provider, "")

    def insights_model_override(
        self, provider: str, workspace: str | None = None
    ) -> str:
        """Session-insights model override for ``provider``: workspace, then engine, else ""."""
        return self.workspace_override(workspace, provider, "insights_model") or (
            self.provider_insights_models or {}
        ).get(provider, "")

    def default_model_for_provider(
        self, provider: str, workspace: str | None = None
    ) -> str:
        """The default model for a provider, or ``""`` when unset.

        Used when a chat is created on a provider that is not the workspace's
        default provider, so the per-provider default still applies. A
        ``workspace`` override for that provider wins over the operator default.
        """
        from ciao import provider_registry

        override = self.workspace_override(workspace, provider, "model")
        if override:
            return override

        descriptor = provider_registry.get(provider)
        if descriptor is None:
            return ""
        return self._operator_default_model(descriptor)

    def default_provider_for_workspace(self, workspace: str | None) -> str:
        from ciao import provider_registry

        workspace_config = self.workspace(workspace)
        # An unregistered value (a stale reference to a removed backend, a
        # typo) leaves the answer at the default provider.
        if workspace_config and provider_registry.is_provider(
            workspace_config.default_provider
        ):
            return workspace_config.default_provider
        return "claude"

    def default_mode_for_provider(
        self, provider: str, workspace: str | None = None
    ) -> BridgeMode:
        """The default execution (permission) mode for new chats on ``provider``.

        A ``workspace`` override wins; then an operator pin (Settings →
        Providers → permission mode); otherwise every provider falls back to ``claude_mode`` (auto).
        ``manual`` maps to the BridgeMode ``normal`` — ask for every
        action; ``bypass`` allows everything; ``auto`` runs the permissive
        classifier-gated default.
        """
        override = self.workspace_override(workspace, provider, "permission_mode")
        if override:
            return cast(BridgeMode, "normal" if override == "manual" else override)
        mode = (self.provider_default_modes or {}).get(provider, "")
        if mode in {"normal", "plan", "auto", "bypass"}:
            return cast(BridgeMode, mode)
        return self.claude_mode

    def disallowed_tools_for_workspace(self, workspace: str | None) -> list[str]:
        """Tools to deny for a chat in this workspace.

        Denies are additive. The harness trims come from the workspace's
        ``claude_trim`` map (see ``ciao/claude_trim.py``), with the catalog
        defaults for every option it does not name. ``disallowed_tools`` adds
        extra tools on top of them, and an ``mcp__<server>`` entry there blocks
        one MCP server. MCP servers are otherwise scoped by where they are
        declared — each workspace's own agent-root ``.mcp.json`` and the
        provider's local scope — and every declared server loads.

        ``disallowed_tools`` is only applied when the chat's provider is
        ``claude`` (see the ``if chat.provider != "claude": return []`` guard in
        project_chats).

        An unregistered workspace name — a stale reference, or a renamed or
        deleted workspace — gets the catalog defaults rather than an empty
        denylist. It was the one input that reached the model with nothing denied.
        """
        workspace_config = self.workspace(workspace)
        trim = workspace_config.claude_trim if workspace_config else {}
        extras = (workspace_config.disallowed_tools or []) if workspace_config else []
        return list(dict.fromkeys([*trim_tool_denies(trim), *extras]))

    def claude_settings_for_workspace(self, workspace: str | None) -> dict[str, Any]:
        """The Claude CLI settings layer (``skillOverrides`` plus ``disable*`` keys) for this workspace.

        Built from the workspace's ``claude_trim``; an unregistered workspace gets the catalog defaults.
        """
        workspace_config = self.workspace(workspace)
        return claude_settings_layer(workspace_config.claude_trim if workspace_config else {})

    @classmethod
    def from_env(
        cls, env: dict[str, str] | None = None, *, export: bool = True,
    ) -> "CiaoConfig":
        """Build a config from the environment.

        With ``env is None`` the workspace's ``.env`` is applied. ``export``
        decides whether that lands in ``os.environ``:

        ``True`` (default) is what a process that goes on to SPAWN something
        needs — the server and every CLI path that shells out to a provider.
        `.mcp.json` stores credentials as ``${NAME}`` placeholders resolved by
        the provider CLI from the environment it inherits, so dropping the
        export would reach the Notion/n8n servers as an unresolved literal and
        every call would 401. Injected keys are recorded so
        :func:`reset_exported_dotenv` can put the process back.

        ``False`` reads the same values without touching the process: the
        file is overlaid *under* ``os.environ`` in a private mapping, which is
        the same precedence ``load_dotenv`` applies and the same trick the
        discovery branch below already uses. Use it for anything read-only —
        a diagnostic must not change its caller's environment.
        """
        overlay: dict[str, str] = {}
        if env is None:
            workspace_env_val = os.environ.get("CIAO_WORKSPACE", "").strip() or "."
            dotenv_path = Path(workspace_env_val).expanduser().resolve() / ".env"
            if dotenv_path.exists():
                if export:
                    from dotenv import load_dotenv

                    # Which keys this call is about to add. load_dotenv does not
                    # report that, and it must not be inferred afterwards: a key
                    # the process already carried looks identical once set, and
                    # removing one of those on reset would clear a variable the
                    # operator exported themselves.
                    before = set(os.environ)
                    load_dotenv(dotenv_path)
                    added = set(os.environ) - before
                    for key in added & DOTENV_IGNORED_KEYS:
                        os.environ.pop(key, None)
                    _EXPORTED_DOTENV_KEYS.update(added - DOTENV_IGNORED_KEYS)
                else:
                    from dotenv import dotenv_values

                    try:
                        overlay = without_ignored_dotenv_keys(
                            dotenv_values(dotenv_path)
                        )
                    except OSError:
                        overlay = {}
            if export:
                # Default to disabling Claude Code's auto memory inside Ciaobot
                os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
                # Artifacts publish to claude.ai; ciaobot has no use for that surface
                os.environ.setdefault("CLAUDE_CODE_DISABLE_ARTIFACT", "1")
            else:
                overlay.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
                overlay.setdefault("CLAUDE_CODE_DISABLE_ARTIFACT", "1")

        if env is not None:
            source: Any = env
        elif overlay:
            # Process environment wins over the file, matching load_dotenv.
            source = {**overlay, **os.environ}
        else:
            source = os.environ

        if env is None and not _workspace_env(source):
            from ciao.setup_marker import is_set_up_workspace

            try:
                cwd: Path | None = Path.cwd()
            except OSError:  # the directory the shell stands in was deleted
                cwd = None
            if cwd is not None and is_set_up_workspace(cwd):
                # Started inside a set-up workspace (`ciao run` from the
                # workspace folder). Setup used to write
                # CIAO_WORKSPACE=. into `.env` for this; the registry is the
                # marker now. Checked before discovery, so a run inside a
                # scratch workspace never attaches to the installed one.
                source = {**source, "CIAO_WORKSPACE": str(cwd)}
        if env is None and not _workspace_env(source):
            # A bare-shell CLI invocation has no CIAO_WORKSPACE: read the install the
            # LaunchAgent points at (see `installed_workspace_env`). Gated on
            # ``env is None``: explicit env dicts keep their exact semantics.
            source = installed_workspace_env(source)

        pwa_auth_token = source.get("PWA_AUTH_TOKEN", "").strip()
        # Password protection is always on; there is no setting that turns it
        # off. Without a workspace this is the first-run wizard.
        bootstrap_mode = not _workspace_env(source)
        if bootstrap_mode:
            workspace_root = _bootstrap_workspace(source)
            runtime_default = workspace_root / ".runtime"
            pwa_auth_token = _read_or_create_secret(
                runtime_default / "bootstrap-auth-token"
            )
        else:
            workspace_root = Path(
                _workspace_env(source) or "."
            ).expanduser().resolve()
            runtime_default = Path(".runtime")
            if not pwa_auth_token:
                # A workspace .env with no password (hand-written; setup always
                # writes one). Sessions still have to be signed, so persist a
                # random per-workspace secret rather than a shared constant.
                # Protection stays on: the owner signs in with a one-time
                # `ciao setup-url` login, or sets PWA_AUTH_TOKEN in `.env`.
                pwa_auth_token = _read_or_create_secret(
                    workspace_root / ".runtime" / "session-secret"
                )

        vault_root_raw = source.get("CIAO_VAULT_ROOT", "").strip()
        if vault_root_raw:
            vault_root = Path(vault_root_raw).expanduser()
            if not vault_root.is_absolute():
                vault_root = workspace_root / vault_root
            vault_root = vault_root.resolve()
        else:
            vault_root = (workspace_root / "memory-vault").resolve()
        runtime_root = Path(
            source.get("CIAO_RUNTIME_ROOT", "").strip() or str(runtime_default)
        ).expanduser()
        if not runtime_root.is_absolute():
            runtime_root = workspace_root / runtime_root
        runtime_root = runtime_root.resolve()
        state_path = runtime_root / "state.json"
        media_root = runtime_root / "telegram_media"  # keep old path for existing media
        # The runtime registry is the only source of the workspace list: it is
        # state the app and Settings own. ``CIAO_WORKSPACES`` is no longer
        # read as a source; its raw value is kept only so server startup can
        # import it once (see ``import_legacy_workspaces_env``).
        workspaces_json = ""
        workspaces_path = runtime_root / "workspaces.json"
        try:
            registry_bytes = workspaces_path.read_bytes()
            workspaces_json = registry_bytes.decode("utf-8")
        except FileNotFoundError:
            registry_bytes = None
        except OSError as exc:
            from ciao.workspace_reroot import RuntimeStateUnreadable  # noqa: PLC0415

            raise RuntimeStateUnreadable(workspaces_path) from exc

        workspaces = _parse_workspaces_json(workspaces_json) or _bootstrap_registry(vault_root)

        # Settings the server needs before it exists (bind address, log level),
        # read from the runtime root like the registry above. Read-only: the
        # one-time import of their retired `.env` variables happens at server
        # startup, which then overlays the result (`apply_to_config`).
        from ciao.app_settings import (
            DEFAULT_LOG_LEVEL,
            DEFAULT_PWA_HOST,
            LEGACY_ENV_SETTINGS,
            read_app_settings,
        )

        stored = read_app_settings(runtime_root / "app_settings.json")
        app_repo = Path(stored.app_repo).expanduser().resolve() if stored.app_repo else None
        legacy_env_settings = {
            name: str(source.get(name, "") or "").strip()
            for name in LEGACY_ENV_SETTINGS
            if str(source.get(name, "") or "").strip()
        }

        return cls(
            pwa_auth_token=pwa_auth_token,
            workspace_root=workspace_root,
            state_path=state_path,
            media_root=media_root,
            dev_mode=stored.dev_mode,
            app_repo=app_repo,
            bootstrap_mode=bootstrap_mode,
            vault_root=vault_root,
            claude_mode="auto",
            pwa_port=int(source.get("PWA_PORT", "8443")),
            pwa_host=stored.pwa_host or DEFAULT_PWA_HOST,
            log_level=stored.log_level or DEFAULT_LOG_LEVEL,
            legacy_env_settings=legacy_env_settings,
            workspaces=workspaces,
            _workspace_registry_bytes=registry_bytes,
            legacy_workspaces_env=str(source.get("CIAO_WORKSPACES", "") or "").strip(),
        )


logger = logging.getLogger(__name__)


def active_workspace_vault_root(env: Mapping[str, str]) -> Path | None:
    """The vault the ``CIAO_ACTIVE_WORKSPACE`` workspace owns, or ``None``.

    ``None`` when that variable is unset or names no registered workspace. Read
    only: the same resolution as the memory-proposal commands, with the
    ``PWA_AUTH_TOKEN`` stand-in so a CLI call never mints a session secret.
    """
    active = str(env.get("CIAO_ACTIVE_WORKSPACE", "") or "").strip()
    if not active:
        return None
    source = installed_workspace_env(env)
    if not source.get("PWA_AUTH_TOKEN", "").strip():
        source["PWA_AUTH_TOKEN"] = "vault-root"
    config = CiaoConfig.from_env(source)
    if config.workspace(active) is None:
        return None
    return Path(config.agent_vault_root(active)).resolve()


# Backward-compatible alias used by project_chats.py and other modules
BridgeConfig = CiaoConfig
