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
from ciao.providers.opencode import OpencodeProvider
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
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session_path,
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
    # The watcher publishes only when the count changes, so the final zero
    # is published once.
    assert len(ready_events) == 3
    assert ready_events[0]["remaining"] == 2
    assert ready_events[1]["remaining"] == 1
    assert ready_events[2]["remaining"] == 0
    assert ready_events[0]["chat_id"] == chat.chat_id
    assert ready_events[0]["project_id"] == project.project_id
    # The last-count cache is cleared once the watcher exits so the events
    # snapshot doesn't advertise a stale count.
    assert pcm.background_agent_counts == {}


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
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session_path,
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
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session_path,
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


def test_agent_id_falls_back_when_the_sdk_read_is_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An empty SDK read must not suppress the local-transcript fallback.

    On installs whose CLI writes the nested subagents/ layout the SDK helpers
    return [] instead of raising, so the narrowed request -- which skips
    discovery and reads the id directly -- used to answer with a row carrying
    no messages, and the read-only subagent view rendered an empty transcript.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def _get_subagent_messages(
        session_id: str, agent_id: str, directory: str | None = None
    ) -> list:
        return []

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            list_subagents=lambda *a, **k: [],
            get_subagent_messages=_get_subagent_messages,
        ),
    )
    _write_two_agent_session(tmp_path)

    resp = _client(tmp_path, "sess-1").get(
        "/api/chats/chat-1/subagents", params={"agent_id": "researcher"}
    )

    assert resp.status_code == 200
    entries = resp.json()
    assert [entry["agent_id"] for entry in entries] == ["researcher"]
    assert entries[0]["messages"], "the local transcript should have filled the row"


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


# --- opencode: the poll path must not read transcripts (#1652) ---------------
#
# The live collab tree re-read every child's history on every poll. The list
# endpoint is polled every few seconds by the in-chat panel, so a chat with
# many background agents made the engine re-read every transcript every few
# seconds. Only the detail view (``?agent_id=``) needs a transcript.


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
            return _OpencodeResponse({"data": {}})
        if path.endswith("/message"):
            return _OpencodeResponse({
                "data": [],
                "cursor": {"previous": None, "next": None},
            })
        return _OpencodeResponse({"data": {}})

    async def post(self, path: str, json=None):  # pragma: no cover - unused
        raise AssertionError(f"unexpected POST {path}")


def _opencode_client(tmp_path: Path) -> tuple[TestClient, _OpencodeClient]:
    """A client whose chat is an opencode chat with a live server attached."""
    recorder = _OpencodeClient()
    provider = OpencodeProvider(tmp_path)
    provider._client = recorder  # type: ignore[assignment]
    provider._process = SimpleNamespace(returncode=None)
    provider._session_id = "ses_parent"

    chat = SimpleNamespace(
        session_id="ses_parent",
        provider="opencode",
        chat_id="chat-1",
        user_turn_timings={},
    )
    pcm = SimpleNamespace(
        get_chat=lambda chat_id: chat if chat_id == "chat-1" else None,
        _providers={"chat-1": SimpleNamespace(provider=provider)},
        _agent_root_for_chat=lambda chat_id: None,
    )
    app = Starlette(routes=[Route("/api/chats/{chat_id}/subagents", chat_subagents, methods=["GET"])])
    app.state.project_chat_manager = pcm
    app.state.config = SimpleNamespace(workspace_root=tmp_path / "workspace")
    return TestClient(app), recorder


def test_opencode_subagent_list_reads_no_transcripts(tmp_path: Path) -> None:
    """The unfiltered list is metadata only: no child's history is read."""
    client, recorder = _opencode_client(tmp_path)

    resp = client.get("/api/chats/chat-1/subagents")

    assert resp.status_code == 200
    assert not [path for path in recorder.get_calls if path.endswith("/message")]
    assert sorted(entry["agent_id"] for entry in resp.json()) == [
        "ses_child_a",
        "ses_child_b",
    ]


def test_opencode_subagent_detail_reads_only_that_child(tmp_path: Path) -> None:
    """The detail view reads one transcript, not every sibling's."""
    client, recorder = _opencode_client(tmp_path)

    resp = client.get(
        "/api/chats/chat-1/subagents", params={"agent_id": "ses_child_b"}
    )

    assert resp.status_code == 200
    message_reads = [path for path in recorder.get_calls if path.endswith("/message")]
    assert message_reads == ["/api/session/ses_child_b/message"]
    rows = resp.json()
    assert [entry["agent_id"] for entry in rows] == ["ses_child_b"]


def test_opencode_live_child_status_is_not_inferred_from_historical_rows(
    tmp_path: Path,
) -> None:
    """``/session/active`` decides which child is running; history alone does not.

    A child the live server reports active is running. A child with an open
    assistant message (no ``completed`` time) but absent from the active map of
    the live server is a historical row and must read as completed (#1664).
    """
    client, recorder = _opencode_client(tmp_path)
    original_get = recorder.get

    async def get(path: str, *, params=None):
        if path == "/api/session/active":
            return _OpencodeResponse({"data": {"ses_child_a": {"type": "running"}}})
        if path == "/api/session/ses_child_b/message":
            return _OpencodeResponse({
                "data": [{"info": {"role": "assistant", "time": {"created": 1}}, "parts": []}],
                "cursor": {"previous": None, "next": None},
            })
        return await original_get(path, params=params)

    recorder.get = get  # type: ignore[method-assign]

    rows = {
        row["agent_id"]: row for row in client.get("/api/chats/chat-1/subagents").json()
    }

    assert rows["ses_child_a"]["status"] == "running"
    assert rows["ses_child_b"]["status"] == "completed"
    # Metadata-only list read: no child history was fetched to decide this.
    assert not [path for path in recorder.get_calls if path.endswith("/message")]


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
def test_follow_up_announcement_rejects_empty_and_banner_only(text: str) -> None:
    """A bare banner or whitespace must not be treated as a real reply."""
    assert ProjectChatManager._is_worth_announcing_follow_up(text) is False


def test_follow_up_announcement_accepts_real_reply() -> None:
    """Anything with at least a few characters of real prose passes."""
    assert ProjectChatManager._is_worth_announcing_follow_up(
        "Synthesis complete — see the trace."
    ) is True
    assert ProjectChatManager._is_worth_announcing_follow_up(
        "Found the bug in routes_api.py"
    ) is True


@pytest.mark.parametrize("reply", ["Yes", "No", "No.", "Ok", "42", "👍"])
def test_a_terse_answer_to_the_user_still_announces(reply: str) -> None:
    """The banner floor must never reach a reply the user asked for.

    "Yes" is a complete answer to a yes/no question. Gating the regular
    turn-done branch on the follow-up heuristic dropped its unread badge, its
    toast, the ``last_activity_at`` bump that reorders recents, and the
    pending-retry clear, with nothing anywhere reporting a problem.
    """
    # The follow-up heuristic does reject it, which is why it must stay off this path.
    assert ProjectChatManager._is_worth_announcing_follow_up(reply) is False
    # The regular branch's condition is plain non-empty text.
    assert bool(reply.strip()) is True


async def test_drain_between_turns_skips_publish_and_push_for_banner_only_result(
    tmp_path: Path, monkeypatch
) -> None:
    """A short banner-only ResultEvent must not trigger chat_result_ready or a push.

    A follow-up parent turn can return a one-line reply like "ok" or
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
        background_work_outstanding = False

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
        background_work_outstanding = False

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


def _drain_chat(pcm: ProjectChatManager, name: str):
    project = pcm.create_project(name, workspace="personal")
    chat = pcm.create_chat(project.project_id, title=name)
    chat.session_id = f"sess-{name}"
    pcm._save()
    return project, chat


async def test_drain_announces_only_the_follow_up_final_segment(
    tmp_path: Path, monkeypatch
) -> None:
    """A follow-up execution previews its last text block, not the joined turn.

    OpenCode's wake result joins every part of the execution; the unread card
    and push must excerpt the block the chat ends on, while the durable
    handover keeps the whole execution text (#2139).
    """
    from ciao.models import AssistantTextDelta, ResultEvent, ToolUseEvent

    pcm = _make_manager(tmp_path)
    project, chat = _drain_chat(pcm, "drain-segment")
    interim = "Interim working narration."
    final = "Final answer in file.md."
    whole = f"{interim}\n\n{final}"

    class _FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            yield AssistantTextDelta(type="text", text=interim)
            yield ToolUseEvent(type="assistant", tool_name="Bash", tool_input="ls")
            yield AssistantTextDelta(type="text", text=final)
            yield ResultEvent(
                type="result",
                result=whole,
                session_id=chat.session_id,
                is_error=False,
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]
    published: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: None)

    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    ready = [ev for ev in published if ev.get("type") == "chat_result_ready"]
    assert len(ready) == 1
    assert ready[0]["snippet"] == "Final answer in file.md."
    assert chat.last_snippet == "Final answer in file.md."
    # The complete execution text still reaches the handover and the schedule
    # classifier, which judges the finished synthesis rather than a preview.
    assert chat.last_response == whole
    assert pcm._last_drain_result[chat.chat_id] == (whole, False)


async def test_drain_uses_provider_final_text_without_text_events(
    tmp_path: Path, monkeypatch
) -> None:
    """A wake attached after the text frames still names its final block."""
    from ciao.models import ResultEvent

    pcm = _make_manager(tmp_path)
    project, chat = _drain_chat(pcm, "drain-metadata")
    whole = "Interim working narration.\n\nFinal answer in file.md."

    class _FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            yield ResultEvent(
                type="result",
                result=whole,
                session_id=chat.session_id,
                is_error=False,
                final_text="Final answer in file.md.",
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]
    published: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: None)

    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    ready = [ev for ev in published if ev.get("type") == "chat_result_ready"]
    assert [ev["snippet"] for ev in ready] == ["Final answer in file.md."]
    assert pcm._last_drain_result[chat.chat_id] == (whole, False)


async def test_drain_resets_the_final_segment_for_each_result(
    tmp_path: Path, monkeypatch
) -> None:
    """A second wake's answer is its own block, not the previous one plus it."""
    from ciao.models import AssistantTextDelta, ResultEvent

    pcm = _make_manager(tmp_path)
    project, chat = _drain_chat(pcm, "drain-reset")

    class _FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            yield AssistantTextDelta(type="text", text="First answer.")
            yield ResultEvent(
                type="result",
                result="First answer.",
                session_id=chat.session_id,
                is_error=False,
            )
            yield AssistantTextDelta(type="text", text="Second answer.")
            yield ResultEvent(
                type="result",
                result="Second answer.",
                session_id=chat.session_id,
                is_error=False,
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]
    published: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: None)

    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    ready = [ev for ev in published if ev.get("type") == "chat_result_ready"]
    assert [ev["snippet"] for ev in ready] == ["First answer.", "Second answer."]


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
    assert "ciao run start" in prompt
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
        lambda *args, **kwargs: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: False)

    wakes: list[tuple[object, str, int, str]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count, origin)) or "started",
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
    _parent, prompt, count, origin = wakes[0]
    assert count == 1
    assert "bl7dzu4ku" in prompt
    assert "Monitor" in prompt
    assert "ciao run start" in prompt
    assert "tail -f /tmp/adoption_2026-08.log" in prompt
    assert origin == "cli_task_wake"


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
        lambda *args, **kwargs: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: True)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
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
        lambda *args, **kwargs: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: False)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
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
        lambda *args, **kwargs: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: False)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
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
        lambda *args, **kwargs: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: False)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
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


async def test_sweep_wakes_chat_with_running_cli_task_after_restart(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-sweep", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-sweep-test")
    chat.session_id = "sess-task-sweep-1"
    pcm._save()

    session_path = tmp_path / "sess-task-sweep-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda *args, **kwargs: session_path,
    )

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
    )

    async def fake_sleep(seconds: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    swept = pcm.sweep_orphaned_cli_tasks()
    assert swept == 1

    # Let the deferred wake task run out its (patched) sleep.
    await asyncio.sleep(0)
    pending = [
        t for t in list(asyncio.all_tasks())
        if t is not asyncio.current_task()
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    assert len(wakes) == 1
    _parent, prompt, count = wakes[0]
    assert count == 1
    assert prompt.startswith(subagent_tracking.CLI_TASK_WAKE_PREFIX)
    assert "bl7dzu4ku" in prompt


async def test_sweep_skips_task_already_woken(tmp_path: Path, monkeypatch) -> None:
    from ciao.subagent_tracking import CLI_TASK_WAKE_PREFIX

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-swept", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-swept-test")
    chat.session_id = "sess-task-swept-1"
    pcm._save()

    session_path = tmp_path / "sess-task-swept-1.jsonl"
    _write_jsonl(
        session_path,
        [
            *_monitor_task_records(),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        CLI_TASK_WAKE_PREFIX
                        + " 1 CLI task you started (Monitor / background shell) "
                        "were lost.\n\n— Monitor: adoption report (task bl7dzu4ku)"
                    ),
                },
            },
        ],
    )

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda *args, **kwargs: session_path,
    )

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
    )

    assert pcm.sweep_orphaned_cli_tasks() == 0
    assert wakes == []


async def test_sweep_skips_archived_and_non_claude_chats(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-skip", workspace="personal")
    archived = pcm.create_chat(project.project_id, title="task-archived")
    archived.session_id = "sess-task-archived-1"
    archived.archived = True
    opencode_chat = pcm.create_chat(project.project_id, title="task-opencode")
    opencode_chat.session_id = "sess-task-opencode-1"
    opencode_chat.provider = "opencode"
    claude_chat = pcm.create_chat(project.project_id, title="task-no-session")
    claude_chat.session_id = ""
    pcm._save()

    session_path = tmp_path / "sess-task-archived-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda *args, **kwargs: session_path,
    )

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
    )

    assert pcm.sweep_orphaned_cli_tasks() == 0
    assert wakes == []


async def test_sweep_skips_chats_idle_for_more_than_a_week(
    tmp_path: Path, monkeypatch
) -> None:
    from datetime import UTC, datetime, timedelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-stale", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-stale-test")
    chat.session_id = "sess-task-stale-1"
    chat.last_activity_at = (
        datetime.now(UTC) - timedelta(days=8)
    ).isoformat().replace("+00:00", "Z")
    pcm._save()

    session_path = tmp_path / "sess-task-stale-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda *args, **kwargs: session_path,
    )

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
    )

    assert pcm.sweep_orphaned_cli_tasks() == 0
    assert wakes == []


async def test_sweep_wakes_recently_active_chat(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-fresh", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-fresh-test")
    chat.session_id = "sess-task-fresh-1"
    chat.last_activity_at = (
        datetime.now(UTC) - timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    pcm._save()

    session_path = tmp_path / "sess-task-fresh-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda *args, **kwargs: session_path,
    )

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
    )

    async def fake_sleep(seconds: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    assert pcm.sweep_orphaned_cli_tasks() == 1

    await asyncio.sleep(0)
    pending = [
        t for t in list(asyncio.all_tasks())
        if t is not asyncio.current_task()
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    assert len(wakes) == 1


async def test_task_only_watcher_exits_on_restart_drain(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-drain", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-drain-test")
    chat.session_id = "sess-task-drain-1"
    pcm._save()

    session_path = tmp_path / "sess-task-drain-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda *args, **kwargs: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: True)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
    )

    async def fail_sleep(seconds: float) -> None:
        raise AssertionError("task-only watcher must exit when a restart drains")

    monkeypatch.setattr(asyncio, "sleep", fail_sleep)

    pcm.begin_restart_drain()

    await asyncio.wait_for(
        pcm._watch_subagent_completion(chat.chat_id, project.project_id),
        timeout=5,
    )

    assert wakes == []
    assert chat.chat_id not in pcm.active_chat_ids()


async def test_cancelled_drain_wakes_dead_cli_task(
    tmp_path: Path, monkeypatch
) -> None:
    """A cancelled drain has no boot to sweep up after it, so it wakes the chat.

    The task-only watcher above exits on a drain on the promise of
    ``sweep_orphaned_cli_tasks`` at the next start. A cancelled drain has no next
    start, so the chat that left a Monitor running has to be woken here or the
    task is never reported.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-cancel", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-cancel-test")
    chat.session_id = "sess-task-cancel-1"
    pcm._save()

    session_path = tmp_path / "sess-task-cancel-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda *args, **kwargs: session_path,
    )

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
    )

    pcm.begin_restart_drain()
    # The watcher gives up on the task while the drain is up, waking nothing.
    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    assert wakes == []
    assert chat.chat_id not in pcm.active_chat_ids()

    pcm.cancel_restart_drain()

    assert len(wakes) == 1
    parent, prompt, count = wakes[0]
    assert parent is chat
    assert count == 1
    assert prompt.startswith(subagent_tracking.CLI_TASK_WAKE_PREFIX)
    assert "bl7dzu4ku" in prompt


async def test_cancelled_drain_does_not_wake_live_cli_owner(
    tmp_path: Path, monkeypatch
) -> None:
    """The replay must not talk over a CLI that is still alive.

    Same setup as ``test_cancelled_drain_wakes_dead_cli_task`` with the owner
    alive, and the same expectation of no wake — the CLI itself still answers
    its task notifications, so a replay that woke it would interleave a turn
    into one the user is already having. The drain replay and the boot sweep
    both reach their wakes through ``_cli_owner_alive``, so this pins the
    guard both paths share.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-live", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-live-test")
    chat.session_id = "sess-task-live-1"
    pcm._save()

    session_path = tmp_path / "sess-task-live-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda *args, **kwargs: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: True)

    wakes: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        pcm,
        "_deliver_wake",
        lambda parent, prompt, *, count, origin="": wakes.append((parent, prompt, count)) or "started",
    )

    pcm.begin_restart_drain()
    # The watcher gives up on the task while the drain is up, waking nothing.
    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    assert wakes == []
    assert chat.chat_id not in pcm.active_chat_ids()

    pcm.cancel_restart_drain()

    # A live owner handles the notification itself; the replay stays out.
    assert wakes == []


async def test_failed_wake_is_not_rearmed(tmp_path: Path, monkeypatch) -> None:
    """A wake that never reaches the JSONL must not re-arm forever."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("task-rearm", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="task-rearm-test")
    chat.session_id = "sess-task-rearm-1"
    pcm._save()

    session_path = tmp_path / "sess-task-rearm-1.jsonl"
    _write_jsonl(session_path, _monitor_task_records())

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda *args, **kwargs: session_path,
    )
    monkeypatch.setattr(pcm, "_cli_owner_alive", lambda chat_id: False)

    calls: list[tuple[object, str, int]] = []

    def failing_wake(parent, prompt, *, count, origin=""):
        calls.append((parent, prompt, count))
        # Deliberately writes nothing to the JSONL: the wake turn never
        # persists, so nothing marks the task lost.
        return "queued"

    monkeypatch.setattr(pcm, "_deliver_wake", failing_wake)

    async def fake_sleep(seconds: float) -> None:
        return

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # First run: the watcher fires the wake.
    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    assert len(calls) == 1

    # A failed stream's cleanup would start a new watcher: it must not
    # deliver again within the same process.
    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    assert len(calls) == 1

    # The startup sweep after a crash must also skip the already-woken task.
    assert pcm.sweep_orphaned_cli_tasks() == 0
    assert len(calls) == 1


# ── the parked result announce ───────────────────────────────────────────────
#
# The announce for a turn that ends on "I'll report back" is PARKED, not
# dropped. It is released once: by the follow-up result at count zero, by the
# watcher when it saw no background work or no drain is live, by the drain
# ending, or by Stop.


def _announce_spy(pcm) -> list[tuple]:
    """Record announces, and keep detached work from reaching the real vault.

    Only the archive-proposal helper and the memory-pass settle are swallowed —
    everything else spawned this way is scheduled for real, or a stub here would
    silently disable the behaviour under test. The memory-pass hook needs the same
    swallow: in a sync test there is no loop for its detached task.
    """
    calls: list[tuple] = []
    pcm._announce_result_ready = lambda *a: calls.append(a)  # type: ignore[method-assign]

    def spawn(coro, name: str):
        if name.startswith("archive-proposal-helper") or name.startswith(
            "memory-pass-"
        ):
            coro.close()
            return None
        return asyncio.create_task(coro, name=name)

    pcm._spawn_detached = spawn  # type: ignore[method-assign]
    return calls


def test_a_parked_announce_is_released_when_no_follow_up_speaks(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    pcm._park_result_announce("chat-1", "proj-1", "Title", "snippet")
    assert calls == []

    assert pcm._flush_result_announce("chat-1") is True
    assert calls == [("chat-1", "proj-1", "Title", "snippet")]


def test_flushing_is_idempotent(tmp_path: Path) -> None:
    """The watcher's `finally` can run after an explicit flush on the same path."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    pcm._park_result_announce("chat-1", "proj-1", "Title", "snippet")
    assert pcm._flush_result_announce("chat-1") is True
    assert pcm._flush_result_announce("chat-1") is False
    assert len(calls) == 1


def test_a_landed_follow_up_discards_the_parked_announce(tmp_path: Path) -> None:
    """Otherwise the turn pushes twice: once here, once for the follow-up reply."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    pcm._park_result_announce("chat-1", "proj-1", "Title", "snippet")
    pcm._discard_result_announce("chat-1")
    assert pcm._flush_result_announce("chat-1") is False
    assert calls == []


@pytest.mark.asyncio
async def test_watcher_releases_the_announce_when_it_cannot_find_the_session(
    tmp_path: Path,
) -> None:
    """The failure #430 was filed for.

    `find_parent_session_file` returning None makes the watcher return before
    its loop ever runs, so no follow-up can ever be awaited. The old predicate could not
    see that: it dropped the announce and the chat completed in total silence —
    no push, no unread badge, no archive proposal, and nothing logged.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Runs", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Owner")
    chat.session_id = "session-with-no-file-on-disk"
    calls = _announce_spy(pcm)

    pcm._park_result_announce(chat.chat_id, project.project_id, "Title", "snippet")
    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)

    assert calls == [(chat.chat_id, project.project_id, "Title", "snippet")]


@pytest.mark.asyncio
async def test_watcher_releases_the_announce_when_it_raises(tmp_path: Path) -> None:
    """An exception is just another path where no follow-up will speak."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    async def boom(
        chat_id: str, project_id: str, saw_work: list[bool]
    ) -> None:
        raise RuntimeError("watcher blew up")

    pcm._watch_subagent_completion_inner = boom  # type: ignore[method-assign]
    pcm._park_result_announce("chat-1", "proj-1", "Title", "snippet")

    with pytest.raises(RuntimeError):
        await pcm._watch_subagent_completion("chat-1", "proj-1")

    assert calls == [("chat-1", "proj-1", "Title", "snippet")]


@pytest.mark.asyncio
async def test_a_superseded_watcher_does_not_release_the_next_turns_announce(
    tmp_path: Path,
) -> None:
    """`_start_subagent_watcher` cancels and replaces; cancellation is async.

    So a superseded watcher's `finally` can run *after* the next turn has
    parked its own announce. Flushing there would push a result while its
    follow-up turn was still pending, which is the double-push the parking is
    meant to avoid.
    """
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    started = asyncio.Event()

    async def blocks(
        chat_id: str, project_id: str, saw_work: list[bool]
    ) -> None:
        started.set()
        await asyncio.sleep(3600)

    pcm._watch_subagent_completion_inner = blocks  # type: ignore[method-assign]
    old = asyncio.create_task(pcm._watch_subagent_completion("chat-1", "proj-1"))
    pcm._pending_subagent_watchers["chat-1"] = old
    await started.wait()

    # A new turn supersedes it and parks its own announce before the cancelled
    # task's finally has had a chance to run.
    newer = asyncio.create_task(asyncio.sleep(3600))
    pcm._pending_subagent_watchers["chat-1"] = newer
    old.cancel()
    pcm._park_result_announce("chat-1", "proj-1", "Newer", "newer snippet")
    with pytest.raises(asyncio.CancelledError):
        await old

    assert calls == []
    newer.cancel()

    # The registered watcher still owns the release.
    assert pcm._flush_result_announce("chat-1") is True
    assert calls == [("chat-1", "proj-1", "Newer", "newer snippet")]


def test_the_interim_pattern_still_decides_whether_to_park() -> None:
    """Parking is gated on the prose heuristic, which is safe to be broad:
    a false positive only defers the announce to the watcher, which always
    releases it. A real result is announced immediately."""
    from ciao.web.project_chats import ProjectChatManager

    assert ProjectChatManager._is_interim_subagent_text(
        "Dispatched three agents; I'll report back once they finish."
    )
    assert not ProjectChatManager._is_interim_subagent_text(
        "Done — all three agents reported; the changelog is updated."
    )


# ── the drain releases the park at count zero ────────────────────────────────


class _FakeProvider:
    """Drives `_drain_between_turns` with a scripted event sequence."""

    can_drain = True

    background_work_outstanding = False

    def __init__(self, events: list) -> None:
        self._events = events

    async def drain_events(self):
        for event in self._events:
            yield event


def _result_event(text: str, *, is_error: bool = False):
    from ciao.models import ResultEvent

    return ResultEvent(type="result", result=text, is_error=is_error)


@pytest.mark.asyncio
async def test_the_drain_announces_the_synthesis_reply_and_drops_the_park(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._providers["chat-1"] = _FakeProvider([
        _result_event("All three agents reported. The changelog is updated."),
    ])

    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")
    await pcm._drain_between_turns("chat-1", "proj-1")

    # Exactly one announce, and it carries the synthesis reply — not the
    # parked "I'll report back" snippet.
    assert len(calls) == 1
    assert calls[0][3] != "interim snippet"


@pytest.mark.asyncio
async def test_a_follow_up_that_errors_still_releases_the_park(
    tmp_path: Path,
) -> None:
    """The drain only announces a non-error, non-stub follow-up.

    An errored follow-up must not leave the chat with no notification at all,
    so the drain flushes the parked announce instead.
    """
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._providers["chat-1"] = _FakeProvider([_result_event("boom", is_error=True)])

    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")
    await pcm._drain_between_turns("chat-1", "proj-1")

    assert calls == [("chat-1", "proj-1", "Title", "interim snippet")]


@pytest.mark.asyncio
async def test_a_banner_only_follow_up_still_releases_the_park(
    tmp_path: Path,
) -> None:
    """`_is_worth_announcing_follow_up` rejects a short stub, so the drain
    stays silent — which means the parked announce has to go out."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._providers["chat-1"] = _FakeProvider([_result_event("ok.")])
    assert pcm._is_worth_announcing_follow_up("ok.") is False

    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")
    await pcm._drain_between_turns("chat-1", "proj-1")

    assert calls == [("chat-1", "proj-1", "Title", "interim snippet")]


@pytest.mark.asyncio
async def test_a_drain_that_never_gets_a_reply_still_releases_the_park(
    tmp_path: Path,
) -> None:
    """The CLI can drop after the steer and never produce a ResultEvent."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._providers["chat-1"] = _FakeProvider([])

    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")
    await pcm._drain_between_turns("chat-1", "proj-1")

    assert calls == [("chat-1", "proj-1", "Title", "interim snippet")]


@pytest.mark.asyncio
async def test_an_empty_follow_up_result_is_skipped(tmp_path: Path) -> None:
    """The CLI's `turns=0` bookkeeping result opens no stream and records nothing."""
    from ciao.models import ResultEvent

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    pcm._events.publish = capture  # type: ignore[method-assign]
    pcm._providers["chat-1"] = _FakeProvider(
        [ResultEvent(type="result", result="", origin="task-notification")]
    )

    await pcm._drain_between_turns("chat-1", "proj-1")

    types = {ev.get("type") for ev in published}
    assert "chat_streaming_started" not in types
    assert "chat_streaming_done" not in types
    assert "chat-1" not in pcm._last_drain_result
    assert calls == []


@pytest.mark.asyncio
async def test_follow_ups_announce_once_when_the_count_reaches_zero(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    counts = iter([1, 0])
    monkeypatch.setattr(pcm, "_background_count_now", lambda chat_id: next(counts))
    pcm._providers["chat-1"] = _FakeProvider([
        _result_event("Agent A reported: the parser is fine."),
        _result_event("Both agents reported. The bug is in routes_api.py."),
    ])

    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")
    await pcm._drain_between_turns("chat-1", "proj-1")

    assert len(calls) == 1
    assert "Both agents reported" in calls[0][3]
    assert "chat-1" not in pcm._parked_result_announce
    assert pcm._last_drain_result["chat-1"] == (
        "Both agents reported. The bug is in routes_api.py.",
        False,
    )


@pytest.mark.asyncio
async def test_a_follow_up_while_agents_still_run_replaces_the_park(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    monkeypatch.setattr(pcm, "_background_count_now", lambda chat_id: 1)

    class _OneThenBlock:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            yield _result_event("Agent A reported: the parser is fine.")
            await asyncio.Event().wait()

    pcm._providers["chat-1"] = _OneThenBlock()
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")

    task = asyncio.create_task(pcm._drain_between_turns("chat-1", "proj-1"))
    for _ in range(20):
        await asyncio.sleep(0)

    assert calls == []
    assert pcm._parked_result_announce["chat-1"][2].startswith("Agent A reported")
    assert "chat-1" not in pcm._last_drain_result

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_cancelled_drain_leaves_the_park_to_the_next_turn(
    tmp_path: Path,
) -> None:
    """The next user turn cancels the drain and supersedes the interim
    announce, so releasing it here would push a stale non-answer."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    class _Blocking:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            await asyncio.sleep(3600)
            yield  # pragma: no cover

    pcm._providers["chat-1"] = _Blocking()
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")

    task = asyncio.create_task(pcm._drain_between_turns("chat-1", "proj-1"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == []
    assert "chat-1" in pcm._parked_result_announce


@pytest.mark.asyncio
async def test_watcher_leaves_the_park_to_a_live_drain_after_seeing_work(
    tmp_path: Path, monkeypatch
) -> None:
    """A watcher that saw background work must not flush while a drain is live.

    Each finished task brings a native follow-up turn, and the drain announces
    the one that lands at count zero. Flushing here would push the interim
    text and then the real reply: two pushes. The park has to survive the
    watcher and be released (or discarded) by the drain.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("follow-up-handoff", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="follow-up-handoff-test")
    chat.session_id = "sess-follow-up-handoff"
    pcm._save()

    session_path = tmp_path / "sess-follow-up-handoff.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "kick off work"}},
        *_dispatch_records("toolu_h", "agent-h"),
    ]
    completions = iter([_completion_record("agent-h")])

    def flush() -> None:
        session_path.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    flush()

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session_path,
    )

    async def fake_sleep(seconds: float) -> None:
        record = next(completions, None)
        if record is not None:
            records.append(record)
            flush()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class FakeProvider:
        can_drain = True
        background_work_outstanding = False

    pcm._providers[chat.chat_id] = FakeProvider()  # type: ignore[assignment]
    running_drain = asyncio.get_running_loop().create_future()
    pcm._between_turn_drains[chat.chat_id] = running_drain  # type: ignore[assignment]

    calls = _announce_spy(pcm)
    pcm._park_result_announce(chat.chat_id, project.project_id, "Title", "interim")

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    finally:
        running_drain.cancel()

    # Not announced here — the drain owns it — and, crucially, not dropped.
    assert calls == []
    assert chat.chat_id in pcm._parked_result_announce


@pytest.mark.asyncio
async def test_watcher_releases_the_park_when_it_saw_no_background_work(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("no-work", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="no-work-test")
    chat.session_id = "sess-no-work-1"
    pcm._save()

    session_path = tmp_path / "sess-no-work-1.jsonl"
    _write_jsonl(session_path, [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello there."}],
            },
        },
    ])

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session_path,
    )

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    running_drain = asyncio.get_running_loop().create_future()
    pcm._between_turn_drains[chat.chat_id] = running_drain  # type: ignore[assignment]
    calls = _announce_spy(pcm)
    pcm._park_result_announce(chat.chat_id, project.project_id, "Title", "interim")

    try:
        await pcm._watch_subagent_completion(chat.chat_id, project.project_id)
    finally:
        running_drain.cancel()

    assert calls == [(chat.chat_id, project.project_id, "Title", "interim")]


@pytest.mark.asyncio
async def test_the_watchers_box_survives_it_raising_after_seeing_work(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    async def sees_work_then_raises(
        chat_id: str, project_id: str, saw_work: list[bool]
    ) -> None:
        saw_work.append(True)
        raise RuntimeError("watcher blew up")

    pcm._watch_subagent_completion_inner = sees_work_then_raises  # type: ignore[method-assign]
    running_drain = asyncio.get_running_loop().create_future()
    pcm._between_turn_drains["chat-1"] = running_drain  # type: ignore[assignment]
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim")

    try:
        with pytest.raises(RuntimeError):
            await pcm._watch_subagent_completion("chat-1", "proj-1")
    finally:
        running_drain.cancel()

    assert calls == []
    assert "chat-1" in pcm._parked_result_announce


def test_background_count_now_reads_the_session_file(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("count-now", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="count-now-test")
    chat.session_id = "sess-count-now"
    pcm._save()

    session_path = tmp_path / "sess-count-now.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "kick off work"}},
        *_dispatch_records("toolu_1", "agent-a"),
    ]
    _write_jsonl(session_path, records)

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session_path,
    )

    assert pcm._background_count_now(chat.chat_id) == 1
    assert pcm._subagents.running_count(chat.chat_id) == 0

    records.append(_completion_record("agent-a"))
    _write_jsonl(session_path, records)
    assert pcm._background_count_now(chat.chat_id) == 0


@pytest.mark.asyncio
async def test_a_live_user_turn_keeps_the_watchers_finally_quiet(
    tmp_path: Path, monkeypatch
) -> None:
    """A watcher exiting during a live user turn stays quiet.

    That turn discards the park and announces for itself when it ends, so a
    flush here would push "I'll report back once the agents finish" into the
    middle of it.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("supersede-watch", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="supersede-watch-test")
    chat.session_id = "sess-supersede-1"
    pcm._save()

    session_path = tmp_path / "sess-supersede-1.jsonl"
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
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session_path,
    )

    async def fake_sleep(seconds: float) -> None:
        record = next(completions, None)
        if record is not None:
            records.append(record)
            flush()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def steer(self, request) -> bool:  # pragma: no cover
            raise AssertionError("must not steer into a live user turn")

    pcm._providers[chat.chat_id] = FakeProvider()  # type: ignore[assignment]
    # A user send: no between-turns drain left, a live foreground stream.
    pcm._broker.register(chat.chat_id, ChatStream(prompt_text="what now?"))
    calls = _announce_spy(pcm)
    pcm._park_result_announce(chat.chat_id, project.project_id, "Title", "interim")

    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)

    assert calls == []
    assert chat.chat_id in pcm._parked_result_announce


@pytest.mark.asyncio
async def test_a_watcher_with_no_turn_to_supersede_it_still_releases(
    tmp_path: Path, monkeypatch
) -> None:
    """The other half: a drain that is simply gone (Stop, a drain that ended)
    with no user turn in flight must still release the park — nothing else
    would, and the chat would complete in silence."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("no-drain", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="no-drain-test")
    chat.session_id = "sess-no-drain-1"
    pcm._save()

    session_path = tmp_path / "sess-no-drain-1.jsonl"
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
        lambda session_id, workspace_root, *, agent_root=None, force_refresh=False: session_path,
    )

    async def fake_sleep(seconds: float) -> None:
        record = next(completions, None)
        if record is not None:
            records.append(record)
            flush()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def steer(self, request) -> bool:  # pragma: no cover
            raise AssertionError("no drain to capture the reply")

    pcm._providers[chat.chat_id] = FakeProvider()  # type: ignore[assignment]
    calls = _announce_spy(pcm)
    pcm._park_result_announce(chat.chat_id, project.project_id, "Title", "interim")

    await pcm._watch_subagent_completion(chat.chat_id, project.project_id)

    assert calls == [(chat.chat_id, project.project_id, "Title", "interim")]


@pytest.mark.asyncio
async def test_stopping_the_drain_releases_the_park(tmp_path: Path) -> None:
    """Stop ends the drain and then releases the park.

    The drain's cancelled path leaves the park "to the next turn", but Stop
    starts no next turn, so without an explicit release the chat keeps its
    interim message with no unread badge, no push and no archive proposal.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    stream = ChatStream(background=True)
    pcm._broker.register("chat-1", stream)
    drain = asyncio.get_running_loop().create_future()
    pcm._between_turn_drains["chat-1"] = drain  # type: ignore[assignment]
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim")

    assert await pcm.stop_chat("chat-1") is True

    assert calls == [("chat-1", "proj-1", "Title", "interim")]
    assert "chat-1" not in pcm._parked_result_announce


@pytest.mark.asyncio
async def test_stop_racing_a_user_send_leaves_the_park_to_that_turn(
    tmp_path: Path,
) -> None:
    """Stop, then a send: the release must not land inside the new turn.

    ``stop_chat`` awaits the drain teardown before releasing, and that await
    is exactly where a user send gets in — it cancels the drain and registers
    its own foreground stream. Releasing afterwards would push "I'll report
    back once the agents finish" mid-turn; the new turn discards the park and
    announces for itself when it ends.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    background = ChatStream(background=True)
    pcm._broker.register("chat-1", background)
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim")

    async def fake_drain() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # What start_stream does when a user send lands on a background
            # stream, interleaved with the drain's own unwind.
            pcm._broker.clear("chat-1", background)
            pcm._broker.register("chat-1", ChatStream(prompt_text="and now?"))
            raise

    pcm._between_turn_drains["chat-1"] = asyncio.create_task(fake_drain())
    await asyncio.sleep(0)

    assert await pcm.stop_chat("chat-1") is True

    assert calls == []
    assert "chat-1" in pcm._parked_result_announce


def test_the_release_itself_refuses_during_a_live_turn(tmp_path: Path) -> None:
    """The guard lives in `_flush_result_announce`, not at its call sites.

    There are three sites — the watcher's `finally`, the drain and Stop — and
    a stale park pushed mid-turn presents differently from each. Pinning it on
    the shared release is what stops a fourth site repeating it.

    The entry stays parked rather than being popped: the live turn discards it
    and announces for itself at turn-done.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim")

    foreground = ChatStream(background=False)
    pcm._broker.register("chat-1", foreground)
    try:
        assert pcm._flush_result_announce("chat-1") is False
        assert calls == []
        # Not consumed — the live turn still owns it.
        assert "chat-1" in pcm._parked_result_announce
    finally:
        pcm._broker.clear("chat-1", foreground)

    # Once that turn is gone the same call releases normally.
    assert pcm._flush_result_announce("chat-1") is True
    assert calls == [("chat-1", "proj-1", "Title", "interim")]


def test_a_background_stream_does_not_count_as_a_live_turn(tmp_path: Path) -> None:
    """The drain's own stream is background; it must not block its own release."""
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim")

    background = ChatStream(background=True)
    pcm._broker.register("chat-1", background)
    try:
        assert pcm._foreground_turn_active("chat-1") is False
        assert pcm._flush_result_announce("chat-1") is True
    finally:
        pcm._broker.clear("chat-1", background)

    assert calls == [("chat-1", "proj-1", "Title", "interim")]


