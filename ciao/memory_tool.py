"""Bounded agent memory stored as fenced regions inside the workspace ``CLAUDE.md``.

Two regions mirror the former ``~/.ciao/memory.md`` / ``user.md`` pair:

* ``ciao:memory`` — environment facts, conventions, lessons learned.
  Default cap: 2200 chars.
* ``ciao:profile`` — user identity, preferences, communication style.
  Default cap: 1375 chars.

Entries are separated by the ``§`` section sign on its own line. The agent
edits the regions with ``Edit`` like any other file; there is no memory CLI
or control-plane write surface. Caps and hygiene are advisory, reported by
``os_audit`` and acted on by the nightly curation routine.

``write_region`` exists only for the one-time startup migration from the
legacy ``~/.ciao/*.md`` files and archive-time auto-promotion. It is not
exposed to the agent as a command.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────


SECTION_SEP = "§"
"""Section sign U+00A7. On its own line, separates memory entries."""


DEFAULT_MEMORY_CHAR_LIMIT = 2200
"""Advisory cap on the ``ciao:memory`` region (chars). Tunable via
``CIAO_MEMORY_CHAR_LIMIT``."""


DEFAULT_USER_CHAR_LIMIT = 1375
"""Advisory cap on the ``ciao:profile`` region (chars). Tunable via
``CIAO_USER_CHAR_LIMIT``. Named ``user`` for env-var continuity."""


MAX_ENTRY_CHARS = 600
"""Advisory per-entry length threshold reported by ``os_audit``."""


MemoryRegion = Literal["memory", "profile"]
REGIONS: tuple[MemoryRegion, ...] = ("memory", "profile")

_REGION_ALIASES: dict[str, MemoryRegion] = {
    "memory": "memory",
    "profile": "profile",
    "user": "profile",  # legacy target name during tolerant-read window
}


# Zero-width / invisible Unicode characters. Detected by ``os_audit``;
# never silently stripped on read or write.
_INVISIBLE_CHARS = (
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\u2060",  # word joiner
    "\ufeff",  # BOM / zero-width no-break space
    "\u202a",  # LRE
    "\u202b",  # RLE
    "\u202c",  # PDF
    "\u202d",  # LRO
    "\u202e",  # RLO
)

# Only the heading and the advisory cap stamped into the start marker differ
# between regions; the marker grammar itself is uniform, so derive it from the
# region name rather than hand-writing four strings per region.
_REGION_FACTS: dict[MemoryRegion, tuple[str, int]] = {
    "memory": ("## Agent memory", DEFAULT_MEMORY_CHAR_LIMIT),
    "profile": ("## User profile", DEFAULT_USER_CHAR_LIMIT),
}

_REGION_META: dict[MemoryRegion, dict[str, str]] = {
    region: {
        "start": f"<!-- ciao:{region}:start cap={cap} -->",
        "end": f"<!-- ciao:{region}:end -->",
        "heading": heading,
        "start_re": rf"<!--\s*ciao:{region}:start(?:\s+cap=\d+)?\s*-->",
        "end_re": rf"<!--\s*ciao:{region}:end\s*-->",
    }
    for region, (heading, cap) in _REGION_FACTS.items()
}

# Whole fenced blocks (markers plus body), for callers that need to scan a
# guide body without double-counting region entries.
_REGION_BLOCK_RE = re.compile(
    "|".join(
        f"{_REGION_META[region]['start_re']}.*?{_REGION_META[region]['end_re']}"
        for region in REGIONS
    ),
    re.DOTALL,
)


# ── Legacy paths (one-release migration tolerance) ─────────────────────────


def default_memory_dir() -> Path:
    """Resolve the directory that held the legacy memory.md / user.md files.

    Defaults to ``~/.ciao``. Overridable via ``CIAO_MEMORY_DIR`` so tests and
    the migration can point at a tmp path. Removed once the tolerant-read
    window closes.
    """
    override = os.environ.get("CIAO_MEMORY_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ciao"


# ── Entry parsing / serialization ─────────────────────────────────────────


def _normalize(text: str) -> str:
    """CRLF→LF and NFC-normalize. Does not strip invisible Unicode."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", cleaned).strip()


def parse_entries(raw: str) -> list[str]:
    """Split region body on the section separator into trimmed entries."""
    if not raw:
        return []
    parts = re.split(rf"\n?{re.escape(SECTION_SEP)}\n?", raw)
    return [p.strip() for p in parts if p.strip()]


def serialize_entries(entries: list[str]) -> str:
    """Join entries with ``\\n§\\n`` and a trailing newline."""
    if not entries:
        return ""
    return f"\n{SECTION_SEP}\n".join(e.strip() for e in entries) + "\n"


def total_chars(entries: list[str]) -> int:
    """Char count of the serialized form, used for the soft cap."""
    return len(serialize_entries(entries))


def contains_invisible_unicode(text: str) -> bool:
    """True when *text* contains any character from ``_INVISIBLE_CHARS``."""
    return any(ch in text for ch in _INVISIBLE_CHARS)


def region_usage(entries: list[str], limit: int) -> dict[str, Any]:
    """Usage payload shared by audit and Settings."""
    used = total_chars(entries)
    pct = (used / limit * 100) if limit else 0
    return {
        "used_chars": used,
        "char_limit": limit,
        "pct": round(pct, 1),
        "entry_count": len(entries),
    }


# ── Region markers ────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class RegionDiagnostic:
    """Non-fatal problem locating a memory region in a guide."""

    region: str
    code: str
    message: str


def resolve_region(name: str) -> MemoryRegion:
    """Map a region name (including legacy ``user``) to a canonical region."""
    key = name.strip().lower()
    if key not in _REGION_ALIASES:
        raise ValueError(f"unknown memory region: {name!r}")
    return _REGION_ALIASES[key]


def _find_marker_spans(
    text: str, region: MemoryRegion
) -> tuple[list[re.Match[str]], list[re.Match[str]]]:
    meta = _REGION_META[region]
    starts = list(re.finditer(meta["start_re"], text))
    ends = list(re.finditer(meta["end_re"], text))
    return starts, ends


def diagnose_region(guide_text: str, region: MemoryRegion) -> list[RegionDiagnostic]:
    """Return diagnostics for missing / duplicated / inverted markers."""
    starts, ends = _find_marker_spans(guide_text, region)
    diags: list[RegionDiagnostic] = []
    if not starts and not ends:
        diags.append(
            RegionDiagnostic(
                region,
                "missing",
                f"region {region!r} markers are missing",
            )
        )
        return diags
    for kind, matches in (("start", starts), ("end", ends)):
        if len(matches) != 1:
            diags.append(
                RegionDiagnostic(
                    region,
                    "duplicated" if len(matches) > 1 else "missing",
                    f"region {region!r} has {len(matches)} {kind} marker(s); expected 1",
                )
            )
    if len(starts) == 1 and len(ends) == 1 and starts[0].start() > ends[0].start():
        diags.append(
            RegionDiagnostic(
                region,
                "inverted",
                f"region {region!r} end marker appears before start marker",
            )
        )
    return diags


def diagnose_guide(guide: Path) -> list[RegionDiagnostic]:
    """Diagnose marker health for every region in *guide*.

    An empty result means all regions are present and well-formed. A guide
    that cannot be read yields a single diagnostic, so callers can treat any
    non-empty result as "the regions are not usable".
    """
    try:
        text = guide.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [RegionDiagnostic("guide", "missing_file", f"guide not found: {guide}")]
    except OSError as exc:
        logger.exception("memory: failed to read guide %s", guide)
        return [RegionDiagnostic("guide", "io_error", f"failed to read guide: {exc}")]
    return [diag for region in REGIONS for diag in diagnose_region(text, region)]


def strip_region_blocks(text: str) -> str:
    """Remove every fenced memory region — markers and body — from *text*.

    Region entries are audited as their own sources, so a caller scanning the
    surrounding guide body must not count them twice.
    """
    return _REGION_BLOCK_RE.sub("", text)


def _region_body(guide_text: str, region: MemoryRegion) -> tuple[str, list[RegionDiagnostic]]:
    diags = diagnose_region(guide_text, region)
    if diags:
        return "", diags
    starts, ends = _find_marker_spans(guide_text, region)
    start, end = starts[0], ends[0]
    body = guide_text[start.end() : end.start()]
    body = re.sub(
        rf"^\s*{re.escape(_REGION_META[region]['heading'])}\s*\n?",
        "",
        body,
        count=1,
    )
    return body, []


def read_region(
    guide: Path, region: str
) -> tuple[list[str], list[RegionDiagnostic]]:
    """Locate markers in *guide* and parse entries.

    Missing or duplicated markers return ``([], diagnostics)`` — never raises
    for marker problems. I/O failures also return empty + a diagnostic.
    """
    try:
        canonical = resolve_region(region)
    except ValueError as exc:
        return [], [RegionDiagnostic(region, "unknown", str(exc))]

    try:
        text = guide.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], [
            RegionDiagnostic(
                canonical, "missing_file", f"guide not found: {guide}"
            )
        ]
    except OSError as exc:
        logger.exception("memory: failed to read guide %s", guide)
        return [], [
            RegionDiagnostic(
                canonical, "io_error", f"failed to read guide: {exc}"
            )
        ]

    body, diags = _region_body(text, canonical)
    if diags:
        return [], diags
    return parse_entries(_normalize(body)), []


def write_region(guide: Path, region: str, entries: list[str]) -> None:
    """Rewrite only the body of *region* inside *guide*.

    Used by the one-time legacy migration and archive-time auto-promotion.
    Refuses if markers are missing, duplicated, or inverted.
    """
    canonical = resolve_region(region)
    text = guide.read_text(encoding="utf-8")
    diags = diagnose_region(text, canonical)
    if diags:
        raise ValueError("; ".join(d.message for d in diags))

    starts, ends = _find_marker_spans(text, canonical)
    start, end = starts[0], ends[0]
    meta = _REGION_META[canonical]
    body = f"\n{meta['heading']}\n\n"
    serialized = serialize_entries(entries)
    if serialized:
        body += serialized
    else:
        body += "\n"
    guide.write_text(
        text[: start.end()] + body + text[end.start() :],
        encoding="utf-8",
    )


def _empty_region_block(region: MemoryRegion) -> str:
    meta = _REGION_META[region]
    return (
        f"{meta['start']}\n"
        f"{meta['heading']}\n\n"
        f"{meta['end']}\n"
    )


def ensure_regions(guide: Path) -> list[str]:
    """Append any missing memory regions to *guide*. Idempotent.

    Never rewrites existing content. Creates the guide with both empty
    regions when the file is absent. Returns the list of regions added.
    """
    added: list[str] = []
    if not guide.exists():
        guide.parent.mkdir(parents=True, exist_ok=True)
        guide.write_text(
            "# Ciaobot Workspace Guide\n\n"
            + _empty_region_block("memory")
            + "\n"
            + _empty_region_block("profile"),
            encoding="utf-8",
        )
        return list(REGIONS)

    text = guide.read_text(encoding="utf-8")
    append_parts: list[str] = []
    for region in REGIONS:
        starts, ends = _find_marker_spans(text, region)
        if starts or ends:
            continue
        append_parts.append(_empty_region_block(region))
        added.append(region)

    if append_parts:
        suffix = "\n\n" + "\n".join(append_parts)
        if not text.endswith("\n"):
            suffix = "\n" + suffix
        guide.write_text(text + suffix, encoding="utf-8")
    return added


def load_legacy_entries(path: Path) -> list[str]:
    """Read entries from a legacy ``memory.md`` / ``user.md`` file."""
    try:
        return parse_entries(_normalize(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    except OSError:
        logger.exception("memory: failed to read legacy %s", path)
        return []


def migrate_legacy_files(
    guide: Path,
    *,
    memory_dir: Path | None = None,
) -> dict[str, Any]:
    """Ensure regions exist, then fold non-empty legacy files into them.

    Renames each migrated source to ``*.migrated``. Idempotent when legacy
    files are already renamed or absent.
    """
    ensure_regions(guide)
    root = memory_dir or default_memory_dir()
    mapping: list[tuple[MemoryRegion, Path]] = [
        ("memory", root / "memory.md"),
        ("profile", root / "user.md"),
    ]
    migrated: list[str] = []
    for region, source in mapping:
        if not source.is_file():
            continue
        entries = load_legacy_entries(source)
        if not entries:
            source.rename(source.with_suffix(source.suffix + ".migrated"))
            migrated.append(str(source))
            continue
        existing, diags = read_region(guide, region)
        if diags:
            raise ValueError(
                f"cannot migrate into {region}: "
                + "; ".join(d.message for d in diags)
            )
        merged = list(existing)
        for entry in entries:
            if entry not in merged:
                merged.append(entry)
        write_region(guide, region, merged)
        source.rename(source.with_suffix(source.suffix + ".migrated"))
        migrated.append(str(source))
    return {"ok": True, "migrated": migrated, "guide": str(guide)}
