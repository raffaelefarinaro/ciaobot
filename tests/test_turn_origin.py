"""Engine-written user turns carry an ``origin`` tag (#1539).

The provider transcripts record no sender, so the engine keeps its own per-turn
map (``ChatInfo.user_turn_origin``), echoes it on ``user_echo`` and reads it back
in ``/messages`` so the PWA draws a notice card instead of a user bubble.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.models import ResultEvent
from ciao.web.chat_broker import ChatStream
from ciao.web.routes_api import chat_messages
from ciao.web.transcript_service import _render_opencode_thread
from tests.test_chat_messages_interrupted import _make_manager, _Msg, _request
from tests.test_project_chats_queue import _wait_for


def _result(chat) -> ResultEvent:
    return ResultEvent(
        type="result",
        result="assistant answer",
        session_id="sess-x",
        is_error=False,
        effective_model=chat.model,
        usage={},
        quota={},
        cost_usd=0.0,
    )


def _setup(tmp_path: Path, title: str = "origin-test"):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Origins", workspace="personal")
    chat = pcm.create_chat(project.project_id, title=title)
    return pcm, chat


async def _run_turn(pcm, chat, prompt: str, **kwargs) -> list[dict]:
    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield _result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    captured: list[dict] = []
    stream = pcm.start_stream(chat.chat_id, prompt, **kwargs)
    async for ev in stream.subscribe():
        captured.append(ev)
    return [e for e in captured if e.get("type") == "user_echo"]


async def test_start_stream_records_origin_and_echoes_it(tmp_path: Path) -> None:
    pcm, chat = _setup(tmp_path)
    echoes = await asyncio.wait_for(
        _run_turn(
            pcm, chat, "[Ciaobot] 1 background run finished.", origin="background_run"
        ),
        timeout=5.0,
    )
    assert echoes[0]["origin"] == "background_run"
    assert chat.user_turn_origin == {"0": "background_run"}


async def test_start_stream_without_origin_records_nothing(tmp_path: Path) -> None:
    pcm, chat = _setup(tmp_path)
    echoes = await asyncio.wait_for(_run_turn(pcm, chat, "hello"), timeout=5.0)
    assert "origin" not in echoes[0]
    assert chat.user_turn_origin == {}


def test_start_stream_rejects_an_unknown_origin(tmp_path: Path) -> None:
    pcm, chat = _setup(tmp_path)
    with pytest.raises(ValueError):
        pcm.start_stream(chat.chat_id, "hello", origin="cron")
    assert pcm.get_active_stream(chat.chat_id) is None
    assert chat.user_turn_count == 0


def test_queue_message_rejects_an_unknown_origin(tmp_path: Path) -> None:
    pcm, chat = _setup(tmp_path)
    with pytest.raises(ValueError):
        pcm.queue_message(chat.chat_id, "hello", origin="cron")


async def test_a_queued_wake_keeps_its_origin_when_it_becomes_a_turn(
    tmp_path: Path,
) -> None:
    pcm, chat = _setup(tmp_path)
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            await release.wait()
        yield _result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    captured: list[dict] = []
    stream = pcm.start_stream(chat.chat_id, "initial")

    async def consume() -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(consume())
    await _wait_for(lambda: any(e.get("type") == "user_echo" for e in captured))
    assert pcm.queue_message(chat.chat_id, "wake", entry_id="q-w", origin="background_run")
    release.set()
    await asyncio.wait_for(consumer, timeout=5.0)

    echoes = [e for e in captured if e.get("type") == "user_echo"]
    assert len(echoes) == 2
    assert "origin" not in echoes[0]
    assert echoes[1]["origin"] == "background_run"
    assert echoes[1]["entry_id"] == "q-w"
    assert chat.user_turn_origin == {"1": "background_run"}


def test_chat_stream_origin_survives_drain_and_is_dropped_on_edit() -> None:
    stream = ChatStream()
    stream.enqueue("a", origin="cli_task_wake")
    assert stream.drain_one()["origin"] == "cli_task_wake"

    entry_id = stream.enqueue("b")
    assert stream.drain_one() == {"id": entry_id, "text": "b", "images": []}

    stream.enqueue("c", entry_id="q-c", origin="background_run")
    assert stream.edit_pending("q-c", "edited")
    assert "origin" not in stream.drain_one()


def test_adopt_waiting_carries_origin() -> None:
    stream = ChatStream()
    stream.adopt_waiting(
        [
            {"id": "w1", "text": "wake", "images": [], "origin": "background_run"},
            {"id": "w2", "text": "typed", "images": []},
        ],
        "queue",
    )
    first = stream.drain_one()
    second = stream.drain_one()
    assert first["origin"] == "background_run"
    assert "origin" not in second


async def test_prompt_cancelled_rolls_back_origin_and_keeps_it_on_the_waiting_entry(
    tmp_path: Path,
) -> None:
    pcm, chat = _setup(tmp_path)
    chat.user_turn_count = 1
    chat.user_turn_origin = {"0": "background_run"}
    pcm.on_prompt_cancelled(chat.chat_id, ChatStream(prompt_text="wake"), "wake", None, 0)
    assert chat.user_turn_origin == {}
    assert chat.pending_queue[-1]["origin"] == "background_run"


def test_user_turn_origin_survives_save_and_reload(tmp_path: Path) -> None:
    pcm, chat = _setup(tmp_path)
    chat.user_turn_origin["2"] = "schedule"
    pcm._save()

    stored = json.loads((tmp_path / ".runtime" / "web_projects.json").read_text())
    assert stored["chats"][chat.chat_id]["user_turn_origin"] == {"2": "schedule"}

    pcm._chats.clear()
    pcm._load()
    reloaded = pcm.get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.user_turn_origin == {"2": "schedule"}


async def test_origin_on_claude_history_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat = _setup(tmp_path)
    chat.session_id = "sess-origin"
    chat.user_turn_origin = {"1": "webhook"}
    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: [
            _Msg("user", "typed prompt"),
            _Msg("assistant", "working"),
            _Msg("user", "webhook text"),
            _Msg("assistant", "done"),
        ]
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    users = [r for r in json.loads(response.body) if r["role"] == "user"]
    assert "origin" not in users[0]
    assert users[1]["origin"] == "webhook"


def test_origin_on_opencode_history_rows(tmp_path: Path) -> None:
    pcm, chat = _setup(tmp_path)
    chat.user_turn_origin = {"1": "webhook"}

    def user(text: str) -> dict:
        return {"info": {"role": "user"}, "parts": [{"type": "text", "text": text}]}

    rows = _render_opencode_thread({"info": {}, "messages": [user("one"), user("two")]}, chat)
    users = [r for r in rows if r["role"] == "user"]
    assert "origin" not in users[0]
    assert users[1]["origin"] == "webhook"


def test_origin_is_independent_of_unattended(tmp_path: Path) -> None:
    pcm, chat = _setup(tmp_path)
    chat.user_turn_unattended = {"0": True}
    chat.user_turn_origin = {"0": "schedule"}

    def user(text: str) -> dict:
        return {"info": {"role": "user"}, "parts": [{"type": "text", "text": text}]}

    rows = _render_opencode_thread({"info": {}, "messages": [user("tick")]}, chat)
    users = [r for r in rows if r["role"] == "user"]
    assert users[0]["unattended"] is True
    assert users[0]["origin"] == "schedule"
