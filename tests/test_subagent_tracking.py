"""Tests for ciao.subagent_tracking (session-JSONL subagent state)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ciao.subagent_tracking import (
    SUBAGENT_SYNTHESIS_NUDGE,
    SessionSubagentState,
    ends_with_user_question,
    has_finished_transcript,
    is_synthesis_nudge,
    parse_session_subagents,
    running_background_agents,
    running_tasks,
    subagent_transcript_path,
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


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
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


def _monitor_dispatch(tool_use_id: str, description: str) -> dict:
    command = "tail -f /tmp/adoption_2026-08.log | grep -E --line-buffered \"FAILED|PASSED\""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Monitor",
                    "input": {
                        "command": command,
                        "description": description,
                        "timeout_ms": 3600000,
                        "persistent": False,
                    },
                }
            ],
        },
    }


def _task_result(tool_use_id: str, task_id: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "Monitor started (task bl7dzu4ku, timeout 3600000ms).",
                }
            ],
        },
        "toolUseResult": {
            "taskId": task_id,
            "timeoutMs": 3600000,
            "persistent": False,
        },
    }


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


def test_synthesis_nudge_recognized_with_context_prefix() -> None:
    assert is_synthesis_nudge(SUBAGENT_SYNTHESIS_NUDGE)
    # Sent with the chat's context prefix attached, and re-wrapped by whatever
    # wrote the JSONL, so matching must survive both.
    prefixed = (
        '[Chat ID: "chat-1"]\n[Project: "Ciaobot"]\n\n'
        + SUBAGENT_SYNTHESIS_NUDGE.replace(" results ", "\n  results  ")
    )
    assert is_synthesis_nudge(prefixed)
    assert not is_synthesis_nudge("")
    assert not is_synthesis_nudge("post your consolidated final report")


def test_synthesis_nudge_does_not_advance_turn_index(tmp_path: Path) -> None:
    # The nudge is server-injected, so it must not shift `turn_index` — the
    # /messages renderer skips it the same way (see routes_api.chat_messages).
    records = [
        _user_text("first real turn"),
        _user_text(SUBAGENT_SYNTHESIS_NUDGE),
        _user_text("second real turn"),
        _assistant_dispatch("toolu_2", "Dig in"),
        _dispatch_result("toolu_2", "def456"),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.subagents["def456"].turn_index == 1


def test_last_assistant_text_tracks_awaiting_user_answer(tmp_path: Path) -> None:
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        _assistant_text("Agents are running.\n\nWhich slice do you want removed?"),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.last_assistant_text.endswith("removed?")
    assert state.awaiting_user_answer is True

    records.append(_assistant_text("Never mind, I'll report back when they finish."))
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.awaiting_user_answer is False


def test_last_assistant_text_ignores_textless_records(tmp_path: Path) -> None:
    # A trailing tool-only assistant record must not blank out the question.
    records = [
        _assistant_text("Which slice do you want removed?"),
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_9", "name": "Read", "input": {}}
                ],
            },
        },
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.awaiting_user_answer is True


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Which slice do you want removed?", True),
        ("**Which slice do you want removed?**", True),
        ("Done. (Want me to also drop the welcome chat?)", True),
        ("Which one?\n\n", True),
        ("I'll report back when they finish.", False),
        ("Shall I proceed? Yes — starting now.", False),
        ("", False),
        ("---", False),
    ],
)
def test_ends_with_user_question(text: str, expected: bool) -> None:
    assert ends_with_user_question(text) is expected


def test_running_background_counts_only_async_running() -> None:
    state = SessionSubagentState()
    from ciao.subagent_tracking import SubagentInfo

    state.subagents["a"] = SubagentInfo(agent_id="a", is_async=True, status="running")
    state.subagents["b"] = SubagentInfo(agent_id="b", is_async=True, status="completed")
    state.subagents["c"] = SubagentInfo(agent_id="c", is_async=False, status="completed")
    assert state.running_background == 1


# ── Transcript-side completion fallback ──────────────────────────────────


def _write_agent_transcript(
    parent_path: Path, agent_id: str, records: list[dict], *, age_seconds: float = 0.0
) -> Path:
    path = subagent_transcript_path(parent_path, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


def _tool_use_record(name: str = "Bash") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_x", "name": name, "input": {}}],
        },
    }


def test_finished_transcript_needs_final_prose_and_idleness(tmp_path: Path) -> None:
    parent = _write_session(tmp_path, [_user_text("go")])

    # Idle long enough, and the tail is the agent's closing answer.
    _write_agent_transcript(
        parent, "done", [_tool_use_record(), _assistant_text("Here is the report.")],
        age_seconds=600,
    )
    assert has_finished_transcript(parent, "done") is True
    # The CLI's own ids carry an "agent-" prefix; both spellings resolve.
    assert has_finished_transcript(parent, "agent-done") is True

    # Same tail, but written moments ago: too soon to call it.
    _write_agent_transcript(parent, "fresh", [_assistant_text("Here is the report.")])
    assert has_finished_transcript(parent, "fresh") is False

    # Idle for ages but parked on a tool call: a slow Bash step, still running.
    _write_agent_transcript(
        parent, "slow", [_assistant_text("Running the suite."), _tool_use_record()],
        age_seconds=600,
    )
    assert has_finished_transcript(parent, "slow") is False

    # Dispatched but nothing written yet: still starting up.
    assert has_finished_transcript(parent, "missing") is False


def test_running_background_agents_drops_agents_with_finished_transcripts(
    tmp_path: Path,
) -> None:
    """The parent JSONL can miss a completion; the transcript still shows it."""
    records = [
        _user_text("investigate this"),
        _assistant_dispatch("toolu_1", "Trace the bug"),
        _dispatch_result("toolu_1", "aaa111"),
        _assistant_dispatch("toolu_2", "Check the tests"),
        _dispatch_result("toolu_2", "bbb222"),
    ]
    parent = _write_session(tmp_path, records)
    state = parse_session_subagents(parent)
    # No <task-notification> for either agent: the parent still says both run.
    assert state.running_background == 2
    assert running_background_agents(parent, state) == 2

    _write_agent_transcript(
        parent, "aaa111", [_assistant_text("Traced it.")], age_seconds=600
    )
    assert running_background_agents(parent, state) == 1


# ── Pending-notification window (synthesis-nudge hold) ───────────────────


def test_notification_pending_after_enqueue_without_reply(tmp_path: Path) -> None:
    """An enqueued completion the CLI never answered leaves the window open.

    This is the 2026-08-30 daily-log shape: the last agent's notification was
    enqueued, the nudge raced it, and the run ended without any assistant
    record after the enqueue.
    """
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        _user_text(_notification("abc123")),  # interim turn already answered
        _assistant_text("Waiting on the last agent."),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": _notification("def456"),
        },
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.notification_pending is True


def test_notification_pending_after_user_record_without_reply(
    tmp_path: Path,
) -> None:
    """A notification user record no assistant answered holds the window too."""
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        {
            "type": "user",
            "message": {"role": "user", "content": _notification("abc123")},
        },
        # Trailing Stop-hook bookkeeping: not a reply, keeps the window open.
        {"type": "system", "subtype": "stop_hook_summary"},
        {"type": "attachment", "attachment": {"type": "hook_success"}},
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.notification_pending is True


def test_notification_window_closes_when_cli_answers(tmp_path: Path) -> None:
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": _notification("abc123"),
        },
        {
            "type": "queue-operation",
            "operation": "dequeue",
        },
        _assistant_text("All agents finished. Here is the report."),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.notification_pending is False


def test_notification_stays_pending_after_dequeue_until_assistant_reply(tmp_path: Path) -> None:
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        {"type": "queue-operation", "operation": "enqueue", "content": _notification("abc123")},
        {"type": "queue-operation", "operation": "dequeue"},
    ]

    state = parse_session_subagents(_write_session(tmp_path, records))

    assert state.notification_pending is True


def test_parent_assistant_record_does_not_clear_unclaimed_notification(tmp_path: Path) -> None:
    records = [
        _user_text("go"),
        {"type": "queue-operation", "operation": "enqueue", "content": _notification("abc123")},
        _assistant_text("The parent is still working."),
    ]

    state = parse_session_subagents(_write_session(tmp_path, records))

    assert state.notification_pending is True


def test_non_notification_enqueue_never_holds_the_window(tmp_path: Path) -> None:
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": "some plain queued message",
        },
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.notification_pending is False


def test_dequeue_of_ordinary_prompt_does_not_claim_queued_notification(
    tmp_path: Path,
) -> None:
    """Dequeuing a plain prompt queued ahead of a notification must not close it.

    The CLI processes the queue in order: an ordinary prompt enqueued before a
    completion notification is dequeued first, and its assistant reply is not
    evidence that the notification was handled. The notification window must
    stay open until the notification itself is dequeued and answered.
    """
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        {"type": "queue-operation", "operation": "enqueue", "content": "a plain prompt"},
        {"type": "queue-operation", "operation": "enqueue", "content": _notification("abc123")},
        {"type": "queue-operation", "operation": "dequeue"},
        _assistant_text("Handling the plain prompt."),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.notification_pending is True


def test_dequeue_of_notification_stays_pending_until_reply(tmp_path: Path) -> None:
    """Dequeuing the notification itself keeps the window open until a reply."""
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        {"type": "queue-operation", "operation": "enqueue", "content": _notification("abc123")},
        {"type": "queue-operation", "operation": "dequeue"},
        _assistant_text("All agents finished. Here is the report."),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.notification_pending is False


def test_dequeued_notification_that_lands_as_a_user_record_closes_on_one_reply(
    tmp_path: Path,
) -> None:
    """The realistic ordering: enqueue → dequeue → user record → reply.

    One notification passes through all three records, so it must occupy one
    queue slot, not two. Counting it twice left `notification_pending` true
    after the CLI had already answered — and a stuck pending flag pins
    `held_ticks` in the nudge poller, so the *next* real notification starts
    out past its grace and the nudge steers into the CLI's processing window.
    """
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": _notification("abc123"),
        },
        {"type": "queue-operation", "operation": "dequeue"},
        {
            "type": "user",
            "message": {"role": "user", "content": _notification("abc123")},
        },
        _assistant_text("All agents finished. Here is the report."),
    ]

    state = parse_session_subagents(_write_session(tmp_path, records))

    assert state.notification_pending is False


def test_second_notification_after_a_closed_one_reopens_the_window(
    tmp_path: Path,
) -> None:
    """A fresh notification still holds, and only its own reply closes it."""
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Research"),
        _dispatch_result("toolu_1", "abc123"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": _notification("abc123"),
        },
        {"type": "queue-operation", "operation": "dequeue"},
        {
            "type": "user",
            "message": {"role": "user", "content": _notification("abc123")},
        },
        _assistant_text("First agent is done."),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": _notification("def456"),
        },
        {"type": "queue-operation", "operation": "dequeue"},
    ]

    state = parse_session_subagents(_write_session(tmp_path, records))

    assert state.notification_pending is True


def test_monitor_dispatch_is_tracked_as_running_task(tmp_path: Path) -> None:
    description = "adoption report 2026-08 DAG progress and failures"
    records = [
        _user_text("watch the adoption report"),
        _monitor_dispatch("toolu_01SsjL", description),
        _task_result("toolu_01SsjL", "bl7dzu4ku"),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    info = state.subagents["bl7dzu4ku"]
    assert info.kind == "task"
    assert info.status == "running"
    assert info.subagent_type == "Monitor"
    assert info.description == description
    assert info.command.startswith("tail -f /tmp/adoption_2026-08.log")
    # Agent counts are unchanged by CLI tasks.
    assert state.running_background == 0
    assert len(running_tasks(state)) == 1


def test_background_bash_dispatch_is_tracked_as_task(tmp_path: Path) -> None:
    records = [
        _user_text("run the build"),
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_bash1",
                        "name": "Bash",
                        "input": {
                            "command": "npm run build > /tmp/build.log 2>&1",
                            "run_in_background": True,
                        },
                    }
                ],
            },
        },
        _task_result("toolu_bash1", "bdvtr6cvj"),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    info = state.subagents["bdvtr6cvj"]
    assert info.kind == "task"
    assert info.status == "running"
    assert info.subagent_type == "Bash"


def test_task_notification_completes_task(tmp_path: Path) -> None:
    records = [
        _user_text("watch the adoption report"),
        _monitor_dispatch("toolu_01SsjL", "adoption report"),
        _task_result("toolu_01SsjL", "bl7dzu4ku"),
        _user_text(_notification("bl7dzu4ku")),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.subagents["bl7dzu4ku"].status == "completed"
    assert running_tasks(state) == []


def test_stopped_notification_keeps_raw_status(tmp_path: Path) -> None:
    records = [
        _user_text("watch the adoption report"),
        _monitor_dispatch("toolu_01SsjL", "adoption report"),
        _task_result("toolu_01SsjL", "bl7dzu4ku"),
        _user_text(_notification("bl7dzu4ku", "stopped")),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    info = state.subagents["bl7dzu4ku"]
    assert info.status == "completed"
    assert info.raw_status == "stopped"


def test_agent_and_task_counts_are_independent(tmp_path: Path) -> None:
    records = [
        _user_text("go"),
        _assistant_dispatch("toolu_1", "Curate memory"),
        _dispatch_result("toolu_1", "abc123"),
        _monitor_dispatch("toolu_2", "adoption report"),
        _task_result("toolu_2", "bl7dzu4ku"),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    assert state.running_background == 1
    assert len(running_tasks(state)) == 1


def test_running_agents_ignores_tasks(tmp_path: Path) -> None:
    records = [
        _user_text("watch the adoption report"),
        _monitor_dispatch("toolu_01SsjL", "adoption report"),
        _task_result("toolu_01SsjL", "bl7dzu4ku"),
    ]
    path = _write_session(tmp_path, records)
    state = parse_session_subagents(path)
    agents = running_background_agents(path, state)  # exercises running_agents
    assert agents == 0
    assert all(info.kind == "task" for info in state.subagents.values())


def test_cli_task_wake_marks_tasks_lost(tmp_path: Path) -> None:
    from ciao.subagent_tracking import CLI_TASK_WAKE_PREFIX

    records = [
        _user_text("watch the adoption report"),
        _monitor_dispatch("toolu_01SsjL", "adoption report"),
        _task_result("toolu_01SsjL", "bl7dzu4ku"),
        _user_text(
            CLI_TASK_WAKE_PREFIX
            + " 1 CLI task you started (Monitor / background shell) were lost: "
            "the Claude CLI process that owned them has exited.\n\n"
            "— Monitor: adoption report (task bl7dzu4ku)"
        ),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    info = state.subagents["bl7dzu4ku"]
    assert info.status == "lost"
    assert info.raw_status == "lost"
    assert info.kind == "task"
    assert running_tasks(state) == []


def test_cli_task_wake_marks_tasks_lost_with_context_prefix(tmp_path: Path) -> None:
    from ciao.subagent_tracking import CLI_TASK_WAKE_PREFIX

    records = [
        _user_text("watch the adoption report"),
        _monitor_dispatch("toolu_01SsjL", "adoption report"),
        _task_result("toolu_01SsjL", "bl7dzu4ku"),
        _user_text(
            "[CIAO_CONTEXT_BEGIN]\nworkspace=x\n[CIAO_CONTEXT_END]\n"
            + CLI_TASK_WAKE_PREFIX
            + " 1 CLI task you started (Monitor / background shell) were lost: "
            "the Claude CLI process that owned them has exited.\n\n"
            "— Monitor: adoption report (task bl7dzu4ku)"
        ),
    ]
    state = parse_session_subagents(_write_session(tmp_path, records))
    info = state.subagents["bl7dzu4ku"]
    assert info.status == "lost"
    assert info.raw_status == "lost"
    assert running_tasks(state) == []
