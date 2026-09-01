"""Tests for the sidebar's running-subagent feed.

``/api/chats/{id}/subagents`` renders every subagent a chat ever spawned, which
is far too much to poll for a sidebar. ``/api/subagents/running`` answers the
narrower question — what is working right now — from dispatch metadata alone,
and only for the chats the manager already considers active.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import subagent_tracking
from ciao.web.routes_api import running_subagents


def _dispatch(tool_use_id: str, agent_id: str, *, background: bool, description: str) -> list[dict]:
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
                        "input": {
                            "description": description,
                            "subagent_type": "Explore",
                            "run_in_background": background,
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
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": []}
                ],
            },
            "toolUseResult": {"isAsync": background, "agentId": agent_id},
        },
    ]


def _completion(agent_id: str) -> dict:
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


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )


def _client(tmp_path: Path, chats: dict[str, object], active: list[str]) -> TestClient:
    pcm = SimpleNamespace(
        get_chat=lambda chat_id: chats.get(chat_id),
        active_chat_ids=lambda: active,
        _providers={},
        _agent_root_for_chat=lambda chat_id: None,
    )
    app = Starlette(
        routes=[Route("/api/subagents/running", running_subagents, methods=["GET"])]
    )
    app.state.project_chat_manager = pcm
    app.state.config = SimpleNamespace(workspace_root=tmp_path)
    return TestClient(app)


def _chat(chat_id: str, session_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=chat_id, session_id=session_id, provider="claude", archived=False
    )


def test_lists_the_agents_the_parent_session_knows_are_running(
    tmp_path: Path, monkeypatch
) -> None:
    """Named rows, not just a count — and only what the parent file can name.

    A foreground Task enters the parent JSONL only when its ``tool_result``
    lands, which *is* its completion, so it is already finished by then. The
    row set is therefore the background dispatches; foreground work shows in
    the chat's own live trace instead.
    """
    session = tmp_path / "sess-1.jsonl"
    _write(session, [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        *_dispatch("toolu_1", "bg", background=True, description="Sweep callers"),
        *_dispatch("toolu_2", "fg", background=False, description="Verify fix"),
    ])
    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session,
    )

    payload = _client(
        tmp_path, {"chat-1": _chat("chat-1", "sess-1")}, ["chat-1"]
    ).get("/api/subagents/running").json()

    rows = {row["agent_id"]: row for row in payload["chats"]["chat-1"]}
    assert set(rows) == {"bg"}
    assert rows["bg"]["is_async"] is True
    assert rows["bg"]["description"] == "Sweep callers"
    assert rows["bg"]["subagent_type"] == "Explore"
    assert rows["bg"]["status"] == "running"


def test_finished_agents_leave_no_row(tmp_path: Path, monkeypatch) -> None:
    """A completed agent is not archived and leaves nothing behind: omitting the
    chat entirely is how the client drops its rows."""
    session = tmp_path / "sess-2.jsonl"
    _write(session, [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        *_dispatch("toolu_1", "done", background=True, description="Sweep"),
        _completion("done"),
    ])
    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session,
    )

    payload = _client(
        tmp_path, {"chat-1": _chat("chat-1", "sess-2")}, ["chat-1"]
    ).get("/api/subagents/running").json()

    assert payload == {"chats": {}}


def test_only_active_chats_are_scanned(tmp_path: Path, monkeypatch) -> None:
    """One JSONL parse per working chat, not per registry row."""
    session = tmp_path / "sess-3.jsonl"
    _write(session, [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        *_dispatch("toolu_1", "x", background=True, description="Sweep"),
    ])
    scanned: list[str] = []

    def fake_find(session_id, workspace_root, *, agent_root=None, force_refresh=False):
        scanned.append(session_id)
        return session

    monkeypatch.setattr(subagent_tracking, "find_parent_session_file", fake_find)

    chats = {
        "chat-active": _chat("chat-active", "sess-3"),
        "chat-idle": _chat("chat-idle", "sess-4"),
    }
    payload = _client(tmp_path, chats, ["chat-active"]).get(
        "/api/subagents/running"
    ).json()

    assert scanned == ["sess-3"]
    assert list(payload["chats"]) == ["chat-active"]


def test_a_chat_without_a_session_is_skipped(tmp_path: Path) -> None:
    """A brand-new chat has no transcript to parse."""
    chats = {"chat-1": _chat("chat-1", "")}
    payload = _client(tmp_path, chats, ["chat-1"]).get(
        "/api/subagents/running"
    ).json()
    assert payload == {"chats": {}}


def test_one_unreadable_session_does_not_blank_the_sidebar(
    tmp_path: Path, monkeypatch
) -> None:
    """A parse failure on one chat must not take the other chats' rows with it."""
    good = tmp_path / "sess-good.jsonl"
    _write(good, [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        *_dispatch("toolu_1", "ok", background=True, description="Sweep"),
    ])

    def fake_find(session_id, workspace_root, *, agent_root=None, force_refresh=False):
        if session_id == "sess-bad":
            raise OSError("session file exploded")
        return good

    monkeypatch.setattr(subagent_tracking, "find_parent_session_file", fake_find)

    chats = {
        "chat-bad": _chat("chat-bad", "sess-bad"),
        "chat-good": _chat("chat-good", "sess-good"),
    }
    payload = _client(tmp_path, chats, ["chat-bad", "chat-good"]).get(
        "/api/subagents/running"
    ).json()

    assert list(payload["chats"]) == ["chat-good"]


def test_running_agents_drops_an_agent_whose_transcript_went_idle(
    tmp_path: Path,
) -> None:
    """The status-only view would pin a killed dispatch at "running" forever.

    A parent turn interrupted mid-dispatch never writes the ``tool_result`` (or
    the ``<task-notification>``) that moves the status off "running", so the
    idle-transcript fallback is what keeps a dead row out of the sidebar.
    """
    parent = tmp_path / "parent.jsonl"
    _write(parent, [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        *_dispatch("toolu_1", "stale", background=True, description="Sweep"),
    ])
    state = subagent_tracking.parse_session_subagents(parent)
    assert state.subagents["stale"].status == "running"

    # Its own transcript, last touched long ago and ending on a final answer.
    agent_path = subagent_tracking.subagent_transcript_path(parent, "stale")
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    _write(agent_path, [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        }
    ])
    old = time.time() - (subagent_tracking.FINISHED_AGENT_IDLE_SECONDS + 30)
    import os

    os.utime(agent_path, (old, old))

    assert subagent_tracking.running_agents(parent, state) == []
