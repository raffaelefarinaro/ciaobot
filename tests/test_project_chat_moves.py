"""Tests for moving chats between projects via update_chat,
plus event-broadcast coverage for project CRUD."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ArchiveOutcome, ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    """Build a ProjectChatManager backed by tmp_path-only stores."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


class _EventCapture:
    """Test helper: registers itself as an EventsHub subscriber by inserting
    a plain asyncio.Queue into the hub's `_subs` set, so synchronous publishes
    land directly in `events` for assertion."""

    def __init__(self, pcm: ProjectChatManager) -> None:
        self._pcm = pcm
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        pcm._events._subs.add(self.queue)

    def drain(self) -> list[dict]:
        out: list[dict] = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                return out

    def close(self) -> None:
        self._pcm._events._subs.discard(self.queue)


def test_move_chat_happy_path(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    dst = pcm.create_project("2026-q2-dest", workspace="work")
    chat = pcm.create_chat(src.project_id, title="movable")

    cap = _EventCapture(pcm)
    moved = pcm.update_chat(chat.chat_id, project_id=dst.project_id)
    assert moved is not None
    assert moved.project_id == dst.project_id

    events = cap.drain()
    move_events = [e for e in events if e.get("type") == "chat_moved"]
    assert len(move_events) == 1
    assert move_events[0]["chat_id"] == chat.chat_id
    assert move_events[0]["project_id"] == dst.project_id
    assert move_events[0]["old_project_id"] == src.project_id


def test_move_chat_same_project_is_noop(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    chat = pcm.create_chat(src.project_id, title="stationary")

    cap = _EventCapture(pcm)
    result = pcm.update_chat(chat.chat_id, project_id=src.project_id)
    assert result is not None
    assert result.project_id == src.project_id

    events = cap.drain()
    assert not [e for e in events if e.get("type") == "chat_moved"]


def test_move_chat_rejects_cross_workspace(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-work", workspace="work")
    dst = pcm.create_project("2026-q2-personal", workspace="personal")
    chat = pcm.create_chat(src.project_id, title="cross-ws")

    with pytest.raises(ValueError, match="workspace"):
        pcm.update_chat(chat.chat_id, project_id=dst.project_id)

    # Chat should remain in the original project.
    assert pcm.get_chat(chat.chat_id).project_id == src.project_id


def test_move_chat_rejects_unknown_project(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    chat = pcm.create_chat(src.project_id, title="orphaning")

    with pytest.raises(ValueError, match="not found"):
        pcm.update_chat(chat.chat_id, project_id="proj-doesnotexist")

    assert pcm.get_chat(chat.chat_id).project_id == src.project_id


def test_move_chat_rejects_archived(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    dst = pcm.create_project("2026-q2-dest", workspace="work")
    chat = pcm.create_chat(src.project_id, title="archived")
    # Mark archived directly to avoid the full archive_chat side effects.
    pcm._chats[chat.chat_id].archived = True

    with pytest.raises(ValueError, match="archived"):
        pcm.update_chat(chat.chat_id, project_id=dst.project_id)


def test_update_chat_other_fields_still_work(tmp_path: Path) -> None:
    """Adding project_id support must not regress title/model/mode updates."""
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    chat = pcm.create_chat(src.project_id, title="orig")

    updated = pcm.update_chat(
        chat.chat_id, title="renamed", model="opus", mode="auto"
    )
    assert updated is not None
    assert updated.title == "renamed"
    assert updated.model == "opus"
    assert updated.mode == "auto"
    # project_id unchanged when not provided.
    assert updated.project_id == src.project_id


def test_create_project_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    cap = _EventCapture(pcm)

    p = pcm.create_project("2026-q2-broadcast", workspace="work")
    events = cap.drain()
    created = [e for e in events if e.get("type") == "project_created"]
    assert len(created) == 1
    assert created[0]["project"]["project_id"] == p.project_id
    assert created[0]["project"]["name"] == "2026-q2-broadcast"


def test_update_project_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    p = pcm.create_project("2026-q2-renameable", workspace="work")
    cap = _EventCapture(pcm)

    pcm.update_project(p.project_id, name="2026-q2-renamed")
    events = cap.drain()
    updated = [e for e in events if e.get("type") == "project_updated"]
    assert len(updated) == 1
    assert updated[0]["project"]["name"] == "2026-q2-renamed"


def test_delete_project_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    p = pcm.create_project("2026-q2-doomed", workspace="work")
    cap = _EventCapture(pcm)

    pcm.delete_project(p.project_id)
    events = cap.drain()
    deleted = [e for e in events if e.get("type") == "project_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["project_id"] == p.project_id


def test_delete_project_rejects_vault_backed(tmp_path: Path) -> None:
    """Deleting a vault-backed project must fail: otherwise auto-discovery
    re-creates the project on the next list_projects() call. The user must
    use complete_project (which moves the vault entry) or remove the vault
    entry directly."""
    parent = tmp_path / "memory-vault" / "personal" / "projects" / "active"
    folder = parent / "Stuck"
    folder.mkdir(parents=True)
    (folder / "Stuck.md").write_text(
        "---\nname: Stuck\nstatus: active\n---\n# Stuck\n",
        encoding="utf-8",
    )

    pcm = _make_manager(tmp_path)
    pcm.list_projects()  # triggers auto-discovery
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "Stuck")

    with pytest.raises(ValueError, match="vault entry"):
        pcm.delete_project(proj.project_id)

    # Project must remain in state — the guard fires before any mutation.
    assert proj.project_id in pcm._projects
    # Vault folder must remain untouched.
    assert (folder / "Stuck.md").exists()


def test_delete_project_allows_manual_project_without_vault_folder(tmp_path: Path) -> None:
    """Manually-created projects (no vault_folder) can be deleted normally."""
    pcm = _make_manager(tmp_path)
    p = pcm.create_project("Manual", workspace="personal")
    assert p.vault_folder == ""

    ok = pcm.delete_project(p.project_id)
    assert ok is True
    assert p.project_id not in pcm._projects


@pytest.mark.asyncio
@pytest.mark.parametrize(("turn_count", "expected"), [(1, False), (2, True)])
async def test_archive_postprocess_runs_insights_for_multiturn_chats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turn_count: int,
    expected: bool,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("insights-project", workspace="work")
    chat = pcm.create_chat(project.project_id, title="insights chat")
    calls: list[dict] = []

    async def fake_extract_and_append(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("ciao.insights.extract_and_append", fake_extract_and_append)

    pcm.run_archive_postprocess(
        chat.chat_id,
        ArchiveOutcome(
            path=tmp_path / "archive.md",
            session_id="session-1",
            turn_count=turn_count,
            filtered_jsonl="filtered transcript",
        ),
        chat,
        project,
    )
    await asyncio.sleep(0)

    assert bool(calls) is expected


# ── Empty-chat cleanup ──────────────────────────────────────────────────


def test_create_chat_sweeps_prior_empty_chat(tmp_path: Path) -> None:
    """Creating a second chat while the first is still empty drops the first."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-sweep", workspace="work")

    empty = pcm.create_chat(project.project_id)  # default title, no turns
    assert empty.chat_id in pcm._chats

    cap = _EventCapture(pcm)
    fresh = pcm.create_chat(project.project_id)

    assert empty.chat_id not in pcm._chats, "empty chat should have been swept"
    assert fresh.chat_id in pcm._chats

    deleted = [e for e in cap.drain() if e.get("type") == "chat_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["chat_id"] == empty.chat_id
    assert deleted[0]["reason"] == "empty"


def test_create_chat_preserves_non_empty_chats(tmp_path: Path) -> None:
    """Chats that have user turns or a session are kept when a new one opens."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-keep", workspace="work")

    used = pcm.create_chat(project.project_id)
    pcm._chats[used.chat_id].user_turn_count = 1  # simulate a sent message

    renamed = pcm.create_chat(project.project_id)
    renamed.title = "Planning next quarter"
    pcm._chats[renamed.chat_id].title = "Planning next quarter"

    pcm.create_chat(project.project_id)  # triggers sweep

    assert used.chat_id in pcm._chats
    assert renamed.chat_id in pcm._chats


def test_startup_sweeps_empty_chats(tmp_path: Path) -> None:
    """An empty chat saved to disk should not survive a manager restart."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-startup", workspace="work")
    orphan = pcm.create_chat(project.project_id)
    assert orphan.chat_id in pcm._chats

    # Simulate restart by building a fresh manager against the same state dir.
    pcm2 = _make_manager(tmp_path)
    assert orphan.chat_id not in pcm2._chats


def test_delete_chat_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-delete", workspace="work")
    chat = pcm.create_chat(project.project_id)
    pcm._chats[chat.chat_id].user_turn_count = 1  # keep it out of the sweep

    cap = _EventCapture(pcm)
    assert pcm.delete_chat(chat.chat_id) is True

    deleted = [e for e in cap.drain() if e.get("type") == "chat_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["chat_id"] == chat.chat_id
    assert deleted[0]["reason"] == "user"


async def test_archive_chat_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-archive", workspace="work")
    chat = pcm.create_chat(project.project_id)

    cap = _EventCapture(pcm)
    await pcm.archive_chat(chat.chat_id)

    archived = [e for e in cap.drain() if e.get("type") == "chat_archived"]
    assert len(archived) == 1
    assert archived[0]["chat_id"] == chat.chat_id
    assert archived[0]["project_id"] == project.project_id


async def test_archiving_supervisor_also_archives_delegate_subchats(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-delegate-archive", workspace="work")
    parent = pcm.create_chat(project.project_id, title="supervisor")
    child = pcm.create_chat(
        project.project_id,
        title="subchat",
        spawned_from_chat_id=parent.chat_id,
    )

    cap = _EventCapture(pcm)
    await pcm.archive_chat(parent.chat_id)

    assert pcm.get_chat(parent.chat_id).archived is True
    assert pcm.get_chat(child.chat_id).archived is True
    archived_ids = [
        event["chat_id"]
        for event in cap.drain()
        if event.get("type") == "chat_archived"
    ]
    assert archived_ids == [parent.chat_id, child.chat_id]


async def test_archive_cascade_reaches_nested_delegate_descendants(tmp_path: Path) -> None:
    """Delegates cannot nest today, but the cascade walks the whole tree so a
    registry that does contain a grandchild still archives cleanly."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-nested-archive", workspace="work")
    parent = pcm.create_chat(project.project_id, title="supervisor")
    child = pcm.create_chat(
        project.project_id,
        title="subchat",
        spawned_from_chat_id=parent.chat_id,
    )
    grandchild = pcm.create_chat(
        project.project_id,
        title="sub-subchat",
        spawned_from_chat_id=child.chat_id,
    )
    unrelated = pcm.create_chat(project.project_id, title="unrelated")

    await pcm.archive_chat(parent.chat_id)

    assert pcm.get_chat(child.chat_id).archived is True
    assert pcm.get_chat(grandchild.chat_id).archived is True
    assert pcm.get_chat(unrelated.chat_id).archived is False


async def test_archive_cascade_postprocesses_each_subchat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every auto-archived subchat gets its own post-archive processing, and a
    subchat that fails to archive neither strands its siblings nor swallows the
    supervisor's own outcome (which is what drives the caller's post-processing).
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-cascade-postprocess", workspace="work")
    parent = pcm.create_chat(project.project_id, title="supervisor")
    good = pcm.create_chat(
        project.project_id,
        title="healthy subchat",
        spawned_from_chat_id=parent.chat_id,
    )
    bad = pcm.create_chat(
        project.project_id,
        title="failing subchat",
        spawned_from_chat_id=parent.chat_id,
    )

    # An empty chat archives to None, which would skip post-processing
    # entirely; stub the transcript write so each chat yields a real outcome.
    def fake_archive_session(*, ctx: object, **_kwargs: object) -> Path:
        chat_id = ctx.key  # type: ignore[attr-defined]
        if chat_id == bad.chat_id:
            raise RuntimeError("transcript write failed")
        return tmp_path / f"{chat_id}.md"

    monkeypatch.setattr(pcm._transcripts, "archive_session", fake_archive_session)

    postprocessed: list[str] = []
    monkeypatch.setattr(
        pcm,
        "run_archive_postprocess",
        lambda chat_id, outcome, chat_meta, project_meta: postprocessed.append(chat_id),
    )

    result = await pcm.archive_chat(parent.chat_id)

    assert result is not None
    assert result.outcome is not None
    # The supervisor's post-processing is the caller's job, so only the
    # cascaded subchat is handled here.
    assert postprocessed == [good.chat_id]
    assert pcm.get_chat(parent.chat_id).archived is True
    assert pcm.get_chat(good.chat_id).archived is True
    # The failure is skipped, not retried — but it is reported, not swallowed.
    # A half-archived delegate (MCP grant revoked, `archived` never set) cannot
    # be healed by _reconcile_half_archived_chats, so a bare ok would leave the
    # user with no way to know it needs attention.
    assert pcm.get_chat(bad.chat_id).archived is False
    assert result.archived_ids() == [good.chat_id]
    assert result.failed_ids() == [bad.chat_id]
    failed_row = next(row for row in result.delegates if row.chat_id == bad.chat_id)
    assert failed_row.archived is False
    assert "transcript write failed" in failed_row.error


async def test_archive_stops_a_running_delegate_before_snapshotting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering is the whole point, not just that both steps happen.

    Archiving snapshots the transcript, revokes the MCP grant and deletes the
    provider session blob. A delegate still mid-turn at that moment loses
    whatever it emits next, into a chat that can no longer be continued. So the
    stop has to land first — and because the archive is immediate rather than
    deferred, the cascade has to report that it discarded a running turn.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-stop-before-archive", workspace="work")
    parent = pcm.create_chat(project.project_id, title="supervisor")
    running = pcm.create_chat(
        project.project_id,
        title="busy subchat",
        spawned_from_chat_id=parent.chat_id,
    )
    idle = pcm.create_chat(
        project.project_id,
        title="idle subchat",
        spawned_from_chat_id=parent.chat_id,
    )

    calls: list[str] = []
    live = {running.chat_id}

    # A live turn is exactly "the broker has a stream for this chat".
    real_get = pcm._broker.get
    monkeypatch.setattr(
        pcm._broker,
        "get",
        lambda chat_id: object() if chat_id in live else real_get(chat_id),
    )

    async def fake_stop(chat_id: str) -> bool:
        calls.append(f"stop:{chat_id}")
        live.discard(chat_id)
        return True

    monkeypatch.setattr(pcm, "stop_chat", fake_stop)

    def fake_archive_session(*, ctx: object, **_kwargs: object) -> Path:
        chat_id = ctx.key  # type: ignore[attr-defined]
        calls.append(f"archive:{chat_id}")
        return tmp_path / f"{chat_id}.md"

    monkeypatch.setattr(pcm._transcripts, "archive_session", fake_archive_session)
    monkeypatch.setattr(
        pcm, "run_archive_postprocess", lambda *_a, **_k: None
    )

    result = await pcm.archive_chat(parent.chat_id)

    assert result is not None
    # The running delegate's turn ends before its transcript is written.
    assert calls.index(f"stop:{running.chat_id}") < calls.index(
        f"archive:{running.chat_id}"
    )
    # An idle delegate is not stopped at all — nothing to stop.
    assert f"stop:{idle.chat_id}" not in calls
    # Only the delegate that was actually running is reported as interrupted,
    # and both still end up archived.
    assert result.stopped_ids() == [running.chat_id]
    assert sorted(result.archived_ids()) == sorted([running.chat_id, idle.chat_id])
    assert pcm.get_chat(running.chat_id).archived is True
    assert pcm.get_chat(idle.chat_id).archived is True


async def test_archive_route_reports_the_cascade_per_subchat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response has to name what happened, not answer a bare ok.

    The PWA marks children archived from `archived_chat_ids`; a delegate the
    server skipped must stay out of it, or the client hides a chat that is still
    streaming and spending tokens.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-archive-report", workspace="work")
    parent = pcm.create_chat(project.project_id, title="supervisor")
    good = pcm.create_chat(
        project.project_id,
        title="healthy subchat",
        spawned_from_chat_id=parent.chat_id,
    )
    bad = pcm.create_chat(
        project.project_id,
        title="failing subchat",
        spawned_from_chat_id=parent.chat_id,
    )

    live = {good.chat_id}
    real_get = pcm._broker.get
    monkeypatch.setattr(
        pcm._broker,
        "get",
        lambda chat_id: object() if chat_id in live else real_get(chat_id),
    )

    async def fake_stop(chat_id: str) -> bool:
        live.discard(chat_id)
        return True

    monkeypatch.setattr(pcm, "stop_chat", fake_stop)

    def fake_archive_session(*, ctx: object, **_kwargs: object) -> Path:
        chat_id = ctx.key  # type: ignore[attr-defined]
        if chat_id == bad.chat_id:
            raise RuntimeError("transcript write failed")
        return tmp_path / f"{chat_id}.md"

    monkeypatch.setattr(pcm._transcripts, "archive_session", fake_archive_session)
    monkeypatch.setattr(pcm, "run_archive_postprocess", lambda *_a, **_k: None)

    from starlette.requests import Request

    from ciao.web.routes_api import chat_archive

    app = SimpleNamespace(state=SimpleNamespace(project_chat_manager=pcm))
    request = Request({
        "type": "http",
        "method": "POST",
        "path": f"/api/chats/{parent.chat_id}/archive",
        "headers": [],
        "path_params": {"chat_id": parent.chat_id},
        "app": app,
    })
    response = await chat_archive(request)
    payload = json.loads(response.body)

    assert payload["ok"] is True
    # The supervisor and the subchat that made it, and nothing else.
    assert payload["archived_chat_ids"] == [parent.chat_id, good.chat_id]
    assert bad.chat_id not in payload["archived_chat_ids"]
    assert payload["stopped_chat_ids"] == [good.chat_id]
    assert payload["failed_chat_ids"] == [bad.chat_id]
    rows = {row["chat_id"]: row for row in payload["subchats"]}
    assert rows[good.chat_id]["archived"] is True
    assert rows[good.chat_id]["stopped_mid_turn"] is True
    assert rows[bad.chat_id]["archived"] is False
    assert "transcript write failed" in rows[bad.chat_id]["error"]


async def test_delete_and_archive_reclaim_codex_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("codex-reclaim", workspace="work")
    deleted_ids: list[str] = []

    async def _fake_delete(_workspace, thread_id: str, command=None) -> bool:
        deleted_ids.append(thread_id)
        return True

    monkeypatch.setattr(
        "ciao.web.project_chats.CodexProvider.delete_thread",
        _fake_delete,
    )

    chat = pcm.create_chat(project.project_id, title="to-delete")
    chat.provider = "codex"
    chat.session_id = "thread-delete"
    chat.user_turn_count = 1
    # The reclaim fires the delete through asyncio.ensure_future, so this needs
    # a running loop and one yield for the task to actually run.
    assert pcm.delete_chat(chat.chat_id) is True
    await asyncio.sleep(0)
    assert deleted_ids == ["thread-delete"]

    archived = pcm.create_chat(project.project_id, title="to-archive")
    archived.provider = "codex"
    archived.session_id = "thread-archive"
    deleted_ids.clear()
    await pcm.archive_chat(archived.chat_id)
    await asyncio.sleep(0)
    assert deleted_ids == ["thread-archive"]


def test_new_session_reclaims_codex_thread_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("codex-new-session", workspace="work")
    chat = pcm.create_chat(project.project_id, title="rotate")
    chat.provider = "codex"
    chat.session_id = "thread-current"
    chat.previous_session_ids = ["thread-old"]
    chat.user_turn_count = 1

    deleted_ids: list[str] = []

    async def _fake_delete(_workspace, thread_id: str, command=None) -> bool:
        deleted_ids.append(thread_id)
        return True

    monkeypatch.setattr(
        "ciao.web.project_chats.CodexProvider.delete_thread",
        _fake_delete,
    )

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        pcm.new_session(chat.chat_id)
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert deleted_ids == ["thread-old", "thread-current"]
    assert chat.session_id == ""
    assert chat.previous_session_ids == []
