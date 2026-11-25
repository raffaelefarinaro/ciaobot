"""The weekly marker transition for a cycle (v2) state, prepared, published and rotated (#3061 B2).

Every vault lives under tmp_path. Crashes are simulated by raising at one seam
(the prepare store, the publish, the release store) and then entering again
through the ordinary functions, which must settle the prepared transition from
the marker bytes alone. Each publish is counted, because a recovery that
publishes twice is invisible to a green content check.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ciao import curation_cycle as cc
from ciao import curation_logs
from ciao import curation_run as cr
from ciao.curation_run import PASS_RULES, PASS_STALE_NOTE, item_key
from ciao.entity_types import load_entity_types
from ciao.memory_tool import ensure_regions

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)
TODAY = date(2026, 11, 18)
NEXT_WEEK = date(2026, 11, 26)
MARKER_BYTES = b"---\nlast_full_pass: 2026-11-09\n---\n\n# Curation log\n"
TARGET_BYTES = curation_logs.with_full_pass_marker(MARKER_BYTES, "2026-11-18")
PRIOR = cc.marker_identity_of(MARKER_BYTES)
TARGET = cc.marker_identity_of(TARGET_BYTES)
CID = cc.cycle_identity(PRIOR, "")
NEXT_CID = cc.cycle_identity(TARGET, "")
DAILY = item_key(PASS_STALE_NOTE, "People/Ada.md")
LEGACY = item_key(PASS_STALE_NOTE, "People/Old.md")
RULE = item_key(PASS_RULES, "rule-one")
GUIDE = cc.GUIDE_KEY
MERGE = cc.MERGE_KEY
LIVE = frozenset({DAILY, GUIDE, MERGE, RULE})
BOTH_REQUIRED = {cr.HYGIENE_INDEX_KEY: "2026-11-17", cr.HYGIENE_AUDIT_KEY: "2026-11-17"}

# The real writers, captured before any test patches them.
REAL_PUBLISH = curation_logs.publish_log
REAL_STORE = cr._store


def _cycle(**changes: Any) -> cc.CycleState:
    base: dict[str, Any] = {
        "cycle_id": CID,
        "prior_marker": PRIOR,
        "required": dict(BOTH_REQUIRED),
        "optional": {GUIDE: "2026-11-17"},
        "carried": (),
        "legacy_historical": {LEGACY: "2026-11-01"},
        "daily": {DAILY: "2026-11-18"},
        "lease": {},
        "last_run": {},
        "pending": None,
    }
    base.update(changes)
    return cc.CycleState(**base)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    (vault / cr.CURATION_LOG_RELATIVE).write_bytes(MARKER_BYTES)
    return vault


def _put_cycle(vault: Path, cycle: cc.CycleState) -> None:
    cr.state_path(vault).write_text(cc.encode_state(cycle), encoding="utf-8")


def _text(vault: Path) -> str:
    return cr.state_path(vault).read_text(encoding="utf-8")


def _log_path(vault: Path) -> Path:
    return vault / cr.CURATION_LOG_RELATIVE


def _log(vault: Path) -> bytes:
    return _log_path(vault).read_bytes()


def _record_publishes(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Every successful marker publish, in order."""
    published: list[bytes] = []

    def record(path: Path, data: bytes, *, snapshot: Any) -> None:
        REAL_PUBLISH(path, data, snapshot=snapshot)
        published.append(data)

    monkeypatch.setattr(curation_logs, "publish_log", record)
    return published


def _crashing_publish(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Publish attempts that die before anything is written."""
    attempts: list[bytes] = []

    def crash(path: Path, data: bytes, *, snapshot: Any) -> None:
        attempts.append(data)
        raise RuntimeError("power cut before the marker publish")

    monkeypatch.setattr(curation_logs, "publish_log", crash)
    return attempts


def _crash_after_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Any, list[bytes]]:
    """A vault whose run crashed after the transition was prepared, before the publish."""
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle())
    lease = cr.begin_run(vault, holder="run", now=NOW)
    attempts = _crashing_publish(monkeypatch)
    with pytest.raises(RuntimeError):
        cr.end_run(
            vault,
            holder=lease.holder,
            status="ok",
            advance_marker=True,
            live_keys=LIVE,
            today=TODAY,
            now=NOW,
        )
    monkeypatch.setattr(curation_logs, "publish_log", REAL_PUBLISH)
    return vault, lease, attempts


# ── A normal weekly end: publish once, rotate, release in one store ──────


def test_end_run_publishes_the_marker_once_and_rotates_the_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle(legacy_historical={LEGACY: "2026-11-01"}))
    published = _record_publishes(monkeypatch)
    lease = cr.begin_run(vault, holder="run", now=NOW)

    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, live_keys=LIVE, today=TODAY, now=NOW
    )

    assert summary["status"] == "ok"
    assert summary["full_pass_advanced"] is True
    assert published == [TARGET_BYTES]
    assert _log(vault) == TARGET_BYTES
    text = _text(vault)
    assert "committed" not in text and "phase" not in text
    decoded = cc.decode_state(text)
    assert decoded.cycle_id == NEXT_CID
    assert decoded.prior_marker == TARGET
    assert decoded.required == {}
    assert decoded.optional == {}
    assert decoded.daily == {DAILY: "2026-11-18"}
    assert decoded.legacy_historical == {LEGACY: "2026-11-01"}
    assert decoded.carried == tuple(sorted((MERGE, RULE)))
    assert decoded.pending is None
    assert decoded.lease == {}
    assert decoded.last_run["status"] == "ok"
    assert decoded.last_run["finished_at"] == "2026-11-18T12:00:00Z"


def test_a_missing_hygiene_proof_records_the_run_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle(required={cr.HYGIENE_INDEX_KEY: "2026-11-17"}))
    published = _record_publishes(monkeypatch)
    lease = cr.begin_run(vault, holder="run", now=NOW)

    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, live_keys=LIVE, today=TODAY, now=NOW
    )

    assert summary["status"] == "ok"
    assert summary["full_pass_advanced"] is False
    assert published == []
    assert _log(vault) == MARKER_BYTES
    decoded = cc.decode_state(_text(vault))
    assert decoded.cycle_id == CID
    assert decoded.pending is None
    assert decoded.last_run["status"] == "ok"


def test_a_v1_marker_advance_keeps_the_v1_bytes_it_always_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    v1 = {
        "version": 1,
        "done_keys": {**BOTH_REQUIRED, DAILY: "2026-11-18"},
        "lease": {},
        "last_run": {},
    }
    cr.state_path(vault).write_text(json.dumps(v1, indent=2) + "\n", encoding="utf-8")
    lease = cr.begin_run(vault, holder="run", now=NOW)

    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, live_keys={DAILY}, today=TODAY, now=NOW
    )

    assert summary["full_pass_advanced"] is True
    assert _log(vault) == TARGET_BYTES
    expected = {
        "version": 1,
        "done_keys": {DAILY: "2026-11-18"},
        "lease": {},
        "last_run": {
            "status": "ok",
            "finished_at": "2026-11-18T12:00:00Z",
            "planned": 0,
            "completed": 0,
            "deferred": 0,
            "reasons": [],
        },
    }
    assert _text(vault) == json.dumps(expected, ensure_ascii=False, indent=2) + "\n"


def test_the_marker_is_published_while_the_state_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle())
    lease = cr.begin_run(vault, holder="run", now=NOW)
    real_lock = cr._state_lock
    depth = {"now": 0}
    depths: list[int] = []

    @contextlib.contextmanager
    def counting(vault_root: Path):
        depth["now"] += 1
        try:
            with real_lock(vault_root):
                yield
        finally:
            depth["now"] -= 1

    def observe(path: Path, data: bytes, *, snapshot: Any) -> None:
        depths.append(depth["now"])
        REAL_PUBLISH(path, data, snapshot=snapshot)

    monkeypatch.setattr(cr, "_state_lock", counting)
    monkeypatch.setattr(curation_logs, "publish_log", observe)

    cr.end_run(vault, holder=lease.holder, status="ok", advance_marker=True, live_keys=LIVE, today=TODAY, now=NOW)

    assert depths == [1]


# ── Refusals before anything is published ─────────────────────────────────


def test_a_refused_prepare_publishes_nothing_and_rolls_the_transition_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle())
    lease = cr.begin_run(vault, holder="run", now=NOW)
    published = _record_publishes(monkeypatch)

    def refuse_the_prepare(path: Path, state: cr.CurationState) -> None:
        if state.cycle is not None and state.cycle.pending is not None:
            raise cr.CurationStateError("disk full")
        REAL_STORE(path, state)

    monkeypatch.setattr(cr, "_store", refuse_the_prepare)

    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, live_keys=LIVE, today=TODAY, now=NOW
    )

    assert summary["status"] == "failed"
    assert summary["full_pass_advanced"] is False
    assert "disk full" in summary["marker_error"]
    assert published == []
    assert _log(vault) == MARKER_BYTES
    decoded = cc.decode_state(_text(vault))
    assert decoded.cycle_id == CID
    assert decoded.pending is None
    assert decoded.lease == {}


def test_a_refused_publish_rolls_the_transition_back_and_records_a_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle())
    lease = cr.begin_run(vault, holder="run", now=NOW)

    def refuse(path: Path, data: bytes, *, snapshot: Any) -> None:
        raise curation_logs.CurationLogError("the log changed while it was read")

    monkeypatch.setattr(curation_logs, "publish_log", refuse)

    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, live_keys=LIVE, today=TODAY, now=NOW
    )

    assert summary["status"] == "failed"
    assert summary["full_pass_advanced"] is False
    assert _log(vault) == MARKER_BYTES
    decoded = cc.decode_state(_text(vault))
    assert decoded.pending is None
    assert decoded.cycle_id == CID


# ── Crash matrix: every seam settles from the marker bytes ───────────────


def test_a_crash_before_the_publish_is_resumed_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease, attempts = _crash_after_prepare(tmp_path, monkeypatch)

    assert len(attempts) == 1
    assert _log(vault) == MARKER_BYTES
    prepared = cc.decode_state(_text(vault)).pending
    assert prepared is not None
    assert prepared.prior == PRIOR
    assert prepared.target_sha256 == TARGET.sha256
    assert prepared.proven_required == tuple(sorted(BOTH_REQUIRED))
    published = _record_publishes(monkeypatch)

    summary = cr.end_run(vault, holder=lease.holder, status="ok", live_keys=LIVE, today=TODAY, now=NOW)

    assert summary["full_pass_advanced"] is True
    assert published == [TARGET_BYTES]
    assert _log(vault) == TARGET_BYTES
    decoded = cc.decode_state(_text(vault))
    assert decoded.pending is None
    assert decoded.cycle_id == NEXT_CID
    assert decoded.lease == {}


def test_a_live_crashed_lease_is_not_taken_over_by_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease, _ = _crash_after_prepare(tmp_path, monkeypatch)
    before = _text(vault)
    published = _record_publishes(monkeypatch)

    with pytest.raises(cr.CurationBusy):
        cr.begin_run(vault, holder="other", now=NOW)

    assert published == []
    assert _text(vault) == before
    assert _log(vault) == MARKER_BYTES


def test_recovery_after_the_lease_expires_settles_and_the_next_run_takes_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _, _ = _crash_after_prepare(tmp_path, monkeypatch)
    published = _record_publishes(monkeypatch)

    lease = cr.begin_run(vault, holder="next", now=LATER)

    assert published == [TARGET_BYTES]
    decoded = cc.decode_state(_text(vault))
    assert decoded.pending is None
    assert decoded.cycle_id == NEXT_CID
    assert decoded.lease["holder"] == lease.holder


def test_a_crash_after_the_publish_is_committed_without_a_second_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle())
    lease = cr.begin_run(vault, holder="run", now=NOW)
    published = _record_publishes(monkeypatch)

    def crash_on_release(path: Path, state: cr.CurationState) -> None:
        if not state.lease:
            raise RuntimeError("power cut after the marker publish")
        REAL_STORE(path, state)

    monkeypatch.setattr(cr, "_store", crash_on_release)
    with pytest.raises(RuntimeError):
        cr.end_run(
            vault, holder=lease.holder, status="ok", advance_marker=True, live_keys=LIVE, today=TODAY, now=NOW
        )
    monkeypatch.setattr(cr, "_store", REAL_STORE)
    assert published == [TARGET_BYTES]
    assert _log(vault) == TARGET_BYTES
    assert cc.decode_state(_text(vault)).pending is not None

    summary = cr.end_run(vault, holder=lease.holder, status="ok", live_keys=LIVE, today=TODAY, now=NOW)

    assert summary["full_pass_advanced"] is True
    assert published == [TARGET_BYTES]
    assert _log(vault) == TARGET_BYTES
    decoded = cc.decode_state(_text(vault))
    assert decoded.pending is None
    assert decoded.cycle_id == NEXT_CID
    assert decoded.lease == {}


def test_a_resume_that_fails_again_leaves_the_transition_prepared_without_a_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease, attempts = _crash_after_prepare(tmp_path, monkeypatch)
    before = _text(vault)
    refusals: list[bytes] = []

    def refuse(path: Path, data: bytes, *, snapshot: Any) -> None:
        refusals.append(data)
        raise curation_logs.CurationLogError("the log is not a plain file")

    monkeypatch.setattr(curation_logs, "publish_log", refuse)

    with pytest.raises(cr.CurationStateError, match="cannot be settled automatically"):
        cr.end_run(vault, holder=lease.holder, status="ok", live_keys=LIVE, today=TODAY, now=NOW)

    assert len(attempts) == 1
    assert len(refusals) == 1
    assert _text(vault) == before
    assert _log(vault) == MARKER_BYTES


# ── Drift and unreadable bytes block every writer, and write nothing ──────


def _drift_log(vault: Path, kind: str) -> None:
    if kind == "appended":
        (vault / cr.CURATION_LOG_RELATIVE).write_bytes(MARKER_BYTES + b"\nlate report\n")
    elif kind == "truncated":
        (vault / cr.CURATION_LOG_RELATIVE).write_bytes(b"")
    else:
        target = vault.parent / "elsewhere.md"
        target.write_bytes(MARKER_BYTES)
        (vault / cr.CURATION_LOG_RELATIVE).unlink()
        (vault / cr.CURATION_LOG_RELATIVE).symlink_to(target)


@pytest.mark.parametrize("kind", ["appended", "truncated", "symlinked"])
def test_drifted_or_unreadable_bytes_block_every_writer_and_write_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    vault, lease, _ = _crash_after_prepare(tmp_path, monkeypatch)
    _drift_log(vault, kind)
    before_state = _text(vault)
    before_log = _log(vault) if kind != "symlinked" else None

    with pytest.raises(cr.CurationStateError):
        cr.end_run(vault, holder=lease.holder, status="ok", live_keys=LIVE, today=TODAY, now=NOW)
    with pytest.raises(cr.CurationStateError):
        cr.record_done(vault, [DAILY], holder=lease.holder, now=NOW)
    with pytest.raises(cr.CurationStateError):
        cr.renew_run(vault, holder=lease.holder, now=NOW)
    with pytest.raises(cr.CurationStateError):
        cr.append_log(vault, holder=lease.holder, log="curation", payload=b"report\n", now=NOW)
    with pytest.raises(cr.CurationStateError):
        cr.begin_run(vault, holder="next", now=LATER)
    with cr.authorize_holder(vault, holder=lease.holder, clock=lambda: NOW) as allowed:
        assert allowed is False

    assert _text(vault) == before_state
    if before_log is not None:
        assert _log(vault) == before_log


def test_authorize_holder_settles_a_resumable_transition_and_yields_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease, _ = _crash_after_prepare(tmp_path, monkeypatch)
    published = _record_publishes(monkeypatch)

    with cr.authorize_holder(vault, holder=lease.holder, clock=lambda: NOW) as allowed:
        assert allowed is True

    assert published == [TARGET_BYTES]
    decoded = cc.decode_state(_text(vault))
    assert decoded.pending is None
    assert decoded.cycle_id == NEXT_CID
    assert decoded.lease["holder"] == lease.holder


def test_append_log_is_refused_while_a_transition_is_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease, _ = _crash_after_prepare(tmp_path, monkeypatch)
    before_state = _text(vault)

    with pytest.raises(cr.CurationStateError, match="interrupted"):
        cr.append_log(vault, holder=lease.holder, log="curation", payload=b"report\n", now=NOW)

    assert _text(vault) == before_state
    assert _log(vault) == MARKER_BYTES


# ── Expiry before the publish, and the id of the running cycle ───────────


def test_a_lease_that_expires_before_the_publish_publishes_nothing_and_stays_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle())
    lease = cr.begin_run(vault, holder="run", now=NOW)
    published = _record_publishes(monkeypatch)
    calls = {"n": 0}

    def clock() -> datetime:
        calls["n"] += 1
        return NOW if calls["n"] == 1 else LATER

    with pytest.raises(cr.CurationBusy):
        cr.end_run(
            vault,
            holder=lease.holder,
            status="ok",
            advance_marker=True,
            live_keys=LIVE,
            today=TODAY,
            clock=clock,
        )

    assert published == []
    assert _log(vault) == MARKER_BYTES
    assert cc.decode_state(_text(vault)).pending is not None


def test_the_cycle_id_survives_an_iso_week_boundary_until_the_marker_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle())
    published = _record_publishes(monkeypatch)
    lease = cr.begin_run(vault, holder="run", now=NOW)
    cr.end_run(vault, holder=lease.holder, status="ok", planned=1, now=NOW)
    assert cc.decode_state(_text(vault)).cycle_id == CID

    later = NOW + timedelta(days=7)
    lease = cr.begin_run(vault, holder="run", now=later)
    cr.end_run(vault, holder=lease.holder, status="ok", planned=1, now=later)
    assert cc.decode_state(_text(vault)).cycle_id == CID
    assert published == []

    lease = cr.begin_run(vault, holder="run", now=later)
    cr.record_done(vault, list(BOTH_REQUIRED), holder=lease.holder, today=NEXT_WEEK, now=later)
    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, today=NEXT_WEEK, now=later
    )

    assert summary["full_pass_advanced"] is True
    rotated = cc.decode_state(_text(vault))
    assert rotated.cycle_id == cc.cycle_identity(cc.marker_identity_of(_log(vault)), "")
    assert rotated.cycle_id != CID


# ── The next week plans the hygiene pair again, through the real builder ─


def test_the_worklist_plans_the_hygiene_pair_again_after_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease, _ = _crash_after_prepare(tmp_path, monkeypatch)
    cr.begin_run(vault, holder="next", now=LATER)
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Workspace\n", encoding="utf-8", newline="")
    ensure_regions(guide)

    def planned(today: date) -> set[str]:
        worklist = cr.build_worklist(
            vault_root=vault,
            workspace=vault.name,
            guide_path=guide,
            category_registry=load_entity_types(vault),
            workspace_dir=tmp_path,
            today=today,
            done_keys=frozenset(cr.load_state(vault).done_keys),
        )
        return {key for item in worklist.items for key in item.keys}

    assert cr.load_state(vault).cycle is not None
    assert cr.load_state(vault).cycle.pending is None
    assert set(BOTH_REQUIRED) <= planned(NEXT_WEEK)


# ── Carried work survives the rotation; done work does not ───────────────


def test_optional_work_left_outstanding_stays_outstanding_after_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle())
    lease = cr.begin_run(vault, holder="run", now=NOW)

    cr.end_run(vault, holder=lease.holder, status="ok", advance_marker=True, live_keys=LIVE, today=TODAY, now=NOW)

    decoded = cc.decode_state(_text(vault))
    projection = cc.project_obligations([], decoded)
    assert projection.optional_outstanding == tuple(sorted((MERGE, RULE)))
    assert GUIDE not in projection.optional_outstanding


# ── Pins and guards from the security review of d42476765 ────────────────


def test_an_expired_preparer_is_published_once_and_the_entrant_keeps_its_own_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holder A prepares, its lease lapses unpublished, B's begin_run settles and takes B's lease."""
    vault, lease_a, _ = _crash_after_prepare(tmp_path, monkeypatch)
    published = _record_publishes(monkeypatch)

    lease_b = cr.begin_run(vault, holder="entrant", now=LATER)

    assert published == [TARGET_BYTES]
    assert _log(vault) == TARGET_BYTES
    state = cr.load_state(vault)
    assert state.lease == {**lease_b.as_dict(), "proof_hash": lease_b.proof_hash}
    assert state.lease["holder"] != lease_a.holder
    assert state.lease["expires_at"] == "2026-11-18T14:30:00Z"
    assert cc.decode_state(_text(vault)).pending is None
    cr.end_run(vault, holder=lease_b.holder, status="ok", now=LATER)
    assert published == [TARGET_BYTES]


def test_a_lease_that_lapses_between_settle_entry_and_the_publish_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease, _ = _crash_after_prepare(tmp_path, monkeypatch)
    before = _text(vault)
    published = _record_publishes(monkeypatch)
    ticks = iter([NOW, NOW, LATER, LATER, LATER])

    with pytest.raises(cr.CurationBusy):
        cr.end_run(vault, holder=lease.holder, status="ok", clock=lambda: next(ticks))

    assert published == []
    assert _log(vault) == MARKER_BYTES
    assert _text(vault) == before
    assert cc.decode_state(_text(vault)).pending is not None


def test_a_foreign_live_lease_is_never_settled_stolen_or_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease_a, _ = _crash_after_prepare(tmp_path, monkeypatch)
    before = _text(vault)
    published = _record_publishes(monkeypatch)

    with pytest.raises(cr.CurationBusy):
        cr.record_done(vault, [DAILY], holder="foreign", now=NOW)
    with pytest.raises(cr.CurationBusy):
        cr.record_done(vault, [DAILY], now=NOW)
    assert cr.renew_run(vault, holder="foreign", now=NOW) is False

    assert published == []
    assert _log(vault) == MARKER_BYTES
    assert _text(vault) == before
    assert cr.load_state(vault).lease["holder"] == lease_a.holder


def test_a_log_that_changes_between_prepare_and_publish_is_refused_and_nothing_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _put_cycle(vault, _cycle())
    lease = cr.begin_run(vault, holder="run", now=NOW)
    published = _record_publishes(monkeypatch)
    late = MARKER_BYTES + b"\nlate report\n"

    def drift_after_prepare(path: Path, state: cr.CurationState) -> None:
        REAL_STORE(path, state)
        if state.cycle is not None and state.cycle.pending is not None:
            _log_path(vault).write_bytes(late)

    monkeypatch.setattr(cr, "_store", drift_after_prepare)

    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, live_keys=LIVE, today=TODAY, now=NOW
    )

    assert summary["status"] == "failed"
    assert summary["full_pass_advanced"] is False
    assert published == []
    assert _log(vault) == late
    decoded = cc.decode_state(_text(vault))
    assert decoded.pending is None
    assert decoded.cycle_id == CID


def test_a_resume_whose_stamp_does_not_match_the_prepared_digest_blocks_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _, _ = _crash_after_prepare(tmp_path, monkeypatch)
    document = json.loads(_text(vault))
    document["pending"]["target_stamp"] = "2026-11-19"
    cr.state_path(vault).write_text(json.dumps(document), encoding="utf-8")
    before = _text(vault)
    published = _record_publishes(monkeypatch)

    with pytest.raises(cr.CurationStateError, match="cannot be settled automatically"):
        cr.begin_run(vault, holder="next", now=LATER)

    assert published == []
    assert _log(vault) == MARKER_BYTES
    assert _text(vault) == before


def test_append_while_prepared_is_refused_for_the_curation_log_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease, _ = _crash_after_prepare(tmp_path, monkeypatch)
    before = _text(vault)

    with pytest.raises(cr.CurationStateError, match="interrupted"):
        cr.append_log(vault, holder=lease.holder, log="curation", payload=b"report\n", now=NOW)
    result = cr.append_log(vault, holder=lease.holder, log="weekly-review", payload=b"review\n", now=NOW)

    assert result["status"] == "appended"
    assert (vault / cr.LOG_SELECTORS["weekly-review"]).read_bytes().endswith(b"review\n")
    assert _log(vault) == MARKER_BYTES
    assert _text(vault) == before


def test_an_entry_that_only_settled_an_old_transition_reports_the_marker_advanced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, lease, _ = _crash_after_prepare(tmp_path, monkeypatch)
    _record_publishes(monkeypatch)

    summary = cr.end_run(vault, holder=lease.holder, status="ok", advance_marker=False, now=NOW)

    assert summary["full_pass_advanced"] is True
    assert summary["marker_error"] == ""
    assert summary["status"] == "ok"
