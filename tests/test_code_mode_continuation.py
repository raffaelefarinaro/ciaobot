"""The drive loop's one hidden continuation after an agent switches its own chat into code mode."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ciao.cli_envelopes import CODE_MODE_CONTINUE_PROMPT
from ciao.models import ResultEvent
from tests.test_project_chats_queue import _make_manager, _wait_for


def _result(chat, *, is_error: bool = False) -> ResultEvent:
    return ResultEvent(
        type="result",
        result="assistant answer",
        session_id="sess-x",
        is_error=is_error,
        effective_model=chat.model,
        usage={},
        quota={},
        cost_usd=0.0,
    )


async def _run(tmp_path: Path, first_turn, *, queue_during_first: str | None = None):
    """Drive "initial" with `first_turn(pcm, cid)` run inside the first fake turn."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-code-continue", workspace="work")
    chat = pcm.create_chat(project.project_id, title="code-continue")
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        is_error = False
        if len(turn_calls) == 1:
            is_error = bool(first_turn(pcm, chat_id))
            if queue_during_first:
                assert pcm.queue_message(chat_id, queue_during_first) is True
        yield _result(chat, is_error=is_error)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))
    await asyncio.wait_for(consumer, timeout=5.0)
    return pcm, chat, turn_calls, captured


def _switch_on(pcm, cid):
    pcm._chats[cid].code_mode = True
    assert pcm.arm_code_mode_continuation(cid) is True


async def test_agent_switch_runs_one_hidden_continuation(tmp_path: Path) -> None:
    pcm, chat, turn_calls, captured = await _run(tmp_path, _switch_on)
    assert turn_calls == ["initial", CODE_MODE_CONTINUE_PROMPT]
    assert len([e for e in captured if e.get("type") == "user_echo"]) == 1
    assert len([e for e in captured if e.get("type") == "result"]) == 2
    assert chat.user_turn_count == 1


async def test_no_continuation_after_an_error_result(tmp_path: Path) -> None:
    def first(pcm, cid):
        _switch_on(pcm, cid)
        return True

    _, _, turn_calls, _ = await _run(tmp_path, first)
    assert turn_calls == ["initial"]


async def test_no_continuation_after_a_user_stop(tmp_path: Path) -> None:
    def first(pcm, cid):
        _switch_on(pcm, cid)
        pcm.get_active_stream(cid).user_stopped = True

    _, _, turn_calls, _ = await _run(tmp_path, first)
    assert turn_calls == ["initial"]


async def test_a_queued_message_supersedes_the_continuation(tmp_path: Path) -> None:
    _, _, turn_calls, _ = await _run(tmp_path, _switch_on, queue_during_first="msg A")
    assert turn_calls == ["initial", "msg A"]


async def test_no_continuation_when_code_mode_was_switched_back_off(tmp_path: Path) -> None:
    def first(pcm, cid):
        _switch_on(pcm, cid)
        pcm._chats[cid].code_mode = False

    _, _, turn_calls, _ = await _run(tmp_path, first)
    assert turn_calls == ["initial"]


async def test_arming_twice_continues_once(tmp_path: Path) -> None:
    def first(pcm, cid):
        _switch_on(pcm, cid)
        assert pcm.arm_code_mode_continuation(cid) is True

    _, _, turn_calls, _ = await _run(tmp_path, first)
    assert len(turn_calls) == 2


async def test_arm_returns_false_without_a_foreground_turn(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-code-idle", workspace="work")
    chat = pcm.create_chat(project.project_id, title="code-idle")
    assert pcm.arm_code_mode_continuation(chat.chat_id) is False
    assert pcm.arm_code_mode_continuation("missing") is False
