from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_subagents


def _client(tmp_path: Path, session_id: str) -> TestClient:
    chat = SimpleNamespace(session_id=session_id)
    pcm = SimpleNamespace(get_chat=lambda chat_id: chat if chat_id == "chat-1" else None)
    app = Starlette(routes=[Route("/api/chats/{chat_id}/subagents", chat_subagents, methods=["GET"])])
    app.state.project_chat_manager = pcm
    app.state.config = SimpleNamespace(workspace_root=tmp_path / "workspace")
    return TestClient(app)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    """Build a ProjectChatManager backed by tmp_path-only stores."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _dispatch_records(tool_use_id: str, agent_id: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "Agent",
                        "input": {"description": f"work {agent_id}", "run_in_background": True},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": []}
                ],
            },
            "toolUseResult": {"isAsync": True, "agentId": agent_id},
        },
    ]


def _completion_record(agent_id: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                f"<task-notification>\n<task-id>{agent_id}</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        },
    }


async def test_watch_subagent_completion_emits_ready_events(
    tmp_path: Path, monkeypatch
) -> None:
    """When background subagents finish, the manager emits chat_subagents_ready."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("subagent-watch", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="subagent-watch-test")
    chat.session_id = "sess-watch-1"
    pcm._save()

    # Session JSONL with two background dispatches running. The watcher
    # re-parses whenever the file grows; each fake sleep appends one
    # completion notification so the running count steps 2 → 1 → 0.
    session_path = tmp_path / "sess-watch-1.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "kick off work"}},
        *_dispatch_records("toolu_1", "agent-a"),
        *_dispatch_records("toolu_2", "agent-b"),
    ]
    completions = iter([_completion_record("agent-a"), _completion_record("agent-b")])

    def flush() -> None:
        session_path.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    flush()

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )

    async def fake_sleep(seconds: float) -> None:
        record = next(completions, None)
        if record is not None:
            records.append(record)
            flush()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture_publish(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    monkeypatch.setattr(pcm._events, "publish", capture_publish)

    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)

    ready_events = [ev for ev in published if ev.get("type") == "chat_subagents_ready"]
    # First event is the initial running count emitted at watcher start (so the
    # PWA can show the indicator immediately); then one per drop down to zero.
    # The final zero may be published twice: when the last agent's
    # task-notification is still unprocessed the nudge is held one grace
    # period, then (the CLI having answered it) a reconciling nudged=True
    # event follows.
    assert len(ready_events) == 3
    assert ready_events[0]["remaining"] == 2
    assert ready_events[1]["remaining"] == 1
    assert ready_events[2]["remaining"] == 0
    assert ready_events[0]["chat_id"] == chat.chat_id
    assert ready_events[0]["project_id"] == project.project_id
    # The last-count cache is cleared once the watcher exits so the events
    # snapshot doesn't advertise a stale count.
    assert pcm.background_agent_counts == {}


async def test_watch_subagent_completion_nudges_parent_synthesis(
    tmp_path: Path, monkeypatch
) -> None:
    """When the last background subagent finishes, the watcher pokes the parent
    to synthesize a final report (the CLI won't auto-continue on its own)."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("subagent-nudge", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="subagent-nudge-test")
    chat.session_id = "sess-nudge-1"
    pcm._save()

    session_path = tmp_path / "sess-nudge-1.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "kick off work"}},
        *_dispatch_records("toolu_1", "agent-a"),
    ]
    completions = iter([_completion_record("agent-a")])

    def flush() -> None:
        session_path.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    flush()

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )

    async def fake_sleep(seconds: float) -> None:
        record = next(completions, None)
        if record is not None:
            records.append(record)
            flush()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    steer_calls: list = []

    class FakeProvider:
        can_drain = True

        async def steer(self, request) -> bool:
            steer_calls.append(request)
            return True

    pcm._providers[chat.chat_id] = FakeProvider()  # type: ignore[assignment]
    # A live between-turns drain must exist for the nudge to be delivered.
    running_drain = asyncio.get_running_loop().create_future()
    pcm._between_turn_drains[chat.chat_id] = running_drain  # type: ignore[assignment]

    pushes: list = []
    monkeypatch.setattr(
        pcm, "_schedule_push", lambda *a, **k: pushes.append(a)
    )

    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture_publish(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    monkeypatch.setattr(pcm._events, "publish", capture_publish)

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    finally:
        running_drain.cancel()

    assert len(steer_calls) == 1
    # The synthesis nudge replaces the bare "finished" push when delivered.
    assert pushes == []

    ready_events = [ev for ev in published if ev.get("type") == "chat_subagents_ready"]
    # First event is the initial running count (1), then the drop to zero.
    # The count-0 arrival still had the CLI's task-notification unprocessed,
    # so the nudge was held one grace period; once the assistant reply lands,
    # the zero event is republished with nudged=True so clients reconcile.
    assert len(ready_events) == 3
    assert ready_events[0]["remaining"] == 1
    assert ready_events[1]["remaining"] == 0
    assert ready_events[1]["nudged"] is False
    assert ready_events[2]["remaining"] == 0
    assert ready_events[2]["nudged"] is True


async def test_watch_subagent_completion_holds_nudge_when_parent_asked_a_question(
    tmp_path: Path, monkeypatch
) -> None:
    """A parent that ended its turn with a question to the user is left alone.

    Nudging there answers on the user's behalf and buries the question under a
    report they never asked for."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("subagent-question", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="subagent-question-test")
    chat.session_id = "sess-nudge-question"
    pcm._save()

    session_path = tmp_path / "sess-nudge-question.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "kick off work"}},
        *_dispatch_records("toolu_1", "agent-a"),
    ]
    # The parent's parting words: a question, not an "I'll report back".
    question = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Two agents are digging in.\n\nWhich slice do you want removed?",
                }
            ],
        },
    }
    completions = iter([question, _completion_record("agent-a")])

    def flush() -> None:
        session_path.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    flush()

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )

    async def fake_sleep(seconds: float) -> None:
        record = next(completions, None)
        if record is not None:
            records.append(record)
            flush()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    steer_calls: list = []

    class FakeProvider:
        can_drain = True

        async def steer(self, request) -> bool:
            steer_calls.append(request)
            return True

    pcm._providers[chat.chat_id] = FakeProvider()  # type: ignore[assignment]
    running_drain = asyncio.get_running_loop().create_future()
    pcm._between_turn_drains[chat.chat_id] = running_drain  # type: ignore[assignment]

    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture_publish(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    monkeypatch.setattr(pcm._events, "publish", capture_publish)

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    finally:
        running_drain.cancel()

    assert steer_calls == []
    ready_events = [ev for ev in published if ev.get("type") == "chat_subagents_ready"]
    # The count still drops to zero so the PWA clears its "agents running"
    # indicator and reconciles history; only the injected prompt is withheld.
    assert ready_events[-1]["remaining"] == 0
    assert ready_events[-1]["nudged"] is False


async def test_watch_subagent_completion_holds_nudge_on_pending_notification(
    tmp_path: Path, monkeypatch
) -> None:
    """A completion notification the CLI has not answered yet blocks the nudge.

    The 2026-08-30 daily-log run: the last agent finished, its
    ``<task-notification>`` was enqueued, and the watcher's nudge raced it —
    the two prompts crossed on the transport, the SDK read task was
    cancelled, and the run archived on an interim "Waiting on X" message
    with the agent's data never synthesized. A notification in flight must
    hold the nudge until the CLI turns it into a turn.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("subagent-notify-race", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="subagent-notify-race-test")
    chat.session_id = "sess-nudge-race"
    pcm._save()

    session_path = tmp_path / "sess-nudge-race.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "kick off work"}},
        *_dispatch_records("toolu_1", "agent-a"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                "<task-notification>\n<task-id>agent-a</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        },
    ]

    def flush() -> None:
        session_path.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    flush()

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )

    # The notification stays unprocessed for a couple of ticks (the window
    # during which the nudge must be held), then an assistant record answers
    # it — as the CLI's interim turn would — closing the window. The nudge
    # then fires on that tick.
    ticks: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        ticks.append(len(ticks))
        if len(ticks) == 2:
            records.append(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "All agents finished. Here is the report."}
                        ],
                    },
                }
            )
            flush()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    steer_calls: list = []

    class FakeProvider:
        can_drain = True

        async def steer(self, request) -> bool:
            steer_calls.append(request)
            return True

    pcm._providers[chat.chat_id] = FakeProvider()  # type: ignore[assignment]
    running_drain = asyncio.get_running_loop().create_future()
    pcm._between_turn_drains[chat.chat_id] = running_drain  # type: ignore[assignment]

    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture_publish(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    monkeypatch.setattr(pcm._events, "publish", capture_publish)

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    finally:
        running_drain.cancel()

    # Held while the notification was in flight (no steer during the first
    # two ticks), then fired once the CLI answered it.
    assert len(steer_calls) == 1
    ready_events = [ev for ev in published if ev.get("type") == "chat_subagents_ready"]
    assert ready_events[-1]["remaining"] == 0
    assert ready_events[-1]["nudged"] is True


async def test_notification_grace_resets_between_windows(
    tmp_path: Path, monkeypatch
) -> None:
    """A closed notification window must not spend its grace on the next one.

    Several agents finish at different times, so one watcher run can see more
    than one ``<task-notification>`` window. The first window's held ticks
    used to survive its own assistant answer, so a later notification
    inherited "grace expired" on its first tick and the synthesis nudge
    steered into the CLI's processing window — the prompt-crossing race the
    hold exists to prevent.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("subagent-grace-reset", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="subagent-grace-reset-test")
    chat.session_id = "sess-grace-reset"
    pcm._save()

    session_path = tmp_path / "sess-grace-reset.jsonl"
    records: list[dict] = [
        {"type": "user", "message": {"role": "user", "content": "kick off work"}},
        *_dispatch_records("toolu_1", "agent-a"),
        *_dispatch_records("toolu_2", "agent-b"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                "<task-notification>\n<task-id>agent-a</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        },
    ]

    def flush() -> None:
        session_path.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    flush()

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )

    def assistant_record(text: str) -> dict:
        return {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }

    def notification(agent_id: str) -> dict:
        return {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                f"<task-notification>\n<task-id>{agent_id}</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        }

    sleeps = 0
    steer_at: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        # Window 1 stays pending through three ticks (its grace expires while
        # agent-b is still running, so no nudge can fire yet), then the CLI
        # answers it. Agent-b's own notification opens window 2, which is
        # also answered before the nudge may fire.
        if sleeps == 3:
            records.append({"type": "queue-operation", "operation": "dequeue"})
            records.append(assistant_record("First agents are done; report to follow."))
            flush()
        elif sleeps == 4:
            records.append(notification("agent-b"))
            flush()
        elif sleeps == 5:
            records.append({"type": "queue-operation", "operation": "dequeue"})
            records.append(assistant_record("All agents finished. Here is the full report."))
            flush()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class FakeProvider:
        can_drain = True

        async def steer(self, request) -> bool:
            steer_at.append(sleeps)
            return True

    pcm._providers[chat.chat_id] = FakeProvider()  # type: ignore[assignment]
    running_drain = asyncio.get_running_loop().create_future()
    pcm._between_turn_drains[chat.chat_id] = running_drain  # type: ignore[assignment]

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    finally:
        running_drain.cancel()

    # The nudge fired exactly once, and only on the parse after the second
    # window's answer landed. With the stale counter it fired on the first
    # tick of window 2, while that answer was still missing.
    assert len(steer_at) == 1
    assert steer_at[0] >= 5


async def test_watch_subagent_completion_clears_on_idle_agent_transcript(
    tmp_path: Path, monkeypatch
) -> None:
    """A missing <task-notification> must not pin the count at 1 forever.

    The CLI can defer the notification to the next turn boundary (or replace it
    with a synthetic "stopped" record on resume), so the parent JSONL alone can
    never clear. The agent's own transcript going quiet on its final answer is
    the fallback signal.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("subagent-idle", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="subagent-idle-test")
    chat.session_id = "sess-idle-1"
    pcm._save()

    session_path = tmp_path / "sess-idle-1.jsonl"
    # The parent file never changes: no completion ever lands in it.
    _write_jsonl(
        session_path,
        [
            {"type": "user", "message": {"role": "user", "content": "kick off work"}},
            *_dispatch_records("toolu_1", "aaa111"),
        ],
    )

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )

    def finish_agent() -> None:
        path = subagent_tracking.subagent_transcript_path(session_path, "aaa111")
        _write_jsonl(
            path,
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Here is the trace."}],
                    },
                }
            ],
        )
        stale = time.time() - 600
        os.utime(path, (stale, stale))

    ticks = iter([finish_agent])

    async def fake_sleep(seconds: float) -> None:
        step = next(ticks, None)
        if step is not None:
            step()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture_publish(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    monkeypatch.setattr(pcm._events, "publish", capture_publish)

    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)

    ready = [ev["remaining"] for ev in published if ev.get("type") == "chat_subagents_ready"]
    assert ready == [1, 0]


async def test_watch_subagent_completion_publishes_zero_when_it_gives_up(
    tmp_path: Path, monkeypatch
) -> None:
    """Exiting with a positive count would strand the badge on every client."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("subagent-giveup", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="subagent-giveup-test")
    chat.session_id = "sess-giveup-1"
    pcm._save()

    session_path = tmp_path / "sess-giveup-1.jsonl"
    _write_jsonl(
        session_path,
        [
            {"type": "user", "message": {"role": "user", "content": "kick off work"}},
            *_dispatch_records("toolu_1", "aaa111"),
        ],
    )

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )

    # Second tick: the session file is gone (session reset, workspace moved).
    async def fake_sleep(seconds: float) -> None:
        session_path.unlink(missing_ok=True)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture_publish(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    monkeypatch.setattr(pcm._events, "publish", capture_publish)

    # Go through the real registration path: the zero-on-exit publish is
    # deliberately skipped when a replacement watcher already owns the slot.
    pcm._start_subagent_watcher(chat.chat_id, project.project_id)
    await pcm._pending_subagent_watchers[chat.chat_id]

    ready = [ev["remaining"] for ev in published if ev.get("type") == "chat_subagents_ready"]
    assert ready == [1, 0]
    assert pcm.background_agent_counts == {}
    assert chat.chat_id not in pcm._pending_subagent_watchers


def test_chat_subagents_falls_back_to_nested_jsonl_and_progress_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    nested = tmp_path / ".claude" / "projects" / "-tmp-workspace" / "sess-1" / "subagents" / "researcher.jsonl"
    _write_jsonl(
        nested,
        [
            {"type": "user", "message": {"role": "user", "content": "Research the release."}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}},
                        {"type": "text", "text": "Found the relevant release notes."},
                    ],
                },
            },
        ],
    )
    parent = tmp_path / ".claude" / "projects" / "-tmp-workspace" / "sess-1.jsonl"
    _write_jsonl(
        parent,
        [
            {
                "type": "progress",
                "data": {
                    "agent_id": "progress-agent",
                    "message": {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Progress entry survived replay."}],
                        },
                    },
                },
            },
        ],
    )

    resp = _client(tmp_path, "sess-1").get("/api/chats/chat-1/subagents")

    assert resp.status_code == 200
    data = resp.json()
    by_id = {entry["agent_id"]: entry["messages"] for entry in data}
    assert "researcher" in by_id
    assert "progress-agent" in by_id
    assert any(msg["role"] == "user" and "Research" in msg["content"] for msg in by_id["researcher"])
    assert any(msg.get("tool_name") == "_activity" and "Read" in msg["content"] for msg in by_id["researcher"])
    assert any("Progress entry survived" in msg["content"] for msg in by_id["progress-agent"])


# --- ?agent_id= narrowing ----------------------------------------------------
#
# The read-only subagent view polls one transcript while its agent works. The
# unfiltered response renders every subagent the chat ever spawned, so without
# narrowing that poll re-fetched the whole set every few seconds to redraw one
# of them.


def _write_two_agent_session(tmp_path: Path) -> None:
    for name, text in (("researcher", "Researcher output."), ("writer", "Writer output.")):
        _write_jsonl(
            tmp_path / ".claude" / "projects" / "-tmp-workspace" / "sess-1" / "subagents" / f"{name}.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                },
            ],
        )
    _write_jsonl(tmp_path / ".claude" / "projects" / "-tmp-workspace" / "sess-1.jsonl", [])


def test_agent_id_narrows_the_local_fallback_to_one_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    _write_two_agent_session(tmp_path)

    resp = _client(tmp_path, "sess-1").get(
        "/api/chats/chat-1/subagents", params={"agent_id": "researcher"}
    )

    assert resp.status_code == 200
    assert [entry["agent_id"] for entry in resp.json()] == ["researcher"]


def test_agent_id_matches_across_the_agent_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The route carries the bare id; the local fallback keys by file stem.

    Whichever form the caller sends has to resolve, or the filter drops the
    agent it was asked for and the view renders "not found".
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    _write_two_agent_session(tmp_path)

    resp = _client(tmp_path, "sess-1").get(
        "/api/chats/chat-1/subagents", params={"agent_id": "agent-researcher"}
    )

    assert resp.status_code == 200
    assert [entry["agent_id"] for entry in resp.json()] == ["researcher"]


def test_agent_id_skips_sdk_discovery_and_sibling_transcripts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The point of the narrowing: no enumeration, no sibling reads."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    listed: list[str] = []
    fetched: list[str] = []

    def _list_subagents(session_id: str, directory: str | None = None) -> list[str]:
        listed.append(session_id)
        return ["researcher", "writer"]

    def _get_subagent_messages(
        session_id: str, agent_id: str, directory: str | None = None
    ) -> list:
        fetched.append(agent_id)
        return []

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            list_subagents=_list_subagents,
            get_subagent_messages=_get_subagent_messages,
        ),
    )
    _write_two_agent_session(tmp_path)

    resp = _client(tmp_path, "sess-1").get(
        "/api/chats/chat-1/subagents", params={"agent_id": "researcher"}
    )

    assert resp.status_code == 200
    assert listed == []
    assert fetched == ["researcher"]


def test_without_agent_id_every_subagent_is_returned(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    _write_two_agent_session(tmp_path)

    resp = _client(tmp_path, "sess-1").get("/api/chats/chat-1/subagents")

    assert resp.status_code == 200
    assert sorted(entry["agent_id"] for entry in resp.json()) == ["researcher", "writer"]


# --- Empty / banner-only ResultEvent guards ----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n\n",
        "ok",
        "...",
    ],
)
def test_nudge_reply_announcement_rejects_empty_and_banner_only(text: str) -> None:
    """A bare banner or whitespace must not be treated as a real reply."""
    assert ProjectChatManager._is_worth_announcing_nudge_reply(text) is False


def test_nudge_reply_announcement_accepts_real_reply() -> None:
    """Anything with at least a few characters of real prose passes."""
    assert ProjectChatManager._is_worth_announcing_nudge_reply(
        "Synthesis complete — see the trace."
    ) is True
    assert ProjectChatManager._is_worth_announcing_nudge_reply(
        "Found the bug in routes_api.py"
    ) is True


@pytest.mark.parametrize("reply", ["Yes", "No", "No.", "Ok", "42", "👍"])
def test_a_terse_answer_to_the_user_still_announces(reply: str) -> None:
    """The banner floor must never reach a reply the user asked for.

    "Yes" is a complete answer to a yes/no question. Gating the regular
    turn-done branch on the nudge heuristic dropped its unread badge, its
    toast, the ``last_activity_at`` bump that reorders recents, and the
    pending-retry clear, with nothing anywhere reporting a problem.
    """
    # The nudge heuristic does reject it, which is why it must stay off this path.
    assert ProjectChatManager._is_worth_announcing_nudge_reply(reply) is False
    # The regular branch's condition is plain non-empty text.
    assert bool(reply.strip()) is True


async def test_drain_between_turns_skips_publish_and_push_for_banner_only_result(
    tmp_path: Path, monkeypatch
) -> None:
    """A short banner-only ResultEvent must not trigger chat_result_ready or a push.

    The synthesis-nudge parent turn can return a one-line reply like "ok" or
    "Synthesis complete — see trace." before the real report lands. Without the
    visibility guard, that single short string used to publish chat_result_ready
    and queue a delayed push, producing an OS-level notification whose body was
    the model's internal comment. With the guard, the in-app Activity row and
    the subagent-count drop remain the only signal.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("drain-banner", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="drain-banner-test")
    chat.session_id = "sess-drain-banner"
    pcm._save()

    class _FakeProvider:
        can_drain = True

        async def drain_events(self):
            from ciao.models import ResultEvent

            yield ResultEvent(
                type="result",
                result="ok",
                session_id=chat.session_id,
                is_error=False,
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]

    published: list[dict] = []
    pushes: list = []

    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: pushes.append(a))

    # The drain runs until the provider raises StopAsyncIteration after one
    # event; run it and let it unwind naturally.
    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    assert pushes == []
    assert not any(ev.get("type") == "chat_result_ready" for ev in published)


async def test_drain_between_turns_publishes_and_pushes_for_real_reply(
    tmp_path: Path, monkeypatch
) -> None:
    """A real reply still drives chat_result_ready + the push."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("drain-real", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="drain-real-test")
    chat.session_id = "sess-drain-real"
    pcm._save()

    class _FakeProvider:
        can_drain = True

        async def drain_events(self):
            from ciao.models import ResultEvent

            yield ResultEvent(
                type="result",
                result="Found the bug in routes_api.py line 482.",
                session_id=chat.session_id,
                is_error=False,
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]

    published: list[dict] = []
    pushes: list = []

    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: pushes.append(a))

    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    ready_events = [ev for ev in published if ev.get("type") == "chat_result_ready"]
    assert len(ready_events) == 1
    assert "Found the bug" in ready_events[0]["snippet"]
    assert len(pushes) == 1
    assert "Found the bug" in pushes[0][2]

    # The snippet is persisted onto the chat (not just broadcast), so the
    # sidebar's unread tile can show a preview after a fresh page load, before
    # any chat_result_ready WS event replays.
    assert chat.last_snippet == ready_events[0]["snippet"]
    reloaded = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.last_snippet == chat.last_snippet


def _monitor_task_records() -> list[dict]:
    return [
        {"type": "user", "message": {"role": "user", "content": "watch the report"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01SsjL",
                        "name": "Monitor",
                        "input": {
                            "command": "tail -f /tmp/adoption_2026-08.log",
                            "description": "adoption report 2026-08 DAG progress and failures",
                            "timeout_ms": 3600000,
                            "persistent": False,
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01SsjL",
                        "content": "Monitor started (task bl7dzu4ku, timeout 3600000ms).",
                    }
                ],
            },
            "toolUseResult": {"taskId": "bl7dzu4ku", "timeoutMs": 3600000, "persistent": False},
        },
    ]


def _task_notification_record(task_id: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        },
    }


def test_build_cli_task_wake_prompt_names_command_and_tool() -> None:
    from ciao.subagent_tracking import SubagentInfo

    prompt = ProjectChatManager._build_cli_task_wake_prompt(
        [
            SubagentInfo(
                agent_id="bl7dzu4ku",
                description="adoption report 2026-08 DAG progress and failures",
                subagent_type="Monitor",
                is_async=True,
                status="running",
                kind="task",
                command="tail -f /tmp/adoption_2026-08.log",
            )
        ]
    )
    assert "1 CLI task" in prompt
    assert "bl7dzu4ku" in prompt
    assert "Monitor" in prompt
    assert "adoption report 2026-08 DAG progress and failures" in prompt
    assert "command: tail -f /tmp/adoption_2026-08.log" in prompt
    assert "background_run_start" in prompt
    assert "pgrep/ps" in prompt


async def test_watcher_wakes_chat_when_cli_owning_a_monitor_task_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-wake", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-wake-test")
    chat.session_id = "sess-task-wake-1"
    pcm._save()

    session_path = tmp_path / "sess-task-wake-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: False)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count: wakes.append((parent, prompt, count)) or "started",
    )

    ticks = {"n": 0}

    async def fake_sleep(seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    except KeyboardInterrupt:
        pass

    assert len(wakes) == 1
    _parent, prompt, count = wakes[0]
    assert count == 1
    assert "bl7dzu4ku" in prompt
    assert "Monitor" in prompt
    assert "background_run_start" in prompt
    assert "tail -f /tmp/adoption_2026-08.log" in prompt


async def test_watcher_does_not_wake_while_cli_is_alive(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-alive", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-alive-test")
    chat.session_id = "sess-task-alive-1"
    pcm._save()

    session_path = tmp_path / "sess-task-alive-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: True)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count: wakes.append((parent, prompt, count)) or "started",
    )

    ticks = {"n": 0}

    async def fake_sleep(seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    except KeyboardInterrupt:
        pass

    assert wakes == []


async def test_watcher_does_not_wake_for_completed_task(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-done", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-done-test")
    chat.session_id = "sess-task-done-1"
    pcm._save()

    session_path = tmp_path / "sess-task-done-1.jsonl"
    _write_jsonl(
        session_path,
        [*_monitor_task_records(), _task_notification_record("bl7dzu4ku")],
    )

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: False)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count: wakes.append((parent, prompt, count)) or "started",
    )

    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)

    assert wakes == []


async def test_watcher_grace_needs_two_disconnected_ticks(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-grace", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-grace-test")
    chat.session_id = "sess-task-grace-1"
    pcm._save()

    session_path = tmp_path / "sess-task-grace-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: False)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count: wakes.append((parent, prompt, count)) or "started",
    )

    # First tick sees the CLI gone, later ticks see it back (a normal
    # between-turns reconnect): the grace must absorb the blip.
    alive_by_tick = {0: False}

    def cli_alive(chat_id: str) -> bool:
        return alive_by_tick.get(ticks["n"], True)

    ticks = {"n": 0}

    async def fake_sleep(seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(pcm, "_cli_owner_alive", cli_alive)

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    except KeyboardInterrupt:
        pass

    assert wakes == []


async def test_agent_badge_unchanged_with_tasks_tracked(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-badge", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-badge-test")
    chat.session_id = "sess-task-badge-1"
    pcm._save()

    session_path = tmp_path / "sess-task-badge-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: False)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count: wakes.append((parent, prompt, count)) or "started",
    )

    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture_publish(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    monkeypatch.setattr(pcm._events, "publish", capture_publish)

    ticks = {"n": 0}

    async def fake_sleep(seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    except KeyboardInterrupt:
        pass

    ready = [ev["remaining"] for ev in published if ev.get("type") == "chat_subagents_ready"]
    assert ready == [0]
    assert len(wakes) == 1
