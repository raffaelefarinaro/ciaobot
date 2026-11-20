from datetime import UTC, date, datetime, timedelta, timezone
from contextlib import contextmanager
from threading import Event, Thread, current_thread

import pytest

from ciao import curation_run as cr


def test_expired_holder_without_replacement_cannot_finalize_or_change_state(
    tmp_path,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    lease = cr.begin_run(tmp_path, holder="regression", ttl_s=3600, now=now)
    cr.record_done(tmp_path, ["existing"], holder=lease.holder, today=date(2026, 11, 1))
    cr.state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    cr.state_path(tmp_path).write_text(
        cr.state_path(tmp_path).read_text(encoding="utf-8").replace(
            lease.expires_at,
            (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        ),
        encoding="utf-8",
    )
    log = tmp_path / cr.CURATION_LOG_RELATIVE
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("---\nlast_full_pass: 2026-10-01\n---\n\n# Curation log\n", encoding="utf-8")
    before_state = cr.state_path(tmp_path).read_bytes()
    before_log = log.read_bytes()

    with pytest.raises(cr.CurationBusy, match="no longer holds"):
        cr.end_run(
            tmp_path,
            holder=lease.holder,
            status="ok",
            advance_marker=True,
            today=date(2026, 11, 14),
            now=now + timedelta(seconds=2),
        )

    assert cr.state_path(tmp_path).read_bytes() == before_state
    assert log.read_bytes() == before_log


def test_expired_holder_is_rejected_at_end_run_seam(tmp_path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    lease = cr.begin_run(tmp_path, holder="regression", ttl_s=1, now=now)

    # No replacement lease was acquired: expiry alone must end ownership.
    with pytest.raises(cr.CurationBusy, match="no longer holds"):
        cr.end_run(
            tmp_path,
            holder=lease.holder,
            now=now + timedelta(seconds=2),
        )


def test_expiry_at_exact_end_boundary_is_expired(tmp_path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=30, now=now)

    with pytest.raises(cr.CurationBusy):
        cr.end_run(tmp_path, holder=lease.holder, now=now + timedelta(seconds=30))


@pytest.mark.parametrize("ownership", ["missing", "expired", "different"])
def test_end_run_rejects_non_owned_lease_without_mutation(tmp_path, ownership) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    if ownership != "missing":
        lease = cr.begin_run(tmp_path, holder="owner", ttl_s=60, now=now)
        state = cr.load_state(tmp_path)
        if ownership == "expired":
            state.lease["expires_at"] = (now - timedelta(seconds=1)).isoformat()
        else:
            state.lease["holder"] = "replacement"
        cr._store(cr.state_path(tmp_path), state)
        holder = lease.holder
    else:
        holder = "missing-holder"
    log = tmp_path / cr.CURATION_LOG_RELATIVE
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("---\nlast_full_pass: 2026-10-01\n---\n", encoding="utf-8")
    state_path = cr.state_path(tmp_path)
    before_state = state_path.read_bytes() if state_path.exists() else None
    before_log = log.read_bytes()

    with pytest.raises(cr.CurationBusy):
        cr.end_run(tmp_path, holder=holder, now=now)

    assert (state_path.read_bytes() if state_path.exists() else None) == before_state
    assert log.read_bytes() == before_log


@pytest.mark.parametrize("ttl_s", [0, 0.25, 30])
def test_progress_renewal_uses_minimum_and_truncated_ttl(tmp_path, ttl_s) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=3600, now=now)

    cr.record_done(
        tmp_path, ["one"], holder=lease.holder, ttl_s=ttl_s, now=now
    )

    expected_ttl = max(1.0, ttl_s)
    expected = (now + timedelta(seconds=expected_ttl)).isoformat().replace("+00:00", "Z")
    assert cr.load_state(tmp_path).lease["expires_at"] == expected


def test_nonrenewing_progress_keeps_expiry_and_duplicate_count(tmp_path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=3600, now=now)

    assert cr.record_done(
        tmp_path, ["one", "one"], holder=lease.holder, now=now, today=date(2026, 11, 1)
    ) == 1
    expiry = cr.load_state(tmp_path).lease["expires_at"]
    assert cr.record_done(
        tmp_path, ["one"], holder=lease.holder, now=now, today=date(2026, 11, 2)
    ) == 0
    assert cr.load_state(tmp_path).done_keys == {"one": "2026-11-02"}
    assert cr.load_state(tmp_path).lease["expires_at"] == expiry


@pytest.mark.parametrize("holder", ["", "not-the-holder"])
def test_ttl_progress_requires_its_live_holder_without_writing(
    tmp_path, monkeypatch, holder
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    cr.begin_run(tmp_path, holder="owner", ttl_s=60, now=now)
    before = cr.state_path(tmp_path).read_bytes()
    monkeypatch.setattr(cr, "_store", lambda *_: pytest.fail("unexpected state write"))

    with pytest.raises((ValueError, cr.CurationBusy)):
        cr.record_done(tmp_path, ["unverified"], holder=holder, ttl_s=30, now=now)

    assert cr.state_path(tmp_path).read_bytes() == before


def test_default_progress_date_uses_utc_not_explicit_now_timezone(tmp_path) -> None:
    now = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)
    local = now.astimezone(timezone(timedelta(hours=-5)))
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=60, now=now)

    cr.record_done(tmp_path, ["utc-date"], holder=lease.holder, now=local)

    assert cr.load_state(tmp_path).done_keys == {"utc-date": "2026-11-02"}


def test_end_uses_utc_finished_time_and_explicit_marker_date(tmp_path) -> None:
    now = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)
    local = now.astimezone(timezone(timedelta(hours=-5)))
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=60, now=now)
    cr.record_done(tmp_path, sorted(cr.REQUIRED_HYGIENE_KEYS))

    summary = cr.end_run(
        tmp_path, holder=lease.holder, now=local,
        advance_marker=True, today=date(2026, 11, 3),
    )

    assert summary["finished_at"] == now.isoformat().replace("+00:00", "Z")
    assert cr.read_last_full_pass(tmp_path / cr.CURATION_LOG_RELATIVE) == "2026-11-03"


def test_record_done_records_and_renews_in_one_store(tmp_path, monkeypatch) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=3600, now=now)
    original_store = cr._store
    stores = []

    def capture_store(path, state):
        stores.append(state.as_dict())
        original_store(path, state)

    monkeypatch.setattr(cr, "_store", capture_store)
    assert cr.record_done(
        tmp_path, ["one", "one", "two"], holder=lease.holder,
        ttl_s=30, now=now, today=date(2026, 11, 1),
    ) == 2

    assert len(stores) == 1
    assert stores[0]["done_keys"] == {"one": "2026-11-01", "two": "2026-11-01"}
    assert stores[0]["lease"]["expires_at"] == (
        now + timedelta(seconds=30)
    ).isoformat().replace("+00:00", "Z")


def test_repeated_progress_deduplicates_and_shortens_expiry_with_fractional_ttl(
    tmp_path,
) -> None:
    now = datetime(2026, 11, 14, 12, 0, 0, 800000, tzinfo=UTC)
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=3600, now=now)

    assert cr.record_done(
        tmp_path, ["same"], holder=lease.holder, ttl_s=45, now=now
    ) == 1
    first_expiry = cr.load_state(tmp_path).lease["expires_at"]
    assert cr.record_done(
        tmp_path, ["same", "same"], holder=lease.holder, ttl_s=1.7, now=now
    ) == 0

    assert cr.load_state(tmp_path).lease["expires_at"] != first_expiry
    assert cr.load_state(tmp_path).lease["expires_at"] == "2026-11-14T12:00:02Z"


def test_expired_progress_does_not_store_or_change_bytes(tmp_path, monkeypatch) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=3600, now=now)
    before = cr.state_path(tmp_path).read_bytes()
    monkeypatch.setattr(cr, "_store", lambda *_: pytest.fail("unexpected state write"))

    with pytest.raises(cr.CurationBusy):
        cr.record_done(
            tmp_path, ["unverified"], holder=lease.holder,
            ttl_s=30, now=now + timedelta(hours=2),
        )

    assert cr.state_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("replacement", [False, True])
def test_non_ttl_progress_rejects_expired_or_replaced_holder_without_store(
    tmp_path, monkeypatch, replacement
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=60, now=now)
    state = cr.load_state(tmp_path)
    if replacement:
        state.lease["holder"] = "replacement"
    else:
        state.lease["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    cr._store(cr.state_path(tmp_path), state)
    before = cr.state_path(tmp_path).read_bytes()
    monkeypatch.setattr(cr, "_store", lambda *_: pytest.fail("unexpected state write"))

    with pytest.raises(cr.CurationBusy):
        cr.record_done(tmp_path, ["must-not-record"], holder=lease.holder, now=now)

    assert cr.state_path(tmp_path).read_bytes() == before


def test_begin_samples_default_clock_after_lock_acquisition(tmp_path, monkeypatch) -> None:
    from contextlib import contextmanager

    before_lock = datetime.now(UTC).replace(microsecond=0)
    locked_at = before_lock + timedelta(minutes=2)
    clock = [before_lock]

    class LockedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock[0]
            return value if tz else value.replace(tzinfo=None)

    original_lock = cr._state_lock

    @contextmanager
    def lock_after_wait(root):
        with original_lock(root):
            clock[0] = locked_at
            yield

    monkeypatch.setattr(cr, "datetime", LockedClock)
    monkeypatch.setattr(cr, "_state_lock", lock_after_wait)

    lease = cr.begin_run(tmp_path, holder="after-lock", ttl_s=10)

    assert lease.started_at == locked_at.isoformat().replace("+00:00", "Z")


def test_begin_replaces_lease_expired_while_waiting_for_lock(tmp_path, monkeypatch) -> None:
    from contextlib import contextmanager

    before_lock = datetime.now(UTC).replace(microsecond=0)
    locked_at = before_lock + timedelta(minutes=2)
    original_lock = cr._state_lock
    old = cr.Lease(
        holder="old",
        started_at=before_lock.isoformat(),
        expires_at=(before_lock + timedelta(minutes=1)).isoformat(),
        vault_root=tmp_path,
    )
    cr._store(cr.state_path(tmp_path), cr.CurationState(lease=old.as_dict()))

    class LockedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return locked_at if tz else locked_at.replace(tzinfo=None)

    @contextmanager
    def lock_after_wait(root):
        with original_lock(root):
            yield

    monkeypatch.setattr(cr, "datetime", LockedClock)
    monkeypatch.setattr(cr, "_state_lock", lock_after_wait)

    assert cr.begin_run(tmp_path, holder="new").started_at == (
        locked_at.isoformat().replace("+00:00", "Z")
    )


@pytest.mark.parametrize("operation", ["end", "renew"])
def test_expiry_during_lock_wait_is_seen_by_end_and_renew(
    tmp_path, monkeypatch, operation
) -> None:
    from contextlib import contextmanager

    before_lock = datetime.now(UTC).replace(microsecond=0)
    expired_at = before_lock + timedelta(minutes=2)
    clock = [before_lock]
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=60, now=before_lock)
    before_state = cr.state_path(tmp_path).read_bytes()
    original_lock = cr._state_lock

    class WaitingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock[0]
            return value if tz else value.replace(tzinfo=None)

    @contextmanager
    def lock_after_wait(root):
        with original_lock(root):
            clock[0] = expired_at
            yield

    monkeypatch.setattr(cr, "datetime", WaitingClock)
    monkeypatch.setattr(cr, "_state_lock", lock_after_wait)

    if operation == "end":
        with pytest.raises(cr.CurationBusy):
            cr.end_run(tmp_path, holder=lease.holder)
    else:
        assert not cr.renew_run(tmp_path, holder=lease.holder, ttl_s=30)
    assert cr.state_path(tmp_path).read_bytes() == before_state


def test_authorized_end_commit_finishes_after_expiry_before_waiting_acquisition(
    tmp_path, monkeypatch
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    expiry = now + timedelta(seconds=10)
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=10, now=now)
    clock = [now]
    end_at_store = Event()
    begin_waiting = Event()
    original_lock = cr._state_lock
    original_store = cr._store
    result = []
    failures = []

    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock[0]
            return value if tz else value.replace(tzinfo=None)

    @contextmanager
    def observed_lock(root):
        if current_thread().name == "waiting-begin":
            begin_waiting.set()
        with original_lock(root):
            yield

    def pause_end_store(path, state):
        if state.last_run:
            end_at_store.set()
            assert begin_waiting.wait(timeout=5)
            clock[0] = expiry + timedelta(seconds=1)
        original_store(path, state)

    monkeypatch.setattr(cr, "datetime", TestClock)
    monkeypatch.setattr(cr, "_state_lock", observed_lock)
    monkeypatch.setattr(cr, "_store", pause_end_store)

    def finalize():
        try:
            cr.end_run(tmp_path, holder=lease.holder, now=expiry - timedelta(seconds=1))
        except BaseException as exc:
            failures.append(exc)

    def acquire_after_end():
        try:
            assert end_at_store.wait(timeout=5)
            result.append(cr.begin_run(tmp_path, holder="next"))
        except BaseException as exc:
            failures.append(exc)

    ending = Thread(target=finalize, name="authorized-end")
    acquiring = Thread(target=acquire_after_end, name="waiting-begin")
    ending.start()
    acquiring.start()
    ending.join(timeout=5)
    acquiring.join(timeout=5)

    assert not ending.is_alive()
    assert not acquiring.is_alive()
    assert failures == []
    assert len(result) == 1
    assert result[0].started_at == clock[0].isoformat().replace("+00:00", "Z")


def test_expired_lease_cannot_be_revived_by_renewal(tmp_path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    lease = cr.begin_run(tmp_path, holder="owner", ttl_s=60, now=now)
    before = cr.state_path(tmp_path).read_bytes()

    assert not cr.renew_run(
        tmp_path, holder=lease.holder, ttl_s=30, now=now + timedelta(minutes=2)
    )
    assert cr.state_path(tmp_path).read_bytes() == before
