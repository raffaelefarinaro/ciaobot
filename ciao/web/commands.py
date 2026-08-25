"""Slash-command discovery for the PWA.

Scans project-level (``.claude/commands/``) and user-level
(``~/.claude/commands/``) markdown files and returns a small JSON list
the frontend can render as a picker.

Keeps the result shape flat so the Vue side can render without a schema
library: ``{name, description, argument_hint, source, path}``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao import provider_registry

from ciao.skills_inventory import build_skill_inventory

logger = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_PROVIDER_SKILLS_TTL_SECONDS = 60.0
_provider_skills_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


@dataclass(slots=True)
class Command:
    name: str
    description: str
    argument_hint: str
    source: str  # "project", "user", or "skill"
    path: str


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Tiny YAML-ish frontmatter parser: `key: value` per line, no nesting."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _iter_command_files(root: Path, source: str) -> Iterable[Command]:
    if not root.exists() or not root.is_dir():
        return
    for md_path in sorted(root.glob("*.md")):
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        name = md_path.stem
        yield Command(
            name=name,
            description=fm.get("description", "").strip(),
            argument_hint=fm.get("argument-hint", "").strip(),
            source=source,
            path=str(md_path),
        )


def list_commands(workspace_root: Path) -> list[Command]:
    """Merge project + user commands; project wins on name collision."""
    project = {c.name: c for c in _iter_command_files(workspace_root / ".claude" / "commands", "project")}
    user = {c.name: c for c in _iter_command_files(Path.home() / ".claude" / "commands", "user")}
    merged: dict[str, Command] = {}
    merged.update(user)
    merged.update(project)  # project overrides on collision
    return sorted(merged.values(), key=lambda c: c.name)


def _provider_command_dir(workspace_root: Path, provider: str) -> Path | None:
    """Native command directory a provider's own CLI loads, if it has one.

    Claude reads ``.claude/commands/`` (already the canonical Ciaobot source),
    and opencode reads ``.opencode/commands/``.
    """
    target = provider.strip().lower()
    if target == "opencode":
        return workspace_root / ".opencode" / "commands"
    if target == "claude":
        return workspace_root / ".claude" / "commands"
    return None


def list_provider_command_entries(workspace_root: Path, provider: str) -> list[Command]:
    """Commands the provider's own CLI loads from its native command dir."""
    root = _provider_command_dir(workspace_root, provider)
    if root is None:
        return []
    return list(_iter_command_files(root, "provider"))


def list_skill_entries(workspace_root: Path, provider: str = "") -> list[Command]:
    """Return skills installed for a provider in the slash-picker shape.

    Skills are not commands on disk, but Claude and opencode both expose
    provider-installed skills as user-invocable slash entries. Keep the
    picker limited to the target that can actually load the skill.
    """
    target = provider.strip().lower()
    inventory = build_skill_inventory(workspace_root, include_content=False)
    entries: list[Command] = []
    for skill in inventory.get("skills", []):
        if not isinstance(skill, dict):
            continue
        targets = skill.get("installed_targets") or []
        if target and target not in targets:
            continue
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        entries.append(
            Command(
                name=name,
                description=str(skill.get("description") or "").strip(),
                argument_hint="",
                source="skill",
                path=str(skill.get("source") or ""),
            )
        )
    return sorted(entries, key=lambda item: item.name)


def _discover_provider_skill_names(provider: str) -> list[str]:
    """Use the same provider-owned skill discovery shown in Settings."""
    target = provider.strip().lower()
    now = time.monotonic()
    cached = _provider_skills_cache.get(target)
    if cached is not None and now - cached[0] < _PROVIDER_SKILLS_TTL_SECONDS:
        return list(cached[1])

    descriptor = provider_registry.get(target)
    names = descriptor.system_skills() if descriptor is not None else []

    normalized = tuple(sorted({str(name).strip() for name in names if str(name).strip()}))
    _provider_skills_cache[target] = (now, normalized)
    return list(normalized)


def list_provider_skill_entries(provider: str, names: Iterable[str]) -> list[Command]:
    """Render provider-owned skills in the slash-picker command shape."""
    target = provider.strip().lower()
    descriptor = provider_registry.get(target)
    provider_label = (
        descriptor.cli_label if descriptor is not None else (target or "provider")
    )
    entries = {
        str(name).strip(): Command(
            name=str(name).strip(),
            description=f"Loaded by {provider_label}",
            argument_hint="",
            source="skill",
            path=f"provider:{target}",
        )
        for name in names
        if str(name).strip()
    }
    return sorted(entries.values(), key=lambda item: item.name)


def list_picker_entries(workspace_root: Path, provider: str) -> tuple[list[Command], list[Command]]:
    """Return commands plus deduplicated workspace/provider skill entries.

    Commands merge the canonical Ciaobot commands (``.claude/commands/``) with
    the provider's own native command dir (e.g. ``.opencode/commands/``), so
    each provider's own slash entries surface in the picker too.
    """
    commands = list_commands(workspace_root)
    seen = {command.name.casefold() for command in commands}
    for command in list_provider_command_entries(workspace_root, provider):
        key = command.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        commands.append(command)
    commands.sort(key=lambda item: item.name.casefold())

    skills: list[Command] = []
    candidates = [
        *list_skill_entries(workspace_root, provider),
        *list_provider_skill_entries(provider, _discover_provider_skill_names(provider)),
    ]
    for skill in candidates:
        key = skill.name.casefold()
        if key in seen:
            continue
        seen.add(key)
        skills.append(skill)
    return commands, sorted(skills, key=lambda item: item.name.casefold())


def _workspace_root(request: Request) -> Path:
    """Resolve the agent root for the picker's caller.

    ``config.workspace_root`` is the bare install root, which after the
    per-workspace re-rooting migration holds none of a workspace's own
    ``skills/`` — those moved to ``agent_root(name)``. The picker must match
    the root a chat's own provider CLI actually runs from
    (see ``ProjectChatService._agent_root_for_chat``), or workspace-local
    skills silently vanish from the slash picker.
    """
    config = request.app.state.config
    workspace = request.query_params.get("workspace", "") or config.primary_workspace()
    if workspace and config.workspace(workspace):
        return Path(config.agent_root(workspace))
    return Path(config.workspace_root)


async def list_commands_endpoint(request: Request) -> JSONResponse:
    """GET /api/commands — return available slash commands for the UI picker."""
    try:
        workspace_root = _workspace_root(request)
        provider = request.query_params.get("provider", "")
        commands, skills = await asyncio.to_thread(
            list_picker_entries, workspace_root, provider
        )
    except Exception:  # noqa: BLE001 — never 500 the picker
        logger.exception("listing commands failed")
        return JSONResponse({"commands": [], "skills": []})
    return JSONResponse({
        "commands": [asdict(command) for command in commands],
        "skills": [asdict(skill) for skill in skills],
    })


async def rate_limits_endpoint(request: Request) -> JSONResponse:
    """GET /api/rate-limits — return the per-bucket subscription-limit snapshot.

    Data is reactive: we only know a bucket's state once the SDK has emitted
    a ``RateLimitEvent`` for it, so the payload starts empty after a fresh
    deploy and fills in as turns happen.
    """
    from ciao.rate_limits import KNOWN_BUCKETS, RateLimitStore, default_store_path

    try:
        config = request.app.state.config
        runtime_root = Path(config.state_path).parent
        store = RateLimitStore(path=default_store_path(runtime_root))
        payload = store.load()
    except Exception:  # noqa: BLE001 — never 500 the settings page
        logger.exception("reading rate_limits.json failed")
        payload = {"buckets": {}, "last_updated": None}
    # Surface the canonical bucket order so the Vue side can iterate deterministically.
    payload["known_buckets"] = list(KNOWN_BUCKETS)
    return JSONResponse(payload)
