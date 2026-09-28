"""The end-of-conversation memory pass as a normal chat (#602).

The pass replaces the one-shot insights / project-doc / memory-proposal stages
with an attended ``bypass`` chat in the workspace's Memory project, queued one
at a time per workspace, auto-archived when clean and left open when not.

Every test here monkeypatches ``memory_pass.MEMORY_PASS_CHATS``, which is why
the flag is read through the module attribute and never imported by value.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from ciao import archive_jobs as aj
from ciao.config import CiaoConfig
from ciao.models import AgentRequest, ChatContext, ResultEvent, StreamEvent
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


def _register_work_workspace(tmp_path: Path) -> None:
    """Put a ``work`` workspace in the vault so the registry holds one.

    The config is built from the vault, so a manager built before this exists
    knows no workspace at all — and a workspace the registry does not hold owns
    no catalog, which is the whole of the skill-review section's precondition.
    """
    (tmp_path / "memory-vault" / "work" / "Workspace").mkdir(parents=True)


def _write_owned_skill(root: Path, name: str) -> Path:
    """The canonical owned source ``<root>/skills/<name>/SKILL.md``."""
    skill_md = root / "skills" / name / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        f"---\nname: {name}\ndescription: Owned skill\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_md


def _installed_stock_copy(root: Path, name: str) -> Path:
    """A sync-generated copy under ``.claude/skills``, which no pass may edit."""
    skill_dir = root / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / ".ciao-stock-skill").write_text("stock\n", encoding="utf-8")
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(f"# {name}\n", encoding="utf-8")
    return skill_md


def _archive_using(tmp_path: Path, skill: str) -> Path:
    """A real archived transcript whose single turn used ``skill``.

    Rendered by the store rather than handwritten, because the pass cites what
    the archive retains: a hand-written ``- Skills:`` line here would keep this
    test green after the renderer stopped writing one.
    """
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "memory-vault")
    ctx = ChatContext(chat_id=0, key_override="src")
    store.record_turn(
        AgentRequest(
            prompt="File the notes",
            model="sonnet",
            mode="bypass",
            resume_session=None,
            images=[],
        ),
        ctx=ctx,
        response_text="Filed them.",
        effective_model="sonnet",
        session_id="sess-skill",
        usage={},
        quota={},
        input_kind="text",
        context_label="Pricing rework",
        tool_events=[{"id": "t1", "name": "Skill", "input": {"summary": skill}}],
    )
    archived = store.archive_session(
        ctx=ctx,
        active_model="sonnet",
        last_effective_model="sonnet",
        session_id="sess-skill",
    )
    assert archived is not None
    return archived


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


class _TerminalProvider:
    """A provider whose pass ends on a scripted result.

    ``current_session_id`` stays None so the turn stamps no provider session,
    which keeps these tests off the subagent watcher and the between-turns
    drain: the foreground turn has to be the only thing that settles a pass.
    """

    current_session_id = None

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events

    async def execute_streaming(
        self, request: AgentRequest
    ) -> AsyncGenerator[StreamEvent, None]:
        del request
        for event in self._events:
            yield event


class _SilentProvider(_TerminalProvider):
    """A pass that never answers, so its turn stays in flight."""

    def __init__(self) -> None:
        super().__init__([])

    async def execute_streaming(
        self, request: AgentRequest
    ) -> AsyncGenerator[StreamEvent, None]:
        del request
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable: the wait never returns


class _DrainingProvider:
    """A between-turns drain that answers with a scripted result."""

    can_drain = True

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events

    async def drain_events(self) -> AsyncGenerator[StreamEvent, None]:
        for event in self._events:
            yield event


async def _await_detached(manager: ProjectChatManager) -> None:
    """Let the hooks a terminal turn spawned run, and what they spawn in turn.

    ``_spawn_detached`` registers each task synchronously and drops it from a
    done callback, so a task that already finished only needs one loop turn for
    the set to drain. Awaiting ``gather`` alone would spin on exactly those,
    never yielding.
    """
    while True:
        pending = [task for task in manager._detached_tasks if not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


def _no_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_pipeline(job: object, inputs: dict, **kwargs: object) -> object:
        del inputs, kwargs
        return job

    monkeypatch.setattr("ciao.insights.run_archive_pipeline", no_pipeline)


# ── The prompt ────────────────────────────────────────────────────────────


def test_prompt_routes_categories_to_the_vocabulary_block() -> None:
    """The pass writes the notes, so the pass prompt owns the category rules.

    A category the owner added is only usable if the pass reads the block that
    lists it, files the note in the folder that block names, and asks rather
    than invents a `type:` when nothing fits.
    """
    prompt = memory_pass.MEMORY_PASS_PROMPT
    assert "**Categories** section of the vault's `VOCABULARY.md`" in prompt
    assert "the `type:` it lists, in the folder it names" in prompt
    assert "queue a new-category question" in prompt
    assert "instead of inventing a type" in prompt


def test_prompt_still_formats_every_placeholder() -> None:
    """A brace in the added sentence would raise on the next chat's first turn."""
    rendered = memory_pass.MEMORY_PASS_PROMPT.format(
        title="Pricing rework",
        archive="logs/Chats/abc/transcript.md",
        project="Shipped",
        doc="docs/pricing.md",
    )
    for filled in (
        '"Pricing rework"',
        "logs/Chats/abc/transcript.md",
        "Shipped",
        "docs/pricing.md",
    ):
        assert filled in rendered
    assert "{" not in rendered and "}" not in rendered


def test_skill_review_section_formats_every_placeholder() -> None:
    """Same contract for the appended section: the skill list is its one field.

    A JSON example spelled out in the section would be read as a field here,
    which is why the keys are named in prose instead.
    """
    rendered = memory_pass.SKILL_REVIEW_PROMPT.format(skills="notes, web-research")
    assert "notes, web-research" in rendered
    assert "{" not in rendered and "}" not in rendered


def test_pass_reviews_the_owned_skills_the_conversation_used(
    tmp_path: Path, passes_enabled: None, streams: _FakeStreams
) -> None:
    """The skill section is appended, and the memory work is untouched by it.

    Both halves matter: a pass that stopped filing memory notes because a skill
    review was added, or one that reviews the whole catalog instead of what the
    conversation used, is the failure this section must not introduce.
    """
    _register_work_workspace(tmp_path)
    _write_owned_skill(tmp_path, "notes")
    _write_owned_skill(tmp_path, "unused-skill")
    manager = _make_manager(tmp_path)
    source = _source(manager)
    archive = _archive_using(tmp_path, "notes")

    manager.enqueue_memory_pass(
        source, manager.get_project(source.project_id), archive, ""
    )

    prompt = str(streams.calls[0]["prompt"])
    # The memory pass is still the memory pass.
    assert "**Categories** section of the vault's `VOCABULARY.md`" in prompt
    assert "marked as automated (unattended) are not the user's" in prompt
    assert "Finish with a short list of what you changed." in prompt
    # The candidates are the skills the turn used, resolved against the
    # workspace's own catalog: `unused-skill` is owned but was not used.
    assert "own skills in use: notes." in prompt
    assert "unused-skill" not in prompt
    # The rules the section exists to state.
    # Positional, because that is what the parser takes: naming a `--skill`
    # flag the parser has never heard of exits 2 and files nothing.
    assert "ciao skill-proposal-add NAME --input-file FILE" in prompt
    assert "Never edit a skill" in prompt
    assert "filing none is a valid result" in prompt
    # And the evidence the proposal has to carry.
    for field in ("`turn`", "`excerpt`", "`sources`", "`change`"):
        assert field in prompt


def test_pass_asks_for_no_skill_review_when_the_conversation_used_no_skill(
    tmp_path: Path, passes_enabled: None, streams: _FakeStreams
) -> None:
    """A conversation that used no skill gets no section to satisfy.

    The section is the expensive half of the prompt and the one that can be
    wrong, so it is only ever rendered when there is a candidate — and a
    candidate is decided here, not by the model declining to look. The workspace
    owns a skill here: ownership alone is not the trigger, use is.
    """
    _register_work_workspace(tmp_path)
    _write_owned_skill(tmp_path, "notes")
    manager = _make_manager(tmp_path)
    source = _source(manager)
    archive = _archive_file(tmp_path)

    manager.enqueue_memory_pass(
        source, manager.get_project(source.project_id), archive, ""
    )

    prompt = str(streams.calls[0]["prompt"])
    assert "own skills in use" not in prompt
    assert "skill-proposal-add" not in prompt
    assert "Finish with a short list of what you changed." in prompt


def test_pass_refuses_an_installed_stock_copy_as_a_candidate(
    tmp_path: Path, passes_enabled: None, streams: _FakeStreams
) -> None:
    """A used skill the workspace does not own is not a candidate at all.

    The conversation genuinely used ``web-research``, and a pass that filed a
    proposal against it would be asking for an edit to a copy sync refreshes on
    every run. The exclusion is the resolver's, computed before the prompt is
    built: a model-supplied flag is not a boundary.
    """
    _register_work_workspace(tmp_path)
    _installed_stock_copy(tmp_path, "web-research")
    manager = _make_manager(tmp_path)
    source = _source(manager)
    archive = _archive_using(tmp_path, "web-research")

    manager.enqueue_memory_pass(
        source, manager.get_project(source.project_id), archive, ""
    )

    prompt = str(streams.calls[0]["prompt"])
    assert "web-research" not in prompt
    assert "skill-proposal-add" not in prompt


def test_pass_does_not_walk_the_catalog_when_the_workspace_owns_nothing(
    tmp_path: Path, passes_enabled: None, streams: _FakeStreams
) -> None:
    """An unregistered workspace owns no catalog, so nothing is reviewable."""
    _write_owned_skill(tmp_path, "notes")
    manager = _make_manager(tmp_path)
    source = _source(manager)
    archive = _archive_using(tmp_path, "notes")

    # `_source` creates its project in the `work` workspace, so this is the one
    # case that must not raise: the registry holds no `work` at all.
    assert manager._config.workspace("work") is None
    manager.enqueue_memory_pass(
        source, manager.get_project(source.project_id), archive, ""
    )

    assert "own skills in use" not in str(streams.calls[0]["prompt"])


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
    # The trajectory is all the manifest plans; the vault work is the pass's.
    assert list(job.stages) == ["trajectory"]
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
    # Pinned here rather than read off the module: the point is the disabled
    # path, which must stay pinned however the shipped constant is flipped.
    monkeypatch.setattr(memory_pass, "MEMORY_PASS_CHATS", False)
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
    assert job.status_of("trajectory") != aj.SKIPPED
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
    assert list(job.stages) == ["trajectory"]


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


# ── The terminal turn a pass settles on ────────────────────────────────────
#
# `on_turn_finished` above is called directly, which pins the queue but proves
# nothing about whether anything ever calls it: the scheduling lived in the
# streaming loop and only ever ran for a clean result, so an errored or empty
# pass stayed `running` and blocked its workspace queue for good. These drive
# the real turn.


@pytest.mark.parametrize(
    ("result", "is_error", "status"),
    [
        ("the vault is locked", True, "error"),
        ("", False, "empty"),
    ],
)
async def test_an_unclean_foreground_turn_frees_the_workspace_slot(
    tmp_path: Path,
    passes_enabled: None,
    result: str,
    is_error: bool,
    status: str,
) -> None:
    manager = _make_manager(tmp_path)
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n", encoding="utf-8")
    sources = [_source(manager, name) for name in ("First", "Second")]
    ids = [
        manager.enqueue_memory_pass(source, None, archive, "")
        for source in sources
    ]
    first_id, second_id = ids
    # One at a time: the first pass holds the live turn, the second waits.
    assert manager.get_chat(first_id).helper["state"] == "running"
    assert manager.get_chat(second_id).helper["state"] == "queued"

    # Registered before the loop runs the drive task the queue just created, so
    # the turn streams from these and never from a real CLI.
    manager._providers[first_id] = _TerminalProvider(  # type: ignore[assignment]
        [ResultEvent(type="result", result=result, is_error=is_error)]
    )
    manager._providers[second_id] = _SilentProvider()  # type: ignore[assignment]
    try:
        turn = manager._streaming.drive_task(first_id)
        assert turn is not None
        await turn
        await _await_detached(manager)

        first = manager.get_chat(first_id)
        assert first.last_response_status == status
        # A pass that ended badly is left open for its owner, never archived.
        assert first.archived is False
        assert first.helper["state"] == "attention"
        assert manager.get_chat(
            sources[0].chat_id
        ).postprocess["steps"]["memory_pass"]["status"] == "attention"
        # And it is no longer holding the workspace, so the queue moved on.
        assert manager.get_chat(second_id).helper["state"] == "running"
        assert manager._streaming.drive_task(second_id) is not None
    finally:
        # The second pass is still streaming when the test ends; unwind it so
        # the loop closes clean, and let its own terminal settle run.
        second_turn = manager._streaming.drive_task(second_id)
        if second_turn is not None:
            second_turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await second_turn
        await _await_detached(manager)


async def test_an_errored_drain_settles_a_pass_waiting_on_background_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    passes_enabled: None,
    streams: _FakeStreams,
) -> None:
    """A pass that handed the turn to background work settles on its result.

    The drain is a terminal result in its own right, and an errored one is the
    case the pass must not miss: it has to persist as an error, or the pass
    reads the clean status left by the earlier turn and archives the failure.
    """
    manager = _make_manager(tmp_path)
    proposed: list[str] = []

    async def _proposal_helper(chat_id: str) -> bool:
        proposed.append(chat_id)
        return False

    monkeypatch.setattr(manager, "_maybe_archive_proposal_helper", _proposal_helper)
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n", encoding="utf-8")
    pass_id = manager.enqueue_memory_pass(_source(manager, "First"), None, archive, "")
    chat = manager.get_chat(pass_id)
    assert chat is not None
    # Mid-turn, as `stream_chat` leaves a chat: the slot is held, nothing settled.
    chat.last_response = ""
    chat.last_response_status = "running"
    manager._providers[pass_id] = _DrainingProvider(  # type: ignore[assignment]
        [ResultEvent(type="result", result="the vault is locked", is_error=True)]
    )

    await manager._drain_between_turns(pass_id, chat.project_id)
    await _await_detached(manager)

    assert chat.last_response_status == "error"
    assert chat.archived is False
    assert chat.helper["state"] == "attention"
    # The proposal helper stays success-gated: a failure proposes nothing, and
    # the queued pass is untouched because this one already held the slot.
    assert proposed == []
    assert streams.chat_ids == [pass_id]


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


# ── Guardrails ───────────────────────────────────────────────────────────
# A pass must be constrained beyond an ordinary chat: MCP and `gws` are denied
# on Claude, and on opencode (which ignores `disallowed_tools`) the request
# carries a marker that selects the guardrail ruleset.


def _pass_chat(manager: ProjectChatManager) -> ChatInfo:
    """A pass chat in this workspace's Memory project, on the claude provider."""
    project = manager._memory_pass.ensure_project("work")
    return manager.create_chat(
        project.project_id,
        title="Memory pass · Pricing rework",
        helper={
            "kind": "memory_pass",
            "source_chat_id": "chat-1",
            "archive_policy": "when_clean",
        },
    )


def test_memory_pass_chat_denies_mcp_and_gws_tools(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    # A declared server reaches every workspace whose allowlist does not name
    # it, so an ordinary chat already denies it; the pass must deny the rest.
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"n8n": {"command": "n8n"}}}), encoding="utf-8"
    )
    ordinary = _source(manager)
    pass_chat = _pass_chat(manager)

    denied = manager.disallowed_tools_for_chat(pass_chat)
    base = manager.disallowed_tools_for_chat(ordinary)
    # The workspace's own list is kept, not replaced.
    assert denied[: len(base)] == base
    assert "EnterPlanMode" in denied
    assert "mcp__n8n" in denied
    # gws is blocked at the skill layer: the shipped set, read from disk.
    gws = [tool for tool in denied if tool.startswith("Skill(gws-")]
    assert "Skill(gws-gmail)" in gws

    # An ordinary chat is unchanged: no gws deny, and the declared server is
    # denied for the reachability reason it always was.
    assert not [tool for tool in base if tool.startswith("Skill(gws-")]
    assert "mcp__n8n" in base


def test_build_agent_request_marks_only_the_memory_pass(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    ordinary = _source(manager)
    pass_chat = _pass_chat(manager)

    assert manager.build_agent_request(pass_chat, prompt="p").memory_pass is True
    assert manager.build_agent_request(ordinary, prompt="p").memory_pass is False
