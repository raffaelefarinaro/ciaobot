"""``ciao memory learn``: the direct write of one reusable lesson.

Every guard is asserted where it acts: at the seam with ``append_learning``
(what text reaches it), and at the file (what the queue actually holds).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import learning_records as lr
from ciao import learnings_writer as mp
from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal


class _Config:
    def __init__(self, root: Path) -> None:
        self.workspace_root = root
        self.memory_char_limit = 3000
        self.user_char_limit = 1375
        self._root = root
        self.vault_root = root

    def workspace(self, _name: str) -> object:
        return object()

    def agent_root(self, _name: str) -> Path:
        return self._root

    def workspace_vault_root(self, _name: str) -> Path:
        return self._root


def _plane(root: Path, chat: object | None) -> CiaoControlPlane:
    return CiaoControlPlane(
        _Config(root),
        project_chat_manager=SimpleNamespace(get_chat=lambda _chat_id: chat),
        schedule_manager=SimpleNamespace(),
    )  # type: ignore[arg-type]


def _principal() -> McpPrincipal:
    return McpPrincipal(
        token_id="t", chat_id="chat-1", project_id="p", workspace="personal", provider="claude"
    )


def _attended_chat() -> SimpleNamespace:
    return SimpleNamespace(user_turn_count=1, user_turn_unattended={}, user_turn_origin={}, helper={})


def _learnings(root: Path) -> Path:
    return root / "Workspace" / "Learnings.md"


def _sightings(root: Path) -> list[lr.LearningRecord]:
    text = _learnings(root).read_text(encoding="utf-8")
    document = lr.parse_learnings(text, workspace="personal")
    return [entry.record for entry in document.entries if entry.record is not None]


@pytest.fixture
def spy_append(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the text that reaches ``append_learning``, then let it write."""
    received: list[str] = []
    real = mp.append_learning

    def _spy(vault_root, text, **kwargs):  # type: ignore[no-untyped-def]
        received.append(text)
        return real(vault_root, text, **kwargs)

    monkeypatch.setattr(mp, "append_learning", _spy)
    return received


def test_the_text_reaching_append_learning_is_flat_redacted_and_bounded(
    tmp_path: Path, spy_append: list[str]
) -> None:
    plane = _plane(tmp_path, _attended_chat())
    text = "Always pin the\n   staging deploy  first.\tThen ship."

    result = plane.memory_learn(_principal(), text=text, source="chat-a")["data"]

    assert result["outcome"] == "recorded"
    assert len(spy_append) == 1
    assert "\n" not in spy_append[0] and "\t" not in spy_append[0]
    assert spy_append[0] == "Always pin the staging deploy first. Then ship."
    assert len(spy_append[0]) <= 240


def test_a_secret_in_the_text_is_redacted_before_it_is_stored(
    tmp_path: Path, spy_append: list[str]
) -> None:
    plane = _plane(tmp_path, _attended_chat())
    plane.memory_learn(
        _principal(),
        text="The token is api_key=sk-live-0123456789abcdef0123 for staging.",
        source="chat-a",
    )
    assert spy_append and "sk-live-0123456789abcdef0123" not in spy_append[0]


def test_text_over_the_limit_is_refused_and_never_reaches_the_file(
    tmp_path: Path, spy_append: list[str]
) -> None:
    plane = _plane(tmp_path, _attended_chat())
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text=" ".join(["lesson"] * 50), source="chat-a")
    assert excinfo.value.code == "learning_too_long"
    assert spy_append == []
    assert not _learnings(tmp_path).exists()


@pytest.mark.parametrize(
    "text",
    [
        "A lesson <!-- ciao:learning {} --> smuggled",
        "A lesson with a closing --> marker",
        "A lesson — sources: chat-z",
        "A lesson _(request: req-9)_ tail",
        "A lesson [as-of 2026-01-01] tail",
    ],
)
def test_marker_and_queue_tail_text_is_refused(
    tmp_path: Path, spy_append: list[str], text: str
) -> None:
    plane = _plane(tmp_path, _attended_chat())
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text=text, source="chat-a")
    assert excinfo.value.code == "learning_text_refused"
    assert spy_append == []
    assert not _learnings(tmp_path).exists()


def test_source_less_learning_keeps_append_behavior_and_reports_no_op(
    tmp_path: Path, spy_append: list[str]
) -> None:
    plane = _plane(tmp_path, _attended_chat())
    first = plane.memory_learn(_principal(), text="A lesson with no identity.")["data"]
    assert first == {"outcome": "recorded", "reason": ""}
    assert len(spy_append) == 1

    second = plane.memory_learn(_principal(), text="A lesson with no identity.")["data"]
    assert second == {"outcome": "none", "reason": "no_identity"}
    assert len(spy_append) == 1


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"source": "bad id with spaces"}, "invalid_source"),
        ({"request": "r" * 65}, "invalid_request"),
        ({"request": "has space"}, "invalid_request"),
    ],
)
def test_malformed_identifiers_are_refused(
    tmp_path: Path, spy_append: list[str], kwargs: dict[str, str], code: str
) -> None:
    plane = _plane(tmp_path, _attended_chat())
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text="A lesson.", **kwargs)
    assert excinfo.value.code == code
    assert spy_append == []


def test_an_oversized_source_is_refused_before_append_learning(
    tmp_path: Path, spy_append: list[str]
) -> None:
    plane = _plane(tmp_path, _attended_chat())
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(
            _principal(), text="A lesson.", source="s" * 129
        )
    assert excinfo.value.code == "invalid_source"
    assert spy_append == []
    assert not _learnings(tmp_path).exists()


@pytest.mark.parametrize("symlink_workspace", [True, False])
def test_learning_write_refuses_symlinked_destination(
    tmp_path: Path, spy_append: list[str], symlink_workspace: bool
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "Learnings.md"
    target.write_text("outside content\n", encoding="utf-8")
    workspace = tmp_path / "Workspace"
    if symlink_workspace:
        workspace.symlink_to(outside, target_is_directory=True)
    else:
        workspace.mkdir()
        (workspace / "Learnings.md").symlink_to(target)

    plane = _plane(tmp_path, _attended_chat())
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text="A lesson.", source="chat-a")
    assert excinfo.value.code == "learning_path_forbidden"
    assert target.read_text(encoding="utf-8") == "outside content\n"
    assert spy_append == []


def test_learning_write_does_not_follow_the_legacy_fixed_temp_symlink(
    tmp_path: Path, spy_append: list[str]
) -> None:
    workspace = tmp_path / "Workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content\n", encoding="utf-8")
    legacy_temp = workspace / ".Learnings.md.write.tmp"
    legacy_temp.symlink_to(outside)

    plane = _plane(tmp_path, _attended_chat())
    result = plane.memory_learn(
        _principal(), text="A secure lesson.", source="chat-a"
    )["data"]

    assert result["outcome"] == "recorded"
    assert spy_append == ["A secure lesson."]
    assert legacy_temp.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside content\n"
    assert "A secure lesson." in _learnings(tmp_path).read_text(encoding="utf-8")


def test_workspace_swap_aborts_learning_and_releases_descriptor_and_lock(
    tmp_path: Path, spy_append: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from ciao import learnings_writer as memory_proposals

    workspace = tmp_path / "Workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    replacement_learning = outside / "Learnings.md"
    replacement_learning.write_text("replacement content\n", encoding="utf-8")
    moved_workspace = tmp_path / "Workspace.opened"
    real_open_workspace = memory_proposals._learning_workspace_fd
    swapped = False

    @contextmanager
    def swap_after_open(vault_root: Path, *, create: bool):
        nonlocal swapped
        with real_open_workspace(vault_root, create=create) as workspace_fd:
            if create and not swapped:
                workspace.rename(moved_workspace)
                outside.rename(workspace)
                swapped = True
            yield workspace_fd

    monkeypatch.setattr(memory_proposals, "_learning_workspace_fd", swap_after_open)
    plane = _plane(tmp_path, _attended_chat())
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text="A race-safe lesson.", source="chat-a")

    assert excinfo.value.code == "learning_path_changed"
    assert excinfo.value.retryable is True
    assert swapped
    assert (workspace / "Learnings.md").read_text(encoding="utf-8") == "replacement content\n"
    assert not (moved_workspace / "Learnings.md").exists()
    assert list(moved_workspace.glob("*.write.tmp")) == []
    assert spy_append == ["A race-safe lesson."]

    workspace.rename(outside)
    moved_workspace.rename(workspace)
    result = plane.memory_learn(
        _principal(), text="A retry after the path is restored.", source="chat-b"
    )["data"]
    assert result["outcome"] == "recorded"


def test_learning_writer_preserves_existing_file_mode(
    tmp_path: Path, spy_append: list[str]
) -> None:
    workspace = tmp_path / "Workspace"
    workspace.mkdir()
    learnings = workspace / "Learnings.md"
    learnings.write_text("# Learnings\n\n## Active\n", encoding="utf-8")
    learnings.chmod(0o640)

    plane = _plane(tmp_path, _attended_chat())
    plane.memory_learn(_principal(), text="A private lesson.", source="chat-a")

    assert learnings.stat().st_mode & 0o777 == 0o640
    assert spy_append == ["A private lesson."]


def test_learning_temp_is_complete_before_atomic_rename_and_failure_preserves_file(
    tmp_path: Path,
    spy_append: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ciao import learnings_writer as memory_proposals

    workspace = tmp_path / "Workspace"
    workspace.mkdir()
    learnings = workspace / "Learnings.md"
    before = b"# Learnings\n\n## Active\n- Existing note.\n"
    learnings.write_bytes(before)
    expected, operation = memory_proposals.render_learning_append(
        before.decode("utf-8"),
        "A complete staged lesson.",
        workspace="personal",
        source="chat-a",
    )
    assert operation == "add"
    inspected: list[bytes] = []

    def inspect_then_fail(
        src: str,
        _dst: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        fd = os.open(
            src,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=src_dir_fd,
        )
        with os.fdopen(fd, "rb") as staged:
            inspected.append(staged.read())
        raise OSError("injected rename failure")

    monkeypatch.setattr(memory_proposals, "_rename_at", inspect_then_fail)
    plane = _plane(tmp_path, _attended_chat())
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(
            _principal(), text="A complete staged lesson.", source="chat-a"
        )

    assert excinfo.value.code == "learning_write_failed"
    assert inspected == [expected.encode("utf-8")]
    assert excinfo.value.retryable is True
    assert learnings.read_bytes() == before
    assert list(workspace.glob("*.write.tmp")) == []
    assert spy_append == ["A complete staged lesson."]


def test_secure_learning_reader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    from ciao import learnings_writer as memory_proposals

    workspace = tmp_path / "Workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "Learnings.md")

    with memory_proposals._learning_workspace_fd(tmp_path, create=False) as workspace_handle:
        assert workspace_handle is not None
        with pytest.raises(memory_proposals.LearningPathError):
            memory_proposals.read_learning_file(
                tmp_path, secure_destination=True, workspace_handle=workspace_handle
            )


def test_the_sixty_fifth_distinct_sighting_is_refused_and_a_replay_stays_none(
    tmp_path: Path, spy_append: list[str]
) -> None:
    plane = _plane(tmp_path, _attended_chat())
    statement = "Prefer the staging bucket for migration rehearsals."
    for index in range(1, 65):
        plane.memory_learn(_principal(), text=statement, source=f"chat-{index:03d}")
    assert len(_sightings(tmp_path)[0].observations) == 64
    before = _learnings(tmp_path).read_bytes()
    calls_before = len(spy_append)

    full = plane.memory_learn(_principal(), text=statement, source="chat-065")["data"]
    assert full["outcome"] == "none"
    assert full["reason"] == "evidence_full"
    assert len(spy_append) == calls_before
    assert _learnings(tmp_path).read_bytes() == before

    replay = plane.memory_learn(_principal(), text=statement, source="chat-001")["data"]
    assert replay["outcome"] == "none"
    assert replay["reason"] == "already_recorded"
    assert _learnings(tmp_path).read_bytes() == before


def test_a_held_queue_lock_is_a_retryable_error_with_no_write(
    tmp_path: Path, spy_append: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import file_kernel as fk

    def _held(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise fk.QueueLockError("held")

    monkeypatch.setattr(fk, "queue_lock", _held)
    plane = _plane(tmp_path, _attended_chat())
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text="A lesson.", source="chat-a")
    assert excinfo.value.code == "learning_busy"
    assert excinfo.value.retryable is True
    assert spy_append == []
    assert not _learnings(tmp_path).exists()


def test_an_unattended_turn_is_refused(tmp_path: Path, spy_append: list[str]) -> None:
    chat = SimpleNamespace(
        user_turn_count=1, user_turn_unattended={"0": True}, user_turn_origin={}, helper={}
    )
    plane = _plane(tmp_path, chat)
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text="A lesson.", source="chat-a")
    assert excinfo.value.code == "unattended_forbidden"
    assert spy_append == []
    assert not _learnings(tmp_path).exists()


def test_a_memory_pass_helper_chat_may_record_a_learning(
    tmp_path: Path, spy_append: list[str]
) -> None:
    # The memory pass starts its turn with `start_stream(...)` and no
    # `unattended` argument (ciao/web/memory_pass.py), so the chat carries no
    # `user_turn_unattended` key for that turn and the gate lets it through.
    chat = SimpleNamespace(
        user_turn_count=1,
        user_turn_unattended={},
        user_turn_origin={},
        helper={
            "kind": "memory_pass",
            "source_chat_id": "archived-chat",
            "archive_path": "Workspace/Archives/archived-chat.md",
            "archive_policy": "when_clean",
        },
    )
    plane = _plane(tmp_path, chat)
    result = plane.memory_learn(_principal(), text="A lesson from the pass.", source="chat-a")["data"]
    assert result["outcome"] == "recorded"
    assert [r.text for r in _sightings(tmp_path)] == ["A lesson from the pass."]


@pytest.mark.parametrize("source_kind", ["task_completion", "project_closure"])
def test_attended_non_archive_memory_pass_variants_may_record_learnings(
    tmp_path: Path, spy_append: list[str], source_kind: str
) -> None:
    from ciao.web import chat_service

    if source_kind == chat_service.TASK_COMPLETION_SOURCE:
        resolution = "Use the staging gate before production."
        helper = {
            "kind": "memory_pass",
            "source_kind": source_kind,
            "task_path": "Workspace/Tasks/release.md",
            "completion_id": "a" * 32,
            "completed_at": "2026-10-08T10:00:00+00:00",
            "resolution": resolution,
            "resolution_sha256": hashlib.sha256(resolution.encode()).hexdigest(),
            "relayed_by_agent": False,
            "state": "running",
            "archive_policy": "when_clean",
        }
        source = "a" * 32
    else:
        note = "The project shipped after the migration rehearsal."
        helper = {
            "kind": "memory_pass",
            "source_kind": source_kind,
            "vault_folder": "release-project",
            "project_name": "Release project",
            "outcome": "completed",
            "note": note,
            "note_sha256": hashlib.sha256(note.encode()).hexdigest(),
            "recorded_at": "2026-10-08T10:00:00+00:00",
            "state": "running",
            "archive_policy": "when_clean",
        }
        source = ""

    assert chat_service._normalize_chat_helper(helper)["source_kind"] == source_kind
    chat = SimpleNamespace(
        user_turn_count=1,
        user_turn_unattended={},
        user_turn_origin={},
        helper=helper,
    )
    plane = _plane(tmp_path, chat)
    result = plane.memory_learn(
        _principal(), text=f"A learning from {source_kind}.", source=source
    )["data"]
    assert result["outcome"] == "recorded"
    assert spy_append == [f"A learning from {source_kind}."]


@pytest.mark.parametrize("source_kind", ["task_completion", "project_closure"])
def test_unattended_non_archive_memory_pass_variants_are_refused(
    tmp_path: Path, spy_append: list[str], source_kind: str
) -> None:
    from ciao.web import chat_service

    if source_kind == chat_service.TASK_COMPLETION_SOURCE:
        resolution = "Use the staging gate before production."
        helper = {
            "kind": "memory_pass",
            "source_kind": source_kind,
            "task_path": "Workspace/Tasks/release.md",
            "completion_id": "a" * 32,
            "resolution": resolution,
            "resolution_sha256": hashlib.sha256(resolution.encode()).hexdigest(),
            "state": "running",
            "archive_policy": "when_clean",
        }
        source = "a" * 32
    else:
        note = "The project shipped after the migration rehearsal."
        helper = {
            "kind": "memory_pass",
            "source_kind": source_kind,
            "vault_folder": "release-project",
            "outcome": "completed",
            "note": note,
            "note_sha256": hashlib.sha256(note.encode()).hexdigest(),
            "state": "running",
            "archive_policy": "when_clean",
        }
        source = ""
    chat = SimpleNamespace(
        user_turn_count=1,
        user_turn_unattended={"0": True},
        user_turn_origin={},
        helper=helper,
    )
    plane = _plane(tmp_path, chat)
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text="An unattended pass lesson.", source=source)
    assert excinfo.value.code == "unattended_forbidden"
    assert spy_append == []


def test_a_malformed_memory_pass_helper_is_not_trusted(
    tmp_path: Path, spy_append: list[str]
) -> None:
    chat = SimpleNamespace(
        user_turn_count=1,
        user_turn_unattended={},
        user_turn_origin={},
        helper={"kind": "memory_pass"},
    )
    plane = _plane(tmp_path, chat)
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text="An untrusted helper lesson.", source="chat-a")
    assert excinfo.value.code == "unattended_forbidden"
    assert spy_append == []


def test_an_unavailable_chat_keeps_the_existing_attended_policy(
    tmp_path: Path, spy_append: list[str]
) -> None:
    plane = _plane(tmp_path, None)
    result = plane.memory_learn(
        _principal(), text="A lesson from an unavailable chat.", source="chat-a"
    )["data"]
    assert result["outcome"] == "recorded"
    assert len(spy_append) == 1


@pytest.mark.parametrize("origin", ["webhook", "task_delegation", "update_task", "schedule"])
def test_engine_origins_cannot_record_a_learning(
    tmp_path: Path, spy_append: list[str], origin: str
) -> None:
    chat = SimpleNamespace(
        user_turn_count=1,
        user_turn_unattended={},
        user_turn_origin={"0": origin},
        helper={},
    )
    plane = _plane(tmp_path, chat)
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text="An engine-origin lesson.", source="chat-a")
    assert excinfo.value.code == "unattended_forbidden"
    assert spy_append == []


@pytest.mark.parametrize("kind", ["update_task", "memory_pass"])
def test_engine_helper_chats_cannot_record_a_learning(
    tmp_path: Path, spy_append: list[str], kind: str
) -> None:
    chat = SimpleNamespace(
        user_turn_count=1,
        user_turn_unattended={},
        user_turn_origin={},
        helper={"kind": kind},
    )
    plane = _plane(tmp_path, chat)
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.memory_learn(_principal(), text="An engine-helper lesson.", source="chat-a")
    assert excinfo.value.code == "unattended_forbidden"
    assert spy_append == []


def test_the_cli_verb_resolves_to_the_control_plane_operation(tmp_path: Path) -> None:
    from ciao import agent_cli

    text_file = tmp_path / "lesson.txt"
    text_file.write_text("A lesson.\n", encoding="utf-8")
    parser = agent_cli.build_parser()
    op, payload = agent_cli.resolve(
        parser.parse_args(
            ["memory", "learn", "--text-file", str(text_file), "--source", "chat-a",
             "--request", "req-1"]
        )
    )
    assert op == "memory_learn"
    assert payload == {
        "text": "A lesson.",
        "source": "chat-a",
        "request": "req-1",
    }


def test_the_mcp_operation_table_carries_memory_learn_as_a_write() -> None:
    from ciao import mcp_server

    operations = {op.name: op for op in mcp_server.OPERATIONS}
    assert "memory_learn" in operations
    assert operations["memory_learn"].annotations == mcp_server._WRITE
