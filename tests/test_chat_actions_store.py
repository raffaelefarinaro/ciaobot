"""The chat-action store on ProjectChatManager: publishing, dedupe, updates, dismissals and lifecycle clears."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from ciao.chat_actions import ChatActionError
from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import (
    ChatActionConflictError,
    ChatInfo,
    ProjectChatManager,
)
from tests.test_chat_actions_runtime import make_manager


@contextmanager
def _record_events(manager: ProjectChatManager) -> Iterator[list[dict]]:
    """Collect the events the manager publishes while the block runs."""
    published: list[dict] = []
    hub = manager._events
    original = hub.publish
    hub.publish = published.append  # type: ignore[method-assign]
    try:
        yield published
    finally:
        hub.publish = original  # type: ignore[method-assign]


def _registry(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )


def _restart(manager: ProjectChatManager) -> ProjectChatManager:
    """Build a second manager over the same registry, as a restart would."""
    config = manager._config
    return ProjectChatManager(
        config,
        state_store=StateStore(
            config.state_path, config.workspace_root, config.media_root
        ),
        transcript_store=TranscriptStore(
            Path(config.state_path).parent,
            Path(config.state_path).parent / "transcripts",
        ),
        path=manager._path,
    )


def _one_chat(manager: ProjectChatManager) -> ChatInfo:
    project = manager.create_project("Actions", workspace="work")
    return manager.create_chat(project.project_id, title="Needs you")


def _action_events(published: list[dict]) -> list[dict]:
    return [e for e in published if e["type"] == "chat_actions_changed"]


def _sign_in(manager: ProjectChatManager, chat_id: str, server: str = "linear") -> dict:
    payload = manager.raise_action(
        chat_id,
        "mcp_auth",
        {"server": server, "provider": "opencode"},
        origin="engine",
    )
    assert payload is not None
    return payload


def test_a_raised_action_is_published_once_and_lands_in_the_chat_payload(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        payload = _sign_in(manager, chat.chat_id)

    assert payload["state"] == "open"
    assert payload["origin"] == "engine"
    assert len(_action_events(published)) == 1
    assert _action_events(published)[0]["actions"] == [payload]
    assert chat.to_dict()["actions"] == [payload]


def test_raising_the_same_key_and_params_is_a_no_op(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)

    with _record_events(manager) as published:
        second = _sign_in(manager, chat.chat_id)

    assert second["id"] == first["id"]
    assert published == []


def test_raising_a_subset_of_the_current_params_is_a_no_op(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)
    manager.update_action(
        chat.chat_id,
        first["id"],  # type: ignore[arg-type]
        state="waiting",
        params_update={"url": "https://x.example/a"},
    )

    with _record_events(manager) as published:
        again = _sign_in(manager, chat.chat_id)

    assert again["id"] == first["id"]
    assert published == []


def test_changed_params_replace_the_action_with_a_new_id(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)

    with _record_events(manager) as published:
        replaced = manager.raise_action(
            chat.chat_id,
            "mcp_auth",
            {"server": "linear", "provider": "claude"},
            origin="engine",
        )

    assert replaced is not None
    assert replaced["id"] != first["id"]
    assert len(chat.actions) == 1
    assert len(_action_events(published)) == 1


def test_an_unknown_chat_gets_no_action(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    assert (
        manager.raise_action(
            "no-such-chat",
            "mcp_auth",
            {"server": "linear", "provider": "opencode"},
            origin="engine",
        )
        is None
    )


def test_an_archived_chat_refuses_an_action(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    chat.archived = True

    with pytest.raises(ValueError):
        _sign_in(manager, chat.chat_id)


def test_an_invalid_action_never_reaches_the_state(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        with pytest.raises(ChatActionError):
            manager.raise_action(
                chat.chat_id,
                "mcp_auth",
                {"server": "linear", "provider": "codex"},
                origin="engine",
            )

    assert published == []
    assert chat.actions == []


def test_an_update_keeps_the_id_and_publishes_the_whole_list(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)

    with _record_events(manager) as published:
        updated = manager.update_action(chat.chat_id, first["id"], state="waiting")  # type: ignore[arg-type]

    assert updated is not None
    assert updated["id"] == first["id"]
    assert updated["state"] == "waiting"
    assert _action_events(published)[0]["actions"] == [updated]


def test_an_identical_update_publishes_nothing(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)

    with _record_events(manager) as published:
        manager.update_action(chat.chat_id, first["id"], state="open")  # type: ignore[arg-type]

    assert published == []


def test_an_update_revalidates_params(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)

    with pytest.raises(ChatActionError) as info:
        manager.update_action(
            chat.chat_id,
            first["id"],  # type: ignore[arg-type]
            state="waiting",
            params_update={"url": "javascript:x"},
        )

    assert info.value.reason == "invalid_url"
    assert chat.actions[0].state == "open"


def test_an_update_normalizes_the_error(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)

    updated = manager.update_action(
        chat.chat_id,
        first["id"],  # type: ignore[arg-type]
        state="failed",
        error="a\nb",
    )

    assert updated is not None
    assert updated["error"] == "a"


def test_an_agent_raise_with_an_engine_only_param_is_refused(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    params = {"server": "x", "provider": "opencode", "url": "https://a.example/"}

    with pytest.raises(ChatActionError) as info:
        manager.raise_action(chat.chat_id, "mcp_auth", params, origin="agent")

    assert info.value.reason == "unknown_param"
    assert chat.actions == []
    assert manager.raise_action(chat.chat_id, "mcp_auth", params, origin="engine") is not None


def test_a_dismissal_removes_the_action_and_remembers_its_key(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)

    removed = manager.dismiss_action(chat.chat_id, first["id"])  # type: ignore[arg-type]

    assert removed is not None
    assert removed["id"] == first["id"]
    assert chat.actions == []
    assert "mcp_auth:linear" in chat.dismissed_action_keys


def test_a_stale_dismissal_conflicts_with_the_current_list(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    current = _sign_in(manager, chat.chat_id)

    with pytest.raises(ChatActionConflictError) as info:
        manager.dismiss_action(chat.chat_id, "stale-id")

    assert info.value.actions == [current]
    assert chat.actions[0].id == current["id"]


def test_remove_action_removes_without_remembering_the_key(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)

    with _record_events(manager) as published:
        manager.remove_action(chat.chat_id, first["id"])  # type: ignore[arg-type]

    assert len(_action_events(published)) == 1
    assert chat.dismissed_action_keys == set()
    with pytest.raises(ChatActionConflictError):
        manager.remove_action(chat.chat_id, first["id"])  # type: ignore[arg-type]


def test_the_states_map_is_sparse_and_complete(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    with_action = _one_chat(manager)
    _one_chat(manager)
    first = _sign_in(manager, with_action.chat_id)

    states = manager.chat_action_states

    assert list(states) == [with_action.chat_id]
    assert states[with_action.chat_id] == [first]


def test_actions_never_reach_the_registry(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    _sign_in(manager, chat.chat_id)
    chat.dismissed_action_keys.add("mcp_auth:notion")
    manager._save()

    record = _registry(tmp_path)["chats"][chat.chat_id]

    assert "actions" not in record
    assert "dismissed_action_keys" not in record


def test_a_restart_starts_without_actions(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    _sign_in(manager, chat.chat_id)
    manager._save()

    restarted = _restart(manager)

    assert restarted.chat_action_states == {}


def test_a_new_session_clears_actions_and_dismissed_keys(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    _sign_in(manager, chat.chat_id)
    manager.dismiss_action(chat.chat_id, manager.chat_action_payloads(chat.chat_id)[0]["id"])  # type: ignore[arg-type]
    _sign_in(manager, chat.chat_id)

    with _record_events(manager) as published:
        manager.new_session(chat.chat_id)

    assert chat.actions == []
    assert chat.dismissed_action_keys == set()
    assert _action_events(published) == [
        {"type": "chat_actions_changed", "chat_id": chat.chat_id, "actions": []},
    ]


def test_a_handover_clears_actions(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    _sign_in(manager, chat.chat_id)

    with _record_events(manager) as published:
        manager.handover_chat(chat.chat_id, provider="claude", model="haiku")

    assert chat.actions == []
    assert _action_events(published) == [
        {"type": "chat_actions_changed", "chat_id": chat.chat_id, "actions": []},
    ]


@pytest.mark.asyncio
async def test_an_archive_clears_actions(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    _sign_in(manager, chat.chat_id)

    with _record_events(manager) as published:
        await manager.archive_chat(chat.chat_id)

    assert chat.actions == []
    assert _action_events(published) == [
        {"type": "chat_actions_changed", "chat_id": chat.chat_id, "actions": []},
    ]


def test_a_new_session_of_a_chat_without_actions_publishes_no_action_event(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        manager.new_session(chat.chat_id)

    assert _action_events(published) == []


@pytest.mark.asyncio
async def test_a_lifecycle_clear_cancels_each_removed_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    _sign_in(manager, chat.chat_id)
    removed_calls: list[tuple[str, list[Any]]] = []

    def record(chat_id: str, actions: list[Any]) -> None:
        removed_calls.append((chat_id, actions))

    monkeypatch.setattr(manager.actions_runtime, "cancel_removed", record)
    before = list(chat.actions)

    manager.new_session(chat.chat_id)

    assert removed_calls == [(chat.chat_id, before)]


def test_replacing_an_action_cancels_the_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    chat = _one_chat(manager)
    first = _sign_in(manager, chat.chat_id)
    old = manager.get_action(chat.chat_id, first["id"])  # type: ignore[arg-type]
    calls: list[tuple[str, list[Any]]] = []
    monkeypatch.setattr(
        manager.actions_runtime,
        "cancel_removed",
        lambda chat_id, actions: calls.append((chat_id, list(actions))),
    )

    manager.raise_action(
        chat.chat_id,
        "mcp_auth",
        {"server": "linear", "provider": "claude"},
        origin="engine",
    )

    assert calls == [(chat.chat_id, [old])]
