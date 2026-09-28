"""Contract and ownership-boundary tests for scheduled-chat dispatch.

The schedule collaborator is deliberately runnable without a complete
``ProjectChatManager``.  These tests pin the two parts of that boundary that
are easy to regress during a lifecycle move: the outcome vocabulary is still
the one the schedule UI/job log consumes, and the collaborator calls the
manager's named seams instead of bypassing a test/integration patch.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ciao.config import CiaoConfig
from ciao.models import BridgeMode
from ciao.schedules import ScheduleEntry
from ciao.web.chat_broker import ChatStream, EventsHub
from ciao.web.project_chats import ArchiveOutcome, ChatInfo, ProjectChatManager, ProjectInfo
from ciao.web.schedule_dispatch import ScheduleDispatchHost, ScheduleDispatcher


class _Stream:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self._events = events

    async def subscribe(self):
        for event in self._events:
            yield event


class _ScheduleHost:
    """Typed-enough host implementing every dependency in the Protocol."""

    def __init__(self, tmp_path: Path, events: list[dict[str, object]]) -> None:
        self._config = cast(
            CiaoConfig,
            SimpleNamespace(workspace_root=tmp_path),
        )
        self._chats: dict[str, ChatInfo] = {
            "chat-1": ChatInfo(
                chat_id="chat-1",
                project_id="project-1",
                title="Scheduled",
                model="opus",
                mode="auto",
                provider="claude",
            )
        }
        self._projects: dict[str, ProjectInfo] = {
            "project-1": ProjectInfo(
                project_id="project-1",
                name="Holder",
                workspace="personal",
            )
        }
        self.schedule_store = None
        self._events_hub = EventsHub()
        self.events_published: list[dict[str, object]] = []
        self._events_hub.publish = self.events_published.append  # type: ignore[method-assign]
        self._stream_events = events
        self.saves = 0
        self.prepared: list[tuple[str, str, str, str]] = []
        self.started: list[tuple[str, str, bool]] = []
        self.awaits: list[tuple[str, float | None]] = []
        self.discarded: list[str] = []
        self.drain_waits: list[str] = []
        self.needs_user_calls: list[ScheduleEntry] = []
        self.archives: list[str] = []
        self.postprocess: list[tuple[str, ArchiveOutcome]] = []

    @property
    def events(self) -> EventsHub:
        return self._events_hub

    def _save(self, *, reason: str = "registry_mutation") -> None:
        del reason
        self.saves += 1

    def _agent_root_for_chat(self, chat_id: str) -> Path:
        assert chat_id == "chat-1"
        return self._config.workspace_root

    def _resolve_schedule_project(
        self, stale_id: str, entry: ScheduleEntry
    ) -> ProjectInfo | None:
        del stale_id, entry
        return self._projects["project-1"]

    def _rehome_interval_chat(
        self, entry: ScheduleEntry, prompt: str
    ) -> ChatInfo | None:
        del entry, prompt
        return self._chats["chat-1"]

    def create_chat(
        self,
        project_id: str,
        title: str = "New Chat",
        *,
        model: str | None = None,
        mode: str | None = None,
        provider: str | None = None,
    ) -> ChatInfo:
        chat = ChatInfo(
            chat_id="chat-new",
            project_id=project_id,
            title=title,
            model=model or "opus",
            mode=cast(BridgeMode, mode or "auto"),
            provider=provider or "claude",
        )
        self._chats[chat.chat_id] = chat
        return chat

    def schedule_default_provider(self, project_id: str | None) -> str:
        del project_id
        return "claude"

    def prepare_schedule_chat(
        self,
        entry: ScheduleEntry,
        prompt: str,
        model: str,
        mode: str,
        provider: str = "",
    ) -> str | None:
        self.prepared.append((entry.schedule_id, prompt, model, provider))
        return "chat-1"

    def start_stream(
        self,
        chat_id: str,
        prompt: str,
        images=None,
        *,
        is_retry: bool = False,
        unattended: bool = False,
    ) -> ChatStream:
        del images, is_retry
        self.started.append((chat_id, prompt, unattended))
        return cast(ChatStream, _Stream(self._stream_events))

    async def _await_schedule_subagents(
        self, chat_id: str, *, timeout_s: float = 900.0
    ) -> tuple[bool, bool]:
        self.awaits.append((chat_id, timeout_s))
        return True, False

    async def _schedule_run_needs_user(
        self, entry: ScheduleEntry, outcome
    ) -> bool:
        del outcome
        self.needs_user_calls.append(entry)
        return False

    async def _wait_for_drain_result(
        self, chat_id: str, *, timeout_s: float = 180.0
    ) -> tuple[str, bool] | None:
        del timeout_s
        self.drain_waits.append(chat_id)
        return None

    def _discard_schedule_drain_result(self, chat_id: str) -> None:
        self.discarded.append(chat_id)

    @staticmethod
    def _is_interim_subagent_text(text: str) -> bool:
        return "waiting on" in text.lower()

    async def archive_chat(self, chat_id: str) -> ArchiveOutcome | None:
        self.archives.append(chat_id)
        return None

    def run_archive_postprocess(
        self,
        chat_id: str,
        outcome: ArchiveOutcome,
        chat_meta: ChatInfo | None,
        project_meta: ProjectInfo | None,
    ) -> None:
        del chat_meta, project_meta
        self.postprocess.append((chat_id, outcome))


def _entry(*, archive_policy: str = "manual") -> ScheduleEntry:
    return ScheduleEntry(
        schedule_id="sched-contract",
        daily_time_utc="08:00",
        prompt="Run the routine.",
        chat_id=0,
        created_at="2026-09-01T00:00:00Z",
        frequency="daily",
        web_chat_id="chat-1",
        archive_policy=archive_policy,
        workspace="personal",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected_status"),
    [
        ([{"type": "result", "text": "done", "is_error": False}], "ok"),
        (
            [
                {"type": "permission_request"},
                {"type": "result", "text": "waiting", "is_error": False},
            ],
            "skipped",
        ),
        (
            [
                {
                    "type": "tool_use",
                    "tool_name": "AskUserQuestion",
                },
                {"type": "result", "text": "question", "is_error": False},
            ],
            "skipped",
        ),
        ([{"type": "error"}], "error"),
        ([{"type": "result", "text": "failed", "is_error": True}], "error"),
    ],
)
async def test_dispatch_keeps_schedule_outcome_vocabulary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[dict[str, object]],
    expected_status: str,
) -> None:
    host = _ScheduleHost(tmp_path, events)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))
    rows: list[object] = []
    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.job_runs.record_run",
        lambda row: rows.append(row),
    )

    result = await dispatcher.dispatch_schedule(
        _entry(), "Run the routine.", "opus", "auto", "claude"
    )

    assert result == {"chat_id": "chat-1", "status": expected_status}
    assert host.prepared == [("sched-contract", "Run the routine.", "opus", "claude")]
    assert host.started == [("chat-1", "Run the routine.", True)]
    if expected_status == "ok":
        assert host.awaits == [("chat-1", 900.0)]
        assert host.discarded == ["chat-1", "chat-1"]
    else:
        assert host.awaits == []
        assert host.discarded == []
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_auto_archive_stays_a_manager_lifecycle_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _ScheduleHost(
        tmp_path,
        [{"type": "result", "text": "done", "is_error": False}],
    )
    archive = ArchiveOutcome(
        path=tmp_path / "archive.md",
        session_id="session-1",
        turn_count=1,
        filtered_jsonl="{}",
    )

    async def archive_chat(chat_id: str) -> ArchiveOutcome | None:
        host.archives.append(chat_id)
        return archive

    host.archive_chat = archive_chat  # type: ignore[method-assign]
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))
    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.job_runs.record_run",
        lambda row: None,
    )

    result = await dispatcher.dispatch_schedule(
        _entry(archive_policy="auto"),
        "Run the routine.",
        "opus",
        "auto",
        "claude",
    )

    assert result == {
        "chat_id": "chat-1",
        "status": "ok",
        "archived_to": str(archive.path),
    }
    assert host.needs_user_calls == [_entry(archive_policy="auto")]
    assert host.archives == ["chat-1"]
    assert host.postprocess == [("chat-1", archive)]


@pytest.mark.asyncio
async def test_manager_methods_are_thin_schedule_dispatch_delegates() -> None:
    manager = object.__new__(ProjectChatManager)
    calls: list[tuple[str, object]] = []

    class _Dispatcher:
        def prepare_schedule_chat(self, *args, **kwargs):
            calls.append(("prepare", args))
            return "chat-1"

        async def _await_schedule_subagents(self, *args, **kwargs):
            calls.append(("await", args))
            return True, False

        async def _schedule_run_needs_user(self, *args, **kwargs):
            calls.append(("needs", args))
            return False

        async def dispatch_schedule(self, *args, **kwargs):
            calls.append(("dispatch", args))
            return {"chat_id": "chat-1", "status": "ok"}

    manager._schedule_dispatcher = _Dispatcher()  # type: ignore[assignment]
    entry = _entry()

    assert manager.prepare_schedule_chat(entry, "p", "opus", "auto", "claude") == "chat-1"
    assert await manager._await_schedule_subagents("chat-1", timeout_s=2) == (True, False)
    assert await manager._schedule_run_needs_user(entry, SimpleNamespace()) is False
    assert await manager.dispatch_schedule(entry, "p", "opus", "auto", "claude") == {
        "chat_id": "chat-1",
        "status": "ok",
    }
    assert [name for name, _ in calls] == ["prepare", "await", "needs", "dispatch"]


@pytest.mark.asyncio
async def test_dispatch_calls_manager_patches_instead_of_local_implementations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _ScheduleHost(
        tmp_path,
        [{"type": "result", "text": "done", "is_error": False}],
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))
    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.job_runs.record_run",
        lambda row: None,
    )

    async def patched_await(chat_id: str, *, timeout_s: float = 900.0):
        host.awaits.append((chat_id, timeout_s))
        return True, False

    host._await_schedule_subagents = patched_await  # type: ignore[method-assign]
    monkeypatch.setattr(
        dispatcher,
        "prepare_schedule_chat",
        lambda *args, **kwargs: pytest.fail("local preparation bypassed manager seam"),
    )
    monkeypatch.setattr(
        dispatcher,
        "_await_schedule_subagents",
        lambda *args, **kwargs: pytest.fail("local wait bypassed manager seam"),
    )

    result = await dispatcher.dispatch_schedule(
        _entry(), "Run the routine.", "opus", "auto", "claude"
    )

    assert result["status"] == "ok"
    assert host.awaits == [("chat-1", 900.0)]
