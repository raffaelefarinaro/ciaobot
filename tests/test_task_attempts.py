"""``ciao/task_attempts.py``: the durable attempt store (#1033, B5).

The store's whole job is that a recorded attempt means a turn really was handed
over, and that a second caller can never turn one delegation into two turns. So
what is asserted here is the small set of facts the delegation service leans on
and cannot re-derive:

- **One live attempt per task.** A second ``start`` returns the attempt the first
  one made and reports ``created: false``; a settled attempt is history, so a
  start after one is a genuinely new attempt.
- **Transitions are checked.** A settled attempt cannot be moved back to
  ``running`` (that is a new attempt, not a state change), and an unknown state is
  refused rather than defaulted.
- **A crash is derived, not remembered.** An attempt written by another process is
  ``interrupted`` on the next read, so a restart never replays a turn nobody
  confirmed.
- **A release frees the task without rewriting the turn.** Approving a reviewed
  result or detaching the card marks the attempt released, which takes it out of
  the live set — so the task can be delegated again — while its history row still
  reads ``ready_for_review``.
- **Stop/detach settle rather than erase**, and the store itself never completes a
  task: it holds no board at all, so completion stays the board's refusal.

Real clock and real files in a tmp runtime directory: no vault, no live install,
no models.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ciao.task_attempts import (
    LIVE_STATES,
    MAX_ATTEMPTS_PER_TASK,
    PreviousAttempt,
    RESUMABLE_STATES,
    SETTLED_STATES,
    TaskAttempt,
    TaskAttemptError,
    TaskAttemptStore,
    build_prompt,
    build_resume_prompt,
    task_delegation_helper,
)

TASK_ID = "a" * 32
OTHER_TASK_ID = "b" * 32
REVISION = "c" * 64
OTHER_REVISION = "d" * 64


class _Clock:
    """A clock the test advances by hand, so stamps order deterministically."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 1) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _store(tmp_path: Path, workspace: str = "personal", clock: _Clock | None = None) -> TaskAttemptStore:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return TaskAttemptStore(workspace=workspace, runtime_dir=runtime, clock=clock or _Clock())


def _start(store: TaskAttemptStore, **overrides: Any):
    fields: dict[str, Any] = {
        "task_id": TASK_ID,
        "task_revision": REVISION,
        "chat_id": "chat-1",
    }
    fields.update(overrides)
    return store.start(**fields)


# ── One live attempt per task ───────────────────────────────────────────


def test_a_start_records_the_task_revision_and_the_chat_it_runs_in(tmp_path: Path) -> None:
    store = _store(tmp_path)
    started = _start(store)
    assert started.created is True
    attempt = started.attempt
    assert len(attempt.attempt_id) == 32
    assert all(char in "0123456789abcdef" for char in attempt.attempt_id)
    assert attempt.task_id == TASK_ID
    assert attempt.task_revision == REVISION
    assert attempt.chat_id == "chat-1"
    assert attempt.state == "running"
    assert attempt.state in LIVE_STATES
    assert attempt.ended_at == ""
    assert store.get(attempt.attempt_id) == attempt


def test_a_second_start_returns_the_same_attempt_and_mints_nothing(tmp_path: Path) -> None:
    """The double click. Same attempt, same chat, and `created: false` so the
    caller knows not to start a turn."""
    store = _store(tmp_path)
    first = _start(store)
    second = _start(store, chat_id="chat-2", task_revision=OTHER_REVISION)

    assert second.created is False
    assert second.attempt.attempt_id == first.attempt.attempt_id
    # The second caller's chat never became an attempt's chat: it created nothing.
    assert second.attempt.chat_id == "chat-1"
    assert second.attempt.task_revision == REVISION
    assert len(store.list_for_task(TASK_ID)) == 1
    assert store.get_live(TASK_ID) == first.attempt


def test_a_caller_may_name_the_attempt_id_it_is_about_to_stamp(tmp_path: Path) -> None:
    """The chat is created before this store writes, so the id has to exist first.

    The provenance stamp on the chat names the attempt, and a stamp cannot be
    patched in afterwards: the real `update_chat` has no `helper` parameter. So the
    caller mints the id, hands it here, and stamps the same one on the chat. The
    store still mints one itself when given none, which is every other caller.
    """
    store = _store(tmp_path)
    chosen = "a" * 32

    started = _start(store, attempt_id=chosen)

    assert started.created is True
    assert started.attempt.attempt_id == chosen
    assert store.get(chosen) == started.attempt


def test_a_named_attempt_id_that_is_not_an_id_is_refused(tmp_path: Path) -> None:
    """Same rule the decoder applies to a stored row's key, for the same reason: an
    id this module could not match would name a record nothing could read back."""
    store = _store(tmp_path)

    for bad in ("not-an-id", "A" * 32, "a" * 31, "../../etc/passwd"):
        with pytest.raises(TaskAttemptError) as excinfo:
            _start(store, attempt_id=bad)
        assert excinfo.value.code == "invalid_attempt", bad

    assert store.list_for_task(TASK_ID) == ()


def test_a_start_that_loses_the_one_live_attempt_discards_its_own_id(
    tmp_path: Path,
) -> None:
    """The proposed id is minted nothing when the task already has a live attempt.

    The caller has by then created a chat stamped with it, and that chat is left
    empty rather than given a turn — so the id it proposed must not land in the
    document either, or the history would name an attempt that never ran.
    """
    store = _store(tmp_path)
    first = _start(store)

    second = _start(store, chat_id="chat-2", attempt_id="b" * 32)

    assert second.created is False
    assert second.attempt.attempt_id == first.attempt.attempt_id
    assert [row.attempt_id for row in store.list_for_task(TASK_ID)] == [
        first.attempt.attempt_id
    ]


def test_a_start_after_a_settled_attempt_is_a_new_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path, clock=_Clock())
    first = _start(store)
    store.finish(first.attempt.attempt_id, "stopped", detail="stopped by the user")
    assert store.get_live(TASK_ID) is None

    second = _start(store, chat_id="chat-2")
    assert second.created is True
    assert second.attempt.attempt_id != first.attempt.attempt_id
    assert second.attempt.chat_id == "chat-2"
    # The previous attempt is history, not overwritten.
    history = store.list_for_task(TASK_ID)
    assert [row.attempt_id for row in history] == [
        second.attempt.attempt_id,
        first.attempt.attempt_id,
    ]


def test_one_live_attempt_per_task_does_not_leak_across_tasks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mine = _start(store)
    theirs = _start(store, task_id=OTHER_TASK_ID, chat_id="chat-2")

    assert theirs.created is True
    assert theirs.attempt.attempt_id != mine.attempt.attempt_id
    assert store.get_live(TASK_ID) == mine.attempt
    assert store.get_live(OTHER_TASK_ID) == theirs.attempt
    assert set(store.live_by_task()) == {TASK_ID, OTHER_TASK_ID}


def test_a_malformed_id_or_revision_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for field, value in (
        ("task_id", "not-hex"),
        ("task_id", "../../etc/passwd"),
        ("task_revision", "short"),
        ("chat_id", ""),
    ):
        with pytest.raises(TaskAttemptError) as excinfo:
            _start(store, **{field: value})
        assert excinfo.value.code == "invalid_attempt", (field, value)
    assert store.live_by_task() == {}
    assert store.path.parent.exists()


def test_an_unknown_workspace_name_is_refused_at_construction(tmp_path: Path) -> None:
    for name in ("", "   "):
        with pytest.raises(ValueError):
            TaskAttemptStore(workspace=name, runtime_dir=tmp_path, clock=_Clock())


# ── Transitions ────────────────────────────────────────────────────────


def test_a_live_attempt_walks_the_states_a_turn_walks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt

    waiting = store.update_state(attempt.attempt_id, "needs_you")
    assert waiting.state == "needs_you"
    assert waiting.ended_at == ""

    back = store.update_state(attempt.attempt_id, "running")
    assert back.state == "running"

    done = store.finish(attempt.attempt_id, "ready_for_review")
    assert done.state == "ready_for_review"
    assert done.ended_at
    assert done.ended_at >= done.created_at
    assert store.get(attempt.attempt_id).state == "ready_for_review"


def test_a_failed_attempt_is_one_that_ran_and_ended_in_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    failed = store.finish(attempt.attempt_id, "failed", detail="the provider gave up")
    assert failed.state == "failed"
    assert failed.detail == "the provider gave up"


def test_a_settled_attempt_cannot_be_moved_back_to_running(tmp_path: Path) -> None:
    """A new attempt is a new `start`. Reviving a settled one would rewrite the
    record of what a turn did."""
    store = _store(tmp_path)
    attempt = _start(store).attempt
    store.finish(attempt.attempt_id, "interrupted", detail="crash window")
    with pytest.raises(TaskAttemptError) as excinfo:
        store.update_state(attempt.attempt_id, "running")
    assert excinfo.value.code == "invalid_attempt"
    assert store.get(attempt.attempt_id).state == "interrupted"


def test_an_unknown_state_is_refused_rather_than_defaulted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    for state in ("running-ish", "", "DONE", None):
        with pytest.raises(TaskAttemptError) as excinfo:
            store.update_state(attempt.attempt_id, state)  # type: ignore[arg-type]
        assert excinfo.value.code == "invalid_attempt", state
    assert store.get(attempt.attempt_id).state == "running"


def test_the_state_vocabulary_is_exactly_the_one_the_board_draws() -> None:
    """`LIVE_STATES` and `SETTLED_STATES` are what the service branches on, so a
    state added to one and not the other would be a live attempt with no branch
    anywhere."""
    assert LIVE_STATES | SETTLED_STATES == {
        "running",
        "needs_you",
        "failed",
        "interrupted",
        "ready_for_review",
        "stopped",
    }
    assert not LIVE_STATES & SETTLED_STATES
    # `ready_for_review` is a *finished turn awaiting review*, so it is live (the
    # task stays linked and uncompletable) but not resumable.
    assert "ready_for_review" in LIVE_STATES
    assert "ready_for_review" not in RESUMABLE_STATES
    assert RESUMABLE_STATES == {"failed", "interrupted", "stopped"}


def test_an_unknown_attempt_id_is_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for attempt_id in ("f" * 32, "", "short"):
        with pytest.raises(TaskAttemptError) as excinfo:
            store.get(attempt_id)
        assert excinfo.value.code == "not_found", attempt_id
        with pytest.raises(TaskAttemptError) as excinfo:
            store.update_state(attempt_id, "running")
        assert excinfo.value.code == "not_found", attempt_id


def test_a_detail_note_is_bounded_and_whitespace_trimmed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    settled = store.finish(attempt.attempt_id, "failed", detail="  " + "x" * 900 + "  ")
    assert len(settled.detail) == 400
    assert settled.detail == "x" * 400


def test_an_idempotent_settlement_does_not_advance_the_stamp(tmp_path: Path) -> None:
    """Settling an already-settled attempt with the same state and note is not an
    edit; a store that rewrote it would make the file churn on every read path
    that settles defensively."""
    clock = _Clock()
    store = _store(tmp_path, clock=clock)
    attempt = _start(store).attempt
    first = store.finish(attempt.attempt_id, "stopped", detail="done")
    clock.advance(60)
    again = store.finish(attempt.attempt_id, "stopped", detail="done")
    assert again.updated_at == first.updated_at
    assert again.ended_at == first.ended_at


# ── A crash is derived ──────────────────────────────────────────────────


def test_a_restart_derives_a_running_attempt_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attempt was written by a process that is gone, so no turn can be
    running in this one. `interrupted` is the honest answer, and it is derived —
    no startup sweep, nothing remembered."""
    store = _store(tmp_path)
    attempt = _start(store).attempt
    assert store.get_live(TASK_ID).state == "running"

    # A second process, same files.
    monkeypatch.setattr("ciao.task_attempts._PROCESS_TOKEN", "99999-restarted")
    restarted = _store(tmp_path)

    # The row is now settled, so `get_live` says there is none: a new delegation
    # is a new attempt rather than a second turn for one this process cannot see.
    derived = restarted.list_for_task(TASK_ID)[0]
    assert derived.attempt_id == attempt.attempt_id
    assert derived.state == "interrupted"
    assert derived.state in SETTLED_STATES
    assert derived.chat_id == "chat-1"
    assert derived.ended_at
    assert "restarted" in derived.detail
    assert restarted.get_live(TASK_ID) is None


@pytest.mark.parametrize("ended", ["ready_for_review", "needs_you"])
def test_a_restart_keeps_an_attempt_whose_turn_had_already_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ended: str
) -> None:
    """Only `running` claims a turn in flight. A result waiting for review, or a
    turn that ended waiting on the user, lost nothing to the restart: it stays
    live, so the card stays In review and a reply still continues it."""
    store = _store(tmp_path)
    attempt = _start(store).attempt
    store.finish(attempt.attempt_id, ended)

    monkeypatch.setattr("ciao.task_attempts._PROCESS_TOKEN", "99999-restarted")
    restarted = _store(tmp_path)

    live = restarted.get_live(TASK_ID)
    assert live is not None and live.attempt_id == attempt.attempt_id
    assert live.state == ended
    assert restarted.recover_interrupted() == ()


def test_a_restart_never_replays_a_stranded_attempt(tmp_path: Path, monkeypatch) -> None:
    """Deriving `interrupted` is a read-time answer; nothing in the store starts a
    turn, so a stranded attempt is never dispatched again by being read."""
    store = _store(tmp_path)
    _start(store)
    monkeypatch.setattr("ciao.task_attempts._PROCESS_TOKEN", "99999-restarted")
    restarted = _store(tmp_path)
    assert restarted.recover_interrupted()
    # Idempotent: the derivation is already on disk, so a second sweep finds none.
    assert restarted.recover_interrupted() == ()
    assert restarted.list_for_task(TASK_ID)[0].state == "interrupted"
    assert restarted.get_live(TASK_ID) is None


def test_recover_interrupted_persists_what_it_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable half: a caller that wants the ambiguity settled on disk (a boot
    log, the board's first read) gets it written, not just derived in memory."""
    store = _store(tmp_path)
    _start(store)
    monkeypatch.setattr("ciao.task_attempts._PROCESS_TOKEN", "99999-restarted")
    restarted = _store(tmp_path)

    stranded = restarted.recover_interrupted()
    assert [row.state for row in stranded] == ["interrupted"]

    # Read the raw document back with the original process's token: the state on
    # disk is `interrupted`, not `running` waiting for another derivation.
    monkeypatch.setattr("ciao.task_attempts._PROCESS_TOKEN", "original")
    assert _store(tmp_path).list_for_task(TASK_ID)[0].state == "interrupted"


def test_recover_interrupted_is_a_no_op_with_nothing_stranded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(store)
    assert store.recover_interrupted() == ()
    assert store.get_live(TASK_ID).state == "running"


def test_the_document_is_a_private_atomic_json_file(tmp_path: Path) -> None:
    """The store follows `webhooks.py`'s discipline: owner-private, and written
    through a temp file so a reader never sees half a document."""
    store = _store(tmp_path)
    _start(store)
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["schema"] == 1
    assert list(document["attempts"]) == [store.get_live(TASK_ID).attempt_id]
    if os_name_is_posix():
        assert store.path.stat().st_mode & 0o077 == 0, "the attempt file is not owner-private"
    # No temp files left behind.
    assert [p.name for p in store.path.parent.iterdir() if p.name.startswith(".")] == []


def os_name_is_posix() -> bool:
    import os

    return os.name == "posix"


def test_a_corrupt_or_newer_document_is_raised_not_reset(tmp_path: Path) -> None:
    """A store that reset unreadable state would silently lose every attempt, and
    a board with no badges reads as a board with no delegations."""
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)

    for content, note in (
        ("not json at all", "unparseable"),
        ('{"schema": 99, "attempts": {}}', "a newer schema"),
        ('{"schema": 1}', "no attempts mapping"),
        ('{"schema": 1, "attempts": {"zz": {}}}', "a malformed row"),
    ):
        store.path.write_text(content, encoding="utf-8")
        with pytest.raises(TaskAttemptError) as excinfo:
            store.get_live(TASK_ID)
        assert excinfo.value.code == "read_failed", note
        assert store.path.read_text(encoding="utf-8") == content, note


def test_a_missing_document_reads_as_empty_and_creates_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_live(TASK_ID) is None
    assert store.list_for_task(TASK_ID) == ()
    assert store.live_by_task() == {}
    assert store.recover_interrupted() == ()
    assert not store.path.exists(), "a read created the store file"


def test_a_directory_where_the_document_should_be_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.mkdir(parents=True)
    with pytest.raises(TaskAttemptError) as excinfo:
        store.get_live(TASK_ID)
    assert excinfo.value.code == "read_failed"


# ── Stop and detach ─────────────────────────────────────────────────────


def test_stopping_settles_the_attempt_and_keeps_it_as_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    stopped = store.finish(attempt.attempt_id, "stopped", detail="stopped by the user")

    assert stopped.state == "stopped"
    assert stopped.state in RESUMABLE_STATES
    assert stopped.detail == "stopped by the user"
    # A stopped attempt releases the "one live attempt" hold, so the user can
    # hand the task over again; the row itself stays.
    assert store.get_live(TASK_ID) is None
    assert store.get(attempt.attempt_id).state == "stopped"
    assert len(store.list_for_task(TASK_ID)) == 1


def test_needs_you_settles_as_live_because_the_turn_is_waiting(tmp_path: Path) -> None:
    """A question or an approval card is an ordinary paused turn, so the attempt
    holds the task's linkage and no second delegation can start."""
    store = _store(tmp_path)
    attempt = _start(store).attempt
    waiting = store.finish(attempt.attempt_id, "needs_you")
    assert waiting.state == "needs_you"
    assert waiting.state in LIVE_STATES
    assert store.get_live(TASK_ID).attempt_id == attempt.attempt_id


# ── Release ─────────────────────────────────────────────────────────────


def test_a_release_takes_a_reviewed_attempt_out_of_the_live_set(tmp_path: Path) -> None:
    """The defect this exists for: `ready_for_review` is a live state, so releasing
    the card without rewriting it left the attempt holding a task nothing owns —
    and a task held by a live attempt can never be delegated again.

    The marker moves, the record does not: the history row still says the turn
    ended ready for review, which is the only record of how it ended.
    """
    store = _store(tmp_path)
    attempt = _start(store).attempt
    reviewed = store.finish(attempt.attempt_id, "ready_for_review")
    assert store.get_live(TASK_ID) is not None, "a reviewed result still holds the task"

    released = store.release(attempt.attempt_id)

    assert released.state == "ready_for_review", "the outcome the user reviewed"
    assert released.released is True
    assert released.is_live is False
    assert released.detail == "", "a release is not an outcome note"
    # Every read answers the same way, which is what the service branches on.
    assert store.get_live(TASK_ID) is None
    assert store.live_by_task() == {}
    assert store.get(attempt.attempt_id) == released
    # The badge read is history rather than the live one, so it still leads.
    assert store.newest_by_task()[TASK_ID] == released
    payload = released.to_dict()
    assert payload["released"] is True
    assert payload["live"] is False


def test_a_task_whose_attempt_was_released_can_be_delegated_again(tmp_path: Path) -> None:
    """The user takes the card back, so a second hand-over is a new attempt — not
    the old one handed back with `created: false` and no turn started."""
    clock = _Clock()
    store = _store(tmp_path, clock=clock)
    first = _start(store).attempt
    store.release(store.finish(first.attempt_id, "ready_for_review").attempt_id)
    clock.advance(60)

    second = _start(store, chat_id="chat-2")

    assert second.created is True
    assert second.attempt.attempt_id != first.attempt_id
    assert second.attempt.chat_id == "chat-2"
    # Both rows are kept: the release is history, not a deletion.
    assert [row.attempt_id for row in store.list_for_task(TASK_ID)] == [
        second.attempt.attempt_id,
        first.attempt_id,
    ]
    assert store.list_for_task(TASK_ID)[0].is_live, "the new attempt holds the task"


def test_a_release_is_refused_for_an_attempt_that_holds_nothing(tmp_path: Path) -> None:
    """A settled attempt was never holding its task, and a released one is already
    out of the live set. Refusing both keeps a caller that read a stale attempt from
    writing a marker over a record it did not read."""
    store = _store(tmp_path)
    settled = _start(store).attempt
    store.finish(settled.attempt_id, "stopped")

    with pytest.raises(TaskAttemptError) as excinfo:
        store.release(settled.attempt_id)
    assert excinfo.value.code == "invalid_attempt"
    assert "nothing to release" in str(excinfo.value)
    assert store.get(settled.attempt_id).released is False

    live = _start(store, task_id=OTHER_TASK_ID).attempt
    store.release(live.attempt_id)
    with pytest.raises(TaskAttemptError) as excinfo:
        store.release(live.attempt_id)
    assert excinfo.value.code == "invalid_attempt"
    assert "already released" in str(excinfo.value)
    assert store.get(live.attempt_id).released is True


def test_a_released_attempt_is_never_derived_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash derivation asks whether a turn can be running in this process. A
    released attempt already answered that: the user took the task back, so a
    restart must leave the reviewed result exactly as the user left it."""
    store = _store(tmp_path)
    attempt = _start(store).attempt
    store.release(store.finish(attempt.attempt_id, "ready_for_review").attempt_id)

    monkeypatch.setattr("ciao.task_attempts._PROCESS_TOKEN", "99999-restarted")
    restarted = _store(tmp_path)

    row = restarted.get(attempt.attempt_id)
    assert row.state == "ready_for_review"
    assert row.released is True
    assert restarted.get_live(TASK_ID) is None
    assert restarted.recover_interrupted() == ()


def test_the_history_bound_may_drop_a_released_attempt(tmp_path: Path) -> None:
    """The bound protects turns in flight, not history: a released attempt is not in
    flight, so it is the oldest of the settled rows and is dropped first."""
    clock = _Clock()
    store = _store(tmp_path, clock=clock)
    first = _start(store).attempt
    store.release(store.finish(first.attempt_id, "ready_for_review").attempt_id)
    for index in range(MAX_ATTEMPTS_PER_TASK + 10):
        attempt = _start(store, chat_id=f"chat-{index}").attempt
        store.finish(attempt.attempt_id, "failed", detail=f"attempt {index}")
        clock.advance(1)

    assert first.attempt_id not in {row.attempt_id for row in store.list_for_task(TASK_ID)}
    assert len(store.list_for_task(TASK_ID)) == MAX_ATTEMPTS_PER_TASK


def test_the_store_holds_no_board_and_never_completes_a_task(tmp_path: Path) -> None:
    """The store's surface has no completion and no column. That is not an
    oversight: completion belongs to `task_board`, which refuses an agent for it,
    and a store that could set a status would be a second way round that rule."""
    store = _store(tmp_path)
    assert not hasattr(store, "update")
    assert not hasattr(store, "complete")
    assert not hasattr(store, "delete")
    written = {name for name in dir(store) if not name.startswith("_")}
    assert written == {
        "path",
        "start",
        "get",
        "get_live",
        "live_by_task",
        "newest_by_task",
        "list_for_task",
        "recover_interrupted",
        "bind_revision",
        "continue_turn",
        "reopen",
        "update_state",
        "finish",
        "release",
        "report",
    }


# ── Bounded history ─────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["ready_for_review", "needs_you"])
def test_continuing_a_live_attempt_clears_its_old_ending(tmp_path: Path, state: str) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    store.finish(attempt.attempt_id, state, detail="Old ending")
    running = store.continue_turn(attempt.attempt_id)
    assert running.state == "running"
    assert running.ended_at == ""
    assert running.detail == ""
    assert running.task_revision == attempt.task_revision
    assert len(store.list_for_task(TASK_ID)) == 1
    assert store.finish(attempt.attempt_id, "failed").state == "failed"


@pytest.mark.parametrize("released", [False, True])
def test_a_continuation_cannot_revive_a_dead_attempt(tmp_path: Path, released: bool) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    if released:
        store.release(attempt.attempt_id)
    else:
        store.finish(attempt.attempt_id, "failed")
    with pytest.raises(TaskAttemptError):
        store.continue_turn(attempt.attempt_id)


def test_a_task_retried_forever_keeps_a_bounded_history(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock=clock)
    for index in range(MAX_ATTEMPTS_PER_TASK + 10):
        attempt = _start(store).attempt
        store.finish(attempt.attempt_id, "failed", detail=f"attempt {index}")
        clock.advance(1)

    history = store.list_for_task(TASK_ID)
    assert len(history) == MAX_ATTEMPTS_PER_TASK
    # `list_for_task` is newest first, and the bound drops the oldest, so the
    # survivors are the most recent MAX_ATTEMPTS_PER_TASK attempts.
    assert [row.detail for row in history] == [
        f"attempt {index}" for index in range(MAX_ATTEMPTS_PER_TASK + 9, 9, -1)
    ]


def test_the_history_bound_never_drops_a_live_attempt(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock=clock)
    live = _start(store).attempt
    for index in range(MAX_ATTEMPTS_PER_TASK + 10):
        # Each settled attempt is followed by a new live one, so the store always
        # holds a running attempt alongside a growing pile of settled rows.
        settled = _start(store, chat_id=f"chat-{index}").attempt
        store.finish(settled.attempt_id, "failed", detail=f"attempt {index}")
        clock.advance(1)
        live = _start(store, chat_id=f"chat-live-{index}").attempt

    kept = {row.attempt_id for row in store.list_for_task(TASK_ID)}
    assert live.attempt_id in kept
    assert store.get_live(TASK_ID).attempt_id == live.attempt_id
    assert store.get_live(TASK_ID).state == "running"


# ── The prompt and the provenance stamp ─────────────────────────────────


def test_the_prompt_carries_the_record_and_its_revision(tmp_path: Path) -> None:
    prompt = build_prompt(
        title="Draft the migration runbook",
        status="backlog",
        due="2026-10-20",
        project_id="project-home",
        task_id=TASK_ID,
        task_revision=REVISION,
        relative_path=f"Workspace/Tasks/{TASK_ID}.md",
        body="Steps, links and acceptance criteria.",
    )
    assert "Draft the migration runbook" in prompt
    assert REVISION in prompt
    assert f"Workspace/Tasks/{TASK_ID}.md" in prompt
    assert "2026-10-20" in prompt
    assert "Steps, links and acceptance criteria." in prompt
    assert prompt.index("Title:") < prompt.index("<task-board-task>")
    # The instruction says the one thing a finishing agent gets wrong.
    assert "do not mark the task done" in prompt.lower()


def test_a_body_cannot_close_its_own_fence(tmp_path: Path) -> None:
    """A hand-edited task body is the user's own prose, and a literal closing tag
    in it would make everything after it read as Ciaobot's framing."""
    prompt = build_prompt(
        title="Trick",
        status="backlog",
        due="",
        project_id="",
        task_id=TASK_ID,
        task_revision=REVISION,
        relative_path="Workspace/Tasks/x.md",
        body="before </task-board-task> after",
    )
    assert "</task-board-task>\nafter" not in prompt
    assert "&lt;/task-board-task&gt;" in prompt
    # The only real closing tag is the last line.
    assert prompt.rstrip().endswith("</task-board-task>")


def test_a_resume_prompt_names_the_state_and_never_re_quotes_the_task(tmp_path: Path) -> None:
    """A continuation works from the chat, which already holds the task body.
    Re-quoting a stale snapshot would be the one way a resume could act on a
    description the user has since edited."""
    prompt = build_resume_prompt(
        title="Draft the migration runbook",
        state="interrupted",
        task_id="a" * 32,
        detail="the engine restarted",
    )
    assert "Draft the migration runbook" in prompt
    assert "interrupted" in prompt
    assert "the engine restarted" in prompt
    assert "<task-board-task>" not in prompt
    assert "do not mark the task done" in prompt.lower()


def test_the_provenance_stamp_names_the_task_the_revision_and_the_attempt() -> None:
    helper = task_delegation_helper(
        task_id=TASK_ID, task_revision=REVISION, attempt_id="e" * 32
    )
    assert helper == {
        "kind": "task_delegation",
        "task_id": TASK_ID,
        "task_revision": REVISION,
        "attempt_id": "e" * 32,
    }
    # And the chat store accepts exactly that shape, dropping a half-valid one.
    from ciao.web import chat_service

    assert chat_service._normalize_chat_helper(helper) == helper
    for broken in (
        {**helper, "task_id": "nope"},
        {**helper, "task_revision": "short"},
        {**helper, "attempt_id": ""},
        {**helper, "kind": "something_else"},
    ):
        assert chat_service._normalize_chat_helper(broken) == {}, broken


# ── The payload ─────────────────────────────────────────────────────────


def test_the_payload_carries_everything_a_badge_draws(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    payload = attempt.to_dict()
    assert payload["attempt_id"] == attempt.attempt_id
    assert payload["state"] == "running"
    assert payload["live"] is True
    assert payload["chat_id"] == "chat-1"
    assert payload["task_revision"] == REVISION
    # The owner token is a process fact, not a user fact: it says which boot wrote
    # the row, and there is nothing a caller could do with it.
    assert "owner" not in payload

    settled = store.finish(attempt.attempt_id, "stopped").to_dict()
    assert settled["live"] is False


# ── The agent's own report (#1064) ──────────────────────────────────────


def test_a_live_attempt_takes_the_agents_report_and_keeps_it_across_reads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    reported = store.report(attempt.attempt_id, "blocked", "  Need the API key.  ")
    assert (reported.outcome, reported.summary) == ("blocked", "Need the API key.")
    assert reported.state == "running"  # a report never settles the turn
    reread = _store(tmp_path).get(attempt.attempt_id)
    assert (reread.outcome, reread.summary) == ("blocked", "Need the API key.")
    assert reread.to_dict()["outcome"] == "blocked"
    # The latest word wins.
    assert store.report(attempt.attempt_id, "done", "Shipped.").outcome == "done"


def test_a_report_needs_a_known_outcome_a_summary_and_a_live_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    with pytest.raises(TaskAttemptError, match="outcome must be one of"):
        store.report(attempt.attempt_id, "finished", "x")
    with pytest.raises(TaskAttemptError, match="needs a summary"):
        store.report(attempt.attempt_id, "done", "   ")
    store.finish(attempt.attempt_id, "failed", detail="boom")
    with pytest.raises(TaskAttemptError, match="no longer holds"):
        store.report(attempt.attempt_id, "done", "x")


def test_a_new_turn_owes_a_new_report_but_keeps_the_last_summary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    store.report(attempt.attempt_id, "needs_input", "Which region?")
    store.finish(attempt.attempt_id, "needs_you")
    continued = store.continue_turn(attempt.attempt_id)
    assert continued.outcome == ""
    assert continued.summary == "Which region?"
    # Every other transition carries both fields rather than dropping them.
    store.report(attempt.attempt_id, "done", "All set.")
    rebound = store.bind_revision(attempt.attempt_id, OTHER_REVISION)
    assert (rebound.outcome, rebound.summary) == ("done", "All set.")
    settled = store.finish(attempt.attempt_id, "ready_for_review")
    assert settled.outcome == "done"
    assert store.release(attempt.attempt_id).summary == "All set."


def test_a_row_written_before_reports_existed_reads_as_nothing_reported(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt = _start(store).attempt
    document = json.loads(store.path.read_text(encoding="utf-8"))
    for key in ("outcome", "summary"):
        document["attempts"][attempt.attempt_id].pop(key)
    store.path.write_text(json.dumps(document), encoding="utf-8")
    reread = store.get(attempt.attempt_id)
    assert (reread.outcome, reread.summary) == ("", "")


def test_the_prompt_asks_for_a_report_strips_the_log_and_hands_over_earlier_attempts() -> None:
    body = (
        "Do the thing.\n\n<!-- ciao:task-log -->\n## Delegation log\n\n"
        "- old line <!-- attempt:" + "e" * 32 + " -->\n<!-- /ciao:task-log -->\n"
    )
    earlier = PreviousAttempt(
        state="needs_you", outcome="blocked", summary="Got halfway; need the key.",
        detail="", created_at="2026-10-01T09:00:00+00:00", ended_at="2026-10-01T09:30:00+00:00",
        chat_id="chat-9", chat_title="First try", archive_path="memory-vault/x/chat.md",
    )
    prompt = build_prompt(
        title="T", status="backlog", due="", project_id="", task_id=TASK_ID,
        task_revision=REVISION, relative_path="Workspace/Tasks/t.md", body=body,
        previous_attempts=(earlier,),
    )
    assert f"ciao task report {TASK_ID} --outcome done|blocked|needs_input" in prompt
    assert "old line" not in prompt
    assert "Do the thing." in prompt
    assert "Earlier attempts" in prompt
    assert "Blocked" in prompt and "Got halfway; need the key." in prompt
    assert "memory-vault/x/chat.md" in prompt and "chat-9" in prompt
    # No history, no section.
    plain = build_prompt(
        title="T", status="backlog", due="", project_id="", task_id=TASK_ID,
        task_revision=REVISION, relative_path="x", body="b",
    )
    assert "Earlier attempts" not in plain
