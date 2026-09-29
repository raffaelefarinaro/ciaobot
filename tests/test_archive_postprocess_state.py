"""The memory-pass record an archived chat carries.

Archiving a chat queues a memory pass; the pass writes its status onto the
archived chat's ``postprocess`` record so the PWA can link to it. These tests
cover what that record looks like after a restart, and the disk half of
archiving that feeds it.
"""

from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig
from ciao.models import ChatContext
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_service import _restored_postprocess
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _chat(manager: ProjectChatManager) -> str:
    project = manager.create_project("Work", workspace="work")
    return manager.create_chat(project.project_id, title="A chat").chat_id


# ── Restore across restarts ───────────────────────────────────────────────


def test_restore_keeps_only_the_memory_pass_step() -> None:
    """Records from the trajectory pipeline carry run state nothing reads."""
    restored = _restored_postprocess({
        "state": "running",
        "step": "trajectory",
        "job": {"state": "incomplete"},
        "steps": {
            "trajectory": {"status": "ok", "extra": {}},
            "memory_pass": {"status": "ok", "extra": {"chat_id": "m1"}},
        },
        "updated_at": "2026-09-01T00:00:00Z",
    })
    assert restored == {
        "steps": {"memory_pass": {"status": "ok", "extra": {"chat_id": "m1"}}},
        "updated_at": "2026-09-01T00:00:00Z",
    }


def test_a_record_without_a_memory_pass_loads_as_empty() -> None:
    assert _restored_postprocess(
        {"state": "done", "steps": {"trajectory": {"status": "ok"}}}
    ) == {}


def test_missing_or_junk_records_load_as_empty() -> None:
    assert _restored_postprocess(None) == {}
    assert _restored_postprocess({}) == {}
    assert _restored_postprocess("nonsense") == {}
    assert _restored_postprocess({"steps": "nope"}) == {}


def test_the_memory_pass_record_survives_a_reload(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    record = {"steps": {"memory_pass": {"status": "queued", "extra": {"chat_id": "m1"}}}}
    manager.get_chat(chat_id).postprocess = dict(record)
    manager._save()

    reloaded = _make_manager(tmp_path)

    assert reloaded.get_chat(chat_id).postprocess == record


# ── Archive inputs ────────────────────────────────────────────────────────


def test_archive_inputs_read_the_turn_count_before_archiving(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    chat = manager.get_chat(chat_id)
    assert chat is not None
    chat.provider = "opencode"
    archive_path = tmp_path / "archive.md"
    archive_path.write_text("# archived", encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(
        manager._transcripts,
        "peek_turn_count",
        lambda _ctx, provider: calls.append(f"peek:{provider}") or 1,
    )
    monkeypatch.setattr(
        manager._transcripts,
        "archive_session",
        lambda **_kwargs: calls.append("archive") or archive_path,
    )

    turn_count, result = manager._read_archive_inputs(ChatContext.for_web(chat_id), chat)

    assert turn_count == 1
    assert result == archive_path
    assert calls == ["peek:opencode", "archive"]
