from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _messages(answer: str = "Answer") -> list[dict]:
    return [
        {"role": "user", "content": "Question", "turn_index": 0},
        {"role": "assistant", "content": answer},
    ]


def test_fork_chat_creates_fresh_independent_chat_with_copied_history(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(
        project.project_id,
        title="Original",
        model="gpt-5.2-codex",
        provider="codex",
    )
    source.mode = "plan"
    source.thinking_level = "high"
    source.session_id = "source-session"
    source.user_turn_count = 2
    manager._save()

    fork = manager.fork_chat(source.chat_id, messages=_messages(), turn_index=0)

    assert fork.chat_id != source.chat_id
    assert fork.project_id == source.project_id
    assert fork.title == "Original · Fork 1"
    assert fork.provider == "codex"
    assert fork.model == "gpt-5.2-codex"
    assert fork.mode == "plan"
    assert fork.thinking_level == "high"
    assert fork.session_id == ""
    assert fork.handover_context_pending is True
    assert fork.user_turn_count == 1
    assert fork.handover_messages == [
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": "Answer"},
    ]
    assert fork.forked_from_chat_id == source.chat_id
    assert fork.forked_from_turn_index == 0
    assert fork.fork_root_chat_id == source.chat_id
    assert fork.fork_index == 1
    assert fork.fork_base_title == "Original"

    persisted = json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )["chats"][fork.chat_id]
    assert persisted["forked_from_chat_id"] == source.chat_id
    assert persisted["fork_index"] == 1


def test_fork_numbering_is_root_relative_across_fork_of_fork(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Release planning")

    first = manager.fork_chat(source.chat_id, messages=_messages("One"), turn_index=0)
    second = manager.fork_chat(source.chat_id, messages=_messages("Two"), turn_index=0)
    third = manager.fork_chat(first.chat_id, messages=_messages("Three"), turn_index=0)

    assert first.title == "Release planning · Fork 1"
    assert second.title == "Release planning · Fork 2"
    assert third.title == "Release planning · Fork 3"
    assert third.fork_root_chat_id == source.chat_id
    assert third.fork_base_title == "Release planning"


def test_fork_chat_accepts_archived_source_without_mutating_it(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Archived source")
    source.archived = True
    manager._save()

    fork = manager.fork_chat(source.chat_id, messages=_messages(), turn_index=0)

    assert source.archived is True
    assert fork.archived is False


@pytest.mark.parametrize(
    ("messages", "turn_index", "error"),
    [
        ([], 0, "non-empty"),
        ([{"role": "user", "content": "Question"}], 0, "assistant"),
        ([{"role": "system", "content": "Activity"}], 0, "assistant"),
        (_messages(), 1, "turn"),
    ],
)
def test_fork_chat_rejects_invalid_snapshots(
    tmp_path: Path,
    messages: list[dict],
    turn_index: int,
    error: str,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Original")

    with pytest.raises(ValueError, match=error):
        manager.fork_chat(source.chat_id, messages=messages, turn_index=turn_index)


def test_fork_chat_rejects_missing_source(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)

    with pytest.raises(KeyError, match="not found"):
        manager.fork_chat("chat-missing", messages=_messages(), turn_index=0)


def test_deleting_source_does_not_delete_fork(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Original")
    fork = manager.fork_chat(source.chat_id, messages=_messages(), turn_index=0)

    assert manager.delete_chat(source.chat_id) is True

    assert manager.get_chat(fork.chat_id) is fork
    assert fork.title == "Original · Fork 1"


def test_fork_chat_duplicates_image_ownership(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="With image")
    attachment = manager.save_image_upload(b"image-bytes", "source.png")
    source_ref = attachment.path.name
    source.user_turn_count = 1
    source.user_turn_images = {"0": [source_ref]}
    manager._save()

    fork = manager.fork_chat(
        source.chat_id,
        messages=[
            {
                "role": "user",
                "content": "What is in this?",
                "turn_index": 0,
                "images": [source_ref],
            },
            {"role": "assistant", "content": "A test image."},
        ],
        turn_index=0,
    )

    fork_ref = fork.handover_messages[0]["images"][0]
    assert fork_ref != source_ref
    assert fork.user_turn_images == {"0": [fork_ref]}
    assert manager.resolve_image_ref(fork_ref) is not None

    assert manager.delete_chat(source.chat_id) is True
    assert manager.resolve_image_ref(source_ref) is None
    assert manager.resolve_image_ref(fork_ref) is not None


def test_fork_chat_marks_truncated_earlier_history(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Long")
    messages: list[dict] = []
    for index in range(41):
        messages.extend(
            [
                {"role": "user", "content": f"Question {index}"},
                {"role": "assistant", "content": f"Answer {index}"},
            ]
        )

    fork = manager.fork_chat(source.chat_id, messages=messages, turn_index=40)

    assert fork.handover_messages[0] == {
        "role": "system",
        "content": "Earlier conversation history was omitted when this fork was created.",
    }
    assert fork.handover_messages[-1]["content"] == "Answer 40"
    assert fork.user_turn_count == 40


def test_fork_chat_rejects_an_oversized_selected_turn(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Too large")

    with pytest.raises(ValueError, match="selected turn is too large"):
        manager.fork_chat(
            source.chat_id,
            messages=[
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": "x" * 60_001},
            ],
            turn_index=0,
        )


def test_fork_chat_rolls_back_when_final_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Original")
    before = set(manager._chats)

    def fail_save(*args, **kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_save", fail_save)

    with pytest.raises(OSError, match="disk full"):
        manager.fork_chat(source.chat_id, messages=_messages(), turn_index=0)

    assert set(manager._chats) == before
    persisted = json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )
    assert set(persisted["chats"]) == before
