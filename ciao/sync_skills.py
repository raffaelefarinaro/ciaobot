"""Install and mirror Ciao skills, commands, and agents."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ciao import skills_sync, sync_agents_to_pi


@dataclass(frozen=True)
class SyncSkillsResult:
    custom_installed: int = 0
    custom_pruned: int = 0
    upstream_updated: int = 0
    upstream_pruned: int = 0
    pi_skills_linked: int = 0
    pi_skills_pruned: int = 0
    pi_prompts_linked: int = 0
    pi_prompts_pruned: int = 0
    pi_agents_written: int = 0
    pi_agents_pruned: int = 0
    pi_agents_sources: int = 0


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _is_custom_skill_link(path: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        target = os.readlink(path)
    except OSError:
        return False
    return f"/skills/{path.name}" in target


def _ensure_symlink(source: Path, link: Path, *, relative_to: Path | None = None) -> bool:
    source = source.resolve()
    if link.is_symlink():
        try:
            if link.resolve() == source:
                return True
        except FileNotFoundError:
            pass
        link.unlink()
    elif link.exists():
        _remove_path(link)

    link.parent.mkdir(parents=True, exist_ok=True)
    target: Path | str = source
    if relative_to is not None:
        target = os.path.relpath(source, relative_to)
    link.symlink_to(target)
    return True


def _iter_entries(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(path.iterdir(), key=lambda entry: entry.name)


def _refresh_upstream_skills(
    workspace: Path,
    *,
    runner=subprocess.run,
) -> tuple[int, int]:
    lockfile = workspace / "skills-lock.json"
    claude_skills = workspace / ".claude" / "skills"
    if os.environ.get("CIAO_AUTO_UPDATE_GITHUB_SKILLS", "true").strip().lower() in {"0", "false", "no", "off"}:
        print("Skills: automatic GitHub updates disabled, skipping refresh.")
        return 0, 0
    if not lockfile.is_file():
        print(f"Skills: {lockfile} missing, skipping upstream refresh.")
        return 0, 0
    if shutil.which("npx") is None or shutil.which("git") is None:
        print("WARN: npx or git not found, skipping upstream skill refresh", file=sys.stderr)
        return 0, 0

    cache_path = workspace / ".runtime" / "skills-sync-cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    installed = [
        entry.name
        for entry in _iter_entries(claude_skills)
        if not _is_custom_skill_link(entry)
    ]
    lock = _load_json(lockfile)
    cache = _load_json(cache_path)
    heads = skills_sync.remote_heads(set(skills_sync.desired_sources(lock).values()))
    plan = skills_sync.plan(lock, cache, heads, installed)

    for name in plan["to_prune"]:
        _remove_path(claude_skills / name)

    if plan["to_update"]:
        print("Skills: updating changed -> " + " ".join(plan["to_update"]))
        try:
            result = runner(
                ["npx", "-y", "skills", "update", "-p", "-y", *plan["to_update"]],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                print("WARN: skills update reported errors", file=sys.stderr)
        except OSError as exc:
            print(f"WARN: skills update failed: {exc}", file=sys.stderr)
    else:
        print("Skills: upstream unchanged, no fetch needed.")

    cache_path.write_text(
        json.dumps(skills_sync.build_cache(lock, heads, cache), indent=2),
        encoding="utf-8",
    )
    return len(plan["to_update"]), len(plan["to_prune"])


def _rebuild_custom_skill_links(workspace: Path) -> tuple[int, int]:
    custom_dir = workspace / "skills"
    claude_skills = workspace / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)

    installed = 0
    if custom_dir.is_dir():
        for skill_dir in sorted(custom_dir.iterdir(), key=lambda entry: entry.name):
            if not skill_dir.is_dir():
                continue
            if not (skill_dir / "SKILL.md").is_file() or (skill_dir / "SKILL.md").stat().st_size == 0:
                continue
            target = claude_skills / skill_dir.name
            _ensure_symlink(skill_dir, target, relative_to=claude_skills)
            installed += 1

    pruned = 0
    for target in _iter_entries(claude_skills):
        if not target.is_symlink() or target.exists():
            continue
        try:
            current = os.readlink(target)
        except OSError:
            continue
        if "/skills/" not in current:
            continue
        target.unlink(missing_ok=True)
        pruned += 1
    return installed, pruned


def _mirror_dir_symlinks(
    source_dir: Path,
    dest_dir: Path,
    *,
    glob_pattern: str = "*",
    prune_regular: bool,
) -> tuple[int, int]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    linked = 0
    live: set[str] = set()
    if source_dir.is_dir():
        for entry in sorted(source_dir.glob(glob_pattern), key=lambda item: item.name):
            if not entry.exists():
                if entry.is_symlink():
                    print(
                        f"WARN: skipping dangling symlink {entry} -> {os.readlink(entry)}",
                        file=sys.stderr,
                    )
                continue
            live.add(entry.name)
            _ensure_symlink(entry, dest_dir / entry.name)
            linked += 1

    pruned = 0
    for entry in _iter_entries(dest_dir):
        if entry.name in live:
            continue
        if not prune_regular and not entry.is_symlink():
            continue
        _remove_path(entry)
        pruned += 1
    return linked, pruned


def _agent_source_dir(workspace: Path) -> Path:
    subagents = workspace / "subagents"
    if subagents.is_dir():
        return subagents
    return workspace / ".claude" / "agents"


def sync_workspace_skills(
    workspace: Path | str,
    *,
    pi_root: Path | str | None = None,
    refresh_upstream: bool = True,
    runner=subprocess.run,
) -> SyncSkillsResult:
    root = Path(workspace).expanduser().resolve()
    pi_base = Path(pi_root).expanduser().resolve() if pi_root is not None else Path.home() / ".pi" / "agent"

    upstream_updated = 0
    upstream_pruned = 0
    if refresh_upstream:
        upstream_updated, upstream_pruned = _refresh_upstream_skills(root, runner=runner)

    custom_installed, custom_pruned = _rebuild_custom_skill_links(root)
    print(
        f"Skills: {custom_installed} custom-skill symlinks rebuilt, "
        f"{custom_pruned} orphaned pruned."
    )

    pi_skills_linked, pi_skills_pruned = _mirror_dir_symlinks(
        root / ".claude" / "skills",
        pi_base / "skills",
        prune_regular=True,
    )
    print(f"Pi skills mirror: {pi_skills_linked} linked, {pi_skills_pruned} pruned.")

    pi_prompts_linked, pi_prompts_pruned = _mirror_dir_symlinks(
        root / ".claude" / "commands",
        pi_base / "prompts",
        glob_pattern="*.md",
        prune_regular=False,
    )
    print(f"Pi prompts mirror: {pi_prompts_linked} linked, {pi_prompts_pruned} pruned.")

    agent_result = sync_agents_to_pi.sync(_agent_source_dir(root), pi_base / "agents")
    return SyncSkillsResult(
        custom_installed=custom_installed,
        custom_pruned=custom_pruned,
        upstream_updated=upstream_updated,
        upstream_pruned=upstream_pruned,
        pi_skills_linked=pi_skills_linked,
        pi_skills_pruned=pi_skills_pruned,
        pi_prompts_linked=pi_prompts_linked,
        pi_prompts_pruned=pi_prompts_pruned,
        pi_agents_written=agent_result.written,
        pi_agents_pruned=agent_result.pruned,
        pi_agents_sources=agent_result.sources,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root. Defaults to current directory.",
    )
    parser.add_argument(
        "--pi-root",
        type=Path,
        default=None,
        help="Pi agent root. Defaults to ~/.pi/agent.",
    )
    parser.add_argument(
        "--skip-upstream",
        action="store_true",
        help="Skip skills-lock.json remote refresh and only mirror local catalogs.",
    )
    parser.add_argument("--verbose", action="store_true", help="Accepted for script compatibility.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    sync_workspace_skills(
        args.workspace,
        pi_root=args.pi_root,
        refresh_upstream=not args.skip_upstream,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
