"""History rendering of subagent calls from an ACP journal (one card per call, nested children)."""

from __future__ import annotations

from types import SimpleNamespace

from ciao.web import transcript_service

CHAT = SimpleNamespace(turn_index_base=0, user_turn_images={}, user_turn_timings={}, user_turn_unattended={},
                       user_turn_origin={})


def _render(records: list[dict]) -> list[dict]:
    return transcript_service._render_acp_journal(
        [{"type": "user", "text": "go"}, *records], CHAT, start_user_idx=0, workspace_root=None, agent_root=None,
    )


def _cards(rows: list[dict]) -> list[dict]:
    return [row["subagent"] for row in rows if row.get("tool_name") == "_subagent"]


def _call(call_id: str, summary: str, tool: str = "Agent", parent: str | None = None) -> dict:
    record = {"type": "tool_use", "tool_name": tool, "tool_input": summary, "tool_use_id": call_id}
    if parent:
        record["parent_tool_use_id"] = parent
    return record


def _result(call_id: str, **flags: object) -> dict:
    return {"type": "tool_result", "tool_name": "", "tool_use_id": call_id, **flags}


def test_old_journal_shape_renders_one_card_with_no_description() -> None:
    # Live rows before this change: the title was the input, and the call was journalled twice.
    rows = _render([
        {"type": "tool_use", "tool_name": "subagent", "tool_input": "subagent", "tool_use_id": "c1"},
        {"type": "tool_use", "tool_name": "subagent", "tool_input": "subagent", "tool_use_id": "c1"},
        _result("c1"),
    ])

    assert [row["content"] for row in rows if row.get("tool_name") == "_subagent"] == ["Subagent"]
    (card,) = _cards(rows)
    assert (card["description"], card["state"], card["step_count"], card["steps"]) == ("", "done", 0, [])


def test_a_re_announced_call_is_one_activity_line() -> None:
    rows = _render([
        _call("b1", "", tool="bash"),
        {"type": "tool_use", "tool_name": "bash", "tool_input": "ls -la", "tool_use_id": "b1"},
        {"type": "tool_use", "tool_name": "read", "tool_input": "/x", "tool_use_id": "r1"},
    ])

    (activity,) = [row for row in rows if row.get("tool_name") == "_activity"]
    assert activity["content"].splitlines() == ["⚙️ bash ls -la", "⚙️ read /x"]


def test_launch_ack_is_running_and_the_terminal_status_is_done() -> None:
    rows = _render([
        _call("c1", "[general] Sleep 5 then reply done"),
        _result("c1", background=True),
    ])
    assert _cards(rows)[0]["state"] == "running"

    rows = _render([
        _call("c1", "[general] Sleep 5 then reply done"),
        _result("c1", background=True),
        _result("c1", subagent_status="completed"),
    ])
    (card,) = _cards(rows)
    assert (card["agent_type"], card["description"], card["state"]) == ("general", "Sleep 5 then reply done", "done")
    assert rows[1]["content"] == "Subagent: Sleep 5 then reply done"


def test_terminal_failure_and_error_results_show_failed() -> None:
    failed = _render([_call("c1", "[general] x"), _result("c1", background=True), _result("c1", subagent_status="failed")])
    interrupted = _render([_call("c1", "[general] x"), _result("c1", subagent_status="interrupted")])
    errored = _render([_call("c1", "[general] x"), _result("c1", is_error=True)])
    assert [_cards(rows)[0]["state"] for rows in (failed, interrupted, errored)] == ["failed", "failed", "failed"]


def test_child_activity_and_files_nest_under_the_card_and_child_text_stays_out() -> None:
    rows = _render([
        {"type": "text_delta", "text": "Launching."},
        _call("c1", "[general] Scan"),
        {"type": "text_delta", "text": "child words", "parent_tool_use_id": "c1"},
        {"type": "thinking", "text": "child thought", "parent_tool_use_id": "c1"},
        _call("k1", "ls", tool="bash", parent="c1"),
        {"type": "tool_use", "tool_name": "Write", "tool_input": "/w/a.md", "tool_use_id": "k2",
         "parent_tool_use_id": "c1", "file_touches": [{"file_path": "/w/a.md", "action": "created"}]},
        _result("c1", background=True),
        {"type": "text_delta", "text": "Waiting."},
    ])

    assert [row["content"] for row in rows if row["role"] == "assistant"] == ["Launching.", "Waiting."]
    assert not any("child" in str(row.get("content")) for row in rows if row.get("tool_name") != "_subagent")
    (card,) = _cards(rows)
    assert card["step_count"] == 2
    assert card["steps"][0] == {"kind": "activity", "line": "⚙️ bash ls", "tool_use_id": "k1"}
    assert card["steps"][1]["kind"] == "file"
    assert card["steps"][1]["file_path"] == "/w/a.md"
    assert not any(row.get("tool_name") in {"_activity", "_filecard"} and "bash" in str(row.get("content"))
                   for row in rows)


def test_a_re_announced_child_replaces_its_own_step() -> None:
    rows = _render([
        _call("c1", "[general] Scan"),
        _call("k1", "", tool="bash", parent="c1"),
        _call("k1", "ls -la", tool="bash", parent="c1"),
    ])

    (card,) = _cards(rows)
    assert card["step_count"] == 1
    assert card["steps"] == [{"kind": "activity", "line": "⚙️ bash ls -la", "tool_use_id": "k1"}]


def test_child_steps_are_capped_per_card_and_counted_in_full() -> None:
    limit = transcript_service.SUBAGENT_STEP_LIMIT
    children = [_call(f"k{n}", f"cmd {n}", tool="bash", parent="c1") for n in range(limit + 7)]
    rows = _render([_call("c1", "[general] Scan"), *children])

    (card,) = _cards(rows)
    assert card["step_count"] == limit + 7
    assert len(card["steps"]) == limit
    assert card["steps"][-1]["tool_use_id"] == f"k{limit - 1}"


def test_an_orphan_child_keeps_the_subagent_marker_on_its_line() -> None:
    rows = _render([_call("k1", "ls", tool="bash", parent="missing")])

    (activity,) = [row for row in rows if row.get("tool_name") == "_activity"]
    assert activity["content"] == "↳ ⚙️ bash ls"


def test_a_child_with_several_files_keeps_one_step_per_file_and_none_on_a_re_announce() -> None:
    touches = [{"file_path": "/w/a.md", "action": "created"}, {"file_path": "/w/b.md", "action": "created"}]
    write = {"type": "tool_use", "tool_name": "Write", "tool_input": "", "tool_use_id": "k2",
             "parent_tool_use_id": "c1", "file_touches": touches}
    rows = _render([_call("c1", "[general] Write two"), write, dict(write)])

    (card,) = _cards(rows)
    assert card["step_count"] == 2
    assert [step["file_path"] for step in card["steps"]] == ["/w/a.md", "/w/b.md"]
