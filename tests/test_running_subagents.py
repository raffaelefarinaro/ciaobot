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
from ciao.providers.opencode import OpencodeProvider
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


class _OpencodeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class _OpencodeClient:
    """A fake OpenCode server that records every path it is asked for."""

    def __init__(self) -> None:
        self.get_calls: list[str] = []

    async def get(self, path: str, *, params=None):
        self.get_calls.append(path)
        if path == "/api/session":
            return _OpencodeResponse({"data": [
                {"id": "ses_child_a", "parentID": "ses_parent", "title": "First"},
                {"id": "ses_child_b", "parentID": "ses_parent", "title": "Second"},
            ]})
        if path == "/api/session/active":
            return _OpencodeResponse({"data": {
                "ses_child_a": {"type": "running"},
            }})
        if path.endswith("/message"):
            return _OpencodeResponse({
                "data": [],
                "cursor": {"previous": None, "next": None},
            })
        return _OpencodeResponse({"data": {}})

    async def post(self, path: str, json=None):  # pragma: no cover - unused
        raise AssertionError(f"unexpected POST {path}")


def test_running_rows_do_not_read_transcripts(tmp_path: Path) -> None:
    """The sidebar poll needs a count, not a transcript (#1652).

    Reading every child's history here made a chat with many background
    agents re-read every transcript on every poll.
    """
    recorder = _OpencodeClient()
    provider = OpencodeProvider(tmp_path)
    provider._client = recorder  # type: ignore[assignment]
    provider._process = SimpleNamespace(returncode=None)
    provider._session_id = "ses_parent"

    chat = SimpleNamespace(
        chat_id="chat-1",
        session_id="ses_parent",
        provider="opencode",
        archived=False,
        user_turn_timings={},
    )
    pcm = SimpleNamespace(
        get_chat=lambda chat_id: chat if chat_id == "chat-1" else None,
        active_chat_ids=lambda: ["chat-1"],
        _providers={"chat-1": SimpleNamespace(provider=provider)},
        _agent_root_for_chat=lambda chat_id: None,
    )
    app = Starlette(
        routes=[Route("/api/subagents/running", running_subagents, methods=["GET"])]
    )
    app.state.project_chat_manager = pcm
    app.state.config = SimpleNamespace(workspace_root=tmp_path)

    payload = TestClient(app).get("/api/subagents/running").json()

    assert not [path for path in recorder.get_calls if path.endswith("/message")]
    assert [row["agent_id"] for row in payload["chats"]["chat-1"]] == ["ses_child_a"]


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


def test_steer_attachment_counts_as_a_user_turn(tmp_path: Path) -> None:
    """A mid-turn steer is a ``queued_command`` attachment, not a user record.

    It is still a user turn the transcript renders, so an agent dispatched
    after it must be attributed to the turn after the steer, and the
    queue-operation ``remove`` the CLI writes beside it adds nothing.
    """
    session = tmp_path / "sess-steer.jsonl"
    _write(session, [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        *_dispatch("toolu_1", "first", background=True, description="Before the steer"),
        {"type": "queue-operation", "operation": "enqueue", "content": "also this"},
        {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "prompt": "also this",
                "source_uuid": "steer-1",
                "commandMode": "prompt",
            },
        },
        {
            "type": "queue-operation",
            "operation": "remove",
            "reason": "absorbed_mid_turn",
            "commandUuid": "steer-1",
        },
        *_dispatch("toolu_2", "second", background=True, description="After the steer"),
    ])

    state = subagent_tracking.parse_session_subagents(session)

    assert state.subagents["first"].turn_index == 0
    assert state.subagents["second"].turn_index == 1


def test_late_steer_user_record_and_attachment_each_count_once(tmp_path: Path) -> None:
    """The late steer shape (a CLI follow-up turn) is a plain user record."""
    session = tmp_path / "sess-late.jsonl"
    _write(session, [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "prompt": "mid-turn steer",
                "commandMode": "prompt",
            },
        },
        {"type": "user", "message": {"role": "user", "content": "late steer"}},
        *_dispatch("toolu_1", "after", background=True, description="After both"),
    ])

    state = subagent_tracking.parse_session_subagents(session)

    assert state.subagents["after"].turn_index == 2


def _opencode_app(tmp_path: Path, provider: OpencodeProvider | None) -> TestClient:
    chat = SimpleNamespace(
        chat_id="chat-1",
        session_id="ses_parent",
        provider="opencode",
        archived=False,
        user_turn_timings={},
    )
    pcm = SimpleNamespace(
        get_chat=lambda chat_id: chat if chat_id == "chat-1" else None,
        active_chat_ids=lambda: ["chat-1"],
        _providers={"chat-1": SimpleNamespace(provider=provider)} if provider else {},
        _agent_root_for_chat=lambda chat_id: None,
    )
    app = Starlette(
        routes=[Route("/api/subagents/running", running_subagents, methods=["GET"])]
    )
    app.state.project_chat_manager = pcm
    app.state.config = SimpleNamespace(workspace_root=tmp_path)
    return TestClient(app)


def test_opencode_running_rows_include_background_shells(
    tmp_path: Path, monkeypatch
) -> None:
    provider = OpencodeProvider(tmp_path)
    provider._client = object()  # type: ignore[assignment]
    provider._process = SimpleNamespace(returncode=None)
    provider._session_id = "ses_parent"
    provider._background_shells = {"sh_a", "sh_b"}
    provider._shell_commands = {"sh_a": "sleep 20"}

    async def tree() -> list[dict]:
        return [{"info": {"id": "ses_child", "title": "Child"}, "active": True}]

    monkeypatch.setattr(provider, "read_live_collab_tree", tree)

    rows = _opencode_app(tmp_path, provider).get("/api/subagents/running").json()[
        "chats"
    ]["chat-1"]

    assert len(rows) == 3
    shells = [row for row in rows if row["subagent_type"] == "shell"]
    assert len(shells) == 2
    assert {row["agent_id"]: row["description"] for row in shells} == {
        "shell:sh_a": "sleep 20",
        "shell:sh_b": "Background command",
    }
    assert all(row["turn_index"] is None and row["status"] == "running" for row in shells)


def test_opencode_child_predicate_shared(tmp_path: Path, monkeypatch) -> None:
    from ciao.providers.opencode import opencode_collab_tree_counts

    cases = {
        "active": ({"info": {"id": "c"}, "active": True}, 1),
        "inactive": ({"info": {"id": "c"}, "active": False}, 0),
        "absent": ({"info": {"id": "c"}}, 1),
    }
    for name, (item, expected) in cases.items():
        async def read_tree(_root, _session, item=item) -> list[dict]:
            return [item]

        monkeypatch.setattr(
            OpencodeProvider, "read_collab_tree", staticmethod(read_tree)
        )
        rows = _opencode_app(tmp_path, None).get("/api/subagents/running").json()[
            "chats"
        ].get("chat-1", [])
        assert len(rows) == expected, name
        assert opencode_collab_tree_counts([item])[0] == expected, name
