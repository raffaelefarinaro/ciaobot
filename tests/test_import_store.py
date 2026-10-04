"""The private import batch store: selection, digests, progress, dedupe, retention.

What is pinned here is the store contract from #1032, not the routes (those
are ``tests/test_import_batch_routes.py``): workspace scoping, the
``queued → running → done | failed | cancelled | partial`` lifecycle,
idempotent cancellation that freezes progress, fail-closed corruption,
the batch-level ``(provider, source_id, digest)`` re-run dedupe, the
retention sweep keeping accepted provenance, the lock against a concurrent
store instance, and the private atomic write. No provider or model is
involved anywhere: the store never leaves the runtime directory.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ciao.import_discover import BATCH_CAP
from ciao.import_store import (
    CONFLICT,
    CORRUPT_STORE,
    IMPORT_SNAPSHOT_RETENTION_DAYS,
    INVALID_BATCH,
    NOT_FOUND,
    UNSUPPORTED_SCHEMA,
    FactProvenance,
    ImportStore,
    ImportStoreError,
    engine_store_path,
    sweep_import_batches,
)
from ciao.os_support.private import is_private

DIGEST_A = "ab" * 32
DIGEST_B = "cd" * 32


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self, moment: datetime) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now


def _store(tmp_path: Path, clock: _Clock) -> ImportStore:
    return ImportStore(tmp_path / ".runtime" / "import" / "import-batches.json", clock=clock)


def _moment() -> datetime:
    return datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _pair(source_id: str, provider: str = "claude_code") -> dict[str, str]:
    return {"provider": provider, "source_id": source_id}


def _provenance(
    source_id: str, *, accepted: bool, workspace: str = "personal"
) -> FactProvenance:
    return FactProvenance(
        provider="claude_code",
        source_id=source_id,
        anchor="msg_1",
        destination=workspace,
        accepted=accepted,
    )


# ── create / get / list, scoped to a workspace ───────────────────────────


def test_create_get_and_list_are_scoped_to_a_workspace(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))

    mine = store.create(
        workspace="personal", sources=[_pair("sess-a"), _pair("sess-b", "opencode")]
    )
    store.create(workspace="work", sources=[_pair("sess-w")])

    assert mine.status == "queued"
    assert mine.destination == "personal"
    assert mine.progress.total_sources == 2
    assert [source.source_id for source in mine.sources] == ["sess-a", "sess-b"]
    assert all(source.content_digest == "" for source in mine.sources)

    assert store.get(mine.batch_id).batch_id == mine.batch_id
    assert [batch.batch_id for batch in store.list_for_workspace("personal")] == [
        mine.batch_id
    ]
    assert [batch.workspace for batch in store.list_for_workspace("work")] == ["work"]
    with pytest.raises(ImportStoreError) as excinfo:
        store.get("0" * 32)
    assert excinfo.value.code == NOT_FOUND


# ── progress transitions ─────────────────────────────────────────────────


def test_a_batch_walks_queued_running_done(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    batch = store.create(workspace="personal", sources=[_pair("sess-a")])

    running = store.begin(batch.batch_id)
    assert running.status == "running"

    read = store.record_source(
        batch.batch_id, "claude_code", "sess-a", content_digest=DIGEST_A,
        status="extracted",
    )
    assert read.sources[0].content_digest == DIGEST_A
    assert read.sources[0].status == "extracted"

    progressed = store.record_progress(
        batch.batch_id,
        completed_sources=1,
        proposals_filed=3,
        skipped=1,
        current_source_id="sess-a",
    )
    assert progressed.progress.completed_sources == 1
    assert progressed.progress.proposals_filed == 3
    assert progressed.progress.skipped == 1

    done = store.finish(batch.batch_id, "done")
    assert done.status == "done"

    with pytest.raises(ImportStoreError) as excinfo:
        store.begin(batch.batch_id)
    assert excinfo.value.code == CONFLICT
    with pytest.raises(ImportStoreError) as excinfo:
        store.finish(batch.batch_id, "done")
    assert excinfo.value.code == CONFLICT


def test_finish_from_queued_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    batch = store.create(workspace="personal", sources=[_pair("sess-a")])

    with pytest.raises(ImportStoreError) as excinfo:
        store.finish(batch.batch_id, "done")
    assert excinfo.value.code == CONFLICT
    with pytest.raises(ImportStoreError) as excinfo:
        store.finish(batch.batch_id, "cancelled")
    assert excinfo.value.code == INVALID_BATCH


# ── cancel is idempotent and freezes progress ────────────────────────────


def test_cancel_is_idempotent_and_stops_at_the_current_step(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    batch = store.create(workspace="personal", sources=[_pair("sess-a")])
    store.begin(batch.batch_id)
    store.record_progress(batch.batch_id, completed_sources=0, proposals_filed=2)

    first = store.cancel(batch.batch_id, reason="operator stopped it")
    assert first.status == "cancelled"
    assert first.progress.proposals_filed == 2

    second = store.cancel(batch.batch_id, reason="pressed twice")
    assert second.status == "cancelled"
    assert second.error == "operator stopped it"

    for call in (
        lambda: store.record_progress(batch.batch_id, completed_sources=1),
        lambda: store.record_source(batch.batch_id, "claude_code", "sess-a"),
        lambda: store.record_fact_provenance(batch.batch_id, _provenance("sess-a", accepted=False)),
        lambda: store.finish(batch.batch_id, "done"),
    ):
        with pytest.raises(ImportStoreError) as excinfo:
            call()
        assert excinfo.value.code == CONFLICT


def test_cancelling_a_settled_batch_is_a_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    batch = store.create(workspace="personal", sources=[_pair("sess-a")])
    store.begin(batch.batch_id)
    store.finish(batch.batch_id, "partial")

    with pytest.raises(ImportStoreError) as excinfo:
        store.cancel(batch.batch_id)
    assert excinfo.value.code == CONFLICT


# ── a corrupt store fails closed ─────────────────────────────────────────


@pytest.mark.parametrize("payload", ["{ not json", '{"schema": 999, "batches": {}}'])
def test_a_corrupt_store_fails_closed_and_is_never_reset(
    tmp_path: Path, payload: str
) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    created = store.create(workspace="personal", sources=[_pair("sess-a")])
    store.path.write_text(payload, encoding="utf-8")

    for call in (
        lambda: store.get(created.batch_id),
        lambda: store.list_for_workspace("personal"),
        lambda: store.create(workspace="personal", sources=[_pair("sess-b")]),
    ):
        with pytest.raises(ImportStoreError) as excinfo:
            call()
        assert excinfo.value.code in (CORRUPT_STORE, UNSUPPORTED_SCHEMA)

    # The bytes are left alone: a failed read never resets the store.
    assert store.path.read_text(encoding="utf-8") == payload


# ── the batch-level dedupe key ───────────────────────────────────────────


def test_rerunning_the_same_conversation_is_a_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    first = store.create(
        workspace="personal", sources=[{**_pair("sess-a"), "content_digest": DIGEST_A}]
    )
    store.begin(first.batch_id)
    store.finish(first.batch_id, "done")

    # Same (provider, source_id, digest): the conversation is already covered.
    with pytest.raises(ImportStoreError) as excinfo:
        store.create(
            workspace="personal",
            sources=[{**_pair("sess-a"), "content_digest": DIGEST_A}],
        )
    assert excinfo.value.code == CONFLICT
    assert first.batch_id in str(excinfo.value)

    # An unknown digest matches: re-selecting an unread conversation is the
    # same conversation until its content says otherwise.
    with pytest.raises(ImportStoreError) as excinfo:
        store.create(workspace="personal", sources=[_pair("sess-a")])
    assert excinfo.value.code == CONFLICT

    # A different digest is different content, so it may be re-run.
    rerun = store.create(
        workspace="personal",
        sources=[{**_pair("sess-a"), "content_digest": DIGEST_B}],
    )
    assert rerun.batch_id != first.batch_id
    store.begin(rerun.batch_id)
    store.finish(rerun.batch_id, "done")

    # Forgetting releases the key: the conversation may be re-run.
    store.forget(first.batch_id)
    store.forget(rerun.batch_id)
    third = store.create(workspace="personal", sources=[_pair("sess-a")])
    assert third.batch_id not in (first.batch_id, rerun.batch_id)


def test_one_batch_at_a_time_per_workspace(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    store.create(workspace="personal", sources=[_pair("sess-a")])

    with pytest.raises(ImportStoreError) as excinfo:
        store.create(workspace="personal", sources=[_pair("sess-other")])
    assert excinfo.value.code == CONFLICT

    # Another workspace is unaffected.
    other = store.create(workspace="work", sources=[_pair("sess-other")])
    assert other.workspace == "work"


# ── retention: unaccepted snapshots expire, accepted evidence stays ──────


def test_prune_expired_keeps_accepted_provenance(tmp_path: Path) -> None:
    clock = _Clock(_moment())
    store = _store(tmp_path, clock)
    old = store.create(workspace="personal", sources=[_pair("sess-old")])
    store.begin(old.batch_id)
    store.record_source(
        old.batch_id, "claude_code", "sess-old", content_digest=DIGEST_A
    )
    store.record_fact_provenance(old.batch_id, _provenance("sess-old", accepted=True))
    store.record_fact_provenance(
        old.batch_id, _provenance("sess-old", accepted=False)
    )
    store.finish(old.batch_id, "done")

    left_open = store.create(workspace="personal", sources=[_pair("sess-open")])
    store.begin(left_open.batch_id)
    store.finish(left_open.batch_id, "done")
    store.forget(left_open.batch_id)
    queued = store.create(workspace="personal", sources=[_pair("sess-open")])

    clock.now = _moment() + timedelta(days=IMPORT_SNAPSHOT_RETENTION_DAYS + 1)
    # Filed now, in the other workspace (personal still has its open batch):
    # the sweep has something recent to keep.
    recent = store.create(workspace="work", sources=[_pair("sess-fresh")])

    removed = store.prune_expired()
    assert removed == 1

    with pytest.raises(ImportStoreError) as excinfo:
        store.get(old.batch_id)
    assert excinfo.value.code == NOT_FOUND
    # The open batch is in-progress work, not a snapshot: it survives.
    assert store.get(queued.batch_id).status == "queued"
    # So does the recent one.
    assert store.get(recent.batch_id).status == "queued"

    kept = store.retained_provenance()
    assert [(row.source_id, row.accepted) for row in kept] == [("sess-old", True)]


def test_prune_writes_nothing_when_nothing_expired(tmp_path: Path) -> None:
    clock = _Clock(_moment())
    store = _store(tmp_path, clock)
    batch = store.create(workspace="personal", sources=[_pair("sess-a")])
    store.begin(batch.batch_id)
    store.finish(batch.batch_id, "done")
    before = store.path.read_bytes()

    assert store.prune_expired() == 0
    assert store.path.read_bytes() == before


# ── a concurrent store instance cannot lose a batch ──────────────────────


def test_two_store_instances_cannot_lose_a_batch(tmp_path: Path) -> None:
    clock = _Clock(_moment())
    failures: list[BaseException] = []

    def _file_five(offset: int) -> None:
        try:
            local = _store(tmp_path, clock)
            for index in range(offset, offset + 5):
                local.create(
                    workspace=f"ws{index}", sources=[_pair(f"sess-{index}")]
                )
        except BaseException as exc:  # pragma: no cover — reported below
            failures.append(exc)

    threads = [
        threading.Thread(target=_file_five, args=(0,)),
        threading.Thread(target=_file_five, args=(5,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    reader = _store(tmp_path, clock)
    for index in range(10):
        batches = reader.list_for_workspace(f"ws{index}")
        assert [batch.workspace for batch in batches] == [f"ws{index}"]
        assert batches[0].sources[0].source_id == f"sess-{index}"


# ── private, atomic writes ───────────────────────────────────────────────


def test_writes_are_private_atomic_and_leave_no_temps(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    store.create(workspace="personal", sources=[_pair("sess-a")])

    assert is_private(store.path)
    assert store.path.parent.as_posix() == (tmp_path / ".runtime" / "import").as_posix()
    leftovers = list(store.path.parent.glob(f".{store.path.name}.*.tmp"))
    assert leftovers == []
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["schema"] == 1
    assert set(document) == {"schema", "batches", "retained_provenance"}


# ── what is never filed ─────────────────────────────────────────────────


def test_a_ciaobot_own_session_is_never_filed(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))

    with pytest.raises(ImportStoreError) as excinfo:
        store.create(
            workspace="personal",
            sources=[_pair("sess-own")],
            known_own_ids={("claude", "sess-own")},
        )
    assert excinfo.value.code == INVALID_BATCH

    # A bare Ciaobot chat id is refused with no registry answer at all.
    with pytest.raises(ImportStoreError) as excinfo:
        store.create(workspace="personal", sources=[_pair("chat-1234abcd")])
    assert excinfo.value.code == INVALID_BATCH


def test_provenance_must_name_an_external_source(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    batch = store.create(workspace="personal", sources=[_pair("sess-a")])
    store.begin(batch.batch_id)

    blank = FactProvenance(
        provider="claude_code", source_id="sess-a", anchor="",
        destination="personal",
    )
    with pytest.raises(ImportStoreError) as excinfo:
        store.record_fact_provenance(batch.batch_id, blank)
    assert excinfo.value.code == INVALID_BATCH

    own = FactProvenance(
        provider="claude_code", source_id="chat-1234abcd", anchor="msg_1",
        destination="personal",
    )
    with pytest.raises(ImportStoreError) as excinfo:
        store.record_fact_provenance(batch.batch_id, own)
    assert excinfo.value.code == INVALID_BATCH


def test_a_selection_is_bounded_and_well_formed(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))

    with pytest.raises(ImportStoreError) as excinfo:
        store.create(workspace="personal", sources=[])
    assert excinfo.value.code == INVALID_BATCH

    with pytest.raises(ImportStoreError) as excinfo:
        store.create(
            workspace="personal",
            sources=[_pair(f"sess-{index}") for index in range(BATCH_CAP + 1)],
        )
    assert excinfo.value.code == INVALID_BATCH

    with pytest.raises(ImportStoreError) as excinfo:
        store.create(workspace="personal", sources=[_pair("sess-a", provider="nope")])
    assert excinfo.value.code == INVALID_BATCH

    with pytest.raises(ImportStoreError) as excinfo:
        store.create(
            workspace="personal",
            sources=[{**_pair("sess-a"), "content_digest": "not-a-digest"}],
        )
    assert excinfo.value.code == INVALID_BATCH


def test_forget_drops_only_the_batch_record(tmp_path: Path) -> None:
    store = _store(tmp_path, _Clock(_moment()))
    batch = store.create(workspace="personal", sources=[_pair("sess-a")])
    store.begin(batch.batch_id)
    store.finish(batch.batch_id, "failed", error="reader refused it")

    store.forget(batch.batch_id)

    with pytest.raises(ImportStoreError) as excinfo:
        store.get(batch.batch_id)
    assert excinfo.value.code == NOT_FOUND
    assert store.list_for_workspace("personal") == []
    with pytest.raises(ImportStoreError) as excinfo:
        store.forget(batch.batch_id)
    assert excinfo.value.code == NOT_FOUND
    # The file itself stays a valid store: forgetting is not a reset.
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema"] == 1


# ── the engine sweep: one path, fail-soft ──────────────────────────────────


def test_engine_store_path_is_the_runtime_import_file(tmp_path: Path) -> None:
    from types import SimpleNamespace

    config = SimpleNamespace(state_path=tmp_path / ".runtime" / "state.json")

    assert engine_store_path(config).as_posix() == (
        tmp_path / ".runtime" / "import" / "import-batches.json"
    ).as_posix()


def test_sweep_prunes_through_the_engine_path_and_keeps_evidence(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    config = SimpleNamespace(state_path=tmp_path / ".runtime" / "state.json")
    old_clock = _Clock(datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC))
    store = ImportStore(engine_store_path(config), clock=old_clock)
    batch = store.create(workspace="personal", sources=[_pair("sess-old")])
    store.begin(batch.batch_id)
    store.record_fact_provenance(batch.batch_id, _provenance("sess-old", accepted=True))
    store.record_fact_provenance(
        batch.batch_id, _provenance("sess-old", accepted=False)
    )
    store.finish(batch.batch_id, "done")

    assert sweep_import_batches(config) == 1

    swept = ImportStore(engine_store_path(config))
    with pytest.raises(ImportStoreError) as excinfo:
        swept.get(batch.batch_id)
    assert excinfo.value.code == NOT_FOUND
    assert [(row.source_id, row.accepted) for row in swept.retained_provenance()] == [
        ("sess-old", True)
    ]


def test_sweep_is_fail_soft_on_a_corrupt_store(tmp_path: Path) -> None:
    from types import SimpleNamespace

    config = SimpleNamespace(state_path=tmp_path / ".runtime" / "state.json")
    path = engine_store_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    # A corrupt batch file must never block engine startup: the sweep logs
    # and answers zero, and the file stays fail-closed for real readers.
    assert sweep_import_batches(config) == 0
    assert path.read_text(encoding="utf-8") == "{ not json"
