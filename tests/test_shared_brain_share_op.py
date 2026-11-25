"""Managed, chat-scoped entry point for shared-brain note drafts."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import control_plane as cp
from ciao import share_proposals as sp
from ciao.web.chat_broker import ChatStream


class _Chat:
    chat_id = "chat-1"
    project_id = "project-1"
    provider = "claude"
    user_turn_count = 5
    user_turn_unattended: dict[str, bool] = {}
    user_turn_ids = {"4": "turn-4"}
    user_turn_origin: dict[str, str] = {}
    user_turn_timings = {"4": {"sent_at": "2026-11-24T00:00:00Z"}}
    helper: dict[str, str] = {}


class _Plane(cp.CiaoControlPlane):
    def __init__(
        self, vault: Path, *, chat: _Chat | None = None, brains: list[object] | None = None,
        active: bool = True, running_turn_index: int | None = 4, background: bool = False,
    ) -> None:
        self.config = SimpleNamespace(
            state_path=vault.parent / ".runtime" / "state.json",
            workspace=lambda name: SimpleNamespace(shared_brains=brains or []) if name == "personal" else None,
            workspace_vault_root=lambda _name: vault,
        )
        self.chat = chat or _Chat()
        self.project = SimpleNamespace(workspace="personal", project_id="project-1")
        self.stream = ChatStream(background=background)
        self.stream.begin_publish_turn(running_turn_index)
        self.active = active
        self.pcm = SimpleNamespace(
            get_chat=lambda _chat_id: self.chat,
            get_project=lambda _project_id: self.project,
            get_active_stream=lambda _chat_id: self.stream if self.active else None,
            _streaming=SimpleNamespace(drive_task=lambda _chat_id, _stream: self.driver),
        )


def _principal() -> cp.AgentPrincipal:
    return cp.AgentPrincipal("token", "chat-1", "project-1", "personal", "claude")


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "workspace" / "vault"
    root.mkdir(parents=True)
    return root


def test_share_reads_exact_bytes_from_workspace_vault_and_reuses_turn_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    body = b"# Team note\n\n$() `quoted`\n"
    (vault / "draft.md").write_bytes(body)
    calls: list[dict[str, object]] = []

    def draft_share(_config, **kwargs):
        calls.append(kwargs)
        return sp.ShareDraft("proposal-1", {}, "/api/proposals/review/opaque")

    monkeypatch.setattr(sp, "draft_share", draft_share)
    plane = _Plane(vault)
    first = asyncio.run(plane.note_share(_principal(), path="Notes/Idea.md", body_file="draft.md", brain="team"))
    second = asyncio.run(plane.note_share(_principal(), path="Notes/Idea.md", body_file="draft.md", brain="team"))
    (vault / "draft.md").write_bytes(body + b"new body\n")
    third = asyncio.run(plane.note_share(_principal(), path="Notes/Idea.md", body_file="draft.md", brain="team"))
    fourth = asyncio.run(plane.note_share(_principal(), path="Notes/Other.md", body_file="draft.md", brain="team"))

    assert first["data"] == {"status": "queued", "proposal_id": "proposal-1", "review_path": "/api/proposals/review/opaque"}
    assert second["data"]["status"] == "queued"
    assert [call["proposed"] for call in calls] == [body, body, body + b"new body\n", body + b"new body\n"]
    assert all(call["operation"] is None for call in calls)
    assert calls[0]["request_key"] == calls[1]["request_key"]
    assert calls[0]["request_key"] != calls[2]["request_key"]
    assert calls[2]["request_key"] != calls[3]["request_key"]
    assert calls[0]["workspace"] == "personal"


@pytest.mark.parametrize("origin", ["schedule", "delegate", "memory_pass", "care"])
def test_share_refuses_non_person_turn_before_reading_or_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    vault = _vault(tmp_path)
    chat = _Chat()
    chat.user_turn_origin = {"4": origin}
    plane = _Plane(vault, chat=chat)
    called = False

    def draft_share(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must refuse before filing")

    monkeypatch.setattr(sp, "draft_share", draft_share)
    with pytest.raises(cp.ControlPlaneError, match="user typed") as error:
        asyncio.run(plane.note_share(_principal(), path="Notes/Idea.md", body_file="missing.md"))
    assert error.value.code == "unattended_forbidden"
    assert not called


def test_share_refuses_turn_without_persisted_timing_before_reading_or_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    chat = _Chat()
    chat.user_turn_timings = {}
    called = False

    def draft_share(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must refuse before filing")

    monkeypatch.setattr(sp, "draft_share", draft_share)
    with pytest.raises(cp.ControlPlaneError) as error:
        asyncio.run(_Plane(vault, chat=chat).note_share(_principal(), path="Notes/Idea.md", body_file="missing.md"))
    assert error.value.code == "note_share_inactive"
    assert not called


def test_reused_chat_bearer_can_draft_and_request_identity_survives_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    (vault / "draft.md").write_bytes(b"same body")
    calls: list[str] = []

    def draft_share(_config, *, request_key, **_kwargs):
        calls.append(request_key)
        return sp.ShareDraft("proposal", {}, "/api/proposals/review/opaque")

    monkeypatch.setattr(sp, "draft_share", draft_share)
    plane = _Plane(vault)
    plane.chat.user_turn_count = 1
    plane.chat.user_turn_ids = {"0": "first-session-turn"}
    plane.chat.user_turn_timings = {"0": {"sent_at": "now"}}
    plane.stream.begin_publish_turn(0)
    first = _principal()
    result = asyncio.run(
        plane.note_share(first, path="Notes/Idea.md", body_file="draft.md")
    )
    assert result["data"]["status"] == "queued"

    # A retained shell/subagent has the same chat bearer during this later
    # active turn, so it can queue a draft. Individual human approval remains
    # the publication gate. A reset can reuse index zero, but the durable turn
    # identity keeps this proposal's request key distinct.
    plane.stream.begin_publish_turn(0)
    plane.chat.user_turn_ids = {"0": "second-session-turn"}
    result = asyncio.run(
        plane.note_share(first, path="Notes/Idea.md", body_file="draft.md")
    )
    assert result["data"]["status"] == "queued"

    assert len(calls) == 2
    assert calls[0] != calls[1]


@pytest.mark.parametrize(
    ("active", "running_turn_index", "background"),
    [(False, 4, False), (True, 3, False), (True, 4, True)],
)
def test_share_requires_the_active_foreground_turn_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active: bool,
    running_turn_index: int,
    background: bool,
) -> None:
    vault = _vault(tmp_path)
    plane = _Plane(vault, active=active, running_turn_index=running_turn_index, background=background)
    called = False

    def draft_share(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("inactive turn must not file")

    monkeypatch.setattr(sp, "draft_share", draft_share)
    with pytest.raises(cp.ControlPlaneError, match="active foreground") as error:
        asyncio.run(plane.note_share(_principal(), path="Notes/Idea.md", body_file="missing.md"))
    assert error.value.code == "note_share_inactive"
    assert not called


def test_share_rechecks_foreground_turn_when_off_loop_work_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    plane = _Plane(vault)
    called = False

    async def stale_before_worker(_key, operation, *, coalesce=True):
        plane.stream.begin_publish_turn(3)
        return operation()

    def draft_share(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("a stale turn must not file")

    monkeypatch.setattr(cp, "run_read", stale_before_worker)
    monkeypatch.setattr(sp, "draft_share", draft_share)
    with pytest.raises(cp.ControlPlaneError, match="active foreground") as error:
        asyncio.run(plane.note_share(_principal(), path="Notes/Idea.md", body_file="missing.md"))
    assert error.value.code == "note_share_inactive"
    assert not called


@pytest.mark.parametrize("change", ["count", "origin", "complete", "next_turn"])
def test_share_queue_seam_rechecks_turn_count_and_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    vault = _vault(tmp_path)
    plane = _Plane(vault)

    def stop_or_replace_turn(_config, *, publish_guard, **_kwargs):
        if change == "count":
            plane.chat.user_turn_count += 1
        elif change == "origin":
            plane.chat.user_turn_origin["4"] = "schedule"
        elif change == "complete":
            plane.stream.end_publish_turn(plane.stream.publish_generation)
        else:
            plane.stream.begin_publish_turn(5)
        try:
            with publish_guard():
                raise AssertionError("the filer must refuse before writing")
        except sp.ShareDraftError as exc:
            return exc.refusal

    monkeypatch.setattr(sp, "draft_share", stop_or_replace_turn)
    (vault / "draft.md").write_bytes(b"draft")
    with pytest.raises(cp.ControlPlaneError):
        asyncio.run(plane.note_share(_principal(), path="Notes/Idea.md", body_file="draft.md"))


def test_late_turn_completion_cannot_invalidate_a_new_publish_generation() -> None:
    stream = ChatStream()
    first = stream.begin_publish_turn(4)
    stream.end_publish_turn(first)
    second = stream.begin_publish_turn(5)

    stream.end_publish_turn(first)

    assert stream.publish_active
    assert stream.publish_generation == second


def test_stop_and_finish_invalidate_the_active_publish_generation() -> None:
    stream = ChatStream(background=False)
    generation = stream.begin_publish_turn(4)

    stream.stop_publish_turn()
    assert not stream.publish_active
    assert stream.running_turn_index is None
    assert stream.publish_generation != generation

    stream.finish()
    assert stream.done
    assert not stream.publish_active
    assert stream.publish_turn_index is None


def test_turn_task_invalidates_before_delayed_driver_resumes() -> None:
    async def run() -> None:
        stream = ChatStream(background=False)
        generation = stream.begin_publish_turn(4)
        release_turn = asyncio.Event()
        release_driver = asyncio.Event()
        driver_resumed = asyncio.Event()

        async def turn() -> None:
            await release_turn.wait()

        task = stream.create_turn_task(
            turn, generation=generation, name="test-chat-turn"
        )

        async def delayed_driver() -> None:
            await task
            await release_driver.wait()
            driver_resumed.set()

        driver = asyncio.create_task(delayed_driver())
        release_turn.set()
        await task
        assert task.done()
        assert not stream.publish_active
        assert stream.running_turn_index is None
        assert not driver_resumed.is_set()
        release_driver.set()
        await driver
        assert driver_resumed.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("relative", ["../outside.md", "link/body.md"])
def test_share_body_reader_refuses_traversal_and_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    vault = _vault(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"must not be read")
    (vault / "link").symlink_to(outside)
    called = False

    def draft_share(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid body file reached filer")

    monkeypatch.setattr(sp, "draft_share", draft_share)
    with pytest.raises(cp.ControlPlaneError):
        asyncio.run(_Plane(vault).note_share(_principal(), path="Notes/Idea.md", body_file=relative))
    assert not called


def test_share_body_reader_requires_regular_file_and_caps_at_65536_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    (vault / "folder").mkdir()
    (vault / "large.md").write_bytes(b"x" * 65_537)
    called = False

    def draft_share(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid body file reached filer")

    monkeypatch.setattr(sp, "draft_share", draft_share)
    for name in ("folder", "large.md"):
        with pytest.raises(cp.ControlPlaneError):
            asyncio.run(_Plane(vault).note_share(_principal(), path="Notes/Idea.md", body_file=name))
    assert not called
