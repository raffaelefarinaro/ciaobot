"""Render the Ciaobot core into provider system prompts.

Used by Claude and OpenCode at session start. Native provider guide
loaders read ``CLAUDE.md``/``AGENTS.md`` separately, bounded memory included,
so nothing here renders the regions. The returned block is appended to Claude
Code's default system prompt via the SDK's ``SystemPromptPreset`` ``append``
field, and is passed as developer instructions to the other providers.

Failure mode: any error in loading or formatting returns an empty string. We
never want a malformed file to kill a chat.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.md"


@functools.lru_cache(maxsize=1)
def _system_instructions() -> str:
    """Load and cache the Ciaobot system-instructions markdown.

    The text lives in ``system_prompt.md`` next to this module so a human can
    read and edit it as plain markdown instead of a Python string literal. Any
    read error logs and returns ``""`` so a missing or malformed file never
    kills a chat.
    """
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        logger.exception("core_prompt: failed to load %s", _SYSTEM_PROMPT_PATH)
        return ""


def system_prompt_payload(
    memory_block: str,
    *,
    base_system_prompt: dict | None = None,
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
    parts.append(_system_instructions())
    if memory_block:
        parts.append(memory_block.strip())

    combined = "\n\n".join(parts).strip()
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": combined,
        "exclude_dynamic_sections": True,
    }
