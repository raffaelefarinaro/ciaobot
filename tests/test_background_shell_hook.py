"""Regression tests for Claude background shell commands.

Claude Code kills a ``Bash(run_in_background=true)`` process when its SDK
turn ends, then reports the stopped task only when the session is resumed.
Ciaobot keeps Bash commands in the foreground so the turn owns the process
until a real tool result exists. Detached launches (``nohup … &``, a bare
trailing ``&``, ``setsid``/``disown``) and the CLI's built-in ``Monitor``
tool are denied outright: they die with the CLI subprocess and never deliver
a completion to the chat. Both denials point at the managed
``background_run_start`` MCP tool.
"""

from __future__ import annotations

import pytest

from ciao.observability.hooks import (
    BACKGROUND_RUN_GUIDANCE,
    build_foreground_bash_hook,
    build_monitor_deny_hook,
)


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


@pytest.mark.asyncio
async def test_nohup_bash_is_denied_with_background_run_guidance() -> None:
    hook = build_foreground_bash_hook()

    out = await hook(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "nohup python scripts/adoption_report.py --force-fetch "
                    "> /tmp/a.log 2>&1 &"
                )
            },
        },
        "tool-use-3",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "background_run_start" in specific["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_trailing_ampersand_is_denied() -> None:
    hook = build_foreground_bash_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": "python x.py &"}},
        "tool-use-4",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "background_run_start" in specific["permissionDecisionReason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "a && b",
        "cmd > log 2>&1",
        "cmd 2>&1 | tee x",
        "cmd |& grep x",
        'echo "a & b"',
        'git commit -m "fix & polish"',
    ],
)
async def test_ordinary_shell_operators_are_not_denied(command: str) -> None:
    hook = build_foreground_bash_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-5",
        None,
    )

    assert out == {}


@pytest.mark.asyncio
async def test_detached_command_with_run_in_background_is_denied() -> None:
    hook = build_foreground_bash_hook()

    out = await hook(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "nohup python x.py > /tmp/x.log 2>&1 &",
                "run_in_background": True,
            },
        },
        "tool-use-8",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "updatedInput" not in specific


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'nohup job &'",
        'sh -c "python x.py &"',
        "zsh -lc 'setsid ./run.sh'",
        'eval "nohup x &"',
    ],
)
async def test_nested_shell_detached_commands_are_denied(command: str) -> None:
    hook = build_foreground_bash_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-9",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "background_run_start" in specific["permissionDecisionReason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        'bash -c "python x.py"',
        'bash -c \'echo "a & b"\'',
        'git commit -m "fix & polish"',
        "sh -c 'a && b'",
    ],
)
async def test_nested_shell_ordinary_commands_are_not_denied(command: str) -> None:
    hook = build_foreground_bash_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-10",
        None,
    )

    assert out == {}


@pytest.mark.asyncio
async def test_monitor_tool_is_denied() -> None:
    hook = build_monitor_deny_hook()

    out = await hook(
        {
            "tool_name": "Monitor",
            "tool_input": {"command": "tail -f /tmp/x.log"},
        },
        "tool-use-6",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert specific["permissionDecisionReason"] == BACKGROUND_RUN_GUIDANCE


@pytest.mark.asyncio
async def test_monitor_hook_ignores_other_tools() -> None:
    hook = build_monitor_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": "tail -f /tmp/x.log"}},
        "tool-use-7",
        None,
    )

    assert out == {}
