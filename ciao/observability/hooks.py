"""Programmatic Claude Agent SDK hooks wired by ClaudeProvider.

Three hooks are wired today:

1. ``UserPromptSubmit`` injects two things into the model's context
   before it sees a user turn.
   a. Compact runtime context: today's date, active workspace, and GWS
      profile. Keeps schedules and reconnected sessions in sync without
      the user having to restate them.
   b. Vault entity tags: whole-word matches against memory-vault/INDEX.md
      get surfaced as ``- [[People/Name]] (person)`` bullets so the model
      can load the right note without guessing who "Emma" or "Ciaobot-
      Improvements" refers to.
2. ``PreToolUse`` on ``Bash`` forces background shell commands to run in the
   foreground. A background process belongs to the Claude SDK subprocess and
   is stopped when the turn ends, while its terminal notification is not
   emitted until a later turn resumes the session.

Kept small and fail-open: any exception becomes a DEBUG log and the
original prompt/tool output reaches the model untouched.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.context.entity_tagger import find_entities, format_entities

logger = logging.getLogger(__name__)

# The injected context wrapper (project context, canonical doc path, prior
# entity block) is prepended to the prompt before this hook runs. Scanning
# it makes entity detection trigger on injected file paths and boilerplate
# instead of what the user actually typed, so strip it before matching.
_CIAO_CONTEXT_RE = re.compile(r"(?s)^\[CIAO_CONTEXT_BEGIN\].*?\[CIAO_CONTEXT_END\]\s*")


def build_foreground_bash_hook():
    """Return a PreToolUse callback that keeps Bash inside the active turn.

    Claude Code's background Bash process is owned by the managed CLI
    subprocess. If the model ends the turn after dispatch, the process is
    stopped and its ``<task-notification>`` is only written when a later turn
    resumes the session. Rewriting the call keeps the provider stream open
    until Bash returns a real result that the model can report.

    Background ``Agent`` calls are intentionally untouched. Ciaobot has a
    separate durable watcher and UI state for those.
    """

    async def on_pre_tool_use(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,  # HookContext; untyped here to avoid an import cycle
    ) -> dict[str, Any]:
        del tool_use_id, context  # unused
        if input_data.get("tool_name") != "Bash":
            return {}
        tool_input = input_data.get("tool_input")
        if (
            not isinstance(tool_input, dict)
            or tool_input.get("run_in_background") is not True
        ):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": {**tool_input, "run_in_background": False},
                "additionalContext": (
                    "Ciaobot kept this Bash command in the foreground because "
                    "background shell processes stop when the SDK turn ends. "
                    "Wait for the tool result before replying."
                ),
            }
        }

    return on_pre_tool_use


def _legacy_workspace_context(raw: str | None) -> str:
    """Return old-style logical workspace values carried in CIAO_WORKSPACE.

    Historically Ciaobot used ``CIAO_WORKSPACE=personal|work`` in provider env.
    Public setup needs ``CIAO_WORKSPACE`` to be a filesystem path, so only
    preserve the two legacy context values here.
    """
    value = (raw or "").strip()
    return value if value in {"personal", "work"} else ""


def _runtime_lines(cwd: Path, extra_env: dict[str, str] | None = None) -> list[str]:
    """Collect non-empty key=value runtime context lines.

    ``extra_env`` is the per-request env the provider hands the spawned CLI
    (workspace, GWS profile, active project). The hook callback runs in the
    ciao server process, so ``os.environ`` only ever holds the global
    defaults; merging ``extra_env`` on top is what makes ``workspace=`` track
    the active chat instead of always reading ``personal``. Mirrors
    ``ciao.providers.base.build_runtime_context``.
    """
    env = {**os.environ, **(extra_env or {})}
    lines = [f"today={datetime.now(UTC).date().isoformat()}"]
    workspace = (
        env.get("CIAO_ACTIVE_WORKSPACE")
        or _legacy_workspace_context(env.get("CIAO_WORKSPACE"))
        or env.get("GWS_PROFILE")
    )
    if workspace:
        lines.append(f"workspace={workspace}")
    gws = env.get("GWS_PROFILE")
    if gws and gws != workspace:
        lines.append(f"gws_profile={gws}")
    project = env.get("CIAO_ACTIVE_PROJECT")
    if project:
        lines.append(f"active_project={project}")
    lines.append(f"cwd={cwd}")
    return lines


def build_user_prompt_submit_hook(
    vault_root: Path, extra_env: dict[str, str] | None = None
):
    """Return a UserPromptSubmit callback bound to a vault root.

    ``extra_env`` carries the per-request workspace/profile/project the
    provider built for this chat (see ``_build_extra_env``). Captured in the
    closure so the injected ``<ciao-runtime>`` block reflects the active chat
    rather than the server's global default. The callback shape matches
    claude_agent_sdk.types.HookCallback.
    """

    async def on_user_prompt_submit(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,  # HookContext; untyped here to avoid an import cycle
    ) -> dict[str, Any]:
        del tool_use_id, context  # unused
        try:
            prompt = input_data.get("prompt") or ""
            cwd = Path(input_data.get("cwd") or vault_root.parent)
            runtime = _runtime_lines(cwd, extra_env)
            env = {**os.environ, **(extra_env or {})}
            workspace = (
                env.get("CIAO_ACTIVE_WORKSPACE")
                or _legacy_workspace_context(env.get("CIAO_WORKSPACE"))
                or env.get("GWS_PROFILE")
            )
            legacy_workspace = env.get("CIAO_LEGACY_ENTITY_WORKSPACE", "")
            scan_text = _CIAO_CONTEXT_RE.sub("", prompt)
            entities = find_entities(
                scan_text,
                vault_root,
                workspace=workspace,
                legacy_workspace=legacy_workspace,
            )
            sections: list[str] = ["[SITUATIONAL CONTEXT: Runtime & Vault Entities]"]
            sections.append("<ciao-runtime>\n" + "\n".join(runtime) + "\n</ciao-runtime>")
            tagged = format_entities(entities)
            if tagged:
                sections.append("<ciao-entities>\n" + tagged + "\n</ciao-entities>")
            additional = "\n".join(sections)
        except Exception:  # noqa: BLE001 — never block a user turn on hook failure
            logger.debug("UserPromptSubmit hook failed; skipping", exc_info=True)
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional,
            }
        }

    return on_user_prompt_submit
