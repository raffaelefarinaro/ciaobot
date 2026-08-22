"""Render the compact Ciaobot core into provider system prompts.

Used by Claude, Codex, and OpenCode at session start. Native provider guide
loaders read ``CLAUDE.md``/``AGENTS.md`` separately; bounded memory is not
rendered here. The returned block is appended to Claude Code's default system
prompt via the SDK's ``SystemPromptPreset`` ``append`` field, and is passed as
developer instructions to the other providers.

Frozen-snapshot rule: this block is captured once per session. In-session
``Edit`` of the regions lands on disk immediately, but the model only sees
the new state on the next session. Both CLIs already read ``CLAUDE.md`` once
at session start, so region edits follow the same rule.

Failure mode: any error in loading or formatting returns an empty string. We
never want a malformed region to kill a chat.
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
    is_entry_expired,
    read_region,
    serialize_entries,
    total_chars,
)

logger = logging.getLogger(__name__)


_RULE = "═" * 46
_MEMORY_HEADER = "MEMORY (your personal notes)"
_USER_HEADER = "USER PROFILE"

# Rendered when both regions are empty. Without this, a fresh install
# never shows the memory block at all, so the model has no visible cue to
# seed entry #1 (the block itself is the reinforcement loop once non-empty).
_EMPTY_STATE_NUDGE = (
    "Your bounded memory regions in the workspace CLAUDE.md "
    "(`ciao:memory` and `ciao:profile`) are empty. When you learn a durable "
    "fact this session, Edit the matching region: `ciao:memory` for "
    "preferences, environment facts, and lessons learned; `ciao:profile` for "
    f"the user's identity, role, and communication style. Separate entries "
    f"with '{SECTION_SEP}' on its own line. Edits persist immediately and "
    "appear in this block from the next session on."
)

def _section(
    title: str,
    entries: list[str],
    limit: int,
    *,
    stored_entries: list[str] | None = None,
    expired_count: int = 0,
) -> str | None:
    """Render one labeled memory section. Empty regions return None."""
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
            "the prompt. Edit the matching CLAUDE.md region to remove expired "
            "entries and reclaim their stored character budget."
        )
    return f"{_RULE}\n{header}\n{_RULE}\n{body}"


def build_memory_block(
    *,
    guide_path: Path | None = None,
    memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
    user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
    today: datetime.date | None = None,
) -> str:
    """Read both CLAUDE.md regions and render the combined block.

    Expired entries are filtered out of the prompt but still count toward
    the stored usage shown in the header.
    """
    if guide_path is None:
        logger.warning("memory_injector: guide_path is required; returning empty")
        return ""
    try:
        mem_entries, mem_diags = read_region(guide_path, "memory")
        usr_entries, usr_diags = read_region(guide_path, "profile")
        for diag in (*mem_diags, *usr_diags):
            logger.info("memory_injector: %s (%s)", diag.message, diag.code)
    except Exception:  # noqa: BLE001
        logger.exception("memory_injector: failed to load regions from %s", guide_path)
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
    # state is read-only until next session. Editing is via Edit on CLAUDE.md.
    preamble = (
        "The block below is a frozen snapshot of the bounded memory regions "
        "in the workspace CLAUDE.md at session start. Edit the `ciao:memory` "
        "and `ciao:profile` regions with Edit; entries are separated by "
        f"'{SECTION_SEP}' on its own line. Edits persist immediately but only "
        "appear in this block on the next session. Each section header carries "
        "current usage — that is the only usage signal; there is no memory "
        "command.\n"
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
    ``SystemPromptPreset`` ``append`` field. ``exclude_dynamic_sections`` moves
    per-session cwd / git / OS / auto-memory paths into the first user message so
    the static preset + append stay cacheable across sessions (Claude SDK ≥0.1.58).
    """
    existing_append = ""
    if isinstance(base_system_prompt, dict):
        existing_append = str(base_system_prompt.get("append") or "")

    parts = []
    if existing_append:
        parts.append(existing_append)
    parts.append("[SYSTEM EXPERTISE: Ciaobot core]")
    instructions = _system_instructions()
    if control_surface == "mcp":
        instructions = _mcp_system_instructions(instructions)
    parts.append(instructions)
    if memory_block:
        parts.append(memory_block.strip())

    combined = "\n\n".join(parts).strip()
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": combined,
        "exclude_dynamic_sections": True,
    }


def _mcp_system_instructions(instructions: str) -> str:
    """Strip legacy transport recipes when the managed process has typed MCP tools.

    The behavioral policy (security, approvals, workspace identity, memory
    semantics, entity detection, canonical docs, gws security) is identical to
    the legacy arm. Only the CLI/curl/direct-file recipes are removed: the typed
    MCP tools are self-describing and the server-level instructions already state
    the prefer-MCP policy, so repeating transport recipes in the prompt is noise.
    """
    # Drop the vault CLI fallback + hygiene recipe lines.
    text = instructions.replace(
        "- Direct CLI fallback: `ciao vault-search \"<query>\" --limit 5`; rebuild stale search/entity data with `ciao vault-index`.\n",
        "",
    ).replace(
        "\n- Vault hygiene: `ciao vault-lint` for broken wikilinks, orphans, and near-duplicates.",
        "",
    )
    # Replace the legacy "Other agent CLIs" recipe block with a single MCP nudge.
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
