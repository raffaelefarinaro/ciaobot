"""Contract and ownership-boundary tests for scheduled-chat dispatch.

The schedule collaborator is deliberately runnable without a complete
``ProjectChatManager``.  These tests pin the two parts of that boundary that
are easy to regress during a lifecycle move: the outcome vocabulary is still
the one the schedule UI/job log consumes, and the collaborator calls the
manager's named seams instead of bypassing a test/integration patch.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ciao import curation_run
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.job_runs import JobRun
from ciao.models import BridgeMode
from ciao.schedule_command import CommandRun
from ciao.schedules import ScheduleEntry
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_broker import ChatStream, EventsHub
from ciao.web.project_chats import ArchiveOutcome, ChatInfo, ProjectChatManager, ProjectInfo
from ciao.web.schedule_dispatch import ScheduleDispatchHost, ScheduleDispatcher


class _Stream:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self._events = events

    async def subscribe(self):
        for event in self._events:
            yield event


class _MutatingStream(_Stream):
    """Move the target after the turn started, then finish normally."""

    def __init__(self, host: "_ScheduleHost", events: list[dict[str, object]]) -> None:
        super().__init__(events)
        self._host = host

    async def subscribe(self):
        self._host._projects["project-1"].workspace = "work"
        async for event in super().subscribe():
            yield event


class _ScheduleHost:
    """Typed-enough host implementing every dependency in the Protocol."""

    def __init__(self, tmp_path: Path, events: list[dict[str, object]]) -> None:
        self.vault_root_calls: list[str | None] = []
        self._config = cast(
            CiaoConfig,
            SimpleNamespace(
                workspace_root=tmp_path,
                workspace_vault_root=self._workspace_vault_root,
            ),
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
        self.command_workspace = "personal"
        self._events_hub = EventsHub()
        self.events_published: list[dict[str, object]] = []
        self._events_hub.publish = self.events_published.append  # type: ignore[method-assign]
        self._stream_events = events
        self.saves = 0
        self.prepared: list[tuple[str, str, str, str]] = []
        self.started: list[tuple[str, str, bool]] = []
        self.origins: list[str] = []
        self.awaits: list[tuple[str, float | None]] = []
        self.discarded: list[str] = []
        self.drain_waits: list[str] = []
        self.needs_user_calls: list[ScheduleEntry] = []
        self.archives: list[str] = []
        self.postprocess: list[tuple[str, ArchiveOutcome]] = []

    @property
    def events(self) -> EventsHub:
        return self._events_hub

    def _workspace_vault_root(self, workspace: str | None) -> Path:
        self.vault_root_calls.append(workspace)
        return self._config.workspace_root / "vault"

    def _save(self, *, reason: str = "registry_mutation") -> None:
        del reason
        self.saves += 1

    def _agent_root_for_chat(self, chat_id: str) -> Path:
        assert chat_id == "chat-1"
        return self._config.workspace_root

    def schedule_command_env(self, entry: ScheduleEntry) -> tuple[Path, dict[str, str]]:
        del entry
        return self._config.workspace_root, {
            "CIAO_WORKSPACE": str(self._config.workspace_root),
            "CIAO_ACTIVE_WORKSPACE": self.command_workspace,
        }

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
        origin: str = "",
    ) -> ChatStream:
        del images, is_retry
        self.started.append((chat_id, prompt, unattended))
        self.origins.append(origin)
        return cast(ChatStream, _Stream(self._stream_events))

    async def _await_schedule_subagents(
        self, chat_id: str, *, timeout_s: float = 900.0
    ) -> tuple[bool, bool]:
        self.awaits.append((chat_id, timeout_s))
        return True, False

    async def _schedule_run_needs_user(
        self, entry: ScheduleEntry, outcome, *, chat_id: str = ""
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


def _capture_run_records(monkeypatch: pytest.MonkeyPatch) -> list[JobRun]:
    rows: list[JobRun] = []
    monkeypatch.setattr("ciao.web.schedule_dispatch.job_runs.record_run", rows.append)
    return rows


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
    assert host.origins == ["schedule"]
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
        turn_count=1,
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


def _curation_entry(*, archive_policy: str = "manual") -> ScheduleEntry:
    return dataclasses.replace(
        _entry(archive_policy=archive_policy),
        schedule_id="system-memory-curation@personal",
    )


class _LeaseTakingStream(_Stream):
    """A turn that takes and releases the curation lease while it streams.

    ``_sched_started`` is sampled inside ``dispatch_schedule``, before the
    stream is consumed, so the state file has to be written from inside
    ``subscribe`` for the stamp to fall inside the run's own window. No sleep:
    the lease stamps are compared against a second-resolution floor.
    """

    def __init__(self, events: list[dict[str, object]], vault: Path) -> None:
        super().__init__(events)
        self._vault = vault

    async def subscribe(self):
        lease = curation_run.begin_run(self._vault, holder="t")
        curation_run.end_run(self._vault, holder=lease.holder)
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_curation_run_without_lease_records_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean turn that never took the lease did not do its job (#866).

    ``ciao`` off the agent's PATH leaves the agent saying "nothing happened"
    and ending the turn without a single error event, which the dispatcher can
    only see as a healthy run.
    """
    host = _ScheduleHost(
        tmp_path,
        [{"type": "result", "text": "nothing happened", "is_error": False}],
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))
    rows: list[object] = []
    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.job_runs.record_run",
        lambda row: rows.append(row),
    )

    result = await dispatcher.dispatch_schedule(
        _curation_entry(archive_policy="auto"),
        "Run the routine.",
        "opus",
        "auto",
        "claude",
    )

    assert result["status"] == "error"
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "error"
    assert "curation lease was never taken" in row.error
    # An unclean run must stay visible, not be swept into the archive.
    assert host.archives == []


@pytest.mark.asyncio
async def test_curation_run_that_took_the_lease_stays_ok(
    tmp_path: Path,
) -> None:
    host = _ScheduleHost(
        tmp_path,
        [{"type": "result", "text": "curated", "is_error": False}],
    )
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    host.start_stream = (  # type: ignore[method-assign]
        lambda chat_id, prompt, images=None, *, is_retry=False, unattended=False, origin="": cast(
            ChatStream, _LeaseTakingStream(host._stream_events, vault)
        )
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    result = await dispatcher.dispatch_schedule(
        _curation_entry(), "Run the routine.", "opus", "auto", "claude"
    )

    assert result["status"] == "ok"
    assert host.vault_root_calls == ["personal"]


@pytest.mark.asyncio
async def test_curation_run_with_other_holder_live_lease_stays_ok(
    tmp_path: Path,
) -> None:
    """Standing down because another run holds the lease is not a failure."""
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    started = datetime.now(UTC) - timedelta(seconds=5)
    curation_run.begin_run(vault, holder="other", now=started)
    host = _ScheduleHost(
        tmp_path,
        [{"type": "result", "text": "nothing happened", "is_error": False}],
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    result = await dispatcher.dispatch_schedule(
        _curation_entry(), "Run the routine.", "opus", "auto", "claude"
    )

    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_other_schedules_never_read_curation_state(
    tmp_path: Path,
) -> None:
    """The lease check is scoped to the curation routine by schedule id."""
    host = _ScheduleHost(
        tmp_path,
        [{"type": "result", "text": "done", "is_error": False}],
    )

    def _refuse(workspace: str | None) -> Path:
        raise AssertionError(f"vault state read for {workspace!r}")

    host._config.workspace_vault_root = _refuse  # type: ignore[method-assign]
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    result = await dispatcher.dispatch_schedule(
        _entry(), "Run the routine.", "opus", "auto", "claude"
    )

    assert result["status"] == "ok"
    assert host.vault_root_calls == []


@pytest.mark.asyncio
async def test_schedule_dispatch_records_run_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(
        tmp_path, [{"type": "result", "text": "done", "is_error": False}]
    )
    rows = _capture_run_records(monkeypatch)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        _entry(), "Run the routine.", "opus", "auto", "claude"
    )

    assert rows[0].extra["workspace"] == "personal"


@pytest.mark.asyncio
async def test_schedule_dispatch_target_workspace_overrides_stale_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(
        tmp_path, [{"type": "result", "text": "done", "is_error": False}]
    )
    rows = _capture_run_records(monkeypatch)
    entry = _entry()
    entry.workspace = "stale-work"
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, "Run the routine.", "opus", "auto", "claude"
    )

    assert rows[0].extra["workspace"] == "personal"


@pytest.mark.asyncio
async def test_schedule_dispatch_workspace_does_not_change_during_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(
        tmp_path, [{"type": "result", "text": "done", "is_error": False}]
    )
    rows = _capture_run_records(monkeypatch)
    host.start_stream = (  # type: ignore[method-assign]
        lambda chat_id, prompt, images=None, *, is_retry=False, unattended=False, origin="": cast(
            ChatStream, _MutatingStream(host, host._stream_events)
        )
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        _entry(), "Run the routine.", "opus", "auto", "claude"
    )

    assert host._projects["project-1"].workspace == "work"
    assert rows[0].extra["workspace"] == "personal"


@pytest.mark.asyncio
async def test_schedule_dispatch_records_empty_workspace_when_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(
        tmp_path, [{"type": "result", "text": "done", "is_error": False}]
    )
    host._projects["project-1"].workspace = ""
    rows = _capture_run_records(monkeypatch)
    entry = _entry()
    entry.workspace = "stale-work"
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, "Run the routine.", "opus", "auto", "claude"
    )

    assert rows[0].extra["workspace"] == ""


@pytest.mark.asyncio
async def test_schedule_dispatch_command_to_turn_mixed_workspace_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(
        tmp_path, [{"type": "result", "text": "done", "is_error": False}]
    )
    rows = _capture_run_records(monkeypatch)
    entry = _entry()
    entry.command = "probe"

    async def _command(*args: object, **kwargs: object) -> CommandRun:
        del args, kwargs
        host._projects["project-1"].workspace = "work"
        return CommandRun(
            command="probe",
            exit_code=0,
            stdout="command output\n",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.run_schedule_command", _command
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, "Run the routine.", "opus", "auto", "claude"
    )

    assert host._projects["project-1"].workspace == "work"
    assert rows[0].extra["workspace"] == ""


@pytest.mark.asyncio
async def test_schedule_dispatch_command_with_unknown_prompt_owner_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(
        tmp_path, [{"type": "result", "text": "done", "is_error": False}]
    )
    host._projects.clear()
    rows = _capture_run_records(monkeypatch)
    entry = _entry()
    entry.command = "probe"
    entry.workspace = "original"

    async def _command(*args: object, **kwargs: object) -> CommandRun:
        del args, kwargs
        entry.workspace = "mutated"
        return CommandRun(
            command="probe",
            exit_code=0,
            stdout="command output\n",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.run_schedule_command", _command
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, "Run the routine.", "opus", "auto", "claude"
    )

    assert entry.workspace == "mutated"
    assert rows[0].extra["workspace"] == ""


class _RealManagerHost(_ScheduleHost):
    """Use the real manager for command-env and prepared-target selection."""

    def __init__(
        self, manager: ProjectChatManager, events: list[dict[str, object]]
    ) -> None:
        super().__init__(manager._config.workspace_root, events)
        self.manager = manager
        self._config = manager._config
        self._chats = manager._chats
        self._projects = manager._projects

    def schedule_command_env(self, entry: ScheduleEntry) -> tuple[Path, dict[str, str]]:
        return self.manager.schedule_command_env(entry)

    def prepare_schedule_chat(
        self,
        entry: ScheduleEntry,
        prompt: str,
        model: str,
        mode: BridgeMode,
        provider: str = "",
    ) -> str | None:
        return self.manager.prepare_schedule_chat(
            entry, prompt, model, mode, provider
        )


def _workspace_pair_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _both_targets(tmp_path: Path):
    manager = _workspace_pair_manager(tmp_path)
    personal_project = manager.create_project("Personal target", workspace="personal")
    work_project = manager.create_project("Work target", workspace="work")
    personal_chat = manager.create_chat(
        personal_project.project_id, title="Personal chat", provider="claude"
    )
    entry = _entry()
    entry.web_chat_id = personal_chat.chat_id
    entry.web_project_id = work_project.project_id
    entry.workspace = "personal"
    return manager, entry, personal_chat, work_project


@pytest.mark.asyncio
async def test_real_manager_quiet_command_records_command_env_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, entry, _personal_chat, _work_project = _both_targets(tmp_path)
    host = _RealManagerHost(
        manager, [{"type": "result", "text": "unused", "is_error": False}]
    )
    rows = _capture_run_records(monkeypatch)
    entry.command = "probe"
    seen: dict[str, object] = {}

    async def _command(command: str, cwd: Path, env: dict[str, str]) -> CommandRun:
        seen["cwd"] = cwd
        seen["env"] = dict(env)
        return CommandRun(
            command=command,
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.run_schedule_command", _command
    )

    def _refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("quiet command must not prepare or resolve a target")

    monkeypatch.setattr(manager, "prepare_schedule_chat", _refuse)
    monkeypatch.setattr(manager, "create_chat", _refuse)
    monkeypatch.setattr(manager, "_resolve_schedule_project", _refuse)
    monkeypatch.setattr(manager, "_rehome_interval_chat", _refuse)
    chats_before = set(manager._chats)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, "Run the routine.", "opus", "auto", "claude"
    )

    command_env = cast(dict[str, str], seen["env"])
    assert command_env["CIAO_ACTIVE_WORKSPACE"] == "personal"
    assert Path(cast(Path, seen["cwd"])) == manager._config.agent_root("personal")
    assert rows[0].extra["workspace"] == "personal"
    assert host.started == []
    assert set(manager._chats) == chats_before


@pytest.mark.asyncio
async def test_real_manager_command_to_project_turn_mixed_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, entry, _personal_chat, work_project = _both_targets(tmp_path)
    host = _RealManagerHost(
        manager, [{"type": "result", "text": "done", "is_error": False}]
    )
    rows = _capture_run_records(monkeypatch)
    entry.command = "probe"
    seen: dict[str, object] = {}

    async def _command(command: str, cwd: Path, env: dict[str, str]) -> CommandRun:
        seen["cwd"] = cwd
        seen["env"] = dict(env)
        return CommandRun(
            command=command,
            exit_code=0,
            stdout="command output\n",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.run_schedule_command", _command
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, "Run the routine.", "opus", "auto", "claude"
    )

    target_id = host.started[0][0]
    target_chat = manager._chats[target_id]
    command_env = cast(dict[str, str], seen["env"])
    assert target_chat.project_id == work_project.project_id
    assert manager._projects[target_chat.project_id].workspace == "work"
    assert command_env["CIAO_ACTIVE_WORKSPACE"] == "personal"
    assert Path(cast(Path, seen["cwd"])) == manager._config.agent_root("personal")
    assert rows[0].extra["workspace"] == ""


@pytest.mark.asyncio
async def test_schedule_dispatch_prompt_owner_change_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(
        tmp_path, [{"type": "result", "text": "done", "is_error": False}]
    )
    rows = _capture_run_records(monkeypatch)
    entry = _entry()
    entry.prompt = "{{ERROR_LOG}}"

    def _error_log(*args: object, **kwargs: object) -> str:
        del args, kwargs
        host._projects["project-1"].workspace = "work"
        return ""

    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.tail_error_log", _error_log
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, entry.prompt, "opus", "auto", "claude"
    )

    assert host._projects["project-1"].workspace == "work"
    assert rows[0].extra["workspace"] == ""


@pytest.mark.asyncio
async def test_schedule_dispatch_error_log_placeholder_is_unscoped_on_model_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "SYNTH-BETA-GLOBAL-ERROR"
    host = _ScheduleHost(tmp_path, [{"type": "error"}])
    rows = _capture_run_records(monkeypatch)
    entry = _entry()
    entry.prompt = "{{ERROR_LOG}}"
    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.tail_error_log",
        lambda *args, **kwargs: marker,
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, entry.prompt, "opus", "auto", "claude"
    )

    assert marker in host.started[0][1]
    assert rows[0].status == "error"
    assert rows[0].extra["workspace"] == ""


@pytest.mark.asyncio
async def test_schedule_dispatch_issue_report_placeholder_is_unscoped_on_model_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "SYNTH-BETA-GLOBAL-REPORT"
    host = _ScheduleHost(tmp_path, [{"type": "error"}])
    rows = _capture_run_records(monkeypatch)
    entry = _entry()
    entry.prompt = "{{ISSUE_REPORT}}"
    monkeypatch.setattr(
        "ciao.debug_report.build_issue_report",
        lambda *args, **kwargs: {"report_text": marker},
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, entry.prompt, "opus", "auto", "claude"
    )

    assert marker in host.started[0][1]
    assert rows[0].status == "error"
    assert rows[0].extra["workspace"] == ""


@pytest.mark.asyncio
async def test_schedule_dispatch_command_turn_with_global_placeholder_is_unscoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "SYNTH-BETA-COMMAND-GLOBAL-ERROR"
    host = _ScheduleHost(tmp_path, [{"type": "error"}])
    rows = _capture_run_records(monkeypatch)
    entry = _entry()
    entry.command = "probe"
    entry.prompt = "{{ERROR_LOG}}"

    async def _command(*args: object, **kwargs: object) -> CommandRun:
        del args, kwargs
        return CommandRun(
            command="probe",
            exit_code=0,
            stdout="command output\n",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.run_schedule_command", _command
    )
    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.tail_error_log",
        lambda *args, **kwargs: marker,
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        entry, entry.prompt, "opus", "auto", "claude"
    )

    assert marker in host.started[0][1]
    assert rows[0].status == "error"
    assert rows[0].extra["workspace"] == ""


@pytest.mark.asyncio
async def test_schedule_dispatch_no_placeholder_error_run_keeps_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(tmp_path, [{"type": "error"}])
    rows = _capture_run_records(monkeypatch)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(
        _entry(), "Run the routine.", "opus", "auto", "claude"
    )

    assert rows[0].status == "error"
    assert rows[0].extra["workspace"] == "personal"
