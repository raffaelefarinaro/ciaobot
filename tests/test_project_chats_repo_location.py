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
