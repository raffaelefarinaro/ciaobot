"""Instruction, command, and subagent assets for the Settings UI."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.memory_injector import build_memory_block, system_prompt_payload
from ciao.observability.hooks import _runtime_lines
from ciao.sync_skills import sync_workspace_skills
from ciao.web.commands import _parse_frontmatter

logger = logging.getLogger(__name__)

_ASSET_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_IMPORT_RE = re.compile(r"(?<!\w)@([^\s\)\]>,]+\.md)")


@dataclass(slots=True)
class PromptAsset:
    id: str
    title: str
    description: str
    source: str
    path: str
    editable: bool
    content: str
    scope: str = ""
    parent_id: str = ""
    level: int = 0
    status: str = "ok"
    imports: list[str] = field(default_factory=list)


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


def _asset_id(path: Path, root: Path) -> str:
    return "file:" + _relative_or_absolute(path, root)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_editable_file(path: Path, config: Any) -> bool:
    return _is_under(path, Path(config.workspace_root)) or _is_under(path, Path(config.vault_root))


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


def _vault_mirror_path(config: Any, category: str, name: str) -> Path:
    return Path(config.vault_root) / "Workspace" / category / f"{name}.md"


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


def _resolve_import(raw: str, *, base: Path) -> Path:
    value = raw.strip()
    if value.startswith("@"):
        value = value[1:]
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (base.parent / candidate).resolve()


def _prompt_file_asset(
    *,
    path: Path,
    config: Any,
    title: str,
    description: str,
    scope: str,
    source: str = "file",
    parent_id: str = "",
    level: int = 0,
    status: str = "ok",
    content: str | None = None,
) -> PromptAsset:
    root = Path(config.workspace_root)
    text = _read_text(path) if content is None and path.exists() else (content or "")
    return PromptAsset(
        id=_asset_id(path, root),
        title=title,
        description=description,
        source=source,
        path=_relative_or_absolute(path, root),
        editable=path.exists() and _is_editable_file(path, config),
        content=text,
        scope=scope,
        parent_id=parent_id,
        level=level,
        status=status,
        imports=_IMPORT_RE.findall(text),
    )


def _collect_import_assets(
    *,
    parent: PromptAsset,
    parent_path: Path,
    config: Any,
    seen: set[Path],
    depth: int = 0,
) -> list[PromptAsset]:
    if depth >= 4:
        return []
    root = Path(config.workspace_root)
    allowed_roots = [root, Path(config.vault_root), Path.home() / ".claude"]
    out: list[PromptAsset] = []
    for raw in parent.imports:
        path = _resolve_import(raw, base=parent_path)
        status = "ok"
        description = f"Imported by {parent.title}."
        content = None
        if not path.exists():
            status = "missing"
            description = f"Referenced by {parent.title}, but the file does not exist."
            content = ""
        elif not any(_is_under(path, allowed) for allowed in allowed_roots):
            status = "blocked"
            description = f"Referenced by {parent.title}, but outside the configured workspace/vault roots."
            content = ""
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        asset = _prompt_file_asset(
            path=path,
            config=config,
            title=f"Import: {path.name}",
            description=description,
            scope="import",
            source="file-import",
            parent_id=parent.id,
            level=parent.level + 1,
            status=status,
            content=content,
        )
        out.append(asset)
        if status == "ok":
            out.extend(_collect_import_assets(
                parent=asset,
                parent_path=path,
                config=config,
                seen=seen,
                depth=depth + 1,
            ))
    return out


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
    add("vault-root", "Vault root", "ok" if vault.is_dir() else "error", "Vault root exists." if vault.is_dir() else "Vault root is missing.", vault)
    add("vault-writable", "Vault writable", "ok" if os.access(vault, os.W_OK) else "error", "Vault is writable." if os.access(vault, os.W_OK) else "Vault is not writable.", vault)

    memory_paths = []
    ws_names = []
    if hasattr(config, "workspace_names") and callable(config.workspace_names):
        ws_names = config.workspace_names()

    if not ws_names:
        memory_paths.append((vault / "MEMORY.md", "Workspace MEMORY.md"))
    else:
        for name in ws_names:
            ws_config = config.workspace(name)
            raw_root = ws_config.vault_root if ws_config else name
            ws_vault_root = Path(raw_root).expanduser()
            if not ws_vault_root.is_absolute():
                if name in {"personal", "work"} and raw_root == name:
                    ws_vault_root = (vault / name).resolve()
                else:
                    ws_vault_root = (root / ws_vault_root).resolve()
            title = f"Workspace MEMORY.md ({name})" if len(ws_names) > 1 else "Workspace MEMORY.md"
            memory_paths.append((ws_vault_root / "MEMORY.md", title))

    check_paths = [(root / "CLAUDE.md", "Project CLAUDE.md")]
    check_paths.extend(memory_paths)
    check_paths.extend([
        (root / "subagents", "Canonical subagents directory"),
        (root / "commands", "Canonical commands directory"),
        (root / ".claude" / "agents", "Generated .claude agents directory"),
        (root / ".claude" / "commands", "Generated .claude commands directory"),
    ])

    for path, title in check_paths:
        exists = path.exists()
        add(
            f"path-{_relative_or_absolute(path, root)}",
            title,
            "ok" if exists else "warn",
            "Present." if exists else "Missing; Ciaobot can continue, but this workspace is less discoverable to Claude Code.",
            path,
            "Create it or run sync-skills." if not exists else "",
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
                    f"unsynced-{label}-{source.stem}",
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
    ]:
        if not link_dir.exists():
            continue
        for path in link_dir.rglob("*"):
            if path.is_symlink() and not path.exists():
                add(
                    f"broken-{label}-{path.name}",
                    f"Broken .claude {label} link",
                    "error",
                    "Generated Claude Code link points at a missing file.",
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

    for path in _iter_markdown_files(root / "subagents"):
        items.append(_agent_asset_from_file(
            path, root=root, source="workspace", scope="custom", editable=True, vault_root=vault_root,
        ))
    for path in _iter_markdown_files(root / ".claude" / "agents"):
        if (root / "subagents" / path.name).exists():
            continue
        items.append(_agent_asset_from_file(
            path, root=root, source="project", scope="installed", editable=False, vault_root=vault_root,
        ))
    for path in _iter_markdown_files(Path.home() / ".claude" / "agents"):
        items.append(_agent_asset_from_file(
            path, root=root, source="user", scope="global", editable=False, vault_root=vault_root,
        ))
    return sorted(_dedupe_by_name(items), key=lambda item: item.name)


def list_command_assets(config: Any) -> list[CommandAsset]:
    root = Path(config.workspace_root)
    vault_root = Path(config.vault_root)
    items: list[CommandAsset] = []

    for path in _iter_markdown_files(root / "commands"):
        items.append(_command_asset_from_file(
            path, root=root, source="workspace", scope="custom", editable=True, vault_root=vault_root,
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


def list_prompt_assets(config: Any) -> list[PromptAsset]:
    root = Path(config.workspace_root)
    prompts: list[PromptAsset] = []
    seen: set[Path] = set()

    memory_enabled = bool(getattr(config, "memory_enabled", True))
    memory_block = ""
    if memory_enabled:
        memory_block = build_memory_block(
            memory_char_limit=int(getattr(config, "memory_char_limit", 2200)),
            user_char_limit=int(getattr(config, "user_char_limit", 1800)),
        )
    system_prompt = system_prompt_payload(memory_block) or {}
    sys_prompt_asset = PromptAsset(
        id="ciaobot-system-prompt",
        title="Ciaobot system prompt append",
        description="Generated instructions appended to Claude Code's default preset at session start.",
        source="generated",
        path="",
        editable=False,
        content=str(system_prompt.get("append") or ""),
        scope="generated",
    )

    added_sys_prompt = False

    file_assets = [
        (Path.home() / ".claude" / "CLAUDE.md", "Claude Code global instructions", "User-level Claude Code instructions loaded before project files.", "global"),
        (root / "CLAUDE.md", "Claude Code project instructions", "Project-local Claude Code instructions loaded by the CLI.", "project"),
        (root / "CLAUDE.local.md", "Claude Code local instructions", "Machine-local project instructions when present.", "local"),
        (root / ".claude" / "CLAUDE.md", "Claude Code .claude instructions", "Project .claude instruction file when present.", "project"),
    ]

    ws_names = []
    if hasattr(config, "workspace_names") and callable(config.workspace_names):
        ws_names = config.workspace_names()

    if not ws_names:
        file_assets.append((Path(config.vault_root) / "MEMORY.md", "Workspace memory", "Durable workspace memory file under the configured vault root.", "vault"))
    else:
        for name in ws_names:
            ws_config = config.workspace(name)
            raw_root = ws_config.vault_root if ws_config else name
            ws_vault_root = Path(raw_root).expanduser()
            if not ws_vault_root.is_absolute():
                if name in {"personal", "work"} and raw_root == name:
                    ws_vault_root = (config.vault_root / name).resolve()
                else:
                    ws_vault_root = (root / ws_vault_root).resolve()

            title = f"Workspace memory ({name})" if len(ws_names) > 1 else "Workspace memory"
            file_assets.append((
                ws_vault_root / "MEMORY.md",
                title,
                f"Durable workspace memory file under the configured {name} vault root.",
                "vault"
            ))

    for path, title, description, scope in file_assets:
        if path == root / "CLAUDE.md" and not added_sys_prompt:
            prompts.append(sys_prompt_asset)
            added_sys_prompt = True

        if path.exists():
            seen.add(path.resolve())
            asset = _prompt_file_asset(
                path=path,
                config=config,
                title=title,
                description=description,
                scope=scope,
            )
            prompts.append(asset)
            prompts.extend(_collect_import_assets(
                parent=asset,
                parent_path=path,
                config=config,
                seen=seen,
            ))

    if not added_sys_prompt:
        prompts.append(sys_prompt_asset)

    runtime_preview = "<ciao-runtime>\n" + "\n".join(_runtime_lines(root, os.environ.copy())) + "\n</ciao-runtime>"
    prompts.append(PromptAsset(
        id="runtime-context-hook",
        title="Per-turn runtime context hook",
        description="Generated context injected before each user prompt; entity tags are added dynamically from vault matches.",
        source="generated",
        path="",
        editable=False,
        content=runtime_preview,
        scope="generated",
    ))
    return prompts


async def agent_assets_endpoint(request: Request) -> JSONResponse:
    """GET /api/agent-assets — Settings inventory for instructions, agents, and commands."""
    config = request.app.state.config
    try:
        return JSONResponse({
            "instructions": [asdict(item) for item in list_prompt_assets(config)],
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
