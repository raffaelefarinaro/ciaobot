from __future__ import annotations

from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _manager(tmp_path: Path) -> ProjectChatManager:
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


@pytest.mark.asyncio
async def test_repo_location_round_trips_and_clears_when_unlinked(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Repo", workspace="work")
    chat = manager.create_chat(project.project_id, title="C")
    loc = {"path": "/x/wt", "branch": "feat", "is_worktree": True, "exists": True}
    chat.repo_location = loc
    manager._save()

    reloaded = _manager(tmp_path)
    restored = reloaded.get_chat(chat.chat_id)
    assert restored is not None
    assert restored.repo_location == loc
    assert restored.to_dict()["repo_location"] == loc

    assert project.repo_path == ""
    assert await manager.refresh_repo_location(chat.chat_id, []) is None
    assert manager.get_chat(chat.chat_id).repo_location == {}


@pytest.mark.asyncio
async def test_delete_chat_drops_its_repo_location_lock(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Repo", workspace="work")
    chat = manager.create_chat(project.project_id, title="C")
    repo = tmp_path / "repo"
    repo.mkdir()
    project.repo_path = str(repo)
    chat.code_mode = True
    await manager.refresh_repo_location(chat.chat_id, [])
    assert chat.chat_id in manager._repo_location_locks

    manager.delete_chat(chat.chat_id)
    assert chat.chat_id not in manager._repo_location_locks


@pytest.mark.asyncio
async def test_to_dict_failure_still_returns_the_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Repo", workspace="work")
    chat = manager.create_chat(project.project_id, title="C")
    project.repo_path = str(tmp_path)
    chat.code_mode = True
    loc = {"path": "/x/wt", "branch": "feat", "is_worktree": True, "exists": True}

    async def fake_update(chat_id, hints):
        return loc, True

    def boom(self: object) -> dict:
        raise RuntimeError("git exploded")

    manager._update_repo_location = fake_update  # type: ignore[method-assign]
    monkeypatch.setattr(type(project), "to_dict", boom)
    assert await manager.refresh_repo_location(chat.chat_id, []) == loc


@pytest.mark.asyncio
async def test_code_mode_off_skips_location_refresh(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Repo", workspace="work")
    chat = manager.create_chat(project.project_id, title="C")
    repo = tmp_path / "repo"
    repo.mkdir()
    project.repo_path = str(repo)
    chat.repo_location = {"path": str(repo), "branch": "main", "is_worktree": False, "exists": True}
    assert chat.code_mode is False

    assert await manager.refresh_repo_location(chat.chat_id, []) is None
    assert chat.repo_location == {}
    assert chat.chat_id not in manager._repo_location_locks
