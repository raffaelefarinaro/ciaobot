"""The v2 facade over the curation state, and the operator-run v1 to v2 migration (#3061 B1).

Every test builds its vault under tmp_path. Nothing here touches a real vault,
the live engine or a LaunchAgent. A v1 file keeps its exact bytes through the
lease and progress functions; a v2 file is read through the same functions the
nightly run uses; migration is preview-then-apply and refuses anything it
cannot prove.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ciao import cli
from ciao import curation_cycle as cc
from ciao import curation_run as cr
from ciao.curation_run import PASS_RULES, PASS_STALE_NOTE, item_key
from ciao.vault_index import scan_vault

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 18)
MARKER_BYTES = b"---\nlast_full_pass: 2026-11-09\n---\n\n# Curation log\n"
PRIOR = cc.marker_identity_of(MARKER_BYTES)
CID = cc.cycle_identity(PRIOR, "")
DAILY = item_key(PASS_STALE_NOTE, "People/Ada.md")
DAILY_NEW = item_key(PASS_STALE_NOTE, "People/Bob.md")
FUTURE = item_key(PASS_STALE_NOTE, "People/Future.md")
RULE = item_key(PASS_RULES, "rule-one")
BACKUP_NAME = "Curation-State.v1.bak"
EXPIRED_LEASE = {
    "holder": "old run",
    "started_at": "2026-11-01T00:00:00Z",
    "expires_at": "2026-11-01T00:30:00Z",
    "extension": "kept",
}
V1_LAST_RUN = {
    "status": "ok",
    "finished_at": "2026-11-16T03:00:00Z",
    "planned": 3,
    "completed": 2,
    "deferred": 1,
    "reasons": ["budget"],
}


def _cycle(**changes: Any) -> cc.CycleState:
    base: dict[str, Any] = {
        "cycle_id": CID,
        "prior_marker": PRIOR,
        "required": {cr.HYGIENE_INDEX_KEY: "2026-11-17"},
        "optional": {cc.GUIDE_KEY: "2026-11-17"},
        "carried": (),
        "legacy_historical": {cr.HYGIENE_AUDIT_KEY: "2026-11-01"},
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
    return vault


def _put_state(vault: Path, body: str | dict[str, Any]) -> bytes:
    text = body if isinstance(body, str) else json.dumps(body, indent=2) + "\n"
    path = cr.state_path(vault)
    path.write_text(text, encoding="utf-8")
    return path.read_bytes()


def _put_log(vault: Path, data: bytes = MARKER_BYTES) -> None:
    (vault / cr.CURATION_LOG_RELATIVE).write_bytes(data)


def _v1(done: dict[str, str] | None = None, lease: dict[str, Any] | None = None,
        last_run: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "done_keys": done or {},
        "lease": lease or {},
        "last_run": last_run or {},
    }


def _migratable(tmp_path: Path) -> tuple[Path, bytes]:
    """A v1 vault with every kind of key the migration classifies, and an expired lease."""
    vault = _vault(tmp_path)
    _put_log(vault)
    before = _put_state(
        vault,
        _v1(
            done={
                DAILY: "2026-11-17",
                FUTURE: "2026-11-20",
                cr.HYGIENE_INDEX_KEY: "2026-11-16",
                cc.GUIDE_KEY: "2026-11-08",
            },
            lease=dict(EXPIRED_LEASE),
            last_run=dict(V1_LAST_RUN),
        ),
    )
    return vault, before


def _operator_at_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this test process look like an operator's terminal to the --apply gate."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    }


@pytest.fixture
def publish_depths(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The state-lock depth at every state publish, observed without changing behaviour."""
    real_lock = cr._state_lock
    depth = {"now": 0}
    observed: list[int] = []

    @contextlib.contextmanager
    def counting(vault_root: Path):
        depth["now"] += 1
        try:
            with real_lock(vault_root):
                yield
        finally:
            depth["now"] -= 1

    real_publish = cr._publish_bytes

    def publish(path: Path, data: bytes) -> None:
        observed.append(depth["now"])
        real_publish(path, data)

    monkeypatch.setattr(cr, "_state_lock", counting)
    monkeypatch.setattr(cr, "_publish_bytes", publish)
    return observed


# ── Facade: a v2 file is read and written through the v1 consumers ───────


def test_v2_document_loads_with_the_three_current_maps_as_done_keys(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _put_state(vault, cc.encode_state(_cycle()))

    state = cr.load_state(vault)

    assert state.cycle == _cycle()
    assert state.done_keys == {
        cr.HYGIENE_INDEX_KEY: "2026-11-17",
        cc.GUIDE_KEY: "2026-11-17",
        DAILY: "2026-11-18",
    }
    assert cr.HYGIENE_AUDIT_KEY not in state.done_keys
    assert state.lease == {}
    assert state.last_run == {}
    with pytest.raises(RuntimeError):
        state.as_dict()


def test_progress_functions_round_trip_a_v2_file_and_keep_the_cycle_fields(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    cycle = _cycle(carried=(RULE,), pending=None)
    _put_state(vault, cc.encode_state(cycle))

    lease = cr.begin_run(vault, holder="run", now=NOW)
    assert cr.active_lease(vault, now=NOW)["holder"] == lease.holder
    assert cr.unstarted_run_reason(vault, NOW - timedelta(minutes=5), now=NOW) == ""
    assert cr.renew_run(vault, holder=lease.holder, now=NOW)
    added = cr.record_done(
        vault,
        [cr.HYGIENE_AUDIT_KEY, cc.MERGE_KEY, DAILY_NEW],
        holder=lease.holder,
        today=TODAY,
        now=NOW,
    )
    assert added == 3
    summary = cr.end_run(
        vault,
        holder=lease.holder,
        status="ok",
        planned=3,
        completed=3,
        live_keys=frozenset(
            {cr.HYGIENE_INDEX_KEY, cr.HYGIENE_AUDIT_KEY, cc.GUIDE_KEY, cc.MERGE_KEY, DAILY_NEW}
        ),
        now=NOW,
        today=TODAY,
    )

    assert summary["full_pass_advanced"] is False
    decoded = cc.decode_state(cr.state_path(vault).read_text(encoding="utf-8"))
    assert decoded.cycle_id == cycle.cycle_id
    assert decoded.prior_marker == cycle.prior_marker
    assert decoded.carried == (RULE,)
    assert decoded.legacy_historical == cycle.legacy_historical
    assert decoded.pending is None
    assert decoded.required == {cr.HYGIENE_INDEX_KEY: "2026-11-17", cr.HYGIENE_AUDIT_KEY: "2026-11-18"}
    assert decoded.optional == {cc.GUIDE_KEY: "2026-11-17", cc.MERGE_KEY: "2026-11-18"}
    assert decoded.daily == {DAILY_NEW: "2026-11-18"}
    assert decoded.lease == {}
    assert decoded.last_run["status"] == "ok"
    assert decoded.last_run["planned"] == 3


def test_end_run_marker_advance_on_v2_fails_closed_and_publishes_no_marker(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _put_log(vault)
    _put_state(
        vault,
        cc.encode_state(
            _cycle(required={cr.HYGIENE_INDEX_KEY: "2026-11-17", cr.HYGIENE_AUDIT_KEY: "2026-11-17"})
        ),
    )
    before_log = (vault / cr.CURATION_LOG_RELATIVE).read_bytes()
    lease = cr.begin_run(vault, holder="run", now=NOW)

    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, today=TODAY, now=NOW
    )

    assert summary["status"] == "failed"
    assert summary["full_pass_advanced"] is False
    assert "not available" in summary["marker_error"]
    assert (vault / cr.CURATION_LOG_RELATIVE).read_bytes() == before_log
    assert cr.load_state(vault).lease == {}


def test_advance_full_pass_refuses_a_v2_state_and_changes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _put_log(vault)
    before = _put_state(
        vault,
        cc.encode_state(
            _cycle(required={cr.HYGIENE_INDEX_KEY: "2026-11-17", cr.HYGIENE_AUDIT_KEY: "2026-11-17"})
        ),
    )
    before_log = (vault / cr.CURATION_LOG_RELATIVE).read_bytes()

    with pytest.raises(cr.CurationStateError):
        cr.advance_full_pass(vault, today=TODAY)

    assert cr.state_path(vault).read_bytes() == before
    assert (vault / cr.CURATION_LOG_RELATIVE).read_bytes() == before_log


def test_scan_vault_ignores_the_migration_backup(tmp_path: Path) -> None:
    vault, before = _migratable(tmp_path)
    (vault / "People").mkdir()
    (vault / "People" / "Ada.md").write_text("---\ntype: person\n---\n\n# Ada\n", encoding="utf-8")
    backup = vault / "Workspace" / BACKUP_NAME
    backup.write_bytes(before)

    paths = {str(entry.path) for entry in scan_vault(vault, workspace="work")}

    assert not any("Curation-State" in path for path in paths)
    assert any(path.endswith("Ada.md") for path in paths)


def test_v1_state_bytes_are_unchanged_through_begin_renew_record_and_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden: the v1 writer is byte-for-byte what it was before the v2 facade existed."""
    monkeypatch.setattr(cr.uuid, "uuid4", lambda: uuid.UUID(int=1))
    vault = _vault(tmp_path)
    path = cr.state_path(vault)
    path.write_text(
        json.dumps({"version": 1, "done_keys": {"2026-01-01-x": "2025-12-30"}, "lease": {}, "last_run": {}}),
        encoding="utf-8",
    )

    golden_now = datetime(2026, 1, 1, tzinfo=UTC)
    lease = cr.begin_run(vault, holder="golden", now=golden_now)
    assert path.read_text(encoding="utf-8") == GOLDEN_BEGIN
    cr.renew_run(vault, holder=lease.holder, now=golden_now)
    assert path.read_text(encoding="utf-8") == GOLDEN_BEGIN
    cr.record_done(vault, ["pass:one", cr.HYGIENE_INDEX_KEY], holder=lease.holder, now=golden_now)
    assert path.read_text(encoding="utf-8") == GOLDEN_RECORDED
    cr.end_run(
        vault,
        holder=lease.holder,
        status="ok",
        planned=2,
        completed=1,
        deferred=1,
        reasons=["budget"],
        live_keys=frozenset({"pass:one"}),
        now=golden_now,
    )
    assert path.read_text(encoding="utf-8") == GOLDEN_ENDED


GOLDEN_BEGIN = (
    "{\n"
    '  "version": 1,\n'
    '  "done_keys": {\n'
    '    "2026-01-01-x": "2025-12-30"\n'
    "  },\n"
    '  "lease": {\n'
    '    "holder": "golden:00000000-0000-0000-0000-000000000001",\n'
    '    "started_at": "2026-01-01T00:00:00Z",\n'
    '    "expires_at": "2026-01-01T00:30:00Z"\n'
    "  },\n"
    '  "last_run": {}\n'
    "}\n"
)
GOLDEN_RECORDED = (
    "{\n"
    '  "version": 1,\n'
    '  "done_keys": {\n'
    '    "2026-01-01-x": "2025-12-30",\n'
    '    "pass:one": "2026-01-01",\n'
    '    "hygiene:vault-index": "2026-01-01"\n'
    "  },\n"
    '  "lease": {\n'
    '    "holder": "golden:00000000-0000-0000-0000-000000000001",\n'
    '    "started_at": "2026-01-01T00:00:00Z",\n'
    '    "expires_at": "2026-01-01T00:30:00Z"\n'
    "  },\n"
    '  "last_run": {}\n'
    "}\n"
)
GOLDEN_ENDED = (
    "{\n"
    '  "version": 1,\n'
    '  "done_keys": {\n'
    '    "pass:one": "2026-01-01"\n'
    "  },\n"
    '  "lease": {},\n'
    '  "last_run": {\n'
    '    "status": "ok",\n'
    '    "finished_at": "2026-01-01T00:00:00Z",\n'
    '    "planned": 2,\n'
    '    "completed": 1,\n'
    '    "deferred": 1,\n'
    '    "reasons": [\n'
    '      "budget"\n'
    "    ]\n"
    "  }\n"
    "}\n"
)


# ── Migration: preview ───────────────────────────────────────────────────


def test_report_describes_a_migratable_v1_without_writing(tmp_path: Path) -> None:
    vault, before = _migratable(tmp_path)

    report = cr.migration_report(vault, today=TODAY)

    assert report["status"] == "v1"
    assert report["blockers"] == []
    assert report["marker"] == "present"
    assert report["last_full_pass"] == "2026-11-09"
    assert report["daily"] == 1
    assert report["weekly_historical"] == 2
    assert report["ambiguous"] == [cr.HYGIENE_INDEX_KEY]
    assert report["dropped"] == [FUTURE]
    assert report["live_lease"] is None
    assert Path(report["backup_path"]).name == BACKUP_NAME
    assert cr.state_path(vault).read_bytes() == before


def test_preview_creates_no_files_anywhere_in_the_vault(tmp_path: Path) -> None:
    """The preview takes the state lock, whose lock file lives outside the vault by design."""
    vault, _ = _migratable(tmp_path)
    before = _tree(vault)

    cr.migration_report(vault, today=TODAY)

    assert _tree(vault) == before
    assert not (vault / "Workspace" / BACKUP_NAME).exists()


def test_digest_changes_when_the_log_or_the_state_changes(tmp_path: Path) -> None:
    vault, before = _migratable(tmp_path)
    original = cr.migration_report(vault, today=TODAY)["digest"]

    log = vault / cr.CURATION_LOG_RELATIVE
    log.write_bytes(MARKER_BYTES + b"\nnew report\n")
    after_log = cr.migration_report(vault, today=TODAY)["digest"]
    log.write_bytes(MARKER_BYTES)

    _put_state(vault, _v1(done={DAILY: "2026-11-17"}, last_run=dict(V1_LAST_RUN)))
    after_state = cr.migration_report(vault, today=TODAY)["digest"]

    assert len({original, after_log, after_state}) == 3


# ── Migration: apply, success path ───────────────────────────────────────


def test_apply_writes_a_v2_document_that_decodes_and_keeps_daily_and_last_run(tmp_path: Path) -> None:
    vault, _ = _migratable(tmp_path)
    digest = cr.migration_report(vault, today=TODAY)["digest"]

    report = cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest=digest, today=TODAY)

    assert report["status"] == "migrated"
    decoded = cc.decode_state(cr.state_path(vault).read_text(encoding="utf-8"))
    assert decoded.cycle_id == CID
    assert decoded.prior_marker == PRIOR
    assert decoded.daily == {DAILY: "2026-11-17"}
    assert decoded.legacy_historical == {cr.HYGIENE_INDEX_KEY: "2026-11-16", cc.GUIDE_KEY: "2026-11-08"}
    assert decoded.required == {} and decoded.optional == {} and decoded.carried == ()
    assert decoded.lease == {}
    assert decoded.last_run == V1_LAST_RUN
    assert decoded.pending is None
    assert cr.load_state(vault).done_keys == {DAILY: "2026-11-17"}


def test_backup_is_the_exact_pre_migration_bytes_with_private_mode(tmp_path: Path) -> None:
    vault, before = _migratable(tmp_path)
    digest = cr.migration_report(vault, today=TODAY)["digest"]

    cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest=digest, today=TODAY)

    backup = vault / "Workspace" / BACKUP_NAME
    assert backup.read_bytes() == before
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_apply_is_deterministic_across_done_key_insertion_order(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    keys = [DAILY, FUTURE, cr.HYGIENE_INDEX_KEY, cc.GUIDE_KEY]
    forward = {key: "2026-11-17" for key in keys}
    backward = {key: "2026-11-17" for key in reversed(keys)}
    outputs = []
    for root, done in ((first_root, forward), (second_root, backward)):
        vault = _vault(root)
        _put_log(vault)
        _put_state(vault, _v1(done=done, last_run=dict(V1_LAST_RUN)))
        digest = cr.migration_report(vault, today=TODAY)["digest"]
        cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest=digest, today=TODAY)
        outputs.append(cr.state_path(vault).read_bytes())

    assert outputs[0] == outputs[1]


def test_a_second_apply_is_refused_and_leaves_the_v2_bytes_alone(tmp_path: Path) -> None:
    vault, _ = _migratable(tmp_path)
    digest = cr.migration_report(vault, today=TODAY)["digest"]
    cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest=digest, today=TODAY)
    migrated = cr.state_path(vault).read_bytes()

    report = cr.migration_report(vault, today=TODAY)
    assert report["status"] == "v2"
    assert any("already" in reason for reason in report["blockers"])
    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest=digest, today=TODAY)
    assert cr.state_path(vault).read_bytes() == migrated


def test_crash_before_the_publish_leaves_v1_intact_and_the_backup_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, before = _migratable(tmp_path)
    digest = cr.migration_report(vault, today=TODAY)["digest"]
    real_replace = os.replace

    def crashing(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if Path(dst).name == "Curation-State.json":
            raise OSError("simulated crash before publish")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(cr.os, "replace", crashing)
    with pytest.raises(cr.CurationStateError) as raised:
        cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest=digest, today=TODAY)

    assert "delete" in str(raised.value) and BACKUP_NAME in str(raised.value)
    assert cr.state_path(vault).read_bytes() == before
    assert (vault / "Workspace" / BACKUP_NAME).read_bytes() == before
    assert not (vault / "Workspace" / ".Curation-State.json.write.tmp").exists()


def test_apply_publishes_under_the_state_lock(
    tmp_path: Path, publish_depths: list[int]
) -> None:
    vault, _ = _migratable(tmp_path)
    digest = cr.migration_report(vault, today=TODAY)["digest"]

    cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest=digest, today=TODAY)

    assert publish_depths == [1]


# ── Migration: refusals, each with nothing written ───────────────────────


def _assert_untouched(vault: Path, before: bytes | None) -> None:
    path = cr.state_path(vault)
    if before is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == before
    assert not (vault / "Workspace" / BACKUP_NAME).exists()


@pytest.mark.parametrize("confirm", [False, 1, None, "yes"], ids=("false", "one", "none", "text"))
def test_apply_requires_the_literal_quiesced_confirmation(tmp_path: Path, confirm: Any) -> None:
    vault, before = _migratable(tmp_path)
    digest = cr.migration_report(vault, today=TODAY)["digest"]

    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(vault, confirm_quiesced=confirm, expected_digest=digest, today=TODAY)
    _assert_untouched(vault, before)


@pytest.mark.parametrize("digest", ["", "0" * 64], ids=("empty", "wrong"))
def test_apply_refuses_a_missing_or_stale_digest(tmp_path: Path, digest: str) -> None:
    vault, before = _migratable(tmp_path)

    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest=digest, today=TODAY)
    _assert_untouched(vault, before)


def test_a_live_lease_blocks_migration_even_with_a_matching_digest(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _put_log(vault)
    live_until = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    before = _put_state(
        vault,
        _v1(lease={"holder": "run", "started_at": "2026-11-18T00:00:00Z", "expires_at": live_until}),
    )
    report = cr.migration_report(vault, today=TODAY)
    assert report["live_lease"] is not None
    assert any("lease" in reason for reason in report["blockers"])

    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(
            vault, confirm_quiesced=True, expected_digest=report["digest"], today=TODAY
        )
    _assert_untouched(vault, before)


@pytest.mark.parametrize(
    "contents",
    [
        '{"version": 1, "done_keys": {}, "lease": {}',
        '{"version": 1, "version": 1, "done_keys": {}, "lease": {}, "last_run": {}}',
        '{"version": 1, "done_keys": {"x": "yesterday"}, "lease": {}, "last_run": {}}',
    ],
    ids=("truncated", "duplicate-key", "invalid-date"),
)
def test_corrupt_v1_state_is_refused_and_never_treated_as_absent(tmp_path: Path, contents: str) -> None:
    vault = _vault(tmp_path)
    _put_log(vault)
    before = _put_state(vault, contents)

    assert cr.migration_report(vault, today=TODAY)["status"] == "unreadable"
    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest="0" * 64, today=TODAY)
    _assert_untouched(vault, before)


def test_a_symlinked_state_file_is_refused_and_its_target_is_untouched(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _put_log(vault)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(_v1(done={DAILY: "2026-11-17"})), encoding="utf-8")
    cr.state_path(vault).symlink_to(target)
    target_before = target.read_bytes()

    report = cr.migration_report(vault, today=TODAY)
    assert report["status"] == "unreadable"
    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(
            vault, confirm_quiesced=True, expected_digest=report["digest"], today=TODAY
        )
    assert target.read_bytes() == target_before
    assert cr.state_path(vault).is_symlink()
    assert not (vault / "Workspace" / BACKUP_NAME).exists()


def test_an_absent_state_is_refused_and_no_v2_is_invented(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _put_log(vault)
    report = cr.migration_report(vault, today=TODAY)
    assert report["status"] == "absent"

    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(
            vault, confirm_quiesced=True, expected_digest=report["digest"], today=TODAY
        )
    _assert_untouched(vault, None)


def test_an_ambiguous_marker_log_is_refused(tmp_path: Path) -> None:
    vault, before = _migratable(tmp_path)
    _put_log(vault, b"---\nlast_full_pass: 2026-09-01\nlast_full_pass: 2026-09-02\n---\n\n# Curation log\n")
    report = cr.migration_report(vault, today=TODAY)
    assert report["marker"] == "unreadable"
    assert report["blockers"]

    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(
            vault, confirm_quiesced=True, expected_digest=report["digest"], today=TODAY
        )
    _assert_untouched(vault, before)


def test_a_symlinked_marker_log_is_refused(tmp_path: Path) -> None:
    vault, before = _migratable(tmp_path)
    other = tmp_path / "other-log.md"
    other.write_bytes(MARKER_BYTES)
    log = vault / cr.CURATION_LOG_RELATIVE
    log.unlink()
    log.symlink_to(other)

    report = cr.migration_report(vault, today=TODAY)
    assert report["marker"] == "unreadable"
    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(
            vault, confirm_quiesced=True, expected_digest=report["digest"], today=TODAY
        )
    _assert_untouched(vault, before)


def test_a_last_run_with_an_extra_field_is_refused_without_stripping_it(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _put_log(vault)
    last_run = dict(V1_LAST_RUN, extra="kept")
    before = _put_state(vault, _v1(done={DAILY: "2026-11-17"}, last_run=last_run))

    report = cr.migration_report(vault, today=TODAY)
    assert any("last_run" in reason for reason in report["blockers"])
    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(
            vault, confirm_quiesced=True, expected_digest=report["digest"], today=TODAY
        )
    _assert_untouched(vault, before)


def test_an_existing_backup_is_refused_and_never_overwritten(tmp_path: Path) -> None:
    vault, before = _migratable(tmp_path)
    backup = vault / "Workspace" / BACKUP_NAME
    backup.write_bytes(b"an earlier backup")
    report = cr.migration_report(vault, today=TODAY)
    assert any("backup" in reason for reason in report["blockers"])

    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(
            vault, confirm_quiesced=True, expected_digest=report["digest"], today=TODAY
        )
    assert backup.read_bytes() == b"an earlier backup"
    assert cr.state_path(vault).read_bytes() == before


# ── CLI ──────────────────────────────────────────────────────────────────


def _cli_install(tmp_path: Path) -> Path:
    """The registered-install layout from tests/test_cli.py, with one migratable v1 vault."""
    root = tmp_path / "workspace"
    vault = root / "memory-vault" / "client-a"
    (vault / "Workspace").mkdir(parents=True)
    (vault / "People").mkdir()
    (root / ".runtime").mkdir(parents=True)
    (root / ".runtime" / "workspaces.json").write_text(
        json.dumps({"work": {"name": "work", "vault_root": str(vault)}}),
        encoding="utf-8",
    )
    _put_log(vault)
    _put_state(vault, _v1(done={DAILY: "2026-11-17"}, last_run=dict(V1_LAST_RUN)))
    return vault


def test_dry_run_text_prints_the_report_and_the_exact_apply_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = _cli_install(tmp_path)
    digest = cr.migration_report(vault, today=None)["digest"]
    capsys.readouterr()

    code = cli.main(["curation-cycle-migrate", "--vault-root", str(vault), "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "status: v1" in out
    assert digest in out
    assert "--apply" in out and "--confirm-quiesced" in out


def test_dry_run_json_carries_the_digest_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = _cli_install(tmp_path)
    before = cr.state_path(vault).read_bytes()
    capsys.readouterr()

    code = cli.main(["curation-cycle-migrate", "--vault-root", str(vault), "--dry-run", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "v1"
    assert len(payload["digest"]) == 64
    assert cr.state_path(vault).read_bytes() == before


@pytest.mark.parametrize(
    "extra",
    [["--apply"], ["--apply", "--digest", "0" * 64], ["--apply", "--confirm-quiesced"]],
    ids=("bare", "no-confirm", "no-digest"),
)
def test_apply_without_its_required_flags_is_a_usage_error(
    tmp_path: Path, extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    vault = _cli_install(tmp_path)
    before = cr.state_path(vault).read_bytes()

    code = cli.main(["curation-cycle-migrate", "--vault-root", str(vault), *extra])

    assert code == 2
    assert cr.state_path(vault).read_bytes() == before
    assert not (vault / "Workspace" / BACKUP_NAME).exists()


def test_dry_run_and_apply_are_mutually_exclusive(tmp_path: Path) -> None:
    vault = _cli_install(tmp_path)
    with pytest.raises(SystemExit) as raised:
        cli.main(["curation-cycle-migrate", "--vault-root", str(vault), "--dry-run", "--apply"])
    assert raised.value.code == 2


def test_apply_with_the_previewed_digest_migrates_the_registered_install(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _cli_install(tmp_path)
    capsys.readouterr()
    _operator_at_terminal(monkeypatch)
    assert cli.main(["curation-cycle-migrate", "--vault-root", str(vault), "--dry-run", "--json"]) == 0
    digest = json.loads(capsys.readouterr().out)["digest"]

    code = cli.main(
        ["curation-cycle-migrate", "--vault-root", str(vault), "--apply", "--digest", digest,
         "--confirm-quiesced", "--json"]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "migrated"
    assert cc.decode_state(cr.state_path(vault).read_text(encoding="utf-8")).daily == {DAILY: "2026-11-17"}


def test_apply_with_a_stale_digest_exits_one_and_names_the_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _cli_install(tmp_path)
    before = cr.state_path(vault).read_bytes()
    capsys.readouterr()
    _operator_at_terminal(monkeypatch)

    code = cli.main(
        ["curation-cycle-migrate", "--vault-root", str(vault), "--apply", "--digest", "1" * 64,
         "--confirm-quiesced"]
    )

    assert code == 1
    assert "digest" in capsys.readouterr().err
    assert cr.state_path(vault).read_bytes() == before


# ── Hardening: digest day, backup, terminal gate, temporary file, lease ──


@pytest.mark.parametrize("shift", [1, -1], ids=("later-day", "clock-set-back"))
def test_a_shifted_preview_day_is_a_digest_mismatch(tmp_path: Path, shift: int) -> None:
    """`dropped` depends on the day, so the digest must change when the day does."""
    vault, before = _migratable(tmp_path)
    digest = cr.migration_report(vault, today=TODAY)["digest"]

    with pytest.raises(cr.CurationMigrationRefused) as raised:
        cr.migrate_state_to_cycle(
            vault,
            confirm_quiesced=True,
            expected_digest=digest,
            today=TODAY + timedelta(days=shift),
        )

    assert "digest" in str(raised.value)
    _assert_untouched(vault, before)


def test_write_backup_refuses_an_existing_file_and_keeps_its_bytes(tmp_path: Path) -> None:
    backup = tmp_path / "Curation-State.v1.bak"
    backup.write_bytes(b"earlier backup")

    with pytest.raises(cr.CurationMigrationRefused) as raised:
        cr._write_backup(backup, b"new bytes")

    assert "delete" in str(raised.value)
    assert backup.read_bytes() == b"earlier backup"


def test_a_planted_temporary_symlink_is_replaced_not_written_through(tmp_path: Path) -> None:
    vault, _ = _migratable(tmp_path)
    digest = cr.migration_report(vault, today=TODAY)["digest"]
    target = tmp_path / "victim.txt"
    target.write_bytes(b"do not touch")
    planted = vault / "Workspace" / ".Curation-State.json.write.tmp"
    planted.symlink_to(target)

    cr.migrate_state_to_cycle(vault, confirm_quiesced=True, expected_digest=digest, today=TODAY)

    assert target.read_bytes() == b"do not touch"
    state = cr.state_path(vault)
    assert not state.is_symlink()
    assert cc.decode_state(state.read_text(encoding="utf-8")).daily == {DAILY: "2026-11-17"}
    assert not planted.exists()


def test_an_unparsable_lease_expiry_is_a_clean_blocker_and_nothing_is_written(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _put_log(vault)
    before = _put_state(
        vault,
        _v1(lease={"holder": "run", "started_at": "2026-11-18T00:00:00Z", "expires_at": "not-a-date"}),
    )

    report = cr.migration_report(vault, today=TODAY)

    assert report["status"] == "unreadable"
    assert any("lease" in reason for reason in report["blockers"])
    with pytest.raises(cr.CurationMigrationRefused):
        cr.migrate_state_to_cycle(
            vault, confirm_quiesced=True, expected_digest=report["digest"], today=TODAY
        )
    _assert_untouched(vault, before)


def test_apply_is_refused_without_an_interactive_terminal_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _cli_install(tmp_path)
    before = cr.state_path(vault).read_bytes()
    digest = cr.migration_report(vault)["digest"]
    capsys.readouterr()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    code = cli.main(
        ["curation-cycle-migrate", "--vault-root", str(vault), "--apply", "--digest", digest,
         "--confirm-quiesced"]
    )

    assert code == 1
    assert "interactive terminal" in capsys.readouterr().err
    assert cr.state_path(vault).read_bytes() == before
    assert not (vault / "Workspace" / BACKUP_NAME).exists()


def test_apply_needs_stdout_to_be_a_terminal_as_well(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _cli_install(tmp_path)
    digest = cr.migration_report(vault)["digest"]
    capsys.readouterr()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)

    code = cli.main(
        ["curation-cycle-migrate", "--vault-root", str(vault), "--apply", "--digest", digest,
         "--confirm-quiesced"]
    )

    assert code == 1
    assert not (vault / "Workspace" / BACKUP_NAME).exists()


def test_the_dry_run_stays_open_without_a_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _cli_install(tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    assert cli.main(["curation-cycle-migrate", "--vault-root", str(vault), "--dry-run", "--json"]) == 0


def test_the_migration_command_is_absent_from_every_agent_reference() -> None:
    """The nightly agent must not be told the command exists: no help topic, stock file or doc."""
    from ciao import agent_cli

    root = Path(__file__).resolve().parents[1]
    needle = b"curation-cycle-migrate"
    assert needle not in agent_cli._help_text().encode()
    for topic in agent_cli._HELP_TOPICS:
        assert needle not in agent_cli._help_text(topic).encode(), topic
    assert needle not in (root / "ciao" / "system_prompt.md").read_bytes()
    assert needle not in (root / "docs" / "AGENT_CLI.md").read_bytes()
    for path in (root / "ciao" / "stock").rglob("*"):
        if path.is_file():
            assert needle not in path.read_bytes(), path
