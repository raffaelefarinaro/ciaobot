"""Programmatic Claude Agent SDK hooks wired by ClaudeProvider.

One hook is wired today: ``PreToolUse`` on ``Bash`` forces background shell
commands to run in the foreground. A background process belongs to the Claude
SDK subprocess and is stopped when the turn ends, while its terminal
notification is not emitted until a later turn resumes the session.

There is deliberately no ``UserPromptSubmit`` hook. Runtime context (date,
active workspace, GWS profile, cwd) and vault entity tags are built once by
``ciao.context.capsule`` and prepended to the request for every provider, so a
Claude-only second injection would duplicate them. ``_runtime_lines`` stays
here because the Codex provider and the Settings context view render the same
block from it.

Kept small and fail-open: any exception becomes a DEBUG log and the
original prompt/tool output reaches the model untouched.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
