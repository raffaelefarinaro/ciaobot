"""Agent-managed memory files at ``~/.ciao/memory.md`` and ``~/.ciao/user.md``.

Two bounded markdown files the agent reads at session start and can edit during
a turn:

* ``memory.md`` — environment facts, conventions, lessons learned, completed
  task diary. Default cap: 2200 chars.
* ``user.md`` — user identity, preferences, communication style, workflow
  habits. Default cap: 1375 chars.

Entries are separated by the ``§`` section sign on its own line. The agent
edits these files through ``ciao memory`` (also exposed through the
``scripts/memory-cli.py`` compatibility wrapper). Both the parent Claude
session and Pi subagents
take the same CLI path — Pi has no MCP support, and Claude subagents cannot
load MCP servers, so a single CLI route keeps behavior in sync.

Actions exposed by the CLI:

* ``add`` — append a new entry. Fails when the file is full or the entry is
  an exact duplicate.
* ``replace`` — substring-match an existing entry and swap its body.
* ``remove`` — substring-match an existing entry and delete it.
* ``read`` — return the current contents + usage so the agent can consolidate.

The injector (``ciao/memory_injector.py``) reads the same files at session
start and renders them into the system prompt. The CLI persists changes to
disk immediately, but the system prompt is a frozen snapshot of session-start
state — so the model's own edits only show up in context on the next session.
That's intentional: it preserves the SDK's prefix cache across turns.
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
"""Soft cap on ``memory.md`` size (chars). Tunable via
``CIAO_MEMORY_CHAR_LIMIT``."""


DEFAULT_USER_CHAR_LIMIT = 1375
"""Soft cap on ``user.md`` size (chars)."""


MAX_ENTRY_CHARS = 600
"""Hard cap on a single entry. Stops the agent from dumping a wall of text
into memory in one tool call."""


TARGETS = ("memory", "user")
MemoryTarget = Literal["memory", "user"]


# Patterns that look like prompt-injection attempts. Memory files end up in
# the system prompt verbatim, so anything that tries to override system
# instructions or smuggle in invisible Unicode gets rejected before it reaches
# disk. The list is intentionally short; the goal is to block the obvious
# cases, not to be a comprehensive sanitizer. Curation happens at the agent
# layer, not here.
_THREAT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore (?:all |the )?previous (?:instructions|context)", re.I),
    re.compile(r"disregard (?:all |the )?previous (?:instructions|context)", re.I),
    re.compile(r"system prompt (?:override|replace|injection)", re.I),
    re.compile(r"</?(?:system|assistant)>", re.I),
    re.compile(r"\[INST\]|\[/INST\]", re.I),
)

# Zero-width / invisible Unicode characters that should never appear in memory.
_INVISIBLE_CHARS = (
    "​",  # zero-width space
    "‌",  # zero-width non-joiner
    "‍",  # zero-width joiner
    "⁠",  # word joiner
    "﻿",  # BOM / zero-width no-break space
    "‪",  # LRE
    "‫",  # RLE
    "‬",  # PDF
    "‭",  # LRO
    "‮",  # RLO
)


# ── Paths ─────────────────────────────────────────────────────────────────


def default_memory_dir() -> Path:
    """Resolve the directory holding memory.md and user.md.

    Defaults to ``~/.ciao``. Overridable via ``CIAO_MEMORY_DIR`` so tests can
    point at a tmp_path.
    """
    override = os.environ.get("CIAO_MEMORY_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ciao"


def memory_path(memory_dir: Path | None = None) -> Path:
    return (memory_dir or default_memory_dir()) / "memory.md"


def user_path(memory_dir: Path | None = None) -> Path:
    return (memory_dir or default_memory_dir()) / "user.md"


def path_for_target(target: MemoryTarget, memory_dir: Path | None = None) -> Path:
    if target == "memory":
        return memory_path(memory_dir)
    if target == "user":
        return user_path(memory_dir)
    raise ValueError(f"unknown memory target: {target!r}")


# ── Entry parsing / serialization ─────────────────────────────────────────


def _normalize(text: str) -> str:
    """Strip BOM, zero-width chars, and CRLF; NFC-normalize."""
    cleaned = text
    for ch in _INVISIBLE_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", cleaned).strip()


def parse_entries(raw: str) -> list[str]:
    """Split file contents on the section separator into trimmed entries."""
    if not raw:
        return []
    # Accept the separator on its own line OR surrounded by whitespace.
    # ``re.split`` keeps both ``\n§\n`` and ``§`` alone if the file is
    # malformed (e.g., hand-edited without the surrounding newlines).
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


# ── Validation ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class ValidationError(Exception):
    """Raised when an entry fails security or shape checks."""

    reason: str

    def __str__(self) -> str:  # noqa: D401
        return self.reason


def _validate_entry(text: str) -> str:
    """Normalize and run security checks. Returns the cleaned entry."""
    cleaned = _normalize(text)
    if not cleaned:
        raise ValidationError("entry is empty after normalization")
    if len(cleaned) > MAX_ENTRY_CHARS:
        raise ValidationError(
            f"entry exceeds per-entry cap of {MAX_ENTRY_CHARS} chars "
            f"(got {len(cleaned)})"
        )
    if SECTION_SEP in cleaned:
        raise ValidationError(
            f"entry must not contain the section separator {SECTION_SEP!r}"
        )
    for pat in _THREAT_PATTERNS:
        if pat.search(cleaned):
            raise ValidationError(
                "entry rejected: looks like a prompt-injection attempt"
            )
    return cleaned


# ── File I/O ──────────────────────────────────────────────────────────────


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_entries(path: Path) -> list[str]:
    """Read entries from disk. Missing file returns an empty list."""
    try:
        return parse_entries(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except OSError:
        logger.exception("memory: failed to read %s", path)
        return []


def save_entries(path: Path, entries: list[str]) -> None:
    """Persist entries to disk. Caller must have validated each one."""
    _ensure_dir(path)
    path.write_text(serialize_entries(entries), encoding="utf-8")


# ── Actions ───────────────────────────────────────────────────────────────


def _usage_payload(entries: list[str], limit: int) -> dict[str, Any]:
    used = total_chars(entries)
    pct = (used / limit * 100) if limit else 0
    return {
        "used_chars": used,
        "char_limit": limit,
        "pct": round(pct, 1),
        "entry_count": len(entries),
    }


def add_entry(
    path: Path, text: str, *, char_limit: int
) -> dict[str, Any]:
    """Append ``text`` as a new entry. Returns a status dict."""
    try:
        cleaned = _validate_entry(text)
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    entries = load_entries(path)
    if cleaned in entries:
        return {
            "ok": False,
            "error": "entry already present (exact duplicate)",
            **_usage_payload(entries, char_limit),
        }
    candidate = entries + [cleaned]
    if total_chars(candidate) > char_limit:
        return {
            "ok": False,
            "error": (
                "adding this entry would exceed the char limit. "
                "consolidate (merge related entries or remove stale ones) "
                "before adding."
            ),
            "current_entries": entries,
            **_usage_payload(entries, char_limit),
        }
    save_entries(path, candidate)
    return {"ok": True, "added": cleaned, **_usage_payload(candidate, char_limit)}


def replace_entry(
    path: Path,
    old_text: str,
    new_text: str,
    *,
    char_limit: int,
) -> dict[str, Any]:
    """Substring-match an existing entry and replace its body."""
    try:
        cleaned_new = _validate_entry(new_text)
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    needle = _normalize(old_text)
    if not needle:
        return {"ok": False, "error": "old_text is empty"}

    entries = load_entries(path)
    matches = [i for i, e in enumerate(entries) if needle in e]
    if not matches:
        return {"ok": False, "error": "no entry matches old_text"}
    if len(matches) > 1:
        return {
            "ok": False,
            "error": (
                f"old_text matches {len(matches)} entries; "
                "make it more specific so only one is matched"
            ),
        }
    idx = matches[0]
    candidate = list(entries)
    candidate[idx] = cleaned_new
    # Allow replacement past the limit only when the new text is shorter
    # than the old (so consolidation can always finish even when full).
    if total_chars(candidate) > char_limit and len(cleaned_new) > len(entries[idx]):
        return {
            "ok": False,
            "error": "replacement would exceed the char limit",
            **_usage_payload(entries, char_limit),
        }
    save_entries(path, candidate)
    return {
        "ok": True,
        "replaced": entries[idx],
        "with": cleaned_new,
        **_usage_payload(candidate, char_limit),
    }


def remove_entry(
    path: Path, text: str, *, char_limit: int
) -> dict[str, Any]:
    """Substring-match an existing entry and delete it."""
    needle = _normalize(text)
    if not needle:
        return {"ok": False, "error": "text is empty"}

    entries = load_entries(path)
    matches = [i for i, e in enumerate(entries) if needle in e]
    if not matches:
        return {"ok": False, "error": "no entry matches the given text"}
    if len(matches) > 1:
        return {
            "ok": False,
            "error": (
                f"text matches {len(matches)} entries; "
                "make it more specific so only one is matched"
            ),
        }
    idx = matches[0]
    removed = entries[idx]
    candidate = [e for i, e in enumerate(entries) if i != idx]
    save_entries(path, candidate)
    return {
        "ok": True,
        "removed": removed,
        **_usage_payload(candidate, char_limit),
    }


def read_entries(
    path: Path, *, char_limit: int
) -> dict[str, Any]:
    """Return the file's entries plus current usage stats."""
    entries = load_entries(path)
    return {
        "ok": True,
        "entries": entries,
        **_usage_payload(entries, char_limit),
    }


# ── MCP server registration ───────────────────────────────────────────────
