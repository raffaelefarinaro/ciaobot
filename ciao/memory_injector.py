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

import logging
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


def _section(title: str, entries: list[str], limit: int) -> str | None:
    """Render one labeled memory section. Empty files return None."""
    if not entries:
        return None
    used = total_chars(entries)
    pct = (used / limit * 100) if limit else 0
    header = f"{title} [{pct:.0f}% — {used:,}/{limit:,} chars]"
    body = serialize_entries(entries).rstrip()
    return f"{_RULE}\n{header}\n{_RULE}\n{body}"


def build_memory_block(
    *,
    memory_dir: Path | None = None,
    memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
    user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
) -> str:
    """Read both files and render the combined block. Empty -> empty string."""
    try:
        mem_entries = load_entries(memory_path(memory_dir))
        usr_entries = load_entries(user_path(memory_dir))
    except Exception:  # noqa: BLE001
        logger.exception("memory_injector: failed to load files")
        return ""

    sections: list[str] = []
    mem_section = _section(_MEMORY_HEADER, mem_entries, memory_char_limit)
    if mem_section:
        sections.append(mem_section)
    usr_section = _section(_USER_HEADER, usr_entries, user_char_limit)
    if usr_section:
        sections.append(usr_section)

    if not sections:
        return ""

    # Short preamble so the model knows what this block is and that the
    # state is read-only until next session. The memory tool docstring
    # (and the tool's own description) handles the "how to edit" half.
    preamble = (
        "The block below is a frozen snapshot of your bounded memory files at "
        "session start. Use the `memory` tool (action=add|replace|remove|read, "
        f"target=memory|user) to edit them; entries are separated by '{SECTION_SEP}' "
        "on its own line. Tool edits persist immediately but only appear in this "
        "block on the next session.\n"
    )
    return preamble + "\n\n".join(sections)


_CIAOBOT_SYSTEM_INSTRUCTIONS = """\
# CiaoBot System Instructions
- You are CiaoBot, a local-first personal assistant.
- You are running inside a web PWA. Shell commands must run non-interactively. Never block or prompt the operator for stdin.
- Workspace config is in `.env`. Operational state is in `.runtime/`. Durable memories/vault pages are in `memory-vault/`.
- Every device runs on its own `dev/<device_name>` branch.
- Never restart the server process from within a chat turn. Advise the user to deploy or reload instead.
"""


def system_prompt_payload(
    memory_block: str,
    *,
    base_system_prompt: dict | None = None,
) -> dict | None:
    """Build a ``SystemPromptPreset`` dict that appends Ciao instructions and ``memory_block``.

    The returned preset appends to Claude Code's default system prompt via the SDK's
    ``SystemPromptPreset`` ``append`` field.
    """
    existing_append = ""
    if isinstance(base_system_prompt, dict):
        existing_append = str(base_system_prompt.get("append") or "")

    parts = []
    if existing_append:
        parts.append(existing_append)
    parts.append(_CIAOBOT_SYSTEM_INSTRUCTIONS.strip())
    if memory_block:
        parts.append(memory_block.strip())

    combined = "\n\n".join(parts).strip()
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": combined,
    }
