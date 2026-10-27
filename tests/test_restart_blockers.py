"""`ProjectChatManager.restart_blockers`: what a restart or stop would break."""

from __future__ import annotations

import json
import math
from pathlib import Path

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_broker import ChatStream
from ciao.web.project_chats import ProjectChatManager


class _StubProvider:
    background_work_outstanding = False
    background_activity_age = math.inf


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _chat(manager: ProjectChatManager) -> str:
    project = manager.create_project("Blockers", workspace="work")
    return manager.create_chat(project.project_id, title="Chat").chat_id


def _attach(manager: ProjectChatManager, chat_id: str) -> _StubProvider:
    provider = _StubProvider()
    manager._providers[chat_id] = provider  # type: ignore[assignment]
    return provider


def test_streaming_chat_is_a_blocker(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    manager._broker.register(cid, ChatStream(prompt_text="x"))
    assert manager.restart_blockers() == [{"chat_id": cid, "reasons": ["streaming"]}]


def test_background_agents_are_blockers(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    monkeypatch.setattr(manager._subagents, "watching_chat_ids", lambda: {cid})
    assert manager.restart_blockers() == [{"chat_id": cid, "reasons": ["background_agents"]}]


def test_open_native_question_with_live_provider_blocks(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    _attach(manager, cid)
    manager._chats[cid].pending_question = json.dumps({"request_id": "req-1", "questions": []})
    assert manager.restart_blockers() == [{"chat_id": cid, "reasons": ["open_question"]}]


def test_open_native_question_without_provider_does_not_block(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    manager._chats[cid].pending_question = json.dumps({"request_id": "req-1", "questions": []})
    assert manager.restart_blockers() == []


def test_open_native_permission_with_live_provider_blocks(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    _attach(manager, cid)
    manager._chats[cid].pending_permission = json.dumps({"request_id": "perm-1", "tool_name": "Bash"})
    assert manager.restart_blockers() == [{"chat_id": cid, "reasons": ["open_permission"]}]


def test_legacy_question_and_idle_provider_do_not_block(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    _attach(manager, cid)
    manager._chats[cid].pending_question = json.dumps({"questions": [{"id": "q1"}]})
    assert manager.restart_blockers() == []


def test_outstanding_background_work_blocks(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    provider = _attach(manager, cid)
    provider.background_work_outstanding = True
    provider.background_activity_age = 0.0
    assert manager.restart_blockers() == [{"chat_id": cid, "reasons": ["background_work"]}]


def test_pending_queue_alone_does_not_block(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    _attach(manager, cid)
    manager._chats[cid].pending_queue = [{"text": "later"}]
    assert manager.restart_blockers() == []


def test_blockers_are_a_superset_of_active_chat_ids(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    streaming = _chat(manager)
    asking = _chat(manager)
    manager._broker.register(streaming, ChatStream(prompt_text="x"))
    _attach(manager, asking)
    manager._chats[asking].pending_question = json.dumps({"request_id": "req-1"})
    blocked = {b["chat_id"] for b in manager.restart_blockers()}
    assert set(manager.active_chat_ids()) <= blocked
    assert blocked == {streaming, asking}


def test_claude_background_task_reports_background_work(tmp_path: Path) -> None:
    from claude_agent_sdk import TaskStartedMessage

    from ciao.provider_service import ProviderService
    from ciao.providers.claude import ClaudeProvider

    manager = _make_manager(tmp_path)
    cid = _chat(manager)
    provider = ClaudeProvider(tmp_path)
    provider._observe_message(
        TaskStartedMessage(
            subtype="task_started", data={}, task_id="bash-1",
            description="sleep 600", uuid="u", session_id="s", task_type="local_bash",
        )
    )
    service = ProviderService(manager._config, provider="claude")
    service._provider = provider  # type: ignore[assignment]
    manager._providers[cid] = service  # type: ignore[assignment]

    assert manager.restart_blockers() == [{"chat_id": cid, "reasons": ["background_work"]}]
