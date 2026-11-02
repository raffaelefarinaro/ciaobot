"""Resume chats whose turn an engine restart cut off (#2128)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ciao.config import CiaoConfig
from ciao.models import ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import restart_resume
from ciao.web.chat_broker import TURN_ORIGINS
from ciao.web.project_chats import ChatInfo, ProjectChatManager

from tests.conftest import attach_stub_mcp


def _make_manager(tmp_path: Path) -> ProjectChatManager:
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
    return attach_stub_mcp(ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    ))


async def _consume(stream) -> list[dict]:
    events: list[dict] = []
    async for event in stream.subscribe():
        events.append(event)
    return events


class _FakeStreams:
    """Stand in for ``start_stream``: records the turn, dispatches nothing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, chat_id: str, prompt: str, images: object = None, **kwargs: Any
    ) -> None:
        del images
        self.calls.append({"chat_id": chat_id, "prompt": prompt, **kwargs})


def _result_event(chat: ChatInfo) -> ResultEvent:
    return ResultEvent(
        type="result",
        result="done",
        session_id="s1",
        is_error=False,
        effective_model=chat.model,
        usage={},
        quota={},
        cost_usd=0.0,
    )


async def _flush_armed_resumes(
    monkeypatch: pytest.MonkeyPatch, pcm: ProjectChatManager
) -> None:
    monkeypatch.setattr(restart_resume, "RESUME_DELAY_SECONDS", 0.0)
    await asyncio.gather(*list(pcm._detached_tasks))


def test_restart_resume_origin_is_a_turn_origin() -> None:
    assert restart_resume.RESTART_RESUME_ORIGIN in TURN_ORIGINS


async def test_a_turn_stamps_its_process_and_clears_the_stamp_when_it_ends(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("resume", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="resume-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield _result_event(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    assert chat.turn_owner == restart_resume.PROCESS_TOKEN
    assert _make_manager(tmp_path).get_chat(chat.chat_id).turn_owner == (
        restart_resume.PROCESS_TOKEN
    )

    await asyncio.wait_for(_consume(stream), timeout=10.0)
    assert await pcm._streaming.wait_for_drive_cleanup(chat.chat_id, None, 5.0)

    assert chat.turn_owner == ""
    assert _make_manager(tmp_path).get_chat(chat.chat_id).turn_owner == ""


async def test_a_turn_torn_down_by_shutdown_keeps_its_stamp(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("resume", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="resume-test")
    gate = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        await gate.wait()
        yield _result_event(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.sleep(0)
    pcm.begin_shutdown()
    gate.set()
    await asyncio.wait_for(_consume(stream), timeout=10.0)
    assert await pcm._streaming.wait_for_drive_cleanup(chat.chat_id, None, 5.0)

    assert chat.turn_owner == restart_resume.PROCESS_TOKEN
    assert _make_manager(tmp_path).get_chat(chat.chat_id).turn_owner == (
        restart_resume.PROCESS_TOKEN
    )


async def test_an_interrupted_turn_is_resumed_once_in_the_chats_own_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("resume", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="resume-test")
    chat.user_turn_count = 1
    chat.turn_owner = "1-dead"
    pcm._save()

    reloaded = _make_manager(tmp_path)
    recorder = _FakeStreams()
    reloaded.start_stream = recorder  # type: ignore[method-assign]

    assert reloaded.resume_interrupted_turns(enabled=True) == 1
    await _flush_armed_resumes(monkeypatch, reloaded)

    assert recorder.calls == [{
        "chat_id": chat.chat_id,
        "prompt": restart_resume.RESTART_RESUME_PROMPT,
        "origin": "restart_resume",
    }]
    assert reloaded.get_chat(chat.chat_id).turn_owner == ""
    assert _make_manager(tmp_path).get_chat(chat.chat_id).turn_owner == ""

    assert reloaded.resume_interrupted_turns(enabled=True) == 0
    await _flush_armed_resumes(monkeypatch, reloaded)
    assert len(recorder.calls) == 1


async def test_a_resume_that_was_itself_cut_off_is_not_resumed_again(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("resume", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="resume-test")
    chat.user_turn_count = 2
    chat.user_turn_origin = {"1": "restart_resume"}
    chat.turn_owner = "1-dead"
    pcm._save()

    reloaded = _make_manager(tmp_path)
    recorder = _FakeStreams()
    reloaded.start_stream = recorder  # type: ignore[method-assign]

    assert reloaded.resume_interrupted_turns(enabled=True) == 0
    assert recorder.calls == []
    assert reloaded.get_chat(chat.chat_id).turn_owner == ""


def _skip_cases() -> list[Any]:
    def setter(attr: str, value: object) -> Callable[[ChatInfo], None]:
        def apply(chat: ChatInfo) -> None:
            setattr(chat, attr, value)

        return apply

    cases: list[Any] = [
        pytest.param(setter("archived", True), id="archived"),
        pytest.param(setter("pending_question", "q"), id="pending-question"),
        pytest.param(setter("pending_permission", "p"), id="pending-permission"),
        pytest.param(setter("retry_status", "pending"), id="retry-pending"),
        pytest.param(
            setter("user_turn_unattended", {"0": True}), id="unattended-turn"
        ),
        pytest.param(
            setter("user_turn_origin", {"0": "task_delegation"}),
            id="origin-task-delegation",
        ),
        pytest.param(
            setter("user_turn_origin", {"0": "task_resume"}),
            id="origin-task-resume",
        ),
    ]
    for kind in ("memory_pass", "update_task", "proposal", "task_delegation"):
        cases.append(
            pytest.param(setter("helper", {"kind": kind}), id=f"helper-{kind}")
        )
    return cases


@pytest.mark.parametrize("mutate", _skip_cases())
async def test_an_interrupted_chat_is_skipped_when(
    tmp_path: Path, mutate: Callable[[ChatInfo], None]
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("resume", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="resume-test")
    chat.user_turn_count = 1
    chat.turn_owner = "1-dead"
    mutate(chat)
    recorder = _FakeStreams()
    pcm.start_stream = recorder  # type: ignore[method-assign]

    assert pcm.resume_interrupted_turns(enabled=True) == 0
    assert recorder.calls == []
    assert chat.turn_owner == ""


async def test_resume_is_off_when_the_setting_is_off(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("resume", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="resume-test")
    chat.user_turn_count = 1
    chat.turn_owner = "1-dead"
    pcm._save()

    reloaded = _make_manager(tmp_path)
    recorder = _FakeStreams()
    reloaded.start_stream = recorder  # type: ignore[method-assign]

    assert reloaded.resume_interrupted_turns(enabled=False) == 0
    assert recorder.calls == []
    assert reloaded.get_chat(chat.chat_id).turn_owner == ""
    assert _make_manager(tmp_path).get_chat(chat.chat_id).turn_owner == ""


async def test_a_chat_that_moved_on_before_the_resume_fires_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("resume", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="resume-test")
    chat.user_turn_count = 1
    chat.turn_owner = "1-dead"
    pcm._save()

    reloaded = _make_manager(tmp_path)
    recorder = _FakeStreams()
    reloaded.start_stream = recorder  # type: ignore[method-assign]

    assert reloaded.resume_interrupted_turns(enabled=True) == 1
    moved = reloaded.get_chat(chat.chat_id)
    assert moved is not None
    moved.user_turn_count += 1
    await _flush_armed_resumes(monkeypatch, reloaded)

    assert recorder.calls == []
