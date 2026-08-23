"""Render the Ciaobot core into provider system prompts.

Used by Claude, Codex, and OpenCode at session start. Native provider guide
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
import re
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
