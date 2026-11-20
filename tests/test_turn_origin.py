"""Engine-written user turns carry an ``origin`` tag (#1539).

The provider transcripts record no sender, so the engine keeps its own per-turn
map (``ChatInfo.user_turn_origin``), echoes it on ``user_echo`` and reads it back
in ``/messages`` so the PWA draws a notice card instead of a user bubble.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.models import ResultEvent
from ciao.web.chat_broker import ChatStream, shared_origin
from ciao.web.routes_api import chat_messages
from ciao.web import transcript_service
from tests.test_chat_messages_interrupted import _make_manager, _Msg, _request
from tests.test_project_chats_queue import _wait_for


def _result(chat) -> ResultEvent:
    return ResultEvent(
        type="result",
        result="assistant answer",
        session_id="efa632d4-66e7-5764-a29e-02ed1c06def0",
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
    chat.session_id = "5c3a66eb-8820-544d-bf5e-093ac8e156b0"
    chat.user_turn_origin = {"1": "webhook"}
    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: [
            _Msg("user", "typed prompt"),
            _Msg("assistant", "working"),
            _Msg("user", "webhook text"),
            _Msg("assistant", "done"),
        ]
    )
    monkeypatch.setattr("ciao.transcripts.get_session_messages", fake_sdk.get_session_messages)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    users = [r for r in json.loads(response.body) if r["role"] == "user"]
    assert "origin" not in users[0]
    assert users[1]["origin"] == "webhook"


def test_origin_on_acp_journal_rows(tmp_path: Path) -> None:
    _pcm, chat = _setup(tmp_path)
    chat.user_turn_origin = {"1": "webhook"}
    journal = [
        {"type": "user", "text": "one"},
        {"type": "user", "text": "two"},
    ]

    rows = transcript_service._render_acp_journal(
        journal, chat, start_user_idx=0, workspace_root=None, agent_root=None
    )
    users = [r for r in rows if r["role"] == "user"]
    assert "origin" not in users[0]
    assert users[1]["origin"] == "webhook"


def test_origin_is_independent_of_unattended(tmp_path: Path) -> None:
    _pcm, chat = _setup(tmp_path)
    chat.user_turn_unattended = {"0": True}
    chat.user_turn_origin = {"0": "schedule"}

    rows = transcript_service._render_acp_journal(
        [{"type": "user", "text": "tick"}],
        chat,
        start_user_idx=0,
        workspace_root=None,
        agent_root=None,
    )
    users = [r for r in rows if r["role"] == "user"]
    assert users[0]["unattended"] is True
    assert users[0]["origin"] == "schedule"


def test_shared_origin_requires_every_entry_to_agree() -> None:
    assert (
        shared_origin([{"origin": "background_run"}, {"origin": "background_run"}])
        == "background_run"
    )
    assert shared_origin([{"origin": "background_run"}, {}]) == ""
    assert shared_origin([{"origin": "background_run"}, {"origin": "cli_task_wake"}]) == ""
    assert shared_origin([]) == ""
    assert shared_origin([{}]) == ""


WAKE = "[Ciaobot] 1 background run finished."






def _parked_wake_chat(tmp_path: Path):
    pcm, chat = _setup(tmp_path)

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield _result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    chat.pending_queue = [
        {"id": "p1", "text": WAKE, "images": [], "origin": "background_run"},
        {"id": "p2", "text": "stays", "images": []},
    ]
    return pcm, chat


async def test_send_now_of_a_parked_wake_keeps_its_origin(tmp_path: Path) -> None:
    pcm, chat = _parked_wake_chat(tmp_path)
    assert await pcm.send_queued_now(chat.chat_id, ["p1"]) is True
    stream = pcm.get_active_stream(chat.chat_id)
    assert stream is not None
    await _wait_for(lambda: stream.done)
    echo = next(e for e in stream.buffered_events() if e.get("type") == "user_echo")
    assert echo["origin"] == "background_run"
    assert chat.user_turn_origin == {"0": "background_run"}
    assert [e["id"] for e in chat.pending_queue] == ["p2"]


async def test_send_now_of_a_wake_with_a_typed_entry_has_no_origin(
    tmp_path: Path,
) -> None:
    pcm, chat = _parked_wake_chat(tmp_path)
    assert await pcm.send_queued_now(chat.chat_id, ["p1", "p2"]) is True
    stream = pcm.get_active_stream(chat.chat_id)
    assert stream is not None
    await _wait_for(lambda: stream.done)
    echo = next(e for e in stream.buffered_events() if e.get("type") == "user_echo")
    assert "origin" not in echo
    assert echo["text"] == f"{WAKE}\n\nstays"
    assert chat.user_turn_origin == {}





# ── The retry origin ──────────────────────────────────────────────────────
#
# A retry the user asked for is their own message, but it is drawn as a
# notice, so it carries the same durable tag the engine-written turns carry.


async def test_start_stream_records_and_echoes_the_retry_origin(tmp_path: Path) -> None:
    pcm, chat = _setup(tmp_path)
    echoes = await asyncio.wait_for(
        _run_turn(pcm, chat, "try again", origin="retry"), timeout=5.0
    )
    assert echoes[0]["origin"] == "retry"
    assert chat.user_turn_origin == {"0": "retry"}


def test_the_retry_origin_survives_save_and_reload(tmp_path: Path) -> None:
    pcm, chat = _setup(tmp_path)
    chat.user_turn_origin["2"] = "retry"
    pcm._save()

    stored = json.loads((tmp_path / ".runtime" / "web_projects.json").read_text())
    assert stored["chats"][chat.chat_id]["user_turn_origin"] == {"2": "retry"}

    pcm._chats.clear()
    pcm._load()
    reloaded = pcm.get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.user_turn_origin == {"2": "retry"}


async def test_the_retry_origin_reaches_the_history_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reload reconciles the chat from history: the tag must be in it."""
    pcm, chat = _setup(tmp_path)
    chat.session_id = "bd940ee7-03c5-5851-951d-d6e66ec182a7"
    chat.user_turn_origin = {"1": "retry"}
    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: [
            _Msg("user", "typed prompt"),
            _Msg("assistant", "working"),
            _Msg("user", "retried prompt"),
            _Msg("assistant", "done"),
        ]
    )
    monkeypatch.setattr("ciao.transcripts.get_session_messages", fake_sdk.get_session_messages)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    users = [r for r in json.loads(response.body) if r["role"] == "user"]
    assert "origin" not in users[0]
    assert users[1]["origin"] == "retry"


async def test_a_queued_retry_keeps_its_origin_when_it_becomes_a_turn(
    tmp_path: Path,
) -> None:
    """A retry that waits for the running turn keeps its tag when it flushes."""
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
    assert pcm.queue_message(chat.chat_id, "try again", entry_id="q-r", origin="retry")
    release.set()
    await asyncio.wait_for(consumer, timeout=5.0)

    echoes = [e for e in captured if e.get("type") == "user_echo"]
    assert len(echoes) == 2
    assert "origin" not in echoes[0]
    assert echoes[1]["origin"] == "retry"
    assert chat.user_turn_origin == {"1": "retry"}
