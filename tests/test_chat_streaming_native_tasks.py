"""A Claude chat's native background tasks re-arm the watcher after a wake turn."""

from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk import TaskStartedMessage

from ciao.config import CiaoConfig
from ciao.models import ResultEvent
from ciao.provider_service import ProviderService
from ciao.providers.claude import ClaudeProvider
from ciao.web.chat_streaming import ChatStreaming
from tests.test_opencode_followups import _RecordingDriveHost


@pytest.mark.asyncio
async def test_wake_turn_rearms_watcher_for_claude_native_tasks(tmp_path: Path) -> None:
    host: _RecordingDriveHost = _RecordingDriveHost([], tmp_path)
    started: list[tuple[str, str]] = []
    host._start_subagent_watcher = lambda c, p: started.append((c, p))  # type: ignore[method-assign]
    host._background_count_now = lambda _c: 1  # type: ignore[attr-defined]
    host._last_drain = None

    provider = ClaudeProvider(tmp_path)
    provider._observe_message(
        TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="bash-1",
            description="sleep 600",
            uuid="u",
            session_id="s",
            task_type="local_bash",
        )
    )
    service = ProviderService(
        CiaoConfig(
            pwa_auth_token="t",
            workspace_root=tmp_path,
            state_path=tmp_path / "state.json",
            media_root=tmp_path / "media",
        ),
        provider="claude",
    )
    service._provider = provider  # type: ignore[assignment]

    async def drain_events():
        yield ResultEvent(
            type="result", result="BGDONE", session_id="s", origin="task-notification"
        )

    service.drain_events = drain_events  # type: ignore[method-assign]
    host._providers = {"chat-1": service}  # type: ignore[assignment]
    streaming = ChatStreaming(host)  # type: ignore[arg-type]

    await streaming.drain_between_turns("chat-1", "project-1")

    assert started == [("chat-1", "project-1")]
