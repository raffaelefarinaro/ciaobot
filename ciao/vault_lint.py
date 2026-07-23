"""Vault hygiene linter logic."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Match [[Target]], ignoring optional #anchors and |labels
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

# Fenced code blocks and inline code spans are prose *about* wikilinks, not
# real links — guides and changelogs routinely document `[[wikilink]]` syntax.
# Strip them before extracting links so documented syntax isn't flagged.
_FENCE_RE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Common structural filenames that legitimately recur across folders (one
# README/log/etc. per project). Same stem across folders is not a duplicate.
_COMMON_STEMS = {
    "readme", "index", "log", "notes", "general", "overview",
    "changelog", "todo", "template",
}

# Directories that aren't vault content: app state, generated projections, tool
# caches, and any venv/node_modules checked out inside the vault root (#129).
EXCLUDE_DIRS = {
    "Logs", "Templates", ".obsidian",
    ".venv", "venv", "node_modules", ".git",
    ".claude", ".agents", ".codex", "__pycache__",
}


def _links_in(text: str):
    """Yield wikilink targets in ``text``, skipping code spans/fences,
    backslash-escaped brackets, and ``<placeholder>`` template syntax — none
    of which are real links."""
    stripped = _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", text))
    for m in WIKILINK_RE.finditer(stripped):
        if m.start() > 0 and stripped[m.start() - 1] == "\\":
            continue  # escaped \[[...]] — documenting the syntax
        target = m.group(1).strip()
        if "<" in target or ">" in target:
            continue  # placeholder like [[projects/active/<folder>/<folder>]]
        yield target


def _is_template(stem: str) -> bool:
    return "template" in stem.lower()


def run_validation(vault_root: Path) -> dict:
    """Scan the vault directory and find broken wikilinks, orphans, and duplicates."""
    issues: dict[str, list[Any]] = {
        "broken_links": [],
        "orphans": [],
        "duplicates": []
    }

    valid_targets = set()
    files_to_scan = []
    incoming_links: dict[str, list[str]] = {}

    # Exclude directories that aren't vault content (see EXCLUDE_DIRS / #129).
    exclude_dirs = EXCLUDE_DIRS
    exclude_files = {"INDEX.md", "MEMORY.md"}

    normalized_names: dict[str, list[str]] = {}

    for path in vault_root.rglob("*.md"):
        try:
            rel = path.relative_to(vault_root)
        except ValueError:
            continue

        if any(p in exclude_dirs for p in rel.parts):
            continue
        if rel.name in exclude_files:
            continue

        target_stem = path.stem
        target_rel = str(rel.with_suffix(""))
        valid_targets.add(target_stem)
        valid_targets.add(target_rel)

        # Template files contain placeholder links by design; keep them as
        # valid link targets but don't scan them as a source of broken links.
        if not _is_template(target_stem):
            files_to_scan.append((path, str(rel)))

        incoming_links[target_stem] = []
        incoming_links[target_rel] = []

        # Duplicate detection: skip common structural stems (README/log/etc.)
        # that legitimately repeat per folder, and template files.
        if target_stem.lower() not in _COMMON_STEMS and not _is_template(target_stem):
            norm = target_stem.lower().replace("-", "").replace("_", "")
            normalized_names.setdefault(norm, []).append(str(rel))

    for norm, paths in normalized_names.items():
        if len(paths) > 1:
            issues["duplicates"].append(paths)

    for path, rel_str in files_to_scan:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for target in _links_in(content):
            if target in valid_targets:
                incoming_links.setdefault(target, []).append(rel_str)
            else:
                issues["broken_links"].append({
                    "source": rel_str,
                    "target": target
                })

    # Check for memory files links (roots)
    memory_md = vault_root / "personal" / "MEMORY.md"
    memory_work_md = vault_root / "work" / "MEMORY.md"
    memory_links = set()
    for mem_file in (memory_md, memory_work_md):
        if mem_file.exists():
            try:
                mem_content = mem_file.read_text(encoding="utf-8")
                for target in _links_in(mem_content):
                    memory_links.add(target)
            except OSError:
                pass

    orphan_candidate_dirs = {"People", "Projects", "Ideas", "Resources", "Places", "projects", "references"}

    for path, rel_str in files_to_scan:
        stem = path.stem
        rel_path = Path(rel_str)
        rel_no_sfx = str(rel_path.with_suffix(""))

        if not any(part in orphan_candidate_dirs for part in rel_path.parts):
            continue

        has_incoming = False
        if incoming_links.get(stem) or incoming_links.get(rel_no_sfx):
            has_incoming = True
        if stem in memory_links or rel_no_sfx in memory_links:
            has_incoming = True

        if not has_incoming:
            issues["orphans"].append(rel_str)

    return issues
