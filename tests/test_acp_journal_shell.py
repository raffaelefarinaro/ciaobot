"""History rendering of shell calls from an ACP journal: one `_shell` card per call.

The journal records are what the session writes: each normalized event through
``chat_broker.event_to_json``. Fixture-backed cases replay the redacted captures end to end.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ciao.providers.acp.normalize import UpdateNormalizer
from ciao.web import transcript_service
from ciao.web.chat_broker import event_to_json

FIXTURES = Path(__file__).parent / "fixtures" / "acp"
CHAT = SimpleNamespace(turn_index_base=0, user_turn_images={}, user_turn_timings={}, user_turn_unattended={},
                       user_turn_origin={})


def _normalizer() -> UpdateNormalizer:
    return UpdateNormalizer(SimpleNamespace(model_config_id="model"), "s1")


def _journal(fixture: str) -> list[dict[str, Any]]:
    """The records a session journals for one captured run: the prompt, then each event's payload."""
    n = _normalizer()
    records: list[dict[str, Any]] = [{"type": "user", "text": "go"}]
    for line in (FIXTURES / fixture).read_text().splitlines():
        for event in n.feed(json.loads(line)["params"]["update"]):
            payload = event_to_json(event)
            if payload is not None:
                records.append(payload)
    return records


def _render(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return transcript_service._render_acp_journal(records, CHAT, start_user_idx=0, workspace_root=None,
                                                  agent_root=None)


def _shells(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row["shell"] for row in rows if row.get("tool_name") == "_shell"]


def _use(call_id: str, command: str, *, background: bool = False, tool: str = "shell") -> dict[str, Any]:
    record: dict[str, Any] = {"type": "tool_use", "tool_name": tool, "tool_input": command, "tool_use_id": call_id,
                              "shell": True}
    if background:
        record["shell_background"] = True
    return record


def _result(call_id: str, **fields: Any) -> dict[str, Any]:
    return {"type": "tool_result", "tool_name": "", "tool_use_id": call_id, **fields}


def test_foreground_shell_is_one_done_card_with_exit_and_tail() -> None:
    rows = _render(_journal("shell_opencode_foreground.jsonl"))

    (card,) = _shells(rows)
    assert card == {
        "tool_use_id": "call_shell_fg", "command": "echo hi", "background": False, "state": "done",
        "exit_code": 0, "output_tail": "hi",
    }
    (row,) = [r for r in rows if r.get("tool_name") == "_shell"]
    assert (row["role"], row["content"]) == ("system", "echo hi")
    # No activity line is left behind for the call.
    assert not [r for r in rows if r.get("tool_name") == "_activity"]


def test_background_launch_stays_running_with_the_badge_flag() -> None:
    (card,) = _shells(_render(_journal("shell_opencode_background.jsonl")))
    assert (card["state"], card["background"], card["exit_code"], card["output_tail"]) == (
        "running", True, None, "",
    )


def test_claude_default_foreground_settles_without_an_exit_code() -> None:
    cards = {c["tool_use_id"]: c for c in _shells(_render(_journal("shell_claude_default.jsonl")))}
    assert (cards["toolu_shell_cl_fg"]["state"], cards["toolu_shell_cl_fg"]["exit_code"]) == ("done", None)
    assert cards["toolu_shell_cl_fg"]["output_tail"] == "hi"
    assert (cards["toolu_shell_cl_bg"]["state"], cards["toolu_shell_cl_bg"]["background"]) == ("running", True)


def test_claude_terminal_foreground_exit_zero_and_background_null_exit() -> None:
    cards = {c["tool_use_id"]: c for c in _shells(_render(_journal("shell_claude_terminal.jsonl")))}
    assert (cards["toolu_shell_cl_fg"]["state"], cards["toolu_shell_cl_fg"]["exit_code"]) == ("done", 0)
    # The background launch stays running until its terminal_exit arrives; a null exit code
    # settles it as done with the exit unknown.
    assert (cards["toolu_shell_cl_term_bg"]["state"], cards["toolu_shell_cl_term_bg"]["exit_code"]) == (
        "done", None,
    )


def test_non_zero_exit_and_is_error_both_read_as_failed() -> None:
    rows = _render([
        {"type": "user", "text": "go"},
        _use("a", "false"),
        _result("a", exit_code=2, output_tail="boom"),
        _use("b", "make"),
        _result("b", is_error=True),
    ])
    a, b = _shells(rows)
    assert (a["state"], a["exit_code"], a["output_tail"]) == ("failed", 2, "boom")
    assert (b["state"], b["exit_code"]) == ("failed", None)


def test_a_later_result_settles_a_background_card() -> None:
    rows = _render([
        {"type": "user", "text": "go"},
        _use("bg", "sleep 9", background=True),
        _result("bg", background=True, shell_id="sh_1"),
        _result("bg", exit_code=0, output_tail="done"),
    ])
    (card,) = _shells(rows)
    assert (card["state"], card["background"], card["exit_code"], card["output_tail"]) == ("done", True, 0, "done")


def test_a_re_announced_call_is_one_row_with_the_latest_command() -> None:
    rows = _render([
        {"type": "user", "text": "go"},
        _use("c1", ""),
        _use("c1", "echo one"),
        _use("c1", "echo one", background=True),
        _result("c1", exit_code=0),
    ])
    (card,) = _shells(rows)
    assert (card["command"], card["background"], card["state"]) == ("echo one", True, "done")
    assert len([r for r in rows if r.get("tool_name") == "_shell"]) == 1


def test_an_old_one_line_bash_row_renders_as_before() -> None:
    # Journals written before shell cards: a plain tool_use with no shell flags, and a plain result.
    rows = _render([
        {"type": "user", "text": "go"},
        {"type": "tool_use", "tool_name": "Bash", "tool_input": "ls -la", "tool_use_id": "old1"},
        {"type": "tool_result", "tool_name": "Bash", "tool_use_id": "old1"},
    ])
    assert _shells(rows) == []
    (activity,) = [r for r in rows if r.get("tool_name") == "_activity"]
    assert activity["content"].endswith("Bash ls -la")


def test_a_shell_inside_a_subagent_stays_an_activity_line() -> None:
    rows = _render([
        {"type": "user", "text": "go"},
        {"type": "tool_use", "tool_name": "subagent", "tool_input": "[general] audit", "tool_use_id": "sub1"},
        {**_use("child1", "ls", tool="Bash"), "parent_tool_use_id": "sub1"},
        _result("child1", parent_tool_use_id="sub1"),
    ])
    assert _shells(rows) == []
    (card,) = [r["subagent"] for r in rows if r.get("tool_name") == "_subagent"]
    assert [step["kind"] for step in card["steps"]] == ["activity"]


def test_the_row_carries_only_the_documented_keys() -> None:
    (row,) = [r for r in _render(_journal("shell_opencode_foreground.jsonl")) if r.get("tool_name") == "_shell"]
    assert set(row["shell"]) == {"tool_use_id", "command", "background", "state", "exit_code", "output_tail"}


def test_ws_payload_adds_shell_keys_only_when_set() -> None:
    from ciao.models import ToolUseEvent

    plain = event_to_json(ToolUseEvent(type="tool_result", tool_use_id="x"))
    assert plain is not None
    assert not {"exit_code", "output_tail", "shell_id", "shell_pid", "output_path"} & set(plain)

    shell = event_to_json(ToolUseEvent(
        type="tool_result", tool_use_id="x", exit_code=0, output_tail="hi", shell_id="sh_1", shell_pid=3,
        output_path="/Users/example/.local/share/opencode/shell/aa/sh_1.out",
    ))
    assert shell is not None
    assert (shell["exit_code"], shell["output_tail"], shell["shell_id"], shell["shell_pid"]) == (0, "hi", "sh_1", 3)

    use = event_to_json(ToolUseEvent(type="tool_use", tool_name="shell", tool_use_id="x", shell=True,
                                     shell_background=True, tool_input="sleep 9"))
    assert use is not None and (use["shell"], use["shell_background"]) == (True, True)
    read = event_to_json(ToolUseEvent(type="tool_use", tool_name="Read", tool_use_id="y"))
    assert read is not None and "shell" not in read
