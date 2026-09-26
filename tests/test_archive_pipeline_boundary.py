"""Contract and ownership-boundary tests for the archive pipeline collaborator.

The existing archive suites drive the complete manager and its durable files.
These tests instead drive ``ArchivePipeline`` with a small typed host so the
lifecycle ownership is explicit: postprocess state, retry/resume, cancellation,
and the completion/index hooks must all settle through the collaborator rather
than through a second manager-side copy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import cast

import pytest

from ciao import archive_jobs as aj
from ciao.config import CiaoConfig
from ciao.archive_jobs import ArchiveJob
from ciao.web.archive_pipeline import (
    ArchiveInputs,
    ArchivePipeline,
    ArchivePipelineHost,
)
from ciao.web import memory_pass
from ciao.web.chat_broker import EventsHub
from ciao.web.project_chats import ArchiveOutcome, ChatInfo, ProjectInfo


class _Host:
    """The manager surface needed by ``ArchivePipeline`` for these contracts."""

    def __init__(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self._config = CiaoConfig(
            pwa_auth_token="test-token",
            workspace_root=tmp_path,
            state_path=runtime / "state.json",
            media_root=runtime / "media",
            insights_enabled=True,
            trajectories_enabled=True,
        )
        self._runtime_root = runtime
        self._loop: asyncio.AbstractEventLoop | None = None
        self._detached_tasks: set[asyncio.Task[object]] = set()
        self._chats: dict[str, ChatInfo] = {}
        self._projects: dict[str, ProjectInfo] = {}
        self._events_hub = EventsHub()
        self.published: list[dict[str, object]] = []
        self._events_hub.publish = self.published.append  # type: ignore[method-assign]
        self.saves = 0
        self.begin_calls: list[tuple[str, list[str]]] = []
        self.end_calls: list[str] = []
        self.overlay_calls: list[str] = []
        self.run_calls: list[tuple[str, ArchiveJob]] = []
        self.launch_calls: list[tuple[str, ArchiveJob]] = []
        self.index_calls: list[str] = []
        self.cancelled = False
        self.job: ArchiveJob | None = None
        self.inputs: ArchiveInputs = {}
        self.pipeline: ArchivePipeline | None = None

    @property
    def events(self) -> EventsHub:
        return self._events_hub

    def _save(self, *, reason: str = "registry_mutation") -> None:
        del reason
        self.saves += 1

    def _workspace_vault_root(self, workspace: str) -> Path:
        return self._config.workspace_root / "vault" / workspace

    def _spawn_detached(
        self, coro: Coroutine[object, object, object], name: str
    ) -> asyncio.Task[object]:
        del name
        task = asyncio.create_task(coro)
        self._detached_tasks.add(task)
        task.add_done_callback(self._detached_tasks.discard)
        return task

    def _on_job_event(self, event: dict[str, object]) -> None:
        self.published.append({"received": event})

    def _apply_job_event(self, chat_id: str, event: dict[str, object]) -> None:
        del chat_id, event

    def _publish_postprocess(self, chat: ChatInfo) -> None:
        self.events.publish(
            {
                "type": "chat_postprocess",
                "chat_id": chat.chat_id,
                "project_id": chat.project_id,
                "postprocess": dict(chat.postprocess or {}),
            }
        )

    def _begin_postprocess(self, chat_id: str, expected: list[str]) -> None:
        self.begin_calls.append((chat_id, list(expected)))

    def _end_postprocess(self, chat_id: str) -> None:
        self.end_calls.append(chat_id)
        if self.pipeline is not None:
            self.pipeline._end_postprocess(chat_id)

    def _tracked_postprocess(
        self, chat_id: str, coro: Coroutine[object, object, object]
    ) -> Coroutine[object, object, None]:
        assert self.pipeline is not None
        return self.pipeline._tracked_postprocess(chat_id, coro)

    async def _run_job(
        self,
        chat_id: str,
        job: ArchiveJob,
        inputs: ArchiveInputs,
        *,
        stages: list[str] | None = None,
    ) -> None:
        del inputs
        self.run_calls.append((chat_id, job))
        for stage in stages or job.resumable():
            job.mark(stage, aj.SUCCEEDED)
        job.save()
        self._overlay_job_postprocess(chat_id, job)

    def _overlay_job_postprocess(self, chat_id: str, job: ArchiveJob) -> None:
        del job
        self.overlay_calls.append(chat_id)

    def _resume_job(
        self, chat_id: str, archive_path: Path
    ) -> tuple[ArchiveJob, ArchiveInputs] | tuple[None, None]:
        del chat_id, archive_path
        if self.job is None:
            return None, None
        return self.job, self.inputs

    def _launch_job(
        self,
        chat_id: str,
        job: ArchiveJob,
        inputs: ArchiveInputs,
        *,
        stages: list[str] | None = None,
    ) -> None:
        del inputs, stages
        self.launch_calls.append((chat_id, job))

    def _new_job_for_chat(self, chat: ChatInfo, inputs: ArchiveInputs) -> ArchiveJob:
        del inputs
        job = aj.create_job(
            self._runtime_root,
            chat_id=chat.chat_id,
            archive_path=chat.archive_path,
            content_revision_value=aj.archive_content_revision(
                self._config.workspace_root / chat.archive_path
            ),
        )
        self.job = job
        assert self.pipeline is not None
        self.pipeline.jobs[chat.chat_id] = job
        return job

    def _archive_path_for_chat(self, chat: ChatInfo) -> Path:
        return self._config.workspace_root / chat.archive_path

    def _job_inputs(
        self,
        chat: ChatInfo,
        project: ProjectInfo | None,
        *,
        filtered_jsonl: str = "",
        session_id: str = "",
        text_mode: bool = False,
    ) -> ArchiveInputs:
        del project
        return {
            "archive_path": self._archive_path_for_chat(chat),
            "config": self._config,
            "model": "test-model",
            "provider": chat.provider,
            "session_id": session_id,
            "filtered_jsonl": filtered_jsonl,
            "text_mode": text_mode,
            "trajectory_meta": {"workspace": ""},
            "workspace_root": self._config.workspace_root,
            "vault_root": self._config.vault_root,
            "proposal_vault_root": None,
            "guide_path": None,
            "trajectories_enabled": True,
            "memory_proposals_enabled": True,
            "project_doc_path": "",
        }

    def _restore_job_inputs(
        self, chat: ChatInfo, project: ProjectInfo | None, job: ArchiveJob
    ) -> ArchiveInputs:
        del project
        return self._job_inputs(chat, None, session_id="session-1")

    def _persist_job_inputs(self, job: ArchiveJob, inputs: ArchiveInputs) -> None:
        del job, inputs

    def _insights_model_for(self, chat: ChatInfo, workspace: str) -> str:
        del chat, workspace
        return "test-model"

    def _make_archive_index_operation(
        self, outcome: ArchiveOutcome
    ) -> Callable[[], None]:
        del outcome
        return lambda: self.index_calls.append("index")

    def _run_archive_index_best_effort(
        self,
        chat_id: str,
        outcome: ArchiveOutcome,
        operation: Callable[[], None],
    ) -> None:
        del chat_id, outcome
        operation()

    async def _index_archive_file_off_loop(
        self,
        chat_id: str,
        outcome: ArchiveOutcome,
        operation: Callable[[], None],
    ) -> None:
        del chat_id, outcome
        operation()


def _host(tmp_path: Path) -> tuple[_Host, ArchivePipeline, ChatInfo]:
    host = _Host(tmp_path)
    chat = ChatInfo(
        chat_id="chat-1",
        project_id="project-1",
        title="Archived",
        archived=True,
        archive_path="archive.md",
    )
    host._chats[chat.chat_id] = chat
    host._projects["project-1"] = ProjectInfo(
        project_id="project-1",
        name="Holder",
        workspace="personal",
    )
    (tmp_path / "archive.md").write_text("# archived\n", encoding="utf-8")
    host.pipeline = ArchivePipeline(cast(ArchivePipelineHost, host))
    return host, host.pipeline, chat


@pytest.mark.asyncio
async def test_postprocess_failure_cleans_state_and_persists(tmp_path: Path) -> None:
    host, pipeline, chat = _host(tmp_path)
    pipeline._begin_postprocess(chat.chat_id, ["insights"])
    assert pipeline.postprocessing == {chat.chat_id}
    assert chat.postprocess["state"] == "running"

    async def failing() -> None:
        raise RuntimeError("stage failed")

    with pytest.raises(RuntimeError):
        await pipeline._tracked_postprocess(chat.chat_id, failing())

    assert pipeline.postprocessing == set()
    assert chat.postprocess["state"] == "done"
    assert host.saves >= 1
    assert host.end_calls == [chat.chat_id]


@pytest.mark.asyncio
async def test_retry_and_startup_resume_use_owned_manifest_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one-shot insights stage is filtered once memory passes are on, which
    # leaves a resume with nothing to launch. This test is about the manifest
    # state the resume reads, not about which stages are registered, so it pins
    # the flag Off to keep exercising the resume.
    monkeypatch.setattr(memory_pass, "MEMORY_PASS_CHATS", False)
    host, pipeline, chat = _host(tmp_path)
    job = aj.create_job(
        host._runtime_root,
        chat_id=chat.chat_id,
        archive_path=chat.archive_path,
        content_revision_value=aj.archive_content_revision(
            host._config.workspace_root / chat.archive_path
        ),
    )
    job.mark("insights", aj.FAILED, "temporary failure")
    job.save()
    host.job = job
    host.inputs = {
        "archive_path": host._config.workspace_root / chat.archive_path,
        "trajectory_meta": {"workspace": "personal"},
    }
    pipeline.jobs[chat.chat_id] = job

    assert pipeline.retry_insights(chat.chat_id) == "started"
    assert host.launch_calls == [(chat.chat_id, job)]

    # A running stage left by a dead process is made pending and resumed through
    # the same task registry that delete cancellation consumes.
    job.stage("insights").status = aj.RUNNING
    job.save()
    assert await pipeline.resume_interrupted_jobs(max_concurrency=1) == 1
    await asyncio.sleep(0)
    assert chat.chat_id in pipeline.tasks
    await asyncio.gather(*tuple(pipeline.tasks.values()))
    await asyncio.sleep(0)
    assert pipeline.tasks == {}
    reloaded = aj.load_job(host._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.status_of("insights") == aj.SUCCEEDED


@pytest.mark.asyncio
async def test_delete_cancels_task_and_tombstones_manifest(tmp_path: Path) -> None:
    host, pipeline, chat = _host(tmp_path)
    job = aj.create_job(
        host._runtime_root,
        chat_id=chat.chat_id,
        archive_path=chat.archive_path,
        content_revision_value=aj.archive_content_revision(
            host._config.workspace_root / chat.archive_path
        ),
    )
    pipeline.jobs[chat.chat_id] = job

    async def wait_forever() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            host.cancelled = True
            raise

    task = asyncio.create_task(wait_forever())
    pipeline.tasks[chat.chat_id] = task
    await asyncio.sleep(0)

    pipeline._cancel_archive_job(chat.chat_id, chat)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert host.cancelled is True
    assert task.cancelled() or task.cancelling()
    assert chat.chat_id not in pipeline.tasks
    reloaded = aj.load_job(host._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.tombstoned is True


@pytest.mark.asyncio
async def test_success_postprocess_runs_manager_completion_hooks(tmp_path: Path) -> None:
    host, pipeline, chat = _host(tmp_path)
    outcome = ArchiveOutcome(
        path=host._config.workspace_root / chat.archive_path,
        session_id="session-1",
        turn_count=1,
        filtered_jsonl="{}",
    )

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert host.run_calls and host.run_calls[0][0] == chat.chat_id
    assert host.overlay_calls == [chat.chat_id]
    assert host.index_calls == ["index"]
    assert chat.postprocess["state"] == "done"
    assert pipeline.postprocessing == set()


def test_manager_archive_state_is_a_collaborator_view(tmp_path: Path) -> None:
    from ciao.web.project_chats import ProjectChatManager

    manager = object.__new__(ProjectChatManager)
    manager._archive_pipeline = ArchivePipeline(cast(ArchivePipelineHost, manager))
    manager._postprocessing.add("chat-1")
    assert manager._archive_pipeline.postprocessing == {"chat-1"}
    manager._postprocessing = {"chat-2"}
    assert manager._archive_pipeline.postprocessing == {"chat-2"}
    manager._archive_jobs["chat-2"] = cast(ArchiveJob, object())
    assert "chat-2" in manager._archive_pipeline.jobs
