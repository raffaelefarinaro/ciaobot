"""``chat_result_ready`` names the workspace only when that is a real question.

The macOS client heads its completion banner with the workspace so a
notification says where it came from. On a single-workspace server that name
is noise, so the engine omits the field instead of sending one nobody needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager

from tests.conftest import attach_stub_mcp


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


def _announce(
    pcm: ProjectChatManager, workspace: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Publish one result for a chat in ``workspace`` and return that event."""
    project = pcm.create_project("Work", workspace=workspace)
    chat = pcm.create_chat(project.project_id, title="Weekly sweep")
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(pcm._events, "publish", published.append)
    pcm._announce_result_ready(chat.chat_id, project.project_id, "Weekly sweep", "Done")
    events = [event for event in published if event.get("type") == "chat_result_ready"]
    assert len(events) == 1
    return events[0]


def test_a_single_workspace_server_sends_no_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)

    event = _announce(pcm, "personal", monkeypatch)

    assert "workspace" not in event
    assert set(event) == {"type", "chat_id", "project_id", "title", "snippet"}


def test_a_multi_workspace_server_names_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    # The registry is the fact that matters: this server can serve two
    # workspaces, so a result from either one must say which.
    pcm._config.workspaces["work"] = pcm._config.workspaces["personal"]

    event = _announce(pcm, "personal", monkeypatch)

    assert event["workspace"] == "personal"
