"""Render ``~/.ciao/memory.md`` and ``~/.ciao/user.md`` into the system prompt.

Used by :class:`ciao.providers.claude.ClaudeProvider` at session start. The
returned block is appended to Claude Code's default system prompt via the SDK's
``SystemPromptPreset`` ``append`` field, so CLAUDE.md, skills, and agent
discovery keep working untouched.

Frozen-snapshot rule: this block is captured once per session. The
:mod:`ciao.memory_tool` writes change disk immediately, but the model only
sees the new state on the next session. Keeping the block stable preserves
the SDK's prefix cache across turns.

Failure mode: any error in loading or formatting returns an empty string. We
never want a malformed memory file to kill a chat.
"""

from __future__ import annotations

import datetime
import functools
import logging
import re
from pathlib import Path

from ciao.memory_tool import (
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
    SECTION_SEP,
    load_entries,
    memory_path,
    serialize_entries,
    total_chars,
    user_path,
)

logger = logging.getLogger(__name__)


_RULE = "═" * 46
_MEMORY_HEADER = "MEMORY (your personal notes)"
_USER_HEADER = "USER PROFILE"

# Rendered when both memory files are empty. Without this, a fresh install
# never shows the memory block at all, so the model has no visible cue to
# seed entry #1 (the block itself is the reinforcement loop once non-empty).
_EMPTY_STATE_NUDGE = (
    "Your bounded memory files (~/.ciao/memory.md and ~/.ciao/user.md) are "
    "empty. When you learn a durable fact this session, save it: "
    '`ciao memory add --target memory --text "…"` for preferences, '
    "environment facts, and lessons learned; `--target user` for the user's "
    "identity, role, and communication style. Entries persist immediately "
    "and appear in this block from the next session on."
)

_EXPIRATION_TAG_RE = re.compile(r"\[expires:\s*([^\]]*)\]", re.IGNORECASE)
_EXPIRATION_TAG_PREFIX_RE = re.compile(r"\[expires\s*:", re.IGNORECASE)
_EXPIRATION_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def entry_expiration_date(entry: str) -> datetime.date | None:
    """Return the single valid ``[expires: YYYY-MM-DD]`` date in *entry*."""
    if expiration_tag_error(entry) is not None:
        return None
    match = _EXPIRATION_TAG_RE.search(entry)
    if not match:
        return None
    return datetime.date.fromisoformat(match.group(1).strip())


def expiration_tag_error(entry: str) -> str | None:
    """Return a validation message for a malformed expiration tag."""
    matches = list(_EXPIRATION_TAG_RE.finditer(entry))
    prefixes = list(_EXPIRATION_TAG_PREFIX_RE.finditer(entry))
    if len(prefixes) > len(matches):
        return "malformed expiration tag; expected a closing ']'"
    if not matches:
        return None
    if len(matches) > 1:
        return "multiple expiration tags are not allowed on one entry"
    raw = matches[0].group(1).strip()
    if _EXPIRATION_DATE_RE.fullmatch(raw) is None:
        return f"invalid expiration date {raw!r}; expected YYYY-MM-DD"
    try:
        datetime.date.fromisoformat(raw)
    except ValueError:
        return f"invalid expiration date {raw!r}; expected YYYY-MM-DD"
    return None


def is_entry_expired(entry: str, today: datetime.date | None = None) -> bool:
    """Check if an entry contains `[expires: YYYY-MM-DD]` and whether that date has passed."""
    exp_date = entry_expiration_date(entry)
    if exp_date is None:
        return False
    current = today or datetime.date.today()
    return current > exp_date


def _section(
    title: str,
    entries: list[str],
    limit: int,
    *,
    stored_entries: list[str] | None = None,
    expired_count: int = 0,
) -> str | None:
    """Render one labeled memory section. Empty files return None."""
    stored = entries if stored_entries is None else stored_entries
    if not stored:
        return None
    active_used = total_chars(entries)
    stored_used = total_chars(stored)
    if expired_count:
        header = (
            f"{title} [active {active_used:,} chars; stored "
            f"{stored_used:,}/{limit:,} chars; {expired_count} expired]"
        )
    else:
        pct = (stored_used / limit * 100) if limit else 0
        header = f"{title} [{pct:.0f}% — {stored_used:,}/{limit:,} chars]"
    if entries:
        body = serialize_entries(entries).rstrip()
    else:
        body = (
            "All stored entries in this section are expired and omitted from "
            "the prompt. Use the memory tools to remove expired entries and "
            "reclaim their stored character budget."
        )
    return f"{_RULE}\n{header}\n{_RULE}\n{body}"


def build_memory_block(
    *,
    memory_dir: Path | None = None,
    memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
    user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
    today: datetime.date | None = None,
) -> str:
    """Read both files and render the combined block. Expired entries are filtered out."""
    try:
        mem_entries = load_entries(memory_path(memory_dir))
        usr_entries = load_entries(user_path(memory_dir))
    except Exception:  # noqa: BLE001
        logger.exception("memory_injector: failed to load files")
        return ""

    stored_mem_entries = mem_entries
    stored_usr_entries = usr_entries
    mem_entries = [e for e in stored_mem_entries if not is_entry_expired(e, today)]
    usr_entries = [e for e in stored_usr_entries if not is_entry_expired(e, today)]
    expired_mem = len(stored_mem_entries) - len(mem_entries)
    expired_usr = len(stored_usr_entries) - len(usr_entries)

    sections: list[str] = []
    mem_section = _section(
        _MEMORY_HEADER,
        mem_entries,
        memory_char_limit,
        stored_entries=stored_mem_entries,
        expired_count=expired_mem,
    )
    if mem_section:
        sections.append(mem_section)
    usr_section = _section(
        _USER_HEADER,
        usr_entries,
        user_char_limit,
        stored_entries=stored_usr_entries,
        expired_count=expired_usr,
    )
    if usr_section:
        sections.append(usr_section)

    if not sections:
        return _EMPTY_STATE_NUDGE

    # Short preamble so the model knows what this block is and that the
    # state is read-only until next session. Editing is via `ciao memory`
    # (documented in system_prompt.md); see tests/test_memory_injector.py.
    preamble = (
        "The block below is a frozen snapshot of your bounded memory files at "
        "session start. Edit them with `ciao memory read|add|replace|remove "
        f"(--target memory|user); entries are separated by '{SECTION_SEP}' "
        "on its own line. CLI edits persist immediately but only appear in this "
        "block on the next session.\n"
    )
    return preamble + "\n\n".join(sections)


_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.md"


@functools.lru_cache(maxsize=1)
def _system_instructions() -> str:
    """Load and cache the Ciaobot system-instructions markdown.

    The text lives in ``system_prompt.md`` next to this module so a human can
    read and edit it as plain markdown instead of a Python string literal. Any
    read error logs and returns ``""`` so a missing or malformed file never
    kills a chat (same failure posture as :func:`build_memory_block`).
    """
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        logger.exception("memory_injector: failed to load %s", _SYSTEM_PROMPT_PATH)
        return ""


def system_prompt_payload(
    memory_block: str,
    *,
    base_system_prompt: dict | None = None,
    control_surface: str = "legacy",
) -> dict | None:
    """Build a ``SystemPromptPreset`` dict that appends Ciaobot instructions and ``memory_block``.

    The returned preset appends to Claude Code's default system prompt via the SDK's
    ``SystemPromptPreset`` ``append`` field.
    """
    existing_append = ""
    if isinstance(base_system_prompt, dict):
        existing_append = str(base_system_prompt.get("append") or "")

    parts = []
    if existing_append:
        parts.append(existing_append)
    parts.append("[SYSTEM EXPERTISE: SOPs & Durable Memory]")
    instructions = _system_instructions()
    if control_surface == "mcp":
        instructions = _mcp_system_instructions(instructions)
    parts.append(instructions)
    if memory_block:
        block = memory_block.strip()
        if control_surface == "mcp":
            block = block.replace(
                "Edit them with `ciao memory read|add|replace|remove "
                "(--target memory|user);",
                "Edit them with the Ciaobot MCP memory tools;",
            ).replace(
                "CLI edits persist immediately but only appear in this block on the next session.",
                "Tool edits persist immediately but only appear in this block on the next session.",
            )
        parts.append(block)

    combined = "\n\n".join(parts).strip()
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": combined,
    }


def _mcp_system_instructions(instructions: str) -> str:
    """Strip legacy transport recipes when the managed process has typed MCP tools.

    The behavioral policy (security, approvals, workspace identity, memory
    semantics, entity detection, canonical docs, gws security) is identical to
    the legacy arm. Only the CLI/curl/direct-file recipes are removed: the typed
    MCP tools are self-describing and the server-level instructions already state
    the prefer-MCP policy, so repeating transport recipes in the prompt is noise.
    """
    # Drop the bounded-memory CLI recipe sentence.
    text = instructions.replace(
        " Edit with `ciao memory read|add|replace|remove --target memory|user --text \"…\"`.",
        "",
    )
    # Drop the vault CLI fallback + hygiene recipe lines.
    text = text.replace(
        "- Direct CLI fallback: `ciao vault-search \"<query>\" --limit 5`; rebuild stale search/entity data with `ciao vault-index`.\n",
        "",
    ).replace(
        "\n- Vault hygiene: `ciao vault-lint` for broken wikilinks, orphans, and near-duplicates.",
        "",
    )
    # Replace the legacy "Other agent CLIs" recipe block with a single MCP nudge.
    # The typed tools carry their own usage; gws security lives in its own section.
    mcp_nudge = (
        "Use the authenticated Ciaobot MCP tools; prefer them over curl, the "
        "ciao CLI, or direct `.runtime` edits.\n\n"
    )
    text = re.sub(
        r"\*\*Other agent CLIs\*\*.*?(?=\*\*Background memory routines\*\*)",
        mcp_nudge,
        text,
        flags=re.DOTALL,
    )
    # Drop the diagnostics `.runtime` file-path recipe; keep the behavior.
    text = text.replace(
        "inspect local runtime evidence before speculating: `.runtime/server_errors.log`, "
        "`.runtime/job_runs.jsonl`, and, for macOS service/startup problems, "
        "`.runtime/ciao.stderr.log` and `.runtime/ciao.stdout.log` when present. "
        "Use focused tails or summaries; do not dump full logs.",
        "gather diagnostic evidence before speculating; keep excerpts focused.",
    )
    return text
