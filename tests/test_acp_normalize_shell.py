"""Shell calls (ACP `execute` kind) through the normalizer: command, background, exit, output tail.

The fixtures under ``tests/fixtures/acp/shell_*.jsonl`` are redacted from real captures
(OpenCode 2.3.2 over ``opencode acp``; claude-agent-acp 0.108.1 against Claude Code 2.1.326).
Ids, paths and pids are replaced; the frame sequence and the wire shapes are as captured.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.models import ToolUseEvent
from ciao.providers.acp.normalize import UpdateNormalizer, shell_output_tail

FIXTURES = Path(__file__).parent / "fixtures" / "acp"


def _normalizer() -> UpdateNormalizer:
    return UpdateNormalizer(SimpleNamespace(model_config_id="model"), "s1")


def _replay(name: str) -> list[ToolUseEvent]:
    n = _normalizer()
    events: list[ToolUseEvent] = []
    for line in (FIXTURES / name).read_text().splitlines():
        events.extend(n.feed(json.loads(line)["params"]["update"]))
    return events


def _uses(events: list[ToolUseEvent], call_id: str) -> list[ToolUseEvent]:
    return [e for e in events if e.type == "tool_use" and e.tool_use_id == call_id]


def _result(events: list[ToolUseEvent], call_id: str) -> ToolUseEvent:
    (found,) = [e for e in events if e.type == "tool_result" and e.tool_use_id == call_id]
    return found


def _opencode_call(call_id: str, command: str, *, background: bool = False, exit_code: int = 0,
                   output: str = "", metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Synthetic OpenCode frames for a shell call, for the cases no capture covers."""
    raw: dict[str, Any] = {"command": command, "cwd": "/tmp/work"}
    if background:
        raw["background"] = True
    meta = {"status": "completed", "truncated": False, "exit": exit_code} if metadata is None else metadata
    return [
        {"sessionUpdate": "tool_call", "toolCallId": call_id, "name": "shell", "title": "shell",
         "kind": "execute", "status": "pending", "rawInput": {"cwd": "/tmp/work"}},
        {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "in_progress",
         "kind": "execute", "title": command, "rawInput": raw},
        {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed",
         "content": [{"type": "content", "content": {"type": "text", "text": output}}],
         "rawOutput": {"metadata": meta}},
    ]


def _feed_all(frames: list[dict[str, Any]]) -> list[ToolUseEvent]:
    n = _normalizer()
    out: list[ToolUseEvent] = []
    for frame in frames:
        out.extend(n.feed(frame))
    return out


# -- OpenCode ---------------------------------------------------------------------------


def test_opencode_foreground_settles_on_the_result_with_exit_and_tail() -> None:
    events = _replay("shell_opencode_foreground.jsonl")

    # The first frame names no command; the in_progress update names it once, and its repeat is silent.
    first, command = _uses(events, "call_shell_fg")
    assert (first.tool_input, command.tool_input) == ("", "echo hi")
    assert command.shell is True and command.shell_background is False

    done = _result(events, "call_shell_fg")
    assert (done.exit_code, done.output_tail, done.background, done.is_error) == (0, "hi", False, False)
    assert done.shell_id == "" and done.output_path == ""


def test_opencode_background_ack_is_not_a_finished_result() -> None:
    events = _replay("shell_opencode_background.jsonl")

    (use,) = [e for e in _uses(events, "call_shell_bg") if e.tool_input]
    assert use.shell_background is True and use.tool_input == "ping -c 8 127.0.0.1"

    ack = _result(events, "call_shell_bg")
    assert ack.background is True
    assert ack.is_error is False
    assert ack.exit_code is None and ack.output_tail == ""
    assert ack.shell_id == "sh_redacted0001"
    assert ack.shell_pid == 4242
    # The pid is structured metadata; the output path is only in the acknowledgement text, which is never read.
    assert ack.output_path == ""


# -- Claude (default: no terminal meta) -------------------------------------------------


def test_claude_default_foreground_has_output_and_no_exit_code() -> None:
    events = _replay("shell_claude_default.jsonl")

    (use,) = [e for e in _uses(events, "toolu_shell_cl_fg") if e.tool_input]
    assert (use.tool_name, use.tool_input, use.shell) == ("Bash", "echo hi", True)

    done = _result(events, "toolu_shell_cl_fg")
    # Without terminal meta Claude sends no exit code; the call still settles, and its output is the stdout.
    assert done.exit_code is None
    assert (done.background, done.output_tail) == (False, "hi")


def test_claude_default_background_ack_reads_task_id_and_output_path() -> None:
    events = _replay("shell_claude_default.jsonl")

    use = [e for e in _uses(events, "toolu_shell_cl_bg") if e.tool_input][-1]
    assert use.shell_background is True

    ack = _result(events, "toolu_shell_cl_bg")
    assert (ack.background, ack.is_error, ack.exit_code, ack.output_tail) == (True, False, None, "")
    assert ack.shell_id == "bgtask02"
    assert ack.output_path == ""  # named only in the text: never taken from it
    assert ack.shell_pid is None


# -- Claude with terminal_output_delta / terminal_exit meta -----------------------------


def test_claude_terminal_foreground_takes_exit_code_from_terminal_exit() -> None:
    events = _replay("shell_claude_terminal.jsonl")

    done = _result(events, "toolu_shell_cl_fg")
    assert (done.exit_code, done.output_tail, done.background) == (0, "hi", False)


def test_claude_terminal_background_ack_gives_the_path_and_terminal_exit_settles_it() -> None:
    events = _replay("shell_claude_terminal.jsonl")

    results = [e for e in events if e.tool_use_id == "toolu_shell_cl_term_bg" and e.type == "tool_result"]
    assert len(results) == 2
    ack = results[0]
    assert (ack.background, ack.exit_code, ack.output_tail) == (True, None, "")
    assert ack.shell_id == "bgtask01"
    assert ack.output_path == ""  # named only in the text: never taken from it

    # The later `terminal_exit` with a null code ends the process: a settle with the exit unknown.
    settle = results[1]
    assert (settle.background, settle.is_error, settle.exit_code) == (False, False, None)


def test_terminal_exit_settles_an_acknowledged_background_shell_with_its_code() -> None:
    frames = _opencode_call("c-bg", "sleep 9", background=True)[:2]
    frames.append({"sessionUpdate": "tool_call_update", "toolCallId": "c-bg", "status": "completed",
                   "rawOutput": {"metadata": {"status": "running", "pid": 77}},
                   "content": [{"type": "content", "content": {"type": "text", "text": ""}}]})
    frames.append({"sessionUpdate": "tool_call_update", "toolCallId": "c-bg",
                   "_meta": {"terminal_exit": {"terminal_id": "c-bg", "exit_code": 3, "signal": None}}})
    results = [e for e in _feed_all(frames) if e.type == "tool_result" and e.tool_use_id == "c-bg"]
    assert [(r.background, r.exit_code) for r in results] == [(True, None), (False, 3)]


def test_terminal_exit_on_an_unacknowledged_call_is_not_a_second_result() -> None:
    frames = [
        {"sessionUpdate": "tool_call", "toolCallId": "c-fg", "name": "shell", "kind": "execute",
         "status": "pending", "rawInput": {"command": "echo hi"}},
        {"sessionUpdate": "tool_call_update", "toolCallId": "c-fg", "status": "completed",
         "rawOutput": {"metadata": {"status": "completed", "exit": 0}},
         "content": [{"type": "content", "content": {"type": "text", "text": "hi"}}]},
        {"sessionUpdate": "tool_call_update", "toolCallId": "c-fg",
         "_meta": {"terminal_exit": {"terminal_id": "c-fg", "exit_code": 9, "signal": None}}},
    ]
    results = [e for e in _feed_all(frames) if e.type == "tool_result" and e.tool_use_id == "c-fg"]
    assert len(results) == 1 and results[0].exit_code == 0


def test_opencode_background_ack_without_terminal_exit_stays_running() -> None:
    events = _feed_all(_opencode_call("c-oc", "ping -c 8 127.0.0.1", background=True,
                                      metadata={"status": "running", "shellID": "sh_x", "pid": 5}))
    results = [e for e in events if e.type == "tool_result" and e.tool_use_id == "c-oc"]
    assert [r.background for r in results] == [True]


# -- Guards and edge cases (synthetic frames) -------------------------------------------


def test_non_zero_exit_is_carried_not_dropped() -> None:
    events = _feed_all(_opencode_call("c-fail", "false", exit_code=2, output="boom\n"))
    done = _result(events, "c-fail")
    assert (done.exit_code, done.output_tail, done.is_error) == (2, "boom", False)


def test_failed_status_sets_is_error() -> None:
    frames = _opencode_call("c-err", "make", exit_code=1, output="x")
    frames[-1]["status"] = "failed"
    assert _result(_feed_all(frames), "c-err").is_error is True


def test_huge_output_is_bounded_before_it_leaves_the_normalizer() -> None:
    noisy = "\x1b[31mred\x1b[0m\x07\n" + "".join(f"line {i} " + "x" * 200 + "\n" for i in range(20000))
    events = _feed_all(_opencode_call("c-big", "yes", output=noisy))
    tail = _result(events, "c-big").output_tail
    assert len(tail.splitlines()) <= 40
    assert len(tail) <= 4096
    assert "\x1b" not in tail and "\x07" not in tail
    assert tail.endswith("x" * 200)


def test_shell_output_tail_keeps_tabs_and_normalises_carriage_returns() -> None:
    assert shell_output_tail("a\tb\r\nc\rd\n\n") == "a\tb\nc\nd"
    assert shell_output_tail("") == ""


def test_command_is_one_line_of_plain_text() -> None:
    events = _feed_all(_opencode_call("c-cmd", "ls\x1b[2K\x07 -la\nrm x"))
    (use,) = [e for e in _uses(events, "c-cmd") if e.tool_input]
    assert "\x1b" not in use.tool_input and "\x07" not in use.tool_input and "\n" not in use.tool_input
    assert use.tool_input.startswith("ls")


def test_launch_ack_path_outside_the_shell_output_dir_is_not_recorded() -> None:
    frames = _opencode_call(
        "c-path", "sleep 9", background=True,
        output="Command moved to the background (shell ID: sh_abc).\nPID: 7\nOutput is streaming to: /etc/passwd.",
        metadata={"status": "running", "shellID": "sh_abc", "pid": 7},
    )
    ack = _result(_feed_all(frames), "c-path")
    assert ack.background is True and ack.output_path == "" and ack.shell_id == "sh_abc"


def test_invalid_shell_id_in_metadata_is_rejected() -> None:
    frames = _opencode_call(
        "c-id", "sleep 9", background=True, output="Command moved to the background (shell ID: sh_ok1).",
        metadata={"status": "running", "shellID": "../../etc", "pid": True},
    )
    ack = _result(_feed_all(frames), "c-id")
    assert ack.shell_id == "sh_ok1"
    assert ack.shell_pid is None


def test_background_ack_without_background_input_is_an_ordinary_result() -> None:
    frames = _opencode_call("c-plain", "echo", output="done", metadata={"status": "running", "shellID": "sh_x"})
    ack = _result(_feed_all(frames), "c-plain")
    assert ack.background is False


@pytest.mark.parametrize("kind", ["think", "other"])
def test_non_execute_calls_carry_no_shell_facts(kind: str) -> None:
    n = _normalizer()
    events = n.feed({"sessionUpdate": "tool_call", "toolCallId": "c-read", "name": "read", "title": "read",
                     "kind": kind, "status": "pending", "rawInput": {"command": "cat x", "background": True}})
    (use,) = events
    assert isinstance(use, ToolUseEvent)
    assert (use.shell, use.shell_background) == (False, False)
    done = n.feed({"sessionUpdate": "tool_call_update", "toolCallId": "c-read", "status": "completed",
                   "content": [{"type": "content", "content": {"type": "text", "text": "secret output"}}]})
    (result,) = done
    assert (result.exit_code, result.output_tail, result.shell_id) == (None, "", "")


def test_shell_output_tail_keeps_only_the_last_lines() -> None:
    # Short lines, so the line limit (not the character limit) is what cuts the tail.
    tail = shell_output_tail("\n".join(f"line {i}" for i in range(200)))
    assert tail.splitlines() == [f"line {i}" for i in range(160, 200)]


def test_shell_output_tail_removes_ansi_sequences_and_control_characters() -> None:
    assert shell_output_tail("ok \x1b[31mred\x1b[0m \x1b]0;title\x07done\x07") == "ok red done"


def test_a_structured_output_path_under_the_shell_dir_is_recorded() -> None:
    path = "/Users/example/.local/share/opencode/shell/" + "0123456789abcdef" * 2 + "01234567/sh_abc.out"
    frames = _opencode_call(
        "c-out", "sleep 9", background=True, output="Command moved to the background (shell ID: sh_abc).",
        metadata={"status": "running", "shellID": "sh_abc", "pid": 7, "outputPath": path},
    )
    assert _result(_feed_all(frames), "c-out").output_path == path


def test_a_structured_output_path_with_a_dot_dot_segment_is_refused() -> None:
    path = "/Users/example/.local/share/opencode/shell/abc/../../../../etc/sh_abc.out"
    frames = _opencode_call(
        "c-dd", "sleep 9", background=True, output="Command moved to the background (shell ID: sh_abc).",
        metadata={"status": "running", "shellID": "sh_abc", "pid": 7, "outputPath": path},
    )
    assert _result(_feed_all(frames), "c-dd").output_path == ""
