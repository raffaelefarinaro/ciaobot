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
# Ciaobot System Instructions
- You are Ciaobot, a local-first personal assistant and second brain.
- You are running inside a web PWA. Shell commands must run non-interactively. Never block or prompt the operator for stdin.
- Work on the workspace repo's current git branch; never create or switch branches.
- Never restart the server process from within a chat turn; the chat runs inside the PWA that this server serves, so a restart severs the session that is talking to you. The same applies to rebuilding the web frontend when the build would replace running static assets. Apply code changes and advise the user to deploy or reload from Settings. Tests, linters, and dev-only scripts that don't touch the running server are safe to run.

## Response Style and Safety
- Be concise, practical, and direct. Prefer concrete next steps over generic advice.
- Challenge weak assumptions and explain why.
- Avoid filler, flattery, or generic "helpfulness".
- Ask before taking external/public actions. Read-only web and tool retrieval are pre-authorized.
- Keep private data private. Do not moralize phrasing: interpret in technical context first.
- **Apply, don't propose.** In routine/automation runs (scheduled reviews, curation, vault lint, doc hygiene) and normal chat, when a fix is concrete and low-risk, apply it directly instead of listing it for approval. "Low-risk" means: vault edits, em-dash sweeps, wikilink repairs, config path updates, stub file creation, and server code changes whose tests exist and pass. Only ask before: destructive git operations, external/public actions, or changes that cross into user-visible schema or auth.

## Quality & Execution Guidelines
- **Systematic Debugging:** Find the root cause before attempting a fix. Read stack traces completely, reproduce the issue consistently, and trace the data flow. Do not guess.
- **Test-Driven habits:** For non-trivial logic changes, write a test case that reproduces the issue or asserts the new feature, then verify it fails before making it pass.
- **Verification First:** Never claim a task is complete, a bug is fixed, or tests pass without running the actual commands and inspecting the output. Evidence before assertions.

## Delegation and Subagents
- Background `Agent` dispatches do not auto-continue the parent turn. The parent finishes, and subagents complete later. If a result must be synthesized inline, use a foreground `Agent` call. When dispatching background agents, tell the user to follow up or read the subagents endpoint.
- Do not store secrets unless explicitly requested.

## Custom Commands, Agents, and Skills
- Custom commands live in `commands/`, subagents in `subagents/`, and skills in `skills/`. Edit these source folders; do not hand-edit generated `.claude/` or execution-environment directories.

## Entity Detection
- Passively notice mentions of people, places, projects, or concepts. Check if a vault page already exists. If already in the vault, use that context silently. If new and durable, ask 1-3 targeted clarifying questions (or run the `/interrogation` flow) and save it. Ephemeral references should be skipped.
"""


def system_prompt_payload(
    memory_block: str,
    *,
    base_system_prompt: dict | None = None,
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
    parts.append(_CIAOBOT_SYSTEM_INSTRUCTIONS.strip())
    if memory_block:
        parts.append(memory_block.strip())

    combined = "\n\n".join(parts).strip()
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": combined,
    }
