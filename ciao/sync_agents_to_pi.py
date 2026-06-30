"""Convert Ciao subagents into Pi-compatible agent files."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANAGED_MARKER = "# ciao-managed: do not edit, regenerated from subagents/"

TOOL_MAP: dict[str, list[str]] = {
    "Read": ["read"],
    "Grep": ["grep"],
    "Glob": ["find", "ls"],
    "Bash": ["bash"],
    "Edit": ["edit"],
    "Write": ["write"],
    "WebFetch": [],
    "WebSearch": [],
    "Agent": [],
    "TodoWrite": [],
}
CLAUDE_ONLY_FIELDS = frozenset({"skills", "memory"})
DEFAULT_PI_TOOLS = ["read", "grep", "find", "ls", "bash", "edit", "write"]


@dataclass(frozen=True)
class AgentSyncResult:
    written: int
    pruned: int
    sources: int


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter, body)`` for Claude's simple markdown format."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    header = text[4:end]
    body = text[end + 5 :]
    fm: dict[str, Any] = {}
    current_key: str | None = None
    list_buf: list[str] = []
    for raw in header.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("  - ") or line.startswith("- "):
            list_buf.append(line.lstrip("- ").strip())
            continue
        if current_key is not None and list_buf:
            fm[current_key] = list_buf
            list_buf = []
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            current_key = key
            list_buf = []
            continue
        current_key = None
        fm[key] = value
    if current_key is not None and list_buf:
        fm[current_key] = list_buf
    return fm, body


def translate_tools(claude_tools: str | list[str] | None) -> list[str]:
    if not claude_tools:
        return []
    if isinstance(claude_tools, list):
        names = [tool.strip() for tool in claude_tools if tool.strip()]
    else:
        names = [tool.strip() for tool in claude_tools.split(",") if tool.strip()]
    out: list[str] = []
    for name in names:
        pi_names = TOOL_MAP.get(name)
        if pi_names is None:
            print(f"  warn: unknown Claude tool {name!r}, dropping", file=sys.stderr)
            continue
        for pi_name in pi_names:
            if pi_name not in out:
                out.append(pi_name)
    return out


def render_pi_frontmatter(claude_fm: dict[str, Any], source_name: str) -> str:
    name = claude_fm.get("name") or source_name
    description = claude_fm.get("description", "")
    pi_tools = (
        translate_tools(claude_fm.get("tools"))
        if "tools" in claude_fm
        else list(DEFAULT_PI_TOOLS)
    )

    lines = ["---", f"name: {name}"]
    if description:
        lines.append(f"description: {description}")
    if pi_tools:
        lines.append("tools: " + ", ".join(pi_tools))
    lines.append("skills: false")
    lines.append("prompt_mode: replace")
    lines.append("---")
    return "\n".join(lines) + "\n"


def convert(source: Path) -> str:
    text = source.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    for key in CLAUDE_ONLY_FIELDS:
        fm.pop(key, None)
    header = render_pi_frontmatter(fm, source.stem)
    return f"{header}\n{MANAGED_MARKER}\n{body.lstrip()}"


def sync(claude_dir: Path, pi_dir: Path, *, dry_run: bool = False) -> AgentSyncResult:
    pi_dir.mkdir(parents=True, exist_ok=True)
    if not claude_dir.is_dir():
        print(f"No agents dir at {claude_dir}; nothing to mirror.", file=sys.stderr)
        pruned = _prune_managed_agents(pi_dir, seen=set(), dry_run=dry_run)
        return AgentSyncResult(written=0, pruned=pruned, sources=0)
    written = 0
    seen: set[str] = set()

    for src in sorted(claude_dir.glob("*.md")):
        out_path = pi_dir / src.name
        seen.add(src.name)
        rendered = convert(src)
        if out_path.exists() and out_path.read_text(encoding="utf-8") == rendered:
            continue
        if dry_run:
            print(f"  would write {out_path}")
        else:
            out_path.write_text(rendered, encoding="utf-8")
        written += 1

    pruned = _prune_managed_agents(pi_dir, seen=seen, dry_run=dry_run)

    print(f"Pi agents mirror: {written} written, {pruned} pruned, {len(seen)} sources.")
    return AgentSyncResult(written=written, pruned=pruned, sources=len(seen))


def _prune_managed_agents(pi_dir: Path, *, seen: set[str], dry_run: bool) -> int:
    pruned = 0
    for existing in pi_dir.glob("*.md"):
        if existing.name in seen:
            continue
        try:
            text = existing.read_text(encoding="utf-8")
        except OSError:
            continue
        if MANAGED_MARKER not in text:
            continue
        if dry_run:
            print(f"  would prune {existing}")
        else:
            existing.unlink()
        pruned += 1
    return pruned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claude-dir",
        default=str(Path.cwd() / "subagents"),
        help="Source dir of agent *.md files.",
    )
    parser.add_argument(
        "--pi-dir",
        default=os.path.expanduser("~/.pi/agent/agents"),
        help="Destination dir for Pi agents.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    sync(Path(args.claude_dir), Path(args.pi_dir), dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
