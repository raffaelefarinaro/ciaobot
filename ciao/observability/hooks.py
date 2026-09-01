"""Programmatic Claude Agent SDK hooks wired by ClaudeProvider.

Two hooks are wired today, both ``PreToolUse``: one on ``Bash`` forces
background shell commands to run in the foreground and denies detached
invocations (``nohup … &``, a bare trailing ``&``, ``setsid``/``disown``),
and one on ``Monitor`` denies the CLI's built-in watcher. All of those paths
die with the CLI subprocess and never deliver a completion to this chat, so
the denials point the model at the managed ``background_run_start`` MCP tool.
A background process belongs to the Claude SDK subprocess and is stopped when
the turn ends, while its terminal notification is not emitted until a later
turn resumes the session.

There is deliberately no ``UserPromptSubmit`` hook. Runtime context (date,
active workspace, GWS profile, cwd) and vault entity tags are built once by
``ciao.context.capsule`` and prepended to the request for every provider, so a
Claude-only second injection would duplicate them. ``_runtime_lines`` stays
here because the provider and the Settings context view render the same
block from it.

Kept small and fail-open: any exception becomes a DEBUG log and the
original prompt/tool output reaches the model untouched.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


BACKGROUND_RUN_GUIDANCE = (
    "Ciaobot does not run detached shell processes from the Claude CLI: they "
    "belong to the CLI subprocess and are lost when it reconnects, and their "
    "completion is never delivered back to this chat. For a long-running "
    "command use the `background_run_start` MCP tool instead (it survives CLI "
    "restarts and wakes this chat with the exit code, log tail and log path). "
    "Use `background_run_status` only if you need the state mid-turn."
)

# Detached-shell shapes we deny. Quoted substrings are stripped before
# matching, but quotes are not parsed, so an unquoted odd `&` in text (e.g.
# ``echo a & b``) is a known false positive that gets pointed at
# ``background_run_start`` rather than executed.
_DETACHED_SHELL_RE = re.compile(
    r"(^|[;&|]\s*)nohup\s"          # nohup anywhere as a command start
    r"|(?<![&>|<])&(?![&>|])"       # a standalone & (not &&, 2>&1, >&, <&, |&, &>)
    r"|(^|[;&|]\s*)(setsid|disown)\b",
    re.MULTILINE,
)

_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

_NESTED_SHELL_RE = re.compile(r"\b(?:ba|z|da|k)?sh\b[^|;&]*?\s-[A-Za-z]*c\b")


def _looks_detached(command: str) -> bool:
    """Return True when a Bash command spawns a process detached from the
    CLI's lifetime (``nohup``, a bare trailing ``&``, ``setsid``/``disown``).

    Single/double-quoted substrings are stripped first so message text like
    ``git commit -m "fix & polish"`` does not read as a background launch.
    A nested shell invocation (``bash -c '...'``, ``sh -c "..."``, ``eval``)
    gets its quoted inner text inspected too, so ``bash -c 'nohup job &'``
    is still denied.
    """
    quoted = _QUOTED_RE.findall(command)
    stripped = _QUOTED_RE.sub(" ", command)
    if _DETACHED_SHELL_RE.search(stripped):
        return True
    if _NESTED_SHELL_RE.search(stripped) or re.search(r"\beval\b", stripped):
        for inner in quoted:
            if _looks_detached(inner[1:-1]):
                return True
    return False


def build_foreground_bash_hook():
    """Return a PreToolUse callback that keeps Bash inside the active turn.

    Claude Code's background Bash process is owned by the managed CLI
    subprocess. If the model ends the turn after dispatch, the process is
    stopped and its ``<task-notification>`` is only written when a later turn
    resumes the session. Rewriting the call keeps the provider stream open
    until Bash returns a real result that the model can report.

    The same callback denies detached invocations (``nohup … &``, a bare
    trailing ``&``, ``setsid``/``disown``): those belong to the CLI
    subprocess too and are lost when it reconnects, with no completion ever
    delivered to the chat. The deny reason points at the managed
    ``background_run_start`` MCP tool instead of rewriting the command.

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
        if not isinstance(tool_input, dict):
            return {}
        # Detach check comes first: a rewrite alone would still launch the
        # detached child (e.g. ``nohup job &`` with run_in_background=true).
        command = tool_input.get("command")
        if isinstance(command, str) and _looks_detached(command):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        BACKGROUND_RUN_GUIDANCE
                        + " The command you tried: "
                        + command[:200]
                    ),
                }
            }
        if tool_input.get("run_in_background") is True:
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
        return {}

    return on_pre_tool_use


def build_monitor_deny_hook():
    """Return a PreToolUse callback that denies the CLI's built-in ``Monitor``.

    A Monitor watcher is owned by the CLI subprocess: it dies on reconnect
    and its terminal notification is only emitted when a later turn resumes
    the session, so a monitored long run can finish silently. The deny
    reason steers the model to Ciaobot's managed ``background_run_start``
    MCP tool, which survives restarts and wakes the chat on completion.
    """

    async def on_pre_tool_use(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,  # HookContext; untyped here to avoid an import cycle
    ) -> dict[str, Any]:
        del tool_use_id, context  # unused
        if input_data.get("tool_name") != "Monitor":
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": BACKGROUND_RUN_GUIDANCE,
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
