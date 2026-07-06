"""Install and mirror Ciaobot skills, commands, and agents."""

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

from ciao import skills_sync


@dataclass(frozen=True)
class SyncSkillsResult:
    custom_installed: int = 0
    custom_pruned: int = 0
    upstream_updated: int = 0
    upstream_pruned: int = 0
    agents_installed: int = 0
    agents_pruned: int = 0
    commands_installed: int = 0
    commands_pruned: int = 0
    stock_installed: int = 0
    stock_pruned: int = 0


# Marker dropped into skills copied from ciao.stock so stale copies can be
# pruned when the packaged set changes or a workspace skill overrides them.
STOCK_SKILL_MARKER = ".ciao-stock-skill"


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


def _install_stock_skills(workspace: Path) -> tuple[int, int]:
    """Copy packaged ``ciao.stock/skills`` into ``.claude/skills``.

    A workspace ``skills/<name>`` always wins over the packaged skill of the
    same name.  Copies carry ``STOCK_SKILL_MARKER`` so they are refreshed on
    every sync and pruned once they disappear from the package (or become
    shadowed by a workspace skill).
    """
    from importlib import resources

    claude_skills = workspace / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)
    custom_dir = workspace / "skills"

    try:
        stock_skills = resources.files("ciao.stock").joinpath("skills")
        entries = [entry for entry in stock_skills.iterdir() if entry.is_dir()]
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        entries = []

    installed = 0
    live: set[str] = set()
    for entry in sorted(entries, key=lambda item: item.name):
        if not entry.joinpath("SKILL.md").is_file():
            continue
        if (custom_dir / entry.name / "SKILL.md").is_file():
            continue  # workspace skill shadows the packaged one
        target = claude_skills / entry.name
        if target.is_symlink():
            continue  # user-managed link, leave it alone
        with resources.as_file(entry) as source:
            shutil.copytree(source, target, dirs_exist_ok=True)
        (target / STOCK_SKILL_MARKER).touch()
        live.add(entry.name)
        installed += 1

    pruned = 0
    for existing in _iter_entries(claude_skills):
        if existing.name in live or existing.is_symlink() or not existing.is_dir():
            continue
        if not (existing / STOCK_SKILL_MARKER).exists():
            continue
        shutil.rmtree(existing)
        pruned += 1
    return installed, pruned


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


def sync_workspace_skills(
    workspace: Path | str,
    *,
    refresh_upstream: bool = True,
    runner=subprocess.run,
) -> SyncSkillsResult:
    root = Path(workspace).expanduser().resolve()

    upstream_updated = 0
    upstream_pruned = 0
    if refresh_upstream:
        upstream_updated, upstream_pruned = _refresh_upstream_skills(root, runner=runner)

    stock_installed, stock_pruned = _install_stock_skills(root)
    custom_installed, custom_pruned = _rebuild_custom_skill_links(root)
    print(
        f"Skills: {stock_installed} stock skills installed ({stock_pruned} stale pruned), "
        f"{custom_installed} custom-skill symlinks rebuilt, "
        f"{custom_pruned} orphaned pruned."
    )
    agents_installed, agents_pruned = _mirror_dir_symlinks(
        root / "subagents",
        root / ".claude" / "agents",
        glob_pattern="*.md",
        prune_regular=False,
    )
    commands_installed, commands_pruned = _mirror_dir_symlinks(
        root / "commands",
        root / ".claude" / "commands",
        glob_pattern="*.md",
        prune_regular=False,
    )
    print(
        f"Skills: {agents_installed} agent links, {commands_installed} command links rebuilt; "
        f"{agents_pruned + commands_pruned} orphaned pruned."
    )

    return SyncSkillsResult(
        custom_installed=custom_installed,
        custom_pruned=custom_pruned,
        upstream_updated=upstream_updated,
        upstream_pruned=upstream_pruned,
        agents_installed=agents_installed,
        agents_pruned=agents_pruned,
        commands_installed=commands_installed,
        commands_pruned=commands_pruned,
        stock_installed=stock_installed,
        stock_pruned=stock_pruned,
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
        "--skip-upstream",
        action="store_true",
        help="Skip skills-lock.json remote refresh and only mirror local catalogs.",
    )
    parser.add_argument("--verbose", action="store_true", help="Accepted for script compatibility.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    sync_workspace_skills(
        args.workspace,
        refresh_upstream=not args.skip_upstream,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
