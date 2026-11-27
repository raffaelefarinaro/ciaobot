"""Render the Ciaobot core. ACP providers receive it inside the engine's context capsule, at the head of the first prompt of each session."""

from __future__ import annotations

import functools
import logging
from importlib import resources
from pathlib import Path

from ciao.tool_path import engine_path_export

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.md"
_MEMORY_GUIDE_PARTS = ("skills", "ciao-capabilities", "references", "memory-operations", "guide.md")
MEMORY_GUIDE_PATH_TOKEN = "{{DURABLE_MEMORY_GUIDE_PATH}}"
MEMORY_PROCEDURES_PATH_TOKEN = "{{DURABLE_MEMORY_PROCEDURES_PATH}}"
#: Expands to ``tool_path.engine_path_export()``, the shell line that puts the
#: running engine's ``ciao`` first on PATH (a stale install can sit earlier).
ENGINE_PATH_TOKEN = "{{ENGINE_PATH}}"


def durable_memory_guide_path() -> Path:
    """Return the installed engine copy, independent of workspace skill overrides."""
    return Path(str(resources.files("ciao.stock").joinpath(*_MEMORY_GUIDE_PARTS))).resolve()


def resolve_prompt_asset_paths(text: str) -> str:
    """Expand packaged asset tokens in prompts before they reach an agent."""
    guide = durable_memory_guide_path()
    return (
        text.replace(MEMORY_GUIDE_PATH_TOKEN, str(guide))
        .replace(MEMORY_PROCEDURES_PATH_TOKEN, str(guide.parent))
        .replace(ENGINE_PATH_TOKEN, engine_path_export())
    )


@functools.lru_cache(maxsize=2)
def _system_instructions() -> str:
    """Load the Ciaobot system-instructions markdown.

    The text lives in ``system_prompt.md`` next to this module so a human can
    read and edit it as plain markdown instead of a Python string literal.
    There is a single prompt for every agent surface: the tool guidance names
    ``ciao`` commands, never MCP tools. Any read error logs and returns ``""``
    so a missing or malformed file never kills a chat.
    """
    try:
        return resolve_prompt_asset_paths(_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")).strip()
    except Exception:  # noqa: BLE001
        logger.exception("core_prompt: failed to load %s", _SYSTEM_PROMPT_PATH)
        return ""


def unified_core() -> str:
    """The Ciaobot core for every provider: the capsule's ``core`` for ACP, unchanged."""
    return _system_instructions()


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
