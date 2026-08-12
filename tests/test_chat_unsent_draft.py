"""An unsent composer draft keeps a chat out of the empty-chat sweeps.

The draft text lives in the browser that typed it, so the server cannot see it.
But the server owns the emptiness rule — `user_turn_count` never reaches the
client — and three separate paths act on it: the `only_if_empty` delete behind
closing a chat, the cleanup `create_chat` runs, and the cleanup at startup.
Before `has_unsent_draft` existed, typing a prompt into a New Chat and then
creating another chat deleted the first one and stranded the draft in a
localStorage key nothing could reach again.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_detail


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


def test_a_chat_with_an_unsent_draft_is_not_empty(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    assert manager.is_empty_chat(chat.chat_id) is True

    manager.set_unsent_draft(chat.chat_id, True)
    assert manager.is_empty_chat(chat.chat_id) is False

    manager.set_unsent_draft(chat.chat_id, False)
    assert manager.is_empty_chat(chat.chat_id) is True


def test_creating_another_chat_does_not_sweep_away_a_draft(tmp_path: Path) -> None:
    """The path a user actually hits: type, then start a second chat."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    drafted = manager.create_chat(project.project_id, title="New Chat")
    manager.set_unsent_draft(drafted.chat_id, True)

    manager.create_chat(project.project_id, title="New Chat")

    assert drafted.chat_id in manager._chats


def test_an_untouched_new_chat_is_still_swept(tmp_path: Path) -> None:
    """The flag must not turn the cleanup off for genuinely abandoned drafts."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    abandoned = manager.create_chat(project.project_id, title="New Chat")

    manager.create_chat(project.project_id, title="New Chat")

    assert abandoned.chat_id not in manager._chats


def test_the_draft_flag_survives_a_restart(tmp_path: Path) -> None:
    """The startup sweep runs against reloaded state, so the flag must persist."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    manager.set_unsent_draft(chat.chat_id, True)

    reloaded = _make_manager(tmp_path)

    assert reloaded._chats[chat.chat_id].has_unsent_draft is True
    assert reloaded.is_empty_chat(chat.chat_id) is False


def test_state_written_before_the_field_existed_loads_as_no_draft(
    tmp_path: Path,
) -> None:
    """Titled so the startup sweep keeps it: what is under test is the default
    the loader applies to state that predates the field, not the sweep."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="Kept by its title")
    manager.set_unsent_draft(chat.chat_id, True)

    path = manager._path
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["chats"][chat.chat_id]["has_unsent_draft"]
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = _make_manager(tmp_path)

    assert reloaded._chats[chat.chat_id].has_unsent_draft is False


def test_setting_the_same_value_twice_is_idempotent(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    assert manager.set_unsent_draft(chat.chat_id, True) is not None
    assert manager.set_unsent_draft(chat.chat_id, True) is not None
    assert manager._chats[chat.chat_id].has_unsent_draft is True


def test_setting_a_draft_on_a_missing_chat_returns_none(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)

    assert manager.set_unsent_draft("nope", True) is None


def _make_client(manager: ProjectChatManager) -> TestClient:
    app = Starlette(
        routes=[
            Route(
                "/api/chats/{chat_id}", chat_detail, methods=["PATCH", "DELETE"]
            )
        ]
    )
    app.state.project_chat_manager = manager
    app.state.config = manager._config
    return TestClient(app, raise_server_exceptions=False)


def test_patch_reports_the_draft_and_delete_then_declines(tmp_path: Path) -> None:
    """The round trip the composer actually performs."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    client = _make_client(manager)

    res = client.patch(f"/api/chats/{chat.chat_id}", json={"has_unsent_draft": True})
    assert res.status_code == 200
    assert res.json()["has_unsent_draft"] is True

    # Closing the chat must now leave it alone.
    res = client.delete(f"/api/chats/{chat.chat_id}?only_if_empty=1")
    assert res.json() == {"ok": False, "deleted": False, "reason": "not empty"}
    assert chat.chat_id in manager._chats

    # Clearing the composer hands it back to the sweep.
    client.patch(f"/api/chats/{chat.chat_id}", json={"has_unsent_draft": False})
    res = client.delete(f"/api/chats/{chat.chat_id}?only_if_empty=1")
    assert res.json()["deleted"] is True


def test_patch_rejects_a_non_boolean_draft_flag(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    client = _make_client(manager)

    res = client.patch(f"/api/chats/{chat.chat_id}", json={"has_unsent_draft": "yes"})

    assert res.status_code == 400
    assert manager._chats[chat.chat_id].has_unsent_draft is False
