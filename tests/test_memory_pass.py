"""The end-of-conversation memory pass as a normal chat (#602).

The pass replaces the one-shot insights / project-doc / memory-proposal stages
with an attended ``bypass`` chat in the workspace's Memory project, queued one
at a time per workspace, auto-archived when clean and left open when not.

Every test here monkeypatches ``memory_pass.MEMORY_PASS_CHATS``, which is why
the flag is read through the module attribute and never imported by value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ciao import archive_jobs as aj
from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import chat_service, memory_pass
from ciao.web.project_chats import (
    ArchiveOutcome,
    ChatInfo,
    ProjectChatManager,
)

from tests.conftest import attach_stub_mcp


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    manager = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    return attach_stub_mcp(manager)


class _FakeStreams:
    """Stand in for ``start_stream``: records the turn, dispatches nothing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, chat_id: str, prompt: str, images: object = None, **kwargs: Any
    ) -> None:
        del images
        self.calls.append({"chat_id": chat_id, "prompt": prompt, **kwargs})

    @property
    def chat_ids(self) -> list[str]:
        return [str(call["chat_id"]) for call in self.calls]


@pytest.fixture
def passes_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_pass, "MEMORY_PASS_CHATS", True)


@pytest.fixture
def streams(monkeypatch: pytest.MonkeyPatch) -> _FakeStreams:
    fake = _FakeStreams()
    monkeypatch.setattr(
        ProjectChatManager, "start_stream", lambda self, *a, **k: fake(*a, **k)
    )
    return fake


def _stub_archive(
    manager: ProjectChatManager, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Archive for real (flag the chat), without writing any file."""
    archived: list[str] = []

    async def archive_chat(chat_id: str) -> None:
        archived.append(chat_id)
        chat = manager.get_chat(chat_id)
        assert chat is not None
        chat.archived = True
        return None

    monkeypatch.setattr(manager, "archive_chat", archive_chat)
    return archived


def _persisted(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )


def _archive_file(tmp_path: Path) -> Path:
    archive = tmp_path / "logs" / "Chats" / "abc" / "transcript.md"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("# Pricing rework\n", encoding="utf-8")
    return archive


def _source(
    manager: ProjectChatManager, title: str = "Pricing rework", workspace: str = "work"
) -> ChatInfo:
    """An archived chat in its own project, as archiving leaves it."""
    project = manager.create_project(f"Src {title}", workspace=workspace)
    chat = manager.create_chat(project.project_id, title=title)
    chat.archived = True
    chat.archive_path = "logs/Chats/abc/transcript.md"
    manager._save()
    return chat


def _replied(chat: ChatInfo, text: str, status: str = "success") -> None:
    chat.last_response = text
    chat.last_response_status = status


def _no_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_pipeline(job: object, inputs: dict, **kwargs: object) -> object:
        del inputs, kwargs
        return job

    monkeypatch.setattr("ciao.insights.run_archive_pipeline", no_pipeline)


# ── Helper normalisation ──────────────────────────────────────────────────


def test_helper_normalize_memory_pass() -> None:
    valid = {
        "kind": "memory_pass",
        "source_chat_id": "chat-1",
        "archive_path": "logs/Chats/abc/transcript.md",
        "doc_path": "memory-vault/work/projects/active/shipped/README.md",
        "source_title": "Pricing rework",
        "source_project": "Shipped",
        "state": "running",
        "archive_policy": "when_clean",
    }
    assert chat_service._normalize_chat_helper(valid) == valid

    # Fail closed: an unknown state, a missing source and a wrong policy each
    # erase the helper, because a half-valid one makes a pass unrecognisable.
    assert chat_service._normalize_chat_helper({**valid, "state": "paused"}) == {}
    assert chat_service._normalize_chat_helper({**valid, "source_chat_id": ""}) == {}
    assert (
        chat_service._normalize_chat_helper({**valid, "source_chat_id": "c" * 129})
        == {}
    )
    assert (
        chat_service._normalize_chat_helper({**valid, "archive_policy": "manual"})
        == {}
    )
    # State is optional and defaults to the queue's entry state.
    assert (
        chat_service._normalize_chat_helper(
            {k: v for k, v in valid.items() if k != "state"}
        )["state"]
        == "queued"
    )

    # The proposal branch is untouched by the new kind.
    proposal = {
        "kind": "proposal",
        "intent": "resolve",
        "proposal_ids": ["proposal-1"],
        "archive_policy": "when_resolved",
    }
    assert chat_service._normalize_chat_helper(proposal) == proposal
    assert chat_service._normalize_chat_helper({**proposal, "kind": "nope"}) == {}


# ── The Memory project ────────────────────────────────────────────────────


def test_ensure_project_is_idempotent_and_stable(
    tmp_path: Path, streams: _FakeStreams
) -> None:
    manager = _make_manager(tmp_path)

    first = manager._memory_pass.ensure_project("work")
    second = manager._memory_pass.ensure_project("work")

    assert first.project_id == second.project_id
    assert first.kind == memory_pass.MEMORY_PROJECT_KIND
    assert first.is_system is True
    # Not `is_auto`: the PWA picks General with that flag, and the Memory
    # project must not be one of the two special cases it knows about.
    assert first.is_auto is False
    assert memory_pass.is_memory_pass_chat(
        manager.create_chat(first.project_id, title="x"), first
    )
    assert not memory_pass.is_memory_pass_chat(
        manager.create_chat(first.project_id, title="y", helper={}), None
    )

    reloaded = _make_manager(tmp_path)._projects[first.project_id]
    assert reloaded.kind == memory_pass.MEMORY_PROJECT_KIND
    assert reloaded.name == memory_pass.MEMORY_PROJECT_NAME
    assert reloaded.workspace == "work"
    assert [
        project
        for project in _persisted(tmp_path)["projects"].values()
        if project["kind"]
    ] == [
        {
            "name": memory_pass.MEMORY_PROJECT_NAME,
            "workspace": "work",
            "context": "",
            "created_at": reloaded.created_at,
            "order": reloaded.order,
            "vault_folder": "",
            "kind": "memory",
        }
    ]


def test_memory_project_is_not_linked_by_vault_discovery_or_deletable(
    tmp_path: Path, streams: _FakeStreams
) -> None:
    manager = _make_manager(tmp_path)
    memory = manager._memory_pass.ensure_project("work")

    # A vault entry that happens to be called "Memory" must not bind itself to
    # the system project: the app owns it and it has no canonical doc.
    folder = tmp_path / "memory-vault" / "work" / "projects" / "active" / "memory"
    folder.mkdir(parents=True)
    (folder / "README.md").write_text(
        "---\ntitle: Memory\ndescription: the user's own\n---\n", encoding="utf-8"
    )

    manager.list_projects("work")

    assert manager.get_project(memory.project_id).vault_folder == ""
    assert manager.get_project(memory.project_id).context == ""
    with pytest.raises(ValueError):
        manager.delete_project(memory.project_id)
    assert manager.get_project(memory.project_id) is not None


# ── Archive hook ──────────────────────────────────────────────────────────


async def test_postprocess_enqueues_and_skips_one_shot_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    passes_enabled: None,
    streams: _FakeStreams,
) -> None:
    manager = _make_manager(tmp_path)
    source = _source(manager)
    project = manager.get_project(source.project_id)
    archive = _archive_file(tmp_path)
    _no_model_calls(monkeypatch)

    manager.run_archive_postprocess(
        source.chat_id,
        ArchiveOutcome(archive, "sess-1", 1, '{"idx":1}'),
        source,
        project,
    )

    job = aj.load_job(
        manager._runtime_root,
        aj.new_job_id(source.chat_id, source.archive_path),
    )
    assert job is not None
    for stage in ("insights", "project_doc_update", "memory_proposals"):
        assert job.status_of(stage) == aj.SKIPPED
    # Trajectory does not consume insights text, so it still runs.
    assert job.status_of("trajectory") == aj.PENDING

    memory_project = manager._memory_pass.ensure_project("work")
    passes = [
        c
        for c in manager._chats.values()
        if c.project_id == memory_project.project_id
    ]
    assert len(passes) == 1
    memory_chat = passes[0]
    assert memory_chat.mode == "bypass"
    assert memory_chat.helper == {
        "kind": "memory_pass",
        "source_chat_id": source.chat_id,
        "archive_path": str(archive),
        "doc_path": "",
        "source_title": "Pricing rework",
        "source_project": "Src Pricing rework",
        "state": "running",
        "archive_policy": "when_clean",
    }
    # Resolved exactly the way the one-shot stage resolved it.
    assert memory_chat.provider == source.provider
    assert memory_chat.model == manager._insights_model_for(source, "work")
    assert memory_chat.title == "Memory pass · Pricing rework"
    assert streams.chat_ids == [memory_chat.chat_id]

    assert manager.get_chat(source.chat_id).postprocess["steps"]["memory_pass"] == {
        "status": "running",
        "extra": {"chat_id": memory_chat.chat_id},
    }


async def test_postprocess_unchanged_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streams: _FakeStreams,
) -> None:
    assert memory_pass.MEMORY_PASS_CHATS is False
    manager = _make_manager(tmp_path)
    source = _source(manager)
    project = manager.get_project(source.project_id)
    archive = _archive_file(tmp_path)
    _no_model_calls(monkeypatch)

    manager.run_archive_postprocess(
        source.chat_id,
        ArchiveOutcome(archive, "sess-1", 1, '{"idx":1}'),
        source,
        project,
    )

    job = aj.load_job(
        manager._runtime_root,
        aj.new_job_id(source.chat_id, source.archive_path),
    )
    assert job is not None
    assert job.status_of("insights") != aj.SKIPPED
    assert streams.calls == []
    assert all(p.kind == "" for p in manager._projects.values())
    assert (
        "memory_pass"
        not in manager.get_chat(source.chat_id).postprocess.get("steps", {})
    )


async def test_memory_pass_chat_archive_does_not_recurse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    passes_enabled: None,
    streams: _FakeStreams,
) -> None:
    manager = _make_manager(tmp_path)
    source = _source(manager)
    project = manager.get_project(source.project_id)
    archive = _archive_file(tmp_path)
    memory_id = manager.enqueue_memory_pass(source, project, archive, "")
    memory_chat = manager.get_chat(memory_id)
    _no_model_calls(monkeypatch)

    # Archiving the pass itself must not queue a pass of the pass, and must not
    # run the one-shot stages over the pass's own bookkeeping.
    manager.run_archive_postprocess(
        memory_id,
        ArchiveOutcome(archive, "sess-2", 1, '{"idx":1}'),
        memory_chat,
        manager.get_project(memory_chat.project_id),
    )

    memory_project = manager._memory_pass.ensure_project("work")
    assert [
        c.chat_id
        for c in manager._chats.values()
        if c.project_id == memory_project.project_id
    ] == [memory_id]
    job = aj.load_job(
        manager._runtime_root,
        aj.new_job_id(memory_id, memory_chat.archive_path),
    )
    assert job is not None
    assert job.status_of("insights") == aj.SKIPPED
    assert job.status_of("memory_proposals") == aj.SKIPPED


def test_dedupe_by_source_chat(
    tmp_path: Path, passes_enabled: None, streams: _FakeStreams
) -> None:
    manager = _make_manager(tmp_path)
    source = _source(manager)
    project = manager.get_project(source.project_id)
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n", encoding="utf-8")

    first = manager.enqueue_memory_pass(source, project, archive, "")
    assert first is not None
    assert manager.enqueue_memory_pass(source, project, archive, "") is None

    # An archived pass still dedupes: re-archiving the same source must not run
    # a second extraction.
    manager.get_chat(first).archived = True
    assert manager.enqueue_memory_pass(source, project, archive, "") is None
    assert streams.chat_ids == [first]


async def test_one_running_per_workspace_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    passes_enabled: None,
    streams: _FakeStreams,
) -> None:
    manager = _make_manager(tmp_path)
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n", encoding="utf-8")
    _stub_archive(manager, monkeypatch)

    queued = [
        manager.enqueue_memory_pass(_source(manager, name), None, archive, "")
        for name in ("First", "Second")
    ]
    elsewhere = manager.enqueue_memory_pass(
        _source(manager, "Elsewhere", workspace="personal"), None, archive, ""
    )

    # One per workspace: the second work pass waits, while another workspace
    # runs in parallel.
    assert streams.chat_ids == [queued[0], elsewhere]

    first, second = (manager.get_chat(cid) for cid in queued)
    _replied(first, "Updated the people notes.")
    assert await manager._memory_pass.on_turn_finished(first.chat_id) is True

    assert first.archived is True
    assert streams.chat_ids == [queued[0], elsewhere, second.chat_id]
    assert second.helper["state"] == "running"


async def test_clean_finish_archives_and_marks_source_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    passes_enabled: None,
    streams: _FakeStreams,
) -> None:
    manager = _make_manager(tmp_path)
    source = _source(manager)
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n", encoding="utf-8")
    memory_id = manager.enqueue_memory_pass(source, None, archive, "")
    archived = _stub_archive(manager, monkeypatch)
    memory = manager.get_chat(memory_id)
    _replied(memory, "Updated people.md and promoted one durable fact.")

    assert await manager._memory_pass.on_turn_finished(memory_id) is True

    assert archived == [memory_id]
    assert memory.archived is True
    assert memory.helper["state"] == "done"
    assert manager.get_chat(source.chat_id).postprocess["steps"]["memory_pass"] == {
        "status": "ok",
        "extra": {"chat_id": memory_id},
    }


async def test_unclean_finish_marks_attention_and_pumps_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    passes_enabled: None,
    streams: _FakeStreams,
) -> None:
    manager = _make_manager(tmp_path)
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n", encoding="utf-8")
    sources = [_source(manager, name) for name in ("First", "Second")]
    ids = [
        manager.enqueue_memory_pass(source, None, archive, "") for source in sources
    ]
    first, second = (manager.get_chat(cid) for cid in ids)
    _replied(first, "the vault is locked", status="error")

    # An error turn: the pass stays open for the owner and the queue moves on.
    assert await manager._memory_pass.on_turn_finished(first.chat_id) is False

    assert first.archived is False
    assert first.helper["state"] == "attention"
    assert manager.get_chat(
        sources[0].chat_id
    ).postprocess["steps"]["memory_pass"]["status"] == "attention"
    assert second.helper["state"] == "running"
    assert streams.chat_ids == ids


async def test_pending_permission_keeps_the_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    passes_enabled: None,
    streams: _FakeStreams,
) -> None:
    manager = _make_manager(tmp_path)
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n", encoding="utf-8")
    ids = [
        manager.enqueue_memory_pass(_source(manager, name), None, archive, "")
        for name in ("First", "Second")
    ]
    first, second = (manager.get_chat(cid) for cid in ids)
    _stub_archive(manager, monkeypatch)
    _replied(first, "May I run the ciao memory command?")
    first.pending_permission = '{"request_id":"r1","tool_name":"Bash"}'

    # The pass is blocked on the owner, so it is not over: it keeps the slot
    # and the next pass waits rather than racing it for the same notes.
    assert await manager._memory_pass.on_turn_finished(first.chat_id) is False

    assert first.helper["state"] == "running"
    assert first.archived is False
    assert second.helper["state"] == "queued"
    # The queued pass never started: the slot is still held.
    assert streams.chat_ids == [ids[0]]


async def test_resume_after_restart(
    tmp_path: Path, passes_enabled: None, streams: _FakeStreams
) -> None:
    manager = _make_manager(tmp_path)
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n", encoding="utf-8")
    sources = [_source(manager, name) for name in ("First", "Second")]
    ids = [
        manager.enqueue_memory_pass(source, None, archive, "") for source in sources
    ]
    # The first was running when the process died; the second was still queued.
    reloaded = _make_manager(tmp_path)
    reloaded_streams = _FakeStreams()
    reloaded.start_stream = reloaded_streams  # type: ignore[method-assign]
    assert reloaded.get_chat(ids[0]).helper["state"] == "running"
    assert reloaded.get_chat(ids[1]).helper["state"] == "queued"

    await reloaded.resume_memory_passes()

    # A dead turn is not a clean one: it needs the owner, and its slot is freed
    # so the queue can move on.
    assert reloaded.get_chat(ids[0]).helper["state"] == "attention"
    assert reloaded.get_chat(
        sources[0].chat_id
    ).postprocess["steps"]["memory_pass"]["status"] == "attention"
    assert reloaded_streams.chat_ids == [ids[1]]
    assert reloaded.get_chat(ids[1]).helper["state"] == "running"


# ── Notifications and turn shape ──────────────────────────────────────────


async def test_result_push_suppressed_for_memory_pass_chat(
    tmp_path: Path, passes_enabled: None
) -> None:
    manager = _make_manager(tmp_path)
    sent: list[tuple[str, str, str]] = []
    manager.notify_result_cb = lambda *args: sent.append(args)
    source = manager.create_chat(
        manager.create_project("Shipped", workspace="work").project_id,
        title="Pricing rework",
    )
    memory_project = manager._memory_pass.ensure_project("work")
    memory = manager.create_chat(
        memory_project.project_id,
        title="Memory pass · Pricing rework",
        helper={
            "kind": "memory_pass",
            "source_chat_id": source.chat_id,
            "archive_policy": "when_clean",
        },
    )

    manager._schedule_push(memory.chat_id, "t", "s")
    assert sent == []
    assert memory.chat_id not in manager._pending_push

    manager._schedule_push(source.chat_id, "t", "s")
    assert list(manager._pending_push) == [source.chat_id]
    task = manager._pending_push[source.chat_id]
    task.cancel()


def test_start_stream_is_attended(
    tmp_path: Path, passes_enabled: None, streams: _FakeStreams
) -> None:
    manager = _make_manager(tmp_path)
    source = _source(manager)
    project = manager.get_project(source.project_id)
    archive = _archive_file(tmp_path)

    memory_id = manager.enqueue_memory_pass(source, project, archive, "doc.md")

    # `unattended=True` would inject the defer-new-facts capsule and auto-deny
    # cards; the experiment behind this design measured attended turns.
    assert streams.calls[0].get("unattended", False) is False
    prompt = str(streams.calls[0]["prompt"])
    assert str(archive) in prompt
    assert "Pricing rework" in prompt
    assert "doc.md" in prompt
    assert project.name in prompt
    assert streams.chat_ids == [memory_id]
