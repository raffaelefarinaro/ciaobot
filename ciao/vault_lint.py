"""Vault hygiene linter logic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Match [[Target]], ignoring optional #anchors and |labels
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

# Fenced code blocks and inline code spans are prose *about* wikilinks, not
# real links — guides and changelogs routinely document `[[wikilink]]` syntax.
# Strip them before extracting links so documented syntax isn't flagged.
_FENCE_RE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_FRONTMATTER_EXEMPT = {"index.md", "memory.md", "log.md"}


@dataclass(frozen=True)
class _VaultFile:
    path: Path
    relative: Path
    content: str

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


def _workspace_dirs(vault_root: Path) -> list[Path]:
    """Vault subdirectories that look like a workspace (they hold a MEMORY.md).

    Discovered from the layout rather than read from config: the linter runs as
    a standalone script with no server config to consult.
    """
    try:
        return [
            entry
            for entry in vault_root.iterdir()
            if entry.is_dir() and (entry / "MEMORY.md").is_file()
        ]
    except OSError:
        return []


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


def _frontmatter_error(file: _VaultFile) -> dict[str, str] | None:
    if file.relative.name.lower() in _FRONTMATTER_EXEMPT:
        return None

    match = _FRONTMATTER_RE.match(file.content)
    if match is None:
        return {
            "source": file.relative.as_posix(),
            "kind": "missing_frontmatter",
            "message": "frontmatter is missing",
        }

    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        metadata = None
    if not isinstance(metadata, dict):
        return {
            "source": file.relative.as_posix(),
            "kind": "malformed_frontmatter",
            "message": "frontmatter is malformed",
        }

    page_type = metadata.get("type")
    if page_type is None or (isinstance(page_type, str) and not page_type.strip()):
        return {
            "source": file.relative.as_posix(),
            "kind": "missing_type",
            "message": "frontmatter type is missing or empty",
        }
    if not isinstance(page_type, str):
        return {
            "source": file.relative.as_posix(),
            "kind": "invalid_type",
            "message": "frontmatter type must be a string",
        }
    return None


def run_validation(vault_root: Path) -> dict:
    """Scan the vault directory for actionable health findings."""
    issues: dict[str, list[Any]] = {
        "broken_links": [],
        "orphans": [],
        "duplicates": [],
        "frontmatter_errors": [],
        "broken_markdown_links": [],
    }

    vault_files: list[_VaultFile] = []
    for path in sorted(vault_root.rglob("*")):
        if path.suffix.lower() != ".md":
            continue
        try:
            rel = path.relative_to(vault_root)
        except ValueError:
            continue

        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        vault_files.append(_VaultFile(path=path, relative=rel, content=content))

    for file in vault_files:
        error = _frontmatter_error(file)
        if error is not None:
            issues["frontmatter_errors"].append(error)

    valid_targets = set()
    files_to_scan: list[_VaultFile] = []
    incoming_links: dict[str, list[str]] = {}

    link_target_files = [
        file
        for file in vault_files
        if file.relative.name not in {"INDEX.md", "MEMORY.md"}
    ]
    files_to_scan = [
        file
        for file in link_target_files
        if not _is_template(file.path.stem)
    ]

    normalized_names: dict[str, list[str]] = {}

    for file in link_target_files:
        target_stem = file.path.stem
        target_rel = file.relative.with_suffix("").as_posix()
        valid_targets.add(target_stem)
        valid_targets.add(target_rel)

        # Template files contain placeholder links by design; keep them as
        # valid link targets but don't scan them as a source of broken links.
        incoming_links[target_stem] = []
        incoming_links[target_rel] = []

        # Duplicate detection: skip common structural stems (README/log/etc.)
        # that legitimately repeat per folder, and template files.
        if target_stem.lower() not in _COMMON_STEMS and not _is_template(target_stem):
            norm = target_stem.lower().replace("-", "").replace("_", "")
            normalized_names.setdefault(norm, []).append(file.relative.as_posix())

    for norm, paths in normalized_names.items():
        if len(paths) > 1:
            issues["duplicates"].append(paths)

    for file in files_to_scan:
        rel_str = file.relative.as_posix()
        for target in _links_in(file.content):
            if target in valid_targets:
                incoming_links.setdefault(target, []).append(rel_str)
            else:
                issues["broken_links"].append({
                    "source": rel_str,
                    "target": target
                })

    # Check for memory files links (roots). Every workspace subdirectory that
    # has a MEMORY.md counts — hardcoding personal/work meant a differently
    # named workspace's root links were never checked, so everything they
    # referenced looked like an orphan.
    memory_roots = sorted(
        entry / "MEMORY.md"
        for entry in _workspace_dirs(vault_root)
    )
    memory_links = set()
    records_by_path = {file.path: file for file in vault_files}
    for mem_file in memory_roots:
        record = records_by_path.get(mem_file)
        if record is not None:
            for target in _links_in(record.content):
                memory_links.add(target)

    orphan_candidate_dirs = {"People", "Projects", "Ideas", "Resources", "Places", "projects", "references"}

    for file in files_to_scan:
        stem = file.path.stem
        rel_path = file.relative
        rel_no_sfx = rel_path.with_suffix("").as_posix()
        rel_str = file.relative.as_posix()

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
