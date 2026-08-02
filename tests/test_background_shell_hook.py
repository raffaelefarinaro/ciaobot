"""Regression tests for Claude background shell commands.

Claude Code kills a ``Bash(run_in_background=true)`` process when its SDK
turn ends, then reports the stopped task only when the session is resumed.
Ciaobot keeps Bash commands in the foreground so the turn owns the process
until a real tool result exists.
"""

from __future__ import annotations

import pytest

from ciao.observability.hooks import build_foreground_bash_hook


@pytest.mark.asyncio
async def test_background_bash_is_rewritten_to_foreground() -> None:
    hook = build_foreground_bash_hook()
    original = {
        "command": "python -m ciao.skill_evolution",
        "description": "Run skill evolution",
        "run_in_background": True,
        "timeout": 600_000,
    }

    out = await hook(
        {"tool_name": "Bash", "tool_input": original},
        "tool-use-1",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["updatedInput"] == {
        **original,
        "run_in_background": False,
    }
    assert "foreground" in specific["additionalContext"].lower()
    assert original["run_in_background"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Bash", {"command": "pwd"}),
        ("Bash", {"command": "pwd", "run_in_background": False}),
        ("Agent", {"prompt": "inspect", "run_in_background": True}),
    ],
)
async def test_hook_leaves_supported_tool_calls_unchanged(
    tool_name: str, tool_input: dict[str, object]
) -> None:
    hook = build_foreground_bash_hook()

    out = await hook(
        {"tool_name": tool_name, "tool_input": tool_input},
        "tool-use-2",
        None,
    )

    assert out == {}
