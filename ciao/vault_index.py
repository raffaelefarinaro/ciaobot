"""Index memory-vault markdown files from YAML frontmatter and body links.

Modes:
  - default: print TSV to stdout for agent consumption
  - --write: regenerate memory-vault/INDEX.md

The graph (`related` field, `--related-to`, `--neighbors`) is built from both:
  - frontmatter `related:` / `relatedTo:` entries, and
  - inline `[[wikilinks]]` in note bodies (excluding fenced/inline code).

Filters: --workspace, --type, --tag, --name, --related-to, --neighbors
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


def default_vault_root() -> Path:
    env_root = os.environ.get("CIAO_VAULT_ROOT", "").strip()
    root = Path(env_root).expanduser() if env_root else Path("memory-vault")
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()

EXCLUDED_TOP_DIRS = {"Logs", "Templates", ".obsidian"}
EXCLUDED_PATH_PARTS: set[str] = set()

# Directory-based type inference when frontmatter is missing.
DIR_TYPE_MAP = {
    "People": "person",
    "Projects": "project",
    "Ideas": "idea",
    "Resources": "resource",
    "Places": "place",
    "Documents": "document",
    "Workspace": "workspace",
    "references": "reference",
    "products": "product",
    "features": "feature",
    "active": "project",
    "completed": "project",
    "content": "content",
    "journal": "journal",
    "automations": "automation",
}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
# Captures the inner ref of a wikilink, ignoring optional #anchor and |display.
# Examples matched (group 1 in parens):
#   [[Foo]]                  -> Foo
#   [[Foo|Display]]          -> Foo
#   [[Foo#Heading]]          -> Foo
#   [[Foo#Heading|Display]]  -> Foo
#   [[#Heading]]             -> (no match: pure in-page anchor)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


@dataclass
class Entry:
    path: Path  # repo-relative
    title: str
    type: str
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)  # normalized repo-relative paths
    workspace: str = "personal"


def _is_excluded(rel_path: Path) -> bool:
    parts = rel_path.parts
    if not parts:
        return True
    if parts[0] in EXCLUDED_TOP_DIRS:
        return True
    if any(p in EXCLUDED_PATH_PARTS for p in parts):
        return True
    # memory-vault/work/Logs (if any), etc.
    if any(p in EXCLUDED_TOP_DIRS for p in parts[1:]):
        return True
    return False


def _infer_type(rel_path: Path) -> str:
    for part in rel_path.parts:
        if part in DIR_TYPE_MAP:
            return DIR_TYPE_MAP[part]
    return ""


def _workspace_of(rel_from_vault: Path) -> str:
    # Each workspace lives under memory-vault/<workspace>/. Legacy single-root
    # vaults without a workspace segment keep reporting "personal".
    if not rel_from_vault.parts:
        return "personal"
    first = rel_from_vault.parts[0]
    if first in DIR_TYPE_MAP:
        return "personal"
    return first


def _coerce_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                s = item.strip()
                if s:
                    out.append(s)
            else:
                out.append(str(item))
        return out
    return [str(value)]


def _parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _first_h1(text: str) -> str:
    # Skip past frontmatter if present.
    m = FRONTMATTER_RE.match(text)
    body = text[m.end():] if m else text
    h = H1_RE.search(body)
    return h.group(1).strip() if h else ""


def _extract_body_wikilinks(text: str) -> list[str]:
    """Return inner refs of `[[wikilinks]]` in the body, in document order.

    Skips frontmatter, fenced code blocks (```...```), and inline code spans.
    Aliases and anchors are stripped (`[[Foo#H|Display]]` -> `Foo`).
    Pure in-page anchors (`[[#Heading]]`) are not returned.
    Duplicates are preserved here; deduplication happens downstream.
    """
    m = FRONTMATTER_RE.match(text)
    body = text[m.end():] if m else text
    body = FENCED_CODE_RE.sub("", body)
    body = INLINE_CODE_RE.sub("", body)
    out: list[str] = []
    for match in WIKILINK_RE.finditer(body):
        ref = match.group(1).strip()
        if ref:
            out.append(ref)
    return out


def _normalize_related_value(value: str) -> str:
    """Extract a vault-relative-ish reference from a related/relatedTo entry.

    Handles: "People/Mo", "[[People/Mo]]", "[[People/Mo|Display]]", "Projects/Foo.md".
    Returns the inner reference (no brackets, no display alias, no anchor).
    """
    s = value.strip()
    # Wikilink form
    m = WIKILINK_RE.search(s)
    if m:
        return m.group(1).strip()
    # Strip surrounding quotes/backticks
    s = s.strip("\"'`")
    return s


def _build_filename_index(entries: list[Entry]) -> dict[str, list[Path]]:
    idx: dict[str, list[Path]] = defaultdict(list)
    for e in entries:
        # key by vault-relative path without extension
        rel_from_vault = e.path.relative_to("memory-vault")
        stem_key = str(rel_from_vault.with_suffix(""))
        idx[stem_key].append(e.path)
        # also key by filename stem alone for bare references like "Mo"
        idx[e.path.stem].append(e.path)
    return idx


def _resolve_related(ref: str, filename_idx: dict[str, list[Path]]) -> Path | None:
    """Map a related ref (e.g. 'People/Mo', 'Mo', 'Work/People/X') to a repo-relative Path."""
    if not ref:
        return None
    # Strip leading 'memory-vault/' if present.
    if ref.startswith("memory-vault/"):
        ref = ref[len("memory-vault/"):]
    # Strip trailing .md
    if ref.endswith(".md"):
        ref = ref[:-3]
    hits = filename_idx.get(ref)
    if hits:
        return hits[0]
    # Try last segment (bare name)
    tail = ref.rsplit("/", 1)[-1]
    hits = filename_idx.get(tail)
    if hits and len(hits) == 1:
        return hits[0]
    return None


def scan_vault(vault_root: Path | None = None) -> list[Entry]:
    vault_root = (vault_root or default_vault_root()).resolve()
    entries: list[Entry] = []
    for md_path in sorted(vault_root.rglob("*.md")):
        rel_from_vault = md_path.relative_to(vault_root)
        if _is_excluded(rel_from_vault):
            continue
        # Skip top-level INDEX.md / MEMORY.md; MEMORY.md should stay curated,
        # INDEX.md is the output of this script.
        if rel_from_vault.name in {"INDEX.md", "MEMORY.md"}:
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm = _parse_frontmatter(text)
        h1 = _first_h1(text)
        if not fm and not h1:
            continue  # placeholder / .gitkeep-adjacent

        title = (
            (fm.get("title") or fm.get("name") or "").strip()
            or h1
            or md_path.stem
        )
        entry_type = (fm.get("type") or "").strip() or _infer_type(rel_from_vault)
        tags = _coerce_list(fm.get("tags"))
        aliases = _coerce_list(fm.get("aliases"))
        related_raw = _coerce_list(fm.get("related")) + _coerce_list(fm.get("relatedTo"))
        related_refs = [_normalize_related_value(r) for r in related_raw]
        related_refs = [r for r in related_refs if r]
        # Body wikilinks contribute to the same graph; resolution + dedup
        # happen below alongside frontmatter `related:` entries.
        related_refs.extend(_extract_body_wikilinks(text))

        # Render as a vault-relative path with a "memory-vault/" prefix so
        # output is identical regardless of the absolute location of vault_root
        # (this also lets tests run against a synthetic vault under tmp_path).
        repo_rel = Path("memory-vault") / rel_from_vault
        entries.append(
            Entry(
                path=repo_rel,
                title=title,
                type=entry_type,
                tags=tags,
                aliases=aliases,
                related=related_refs,  # resolved below
                workspace=_workspace_of(rel_from_vault),
            )
        )

    # Resolve related refs to actual repo-relative paths.
    filename_idx = _build_filename_index(entries)
    for e in entries:
        resolved: list[str] = []
        seen: set[str] = set()
        for ref in e.related:
            target = _resolve_related(ref, filename_idx)
            if target is None:
                continue
            key = str(target)
            if key in seen or target == e.path:
                continue
            seen.add(key)
            resolved.append(key)
        e.related = resolved

    return entries


def _build_graph(entries: list[Entry]) -> dict[str, set[str]]:
    """Undirected graph keyed by repo-relative path string."""
    graph: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        src = str(e.path)
        for tgt in e.related:
            graph[src].add(tgt)
            graph[tgt].add(src)
    return graph


def _normalize_path_arg(value: str) -> str:
    """Normalize a user-supplied path to match entry.path string form."""
    p = Path(value)
    try:
        p = p.resolve().relative_to(Path.cwd().resolve())
    except (ValueError, OSError):
        p = Path(value)
    return str(p)


def filter_entries(
    entries: list[Entry],
    *,
    workspace: str = "all",
    types: Iterable[str] = (),
    tags: Iterable[str] = (),
    name: str | None = None,
) -> list[Entry]:
    types = list(types)
    tags = list(tags)
    name_lower = name.lower() if name else None

    def ok(e: Entry) -> bool:
        if workspace != "all" and e.workspace != workspace:
            return False
        if types and e.type not in types:
            return False
        if tags and not all(t in e.tags for t in tags):
            return False
        if name_lower:
            hay = [e.title.lower(), *[a.lower() for a in e.aliases]]
            if not any(name_lower in h for h in hay):
                return False
        return True

    return [e for e in entries if ok(e)]


def neighbors(
    entries: list[Entry],
    start_path: str,
    depth: int = 1,
) -> list[tuple[int, Entry]]:
    """BFS neighbors of start_path up to `depth` hops (excludes start)."""
    by_path = {str(e.path): e for e in entries}
    graph = _build_graph(entries)
    if start_path not in by_path:
        return []
    visited: dict[str, int] = {start_path: 0}
    queue: deque[str] = deque([start_path])
    while queue:
        node = queue.popleft()
        d = visited[node]
        if d >= depth:
            continue
        for nb in graph.get(node, ()):
            if nb in visited:
                continue
            visited[nb] = d + 1
            queue.append(nb)
    out: list[tuple[int, Entry]] = []
    for p, d in visited.items():
        if d == 0 or p not in by_path:
            continue
        out.append((d, by_path[p]))
    out.sort(key=lambda x: (x[0], str(x[1].path)))
    return out


# ---- Output formatters -----------------------------------------------------

TSV_HEADERS = ["path", "workspace", "type", "title", "tags", "aliases", "related"]


def format_tsv(entries: list[Entry], include_hops: list[int] | None = None) -> str:
    lines: list[str] = []
    if include_hops is None:
        lines.append("\t".join(TSV_HEADERS))
        for e in entries:
            lines.append(
                "\t".join(
                    [
                        str(e.path),
                        e.workspace,
                        e.type,
                        e.title,
                        ",".join(e.tags),
                        ",".join(e.aliases),
                        ",".join(e.related),
                    ]
                )
            )
    else:
        lines.append("\t".join(["hop", *TSV_HEADERS]))
        for hop, e in zip(include_hops, entries):
            lines.append(
                "\t".join(
                    [
                        str(hop),
                        str(e.path),
                        e.workspace,
                        e.type,
                        e.title,
                        ",".join(e.tags),
                        ",".join(e.aliases),
                        ",".join(e.related),
                    ]
                )
            )
    return "\n".join(lines) + "\n"


def format_json(entries: list[Entry], hops: list[int] | None = None) -> str:
    def item(e: Entry, hop: int | None) -> dict:
        d: dict[str, Any] = {
            "path": str(e.path),
            "workspace": e.workspace,
            "type": e.type,
            "title": e.title,
            "tags": e.tags,
            "aliases": e.aliases,
            "related": e.related,
        }
        if hop is not None:
            d["hop"] = hop
        return d

    if hops is None:
        data = [item(e, None) for e in entries]
    else:
        data = [item(e, h) for h, e in zip(hops, entries)]
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _wikilink(repo_rel: str) -> str:
    # Convert "memory-vault/personal/People/Mo.md" -> "[[personal/People/Mo]]"
    inner = repo_rel
    if inner.startswith("memory-vault/"):
        inner = inner[len("memory-vault/"):]
    if inner.endswith(".md"):
        inner = inner[:-3]
    return f"[[{inner}]]"


def _plain_ref(repo_rel: str) -> str:
    """Non-linking reference for INDEX.md (avoids god-node in Obsidian graph)."""
    inner = repo_rel
    if inner.startswith("memory-vault/"):
        inner = inner[len("memory-vault/"):]
    if inner.endswith(".md"):
        inner = inner[:-3]
    return f"`{inner}`"


def format_md(entries: list[Entry]) -> str:
    # Group: workspace -> type -> entry
    grouped: dict[str, dict[str, list[Entry]]] = defaultdict(lambda: defaultdict(list))
    for e in entries:
        grouped[e.workspace][e.type or "uncategorized"].append(e)

    sections: list[str] = []
    for ws in sorted(grouped):
        sections.append(f"## {ws.capitalize()}")
        for t in sorted(grouped[ws].keys()):
            bucket = sorted(grouped[ws][t], key=lambda x: x.title.lower())
            sections.append(f"\n### {t or 'uncategorized'} ({len(bucket)})\n")
            for e in bucket:
                extras: list[str] = []
                if e.tags:
                    extras.append("tags: " + ", ".join(e.tags))
                if e.aliases:
                    extras.append("aliases: " + ", ".join(e.aliases))
                suffix = f" ({'; '.join(extras)})" if extras else ""
                sections.append(f"- {_plain_ref(str(e.path))}{suffix}")
            sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def write_index_file(entries: list[Entry], dest: Path) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        "<!-- generated by ciao vault-index, do not edit by hand -->\n"
        f"<!-- generated at {now} ({len(entries)} entries) -->\n\n"
        "# Vault Index\n\n"
        "Auto-generated table of contents derived from frontmatter. "
        "For curated priorities see [[MEMORY]]. "
        "For filtered queries run `ciao vault-index --help`.\n\n"
    )
    dest.write_text(header + format_md(entries), encoding="utf-8")


# ---- CLI -------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", default="all")
    p.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    p.add_argument("--type", dest="types", action="append", default=[])
    p.add_argument("--tag", dest="tags", action="append", default=[])
    p.add_argument("--name", default=None)
    p.add_argument("--related-to", dest="related_to", default=None,
                   help="List direct neighbors of the given entry path.")
    p.add_argument("--neighbors", default=None,
                   help="Walk the graph from the given entry up to --depth hops.")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--format", choices=["tsv", "md", "json"], default="tsv")
    p.add_argument("--write", action="store_true",
                   help="Regenerate memory-vault/INDEX.md (ignores other filters).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    vault_root = (args.vault_root or default_vault_root()).resolve()
    entries = scan_vault(vault_root)

    if args.write:
        dest = vault_root / "INDEX.md"
        write_index_file(entries, dest)
        print(f"wrote {dest} ({len(entries)} entries)", file=sys.stderr)
        return 0

    if args.related_to:
        start = _normalize_path_arg(args.related_to)
        hopped = neighbors(entries, start, depth=1)
    elif args.neighbors:
        start = _normalize_path_arg(args.neighbors)
        hopped = neighbors(entries, start, depth=args.depth)
    else:
        hopped = None

    if hopped is not None:
        # Apply filters on top of graph walk results
        filtered = filter_entries(
            [e for _, e in hopped],
            workspace=args.workspace,
            types=args.types,
            tags=args.tags,
            name=args.name,
        )
        # Preserve hop metadata only for entries that survived filtering
        kept_paths = {str(e.path) for e in filtered}
        hop_pairs = [(h, e) for h, e in hopped if str(e.path) in kept_paths]
        ents = [e for _, e in hop_pairs]
        hops = [h for h, _ in hop_pairs]
    else:
        ents = filter_entries(
            entries,
            workspace=args.workspace,
            types=args.types,
            tags=args.tags,
            name=args.name,
        )
        hops = None

    if args.format == "tsv":
        sys.stdout.write(format_tsv(ents, include_hops=hops))
    elif args.format == "json":
        sys.stdout.write(format_json(ents, hops=hops))
    else:  # md
        sys.stdout.write(format_md(ents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
