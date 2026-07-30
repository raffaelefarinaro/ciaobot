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
        lambda session_id, workspace_root: session_path,
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
        lambda session_id, workspace_root: session_path,
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
    assert len(ready_events) == 2
    assert ready_events[0]["remaining"] == 1
    assert ready_events[0]["nudged"] is False
    assert ready_events[1]["remaining"] == 0
    assert ready_events[1]["nudged"] is True


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
        lambda session_id, workspace_root: session_path,
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
        lambda session_id, workspace_root: session_path,
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
        lambda session_id, workspace_root: session_path,
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
