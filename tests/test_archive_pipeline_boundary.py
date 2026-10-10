"""Contract tests for the archive pipeline collaborator.

These drive ``ArchivePipeline`` with a small typed host, so what happens after
an archive is written stays explicit: the memory pass is queued (or not) and
the archive is indexed for search, both through the host protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import cast

import pytest

from ciao.config import CiaoConfig
from ciao.context.capsule import build_context_capsule
from ciao.models import AgentRequest, ChatContext
from ciao.transcripts import TranscriptStore
from ciao.web.archive_pipeline import ArchivePipeline, ArchivePipelineHost
from ciao.web.chat_broker import EventsHub
from ciao.web.project_chats import ArchiveOutcome, ChatInfo, ProjectInfo


class _Host:
    """The manager surface ``ArchivePipeline`` needs for these contracts."""

    def __init__(self, tmp_path: Path, *, insights_enabled: bool = True) -> None:
        runtime = tmp_path / ".runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self._config = CiaoConfig(
            pwa_auth_token="test-token",
            workspace_root=tmp_path,
            state_path=runtime / "state.json",
            media_root=runtime / "media",
            insights_enabled=insights_enabled,
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
    tmp_path: Path, **host_kwargs: bool
) -> tuple[_Host, ArchivePipeline, ChatInfo, ProjectInfo, ArchiveOutcome]:
    host = _Host(tmp_path, **host_kwargs)
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
async def test_insights_off_skips_the_pass_but_still_indexes(tmp_path: Path) -> None:
    host, pipeline, chat, project, outcome = _setup(tmp_path, insights_enabled=False)

    pipeline.run_archive_postprocess(chat.chat_id, outcome, chat, project)
    await asyncio.sleep(0)

    assert host.enqueued == []
    assert host.index_calls == ["index"]


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
