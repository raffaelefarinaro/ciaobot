"""Slash-command discovery for the PWA.

Scans project-level (``.claude/commands/``) and user-level
(``~/.claude/commands/``) markdown files and returns a small JSON list
the frontend can render as a picker.

Keeps the result shape flat so the Vue side can render without a schema
library: ``{name, description, argument_hint, source, path}``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass(slots=True)
class Command:
    name: str
    description: str
    argument_hint: str
    source: str  # "project" or "user"
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


def expand_slash_command(prompt: str, workspace_root: Path) -> str | None:
    """Expand a Ciaobot command for providers without native project commands.

    Returns ``None`` when the prompt is not a known ``/command``. The marker
    keeps the original input recoverable when Codex thread history is rendered
    back into the PWA.
    """
    stripped = prompt.lstrip()
    match = re.match(r"^/([A-Za-z0-9._-]+)(?:\s+([\s\S]*))?$", stripped)
    if match is None:
        return None
    name = match.group(1)
    command = next(
        (item for item in list_commands(workspace_root) if item.name == name),
        None,
    )
    if command is None:
        return None
    try:
        template = Path(command.path).read_text(encoding="utf-8")
    except OSError:
        return None
    template = _FRONTMATTER_RE.sub("", template, count=1).strip()
    arguments = (match.group(2) or "").strip()
    rendered = template.replace("$ARGUMENTS", arguments)
    import json

    return (
        "[CIAO_COMMAND_BEGIN]\n"
        f"command=/{name}\n"
        f"user_input_json={json.dumps(prompt, ensure_ascii=False)}\n"
        "[CIAO_COMMAND_INSTRUCTIONS]\n"
        f"{rendered}\n"
        "[CIAO_COMMAND_END]"
    )


def _workspace_root(request: Request) -> Path:
    config = request.app.state.config
    return Path(config.workspace_root)


async def list_commands_endpoint(request: Request) -> JSONResponse:
    """GET /api/commands — return available slash commands for the UI picker."""
    try:
        commands = list_commands(_workspace_root(request))
    except Exception:  # noqa: BLE001 — never 500 the picker
        logger.exception("listing commands failed")
        return JSONResponse({"commands": []})
    return JSONResponse({"commands": [asdict(c) for c in commands]})


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
