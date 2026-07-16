from __future__ import annotations

import json
from pathlib import Path
import pytest
from typing import AsyncGenerator

from ciao.config import CiaoConfig
from ciao.provider_subchats import (
    ProviderRoute,
    ProviderSubchatRecord,
    ProviderSubchatManager,
)
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.models import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
)


def _make_pcm(tmp_path: Path) -> ProjectChatManager:
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


def test_provider_subchat_record_serialization_roundtrip(tmp_path: Path) -> None:
    pcm = _make_pcm(tmp_path)
    runtime = tmp_path / ".runtime"
    manager_path = runtime / "provider_subchats.json"
    manager = ProviderSubchatManager(pcm._config, pcm, manager_path)

    owner = ProviderRoute(provider="claude", model="sonnet", model_bucket="anthropic", label="Claude")
    participant = ProviderRoute(provider="codex", model="gpt-4", label="Codex")

    record = ProviderSubchatRecord(
        subchat_id="sub-1",
        parent_chat_id="chat-1",
        parent_turn_index=2,
        workspace="personal",
        project_id="proj-1",
        owner=owner,
        participant=participant,
        participant_session_id="session-p1",
        status="completed",
        created_at="2026-07-15T12:00:00Z",
        started_at="2026-07-15T12:01:00Z",
        updated_at="2026-07-15T12:02:00Z",
        completed_at="2026-07-15T12:03:00Z",
        active_seconds=120.5,
        message_count=6,
        input_tokens=1000,
        output_tokens=500,
        quota_limit_hit=False,
        last_error="None",
        limit_extended_at="2026-07-15T12:02:30Z",
        limit_messages_extended=12,
        limit_seconds_extended=1800.0,
    )

    manager._records["sub-1"] = record
    manager._save()

    # Load in a fresh manager
    manager2 = ProviderSubchatManager(pcm._config, pcm, manager_path)
    loaded = manager2.get_record("sub-1")

    assert loaded is not None
    assert loaded.subchat_id == "sub-1"
    assert loaded.owner.provider == "claude"
    assert loaded.owner.model == "sonnet"
    assert loaded.owner.model_bucket == "anthropic"
    assert loaded.participant.provider == "codex"
    assert loaded.participant.model == "gpt-4"
    assert loaded.status == "completed"
    assert loaded.active_seconds == 120.5
    assert loaded.message_count == 6


def test_provider_subchat_reconciles_active_states_on_load(tmp_path: Path) -> None:
    pcm = _make_pcm(tmp_path)
    runtime = tmp_path / ".runtime"
    manager_path = runtime / "provider_subchats.json"
    manager = ProviderSubchatManager(pcm._config, pcm, manager_path)

    owner = ProviderRoute(provider="claude", model="sonnet")
    participant = ProviderRoute(provider="codex", model="gpt-4")

    r1 = ProviderSubchatRecord(
        subchat_id="sub-1",
        parent_chat_id="chat-1",
        parent_turn_index=0,
        workspace="personal",
        project_id="proj-1",
        owner=owner,
        participant=participant,
        status="running",
    )
    r2 = ProviderSubchatRecord(
        subchat_id="sub-2",
        parent_chat_id="chat-1",
        parent_turn_index=0,
        workspace="personal",
        project_id="proj-1",
        owner=owner,
        participant=participant,
        status="created",
    )

    manager._records["sub-1"] = r1
    manager._records["sub-2"] = r2
    manager._save()

    manager2 = ProviderSubchatManager(pcm._config, pcm, manager_path)
    assert manager2.get_record("sub-1").status == "interrupted"
    assert manager2.get_record("sub-2").status == "interrupted"


def test_provider_subchat_transcript_append_and_replay(tmp_path: Path) -> None:
    pcm = _make_pcm(tmp_path)
    runtime = tmp_path / ".runtime"
    manager_path = runtime / "provider_subchats.json"
    manager = ProviderSubchatManager(pcm._config, pcm, manager_path)

    subchat_id = "sub-transcript-test"
    # Append events
    manager.append_event(subchat_id, {"type": "message", "role": "owner", "content": "Hello"})
    manager.append_event(subchat_id, {"type": "message", "role": "participant", "content": "Hi there!"})
    # Append a malformed event (should be ignored on replay)
    event_dir = runtime / "provider_subchats"
    event_dir.mkdir(parents=True, exist_ok=True)
    event_file = event_dir / f"{subchat_id}.jsonl"
    with open(event_file, "a", encoding="utf-8") as f:
        f.write("{\n")  # Broken JSON

    manager.append_event(subchat_id, {"type": "message", "role": "owner", "content": "What's 2+2?"})

    events = manager.get_events(subchat_id)
    assert len(events) == 3
    assert events[0]["role"] == "owner"
    assert events[0]["content"] == "Hello"
    assert events[1]["role"] == "participant"
    assert events[2]["content"] == "What's 2+2?"


def test_provider_subchat_list_and_delete(tmp_path: Path) -> None:
    pcm = _make_pcm(tmp_path)
    runtime = tmp_path / ".runtime"
    manager_path = runtime / "provider_subchats.json"
    manager = ProviderSubchatManager(pcm._config, pcm, manager_path)

    owner = ProviderRoute(provider="claude", model="sonnet")
    participant = ProviderRoute(provider="codex", model="gpt-4")

    r1 = ProviderSubchatRecord(
        subchat_id="sub-1", parent_chat_id="chat-1", parent_turn_index=0,
        workspace="w", project_id="p", owner=owner, participant=participant, status="completed"
    )
    r2 = ProviderSubchatRecord(
        subchat_id="sub-2", parent_chat_id="chat-1", parent_turn_index=1,
        workspace="w", project_id="p", owner=owner, participant=participant, status="completed"
    )

    manager._records["sub-1"] = r1
    manager._records["sub-2"] = r2
    manager._save()

    # List by parent
    records = manager.list_records(parent_chat_id="chat-1")
    assert len(records) == 2

    # Append some events
    manager.append_event("sub-1", {"type": "message", "content": "test"})

    # Delete
    manager.delete_subchat("sub-1")
    assert manager.get_record("sub-1") is None
    assert not (runtime / "provider_subchats" / "sub-1.jsonl").exists()
    assert manager.get_record("sub-2") is not None


class FakeService:
    current_session_id = "fake-session-123"

    def __init__(self) -> None:
        self.stopped = False
        self.provider = None

    async def execute_streaming(self, _request) -> AsyncGenerator[StreamEvent, None]:
        yield AssistantTextDelta(type="assistant", text="2", phase="", parent_tool_use_id="")
        yield AssistantTextDelta(type="assistant", text="+2 is 4", phase="", parent_tool_use_id="")
        yield ResultEvent(
            type="assistant",
            result="2+2 is 4",
            is_error=False,
            effective_model="gpt-4",
            usage={"input_tokens": "10", "output_tokens": "5"},
            session_id="fake-session-123",
        )

    async def stop_active(self) -> bool:
        self.stopped = True
        return True

    async def disconnect(self) -> None:
        pass


@pytest.mark.anyio
async def test_provider_subchat_lifecycle_and_turn_execution(tmp_path: Path) -> None:
    pcm = _make_pcm(tmp_path)
    proj = pcm.create_project("test-subchats", workspace="personal")
    chat = pcm.create_chat(proj.project_id, model="opus", provider="claude")

    runtime = tmp_path / ".runtime"
    manager_path = runtime / "provider_subchats.json"
    manager = ProviderSubchatManager(pcm._config, pcm, manager_path)

    owner = ProviderRoute(provider="claude", model="opus")
    participant = ProviderRoute(provider="codex", model="gpt-4")

    # Create subchat
    record = manager.create_subchat(
        parent_chat_id=chat.chat_id,
        parent_turn_index=0,
        owner=owner,
        participant=participant,
    )
    assert record.status == "created"
    assert record.parent_chat_id == chat.chat_id

    # Inject mock service
    service = FakeService()
    manager._services[record.subchat_id] = service  # type: ignore[assignment]

    # Run turn
    res = await manager.run_consultation_turn(record.subchat_id, "What is 2+2?")
    assert res["status"] == "waiting_owner"
    assert res["reply"] == "2+2 is 4"
    assert res["usage"]["input_tokens"] == 10
    assert res["usage"]["output_tokens"] == 5

    # Check transcript events
    events = manager.get_events(record.subchat_id)
    assert len(events) == 4  # owner prompt + 2 text deltas + 1 result
    # Wait, in run_consultation_turn:
    # 1. append_event(owner_event) -> 1
    # 2. event_to_json -> text_delta -> append -> 2
    # 3. event_to_json -> text_delta -> append -> 3
    # 4. event_to_json -> result -> append -> 4
    assert len(events) == 4
    assert events[0]["role"] == "owner"
    assert events[0]["content"] == "What is 2+2?"
    assert events[1]["text"] == "2"

    # Test limit hit
    record.message_count = 12
    with pytest.raises(ValueError, match="Message limit reached"):
        await manager.run_consultation_turn(record.subchat_id, "Next prompt")

    # Extend limit
    manager.extend_subchat(record.subchat_id, user_authorized=True)
    assert record.limit_messages_extended == 12
    assert record.quota_limit_hit is False

    # Test cancel
    await manager.cancel_subchat(record.subchat_id)
    assert record.status == "cancelled"
    assert service.stopped is True
