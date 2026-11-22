"""The approved-task enqueue writes its archive capture before the pass (#3239-c3b).

An approval on a chat that was already archived enqueues a memory pass directly,
without going through ``run_archive_postprocess``. That path must leave the same
capture record an archive leaves, before the enqueue, and a capture failure must
never stop the pass from being queued.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ciao import archive_capture
from ciao.control_plane import CiaoControlPlane
from ciao.web import archive_pipeline
from ciao.web.archive_pipeline import ArchivePipeline
from ciao.web.project_chats import ChatInfo, ProjectChatManager, ProjectInfo

ATTEMPT_ID = "a" * 32
COMPLETION_ID = "e" * 32


class _NoAttempts:
    def attempts_for_chat(self, chat_id: str) -> tuple[()]:
        del chat_id
        return ()


class _Pcm:
    """The manager surface the approved path and the archive pipeline use."""

    _archive_pipeline_for = ProjectChatManager._archive_pipeline_for

    def __init__(self, root: Path, chat: ChatInfo, project: ProjectInfo, order: list[str]) -> None:
        self._config = SimpleNamespace(workspace_root=root)
        self._chats = {chat.chat_id: chat}
        self._chat = chat
        self._project = project
        self.order = order
        self.enqueued: list[tuple[str, Path, dict[str, str] | None]] = []
        self._archive_pipeline = ArchivePipeline(cast(Any, self))

    def attempt_store(self, workspace: str) -> _NoAttempts:
        del workspace
        return _NoAttempts()

    def _workspace_vault_root(self, workspace: str) -> Path:
        return self._config.workspace_root / "vault" / workspace

    def get_chat(self, chat_id: str) -> ChatInfo | None:
        return self._chat if chat_id == self._chat.chat_id else None

    def get_project(self, project_id: str) -> ProjectInfo | None:
        return self._project if project_id == self._project.project_id else None

    def enqueue_memory_pass(
        self,
        chat: ChatInfo,
        project: ProjectInfo | None,
        archive_path: Path,
        doc_path: str,
        focus: dict[str, str] | None = None,
    ) -> str | None:
        del project, doc_path
        self.order.append("enqueue")
        self.enqueued.append((chat.chat_id, archive_path, focus))
        return "memory-chat"


def _build(
    tmp_path: Path, *, archive_path: str = "archive/chat-1.md"
) -> tuple[CiaoControlPlane, _Pcm, list[str], list[Any]]:
    chat = ChatInfo(
        chat_id="chat-1",
        project_id="project-1",
        title="Archived",
        archived=True,
        archive_path=archive_path,
        user_turn_count=3,
    )
    project = ProjectInfo(
        project_id="project-1", name="Holder", workspace="personal", vault_folder="holder"
    )
    order: list[str] = []
    # Borrowed from the manager, which the approved path reaches on the real object.
    _Pcm.capture_for_enqueue = ProjectChatManager.capture_for_enqueue  # type: ignore[attr-defined]
    pcm = _Pcm(tmp_path, chat, project, order)
    plane = CiaoControlPlane.__new__(CiaoControlPlane)
    plane.config = SimpleNamespace(workspace_root=tmp_path)
    plane.pcm = pcm
    runs: list[Any] = []
    plane._run_on_engine_loop = lambda name, run: runs.append(run)  # type: ignore[method-assign]
    return plane, pcm, order, runs


def _approve(plane: CiaoControlPlane) -> None:
    attempt = SimpleNamespace(attempt_id=ATTEMPT_ID, chat_id="chat-1", summary="Wrote the runbook.")
    completion = SimpleNamespace(
        id=COMPLETION_ID,
        resolution="",
        completed_at=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
    )
    plane._learn_from_approved("personal", attempt, "Ship the runbook", completion, "Tasks/a.md")


async def _run_approval(plane: CiaoControlPlane, runs: list[Any]) -> None:
    runs.clear()
    _approve(plane)
    assert len(runs) == 1
    await runs[0]()


def _captures(tmp_path: Path) -> list[archive_capture.CaptureRecord]:
    return archive_capture.read_captures(tmp_path / "vault" / "personal").records


@pytest.mark.asyncio
async def test_an_approved_task_enqueue_writes_its_completion_record(tmp_path: Path) -> None:
    plane, pcm, _order, runs = _build(tmp_path)

    await _run_approval(plane, runs)

    [record] = _captures(tmp_path)
    assert record.id == archive_capture.capture_id("personal", "chat-1", "completion", COMPLETION_ID)
    assert (record.kind, record.workspace, record.chat_id) == ("completion", "personal", "chat-1")
    assert (record.completion_id, record.turn_count, record.complete) == (COMPLETION_ID, 3, True)
    [(chat_id, _path, focus)] = pcm.enqueued
    assert chat_id == "chat-1"
    assert focus is not None and focus["focus"] == "approved_task"


@pytest.mark.asyncio
async def test_a_skipped_enqueue_writes_no_capture(tmp_path: Path) -> None:
    plane, pcm, order, runs = _build(tmp_path, archive_path="")

    await _run_approval(plane, runs)

    assert not _captures(tmp_path)
    assert pcm.enqueued == []
    assert order == []


@pytest.mark.asyncio
async def test_the_capture_is_written_before_the_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane, _pcm, order, runs = _build(tmp_path)
    real_append = archive_pipeline.append_capture

    def _spy(vault_root: Path, record: archive_capture.CaptureRecord) -> None:
        order.append("capture")
        real_append(vault_root, record)

    monkeypatch.setattr(archive_pipeline, "append_capture", _spy)

    await _run_approval(plane, runs)

    assert order == ["capture", "enqueue"]


@pytest.mark.asyncio
async def test_a_capture_failure_is_logged_and_the_enqueue_still_happens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    plane, pcm, _order, runs = _build(tmp_path)

    def _broken(vault_root: Path, record: archive_capture.CaptureRecord) -> None:
        del vault_root, record
        raise OSError("disk full")

    monkeypatch.setattr(archive_pipeline, "append_capture", _broken)

    with caplog.at_level(logging.ERROR):
        await _run_approval(plane, runs)

    assert len(pcm.enqueued) == 1
    assert "Failed to write the archive capture for chat-1" in caplog.text


@pytest.mark.asyncio
async def test_a_repeat_approval_folds_to_one_capture_and_still_enqueues(
    tmp_path: Path,
) -> None:
    plane, pcm, _order, runs = _build(tmp_path)

    await _run_approval(plane, runs)
    await _run_approval(plane, runs)

    assert [r.id for r in _captures(tmp_path)] == [
        archive_capture.capture_id("personal", "chat-1", "completion", COMPLETION_ID)
    ]
    assert len(pcm.enqueued) == 2
