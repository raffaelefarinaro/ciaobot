from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from starlette.requests import Request

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.chat_broker import ChatStream
from ciao.web.routes_api import chat_move_to_background


def _manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    config = CiaoConfig(pwa_auth_token="test", workspace_root=tmp_path,
        state_path=runtime / "state.json", media_root=runtime / "media")
    return ProjectChatManager(config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "archives"),
        path=runtime / "web_projects.json")


def _request(chat_id: str, app) -> Request:
    return Request({"type": "http", "method": "POST",
        "path": f"/api/chats/{chat_id}/move-to-background", "headers": [],
        "app": app, "path_params": {"chat_id": chat_id}, "query_string": b""})


def _app(pcm: ProjectChatManager) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(config=pcm._config, project_chat_manager=pcm))


def _chat(pcm: ProjectChatManager) -> str:
    project = pcm.create_project("background-route", workspace="personal")
    return pcm.create_chat(project.project_id, title="background-test").chat_id


def test_route_reports_moved(tmp_path: Path, monkeypatch) -> None:
    pcm = _manager(tmp_path); chat_id = _chat(pcm)
    async def moved(_): return "moved"
    monkeypatch.setattr(pcm, "move_chat_to_background", moved)
    response = asyncio.run(chat_move_to_background(_request(chat_id, _app(pcm))))
    assert response.status_code == 200 and json.loads(response.body) == {"moved": True}


def test_route_reports_nothing_to_move(tmp_path: Path, monkeypatch) -> None:
    pcm = _manager(tmp_path); chat_id = _chat(pcm)
    async def nothing(_): return "nothing"
    monkeypatch.setattr(pcm, "move_chat_to_background", nothing)
    response = asyncio.run(chat_move_to_background(_request(chat_id, _app(pcm))))
    assert response.status_code == 200 and json.loads(response.body) == {"moved": False}


def test_route_refuses_unsupported_and_not_running(tmp_path: Path, monkeypatch) -> None:
    pcm = _manager(tmp_path); chat_id = _chat(pcm)
    async def outcome(_): return state[0]
    monkeypatch.setattr(pcm, "move_chat_to_background", outcome)
    state = ["unsupported"]
    for value in state + ["not_running"]:
        state[0] = value
        response = asyncio.run(chat_move_to_background(_request(chat_id, _app(pcm))))
        assert response.status_code == 409 and json.loads(response.body) == {"error": value}


def test_route_maps_failed_to_502(tmp_path: Path, monkeypatch) -> None:
    pcm = _manager(tmp_path); chat_id = _chat(pcm)
    async def failed(_): return "failed"
    monkeypatch.setattr(pcm, "move_chat_to_background", failed)
    response = asyncio.run(chat_move_to_background(_request(chat_id, _app(pcm))))
    assert response.status_code == 502


def test_route_404s_an_unknown_chat(tmp_path: Path, monkeypatch) -> None:
    pcm = _manager(tmp_path)
    called = False
    async def unexpected(_):
        nonlocal called; called = True; return "moved"
    monkeypatch.setattr(pcm, "move_chat_to_background", unexpected)
    response = asyncio.run(chat_move_to_background(_request("missing", _app(pcm))))
    assert response.status_code == 404 and not called


def test_manager_flags_the_live_stream_when_moved(tmp_path: Path) -> None:
    pcm = _manager(tmp_path); chat_id = _chat(pcm)
    service = SimpleNamespace(move_to_background=AsyncMock(return_value="moved"))
    pcm._providers[chat_id] = service
    stream = ChatStream(); pcm._broker.register(chat_id, stream)
    assert asyncio.run(pcm.move_chat_to_background(chat_id)) == "moved"
    assert stream.moved_to_background is True


def test_manager_flags_the_originating_stream_not_a_replacement(tmp_path: Path) -> None:
    pcm = _manager(tmp_path); chat_id = _chat(pcm)
    stream_a = ChatStream()
    stream_b = ChatStream()
    pcm._broker.register(chat_id, stream_a)

    async def move_and_replace():
        pcm._broker.register(chat_id, stream_b)
        return "moved"

    pcm._providers[chat_id] = SimpleNamespace(
        move_to_background=AsyncMock(side_effect=move_and_replace),
    )
    assert asyncio.run(pcm.move_chat_to_background(chat_id)) == "moved"
    assert stream_a.moved_to_background is True
    assert stream_b.moved_to_background is False


def test_manager_leaves_the_stream_alone_when_nothing_moved(tmp_path: Path) -> None:
    pcm = _manager(tmp_path); chat_id = _chat(pcm)
    pcm._providers[chat_id] = SimpleNamespace(move_to_background=AsyncMock(return_value="nothing"))
    stream = ChatStream(); pcm._broker.register(chat_id, stream)
    assert asyncio.run(pcm.move_chat_to_background(chat_id)) == "nothing"
    assert stream.moved_to_background is False


def test_an_unsuccessful_move_does_not_erase_a_successful_one(tmp_path: Path) -> None:
    async def exercise(label: str, outcomes: list[str]) -> tuple[list[str], bool]:
        root = tmp_path / label
        root.mkdir()
        pcm = _manager(root); chat_id = _chat(pcm)
        stream = ChatStream(); pcm._broker.register(chat_id, stream)
        pcm._providers[chat_id] = SimpleNamespace(
            move_to_background=AsyncMock(side_effect=outcomes),
        )
        results = [
            await pcm.move_chat_to_background(chat_id),
            await pcm.move_chat_to_background(chat_id),
        ]
        return results, stream.moved_to_background

    assert asyncio.run(exercise("moved-then-nothing", ["moved", "nothing"])) == (["moved", "nothing"], True)
    assert asyncio.run(exercise("nothing-then-moved", ["nothing", "moved"])) == (["nothing", "moved"], True)


def test_overlapping_moves_keep_the_mark_until_the_last_answers(tmp_path: Path) -> None:
    async def exercise() -> None:
        pcm = _manager(tmp_path); chat_id = _chat(pcm)
        stream = ChatStream(); pcm._broker.register(chat_id, stream)
        release_moved = asyncio.Event()
        release_nothing = asyncio.Event()
        calls = 0

        async def move():
            nonlocal calls
            call = calls
            calls += 1
            if call == 0:
                await release_moved.wait()
                return "moved"
            await release_nothing.wait()
            return "nothing"

        pcm._providers[chat_id] = SimpleNamespace(move_to_background=move)
        moved_task = asyncio.create_task(pcm.move_chat_to_background(chat_id))
        nothing_task = asyncio.create_task(pcm.move_chat_to_background(chat_id))
        await asyncio.sleep(0)
        release_moved.set()
        assert await moved_task == "moved"
        assert stream.moved_to_background is True
        release_nothing.set()
        assert await nothing_task == "nothing"
        assert stream.moved_to_background is True

    asyncio.run(exercise())


def test_a_lone_unsuccessful_move_clears_the_mark(tmp_path: Path) -> None:
    pcm = _manager(tmp_path); chat_id = _chat(pcm)
    stream = ChatStream(); pcm._broker.register(chat_id, stream)
    pcm._providers[chat_id] = SimpleNamespace(
        move_to_background=AsyncMock(return_value="nothing"),
    )
    assert asyncio.run(pcm.move_chat_to_background(chat_id)) == "nothing"
    assert stream.moved_to_background is False


def test_manager_without_a_provider_is_not_running(tmp_path: Path) -> None:
    pcm = _manager(tmp_path)
    assert asyncio.run(pcm.move_chat_to_background("absent")) == "not_running"
