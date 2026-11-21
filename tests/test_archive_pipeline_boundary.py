"""Contract tests for the archive pipeline collaborator.

These drive ``ArchivePipeline`` with a small typed host, so what happens after
an archive is written stays explicit: the memory pass is queued (or not) and
the archive is indexed for search, both through the host protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from ciao import archive_capture
from ciao.app_settings import AppSettingsStore
from ciao.config import CiaoConfig
from ciao.context.capsule import build_context_capsule
from ciao.models import AgentRequest, ChatContext
from ciao.task_attempts import TaskAttemptError, TaskAttemptStore
from ciao.transcripts import TranscriptStore
from ciao.web import archive_pipeline, memory_pass
from ciao.web.archive_pipeline import ArchivePipeline, ArchivePipelineHost
from ciao.web.chat_broker import EventsHub
from ciao.web.project_chats import ArchiveOutcome, ChatInfo, ProjectInfo


class _Host:
    """The manager surface ``ArchivePipeline`` needs for these contracts."""

    def __init__(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self._config = CiaoConfig(
            pwa_auth_token="test-token",
            workspace_root=tmp_path,
            state_path=runtime / "state.json",
            media_root=runtime / "media",
        )
        self._detached_tasks: set[asyncio.Task[object]] = set()
        self._chats: dict[str, ChatInfo] = {}
        self._projects: dict[str, ProjectInfo] = {}
        self._events_hub = EventsHub()
        self.published: list[dict[str, object]] = []
        self._events_hub.publish = self.published.append  # type: ignore[method-assign]
        self.index_calls: list[str] = []
        self.enqueued: list[tuple[str, Path, str]] = []
        self.focuses: list[dict[str, str] | None] = []

    @property
    def events(self) -> EventsHub:
        return self._events_hub

    def _workspace_vault_root(self, workspace: str) -> Path:
        return self._config.workspace_root / "vault" / workspace

    def attempt_store(self, workspace: str) -> TaskAttemptStore:
        return TaskAttemptStore(
            workspace=workspace,
            runtime_dir=Path(self._config.state_path).parent,
            clock=lambda: datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
        )

    def _spawn_detached(
        self, coro: Coroutine[object, object, object], name: str
    ) -> asyncio.Task[object]:
        del name
        task = asyncio.create_task(coro)
        self._detached_tasks.add(task)
        task.add_done_callback(self._detached_tasks.discard)
        return task

    def enqueue_memory_pass(
        self,
        source: ChatInfo,
        project: ProjectInfo | None,
        archive_path: Path,
        doc_path: str,
        focus: dict[str, str] | None = None,
    ) -> str | None:
        del project
        self.enqueued.append((source.chat_id, archive_path, doc_path))
        self.focuses.append(focus)
        return "memory-chat"

    def _make_archive_index_operation(
        self, outcome: ArchiveOutcome
    ) -> Callable[[], None]:
        del outcome
        return lambda: self.index_calls.append("index")

    def _run_archive_index_best_effort(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> None:
        del chat_id, outcome
        operation()

    async def _index_archive_file_off_loop(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> None:
        del chat_id, outcome
        operation()


def _setup(
    tmp_path: Path,
) -> tuple[_Host, ArchivePipeline, ChatInfo, ProjectInfo, ArchiveOutcome]:
    host = _Host(tmp_path)
    chat = ChatInfo(chat_id="chat-1", project_id="project-1", title="Archived", archived=True)
    project = ProjectInfo(
        project_id="project-1",
        name="Holder",
        workspace="personal",
        vault_folder="holder",
    )
    host._chats[chat.chat_id] = chat
    host._projects[project.project_id] = project
    archive = tmp_path / "archive.md"
    archive.write_text("# archived\n", encoding="utf-8")
    outcome = ArchiveOutcome(path=archive, turn_count=2)
    return host, ArchivePipeline(cast(ArchivePipelineHost, host)), chat, project, outcome


@pytest.mark.asyncio
async def test_archive_queues_the_memory_pass_and_indexes(tmp_path: Path) -> None:
    host, pipeline, chat, project, outcome = _setup(tmp_path)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == [(chat.chat_id, outcome.path, project.vault_doc_path)]
    assert host.index_calls == ["index"]
    # The archive path is stamped on the chat when the caller had not yet.
    assert chat.archive_path == "archive.md"


@pytest.mark.asyncio
async def test_archive_enqueues_the_pass_even_with_a_persisted_opt_out(
    tmp_path: Path,
) -> None:
    host, pipeline, chat, project, outcome = _setup(tmp_path)
    path = tmp_path / ".runtime" / "app_settings.json"
    path.write_text('{"insights_enabled": false}', encoding="utf-8")
    AppSettingsStore(path).apply_to_config(host._config)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == [(chat.chat_id, outcome.path, project.vault_doc_path)]


def _real_archive(tmp_path: Path, prompts: list[str]) -> Path:
    """Archive one turn per prompt through the transcript store, for real."""
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "chat-archive")
    for prompt in prompts:
        store.record_turn(
            AgentRequest(
                prompt=prompt,
                model="sonnet",
                mode="bypass",
                resume_session=None,
                images=[],
            ),
            ctx=ChatContext(chat_id=1),
            response_text="ok",
            effective_model="sonnet",
            session_id="ses_1",
            usage={},
            quota={},
            input_kind="text",
        )
    archived = store.archive_session(
        ctx=ChatContext(chat_id=1),
        active_model="sonnet",
        last_effective_model="sonnet",
        session_id="ses_1",
    )
    assert archived is not None
    return archived


def _automated(text: str) -> str:
    capsule = build_context_capsule(unattended=True)
    return f"[CIAO_CONTEXT_BEGIN]\n{capsule}\n[CIAO_CONTEXT_END]\n\n{text}"


@pytest.mark.asyncio
async def test_an_all_unattended_archive_queues_no_pass(tmp_path: Path) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    archive = _real_archive(tmp_path, [_automated("Curate"), _automated("Care")])
    outcome = ArchiveOutcome(path=archive, turn_count=2)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == []
    assert host.index_calls == ["index"]


@pytest.mark.asyncio
async def test_a_focused_pass_on_an_all_unattended_archive_still_runs(
    tmp_path: Path,
) -> None:
    """A pass the owner asked for is never skipped by the unattended gate."""
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    archive = _real_archive(tmp_path, [_automated("Curate")])
    outcome = ArchiveOutcome(path=archive, turn_count=1)

    pipeline.run_archive_postprocess(
        chat.chat_id, outcome, chat, project, focus={"kind": "approved_task"}
    )
    await asyncio.sleep(0)

    assert host.enqueued == [(chat.chat_id, archive, project.vault_doc_path)]


@pytest.mark.asyncio
async def test_an_archive_with_one_human_turn_queues_the_pass(tmp_path: Path) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    archive = _real_archive(
        tmp_path, [_automated("Curate"), "Remember that Acme kickoff moved"]
    )
    outcome = ArchiveOutcome(path=archive, turn_count=2)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == [(chat.chat_id, archive, project.vault_doc_path)]


@pytest.mark.asyncio
async def test_a_normal_archive_queues_the_pass(tmp_path: Path) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    archive = _real_archive(tmp_path, ["Plan the Q3 review"])
    outcome = ArchiveOutcome(path=archive, turn_count=1)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == [(chat.chat_id, archive, project.vault_doc_path)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schedule_id", ["system-memory-curation", "system-memory-curation@personal"]
)
async def test_a_system_schedule_archive_queues_no_pass(
    tmp_path: Path, schedule_id: str
) -> None:
    """Workspace care is the memory machinery: even a human turn in its chat
    (a retried run once replayed as attended) does not buy a pass over it."""
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    chat.schedule_id = schedule_id
    archive = _real_archive(tmp_path, [_automated("Care"), "looks good, carry on"])
    outcome = ArchiveOutcome(path=archive, turn_count=2)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == []
    assert host.index_calls == ["index"]


@pytest.mark.asyncio
async def test_a_focused_pass_on_a_system_schedule_archive_still_runs(
    tmp_path: Path,
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    chat.schedule_id = "system-memory-curation@personal"
    archive = _real_archive(tmp_path, ["Care"])
    outcome = ArchiveOutcome(path=archive, turn_count=1)

    pipeline.run_archive_postprocess(
        chat.chat_id, outcome, chat, project, focus={"kind": "approved_task"}
    )
    await asyncio.sleep(0)

    # A system-schedule chat folds into no project doc.
    assert host.enqueued == [(chat.chat_id, archive, "")]


@pytest.mark.asyncio
async def test_a_user_schedule_archive_with_a_human_turn_queues_the_pass(
    tmp_path: Path,
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    chat.schedule_id = "daily-briefing"
    archive = _real_archive(tmp_path, [_automated("Brief me"), "move the 3pm"])
    outcome = ArchiveOutcome(path=archive, turn_count=2)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == [(chat.chat_id, archive, project.vault_doc_path)]


@pytest.mark.asyncio
async def test_an_unparseable_archive_still_queues_the_pass(tmp_path: Path) -> None:
    host, pipeline, chat, project, outcome = _setup(tmp_path)
    outcome.path.write_text("not a transcript at all\n", encoding="utf-8")

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == [(chat.chat_id, outcome.path, project.vault_doc_path)]


@pytest.mark.asyncio
async def test_an_empty_archive_queues_no_pass(tmp_path: Path) -> None:
    host, pipeline, chat, project, outcome = _setup(tmp_path)
    empty = ArchiveOutcome(path=outcome.path, turn_count=0)

    pipeline.run_archive_postprocess(chat.chat_id, empty, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == []
    assert host.index_calls == ["index"]


def test_synchronous_callers_index_inline(tmp_path: Path) -> None:
    host, pipeline, chat, project, outcome = _setup(tmp_path)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)

    assert host.index_calls == ["index"]


def test_publish_postprocess_announces_the_record(tmp_path: Path) -> None:
    host, pipeline, chat, _project, _outcome = _setup(tmp_path)
    chat.postprocess = {"steps": {"memory_pass": {"status": "queued", "extra": {}}}}

    pipeline._publish_postprocess(chat)

    assert host.published == [
        {
            "type": "chat_postprocess",
            "chat_id": chat.chat_id,
            "project_id": chat.project_id,
            "postprocess": chat.postprocess,
        }
    ]


# ── Archive capture record (#3239-c2) ───────────────────────────────

_FIXED_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
_COMPLETION = "d" * 32


class _Clock:
    """Stands in for ``datetime`` in the pipeline: each ``now`` returns the next instant."""

    instants: list[datetime] = []

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        del tz
        return cls.instants.pop(0) if len(cls.instants) > 1 else cls.instants[0]


def _capture_spy(monkeypatch: pytest.MonkeyPatch) -> list[archive_capture.CaptureRecord]:
    written: list[archive_capture.CaptureRecord] = []
    _Clock.instants = [_FIXED_NOW]
    monkeypatch.setattr(archive_pipeline, "datetime", _Clock)
    monkeypatch.setattr(
        archive_pipeline,
        "append_capture",
        lambda vault_root, record: written.append(record),
        raising=True,
    )
    return written


def _seed_two_attempts_and_decisions(host: _Host, chat_id: str) -> tuple[str, ...]:
    """Two attempts in one chat, and 12 decisions for the first plus one for an unrelated attempt."""
    store = host.attempt_store("personal")
    first = store.start(task_id="a" * 32, task_revision="c" * 64, chat_id=chat_id)
    second = store.start(task_id="b" * 32, task_revision="c" * 64, chat_id=chat_id)
    attempt_ids = tuple(a.attempt_id for a in store.attempts_for_chat(chat_id))
    assert set(attempt_ids) == {first.attempt.attempt_id, second.attempt.attempt_id}
    rows = [_decision_row(first.attempt.attempt_id, n) for n in range(1, 13)]
    rows.append(_decision_row("2" * 32, 99))
    _write_decisions(host, rows)
    return attempt_ids


def _decision_row(attempt_id: str, n: int) -> str:
    return json.dumps({
        "v": 1, "id": f"{n:032x}", "at": "2026-11-10T10:00:00+00:00", "kind": "reject",
        "task_id": "a" * 32, "task_title": "Title", "task_created_by": "agent",
        "project_id": "p1", "attempt_id": attempt_id, "attempt_state": "ready_for_review",
        "actor": "user", "via": "pwa", "reason": "Not mine to release.",
    })


def _write_decisions(host: _Host, rows: list[str]) -> None:
    path = host._config.workspace_root / "vault" / "personal" / "Workspace" / "Task-Decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(row + "\n" for row in rows), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_queued_archive_hands_one_exact_record_to_the_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    written = _capture_spy(monkeypatch)
    attempt_ids = _seed_two_attempts_and_decisions(host, chat.chat_id)
    archive = _real_archive(tmp_path, ["Plan the Q3 review", "and the budget"])
    outcome = ArchiveOutcome(path=archive, turn_count=2)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert written == [
        archive_capture.CaptureRecord(
            id=archive_capture.capture_id("personal", chat.chat_id, "archive", ""),
            kind="archive",
            workspace="personal",
            chat_id=chat.chat_id,
            captured_at=_FIXED_NOW,
            turn_count=2,
            attempt_ids=attempt_ids,
            attempts_status="ok",
            decision_ids=tuple(f"{n:032x}" for n in range(12, 0, -1)),
            decisions_status="ok",
            omitted_decisions=0,
            completion_id="",
            complete=True,
        )
    ]
    assert host.enqueued == [(chat.chat_id, archive, project.vault_doc_path)]


@pytest.mark.asyncio
async def test_a_chat_with_no_attempt_captures_empty_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    written = _capture_spy(monkeypatch)
    archive = _real_archive(tmp_path, ["Plan the Q3 review"])

    pipeline.run_archive_postprocess(
        chat.chat_id, ArchiveOutcome(path=archive, turn_count=1), chat, project
    )

    assert [(r.attempt_ids, r.attempts_status, r.decision_ids, r.complete) for r in written] == [
        ((), "ok", (), True)
    ]


@pytest.mark.asyncio
async def test_an_unreadable_attempt_store_captures_unreadable_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    written = _capture_spy(monkeypatch)

    class _Broken:
        def attempts_for_chat(self, chat_id: str) -> tuple[()]:
            raise TaskAttemptError("unreadable", f"store for {chat_id} is broken")

    monkeypatch.setattr(host, "attempt_store", lambda workspace: _Broken(), raising=False)
    archive = _real_archive(tmp_path, ["Plan the Q3 review"])

    pipeline.run_archive_postprocess(
        chat.chat_id, ArchiveOutcome(path=archive, turn_count=1), chat, project
    )

    assert [(r.attempt_ids, r.attempts_status, r.complete) for r in written] == [
        ((), "unreadable", False)
    ]
    assert host.enqueued == [(chat.chat_id, archive, project.vault_doc_path)]


@pytest.mark.asyncio
async def test_a_malformed_decisions_file_captures_its_status_and_keeps_valid_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    written = _capture_spy(monkeypatch)
    store = host.attempt_store("personal")
    started = store.start(task_id="a" * 32, task_revision="c" * 64, chat_id=chat.chat_id)
    _write_decisions(host, [_decision_row(started.attempt.attempt_id, 1), "{not json"])
    archive = _real_archive(tmp_path, ["Plan the Q3 review"])

    pipeline.run_archive_postprocess(
        chat.chat_id, ArchiveOutcome(path=archive, turn_count=1), chat, project
    )

    assert len(written) == 1
    assert written[0].decisions_status == "malformed"
    assert written[0].decision_ids == (f"{1:032x}",)
    assert written[0].complete is False


@pytest.mark.asyncio
async def test_a_focused_approved_task_archive_captures_a_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    written = _capture_spy(monkeypatch)
    archive = _real_archive(tmp_path, ["Care"])
    focus = {"focus": "approved_task", "completion_id": _COMPLETION, "task_title": "T"}

    pipeline.run_archive_postprocess(
        chat.chat_id, ArchiveOutcome(path=archive, turn_count=1), chat, project, focus
    )

    assert [(r.kind, r.completion_id, r.id) for r in written] == [
        (
            "completion",
            _COMPLETION,
            archive_capture.capture_id("personal", chat.chat_id, "completion", _COMPLETION),
        )
    ]


@pytest.mark.asyncio
async def test_a_focus_without_a_valid_completion_id_captures_an_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    written = _capture_spy(monkeypatch)
    archive = _real_archive(tmp_path, ["Care"])
    focus = {"focus": "approved_task", "completion_id": "NOT-HEX", "task_title": "T"}

    pipeline.run_archive_postprocess(
        chat.chat_id, ArchiveOutcome(path=archive, turn_count=1), chat, project, focus
    )

    assert [(r.kind, r.completion_id) for r in written] == [("archive", "")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["system_schedule", "all_unattended", "empty", "memory_project", "passes_disabled"],
)
async def test_a_guarded_archive_writes_no_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    host, pipeline, chat, project, outcome = _setup(tmp_path)
    written = _capture_spy(monkeypatch)
    turn_count = 2
    if case == "system_schedule":
        chat.schedule_id = "system-memory-curation@personal"
        archive = _real_archive(tmp_path, ["Care", "looks good"])
    elif case == "all_unattended":
        archive = _real_archive(tmp_path, [_automated("Curate"), _automated("Care")])
    elif case == "empty":
        archive, turn_count = outcome.path, 0
    elif case == "memory_project":
        project.kind = memory_pass.MEMORY_PROJECT_KIND
        archive = _real_archive(tmp_path, ["Plan the Q3 review", "and more"])
    else:
        monkeypatch.setattr(memory_pass, "MEMORY_PASS_CHATS", False)
        archive = _real_archive(tmp_path, ["Plan the Q3 review", "and more"])

    pipeline.run_archive_postprocess(
        chat.chat_id, ArchiveOutcome(path=archive, turn_count=turn_count), chat, project
    )
    await asyncio.sleep(0)

    assert written == []
    assert host.enqueued == []


@pytest.mark.asyncio
async def test_a_failed_capture_write_never_fails_or_blocks_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)

    def _disk_full(vault_root: Path, record: object) -> None:
        del vault_root, record
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(archive_pipeline, "append_capture", _disk_full, raising=True)
    archive = _real_archive(tmp_path, ["Plan the Q3 review"])

    with caplog.at_level(logging.ERROR, logger=archive_pipeline.__name__):
        pipeline.run_archive_postprocess(
            chat.chat_id, ArchiveOutcome(path=archive, turn_count=1), chat, project
        )
        await asyncio.sleep(0)

    assert host.enqueued == [(chat.chat_id, archive, project.vault_doc_path)]
    assert host.index_calls == ["index"]
    assert any("capture" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_repeat_archive_of_the_same_chat_folds_to_the_first_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, pipeline, chat, project, _outcome = _setup(tmp_path)
    first_at = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    _Clock.instants = [first_at, datetime(2026, 11, 21, 9, 0, tzinfo=UTC)]
    monkeypatch.setattr(archive_pipeline, "datetime", _Clock)
    _seed_two_attempts_and_decisions(host, chat.chat_id)
    archive = _real_archive(tmp_path, ["Plan the Q3 review"])
    outcome = ArchiveOutcome(path=archive, turn_count=1)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)

    vault = host._config.workspace_root / "vault" / "personal"
    lines = archive_capture.captures_path(vault).read_text(encoding="utf-8").splitlines()
    read = archive_capture.read_captures(vault)
    assert len(lines) == 2
    assert read.status == "ok"
    assert [(r.chat_id, r.captured_at) for r in read.records] == [(chat.chat_id, first_at)]
    assert host.enqueued == [(chat.chat_id, archive, project.vault_doc_path)] * 2
