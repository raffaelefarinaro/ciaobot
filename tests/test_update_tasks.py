"""Contract tests for durable update-task state and cheap applicability (#756).

Two things are proved here that the shipped data cannot prove, because both
ship empty: the state store round-trips and suppresses per revision, and the
applicability layer answers `applicable` / `not_applicable` / `unknown` for a
detector that exists, one that does not and one that raises. The end-to-end test
injects a packaged catalog (a temp root, monkeypatched over
`update_task_catalog.packaged_root`) so the layer is exercised exactly as an
installed wheel would run it while `ciao/stock/update-tasks/catalog.json` stays
`[]`.

Every test drives tmp directories only. No real vault, no real runtime, no
engine, and no network: the module has no `eval`, no shell and no remote fetch
to exercise.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from ciao import async_reads, fts_search, update_task_catalog, update_tasks, vault_index
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.update_task_catalog import CATALOG_FILENAME, UpdateTask, load_catalog
from ciao.update_tasks import (
    APPLICABILITY_TTL_S,
    APPLICABLE,
    NOT_APPLICABLE,
    UNKNOWN,
    Detection,
    UpdateTaskStateError,
)

# A detector name that has an implementation, and one that does not, so both
# halves of the registry contract are visible in the same fixture.
REGISTERED = "has-legacy-rows"
UNREGISTERED = "no-such-detector"
CHECK = "no-legacy-rows"

# The one real task (#728-E) and the two probes behind it.
SHIPPED: dict[str, str] = {
    "detector": "learnings-cleanup-review-needed",
    "completion_check": "learnings-cleanup-review-recorded",
}


@pytest.fixture(autouse=True)
def clean_caches() -> Iterator[None]:
    """Drop the module's process-wide caches between tests.

    The applicability cache is keyed by scope, and the read executor is shared by
    every `run_read` in the process. A test that evaluated a fixture workspace
    could otherwise be served a previous test's answer, or leave a worker
    running against a directory pytest has already deleted.
    """
    update_tasks.clear_applicability_cache()
    yield
    update_tasks.clear_applicability_cache()
    async_reads.reset_vault_read_executor()


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            )
        },
    )


def _task(**overrides: Any) -> UpdateTask:
    """One task definition, with `overrides` applied to it."""
    row: dict[str, Any] = {
        "id": "review-legacy-rows",
        "revision": 1,
        "since_version": "1.0.0",
        "scope": "workspace",
        "title": "Review legacy rows",
        "why": "Older entries need a decision before they can be indexed.",
        "detector": REGISTERED,
        "completion_check": CHECK,
        "prompt_resource": "prompts/review-legacy-rows-1.md",
    }
    row.update(overrides)
    return UpdateTask(**row)


# ── The state store ─────────────────────────────────────────────────────────


def test_state_round_trips_per_scope_and_holds_no_absolute_path(tmp_path: Path) -> None:
    """A workspace task and an install task land in two files, and both read back."""
    config = _config(tmp_path)
    vault_task = _task()
    install_task = _task(id="refresh-index", scope="install")

    update_tasks.write_task_state(
        update_tasks.TaskState(
            task_id=vault_task.id,
            revision=vault_task.revision,
            scope="workspace",
            lifecycle="offered",
            updated_at="2026-01-01T00:00:00+00:00",
            attempted_fingerprint="abc123",
            evidence={"rows": 12, "note": "Workspace/Learnings.md"},
        ),
        config=config,
        workspace="personal",
    )
    update_tasks.write_task_state(
        update_tasks.TaskState(
            task_id=install_task.id,
            revision=install_task.revision,
            scope="install",
            lifecycle="in_progress",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
        config=config,
        workspace="personal",
    )

    vault_file = config.workspace_vault_root("personal") / "Workspace" / "Update-Tasks.json"
    runtime_file = tmp_path / ".runtime" / "update-tasks.json"
    assert vault_file.is_file(), "a workspace task's state belongs to its vault"
    assert runtime_file.is_file(), "an install task's state belongs to the runtime"

    # Each file holds only its own scope's record, and both are schema-1
    # documents keyed by "<id>@<revision>".
    vault_document = json.loads(vault_file.read_text(encoding="utf-8"))
    assert vault_document["schema"] == update_tasks.STATE_SCHEMA
    assert list(vault_document["tasks"]) == ["review-legacy-rows@1"]
    runtime_document = json.loads(runtime_file.read_text(encoding="utf-8"))
    assert list(runtime_document["tasks"]) == ["refresh-index@1"]

    # Round trip: what was written is what a reader sees.
    state = update_tasks.read_task_state(vault_task, config=config, workspace="personal")
    assert state is not None
    assert state.lifecycle == "offered"
    assert state.attempted_fingerprint == "abc123"
    assert state.evidence == {"rows": 12, "note": "Workspace/Learnings.md"}
    assert state.scope == "workspace"
    assert update_tasks.read_task_state(vault_task, config=config, workspace="other") is None

    # The file travels: no absolute path from this machine is in it, while the
    # relative note path it was given is.
    for document_path in (vault_file, runtime_file):
        assert str(tmp_path) not in document_path.read_text(encoding="utf-8")
    assert "Workspace/Learnings.md" in vault_file.read_text(encoding="utf-8")


def test_write_refuses_an_absolute_path_in_evidence(tmp_path: Path) -> None:
    """A state file may not carry a location from this machine, so the write fails."""
    config = _config(tmp_path)
    task = _task()

    with pytest.raises(UpdateTaskStateError, match="not storable"):
        update_tasks.write_task_state(
            update_tasks.TaskState(
                task_id=task.id,
                revision=task.revision,
                scope="workspace",
                lifecycle="offered",
                updated_at="2026-01-01T00:00:00+00:00",
                evidence={"path": str(tmp_path / "memory-vault")},
            ),
            config=config,
            workspace="personal",
        )

    # Nothing was created: a refused write is not a partial one.
    assert not update_tasks.state_path_for(config, "workspace", "personal").exists()


def test_write_is_atomic_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `os.replace` per write, no temp file left, and identical bytes for a no-op write."""
    config = _config(tmp_path)
    task = _task()
    state = update_tasks.TaskState(
        task_id=task.id,
        revision=task.revision,
        scope="workspace",
        lifecycle="waiting_review",
        updated_at="2026-01-01T00:00:00+00:00",
        chat_id="chat-1",
    )

    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: Any, dst: Any, **kwargs: Any) -> None:
        seen.append((str(src), str(dst)))
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(update_tasks.os, "replace", spy)

    path = update_tasks.write_task_state(state, config=config, workspace="personal")
    first = path.read_bytes()
    update_tasks.write_task_state(state, config=config, workspace="personal")
    second = path.read_bytes()

    assert first == second, "writing an unchanged state must produce the same bytes"
    assert len(seen) == 2, "each write is one atomic rename, not a partial rewrite"
    for source, destination in seen:
        assert destination == str(path)
        assert Path(source).parent == path.parent, "the temp file is beside its target"
        assert source.endswith(".tmp")
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name], (
        "no temp file survives a write that landed"
    )


def test_update_tasks_file_is_reserved_from_recall() -> None:
    """`Workspace/Update-Tasks.json` is bookkeeping, in the index and in the lint.

    A guard rather than the fix for a live leak: every consumer of the reserved
    set reads markdown only today (`fts_search._index_directory`'s `.md`
    default, `fts_search.index_file`'s archived transcripts, `vault_index`'s
    `rglob("*.md")`, `vault_lint._markdown_source_paths`), so nothing can reach a
    `.json` file. It is here because that is where a vault's own bookkeeping
    belongs, and because the day a consumer widens to non-markdown files this
    name must already be in the set rather than becoming an index row.
    """
    assert "update-tasks.json" in vault_index.RESERVED_UNINDEXED_FILES
    assert fts_search.RESERVED_UNINDEXED_FILES is vault_index.RESERVED_UNINDEXED_FILES
    assert vault_index.is_reserved_bookkeeping(Path("Workspace/Update-Tasks.json"))
    # A user's own note that happens to share the name stays scannable.
    assert not vault_index.is_reserved_bookkeeping(Path("projects/acme/Update-Tasks.json"))
    assert not vault_index.is_reserved_bookkeeping(Path("Update-Tasks.json"))


def test_ordinary_text_that_merely_looks_like_a_path_stores(tmp_path: Path) -> None:
    """The portability rule must not swallow a reason the operator actually typed.

    A dismissal reason is prose. `a: not relevant`, `~50 notes` and `x:y` are not
    paths, and a rule that called them paths would raise out of
    `record_dismissal` and lose the decision — the one write here that needs no
    proof and no permission. What the rule refuses is the thing it is for: a
    location that means nothing on the next machine.
    """
    config = _config(tmp_path)
    task = _task()

    for reason in ("a: not relevant", "~50 notes", "x:y", "line 3:4", "50% done"):
        state = update_tasks.record_dismissal(
            task, config=config, workspace="personal", reason=reason
        )
        assert state.evidence["dismiss_reason"] == reason

    for text in ("a: b", "~50 notes", "x:y", "", "note.md", "Workspace/Learnings.md"):
        assert not update_tasks._looks_absolute(text), text
    for text in (
        "/Users/someone/vault",
        "\\Users\\someone",
        "~/vault",
        "~",
        "C:\\vault",
        "D:/vault",
        "a\0b",
    ):
        assert update_tasks._looks_absolute(text), text

    # And the refusal still stands for a real path, through the public writer.
    with pytest.raises(UpdateTaskStateError, match="not storable"):
        update_tasks.write_task_state(
            update_tasks.TaskState(
                task_id=task.id,
                revision=task.revision,
                scope="workspace",
                lifecycle="offered",
                updated_at="2026-01-01T00:00:00+00:00",
                evidence={"path": "~/vault"},
            ),
            config=config,
            workspace="personal",
        )


# ── Suppression, revision and reopening ─────────────────────────────────────


def test_dismissal_and_completion_suppress_only_at_their_own_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision at revision 1 is not a decision about revision 2."""
    config = _config(tmp_path)
    monkeypatch.setattr(
        update_tasks, "COMPLETION_FUNCTIONS", {CHECK: lambda **_: Detection(True, {"rows": 0})}
    )
    dismissed = _task()
    completed = _task(id="finish-migration")

    update_tasks.record_dismissal(
        dismissed, config=config, workspace="personal", reason="not now"
    )
    done = update_tasks.record_completion(completed, config=config, workspace="personal")
    assert done is not None and done.lifecycle == "completed"

    assert _suppressed(dismissed, config)
    assert _suppressed(completed, config)
    # The next revision of the same task is a different piece of work.
    assert not _suppressed(_task(revision=2), config)
    assert not _suppressed(_task(id="finish-migration", revision=2), config)
    # And an unfinished attempt is not a decision.
    update_tasks.write_task_state(
        update_tasks.TaskState(
            task_id=_task(id="another").id,
            revision=1,
            scope="workspace",
            lifecycle="in_progress",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
        config=config,
        workspace="personal",
    )
    assert not _suppressed(_task(id="another"), config)


def test_reopen_clears_a_dismissal_and_leaves_others_alone(tmp_path: Path) -> None:
    config = _config(tmp_path)
    task = _task()
    update_tasks.record_dismissal(
        task, config=config, workspace="personal", now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    reopened = update_tasks.reopen_task(
        task, config=config, workspace="personal", now=datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert reopened.lifecycle == "offered"
    assert reopened.updated_at == "2026-01-02T00:00:00+00:00"
    assert reopened.attempted_fingerprint == "", (
        "the dismissal was about the attempt it followed, not the next one"
    )
    state = update_tasks.read_task_state(task, config=config, workspace="personal")
    assert state is not None and state.lifecycle == "offered"

    # Reopening something that is not dismissed changes nothing and rewrites
    # nothing: an in-flight attempt is not something to overwrite.
    path = update_tasks.state_path_for(config, "workspace", "personal")
    before = path.read_bytes()
    again = update_tasks.reopen_task(task, config=config, workspace="personal")
    assert again.lifecycle == "offered"
    assert path.read_bytes() == before

    # And a task with nothing recorded has no dismissal to reverse, so reopen
    # writes nothing: an absent record already reads as "offered", and an empty
    # `offered` record would be a document a reader has to treat specially.
    unseen = _task(id="never-seen")
    fresh = update_tasks.reopen_task(
        unseen, config=config, workspace="personal", now=datetime(2026, 1, 3, tzinfo=UTC)
    )
    assert fresh.lifecycle == "offered"
    assert (
        update_tasks.read_task_state(unseen, config=config, workspace="personal") is None
    )
    assert path.read_bytes() == before, "no record was invented"


def test_a_concurrent_writer_cannot_interleave_inside_a_dismissal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the record a write replaces and writing it are one critical section.

    A record is the whole document, so a read taken outside the lock and carried
    into a later write drops whatever another writer added in between — a chat id
    the launch path set, an attempt fingerprint the check path set — with no error
    anywhere, because the second write simply wins. The observable is the lock
    itself: while a dismissal is between its read and its write, another writer of
    the same scope must be locked out.

    The read is slowed on the worker thread (and only there) so the main thread
    gets a deterministic window to probe, with a bounded wait so a regression
    fails the assertion instead of hanging the suite.
    """
    config = _config(tmp_path)
    task = _task()
    path = update_tasks.state_path_for(config, "workspace", "personal")
    lock = async_reads.keyed_lock(f"update-tasks:{path}")
    read_seen = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    real_read = update_tasks._read_document

    def _slow_read(target: Path) -> Any:
        outcome = real_read(target)
        if threading.current_thread() is not threading.main_thread():
            read_seen.set()
            release.wait(timeout=2.0)
        return outcome

    def _dismiss() -> None:
        try:
            update_tasks.record_dismissal(
                task, config=config, workspace="personal", reason="not now"
            )
        except BaseException as error:  # noqa: BLE001 — reported by the assertions
            failures.append(error)

    monkeypatch.setattr(update_tasks, "_read_document", _slow_read)
    # Daemon, and the probe lock is handed straight back: a regression here has
    # the worker blocked on the lock this test just took, and a suite that hangs
    # on a failed assertion reports less than a suite that fails.
    worker = threading.Thread(target=_dismiss, name="dismissal", daemon=True)
    worker.start()
    try:
        assert read_seen.wait(timeout=5.0), "the dismissal never read the record"
        acquired = lock.acquire(timeout=0.2)
        try:
            assert not acquired, (
                "another writer of this scope got in between the read of a "
                "record and the write that replaces it, so its update would "
                "be lost"
            )
        finally:
            if acquired:
                lock.release()
    finally:
        release.set()
        worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert failures == []
    state = update_tasks.read_task_state(task, config=config, workspace="personal")
    assert state is not None and state.lifecycle == "dismissed"


def test_completion_needs_a_registered_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No implementation, a raising check, or an unsatisfied check record nothing."""
    config = _config(tmp_path)
    task = _task()

    assert update_tasks.record_completion(task, config=config, workspace="personal") is None

    def _raise(**_: Any) -> Detection:
        raise RuntimeError("vault is locked")

    def _unmet(**_: Any) -> Detection:
        return Detection(False, {"rows": 9})

    monkeypatch.setattr(update_tasks, "COMPLETION_FUNCTIONS", {CHECK: _raise})
    assert update_tasks.record_completion(task, config=config, workspace="personal") is None
    monkeypatch.setattr(update_tasks, "COMPLETION_FUNCTIONS", {CHECK: _unmet})
    assert update_tasks.record_completion(task, config=config, workspace="personal") is None
    monkeypatch.setattr(update_tasks, "COMPLETION_FUNCTIONS", {CHECK: lambda **_: "yes"})
    assert update_tasks.record_completion(task, config=config, workspace="personal") is None

    assert not update_tasks.state_path_for(config, "workspace", "personal").exists()


def test_record_completion_keeps_storable_evidence_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check's own debug value is dropped rather than persisted or raised over."""
    config = _config(tmp_path)
    monkeypatch.setattr(
        update_tasks,
        "COMPLETION_FUNCTIONS",
        {
            CHECK: lambda **_: Detection(
                True, {"rows": 0, "scanned": "/Users/someone/vault", "tag": "ok"}
            )
        },
    )

    state = update_tasks.record_completion(_task(), config=config, workspace="personal")

    assert state is not None
    assert state.evidence == {"rows": 0, "tag": "ok"}
    assert str(tmp_path) not in json.dumps(state.evidence)


def test_a_lifecycle_written_over_an_existing_record_keeps_its_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second decision for the same task replaces the evidence, not the identity.

    The record that matters across a lifecycle change is the one a dismissal must
    not erase: which chat the task was in, which attempt it was, and which
    prompt. The evidence belongs to the lifecycle it justified, so it is the
    evidence that is replaced.
    """
    config = _config(tmp_path)
    task = _task()
    monkeypatch.setattr(
        update_tasks, "COMPLETION_FUNCTIONS", {CHECK: lambda **_: Detection(True, {"rows": 0})}
    )
    update_tasks.write_task_state(
        update_tasks.TaskState(
            task_id=task.id,
            revision=task.revision,
            scope="workspace",
            lifecycle="waiting_review",
            updated_at="2026-01-01T00:00:00+00:00",
            attempted_fingerprint="fp-1",
            chat_id="chat-7",
            prompt_digest="sha-9",
            evidence={"rows": 5},
        ),
        config=config,
        workspace="personal",
    )

    dismissed = update_tasks.record_dismissal(
        task, config=config, workspace="personal", reason="not now"
    )
    assert dismissed.chat_id == "chat-7"
    assert dismissed.attempted_fingerprint == "fp-1"
    assert dismissed.prompt_digest == "sha-9"
    assert dismissed.evidence == {"dismiss_reason": "not now"}

    completed = update_tasks.record_completion(task, config=config, workspace="personal")
    assert completed is not None
    assert completed.lifecycle == "completed"
    assert completed.chat_id == "chat-7"
    assert completed.attempted_fingerprint == "fp-1"
    assert completed.evidence == {"rows": 0}

    # One record for this revision, and it is the one a reader sees.
    document = json.loads(
        update_tasks.state_path_for(config, "workspace", "personal").read_text(
            encoding="utf-8"
        )
    )
    assert list(document["tasks"]) == ["review-legacy-rows@1"]
    state = update_tasks.read_task_state(task, config=config, workspace="personal")
    assert state is not None and state.lifecycle == "completed"


# ── Applicability ───────────────────────────────────────────────────────────


def test_registered_detector_answers_all_three_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    task = _task()
    calls: list[date] = []

    def _rows(*, config: Any, workspace: str, today: date) -> Detection:
        calls.append(today)
        return Detection(True, {"rows": 3, "workspace": workspace})

    monkeypatch.setattr(update_tasks, "DETECTOR_FUNCTIONS", {REGISTERED: _rows})

    applicable = update_tasks.apply_detector(
        task, config=config, workspace="personal", today=date(2026, 3, 1)
    )
    assert applicable.status == APPLICABLE
    assert applicable.evidence["rows"] == 3
    assert calls == [date(2026, 3, 1)], "the date is passed through, not read here"

    monkeypatch.setattr(
        update_tasks,
        "DETECTOR_FUNCTIONS",
        {REGISTERED: lambda **_: Detection(False, {"rows": 0})},
    )
    absent = update_tasks.apply_detector(task, config=config, workspace="personal")
    assert absent.status == NOT_APPLICABLE

    # The fingerprint follows the situation, not the wording: same numbers, a
    # different task, a different digest.
    assert absent.fingerprint != applicable.fingerprint
    assert (
        update_tasks.apply_detector(
            _task(revision=2), config=config, workspace="personal"
        ).fingerprint
        != absent.fingerprint
    )
    assert len(absent.fingerprint) == update_tasks.FINGERPRINT_CHARS


def test_unregistered_detector_is_unknown_and_never_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped state: a row's name with no implementation behind it."""
    config = _config(tmp_path)
    task = _task(detector=UNREGISTERED)
    monkeypatch.setattr(update_tasks, "DETECTOR_FUNCTIONS", {})

    result = update_tasks.apply_detector(task, config=config, workspace="personal")

    assert result.status == UNKNOWN
    assert result.evidence["reason"] == "detector_not_implemented"
    assert result.evidence["detector"] == UNREGISTERED
    assert result.status in update_tasks.APPLICABILITY_STATUSES


def test_a_failing_or_rude_detector_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    task = _task()

    def _raise(**_: Any) -> Detection:
        raise OSError("vault is on a disconnected volume")

    def _rude(**_: Any) -> Any:
        return "probably yes"

    for probe, reason in (
        (_raise, "detector_failed"),
        (_rude, "detector_returned_no_detection"),
    ):
        monkeypatch.setattr(update_tasks, "DETECTOR_FUNCTIONS", {REGISTERED: probe})
        result = update_tasks.apply_detector(task, config=config, workspace="personal")
        assert result.status == UNKNOWN, f"{reason} must not look applicable"
        assert result.evidence["reason"] == reason

    # Even evidence that cannot be canonicalized still yields a fingerprint.
    monkeypatch.setattr(
        update_tasks,
        "DETECTOR_FUNCTIONS",
        {REGISTERED: lambda **_: Detection(True, {"rows": {1: "x"}})},
    )
    assert update_tasks.apply_detector(task, config=config, workspace="personal").fingerprint


async def test_an_unreadable_state_file_is_unknown_not_an_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document this code cannot read withholds the answer instead of re-offering."""
    config = _config(tmp_path)
    path = update_tasks.state_path_for(config, "workspace", "personal")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema": 1, "tasks": {"a@1": ', encoding="utf-8")
    ran: list[str] = []
    monkeypatch.setattr(
        update_tasks,
        "DETECTOR_FUNCTIONS",
        {REGISTERED: lambda **_: (ran.append("x"), Detection(True))[1]},
    )

    statuses = await update_tasks.evaluate(
        config,
        workspace="personal",
        installed_version="1.2.0",
        catalog=_catalog(_task()),
    )

    assert statuses[0].applicability.status == UNKNOWN
    assert statuses[0].applicability.evidence["reason"] == "state_unreadable"
    assert not statuses[0].offered
    assert ran == [], "an answer nobody may report is not worth computing"

    # And a write refuses to build a new document on top of the unreadable one.
    with pytest.raises(UpdateTaskStateError, match="readable"):
        update_tasks.record_dismissal(_task(), config=config, workspace="personal")


async def test_a_state_file_that_becomes_unreadable_outranks_a_warm_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache hit is not allowed to answer for a file the install cannot read.

    A state file can become unreadable inside a window — a half-finished sync, a
    hand edit, a filesystem that lost the file mid-read. The record is what says
    whether the operator already declined the task, so a cached detector answer
    must not stand in for it: that is a task coming back as offered on the
    strength of an answer the install can no longer support.
    """
    config = _config(tmp_path)
    calls: list[int] = []

    def _counting(**_: Any) -> Detection:
        calls.append(1)
        return Detection(True, {"rows": 3})

    monkeypatch.setattr(update_tasks, "DETECTOR_FUNCTIONS", {REGISTERED: _counting})
    catalog = _catalog(_task())
    path = update_tasks.state_path_for(config, "workspace", "personal")
    kwargs = {
        "workspace": "personal",
        "installed_version": "1.2.0",
        "catalog": catalog,
        "change_token": "a",
        "now": 1_000.0,
    }

    warm = await update_tasks.evaluate(config, **kwargs)
    assert warm[0].offered, "the window has to be warm for this to be the bug"
    assert calls == [1]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema": 1, "tasks": {"review-legacy-rows@1": ', encoding="utf-8")

    cold = await update_tasks.evaluate(config, **kwargs)
    assert cold[0].applicability.status == UNKNOWN
    assert cold[0].applicability.evidence["reason"] == "state_unreadable"
    assert not cold[0].offered
    assert calls == [1], "an answer nobody may report is not worth recomputing"

    # The withheld answer was not stored either: a repaired file is answered by
    # the window that was already open, not by the `unknown` the broken file
    # produced, and a stored `unknown` would have hidden the task for the rest of
    # the window.
    path.write_text('{"schema": 1, "tasks": {}}', encoding="utf-8")
    healed = await update_tasks.evaluate(config, **kwargs)
    assert healed[0].applicability.status == APPLICABLE
    assert healed[0].offered
    assert calls == [1], "the answer from before the corruption is still fresh"


# ── The cached check ────────────────────────────────────────────────────────


async def test_evaluate_honours_the_ttl_and_the_change_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detector runs once per fresh window, and a change re-runs it at once."""
    config = _config(tmp_path)
    calls: list[str] = []

    def _counting(*, config: Any, workspace: str, today: date) -> Detection:
        calls.append(workspace)
        return Detection(True, {"rows": len(calls)})

    monkeypatch.setattr(update_tasks, "DETECTOR_FUNCTIONS", {REGISTERED: _counting})
    catalog = _catalog(_task())
    start = 1_000.0
    keys: list[str] = []
    real_run_read = update_tasks.run_read

    async def _spy(key: str, operation: Any, **kwargs: Any) -> Any:
        keys.append(key)
        return await real_run_read(key, operation, **kwargs)

    monkeypatch.setattr(update_tasks, "run_read", _spy)

    async def _evaluate(token: str, instant: float) -> list[update_tasks.TaskStatus]:
        return await update_tasks.evaluate(
            config,
            workspace="personal",
            installed_version="1.2.0",
            catalog=catalog,
            change_token=token,
            now=instant,
        )

    first = await _evaluate("token-a", start)
    again = await _evaluate("token-a", start + APPLICABILITY_TTL_S - 1)
    assert len(calls) == 1, "a fresh window must not re-run the detector"
    assert first[0].applicability.fingerprint == again[0].applicability.fingerprint

    expired = await _evaluate("token-a", start + APPLICABILITY_TTL_S)
    assert len(calls) == 2, "the window is finite, so it expires"
    assert expired[0].applicability.evidence["rows"] == 2

    changed = await _evaluate("token-b", start)
    assert len(calls) == 3, "a new change token invalidates immediately"
    assert changed[0].applicability.evidence["rows"] == 3

    # A caller that supplies no token gets a purely time-based window.
    untokened = await _evaluate("", start + APPLICABILITY_TTL_S - 1)
    assert len(calls) == 4
    assert untokened[0].applicability.evidence["rows"] == 4

    # Every one of those checks went through the bounded off-loop executor,
    # keyed so that two callers of the same situation share one worker — and
    # the cache hit is still a read, because the state file is not cached.
    assert len(keys) == 5
    assert all(key.startswith("update-tasks:") for key in keys)
    assert keys[0].endswith("personal:1.2.0:token-a")
    assert keys[3].endswith("personal:1.2.0:token-b")
    assert keys[4].endswith("personal:1.2.0:")


async def test_the_freshness_window_is_per_task_not_per_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One task being recomputed must not extend the window of another.

    The window is a claim about an answer, so it has to travel with that answer.
    Held per scope instead, a catalog that grew a task would re-stamp every
    answer in the scope: the task evaluated at ``t0`` would be served until
    ``t0 + 299 + TTL`` rather than ``t0 + TTL``, and an answer computed for one
    change token would be served under the next one. Both make the app report a
    condition that has since changed, for as long as the app keeps being looked
    at — the one thing a freshness window is for.
    """
    config = _config(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        update_tasks,
        "DETECTOR_FUNCTIONS",
        {REGISTERED: lambda **_: (calls.append("x"), Detection(True))[1]},
    )
    early = _task(id="early")
    late = _task(id="late")
    start = 1_000.0

    async def _evaluate(
        catalog: update_task_catalog.TaskCatalog, token: str, instant: float
    ) -> list[update_tasks.TaskStatus]:
        return await update_tasks.evaluate(
            config,
            workspace="personal",
            installed_version="1.2.0",
            catalog=catalog,
            change_token=token,
            now=instant,
        )

    await _evaluate(_catalog(early), "a", start)
    assert calls == ["x"]

    # A catalog that gained a task one second before the first window closes.
    await _evaluate(_catalog(early, late), "a", start + APPLICABILITY_TTL_S - 1)
    assert calls == ["x", "x"], "the new task runs, the warm one does not"

    # `early` must now be exactly at its own TTL, whatever `late` did.
    await _evaluate(_catalog(early, late), "a", start + APPLICABILITY_TTL_S)
    assert calls == ["x", "x", "x"], (
        "the answer computed at t0 expires at t0 + TTL, not at the last "
        "evaluation's TTL"
    )

    # And a different situation re-runs every task, including one whose answer
    # was inside its window.
    await _evaluate(_catalog(early, late), "b", start + APPLICABILITY_TTL_S + 1)
    assert calls == ["x", "x", "x", "x", "x"], (
        "no answer computed for another token is reused under this one"
    )


async def test_a_failing_detector_is_retried_inside_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detector that raised is not cached: the fault may already be gone.

    A probe that failed says nothing about the workspace, so holding it for the
    whole window would keep reporting ``unknown`` for five minutes after the
    volume came back — the operator's task stays hidden because of an error that
    no longer exists.
    """
    config = _config(tmp_path)
    attempts: list[int] = []

    def _flaky(**_: Any) -> Detection:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("the vault is on a disconnected volume")
        return Detection(True, {"rows": 1})

    monkeypatch.setattr(update_tasks, "DETECTOR_FUNCTIONS", {REGISTERED: _flaky})
    catalog = _catalog(_task())
    kwargs = {"workspace": "personal", "installed_version": "1.2.0", "catalog": catalog}

    first = await update_tasks.evaluate(
        config, change_token="a", now=1_000.0, **kwargs
    )
    assert first[0].applicability.status == UNKNOWN
    assert first[0].applicability.evidence["reason"] == "detector_failed"

    second = await update_tasks.evaluate(
        config, change_token="a", now=1_000.5, **kwargs
    )
    assert len(attempts) == 2, "a failed probe is retried rather than remembered"
    assert second[0].applicability.status == APPLICABLE

    # A real answer *is* remembered, so this is not the cache being disabled.
    third = await update_tasks.evaluate(
        config, change_token="a", now=1_000.6, **kwargs
    )
    assert len(attempts) == 2
    assert third[0].applicability.status == APPLICABLE


async def test_evaluate_reports_a_dismissed_task_as_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suppression comes from the state read, which is never cached."""
    config = _config(tmp_path)
    monkeypatch.setattr(
        update_tasks,
        "DETECTOR_FUNCTIONS",
        {REGISTERED: lambda **_: Detection(True, {"rows": 1})},
    )
    catalog = _catalog(_task())
    kwargs = {"workspace": "personal", "installed_version": "1.2.0", "catalog": catalog}

    before = await update_tasks.evaluate(config, **kwargs)
    assert update_tasks.offered_tasks(before)[0].task.id == "review-legacy-rows"

    update_tasks.record_dismissal(_task(), config=config, workspace="personal")

    after = await update_tasks.evaluate(config, **kwargs)
    assert after[0].applicability.status == APPLICABLE, (
        "the answer is still applicable; the decision is what changed"
    )
    assert after[0].suppressed and not after[0].offered
    assert update_tasks.offered_tasks(after) == []


async def test_a_downgrade_hides_a_task_and_keeps_its_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        update_tasks, "DETECTOR_FUNCTIONS", {REGISTERED: lambda **_: Detection(True)}
    )
    catalog = _catalog(_task(since_version="2.0.0"))
    update_tasks.record_dismissal(
        _task(since_version="2.0.0"), config=config, workspace="personal"
    )

    hidden = await update_tasks.evaluate(
        config, workspace="personal", installed_version="1.2.0", catalog=catalog
    )
    assert hidden == [], "a version that cannot support a task does not offer it"

    upgraded = await update_tasks.evaluate(
        config, workspace="personal", installed_version="2.1.0", catalog=catalog
    )
    assert len(upgraded) == 1
    assert upgraded[0].suppressed, "the record survived the downgrade"
    assert (
        update_tasks.read_task_state(
            _task(since_version="2.0.0"), config=config, workspace="personal"
        )
        is not None
    )


# ── The shipped catalog, and the layer over an injected one ─────────────────


def test_the_shipped_catalog_ships_one_task_and_both_probes_exist(
    tmp_path: Path,
) -> None:
    """The catalog's names and this module's implementations are the same set.

    This is the property the whole applicability layer rests on: a task row may
    only name a probe that exists, so there is no state in which the packaged
    catalog offers follow-up work this engine cannot decide or verify. Before
    #728-E both registries were empty and the catalog shipped no task; the first
    real task landed with its detector and its completion check, and the two have
    to keep landing together.
    """
    catalog = load_catalog()

    assert not catalog.diagnostics, (
        "the packaged catalog has diagnostics: "
        f"{[(d.code, d.message) for d in catalog.diagnostics]}"
    )
    assert [task.id for task in catalog.tasks] == ["learnings-cleanup"]
    assert update_task_catalog.DETECTORS == set(update_tasks.DETECTOR_FUNCTIONS)
    assert update_task_catalog.COMPLETION_CHECKS == set(
        update_tasks.COMPLETION_FUNCTIONS
    )
    # And the shipped task actually reaches its probe over a real workspace,
    # rather than answering `detector_not_implemented` on a Home render. An empty
    # vault has no learnings document, so the honest answer here is
    # `not_applicable` — the postcondition is absent, and that is a claim rather
    # than an admission that nothing ran.
    for task in catalog.tasks:
        result = update_tasks.apply_detector(
            task, config=_config(tmp_path), workspace="personal"
        )
        assert result.status == NOT_APPLICABLE, result.evidence
        assert "detector_not_implemented" not in result.evidence.values()


async def test_a_test_injected_catalog_proves_the_layer_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path an installed wheel runs, over a temp packaged root.

    The catalog is injected through `update_task_catalog.packaged_root` rather
    than handed to `evaluate`, so `load_catalog()` really is the thing under test
    — the same call a Home render would make. Revision 2 replaces revision 1 in
    the file (a catalog holds one definition per id, which is the loader's rule,
    not this layer's), and that replacement is what a maintainer means by "the
    work behind this changed": the task is offered again.
    """
    config = _config(tmp_path)
    root = tmp_path / "packaged"
    (root / "prompts").mkdir(parents=True)
    monkeypatch.setattr(update_task_catalog, "packaged_root", lambda: root)
    monkeypatch.setattr(update_task_catalog, "DETECTORS", frozenset({REGISTERED}))
    monkeypatch.setattr(update_task_catalog, "COMPLETION_CHECKS", frozenset({CHECK}))
    monkeypatch.setattr(
        update_tasks,
        "DETECTOR_FUNCTIONS",
        {REGISTERED: lambda **_: Detection(True, {"rows": 7})},
    )

    def _publish(revision: int) -> None:
        (root / CATALOG_FILENAME).write_text(
            json.dumps([_row(revision=revision)]), encoding="utf-8"
        )
        (root / "prompts" / f"review-legacy-rows-{revision}.md").write_text(
            f"Revision {revision}.\n", encoding="utf-8"
        )

    _publish(1)
    fresh = await update_tasks.evaluate(
        config, workspace="personal", installed_version="1.2.0", change_token="a"
    )

    assert [status.task.revision for status in fresh] == [1]
    assert all(status.applicability.status == APPLICABLE for status in fresh)
    assert all(status.applicability.evidence["detector"] == REGISTERED for status in fresh)
    assert all(status.offered for status in fresh)

    update_tasks.record_dismissal(fresh[0].task, config=config, workspace="personal")
    dismissed = await update_tasks.evaluate(
        config, workspace="personal", installed_version="1.2.0", change_token="a"
    )
    assert dismissed[0].applicability.status == APPLICABLE, (
        "the detector's answer did not change; the decision did"
    )
    assert dismissed[0].suppressed
    assert update_tasks.offered_tasks(dismissed) == []

    _publish(2)
    revised = await update_tasks.evaluate(
        config, workspace="personal", installed_version="1.2.0", change_token="a"
    )
    assert [status.task.revision for status in revised] == [2]
    assert revised[0].offered, "a new revision is a new piece of work"
    assert (
        update_tasks.read_task_state(
            _task(), config=config, workspace="personal"
        ).lifecycle
        == "dismissed"
    ), "the old record is still on disk, not overwritten by the new revision"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _suppressed(task: UpdateTask, config: CiaoConfig) -> bool:
    state = update_tasks.read_task_state(task, config=config, workspace="personal")
    return state is not None and state.lifecycle in update_tasks.SUPPRESSING_LIFECYCLES


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "review-legacy-rows",
        "revision": 1,
        "since_version": "1.0.0",
        "scope": "workspace",
        "title": "Review legacy rows",
        "why": "Older entries need a decision before they can be indexed.",
        "detector": REGISTERED,
        "completion_check": CHECK,
        "prompt_resource": "prompts/review-legacy-rows-1.md",
        "depends_on": [],
    }
    row.update(overrides)
    return row


def _catalog(*tasks: UpdateTask) -> update_task_catalog.TaskCatalog:
    """A loaded catalog of the definitions the test already has, in order.

    Variadic because the freshness window is a per-task claim: a test that wants
    to see one task's window close while another's stays open needs two tasks in
    one catalog.
    """
    return update_task_catalog.TaskCatalog(tasks=tuple(tasks))


# ── The shipped probes (#728-E) ─────────────────────────────────────────────


def _learnings_vault(tmp_path: Path, text: str) -> CiaoConfig:
    """A registry whose ``personal`` workspace holds a learnings document."""
    config = _config(tmp_path)
    vault = Path(config.workspace_vault_root("personal"))
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    (vault / "Workspace" / "Learnings.md").write_text(text, encoding="utf-8")
    return config


_ONE_ENTRY = (
    "---\ntags: [ciao, learnings]\nupdated: 2026-09-01\n---\n"
    "# Learnings\n\n## Active\n\n"
    "- Nothing has ever proposed this lesson.\n"
)


def test_the_shipped_detector_answers_on_the_vault_it_is_given(tmp_path: Path) -> None:
    """Applicable exactly when the reconciliation has rows it will not retire.

    Those are the rows the attended workflow exists for. An empty set of them means
    the unattended pass is already doing everything this engine can do on its own,
    so there is nothing to offer — and the evidence is the counts, not a boolean,
    so a document that gained a lesson comes back as a different situation.
    """
    detector = update_tasks.DETECTOR_FUNCTIONS[SHIPPED["detector"]]
    config = _learnings_vault(tmp_path, _ONE_ENTRY)

    outcome = detector(config=config, workspace="personal", today=date(2026, 9, 30))

    assert isinstance(outcome, Detection)
    assert outcome.applicable is True
    assert outcome.evidence["active"] == 1
    assert outcome.evidence["unmatched"] == 1
    assert outcome.evidence["reasons"] == ["never_proposed"]

    # A workspace with no learnings document has nothing for a person to review,
    # which is a positive claim about the absence rather than a failure to look.
    empty = _config(tmp_path / "other")
    (Path(empty.workspace_vault_root("personal")) / "Workspace").mkdir(parents=True)
    assert detector(
        config=empty, workspace="personal", today=date(2026, 9, 30)
    ).applicable is False


def test_the_shipped_detector_refuses_to_plan_when_the_store_is_unreadable(
    tmp_path: Path,
) -> None:
    """A store this code cannot read blocks the plan, and the detector has to
    report that as "no" rather than as "applicable" — offering the operator a
    review of a document the engine has just refused to reconcile."""
    detector = update_tasks.DETECTOR_FUNCTIONS[SHIPPED["detector"]]
    config = _learnings_vault(tmp_path, _ONE_ENTRY)
    store = Path(config.workspace_vault_root("personal")) / "Workspace" / (
        "learnings-cleanup.json"
    )
    store.write_text("{ not json", encoding="utf-8")

    outcome = detector(config=config, workspace="personal", today=date(2026, 9, 30))

    assert outcome.applicable is False
    assert "cannot be read" in outcome.evidence["blocked"]


def test_the_shipped_completion_check_needs_a_receipt_for_the_current_revision(
    tmp_path: Path,
) -> None:
    """A receipt, not a chat — and for *this* document.

    The postcondition is "a person looked at this file as it is now and said what
    should go", and the receipt is the only durable record of it. One that names a
    revision the file no longer has is a review of a different document, and
    treating it as one is how a stale cleanup comes to certify itself.
    """
    from ciao import learnings_cleanup

    check = update_tasks.COMPLETION_FUNCTIONS[SHIPPED["completion_check"]]
    config = _learnings_vault(tmp_path, _ONE_ENTRY)
    vault = Path(config.workspace_vault_root("personal"))
    migration = Path(config.state_path).parent / "migration"
    migration.mkdir(parents=True, exist_ok=True)

    # Nothing has been reviewed: not complete.
    assert check(config=config, workspace="personal").applicable is False

    # A reviewed no-op: the caller attests, and a receipt lands with no removals.
    plan = learnings_cleanup.plan_cleanup(vault, workspace="personal", config=config)
    result = learnings_cleanup.apply_cleanup(
        vault,
        plan,
        workspace="personal",
        config=config,
        actor="operator",
        today=date(2026, 9, 30),
        reviewed=True,
    )
    learnings_cleanup.write_receipt(
        learnings_cleanup.new_receipt_path(Path(config.state_path).parent), result.receipt
    )
    assert check(config=config, workspace="personal").applicable is True

    # Edit the document: the receipt is now about other bytes.
    (vault / "Workspace" / "Learnings.md").write_text(
        _ONE_ENTRY + "- A brand new lesson.\n", encoding="utf-8"
    )
    stale = check(config=config, workspace="personal")
    assert stale.applicable is False
    assert stale.evidence["reason"] == "no_review_receipt"


def test_the_shipped_completion_check_accepts_a_receipt_that_removed_something(
    tmp_path: Path,
) -> None:
    """A removal is the other half of the same answer, and the receipt names the
    revision it *left* as well as the one it read — so the check has to look at
    both sides or it would never be satisfied by the work it was written for."""
    from ciao import learnings_cleanup
    from ciao.learning_records import (
        LearningRecord,
        allocate_learning_id,
        entry_revision,
        render_learning,
    )
    from ciao import skill_proposals as sp
    from ciao.memory_receipts import write_queue_atomically

    record = LearningRecord(
        learning_id=allocate_learning_id("personal", "- A settled lesson."),
        key="settled",
        text="A settled lesson.",
    )
    config = _learnings_vault(
        tmp_path,
        "---\ntags: [ciao, learnings]\nupdated: 2026-09-01\n---\n"
        "# Learnings\n\n## Active\n\n"
        f"{render_learning(record)}\n- Nothing has ever proposed this lesson.\n",
    )
    vault = Path(config.workspace_vault_root("personal"))
    proposal = sp.SkillProposal(
        id=sp.proposal_id("personal", "web-research"),
        workspace="personal",
        skill="web-research",
        canonical_path="/agent/skills/web-research/SKILL.md",
        reviewed_revision="a" * 64,
        title="t", problem="p", change="c", rationale="r",
        sources=(sp.SkillEvidence(chat_id="s", archive="a", turn="", excerpt="e"),),
        lifecycle=sp.PENDING, chat_id="", updated_at="2026-09-01T00:00:00Z",
        origins=(sp.SkillOrigin(
            workspace="personal", learning_id=record.learning_id,
            source_revision=entry_revision(record), finding="add it",
            state=sp.ORIGIN_APPLIED, verification="mrcpt_0123456789abcdef"),),
    )
    write_queue_atomically(
        sp.proposal_path(config, "personal", "web-research"), sp.render_proposal(proposal)
    )

    plan = learnings_cleanup.plan_cleanup(vault, workspace="personal", config=config)
    assert [row.key for row in plan.removals] == ["settled"]
    result = learnings_cleanup.apply_cleanup(
        vault, plan, workspace="personal", config=config, today=date(2026, 9, 30)
    )
    learnings_cleanup.write_receipt(
        learnings_cleanup.new_receipt_path(Path(config.state_path).parent), result.receipt
    )

    check = update_tasks.COMPLETION_FUNCTIONS[SHIPPED["completion_check"]]
    outcome = check(config=config, workspace="personal")

    assert outcome.applicable is True
    assert outcome.evidence["removed"] == 1
