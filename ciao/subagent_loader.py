"""Subagent package loader supporting legacy .md and folder-based packages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ciao.web.commands import _FRONTMATTER_RE, _parse_frontmatter

_NAME_VALIDATOR = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(slots=True)
class SubagentManifest:
    """Parsed representation of a workspace subagent."""

    name: str
    description: str
    instructions: str
    path: Path
    is_folder: bool
    tools: list[str] = field(default_factory=list)
    resources: list[Path] = field(default_factory=list)


def parse_subagent_file(path: Path) -> SubagentManifest | None:
    """Parse a single-file markdown subagent definition."""
    if not path.is_file() or path.suffix != ".md":
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    frontmatter = _parse_frontmatter(text)
    body = _FRONTMATTER_RE.sub("", text, count=1).strip()
    name = path.stem
    description = frontmatter.get("description", "").strip() or f"Ciaobot {name} role"

    return SubagentManifest(
        name=name,
        description=description,
        instructions=body,
        path=path,
        is_folder=False,
    )


def parse_subagent_folder(folder: Path) -> SubagentManifest | None:
    """Parse a folder-based subagent package."""
    if not folder.is_dir():
        return None

    # Check for instructions file: SKILL.md, INSTRUCTIONS.md, or <folder.name>.md
    main_file = None
    for candidate in ("SKILL.md", "INSTRUCTIONS.md", f"{folder.name}.md"):
        target = folder / candidate
        if target.is_file():
            main_file = target
            break

    if not main_file:
        return None

    try:
        text = main_file.read_text(encoding="utf-8")
    except OSError:
        return None

    frontmatter = _parse_frontmatter(text)
    body = _FRONTMATTER_RE.sub("", text, count=1).strip()
    name = frontmatter.get("name", "").strip() or folder.name
    description = frontmatter.get("description", "").strip() or f"Ciaobot {name} subagent package"

    tools: list[str] = []
    tools_dir = folder / "tools"
    if tools_dir.is_dir():
        for t in tools_dir.iterdir():
            if t.is_file() and not t.name.startswith("."):
                tools.append(t.name)

    resources: list[Path] = []
    res_dir = folder / "resources"
    if res_dir.is_dir():
        for r in res_dir.iterdir():
            if r.is_file() and not r.name.startswith("."):
                resources.append(r)

    return SubagentManifest(
        name=name,
        description=description,
        instructions=body,
        path=folder,
        is_folder=True,
        tools=sorted(tools),
        resources=sorted(resources, key=lambda p: p.name),
    )


def scaffold_subagent(workspace: Path, name: str) -> Path:
    """Scaffold a new folder-based subagent under `<workspace>/subagents/<name>/`."""
    if not _NAME_VALIDATOR.fullmatch(name):
        raise ValueError(f"Invalid subagent name '{name}': must be alphanumeric, hyphen, or underscore.")

    folder = workspace / "subagents" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "scripts").mkdir(exist_ok=True)
    (folder / "resources").mkdir(exist_ok=True)

    skill_file = folder / "SKILL.md"
    if not skill_file.exists():
        content = (
            "---\n"
            f"name: {name}\n"
            f"description: Subagent role package for {name}.\n"
            "---\n\n"
            f"# {name.capitalize()} Subagent Role\n\n"
            "Describe the background, instructions, and rules for this subagent.\n"
        )
        skill_file.write_text(content, encoding="utf-8")

    return folder
