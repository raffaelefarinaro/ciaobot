"""Survey a vault root without writing anything.

Why this exists
---------------
A later release moves every vault file into a per-workspace directory. That
migration is proved by fixtures, and fixtures only prove what they cover, so
the shapes the real vault contains have to be known before the fixtures are
written. This module turns a vault root into a test spec: every shape it
reports needs a fixture counterpart, and a reported shape with no fixture is
a known coverage hole. Writing an earlier draft of this census is what found
two gaps in the migration design before any migration code existed.

The census is deliberately read-only. It never writes, moves, renames, or
touches git. It does not follow symlinks when walking, so a symlink loop cannot
hang it; symlinks are reported, not traversed.

`scan_vault` in `vault_index` skips `Logs/`, `Templates/` and `.obsidian`
(see `EXCLUDED_TOP_DIRS`), but `Logs/` holds most of the notes in a real
vault, so this census walks those directories itself rather than building on
`scan_vault`. The only helpers reused are `FRONTMATTER_RE` (for the
no-frontmatter check) and `vault_workspaces` (the fallback registry when no
explicit workspace list is given).
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ciao.vault_links import FRONTMATTER_RE
from ciao.vault_rehome import vault_workspaces


@dataclass
class WorkspaceCensus:
    """The survey of one vault root, as a plain data structure.

    ``note_counts`` and ``non_md_counts`` are keyed by top-level directory
    name and count recursively under that directory. ``root_notes`` counts
    markdown files sitting directly in the vault root (generated files such as
    INDEX.md), which belong to no top-level directory. ``symlinks`` carries
    ``{"path", "target"}`` pairs, vault-relative path and link target.
    ``duplicate_stems`` maps a filename stem to every vault-relative path that
    uses it, for stems that appear in more than one directory.

    ``root_non_md`` NAMES the non-markdown files sitting directly in the vault
    root. They used to be counted nowhere at all: the survey's top-level loop has
    a branch for directories and a branch for root ``.md`` files, and a loose
    ``.zip`` or ``.png`` at the root matched neither, while ``non_md_counts`` is
    keyed by directory so it has no bucket for them. A census whose job is to turn
    every real vault shape into a required fixture cannot be silent about one — and
    this shape in particular is what the re-rooting plan reports as
    ``unclassified`` and refuses on, so it is the shape most worth naming. Listed
    rather than counted, because a fixture needs the extensions.
    """

    vault_root: str
    note_counts: dict[str, int] = field(default_factory=dict)
    non_md_counts: dict[str, int] = field(default_factory=dict)
    symlinks: list[dict[str, str]] = field(default_factory=list)
    max_depth: int = 0
    duplicate_stems: dict[str, list[str]] = field(default_factory=dict)
    no_frontmatter: list[str] = field(default_factory=list)
    registered_workspaces: list[str] = field(default_factory=list)
    unregistered_dirs: list[str] = field(default_factory=list)
    root_notes: int = 0
    root_non_md: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Render the census as a JSON-serialisable dict."""
        return {
            "vault_root": self.vault_root,
            "note_counts": dict(sorted(self.note_counts.items())),
            "non_md_counts": dict(sorted(self.non_md_counts.items())),
            "symlinks": self.symlinks,
            "max_depth": self.max_depth,
            "duplicate_stems": {
                stem: sorted(paths)
                for stem, paths in sorted(self.duplicate_stems.items())
            },
            "no_frontmatter": sorted(self.no_frontmatter),
            "registered_workspaces": sorted(self.registered_workspaces),
            "unregistered_dirs": sorted(self.unregistered_dirs),
            "root_notes": self.root_notes,
            "root_non_md": sorted(self.root_non_md),
        }


def _symlink_target(path: Path) -> str:
    """The link target as a posix string, or "" when it cannot be read."""
    try:
        return path.readlink().as_posix()
    except OSError:
        return ""


def _walk_dir(
    top: Path, root: Path
) -> tuple[int, int, list[dict[str, str]], int, list[Path]]:
    """Count files and collect symlinks under one top-level directory.

    Returns (md_count, non_md_count, symlinks, max_depth, md_paths). Walks with
    ``followlinks=False`` and prunes symlinked directories from the descent, so
    a symlink loop cannot hang the walk. Symlinked files and directories are
    reported, never traversed.
    """
    md_count = 0
    non_md_count = 0
    symlinks: list[dict[str, str]] = []
    max_depth = 0
    md_paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirpath_path = Path(dirpath)
        depth = len(dirpath_path.relative_to(root).parts)
        max_depth = max(max_depth, depth)
        for dname in list(dirnames):
            dpath = dirpath_path / dname
            if dpath.is_symlink():
                symlinks.append(
                    {
                        "path": str(dpath.relative_to(root)),
                        "target": _symlink_target(dpath),
                    }
                )
                dirnames.remove(dname)
        for fname in filenames:
            fpath = dirpath_path / fname
            if fpath.is_symlink():
                symlinks.append(
                    {
                        "path": str(fpath.relative_to(root)),
                        "target": _symlink_target(fpath),
                    }
                )
                continue
            if fname.lower().endswith(".md"):
                md_count += 1
                md_paths.append(fpath)
            else:
                non_md_count += 1
    return md_count, non_md_count, symlinks, max_depth, md_paths


def survey_vault(
    vault_root: Path | str,
    *,
    registered_workspaces: Sequence[str] | None = None,
) -> WorkspaceCensus:
    """Survey a vault root, returning a read-only census.

    ``registered_workspaces`` is the registry the migration will move files
    into. When omitted, it falls back to `vault_workspaces` inference, which
    treats every top-level directory that is not a note-type folder or an
    excluded one as a workspace. The gap-finder is the difference: a top-level
    directory that is not a registered workspace has no destination in the
    migration.
    """
    root = Path(vault_root).resolve()
    names = (
        list(registered_workspaces)
        if registered_workspaces is not None
        else vault_workspaces(root)
    )
    census = WorkspaceCensus(
        vault_root=str(root),
        registered_workspaces=sorted(names),
    )
    if not root.is_dir():
        return census

    registered = set(names)
    top_dirs: list[str] = []
    md_paths: list[Path] = []

    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_symlink():
            census.symlinks.append(
                {"path": entry.name, "target": _symlink_target(entry)}
            )
            continue
        if entry.is_dir():
            top_dirs.append(entry.name)
            md_count, non_md_count, symlinks, depth, dir_md = _walk_dir(entry, root)
            census.note_counts[entry.name] = md_count
            census.non_md_counts[entry.name] = non_md_count
            census.symlinks.extend(symlinks)
            census.max_depth = max(census.max_depth, depth)
            md_paths.extend(dir_md)
        elif entry.is_file() and entry.name.lower().endswith(".md"):
            census.root_notes += 1
            md_paths.append(entry)
        elif entry.is_file():
            census.root_non_md.append(entry.name)

    census.unregistered_dirs = sorted(d for d in top_dirs if d not in registered)

    by_stem: dict[str, list[Path]] = defaultdict(list)
    for path in md_paths:
        by_stem[path.stem].append(path)
    census.duplicate_stems = {
        stem: [str(p.relative_to(root)) for p in paths]
        for stem, paths in by_stem.items()
        if len(paths) > 1
    }

    for path in md_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if FRONTMATTER_RE.match(text) is None:
            census.no_frontmatter.append(str(path.relative_to(root)))

    return census


def format_census(census: WorkspaceCensus) -> str:
    """Render the census as human-readable text."""
    lines: list[str] = []
    lines.append(f"Vault root: {census.vault_root}")
    lines.append("")
    lines.append("Notes per top-level directory:")
    for name, count in sorted(census.note_counts.items()):
        lines.append(f"  {name}: {count}")
    lines.append(f"  (root): {census.root_notes}")
    lines.append("")
    lines.append("Non-markdown files per top-level directory:")
    for name, count in sorted(census.non_md_counts.items()):
        lines.append(f"  {name}: {count}")
    lines.append(f"  (root): {len(census.root_non_md)}")
    for name in sorted(census.root_non_md):
        lines.append(f"    {name}")
    lines.append("")
    lines.append(f"Max directory depth: {census.max_depth}")
    lines.append(f"Symlinks: {len(census.symlinks)}")
    for link in census.symlinks:
        lines.append(f"  {link['path']} -> {link['target']}")
    lines.append("")
    lines.append(f"Duplicate note stems: {len(census.duplicate_stems)}")
    for stem, paths in sorted(census.duplicate_stems.items()):
        lines.append(f"  {stem}:")
        for path in paths:
            lines.append(f"    {path}")
    lines.append("")
    lines.append(f"Notes without frontmatter: {len(census.no_frontmatter)}")
    for path in census.no_frontmatter:
        lines.append(f"  {path}")
    lines.append("")
    registered = ", ".join(census.registered_workspaces) or "(none)"
    lines.append(f"Registered workspaces: {registered}")
    unregistered = ", ".join(census.unregistered_dirs) or "(none)"
    lines.append(f"Unregistered top-level directories: {unregistered}")
    return "\n".join(lines) + "\n"
