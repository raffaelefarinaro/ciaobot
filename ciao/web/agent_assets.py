"""Instruction, command, and subagent assets for the Settings UI."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.memory_tool import ensure_regions
from ciao.sync_skills import sync_workspace_skills
from ciao.web.commands import _parse_frontmatter

logger = logging.getLogger(__name__)

_ASSET_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


@dataclass(slots=True)
class AgentAsset:
    name: str
    description: str
    source: str
    scope: str
    path: str
    editable: bool
    vault_path: str
    content: str = ""


@dataclass(slots=True)
class CommandAsset:
    name: str
    description: str
    argument_hint: str
    source: str
    scope: str
    path: str
    editable: bool
    vault_path: str
    content: str = ""


@dataclass(slots=True)
class WorkspaceHealthCheck:
    id: str
    title: str
    status: str
    detail: str
    path: str = ""
    action: str = ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _normalize_asset_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")
    if not _ASSET_NAME_RE.match(name):
        raise ValueError("name must start with a letter and contain only lowercase letters, numbers, and dashes")
    return name


def _frontmatter_string(fields: dict[str, str]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        cleaned = value.replace("\n", " ").strip()
        lines.append(f"{key}: {cleaned}")
    lines.append("---")
    return "\n".join(lines)


def _body_without_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].lstrip()
    return text


def _frontmatter_body(text: str) -> tuple[dict[str, str], str]:
    return _parse_frontmatter(text), _body_without_frontmatter(text).strip()


def _iter_markdown_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*.md") if path.is_file() or path.is_symlink())


def _mirror_vault_root(config: Any) -> Path:
    """The vault that receives a Settings-written mirror doc.

    Settings has no workspace context — it edits install-wide assets — so after
    the re-rooting the mirror goes to the PRIMARY root's vault, the same choice
    P10.4 and P10.5 made for the shared guide and the skill catalog. Before the
    re-rooting `agent_vault_root` returns the shared vault, so nothing changes.
    """
    primary = getattr(config, "primary_workspace", None)
    resolver = getattr(config, "agent_vault_root", None)
    if callable(primary) and callable(resolver):
        try:
            name = primary()
            if name:
                return Path(resolver(name))
        except (ValueError, OSError):
            pass
    return Path(config.vault_root)


def _vault_mirror_path(config: Any, category: str, name: str) -> Path:
    return _mirror_vault_root(config) / "Workspace" / category / f"{name}.md"


def _write_vault_mirror(
    *,
    config: Any,
    category: str,
    name: str,
    title: str,
    description: str,
    canonical_path: Path,
    body: str,
) -> Path:
    mirror = _vault_mirror_path(config, category, name)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    rel = _relative_or_absolute(canonical_path, Path(config.workspace_root))
    mirror.write_text(
        "\n".join(
            [
                "---",
                f"type: {category[:-1].lower()}",
                f"name: {name}",
                f"title: {title}",
                f"description: {description}",
                f"canonical_path: {rel}",
                "source: ciao-settings",
                "---",
                "",
                f"# {title}",
                "",
                body.strip(),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return mirror


def _write_subagent_file(path: Path, *, name: str, description: str, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n\n".join([
            _frontmatter_string({"name": name, "description": description}),
            content.strip(),
        ]) + "\n",
        encoding="utf-8",
    )


def _write_command_file(
    path: Path,
    *,
    description: str,
    argument_hint: str,
    content: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"description": description}
    if argument_hint:
        fields["argument-hint"] = argument_hint
    path.write_text(
        "\n\n".join([
            _frontmatter_string(fields),
            content.strip(),
        ]) + "\n",
        encoding="utf-8",
    )


def _installed_name_conflict(installed_path: Path, target_path: Path) -> bool:
    """True when creating ``target_path`` would replace an installed asset."""
    if not installed_path.exists() and not installed_path.is_symlink():
        return False
    if not installed_path.is_symlink():
        return True
    try:
        return installed_path.resolve() != target_path.resolve()
    except FileNotFoundError:
        return True


def _agent_asset_from_file(path: Path, *, root: Path, source: str, scope: str, editable: bool, vault_root: Path) -> AgentAsset:
    text = _read_text(path)
    fm = _parse_frontmatter(text)
    name = fm.get("name", "").strip() or path.stem
    mirror = vault_root / "Workspace" / "Subagents" / f"{name}.md"
    return AgentAsset(
        name=name,
        description=fm.get("description", "").strip(),
        source=source,
        scope=scope,
        path=_relative_or_absolute(path, root),
        editable=editable,
        vault_path=_relative_or_absolute(mirror, root) if mirror.exists() else "",
        content=text,
    )


def _command_asset_from_file(path: Path, *, root: Path, source: str, scope: str, editable: bool, vault_root: Path) -> CommandAsset:
    text = _read_text(path)
    fm = _parse_frontmatter(text)
    name = path.stem
    mirror = vault_root / "Workspace" / "Commands" / f"{name}.md"
    return CommandAsset(
        name=name,
        description=fm.get("description", "").strip(),
        argument_hint=fm.get("argument-hint", "").strip(),
        source=source,
        scope=scope,
        path=_relative_or_absolute(path, root),
        editable=editable,
        vault_path=_relative_or_absolute(mirror, root) if mirror.exists() else "",
        content=text,
    )


def _dedupe_by_name(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for item in items:
        name = getattr(item, "name", "")
        if name in seen:
            continue
        seen.add(name)
        out.append(item)
    return out


def _memory_proposal_paths(config: Any, vault: Path, root: Path) -> list[tuple[Path, str]]:
    """Proposal queue files under each workspace vault."""
    paths: list[tuple[Path, str]] = []
    ws_names: list[str] = []
    if hasattr(config, "workspace_names") and callable(config.workspace_names):
        ws_names = config.workspace_names()

    if not ws_names:
        for name in ("personal", "work"):
            ws_dir = vault / name
            if name == "personal" or ws_dir.is_dir():
                paths.append((
                    ws_dir / "Workspace" / "Memory-Proposals.md",
                    f"Memory proposals ({name})" if (vault / "work").is_dir() else "Memory proposals",
                ))
        return paths

    for name in ws_names:
        ws_vault_root = config.workspace_vault_root(name)
        title = f"Memory proposals ({name})" if len(ws_names) > 1 else "Memory proposals"
        paths.append((ws_vault_root / "Workspace" / "Memory-Proposals.md", title))
    return paths


def _workspace_memory_paths(
    config: Any, root: Path, vault: Path, *, workspace: str = ""
) -> list[tuple[Path, str]]:
    """MEMORY.md locations per configured workspace (shared by the health
    check and the fix endpoint so they cannot drift).

    ``workspace`` narrows the answer to that one workspace, for a caller already
    iterating agent roots. Without it the health check ran every workspace's
    MEMORY.md under every agent root: two workspaces produced four rows, half of
    them foreign to the root being checked, rendered with an absolute path and a
    doubled label ("Workspace MEMORY.md (work) (personal)"). One shared vault
    made the cross product correct; one vault per root does not.
    """
    memory_paths: list[tuple[Path, str]] = []
    ws_names = []
    if hasattr(config, "workspace_names") and callable(config.workspace_names):
        ws_names = config.workspace_names()
    if workspace:
        # An unregistered name gets no row rather than the shared vault's, which
        # under a per-root path would name a file that root does not own.
        return (
            [(config.workspace_vault_root(workspace) / "MEMORY.md", "Workspace MEMORY.md")]
            if workspace in ws_names
            else []
        )

    if not ws_names:
        memory_paths.append((vault / "MEMORY.md", "Workspace MEMORY.md"))
    else:
        for name in ws_names:
            ws_vault_root = config.workspace_vault_root(name)
            title = f"Workspace MEMORY.md ({name})" if len(ws_names) > 1 else "Workspace MEMORY.md"
            memory_paths.append((ws_vault_root / "MEMORY.md", title))
    return memory_paths


def workspace_health(config: Any) -> dict:
    root = Path(config.workspace_root)
    vault = Path(config.vault_root)
    checks: list[WorkspaceHealthCheck] = []

    def add(id_: str, title: str, status: str, detail: str, path: Path | str = "", action: str = "") -> None:
        checks.append(WorkspaceHealthCheck(
            id=id_,
            title=title,
            status=status,
            detail=detail,
            path=_relative_or_absolute(path, root) if isinstance(path, Path) and path else str(path or ""),
            action=action,
        ))

    add("workspace-root", "Workspace root", "ok" if root.is_dir() else "error", "Workspace root exists." if root.is_dir() else "Workspace root is missing.", root)
    add("workspace-writable", "Workspace writable", "ok" if os.access(root, os.W_OK) else "error", "Workspace is writable." if os.access(root, os.W_OK) else "Workspace is not writable.", root)
    # One vault before the re-rooting, one per agent root after it. Checking
    # `config.vault_root` alone reported "Vault root is missing" and "Vault is
    # not writable" on a CORRECTLY migrated install, because that path is exactly
    # what the migration empties.
    vault_targets: list[tuple[Path, str]] = []
    getter = getattr(config, "vault_scan_targets", None)
    if callable(getter):
        try:
            vault_targets = [(Path(root), name) for root, name, _prefix in getter()]
        except Exception:  # noqa: BLE001 — fall back to the single-vault check
            vault_targets = []
    if not vault_targets:
        vault_targets = [(vault, "")]
    for target, name in vault_targets:
        label = f"Vault root ({name})" if name else "Vault root"
        suffix = f" for {name}" if name else ""
        add(
            f"vault-root{'-' + name if name else ''}",
            label,
            "ok" if target.is_dir() else "error",
            f"Vault root{suffix} exists." if target.is_dir() else f"Vault root{suffix} is missing.",
            target,
        )
        writable = target.is_dir() and os.access(target, os.W_OK)
        add(
            f"vault-writable{'-' + name if name else ''}",
            f"Vault writable ({name})" if name else "Vault writable",
            "ok" if writable else "error",
            f"Vault{suffix} is writable." if writable else f"Vault{suffix} is not writable.",
            target,
        )

    # Every agent root, which is the install root before the re-rooting and
    # one per workspace after it. Checking only `workspace_root` found the
    # install root's stale `.claude/`, whose links point at a catalog that
    # moved, so a correctly migrated install reported every custom skill as a
    # broken generated link.
    root_targets: list[tuple[Path, str]] = []
    targets_getter = getattr(config, "agent_root_targets", None)
    if callable(targets_getter):
        try:
            root_targets = [(Path(r), str(n)) for r, n in targets_getter()]
        except Exception:  # noqa: BLE001 — fall back to the single-root checks
            root_targets = []
    if not root_targets:
        root_targets = [(root, "")]

    for agent_root, ws_name in root_targets:
        root = agent_root
        suffix = f" ({ws_name})" if ws_name else ""
        id_suffix = f"-{ws_name}" if ws_name else ""
        memory_paths = _workspace_memory_paths(config, root, vault, workspace=ws_name)

        check_paths = [
            (root / "CLAUDE.md", "Project CLAUDE.md"),
            (root / "AGENTS.md", "Project AGENTS.md"),
        ]
        check_paths.extend(memory_paths)
        check_paths.extend([
            (root / "subagents", "Canonical subagents directory"),
            (root / "commands", "Canonical commands directory"),
            (root / ".claude" / "agents", "Generated .claude agents directory"),
            (root / ".claude" / "commands", "Generated .claude commands directory"),
            (root / ".claude" / "skills", "Generated skills directory"),
            # OpenCode reads the shared Claude skills catalog, workspace guides,
            # and its own optional projections natively.
            (root / ".opencode" / "agents", "Generated opencode subagents directory"),
            (root / ".opencode" / "commands", "Generated opencode commands directory"),
        ])

        for path, title in check_paths:
            exists = path.exists()
            add(
                f"path-{_relative_or_absolute(path, root)}{id_suffix}",
                title + suffix,
                "ok" if exists else "warn",
                "Present." if exists else "Missing; Ciaobot can continue, but this workspace is less discoverable to its agent providers.",
                path,
                "Create it or run sync-skills." if not exists else "",
            )

        claude_guide = root / "CLAUDE.md"
        shared_guide = root / "AGENTS.md"
        if claude_guide.is_file() and (shared_guide.exists() or shared_guide.is_symlink()):
            try:
                guides_linked = shared_guide.resolve() == claude_guide.resolve()
            except OSError:
                guides_linked = False
            add(
                f"guides-linked{id_suffix}",
                "Linked workspace guides" + suffix,
                "ok" if guides_linked else "warn",
                "AGENTS.md links to CLAUDE.md, so Claude Code and opencode share one workspace guide."
                if guides_linked
                else "AGENTS.md is a separate file, so Claude Code and opencode read different workspace instructions.",
                shared_guide,
                "" if guides_linked else "Merge AGENTS.md into CLAUDE.md, delete AGENTS.md, then run sync-skills to relink.",
            )

        if claude_guide.is_file():
            from ciao.memory_tool import diagnose_guide

            region_diags = diagnose_guide(claude_guide)
            add(
                f"memory-regions{id_suffix}",
                "Bounded memory regions" + suffix,
                "ok" if not region_diags else "warn",
                "The `ciao:memory` and `ciao:profile` regions are present and well-formed."
                if not region_diags
                else "; ".join(d.message for d in region_diags),
                claude_guide,
                "" if not region_diags else "Run sync-skills to add any missing region markers.",
            )

        for source_dir, link_dir, label in [
            (root / "subagents", root / ".claude" / "agents", "subagent"),
            (root / "commands", root / ".claude" / "commands", "command"),
        ]:
            for source in _iter_markdown_files(source_dir):
                link = link_dir / source.name
                try:
                    synced = link.is_symlink() and link.resolve() == source.resolve()
                except OSError:
                    synced = False
                if not synced:
                    add(
                        f"unsynced-{label}-{source.stem}{id_suffix}",
                        f"Unsynced {label}: {source.stem}",
                        "warn",
                        f"Custom {label} is not linked into Claude Code discovery.",
                        source,
                        "Run sync-skills.",
                    )

        for link_dir, label in [
            (root / ".claude" / "agents", "agent"),
            (root / ".claude" / "commands", "command"),
            (root / ".claude" / "skills", "skill"),
            (root / ".agents" / "skills", "provider skill"),
        ]:
            if not link_dir.exists():
                continue
            for path in link_dir.rglob("*"):
                if path.is_symlink() and not path.exists():
                    add(
                        f"broken-{label}-{path.name}{id_suffix}",
                        f"Broken generated {label} link",
                        "error",
                        "Generated provider asset points at a missing file.",
                        path,
                        "Run sync-skills.",
                    )

    overall = "ok"
    if any(check.status == "error" for check in checks):
        overall = "error"
    elif any(check.status == "warn" for check in checks):
        overall = "warn"
    return {"status": overall, "checks": [asdict(check) for check in checks]}


def list_subagents(config: Any) -> list[AgentAsset]:
    root = Path(config.workspace_root)
    vault_root = Path(config.vault_root)
    items: list[AgentAsset] = []
    from ciao.sync_skills import _is_managed_stock_agent

    for path in _iter_markdown_files(root / "subagents"):
        items.append(_agent_asset_from_file(
            path, root=root, source="workspace", scope="custom", editable=True, vault_root=vault_root,
        ))
    for path in _iter_markdown_files(root / ".claude" / "agents"):
        if (root / "subagents" / path.name).exists():
            continue
        stock = _is_managed_stock_agent(path)
        items.append(_agent_asset_from_file(
            path,
            root=root,
            source="ciaobot" if stock else "project",
            scope="built-in" if stock else "installed",
            editable=False,
            vault_root=vault_root,
        ))
    for path in _iter_markdown_files(Path.home() / ".claude" / "agents"):
        items.append(_agent_asset_from_file(
            path, root=root, source="user", scope="global", editable=False, vault_root=vault_root,
        ))
    return sorted(_dedupe_by_name(items), key=lambda item: item.name)


def _stock_command_sources() -> dict[str, str]:
    """Packaged ``ciao.stock/commands`` text, keyed by command name.

    Stock commands are seeded straight into the canonical, editable
    ``commands/`` directory (see ``_seed_stock_commands``) so they stay
    covered by weekly-review hygiene checks. That means the only way to
    tell a still-stock command apart from a user-authored one is to diff
    its content against the packaged source.
    """
    try:
        stock_commands = resources.files("ciao.stock").joinpath("commands")
        entries = [entry for entry in stock_commands.iterdir() if entry.name.endswith(".md")]
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        return {}
    out: dict[str, str] = {}
    for entry in entries:
        with resources.as_file(entry) as stock_path:
            out[stock_path.stem] = stock_path.read_text(encoding="utf-8")
    return out


def list_command_assets(config: Any) -> list[CommandAsset]:
    root = Path(config.workspace_root)
    vault_root = Path(config.vault_root)
    items: list[CommandAsset] = []
    stock_sources = _stock_command_sources()

    for path in _iter_markdown_files(root / "commands"):
        is_stock = stock_sources.get(path.stem) == _read_text(path)
        items.append(_command_asset_from_file(
            path,
            root=root,
            source="ciaobot" if is_stock else "workspace",
            scope="built-in" if is_stock else "custom",
            editable=True,
            vault_root=vault_root,
        ))
    for path in _iter_markdown_files(root / ".claude" / "commands"):
        if (root / "commands" / path.name).exists():
            continue
        items.append(_command_asset_from_file(
            path, root=root, source="project", scope="installed", editable=False, vault_root=vault_root,
        ))
    for path in _iter_markdown_files(Path.home() / ".claude" / "commands"):
        items.append(_command_asset_from_file(
            path, root=root, source="user", scope="global", editable=False, vault_root=vault_root,
        ))
    return sorted(_dedupe_by_name(items), key=lambda item: item.name)


async def agent_assets_endpoint(request: Request) -> JSONResponse:
    """GET /api/agent-assets — Settings inventory for subagents, commands, and health."""
    config = request.app.state.config
    try:
        return JSONResponse({
            "subagents": [asdict(item) for item in list_subagents(config)],
            "commands": [asdict(item) for item in list_command_assets(config)],
            "health": workspace_health(config),
        })
    except Exception:  # noqa: BLE001
        logger.exception("listing agent assets failed")
        return JSONResponse({"error": "failed to list agent assets"}, status_code=500)


async def create_subagent_endpoint(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        name = _normalize_asset_name(str(body.get("name", "")))
        description = str(body.get("description", "")).strip()
        prompt = str(body.get("prompt", "")).strip()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not description or not prompt:
        return JSONResponse({"error": "description and prompt are required"}, status_code=400)

    root = Path(config.workspace_root)
    target = root / "subagents" / f"{name}.md"
    if target.exists():
        return JSONResponse({"error": f"subagent '{name}' already exists"}, status_code=409)
    installed = root / ".claude" / "agents" / f"{name}.md"
    if _installed_name_conflict(installed, target):
        return JSONResponse({"error": f"subagent '{name}' conflicts with an installed/system subagent"}, status_code=409)
    target.parent.mkdir(parents=True, exist_ok=True)
    title = name.replace("-", " ").title()
    content = f"# {title}\n\n{prompt}"
    _write_subagent_file(target, name=name, description=description, content=content)
    mirror = _write_vault_mirror(
        config=config,
        category="Subagents",
        name=name,
        title=title,
        description=description,
        canonical_path=target,
        body=prompt,
    )
    sync_workspace_skills(root, refresh_upstream=False)
    return JSONResponse({
        "ok": True,
        "asset": asdict(_agent_asset_from_file(
            target, root=root, source="workspace", scope="custom", editable=True, vault_root=Path(config.vault_root),
        )),
        "path": _relative_or_absolute(target, root),
        "vault_path": _relative_or_absolute(mirror, root),
    }, status_code=201)


async def create_command_endpoint(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        name = _normalize_asset_name(str(body.get("name", "")))
        description = str(body.get("description", "")).strip()
        argument_hint = str(body.get("argument_hint", "")).strip()
        prompt = str(body.get("prompt", "")).strip()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not description or not prompt:
        return JSONResponse({"error": "description and prompt are required"}, status_code=400)

    root = Path(config.workspace_root)
    target = root / "commands" / f"{name}.md"
    if target.exists():
        return JSONResponse({"error": f"command '{name}' already exists"}, status_code=409)
    installed = root / ".claude" / "commands" / f"{name}.md"
    if _installed_name_conflict(installed, target):
        return JSONResponse({"error": f"command '{name}' conflicts with an installed/system command"}, status_code=409)
    target.parent.mkdir(parents=True, exist_ok=True)
    title = name.replace("-", " ").title()
    content = f"# {title}: $ARGUMENTS\n\n{prompt}"
    _write_command_file(
        target,
        description=description,
        argument_hint=argument_hint,
        content=content,
    )
    mirror = _write_vault_mirror(
        config=config,
        category="Commands",
        name=name,
        title=title,
        description=description,
        canonical_path=target,
        body=_body_without_frontmatter(content),
    )
    sync_workspace_skills(root, refresh_upstream=False)
    return JSONResponse({
        "ok": True,
        "asset": asdict(_command_asset_from_file(
            target, root=root, source="workspace", scope="custom", editable=True, vault_root=Path(config.vault_root),
        )),
        "path": _relative_or_absolute(target, root),
        "vault_path": _relative_or_absolute(mirror, root),
    }, status_code=201)


async def update_subagent_endpoint(request: Request) -> JSONResponse:
    config = request.app.state.config
    root = Path(config.workspace_root)
    try:
        name = _normalize_asset_name(request.path_params["name"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    target = root / "subagents" / f"{name}.md"
    if not target.exists():
        return JSONResponse({"error": f"custom subagent '{name}' not found"}, status_code=404)
    current_fm, current_body = _frontmatter_body(_read_text(target))
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    description = str(body.get("description", current_fm.get("description", ""))).strip()
    content = str(body.get("content", body.get("prompt", current_body))).strip()
    if not description or not content:
        return JSONResponse({"error": "description and content are required"}, status_code=400)
    _write_subagent_file(target, name=name, description=description, content=content)
    mirror = _write_vault_mirror(
        config=config,
        category="Subagents",
        name=name,
        title=name.replace("-", " ").title(),
        description=description,
        canonical_path=target,
        body=content,
    )
    sync_workspace_skills(root, refresh_upstream=False)
    return JSONResponse({
        "ok": True,
        "asset": asdict(_agent_asset_from_file(
            target, root=root, source="workspace", scope="custom", editable=True, vault_root=Path(config.vault_root),
        )),
        "path": _relative_or_absolute(target, root),
        "vault_path": _relative_or_absolute(mirror, root),
    })


async def update_command_endpoint(request: Request) -> JSONResponse:
    config = request.app.state.config
    root = Path(config.workspace_root)
    try:
        name = _normalize_asset_name(request.path_params["name"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    target = root / "commands" / f"{name}.md"
    if not target.exists():
        return JSONResponse({"error": f"custom command '{name}' not found"}, status_code=404)
    current_fm, current_body = _frontmatter_body(_read_text(target))
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    description = str(body.get("description", current_fm.get("description", ""))).strip()
    argument_hint = str(body.get("argument_hint", current_fm.get("argument-hint", ""))).strip()
    content = str(body.get("content", body.get("prompt", current_body))).strip()
    if not description or not content:
        return JSONResponse({"error": "description and content are required"}, status_code=400)
    _write_command_file(
        target,
        description=description,
        argument_hint=argument_hint,
        content=content,
    )
    mirror = _write_vault_mirror(
        config=config,
        category="Commands",
        name=name,
        title=name.replace("-", " ").title(),
        description=description,
        canonical_path=target,
        body=content,
    )
    sync_workspace_skills(root, refresh_upstream=False)
    return JSONResponse({
        "ok": True,
        "asset": asdict(_command_asset_from_file(
            target, root=root, source="workspace", scope="custom", editable=True, vault_root=Path(config.vault_root),
        )),
        "path": _relative_or_absolute(target, root),
        "vault_path": _relative_or_absolute(mirror, root),
    })


def _delete_generated_link(link: Path, target: Path) -> None:
    if not link.is_symlink():
        return
    try:
        if link.resolve() != target.resolve():
            return
    except FileNotFoundError:
        pass
    link.unlink(missing_ok=True)


async def delete_subagent_endpoint(request: Request) -> JSONResponse:
    config = request.app.state.config
    root = Path(config.workspace_root)
    try:
        name = _normalize_asset_name(request.path_params["name"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    target = root / "subagents" / f"{name}.md"
    if not target.exists():
        return JSONResponse({"error": f"custom subagent '{name}' not found"}, status_code=404)
    target.unlink()
    _vault_mirror_path(config, "Subagents", name).unlink(missing_ok=True)
    _delete_generated_link(root / ".claude" / "agents" / f"{name}.md", target)
    sync_workspace_skills(root, refresh_upstream=False)
    return JSONResponse({"ok": True, "name": name})


async def delete_command_endpoint(request: Request) -> JSONResponse:
    config = request.app.state.config
    root = Path(config.workspace_root)
    try:
        name = _normalize_asset_name(request.path_params["name"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    target = root / "commands" / f"{name}.md"
    if not target.exists():
        return JSONResponse({"error": f"custom command '{name}' not found"}, status_code=404)
    target.unlink()
    _vault_mirror_path(config, "Commands", name).unlink(missing_ok=True)
    _delete_generated_link(root / ".claude" / "commands" / f"{name}.md", target)
    sync_workspace_skills(root, refresh_upstream=False)
    return JSONResponse({"ok": True, "name": name})


async def workspace_health_endpoint(request: Request) -> JSONResponse:
    try:
        return JSONResponse(workspace_health(request.app.state.config))
    except Exception:  # noqa: BLE001
        logger.exception("workspace health failed")
        return JSONResponse({"error": "failed to scan workspace"}, status_code=500)


def _merge_agents_into_claude(root: Path) -> bool:
    """Fold a real, user-authored ``AGENTS.md`` into ``CLAUDE.md``, then symlink it.

    A no-op unless ``AGENTS.md`` exists as a regular file (not a symlink) whose
    content actually differs from ``CLAUDE.md`` (or ``CLAUDE.md`` is missing).
    The prior ``AGENTS.md`` is preserved as ``AGENTS.md.bak`` before anything
    is rewritten. Returns whether a merge happened.
    """
    claude = root / "CLAUDE.md"
    agents = root / "AGENTS.md"
    if not agents.is_file() or agents.is_symlink():
        return False
    try:
        if claude.is_file() and agents.resolve() == claude.resolve():
            return False
    except OSError:
        pass

    try:
        agents_text = agents.read_text(encoding="utf-8")
    except OSError:
        return False

    if claude.is_file():
        try:
            claude_text = claude.read_text(encoding="utf-8")
        except OSError:
            return False
        if claude_text.strip() == agents_text.strip():
            return False
    else:
        claude_text = ""

    try:
        (root / "AGENTS.md.bak").write_text(agents_text, encoding="utf-8")
    except OSError:
        return False

    if not claude_text:
        merged_text = agents_text
    else:
        existing_lines = {line.strip() for line in claude_text.splitlines() if line.strip()}
        unique_lines = [
            line for line in agents_text.splitlines()
            if line.strip() and line.strip() not in existing_lines
        ]
        if unique_lines:
            merged_text = (
                claude_text.rstrip()
                + "\n\n## Merged from AGENTS.md\n\n"
                + "\n".join(unique_lines)
                + "\n"
            )
        else:
            merged_text = claude_text

    try:
        claude.write_text(merged_text, encoding="utf-8")
    except OSError:
        return False

    try:
        ensure_regions(claude)
    except OSError:
        pass

    try:
        agents.unlink()
        agents.symlink_to(claude.name)
    except OSError:
        return False
    return True


def repair_workspace_health(config: Any) -> dict:
    """Apply the automatic remedies for every fixable health check.

    Covers exactly the actions the checks suggest in prose: create the
    missing scaffold files/directories, merge a stray user-authored
    ``AGENTS.md`` into ``CLAUDE.md``, then rebuild the Claude Code discovery
    links (sync-skills, without the network-touching upstream refresh).
    Returns the fresh health report, with ``merged_agents_guide: True`` added
    when the ``AGENTS.md`` merge above actually ran.
    """
    from ciao.cli import _copy_tree_if_missing, _write_if_missing

    install_root = Path(config.workspace_root)
    vault = Path(config.vault_root)

    # Every agent root the health check reports on, so the remedy covers what
    # the check found. Repairing `workspace_root` alone re-created CLAUDE.md,
    # subagents/ and commands/ in the install root of a migrated install — the
    # agent-shaped debris the migration exists to remove, put back by the
    # button that claims to fix things (the same bug `ciao setup` had).
    root_targets: list[tuple[Path, str]] = []
    targets_getter = getattr(config, "agent_root_targets", None)
    if callable(targets_getter):
        try:
            root_targets = [(Path(r), str(n)) for r, n in targets_getter()]
        except Exception:  # noqa: BLE001 — fall back to the single-root repair
            root_targets = []
    if not root_targets:
        root_targets = [(install_root, "")]

    # Workspace docs (CLAUDE.md and friends) come from the packaged stock
    # seeds — the same source `ciao setup` uses.
    stock_workspace = resources.files("ciao.stock").joinpath("workspace")
    merged_agents_guide = False
    for root, ws_name in root_targets:
        _copy_tree_if_missing(stock_workspace, root)
        for memory_path, _title in _workspace_memory_paths(
            config, root, vault, workspace=ws_name
        ):
            _write_if_missing(
                memory_path, "# Memory\n\nDurable workspace memory lives here.\n"
            )
        for asset_dir in ("subagents", "commands"):
            (root / asset_dir).mkdir(parents=True, exist_ok=True)
        merged_agents_guide = _merge_agents_into_claude(root) or merged_agents_guide
        sync_workspace_skills(root, refresh_upstream=False)
    health = workspace_health(config)
    if merged_agents_guide:
        health["merged_agents_guide"] = True
    return health


async def workspace_health_fix_endpoint(request: Request) -> JSONResponse:
    """One-click remedy behind the Settings 'Fix issues' button."""
    try:
        return JSONResponse(repair_workspace_health(request.app.state.config))
    except Exception:  # noqa: BLE001
        logger.exception("workspace health fix failed")
        return JSONResponse({"error": "failed to repair workspace"}, status_code=500)


async def os_audit_endpoint(request: Request) -> JSONResponse:
    """GET /api/agent-assets/audit — Return AI OS health and context audit report."""
    from ciao.os_audit import run_os_audit

    config = request.app.state.config
    try:
        workspace_root = Path(config.workspace_root)
        state_path = Path(
            getattr(config, "state_path", workspace_root / ".runtime" / "state.json")
        )
        report = run_os_audit(
            workspace_dir=workspace_root,
            vault_root=Path(config.vault_root),
            runtime_dir=state_path.parent,
            proposal_paths=[
                path
                for path, _title in _memory_proposal_paths(
                    config,
                    Path(config.vault_root),
                    workspace_root,
                )
            ],
            config=config,
        )
        return JSONResponse(report)
    except Exception:  # noqa: BLE001
        logger.exception("AI OS audit failed")
        return JSONResponse({"error": "failed to run AI OS audit"}, status_code=500)
