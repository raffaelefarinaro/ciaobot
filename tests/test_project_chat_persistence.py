from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
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


def _persisted_chats(tmp_path: Path) -> dict[str, dict]:
    payload = json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )
    assert payload["revision"] > 0
    return payload["chats"]


def test_stale_manager_does_not_drop_chat_created_by_other_process(tmp_path: Path) -> None:
    first = _make_manager(tmp_path)
    project = first.create_project("Shared", workspace="work")
    stale = _make_manager(tmp_path)

    first_chat = first.create_chat(project.project_id, title="Created by first")
    stale_chat = stale.create_chat(project.project_id, title="Created by stale")

    chats = _persisted_chats(tmp_path)
    assert first_chat.chat_id in chats
    assert stale_chat.chat_id in chats


def test_concurrent_field_updates_to_one_chat_are_merged(tmp_path: Path) -> None:
    first = _make_manager(tmp_path)
    project = first.create_project("Shared", workspace="work")
    chat = first.create_chat(project.project_id, title="Original")
    stale = _make_manager(tmp_path)

    first.rename_chat(chat.chat_id, "Renamed")
    stale_chat = stale.get_chat(chat.chat_id)
    assert stale_chat is not None
    stale_chat.last_read_at = "2026-07-14T12:00:00Z"
    stale._save()

    persisted = _persisted_chats(tmp_path)[chat.chat_id]
    assert persisted["title"] == "Renamed"
    assert persisted["last_read_at"] == "2026-07-14T12:00:00Z"


def test_stale_manager_does_not_resurrect_concurrently_deleted_chat(tmp_path: Path) -> None:
    first = _make_manager(tmp_path)
    project = first.create_project("Shared", workspace="work")
    deleted = first.create_chat(project.project_id, title="Delete me")
    survivor = first.create_chat(project.project_id, title="Keep me")
    stale = _make_manager(tmp_path)

    assert first.delete_chat(deleted.chat_id) is True
    stale.rename_chat(survivor.chat_id, "Still here")

    chats = _persisted_chats(tmp_path)
    assert deleted.chat_id not in chats
    assert chats[survivor.chat_id]["title"] == "Still here"


def test_vault_project_identity_is_stable_after_registry_rebuild(tmp_path: Path) -> None:
    folder = tmp_path / "memory-vault" / "work" / "projects" / "active" / "rossmann-mvp"
    folder.mkdir(parents=True)
    (folder / "README.md").write_text(
        "---\ntitle: Rossmann MVP\ndescription: Shelf recognition\n---\n",
        encoding="utf-8",
    )

    first = _make_manager(tmp_path)
    first_project = next(
        project for project in first.list_projects("work")
        if project.vault_folder == "rossmann-mvp"
    )
    (tmp_path / ".runtime" / "web_projects.json").unlink()

    rebuilt = _make_manager(tmp_path)
    rebuilt_project = next(
        project for project in rebuilt.list_projects("work")
        if project.vault_folder == "rossmann-mvp"
    )
    assert rebuilt_project.project_id == first_project.project_id


def test_registry_audit_records_chat_create_and_delete(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Audited", workspace="work")
    chat = manager.create_chat(project.project_id, title="Temporary")
    assert manager.delete_chat(chat.chat_id) is True

    audit_path = tmp_path / ".runtime" / "web_projects.audit.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert any(chat.chat_id in event["chats"]["added"] for event in events)
    assert any(
        event["reason"] == "user_chat_delete"
        and chat.chat_id in event["chats"]["deleted"]
        for event in events
    )
