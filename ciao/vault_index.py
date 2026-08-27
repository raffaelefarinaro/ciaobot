"""Index memory-vault markdown files from YAML frontmatter and body links.

Modes:
  - default: print TSV to stdout for agent consumption
  - --write: regenerate memory-vault/INDEX.md

The graph (`related` field, `--related-to`, `--neighbors`) is built from both:
  - frontmatter `related:` / `relatedTo:` entries (bare refs, never links), and
  - relative markdown links in note bodies, e.g. `[Mo](./People/Mo.md)`
    (excluding fenced/inline code).

Markdown links are the vault's only cross-link dialect. A wikilink is opaque
body text to anything that isn't Obsidian, so the edges never travelled with
the notes; a relative markdown link resolves in Obsidian, on GitHub, in a
plain editor, and to any OKF consumer reading the folder as a bundle.

Filters: --workspace, --type, --tag, --name, --related-to, --neighbors
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

import yaml


def default_vault_root() -> Path:
    """Locate the vault when no root was passed.

    A relative value resolves against ``CIAO_WORKSPACE`` rather than the cwd. The
    bundled engine's launcher ``cd``s into the app's ``ciao-runtime`` directory
    before exec'ing Python, so resolving against the cwd sent a relative
    ``CIAO_VAULT_ROOT=memory-vault`` inside the app bundle — which is why
    ``vault-index --write`` failed from a routine while working from a shell.
    """
    env_root = os.environ.get("CIAO_VAULT_ROOT", "").strip()
    root = Path(env_root).expanduser() if env_root else Path("memory-vault")
    if not root.is_absolute():
        workspace = os.environ.get("CIAO_WORKSPACE", "").strip()
        base = Path(workspace).expanduser() if workspace else Path.cwd()
        root = base / root
    return root.resolve()

EXCLUDED_TOP_DIRS = {"Logs", "Templates", ".obsidian"}

# Generated or curated files that are *about* the vault rather than notes in it.
# Compared casefolded: OKF names `index.md` and `log.md` in lowercase, so an
# agent-written or imported bundle produces exactly those, and a case-sensitive
# check indexed them as ordinary notes — which turns a whole-vault table of
# contents into a god-node in the Memory Map, the failure `_index_link`'s
# exclusion exists to prevent. Shared by `vault_lint` and `fts_search` so the
# three cannot drift.
GENERATED_VAULT_FILES = frozenset({"index.md", "memory.md", "vocabulary.md"})


def is_generated_vault_file(name: str) -> bool:
    return (name or "").casefold() in GENERATED_VAULT_FILES


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

# The closed vocabulary for frontmatter ``type:``.
#
# Seeded from every DIR_TYPE_MAP *value* so path inference can never produce a
# type the linter rejects, plus the types the vault earned by use that no
# directory name implies. Deliberately a separate constant rather than more
# DIR_TYPE_MAP entries: that map's *keys* are directory names, and
# ``_workspace_of`` tests membership in them to tell a folder type from a
# workspace name, so adding a key silently changes workspace inference.
CANONICAL_TYPES = frozenset(DIR_TYPE_MAP.values()) | {
    "log",
    "note",
    "skill-proposal",
}

# Near-duplicate values seen in real vaults, mapped to the canonical type they
# meant. Reported as drift with the target named, so the fix is a rename with a
# known destination rather than a judgement call. Without this the index grows
# one section per synonym: `doc (1)` next to `document (1)`.
TYPE_ALIASES = {
    "analysis": "reference",
    "discussion-prep": "note",
    "doc": "document",
    "feature-brief": "feature",
    "hackathon-log": "journal",
    "plan": "document",
    "planning-doc": "document",
    "project-log": "log",
    "template": "document",
}


def canonical_type(raw: str) -> str:
    """Return the canonical form of a frontmatter ``type``.

    A canonical value maps to itself, a known alias to its target, and anything
    else to ``""`` — which is what ``vault_lint`` reports as ``unknown_type``.

    The comparison is case-insensitive: a case variant of a canonical or alias
    value (``Note`` beside ``note``, ``Doc`` beside ``doc``) still maps to the
    canonical/alias target, so the lint treats it as a safe rename rather than
    a brand-new type that needs promotion.
    """
    value = (raw or "").strip()
    if value in CANONICAL_TYPES:
        return value
    if value in TYPE_ALIASES:
        return TYPE_ALIASES[value]
    lowered = value.lower()
    for canonical in CANONICAL_TYPES:
        if canonical.lower() == lowered:
            return canonical
    for alias, target in TYPE_ALIASES.items():
        if alias.lower() == lowered:
            return target
    return ""


# Promotion threshold: a non-canonical type or an emerging tag that reaches
# this many uses becomes a candidate for the canonical/established set. Shared
# by the promotion/merge proposal audit (ciao.vocabulary_proposals) and the
# established-tag tier in VOCABULARY.md, so one value classifies both.
DEFAULT_PROMOTION_THRESHOLD = 5


def promotion_threshold() -> int:
    raw = os.environ.get("VOCAB_PROMOTION_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_PROMOTION_THRESHOLD
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_PROMOTION_THRESHOLD
    except ValueError:
        return DEFAULT_PROMOTION_THRESHOLD

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
# One inline markdown link. `label` is the visible text (needed so a stripped
# link can be replaced by readable prose instead of vanishing); the destination
# is either angle-bracketed — the only form that survives a filename with a
# space — or bare, with an optional link title after it.
# Examples matched:
#   [Mo](./People/Mo.md)              -> label "Mo", bare "./People/Mo.md"
#   [Mo](<./People/Mo Salah.md>)      -> label "Mo", angle "./People/Mo Salah.md"
#   [Mo](./People/Mo.md "Tooltip")    -> title ignored
# A leading `!` (image) or backslash (escaped) is rejected at the call site,
# where the preceding character is in hand.
MARKDOWN_LINK_RE = re.compile(
    r"\[(?P<label>[^\[\]\n]*)\]\("
    r"[ \t]*(?:<(?P<angle>[^<>\n]*)>|(?P<bare>[^\s()]*))"
    r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?[ \t]*\)"
)
# Duplicated from `vault_lint` rather than imported: `vault_lint` imports this
# module, so the dependency only runs one way.
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_MD_SUFFIXES = (".md", ".markdown")
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# A `related:`/`relatedTo:` frontmatter key, optionally with an inline flow
# value on the same line (`related: [A, B]` or `related: People/Mo`).
_FM_RELATED_KEY_RE = re.compile(r"^(related|relatedTo):[ \t]*(.*)$")
_FM_LIST_ITEM_RE = re.compile(r"^(\s+)-\s?(.*)$")


@dataclass
class Entry:
    path: Path  # repo-relative
    title: str
    type: str
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)  # normalized repo-relative paths
    # Refs that resolved in ANOTHER root, and refs that resolved nowhere. Both
    # used to be discarded by the resolution loop without a trace, which made a
    # dangling link and a deliberate cross-workspace link indistinguishable —
    # from each other and from a note with no links at all. Measured on a real
    # two-root vault: 14 of 293 `related` refs named another workspace and
    # vanished, including a person's own other half.
    #
    # `related` itself stays scoped to one root on purpose: the graph is rendered
    # per workspace, so an edge to a node that is not in the graph draws nothing.
    # Keeping these separate preserves that while making the loss inspectable.
    related_external: list[str] = field(default_factory=list)
    related_unresolved: list[str] = field(default_factory=list)
    workspace: str = "personal"
    description: str = ""
    # Frontmatter ``updated:`` (YYYY-MM-DD): when the note's facts were last
    # verified, as opposed to when the file was last written. Empty when the
    # note carries no such key — consumers then fall back to mtime, which
    # says "touched", not "checked". See ciao.memory_audit for the consumer.
    updated: str = ""


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


def markdown_destination(path: str) -> str:
    """Spell ``path`` so it survives as a markdown link destination.

    CommonMark ends a bare destination at the first whitespace, so
    `(./My Note.md)` would resolve to `./My`. Angle brackets keep the whole
    path and stay readable (unlike percent-encoding a unicode filename); `<`
    and `>` inside a filename are escaped so they cannot close the bracket
    early. Shared by every emitter so the dialect is spelled in one place.
    """
    escaped = path.replace("<", "%3C").replace(">", "%3E")
    if any(ch.isspace() for ch in escaped) or "(" in escaped or ")" in escaped:
        return f"<{escaped}>"
    return escaped


def vault_link_ref(destination: str) -> str:
    """Return the note-relative ref a markdown link destination points at.

    Empty for anything that is not an in-vault markdown link: a URI, an
    absolute or protocol-relative path, a Windows drive path, a pure `#anchor`,
    a `<placeholder>` left in a template, or a non-markdown target such as an
    image. Fragments and queries are dropped — nothing in Ciaobot scrolls to a
    heading (the wikilink parser this replaces also parsed anchors and threw
    them away), so `./Mo.md#History` is an edge to `Mo` and nothing more.

    The `.md`/`.markdown` suffix is stripped because every consumer keys its
    lookups on extension-less refs, exactly as it did for `[[People/Mo]]`.
    """
    target = (destination or "").strip()
    if not target or "<" in target or ">" in target:
        return ""
    if target.startswith(("#", "/")) or _URI_SCHEME_RE.match(target):
        return ""
    target = unquote(target.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
    if not target or target.startswith("/"):
        return ""
    lowered = target.lower()
    for suffix in _MD_SUFFIXES:
        if lowered.endswith(suffix):
            return target[: -len(suffix)]
    return ""


def resolve_vault_link(source_rel: str | Path, ref: str) -> str:
    """Resolve a link ``ref`` found in ``source_rel`` to a vault-relative ref.

    ``source_rel`` is the containing *note's* vault-relative path (not its
    directory) — a markdown link is relative to the note that holds it, which
    is the one thing a wikilink never had to care about and therefore the one
    thing every caller has to start passing.

    Returns "" when the link leaves the vault root: an edge out of the bundle
    is not a vault edge.
    """
    if not ref:
        return ""
    source = Path(source_rel).as_posix() if source_rel else ""
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(source), ref))
    if joined in {".", ".."} or joined.startswith("../"):
        return ""
    return joined


def _is_link_start(text: str, index: int) -> bool:
    """False when the `[` at ``index`` opens an image or is backslash-escaped.

    `![alt](x.png)` is an embed, not a link, and `\\[not a link](x.md)` is prose
    documenting the syntax — both would otherwise become phantom graph edges.
    """
    if index == 0:
        return True
    if text[index - 1] == "!":
        return False
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 0


def _body_after_frontmatter(text: str) -> str:
    """Body with frontmatter, fenced blocks, and inline code spans removed."""
    m = FRONTMATTER_RE.match(text)
    body = text[m.end():] if m else text
    return INLINE_CODE_RE.sub("", FENCED_CODE_RE.sub("", body))


def _extract_body_links(text: str, source_rel: str | Path = "") -> list[str]:
    """Return vault-relative refs of the body's markdown links, in doc order.

    Skips frontmatter, fenced code blocks (```...```), and inline code spans.
    Destinations that are not in-vault markdown links (URLs, images, pure
    anchors) and links escaping the vault root are dropped, as is a leftover
    `[[wikilink]]` — after the swap it is simply body text, not an edge.
    Duplicates are preserved here; deduplication happens downstream.
    """
    body = _body_after_frontmatter(text)
    out: list[str] = []
    for match in MARKDOWN_LINK_RE.finditer(body):
        if not _is_link_start(body, match.start()):
            continue
        raw = match.group("angle")
        if raw is None:
            raw = match.group("bare") or ""
        resolved = resolve_vault_link(source_rel, vault_link_ref(raw))
        if resolved:
            out.append(resolved)
    return out


def _normalize_related_value(value: str) -> str:
    """Extract a vault-relative-ish reference from a related/relatedTo entry.

    Handles: "People/Mo", "Projects/Foo.md", and the same quoted or backticked.

    Frontmatter refs stay *bare* — deliberately not markdown links. YAML sees
    `related: [Mo](./People/Mo.md)` as one opaque string, which `_resolve_related`
    cannot resolve, and OKF has no opinion on frontmatter link syntax. A leftover
    `[[People/Mo]]` from before the swap no longer resolves; the link migration
    normalizes those to bare refs.
    """
    return value.strip().strip("\"'`")


def _build_filename_index(
    entries: list[Entry], path_prefix: Path | None = None
) -> dict[str, list[Path]]:
    """Index entries by vault-relative path and by bare stem.

    ``path_prefix`` is what ``Entry.path`` is rendered under. It was hardcoded to
    ``memory-vault``, which raises the moment a scan renders paths under a
    per-root prefix, so a caller that knows the prefix has to say so.
    """
    prefix = Path("memory-vault") if path_prefix is None else Path(path_prefix)
    idx: dict[str, list[Path]] = defaultdict(list)
    for e in entries:
        # key by vault-relative path without extension
        rel_from_vault = _strip_prefix(e.path, prefix)
        stem_key = str(rel_from_vault.with_suffix(""))
        idx[stem_key].append(e.path)
        # also key by filename stem alone for bare references like "Mo"
        idx[e.path.stem].append(e.path)
    return idx


def _strip_prefix(path: Path, prefix: Path) -> Path:
    """``path`` relative to ``prefix``, or unchanged when it is not under it.

    Total rather than raising: a rendered path and a prefix can legitimately
    disagree (a caller merging roots, an entry built by hand in a test), and the
    useful answer there is "leave it alone", not an exception halfway through a
    vault scan.
    """
    try:
        return path.relative_to(prefix)
    except ValueError:
        return path


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


def scan_vault(
    vault_root: Path | None = None,
    *,
    workspace: str = "",
    path_prefix: Path | None = None,
) -> list[Entry]:
    """Scan one vault into entries.

    ``workspace`` stamps every entry instead of inferring the workspace from the
    first path segment. After the re-rooting a vault belongs to exactly one
    workspace and its first segment is a FOLDER name, so inference returns
    things like ``projects`` and any ``?workspace=`` filter drops nearly
    everything. Pass it whenever the caller knows which root it is reading.

    ``path_prefix`` is what rendered paths are relative to, defaulting to
    ``memory-vault`` so a single-vault scan is byte-identical to before. A
    caller merging several roots into one graph must pass a per-root prefix,
    or two roots holding a note of the same name render the same path and
    collide — the same defect the search index had.
    """
    vault_root = (vault_root or default_vault_root()).resolve()
    prefix = Path("memory-vault") if path_prefix is None else Path(path_prefix)
    entries: list[Entry] = []
    for md_path in sorted(vault_root.rglob("*.md")):
        rel_from_vault = md_path.relative_to(vault_root)
        if _is_excluded(rel_from_vault):
            continue
        # MEMORY.md stays curated; INDEX.md and VOCABULARY.md are this
        # script's own output. None of them are notes.
        if is_generated_vault_file(rel_from_vault.name):
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
        description = (fm.get("description") or "").strip()
        updated = str(fm.get("updated") or "").strip()
        related_raw = _coerce_list(fm.get("related")) + _coerce_list(fm.get("relatedTo"))
        related_refs = [_normalize_related_value(r) for r in related_raw]
        related_refs = [r for r in related_refs if r]
        # Body links contribute to the same graph; resolution + dedup happen
        # below alongside frontmatter `related:` entries. The note's own
        # vault-relative path is what makes its relative destinations resolvable.
        related_refs.extend(_extract_body_links(text, rel_from_vault))

        # Render as a vault-relative path with a "memory-vault/" prefix so
        # output is identical regardless of the absolute location of vault_root
        # (this also lets tests run against a synthetic vault under tmp_path).
        repo_rel = prefix / rel_from_vault
        entries.append(
            Entry(
                path=repo_rel,
                title=title,
                type=entry_type,
                tags=tags,
                aliases=aliases,
                related=related_refs,  # resolved below
                workspace=workspace or _workspace_of(rel_from_vault),
                description=description,
                updated=updated,
            )
        )

    # Resolve related refs to actual repo-relative paths.
    filename_idx = _build_filename_index(entries, prefix)
    for e in entries:
        resolved: list[str] = []
        unresolved: list[str] = []
        seen: set[str] = set()
        for ref in e.related:
            target = _resolve_related(ref, filename_idx)
            if target is None:
                # Not necessarily broken: in a per-root scan this is every ref
                # naming another workspace. `scan_targets` sorts the two apart
                # once it has seen all the roots; a single-vault scan cannot
                # tell, and says so by leaving it here.
                if ref not in unresolved:
                    unresolved.append(ref)
                continue
            key = str(target)
            if key in seen or target == e.path:
                continue
            seen.add(key)
            resolved.append(key)
        e.related = resolved
        e.related_unresolved = unresolved

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


def _ref_matches(raw: str, filename_idx: dict[str, list[Path]], deleted_path: str) -> bool:
    """True if a raw related/link reference string resolves to deleted_path."""
    target = _resolve_related(_normalize_related_value(raw), filename_idx)
    return target is not None and str(target) == deleted_path


def _strip_frontmatter_related(
    fm_text: str, filename_idx: dict[str, list[Path]], deleted_path: str
) -> tuple[str, bool]:
    """Remove `related:`/`relatedTo:` entries pointing at deleted_path.

    Edits surgically line-by-line instead of round-tripping through
    yaml.safe_dump, so untouched keys keep their original formatting,
    quoting, and order.
    """
    lines = fm_text.split("\n")
    out: list[str] = []
    changed = False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = _FM_RELATED_KEY_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        key, inline_value = m.group(1), m.group(2).strip()
        if inline_value.startswith("[") and inline_value.endswith("]"):
            items = [it.strip().strip("\"'") for it in inline_value[1:-1].split(",") if it.strip()]
            kept = [it for it in items if not _ref_matches(it, filename_idx, deleted_path)]
            if len(kept) != len(items):
                changed = True
            if kept:
                out.append(f"{key}: [{', '.join(kept)}]")
            i += 1
            continue
        if inline_value:
            if _ref_matches(inline_value, filename_idx, deleted_path):
                changed = True
            else:
                out.append(line)
            i += 1
            continue
        # Block list style: gather the indented "- item" lines that follow.
        j = i + 1
        kept_item_lines: list[str] = []
        block_changed = False
        while j < n:
            item_m = _FM_LIST_ITEM_RE.match(lines[j])
            if not item_m:
                break
            item_value = item_m.group(2).strip().strip("\"'")
            if _ref_matches(item_value, filename_idx, deleted_path):
                block_changed = True
            else:
                kept_item_lines.append(lines[j])
            j += 1
        if block_changed:
            changed = True
        if kept_item_lines:
            out.append(line)
            out.extend(kept_item_lines)
        # else: every item under this key was stripped, so drop the key too.
        i = j
    return "\n".join(out), changed


def _strip_body_links(
    body: str,
    filename_idx: dict[str, list[Path]],
    deleted_path: str,
    source_rel: str | Path = "",
) -> tuple[str, bool]:
    """Replace body markdown links resolving to deleted_path with plain text.

    A link becomes its label (`[Mo Salah](./People/Mo.md)` -> `Mo Salah`), or
    the ref's last path segment when the label is empty, so the sentence still
    reads naturally instead of pointing at a file that no longer exists.
    Matches inside fenced code blocks or inline code spans are left untouched,
    mirroring `_extract_body_links`.

    ``source_rel`` is the containing note's vault-relative path: the same
    destination means different targets from different directories, so
    stripping without it would erase the wrong link (or none at all).
    """
    excluded: list[tuple[int, int]] = []
    for m in FENCED_CODE_RE.finditer(body):
        excluded.append((m.start(), m.end()))
    for m in INLINE_CODE_RE.finditer(body):
        excluded.append((m.start(), m.end()))

    def _is_excluded_pos(pos: int) -> bool:
        return any(start <= pos < end for start, end in excluded)

    changed = False
    out: list[str] = []
    last = 0
    for m in MARKDOWN_LINK_RE.finditer(body):
        if _is_excluded_pos(m.start()) or not _is_link_start(body, m.start()):
            continue
        raw = m.group("angle")
        if raw is None:
            raw = m.group("bare") or ""
        ref = resolve_vault_link(source_rel, vault_link_ref(raw))
        if not ref or not _ref_matches(ref, filename_idx, deleted_path):
            continue
        replacement = m.group("label").strip() or ref.rsplit("/", 1)[-1]
        out.append(body[last:m.start()])
        out.append(replacement)
        last = m.end()
        changed = True
    out.append(body[last:])
    return ("".join(out), changed)


def _strip_all_references(
    text: str,
    filename_idx: dict[str, list[Path]],
    deleted_path: str,
    source_rel: str | Path = "",
) -> tuple[str, bool]:
    m = FRONTMATTER_RE.match(text)
    changed = False
    if m:
        new_fm_text, fm_changed = _strip_frontmatter_related(m.group(1), filename_idx, deleted_path)
        if fm_changed:
            changed = True
            text = text[:m.start()] + f"---\n{new_fm_text}\n---\n" + text[m.end():]
    m2 = FRONTMATTER_RE.match(text)
    body_start = m2.end() if m2 else 0
    new_body, body_changed = _strip_body_links(
        text[body_start:], filename_idx, deleted_path, source_rel
    )
    if body_changed:
        changed = True
        text = text[:body_start] + new_body
    return text, changed


def _commit_staged_edits(edits: list[tuple[Path, str, str]]) -> list[str]:
    """Swap staged note rewrites in as one operation, rolling back on failure.

    Phase 2 of `strip_references`. Every new text first lands in a temp file
    next to its target (same directory, so `os.replace` stays atomic and on
    the same filesystem), and only then are the targets swapped in. If any
    stage or swap fails, every already-swapped file is restored from its
    recorded original in reverse order, leftover temps are removed, and the
    original error propagates: a raised error means the vault is unchanged.
    """
    staged: list[tuple[Path, Path]] = []  # (target, temp) in stage order
    replaced: list[tuple[Path, str]] = []  # (target, original) already swapped in
    try:
        for abs_path, _original, new_text in edits:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=abs_path.parent,
                prefix=f".{abs_path.name}.",
                suffix=".tmp",
            ) as handle:
                handle.write(new_text)
                temp = Path(handle.name)
            # A fresh temp file lands at 0600 and os.replace would silently
            # tighten the rewritten note's permissions; carry the old mode over.
            try:
                os.chmod(temp, abs_path.stat().st_mode & 0o7777)
            except OSError:
                pass
            staged.append((abs_path, temp))
        for (target, temp), (_, original, _new_text) in zip(staged, edits):
            os.replace(temp, target)
            replaced.append((target, original))
    except OSError:
        for target, original in reversed(replaced):
            target.write_text(original, encoding="utf-8")
        for _, temp in staged:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return [str(abs_path) for abs_path, _, _ in edits]


def strip_references(
    vault_root: Path, deleted_path: str, *, path_prefix: Path | None = None
) -> list[str]:
    """Remove literal references to `deleted_path` from every note linking to it.

    Must run BEFORE the target file is deleted from disk: resolution depends
    on the target still existing in the filename index built here, or refs to
    it would already come back unresolved (and thus invisible to this
    function) exactly as `scan_vault` treats any other broken link.

    `deleted_path` is the `Entry.path` string form (e.g.
    "memory-vault/work/People/Mo.md"). Returns the repo-relative paths of the
    files actually edited on disk.

    The cleanup is all-or-nothing: rewrites are computed without touching any
    file, staged to temp files, then swapped in; a failure at any point rolls
    every already-swapped file back, so on success the caller may safely
    delete the target, and on failure the vault is exactly as before.

    ``path_prefix`` must be the prefix the CALLER's id was rendered with. After
    the re-rooting an id reads `<root>/memory-vault/...`, while a bare scan of
    one vault renders `memory-vault/...`: the two never compared equal, so
    every reference was left untouched and the note was deleted anyway -
    dangling `related:` entries and markdown links on every migrated install,
    reported as `edited_backlinks: []`.
    """
    vault_root = vault_root.resolve()
    prefix = path_prefix or Path("memory-vault")
    entries = scan_vault(vault_root, path_prefix=path_prefix)
    filename_idx = _build_filename_index(entries)
    # Phase 1 (pure): compute every rewrite up front, so a bad or unreadable
    # note aborts the whole cleanup before any other note has been touched.
    edits: list[tuple[Path, str, str]] = []
    edited: list[str] = []
    for e in entries:
        if str(e.path) == deleted_path or deleted_path not in e.related:
            continue
        rel_from_vault = _strip_prefix(e.path, prefix)
        abs_path = vault_root / rel_from_vault
        try:
            text = abs_path.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text, file_changed = _strip_all_references(
            text, filename_idx, deleted_path, rel_from_vault
        )
        if file_changed:
            edits.append((abs_path, text, new_text))
            edited.append(str(e.path))
    # Phase 2: stage + swap everything in, or roll back to the original bytes.
    _commit_staged_edits(edits)
    return edited


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
            "updated": e.updated,
        }
        if hop is not None:
            d["hop"] = hop
        return d

    if hops is None:
        data = [item(e, None) for e in entries]
    else:
        data = [item(e, h) for h, e in zip(hops, entries)]
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _vault_relative_ref(repo_rel: str) -> str:
    """"memory-vault/personal/People/Mo.md" -> "personal/People/Mo"."""
    inner = repo_rel
    if inner.startswith("memory-vault/"):
        inner = inner[len("memory-vault/"):]
    for suffix in _MD_SUFFIXES:
        if inner.lower().endswith(suffix):
            return inner[: -len(suffix)]
    return inner


def _index_link(repo_rel: str) -> str:
    """Render one INDEX.md entry as a real relative markdown link.

    This was a backticked non-link purely to keep INDEX.md out of Obsidian's
    graph view. Now that markdown links are the vault's dialect, a real link is
    what makes the index navigable in the file viewer, on GitHub, and to any OKF
    consumer — which is what OKF's `index.md` progressive disclosure is for.
    Still not a Ciaobot node: `scan_vault` skips generated files, so the
    god-node the backticks guarded against cannot come back.

    The label keeps the full vault-relative path: `context/entity_tagger.py`
    parses it back out of INDEX.md, and the path is what tells two notes with
    the same stem apart. INDEX.md sits at the vault root, so the destination is
    simply "./" + that path.
    """
    inner = _vault_relative_ref(repo_rel)
    return f"[{inner}]({markdown_destination(f'./{inner}.md')})"


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
                sections.append(f"- {_index_link(str(e.path))}{suffix}")
            sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def write_index_file(entries: list[Entry], dest: Path) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Link MEMORY.md only when it is actually there. A generated file must never
    # manufacture a permanent lint finding: an unconditional link would be a
    # standing `broken_markdown_links` entry (so `os-audit` exit 1 forever) in
    # every vault that has no root MEMORY.md.
    memory = (
        f"For curated priorities see [MEMORY]({markdown_destination('./MEMORY.md')}). "
        if (dest.parent / "MEMORY.md").is_file()
        else ""
    )
    header = (
        "<!-- generated by ciao vault-index, do not edit by hand -->\n"
        f"<!-- generated at {now} ({len(entries)} entries) -->\n\n"
        "# Vault Index\n\n"
        "Auto-generated table of contents derived from frontmatter. "
        f"{memory}"
        "For filtered queries run `ciao vault-index --help`.\n\n"
    )
    dest.write_text(header + format_md(entries), encoding="utf-8")


def scan_targets(
    targets: list[tuple[Path, str, Path]],
) -> tuple[list[Entry], dict[str, Path]]:
    """Scan several vaults into one entry list, plus rendered-path -> file map.

    Each target is ``(vault root, workspace stamp, rendered path prefix)``.
    Deliberately one scan per target rather than one walk over a common parent:
    ``related:`` links resolve inside a single scan, so scanning per root keeps a
    link from resolving across roots. That is the behaviour the graph already
    wanted — it drops cross-workspace edges — and here it comes for free instead
    of needing a filter.

    The map exists because rendered paths are no longer a fixed offset from one
    vault root, so a caller that needs the real file (to stat it, say) cannot
    reconstruct it by stripping a prefix.
    """
    merged: list[Entry] = []
    absolute: dict[str, Path] = {}
    prefixes: dict[str, Path] = {}
    for vault_root, workspace, prefix in targets:
        root = Path(vault_root)
        if not root.is_dir():
            continue
        scanned = scan_vault(root, workspace=workspace, path_prefix=prefix)
        prefixes[workspace] = Path(prefix)
        for entry in scanned:
            rendered = str(entry.path)
            try:
                tail = entry.path.relative_to(prefix)
            except ValueError:
                tail = entry.path
            absolute[rendered] = root / tail
        merged.extend(scanned)
    _resolve_cross_workspace(merged, prefixes)
    return merged, absolute


def _build_workspace_index(
    entries: list[Entry], prefixes: dict[str, Path]
) -> dict[str, list[Path]]:
    """Entries keyed the way a cross-workspace ref spells them.

    A ref names the other half as ``work/People/Ipek-Kahraman-Scandit`` — the
    workspace, then the path inside that workspace's vault. Neither of
    ``_build_filename_index``'s keys is that shape: it strips the whole prefix,
    workspace segment included, because within one root the segment is not part
    of any ref. So this keys by ``<workspace>/<vault-relative>`` and by
    ``<workspace>/<stem>``.
    """
    idx: dict[str, list[Path]] = defaultdict(list)
    for e in entries:
        if not e.workspace:
            continue
        inside = _strip_prefix(e.path, prefixes.get(e.workspace, Path("memory-vault")))
        idx[f"{e.workspace}/{inside.with_suffix('')}"].append(e.path)
        idx[f"{e.workspace}/{e.path.stem}"].append(e.path)
    return idx


def _resolve_cross_workspace(entries: list[Entry], prefixes: dict[str, Path]) -> None:
    """Sort each entry's unresolved refs into cross-workspace hits and misses.

    Runs after every root is scanned, which is the earliest point at which the
    two are distinguishable: inside one root's scan, "names another workspace"
    and "names nothing" look identical.
    """
    if len(prefixes) < 2:
        return
    idx = _build_workspace_index(entries, prefixes)
    workspaces = set(prefixes)
    owner = {str(e.path): e.workspace for e in entries}
    for e in entries:
        if not e.related_unresolved:
            continue
        external: list[str] = []
        still_missing: list[str] = []
        seen = {*e.related}
        for ref in e.related_unresolved:
            target = _resolve_workspace_ref(ref, e.workspace, idx, workspaces)
            if target is None or str(target) == str(e.path):
                still_missing.append(ref)
                continue
            key = str(target)
            if key in seen:
                continue
            seen.add(key)
            # A ref can name this root explicitly (`work/projects/x` written
            # inside work), which the in-root pass also fails to resolve because
            # its keys omit the workspace segment. That is an ordinary edge that
            # was being dropped, not a cross-workspace one — it belongs in
            # `related` so the graph draws it. Seven such refs exist on the live
            # vault, against seventeen genuinely crossing ones.
            if owner.get(key, "") == e.workspace:
                e.related.append(key)
            else:
                external.append(key)
        e.related_external = external
        e.related_unresolved = still_missing


def _resolve_workspace_ref(
    ref: str,
    workspace: str,
    idx: dict[str, list[Path]],
    workspaces: set[str],
) -> Path | None:
    """One ref, resolved against another workspace's notes.

    Two spellings occur. A prefixed ref (``work/People/X``) names its workspace
    outright. A pre-migration ref is unprefixed (``People/Oliver`` inside the
    work root), and after the split that can only mean another root — tried
    against each, and accepted only when exactly one has it, because guessing
    between two same-named notes is how a link lands on the wrong person.
    """
    value = ref.strip()
    if value.startswith("memory-vault/"):
        value = value[len("memory-vault/"):]
    if value.endswith(".md"):
        value = value[:-3]
    if not value:
        return None
    head = value.split("/", 1)[0]
    if head in workspaces:
        hits = idx.get(value)
        return hits[0] if hits else None
    others = sorted(name for name in workspaces if name and name != workspace)
    found = [hit for name in others for hit in idx.get(f"{name}/{value}", [])]
    return found[0] if len(found) == 1 else None


def vocabulary_report(entries: list[Entry]) -> dict[str, Any]:
    """Summarize the vocabulary actually in use across ``entries``.

    ``types`` counts canonical values; ``type_drift`` maps each non-canonical
    value to its alias target (``""`` when there is none) and the paths using
    it. Tags are counted with the workspaces they appear in, so a work-flavoured
    tag isn't offered to a personal chat as an established choice.
    """
    types: dict[str, int] = defaultdict(int)
    drift: dict[str, dict[str, Any]] = {}
    tags: dict[str, int] = defaultdict(int)
    tag_workspaces: dict[str, set[str]] = defaultdict(set)

    for entry in entries:
        raw = (entry.type or "").strip()
        if raw:
            canonical = canonical_type(raw)
            if canonical == raw:
                types[raw] += 1
            else:
                record = drift.setdefault(
                    raw, {"suggested": canonical, "paths": []}
                )
                record["paths"].append(str(entry.path))
        for tag in entry.tags:
            tags[tag] += 1
            tag_workspaces[tag].add(entry.workspace)

    for record in drift.values():
        record["paths"].sort()
    return {
        "types": dict(sorted(types.items())),
        "type_drift": dict(sorted(drift.items())),
        "tags": dict(sorted(tags.items())),
        "tag_workspaces": {
            tag: sorted(names) for tag, names in sorted(tag_workspaces.items())
        },
        "unused_canonical_types": sorted(CANONICAL_TYPES - set(types)),
    }


def format_vocabulary(entries: list[Entry]) -> str:
    """Render the vocabulary as the agent-facing `VOCABULARY.md` body.

    Types are a closed set, so they are listed with counts and any drift is
    called out with its target. Tags stay open, so they are stratified by use:
    an established tag should be reused, a one-off is a merge candidate or a
    typo. Advice for tags, enforcement for types.
    """
    report = vocabulary_report(entries)
    tags: dict[str, int] = report["tags"]
    workspaces: dict[str, list[str]] = report["tag_workspaces"]
    lines: list[str] = []
    established = promotion_threshold()

    lines.append("## Types (canonical — choose one of these)\n")
    for name in sorted(CANONICAL_TYPES):
        count = report["types"].get(name, 0)
        lines.append(f"- `{name}` ({count})")

    if report["type_drift"]:
        lines.append("\n## Types (drift — not canonical, rename these)\n")
        for name, record in report["type_drift"].items():
            target = record["suggested"] or "no canonical equivalent — decide"
            lines.append(f"- `{name}` → `{target}`")
            for path in record["paths"]:
                lines.append(f"  - {path}")

    def _tier(title: str, hint: str, keep) -> None:
        picked = sorted(tag for tag, count in tags.items() if keep(count))
        if not picked:
            return
        lines.append(f"\n## {title}\n")
        lines.append(f"{hint}\n")
        for tag in picked:
            where = ", ".join(workspaces.get(tag, []))
            suffix = f" — {where}" if where else ""
            lines.append(f"- `{tag}` ({tags[tag]}){suffix}")

    _tier(
        "Tags (established)",
        f"{established} or more uses. Prefer one of these over inventing a new tag.",
        lambda count: count >= established,
    )
    _tier(
        "Tags (emerging)",
        "Two to four uses. Reuse when it fits; these are becoming conventions.",
        lambda count: 2 <= count < 5,
    )
    _tier(
        "Tags (candidates)",
        "Used once — a merge candidate or a typo, not yet a convention.",
        lambda count: count == 1,
    )
    return "\n".join(lines).rstrip() + "\n"


def write_vocabulary_file(entries: list[Entry], dest: Path) -> None:
    """Write `VOCABULARY.md`.

    Deliberately carries no generated-at timestamp, unlike ``INDEX.md``: this
    file is read by the memory agent before it writes frontmatter, and a
    timestamp would dirty it in git on every rebuild even when the vocabulary
    itself never moved.
    """
    header = (
        "<!-- generated by ciao vault-index, do not edit by hand -->\n\n"
        "# Vault Vocabulary\n\n"
        "The categories this vault already uses. Pick `type:` from the "
        "canonical list — it is a closed set. Prefer an existing tag over a new "
        "one; when a new tag is genuinely needed, use `namespace/value` form "
        "(e.g. `project/active`, `product/barcode-capture`).\n\n"
    )
    dest.write_text(header + format_vocabulary(entries), encoding="utf-8")


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
        # Same parsed frontmatter, no extra I/O: the vocabulary is a second
        # rendering of the entries already in hand.
        vocabulary = vault_root / "VOCABULARY.md"
        write_vocabulary_file(entries, vocabulary)
        drift = vocabulary_report(entries)["type_drift"]
        print(
            f"wrote {vocabulary} ({len(drift)} non-canonical type"
            f"{'' if len(drift) == 1 else 's'})",
            file=sys.stderr,
        )
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
