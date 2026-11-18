"""ACP update normalization (#3137): pure update -> StreamEvent translation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.models import (
    AssistantTextDelta,
    ModelChangedEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ciao.providers.acp.normalize import TurnText, UpdateNormalizer, canonical, result_event

FIXTURES = Path(__file__).parent / "fixtures" / "acp"


def _normalizer() -> UpdateNormalizer:
    return UpdateNormalizer(SimpleNamespace(model_config_id="model"), "s1")


def _text(text: str, **extra: Any) -> dict[str, Any]:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}, **extra}


def test_canonical_prefers_camel_over_snake_duplicate() -> None:
    out = canonical({"toolCallId": "camel", "tool_call_id": "snake", "nested": {"tool_call_id": "s", "toolCallId": "c"}})
    assert out == {"toolCallId": "camel", "nested": {"toolCallId": "c"}}
    # order must not matter
    assert canonical({"tool_call_id": "snake", "toolCallId": "camel"}) == {"toolCallId": "camel"}


def test_canonical_camelizes_snake_only_keys() -> None:
    out = canonical({"session_update": "plan", "items": [{"tool_call_id": "t"}]})
    assert out == {"sessionUpdate": "plan", "items": [{"toolCallId": "t"}]}


def test_canonical_leaves_opaque_payload_keys_alone() -> None:
    out = canonical({"rawInput": {"file_path": "/a"}, "_meta": {"terminal_info": {"terminal_id": "x"}}})
    assert out == {"rawInput": {"file_path": "/a"}, "_meta": {"terminal_info": {"terminal_id": "x"}}}


def test_message_chunk_carries_claude_parent_tool_use_id() -> None:
    n = _normalizer()
    (event,) = n.feed(_text("hi", _meta={"claudeCode": {"parentToolUseId": "toolu_task"}}))
    assert isinstance(event, AssistantTextDelta)
    assert (event.text, event.parent_tool_use_id) == ("hi", "toolu_task")
    (top,) = n.feed(_text("top"))
    assert isinstance(top, AssistantTextDelta) and top.parent_tool_use_id is None


def test_opencode_child_meta_sets_parent() -> None:
    n = _normalizer()
    meta = {"_meta": {"opencode/child-session": {"id": "ses_child", "parentID": "ses_root", "depth": 1}}}
    (event,) = n.feed({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "t"}, **meta})
    assert isinstance(event, ThinkingEvent)
    assert event.parent_tool_use_id == "ses_child"
    assert n.parent_of({}) is None


def test_non_text_chunk_is_ignored() -> None:
    n = _normalizer()
    assert n.feed({"sessionUpdate": "agent_message_chunk", "content": {"type": "image", "data": "x"}}) == []


def _tool_call(raw: Any, *, kind: str = "execute", title: str = "Run", locations: Any = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"sessionUpdate": "tool_call", "toolCallId": "t1", "kind": kind, "title": title}
    if raw is not None:
        frame["rawInput"] = raw
    if locations is not None:
        frame["locations"] = locations
    return frame


def test_tool_call_summary_uses_command_then_location_then_title() -> None:
    (cmd,) = _normalizer().feed(_tool_call({"command": "x" * 300}))
    assert isinstance(cmd, ToolUseEvent) and cmd.tool_input == "x" * 200
    (loc,) = _normalizer().feed(_tool_call({"other": 1}, kind="read", locations=[{"path": "/w/a.py"}]))
    assert isinstance(loc, ToolUseEvent) and loc.tool_input == "/w/a.py"
    (title,) = _normalizer().feed(_tool_call(None, kind="think", title="Pondering"))
    assert isinstance(title, ToolUseEvent) and title.tool_input == "Pondering" and title.tool_name == "Pondering"


def test_tool_name_falls_back_to_kind_then_tool() -> None:
    (by_kind,) = _normalizer().feed({"sessionUpdate": "tool_call", "toolCallId": "a", "kind": "fetch"})
    (bare,) = _normalizer().feed({"sessionUpdate": "tool_call", "toolCallId": "b"})
    assert isinstance(by_kind, ToolUseEvent) and by_kind.tool_name == "fetch"
    assert isinstance(bare, ToolUseEvent) and bare.tool_name == "tool"


@pytest.mark.parametrize("kind", ["edit", "delete", "move"])
def test_edit_tool_call_produces_file_touches(kind: str) -> None:
    (event,) = _normalizer().feed(_tool_call(None, kind=kind, locations=[{"path": "/w/a.py"}, {"path": "/w/b.py"}]))
    assert isinstance(event, ToolUseEvent)
    assert event.file_touches == [
        {"file_path": "/w/a.py", "action": "edit"},
        {"file_path": "/w/b.py", "action": "edit"},
    ]


@pytest.mark.parametrize("kind", ["read", "search", "execute", "fetch", "other"])
def test_read_tool_call_has_no_file_touches(kind: str) -> None:
    (event,) = _normalizer().feed(_tool_call(None, kind=kind, locations=[{"path": "/w/a.py"}]))
    assert isinstance(event, ToolUseEvent)
    assert event.file_touches is None


def test_execute_tool_call_reports_work_dirs_and_facts() -> None:
    (event,) = _normalizer().feed(_tool_call({"command": "cd /work/app && ciao memory"}))
    assert isinstance(event, ToolUseEvent)
    assert event.work_dirs == ["/work/app"]
    assert event.control_surface == "cli"
    assert event.input_chars and event.input_chars > 0
    (read,) = _normalizer().feed(_tool_call({"command": "cd /work/app"}, kind="read"))
    assert isinstance(read, ToolUseEvent) and read.work_dirs is None


def test_tool_call_update_failed_is_error_result() -> None:
    n = _normalizer()
    n.feed(_tool_call(None))
    (event,) = n.feed({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "failed"})
    assert isinstance(event, ToolUseEvent)
    assert (event.type, event.tool_use_id, event.is_error) == ("tool_result", "t1", True)


def test_tool_call_update_completed_not_error() -> None:
    n = _normalizer()
    n.feed(_tool_call(None))
    (event,) = n.feed({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
    assert isinstance(event, ToolUseEvent)
    assert (event.type, event.is_error) == ("tool_result", False)


def test_tool_call_update_refreshes_same_id_with_new_title_and_locations() -> None:
    n = _normalizer()
    n.feed(_tool_call(None, kind="edit", title="Preparing file", locations=[]))
    (event,) = n.feed(
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "title": "Write a.ts", "locations": [{"path": "/w/a.ts"}]}
    )
    assert isinstance(event, ToolUseEvent)
    assert (event.type, event.tool_use_id, event.tool_name) == ("tool_use", "t1", "Write a.ts")
    assert event.file_touches == [{"file_path": "/w/a.ts", "action": "edit"}]
    # an update that changes nothing, or names an unknown call, emits nothing
    assert n.feed({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "title": "Write a.ts"}) == []
    assert n.feed({"sessionUpdate": "tool_call_update", "toolCallId": "nope", "title": "x"}) == []


def test_usage_update_records_context_and_cost_without_event() -> None:
    n = _normalizer()
    assert n.feed({"sessionUpdate": "usage_update", "used": 10, "size": 200, "cost": {"amount": 0.5, "currency": "USD"}}) == []
    assert n.usage == {"context_used": "10", "context_size": "200", "cost_usd": "0.5"}
    n.record_prompt_usage({"inputTokens": 3, "outputTokens": 4, "totalTokens": 7})
    assert n.usage["input_tokens"] == "3" and n.usage["total_tokens"] == "7"


def _options(model: str) -> dict[str, Any]:
    return {
        "sessionUpdate": "config_option_update",
        "configOptions": [
            {"id": "model", "currentValue": model},
            {"id": "mode", "currentValue": "default"},
        ],
    }


def test_model_config_update_emits_model_changed_once() -> None:
    n = _normalizer()
    first = n.feed(_options("m1"))
    assert [(type(e), e.model) for e in first if isinstance(e, ModelChangedEvent)] == [(ModelChangedEvent, "m1")]
    assert n.feed(_options("m1")) == []
    (changed,) = n.feed(_options("m2"))
    assert isinstance(changed, ModelChangedEvent) and changed.model == "m2"
    assert n.current_values == {"model": "m2", "mode": "default"}


def test_current_mode_update_is_kept_without_event() -> None:
    n = _normalizer()
    assert n.feed({"sessionUpdate": "current_mode_update", "currentModeId": "plan"}) == []
    assert n.current_values["mode"] == "plan"


def test_compaction_completed_sets_flag_and_status() -> None:
    n = _normalizer()
    (event,) = n.feed({"sessionUpdate": "compaction_update", "compactionId": "c", "status": "completed"})
    assert isinstance(event, SystemStatusEvent) and event.status == "Compacted context"
    assert n.compacted is True


@pytest.mark.parametrize("status", ["in_progress", "failed", "cancelled"])
def test_compaction_in_progress_does_not(status: str) -> None:
    n = _normalizer()
    assert n.feed({"sessionUpdate": "compaction_update", "compactionId": "c", "status": status}) == []
    assert n.compacted is False


@pytest.mark.parametrize(
    "kind", ["plan", "available_commands_update", "session_info_update", "notice", "user_message_chunk", "compaction_summary_chunk"]
)
def test_known_silent_kinds_emit_nothing(kind: str) -> None:
    assert _normalizer().feed({"sessionUpdate": kind}) == []


def test_unknown_kind_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    n = _normalizer()
    with caplog.at_level(logging.DEBUG, logger="ciao.providers.acp.normalize"):
        assert n.feed({"sessionUpdate": "brand_new"}) == []
        assert n.feed({"sessionUpdate": "brand_new"}) == []
        assert n.feed({"sessionUpdate": "other_new"}) == []
    messages = [r.getMessage() for r in caplog.records]
    assert sum("brand_new" in m for m in messages) == 1  # one DEBUG line per kind
    assert sum("other_new" in m for m in messages) == 1


def test_snake_case_frame_is_normalized_like_camel() -> None:
    (event,) = _normalizer().feed({"session_update": "tool_call", "tool_call_id": "t9", "kind": "read"})
    assert isinstance(event, ToolUseEvent) and event.tool_use_id == "t9"


def _tool(tool_id: str | None, parent: str | None = None, type_: str = "tool_use") -> ToolUseEvent:
    return ToolUseEvent(type=type_, tool_name="x", tool_use_id=tool_id, parent_tool_use_id=parent)


def _delta(text: str, parent: str | None = None) -> AssistantTextDelta:
    return AssistantTextDelta(type="text", text=text, parent_tool_use_id=parent)


def test_turn_text_final_text_is_segment_after_last_top_level_tool() -> None:
    t = TurnText()
    for e in (_delta("a "), _tool("1"), _delta("b "), _tool("2"), _delta("c")):
        t.feed(e)
    assert t.result == "a b c"
    assert t.final_text == "c"
    # a refresh of an already-seen call does not start a new segment
    t.feed(_tool("2"))
    t.feed(_delta("d"))
    assert t.final_text == "cd"
    # a tool_result never splits
    t.feed(_tool("2", type_="tool_result"))
    assert t.final_text == "cd"


def test_turn_text_without_tool_call_has_no_final_text() -> None:
    t = TurnText()
    t.feed(_delta("only"))
    assert (t.result, t.final_text) == ("only", None)


def test_turn_text_tool_last_gives_empty_final_text() -> None:
    t = TurnText()
    t.feed(_delta("said"))
    t.feed(_tool("1"))
    assert (t.result, t.final_text) == ("said", "")


def test_subagent_text_not_in_result() -> None:
    t = TurnText()
    for e in (_delta("main "), _tool("sub1", parent="task"), _delta("child", parent="task"), _delta("end")):
        t.feed(e)
    assert t.result == "main end"
    assert t.final_text is None  # the child's tool call did not split the top-level text


@pytest.mark.parametrize(
    ("stop", "body", "is_error", "stopped", "expected"),
    [
        ("end_turn", "answer", False, False, "answer"),
        ("cancelled", "partial", False, True, "partial"),
        ("max_tokens", "cut off", True, False, "cut off"),
        ("max_tokens", "", True, False, "The model hit its output limit before finishing."),
        ("max_turn_requests", "text", True, False, "The agent stopped after too many steps in one turn."),
        ("refusal", "text", True, False, "The model declined to continue this request."),
        ("mystery", "text", True, False, "The agent ended the turn with an unknown reason: mystery"),
    ],
)
def test_stop_reason_mapping(stop: str, body: str, is_error: bool, stopped: bool, expected: str) -> None:
    t = TurnText()
    t.feed(_delta(body))
    event = result_event(stop, text=t, session_id="s1", model="m", usage={"context_used": "1"}, origin="o")
    assert event.type == "result"
    assert event.result == expected
    assert (event.is_error, event.stopped) == (is_error, stopped)
    assert (event.session_id, event.effective_model, event.usage, event.origin) == ("s1", "m", {"context_used": "1"}, "o")
    assert event.final_text is None


def test_result_event_carries_final_text_and_copies_usage() -> None:
    t = TurnText()
    for e in (_delta("a"), _tool("1"), _delta("b")):
        t.feed(e)
    usage = {"k": "v"}
    event = result_event("end_turn", text=t, session_id="s", model="m", usage=usage)
    assert (event.result, event.final_text) == ("ab", "b")
    usage["k"] = "changed"
    assert event.usage == {"k": "v"}


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode"])
def test_fixture_replay(agent: str) -> None:
    n = _normalizer()
    got: list[list[Any]] = []
    for line in (FIXTURES / f"updates_{agent}.jsonl").read_text().splitlines():
        for e in n.feed(json.loads(line)):
            got.append([e.type, getattr(e, "tool_use_id", None), getattr(e, "parent_tool_use_id", None)])
    assert got == json.loads((FIXTURES / f"updates_{agent}.golden.json").read_text())


def test_fixture_replay_claude_turn_text_and_state() -> None:
    n = _normalizer()
    text = TurnText()
    for line in (FIXTURES / "updates_claude.jsonl").read_text().splitlines():
        for e in n.feed(json.loads(line)):
            text.feed(e)
    assert text.result == "Looking at the file. Done."
    assert text.final_text == "Done."
    assert n.compacted is True
    assert n.usage == {"context_used": "2", "context_size": "200000"}
