"""Provider-neutral prompt and transcript helpers, and OpenCode's permission rules.

The Claude SDK driver's tests went with the driver (#3138 C2b). Its disable options are
covered by ``tests/test_acp_agents.py`` (``_meta.claudeCode.options``), and the prompt,
history and card helpers below are shared by every provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.models import AgentRequest, ImageAttachment
from ciao.providers.base import build_prompt


def test_build_prompt_summarizes_images(tmp_path: Path) -> None:
    request = AgentRequest(
        prompt="Inspect this.",
        model="sonnet",
        mode="normal",
        images=[
            ImageAttachment(
                path=tmp_path / "image.png",
                mime_type="image/png",
                original_filename="image.png",
                caption="look here",
            )
        ],
    )
    prompt = build_prompt(request)
    assert "[INCOMING IMAGES]" in prompt
    assert "image.png" in prompt
    assert "look here" in prompt


def test_summarize_task_notification_falls_back_to_a_status_line() -> None:
    """A notification no card recognises collapses to a one-line system
    bubble, so it stays visible without leaking the raw XML into history.
    """
    from ciao.web.transcript_service import _summarize_task_notification

    failed = (
        "<task-notification>\n"
        "<task-id>x</task-id>\n"
        "<status>failed</status>\n"
        "<summary>Hit timeout after 600s\nsecond line</summary>\n"
        "</task-notification>"
    )
    fail_summary = _summarize_task_notification(failed)
    assert fail_summary == "\U0001F916 Subagent failed: Hit timeout after 600s"

    # Plain user text must be left untouched.
    assert _summarize_task_notification("hello world") is None


def test_subagent_completion_renders_as_background_run_card() -> None:
    """A Task subagent completion draws as the background-run card OpenCode
    subagent wakes already get, with the report's last lines as its tail.
    """
    from ciao.background_notice import format_finished_runs
    from ciao.web.transcript_service import _native_task_notice

    envelope = (
        "<task-notification>\n"
        "<task-id>a7b9ecacf3a281869</task-id>\n"
        "<tool-use-id>toolu_016N18MKh4oq2TMMmGmQJ5RD</tool-use-id>\n"
        "<output-file>/tmp/claude-501/sess/tasks/a7b9ecacf3a281869.output</output-file>\n"
        "<status>completed</status>\n"
        '<summary>Agent "Implement issue 2766" finished</summary>\n'
        "<note>The same task-id may notify more than once.</note>\n"
        "<result>Opened PR #2801.\n\nCI is green.</result>\n"
        "</task-notification>"
    )
    expected, _failed = format_finished_runs(
        [{
            "status": "ok",
            "label": "Implement issue 2766",
            "run_id": "a7b9ecacf3a281869",
            "exit_code": None,
            "error": "",
            "log_path": None,
            "last_lines": ["Opened PR #2801.", "CI is green."],
        }],
        tail_lines=50,
    )
    assert _native_task_notice(envelope) == expected
    assert expected.startswith("[Ciaobot] 1 background run finished.")
    assert "/tmp/claude-501" not in expected


def test_is_cli_internal_envelope_matches_known_wrappers() -> None:
    """Bash output, command echoes, and other CLI-synthesized user-role
    messages should be recognized so chat_messages can drop them on replay.
    """
    from ciao.web.transcript_service import _is_cli_internal_envelope

    cli_payloads = [
        "<task-notification><status>completed</status></task-notification>",
        "<bash-stdout>hello\n</bash-stdout>",
        "<bash-stderr>err</bash-stderr>",
        "<command-name>/model</command-name><command-args>opus</command-args>",
        "<local-command-stdout>x</local-command-stdout>",
        "  \n<teammate-message>hi</teammate-message>",  # leading whitespace ok
    ]
    for payload in cli_payloads:
        assert _is_cli_internal_envelope(payload), payload

    # Real user prose with an angle bracket must not be misclassified.
    assert not _is_cli_internal_envelope("Is X < Y true?")
    assert not _is_cli_internal_envelope("hello")
    assert not _is_cli_internal_envelope("<p>html-like text but not a CLI tag</p>")


def test_strip_injected_context_removes_image_manifest(tmp_path: Path) -> None:
    """The UI replay path must undo the manifest `build_prompt` appends."""
    from ciao.web.transcript_service import _strip_injected_context

    request = AgentRequest(
        prompt="First line.\nSecond line.",
        model="sonnet",
        mode="normal",
        images=[
            ImageAttachment(
                path=tmp_path / "a.png",
                mime_type="image/png",
                original_filename="a.png",
                caption="look here",
            ),
            ImageAttachment(
                path=tmp_path / "b.png",
                mime_type="image/png",
                original_filename="b.png",
                caption=None,
            ),
        ],
    )
    prompt = build_prompt(request)
    assert "[INCOMING IMAGES]" in prompt

    # Plain trailer (no CIAO context prefix)
    assert _strip_injected_context(prompt) == "First line.\nSecond line."

    # With injected CIAO context block
    with_prefix = (
        "[CIAO_CONTEXT_BEGIN]\n"
        '[Project: "Ciaobot Improvements"]\n'
        "[CIAO_CONTEXT_END]\n\n"
        + prompt
    )
    assert _strip_injected_context(with_prefix) == "First line.\nSecond line."


@pytest.mark.parametrize("mode", ["normal", "plan"])
def test_opencode_manual_and_plan_modes_add_no_allow_rules(mode: str) -> None:
    """The same carve-out on the other provider."""
    from ciao.providers.opencode import mode_settings

    _agent, rules = mode_settings(mode)
    resources = " ".join(str(rule.get("resource") or "") for rule in rules)
    assert "ciao schedule create" not in resources
    assert "ciao memory update" not in resources


def test_opencode_auto_mode_keeps_every_ciao_command_gated() -> None:
    """Auto mode pre-approves no `ciao …` argv prefix (a shell-suffix bypass
    risk); every shell command, including `ciao …`, keeps a card."""
    from ciao.providers.opencode import mode_settings

    _agent, rules = mode_settings("auto")
    shell = [r for r in rules if r.get("action") == "shell"]
    assert not any(str(r.get("resource") or "").startswith("ciao ") for r in shell)
    assert shell[0] == {"action": "shell", "resource": "*", "effect": "ask"}
