"""Tests for ciao.subagent_tracking (session-JSONL subagent state)."""

from __future__ import annotations

import json
from pathlib import Path

from ciao.subagent_tracking import (
    SessionSubagentState,
    parse_session_subagents,
)


def _user_text(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_dispatch(tool_use_id: str, description: str, subagent_type: str = "memory") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Dispatching."},
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Agent",
                    "input": {
                        "description": description,
                        "subagent_type": subagent_type,
                        "run_in_background": True,
                    },
                },
            ],
        },
    }


def _dispatch_result(tool_use_id: str, agent_id: str, *, is_async: bool = True) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": "Async agent launched."}],
                }
            ],
        },
        "toolUseResult": {
            "isAsync": is_async,
            "status": "async_launched" if is_async else "completed",
            "agentId": agent_id,
            "description": "",
        },
    }


def _notification(agent_id: str, status: str = "completed") -> str:
    return (
        "<task-notification>\n"
        f"<task-id>{agent_id}</task-id>\n"
        "<tool-use-id>call_x</tool-use-id>\n"
        f"<status>{status}</status>\n"
        "<summary>Agent finished</summary>\n"
        "</task-notification>"
    )


def _write_session(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_async_dispatch_is_running_until_notification(tmp_path: Path) -> None:
    records = [
        _user_text("please curate my memory"),
        _assistant_dispatch("toolu_1", "Curate memory"),
        _dispatch_result("toolu_1", "abc123"),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    info = state.subagents["abc123"]
    assert info.is_async is True
    assert info.status == "running"
    assert info.tool_use_id == "toolu_1"
    assert info.description == "Curate memory"
    assert info.subagent_type == "memory"
    assert info.turn_index == 0
    assert state.running_background == 1

    records.append(_user_text(_notification("abc123")))
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.subagents["abc123"].status == "completed"
    assert state.running_background == 0


def test_enqueued_notification_counts_as_completion(tmp_path: Path) -> None:
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": _notification("abc123", status="failed"),
        },
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.subagents["abc123"].status == "failed"
    assert state.running_background == 0


def test_sync_dispatch_completes_immediately(tmp_path: Path) -> None:
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Explore"),
        _dispatch_result("toolu_1", "abc123", is_async=False),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.subagents["abc123"].status == "completed"
    assert state.running_background == 0


def test_turn_index_skips_non_user_bubbles(tmp_path: Path) -> None:
    records = [
        _user_text("first real turn"),
        _user_text("/model claude-opus-4-8"),  # control slash: not a turn
        _user_text("<task-notification><task-id>x</task-id></task-notification>"),
        _user_text("second real turn"),
        _assistant_dispatch("toolu_2", "Dig in"),
        _dispatch_result("toolu_2", "def456"),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.subagents["def456"].turn_index == 1


def test_orphan_notification_recorded(tmp_path: Path) -> None:
    # Nested agents (spawned by another subagent) notify the parent session
    # without a parent-level dispatch record.
    records = [_user_text(_notification("nested9"))]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.subagents["nested9"].status == "completed"
    assert state.running_background == 0


def test_missing_file_returns_empty_state(tmp_path: Path) -> None:
    state = parse_session_subagents(tmp_path / "missing.jsonl")
    assert state.subagents == {}
    assert state.running_background == 0


def test_running_background_counts_only_async_running() -> None:
    state = SessionSubagentState()
    from ciao.subagent_tracking import SubagentInfo

    state.subagents["a"] = SubagentInfo(agent_id="a", is_async=True, status="running")
    state.subagents["b"] = SubagentInfo(agent_id="b", is_async=True, status="completed")
    state.subagents["c"] = SubagentInfo(agent_id="c", is_async=False, status="completed")
    assert state.running_background == 1
