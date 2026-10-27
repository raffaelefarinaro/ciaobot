"""Programmatic Claude Agent SDK hooks wired by ClaudeProvider.

One hook is wired today, a ``PreToolUse`` hook on ``Bash`` that denies
detached invocations (``nohup … &``, a bare trailing ``&``,
``setsid``/``disown``). Those escape the CLI's tracking: no wake, no UI row,
and an orphan that survives teardown. Native ``run_in_background`` Bash and
``Monitor`` are allowed unchanged: the CLI tracks them and wakes the chat when
they finish, and Ciaobot keeps the provider alive while they run.

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
    "Do not detach processes with nohup, &, setsid or disown: Ciaobot cannot "
    "track them or wake this chat. Run the command with the Bash tool's "
    "`run_in_background: true` instead; the chat is woken when it finishes. "
    "Use `ciao run start -- <cmd>` only for a command that must survive this "
    "chat's provider process."
)

# Detached-shell shapes we deny. Quoted substrings, heredoc bodies, and
# comments are stripped before matching, but quotes are not parsed, so an
# unquoted odd `&` in text (e.g. ``echo a & b``) remains a known false
# positive. A wrongly denied command is acceptable: it only asks the model
# to rephrase or use ``run_in_background``, it never runs anything.
_DETACHED_SHELL_RE = re.compile(
    r"(^|[;&|]\s*)nohup\s"              # nohup anywhere as a command start
    r"|(?<![&>|<\\])&(?![&>|])"         # a standalone & (not &&, 2>&1, >&, <&, |&, &>, \&)
    r"|(^|[;&|]\s*)(setsid|disown)\b",
    re.MULTILINE,
)

_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# Heredoc body: from the end of the ``<<DELIM`` line through the line that
# equals the delimiter.
_HEREDOC_RE = re.compile(
    r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n[ \t]*\1[ \t]*(?=\n|$)",
    re.MULTILINE | re.DOTALL,
)

_NESTED_SHELL_RE = re.compile(r"\b(?:ba|z|da|k)?sh\b[^|;&]*?\s-[A-Za-z]*c\b")

_COMMENT_RE = re.compile(r"(?m)(^|\s)#[^\n]*")

# Command substitution bodies (innermost, non-nested): $(...) and `...`.
_CMD_SUB_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _looks_detached(command: str) -> bool:
    """Return True when a Bash command spawns a process detached from the
    CLI's lifetime (``nohup``, a bare trailing ``&``, ``setsid``/``disown``).

    The command is normalised before matching: heredoc bodies, single/double
    quoted substrings, and comments are removed, so message text like
    ``git commit -m "fix & polish"``, an escaped ``echo foo\\&bar``, or an
    ``&`` inside a comment or heredoc does not read as a background launch.
    Command-substitution bodies (``$(...)`` and backticks) are inspected
    from the original text before stripping, so
    ``echo "$(nohup x &)"`` is still denied. A nested shell invocation
    (``bash -c '...'``, ``sh -c "..."``, ``eval``) gets its quoted inner
    text inspected too, so ``bash -c 'nohup job &'`` is still denied.
    """
    quoted = _QUOTED_RE.findall(command)
    for match in _CMD_SUB_RE.finditer(command):
        body = match.group(1) if match.group(1) is not None else match.group(2)
        if _looks_detached(body):
            return True
    no_heredocs = _HEREDOC_RE.sub(lambda m: m.group(0).split("\n", 1)[0], command)
    stripped = _QUOTED_RE.sub(" ", no_heredocs)
    stripped = _COMMENT_RE.sub(lambda m: m.group(1), stripped)
    if _DETACHED_SHELL_RE.search(stripped):
        return True
    if _NESTED_SHELL_RE.search(stripped) or re.search(r"\beval\b", stripped):
        for inner in quoted:
            if _looks_detached(inner[1:-1]):
                return True
    return False


def build_detached_shell_deny_hook():
    """Return a PreToolUse callback that denies detached Bash invocations.

    ``nohup … &``, a bare trailing ``&`` and ``setsid``/``disown`` spawn a
    process the CLI does not track: no completion wakes the chat, the UI shows
    no row, and the orphan outlives the CLI subprocess. The deny reason points
    at the Bash tool's native ``run_in_background`` and, for a command that
    must survive the provider process, ``ciao run start -- …``.

    Native ``run_in_background`` Bash, ``Monitor`` and background ``Agent``
    calls are untouched: the CLI tracks them and delivers their completion.
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
        return {}

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
