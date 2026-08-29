"""Shared workspace-registry helpers.

The PWA settings routes (``ciao/web/routes_api.py``) and the MCP control
plane (``ciao/control_plane.py``) both create, update, serialize, and
persist logical workspaces. These helpers used to live inside the routes
module; they are shared here so the two surfaces cannot drift apart on
validation or serialization rules.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, cast

from ciao import provider_registry
from ciao.config import (
    DEFAULT_WORKSPACE_COLOR,
    WorkspaceConfig,
    coerce_workspace_color,
)

WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

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


def workspace_to_dict(workspace: WorkspaceConfig, config: Any) -> dict:
    try:
        color = coerce_workspace_color(getattr(workspace, "color", DEFAULT_WORKSPACE_COLOR))
    except ValueError:
        color = DEFAULT_WORKSPACE_COLOR
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
        "allowed_mcp_servers": (
            list(
                cast(Iterable[Any], getattr(workspace, "allowed_mcp_servers", None))
            )
            if getattr(workspace, "allowed_mcp_servers", None) is not None
            else None
        ),
        "gws_profile": getattr(workspace, "gws_profile", ""),
        "color": color,
    }


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
    setup pointed a workspace at ``CIAO_VAULT_ROOT`` (see
    ``CiaoConfig.legacy_entity_workspace``) every standard per-workspace folder
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
    if "allowed_mcp_servers" in data:
        allowed_mcp_servers = parse_disallowed_tools_value(
            data.get("allowed_mcp_servers")
        )
    elif existing is not None:
        allowed_mcp_servers = existing.allowed_mcp_servers
    else:
        allowed_mcp_servers = None
    if "color" in data:
        color = coerce_workspace_color(data.get("color"))
    elif existing is not None:
        try:
            color = coerce_workspace_color(existing.color)
        except ValueError:
            color = DEFAULT_WORKSPACE_COLOR
    else:
        color = DEFAULT_WORKSPACE_COLOR
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
        allowed_mcp_servers=allowed_mcp_servers,
        gws_profile=str(data.get("gws_profile", existing.gws_profile if existing else "")).strip(),
        color=color,
    )


def _workspaces_path(config: Any) -> Path:
    return Path(config.state_path).resolve().parent / "workspaces.json"


def persist_workspaces(config: Any) -> None:
    persist = getattr(config, "persist_workspace_registry", None)
    if callable(persist):
        persist()
        return
    path = _workspaces_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [workspace_to_dict(workspace, config) for workspace in config.workspaces.values()]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
