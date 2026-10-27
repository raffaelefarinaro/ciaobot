"""Regression tests for Claude background shell commands.

Native ``Bash(run_in_background=true)`` and ``Monitor`` calls are tracked and
woken by the CLI, and Ciaobot keeps the provider alive while they run, so the
hook leaves them alone. Detached launches (``nohup … &``, a bare trailing
``&``, ``setsid``/``disown``) escape the CLI's tracking and are denied; the
denial points at ``run_in_background`` and, for a command that must outlive
the provider, ``ciao run start -- …``.
"""

from __future__ import annotations

import pytest

from ciao.observability.hooks import (
    BACKGROUND_RUN_GUIDANCE,
    build_detached_shell_deny_hook,
)


@pytest.mark.asyncio
async def test_native_background_bash_is_allowed() -> None:
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "python -m ciao.cli vault-index --write",
                "run_in_background": True,
            },
        },
        "tool-use-1",
        None,
    )

    assert out == {}


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
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": tool_name, "tool_input": tool_input},
        "tool-use-2",
        None,
    )

    assert out == {}


@pytest.mark.asyncio
async def test_nohup_bash_is_denied_with_background_run_guidance() -> None:
    hook = build_detached_shell_deny_hook()

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
    assert "run_in_background" in specific["permissionDecisionReason"]
    assert "ciao run start" in specific["permissionDecisionReason"]
    assert BACKGROUND_RUN_GUIDANCE in specific["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_trailing_ampersand_is_denied() -> None:
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": "python x.py &"}},
        "tool-use-4",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "ciao run start" in specific["permissionDecisionReason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        'echo "$(nohup sleep 60 >/tmp/x.log 2>&1 &)"',
        "x=$(nohup job &)",
        "echo `setsid ./run.sh`",
    ],
)
async def test_command_substitution_detached_launches_are_denied(command: str) -> None:
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-14",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "ciao run start" in specific["permissionDecisionReason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        'echo "$(date)"',
        "v=$(git rev-parse HEAD)",
        'echo "$(echo a && echo b)"',
    ],
)
async def test_benign_command_substitutions_are_not_denied(command: str) -> None:
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-15",
        None,
    )

    assert out == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "echo foo\\&bar",
        "cat <<EOF\nResearch & Development\nEOF",
        "ls # a & b",
        "cat <<'EOF'\nx & y\nEOF",
    ],
)
async def test_literal_ampersands_in_text_are_not_denied(command: str) -> None:
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-12",
        None,
    )

    assert out == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "long_job & echo launched",
        "python x.py & sleep 1",
        "cmd &",
    ],
)
async def test_midline_ampersand_jobs_are_denied(command: str) -> None:
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-11",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "ciao run start" in specific["permissionDecisionReason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "a && b",
        "cmd > log 2>&1",
        "cmd 2>&1 | tee x",
        "cmd |& grep x",
        "cmd &> log",
        'echo "a & b"',
        'git commit -m "fix & polish"',
    ],
)
async def test_ordinary_shell_operators_are_not_denied(command: str) -> None:
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-5",
        None,
    )

    assert out == {}


@pytest.mark.asyncio
async def test_detached_command_with_run_in_background_is_denied() -> None:
    hook = build_detached_shell_deny_hook()

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
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-9",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "ciao run start" in specific["permissionDecisionReason"]


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
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-10",
        None,
    )

    assert out == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "nohup x &",
        "job & echo launched",
        "cat <<EOF > f\nhi\nEOF\n./job &",
    ],
)
async def test_detached_jobs_are_denied(command: str) -> None:
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "tool-use-13",
        None,
    )

    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "ciao run start" in specific["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_monitor_tool_is_allowed() -> None:
    hook = build_detached_shell_deny_hook()

    out = await hook(
        {"tool_name": "Monitor", "tool_input": {"command": "tail -f /tmp/x.log"}},
        "tool-use-6",
        None,
    )

    assert out == {}
