"""Corrupt persisted curation state must fail closed.

The state file carries both the run cursor and the lease used to fence
unattended mutations. Treating a damaged file as empty can let a second run
take over and perform a lease-dependent safety action.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ciao import curation_run as cr


def _valid_state(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "done_keys": {},
        "lease": {},
        "last_run": {},
        **changes,
    }


@pytest.mark.parametrize(
    "contents",
    [
        '{"version": 1, "done_keys": {}, "lease": {"holder": "current", '
        '"started_at": "2026-01-01T00:00:00Z", "expires_at": "not-a-date"}, '
        '"last_run": {}}',
        '{"version":',
    ],
    ids=("malformed-expiry", "invalid-json"),
)
def test_corrupt_existing_state_blocks_takeover_and_lease_dependent_action(
    tmp_path: Path, contents: str
) -> None:
    vault = tmp_path / "vault"
    path = cr.state_path(vault)
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    original = path.read_bytes()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(cr.CurationStateError):
        cr.active_lease(vault, now=now)
    with pytest.raises(cr.CurationStateError):
        cr.begin_run(vault, holder="takeover", now=now)
    with pytest.raises(cr.CurationStateError):
        cr.record_done(vault, ["unsafe-action"], holder="current")

    assert path.read_bytes() == original


def test_missing_state_is_valid_and_allows_initial_lease(tmp_path: Path) -> None:
    vault = tmp_path / "vault"

    lease = cr.begin_run(
        vault,
        holder="first-run",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert lease.holder.startswith("first-run:")
    assert cr.active_lease(vault, now=datetime(2026, 1, 1, tzinfo=UTC)) == lease.as_dict()
    assert cr.state_path(vault).exists()


@pytest.mark.parametrize(
    "contents",
    [
        "[]",
        '{"done_keys": {}, "lease": {}, "last_run": {}}',
        '{"version": true, "done_keys": {}, "lease": {}, "last_run": {}}',
        '{"version": 2, "done_keys": {}, "lease": {}, "last_run": {}}',
        '{"version": 1, "done_keys": {}, "lease": {}}',
        '{"version": 1, "done_keys": [], "lease": {}, "last_run": {}}',
        '{"version": 1, "done_keys": {"x": "yesterday"}, "lease": {}, "last_run": {}}',
        '{"version": 1, "done_keys": {}, "lease": {"holder": "x"}, "last_run": {}}',
        '{"version": 1, "done_keys": {}, "lease": {"holder": " ", '
        '"started_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-02T00:00:00Z"}, '
        '"last_run": {}}',
        '{"version": 1, "done_keys": {}, "lease": {"holder": "x", '
        '"started_at": "not-a-date", "expires_at": "2026-01-02T00:00:00Z"}, '
        '"last_run": {}}',
        '{"version": 1, "done_keys": {}, "lease": {"holder": "x", '
        '"started_at": "2026-01-01T00:00:00Z", "expires_at": null}, '
        '"last_run": {}}',
        '{"version": 1, "done_keys": {}, "lease": {}, "last_run": '
        '{"status": "ok", "finished_at": "now", "planned": 0, '
        '"completed": 0, "deferred": 0, "reasons": []}}',
        '{"version": 1, "done_keys": {}, "lease": {}, "last_run": '
        '{"status": "ok", "finished_at": "2026-01-01T00:00:00Z", "planned": true, '
        '"completed": 0, "deferred": 0, "reasons": []}}',
        '{"version": 1, "done_keys": {}, "lease": {}, "last_run": {}, "future": 1}',
    ],
    ids=(
        "non-object",
        "versionless",
        "boolean-version",
        "future-version",
        "partial-document",
        "wrong-done-map",
        "invalid-done-date",
        "partial-lease",
        "blank-holder",
        "invalid-start",
        "missing-expiry",
        "invalid-last-run",
        "boolean-count",
        "unknown-field",
    ),
)
def test_invalid_state_documents_are_refused_without_rewriting(
    tmp_path: Path, contents: str
) -> None:
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(cr.CurationStateError):
        cr.load_state(path.parent.parent)

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "contents",
    [
        '{"version":1,"version":1,"done_keys":{},"lease":{},"last_run":{}}',
        '{"version":1,"version":2,"done_keys":{},"lease":{},"last_run":{}}',
        '{"version":2,"version":1,"done_keys":{},"lease":{},"last_run":{}}',
        '{"version":1,"done_keys":{},"lease":{"holder":"a","holder":"b",'
        '"started_at":"2026-01-01T00:00:00Z","expires_at":"2026-01-02T00:00:00Z"},'
        '"last_run":{}}',
        '{"version":1,"done_keys":{},"lease":{"holder":"b","holder":"a",'
        '"started_at":"2026-01-01T00:00:00Z","expires_at":"2026-01-02T00:00:00Z"},'
        '"last_run":{}}',
        '{"version":1,"done_keys":{},"lease":{},"last_run":{"status":"ok",'
        '"finished_at":"2026-01-01T00:00:00Z","planned":0,"completed":0,'
        '"deferred":0,"reasons":[],"status":"bad"}}',
        '{"version":1,"done_keys":{},"lease":{},"last_run":{"status":"bad",'
        '"finished_at":"2026-01-01T00:00:00Z","planned":0,"completed":0,'
        '"deferred":0,"reasons":[],"status":"ok"}}',
    ],
)
def test_duplicate_keys_at_any_depth_are_rejected(
    tmp_path: Path, contents: str
) -> None:
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(cr.CurationStateError):
        cr.load_state(path.parent.parent)


def test_deeply_nested_json_is_a_controlled_state_error(tmp_path: Path) -> None:
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    path.write_text('{"version":1,"done_keys":{},"lease":{},"last_run":{},"x":' + "[" * 2000 + "0" + "]" * 2000 + "}", encoding="utf-8")

    with pytest.raises(cr.CurationStateError) as raised:
        cr.load_state(path.parent.parent)

    assert "RecursionError" not in str(raised.value)
    assert len(str(raised.value)) < 500


def test_duplicate_state_error_does_not_include_contents_or_parser_error(
    tmp_path: Path,
) -> None:
    marker = "curation-state-content-marker"
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    path.write_text(
        f'{{"version":1,"done_keys":{{}},"lease":{{}},"last_run":{{}},"x":"{marker}",',
        encoding="utf-8",
    )

    with pytest.raises(cr.CurationStateError) as raised:
        cr.load_state(path.parent.parent)

    assert marker not in str(raised.value)
    assert "JSONDecodeError" not in str(raised.value)


def test_dangling_state_symlink_is_not_treated_as_absent(tmp_path: Path) -> None:
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    _symlink_or_skip(path, path.with_name("missing-target"))

    with pytest.raises(cr.CurationStateError):
        cr.load_state(path.parent.parent)


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if sys.platform == "win32" and exc.winerror == 1314:
            pytest.skip(f"Windows symlink privilege is unavailable: {exc}")
        raise


def test_dangling_workspace_symlink_is_not_fresh_state(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    workspace = vault / "Workspace"
    vault.mkdir()
    _symlink_or_skip(workspace, vault / "missing-workspace")

    with pytest.raises(cr.CurationStateError):
        cr.load_state(vault)
    with pytest.raises(cr.CurationStateError):
        cr.active_lease(vault)
    assert workspace.is_symlink()


def test_valid_v1_empty_state_and_expired_lease_remain_supported(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = cr.state_path(vault)
    path.parent.mkdir(parents=True)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    path.write_text(json.dumps(_valid_state()), encoding="utf-8")
    assert cr.load_state(vault) == cr.CurationState()

    path.write_text(
        json.dumps(
            _valid_state(
                lease={
                    "holder": " legacy holder ",
                    "started_at": "2025-12-31T23:00:00-01:00",
                    "expires_at": "2025-12-31T23:59:59Z",
                }
            )
        ),
        encoding="utf-8",
    )
    assert cr.active_lease(vault, now=now) is None
    replacement = cr.begin_run(vault, holder="next", now=now)
    assert replacement.holder.startswith("next:")


def test_valid_lease_preserves_exact_legacy_holder_and_extra_fields(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = cr.state_path(vault)
    path.parent.mkdir(parents=True)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    raw_lease = {
        "holder": " legacy holder ",
        "started_at": "2026-01-01T00:00:00",
        "expires_at": "2026-01-01T00:05:00+00:00",
        "extension": "kept",
    }
    path.write_text(json.dumps(_valid_state(lease=raw_lease)), encoding="utf-8")

    assert cr.active_lease(vault, now=now) == raw_lease
    assert cr.renew_run(vault, holder=" legacy holder ", now=now)
    assert cr.load_state(vault).lease["extension"] == "kept"


def test_valid_v1_state_roundtrips_through_begin_renew_progress_and_end(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    now = datetime(2026, 1, 1, tzinfo=UTC)

    lease = cr.begin_run(vault, holder="roundtrip", now=now)
    begun = json.loads(cr.state_path(vault).read_text(encoding="utf-8"))
    assert begun == {
        "version": 1,
        "done_keys": {},
        "lease": lease.as_dict(),
        "last_run": {},
    }
    assert cr.renew_run(vault, holder=lease.holder, now=now)
    assert cr.record_done(
        vault, ["pass:one"], holder=lease.holder, now=now
    ) == 1
    completed = cr.end_run(
        vault,
        holder=lease.holder,
        status="ok",
        planned=1,
        completed=1,
        now=now,
    )

    persisted = cr.load_state(vault)
    assert persisted.lease == {}
    assert persisted.done_keys == {"pass:one": "2026-01-01"}
    assert persisted.last_run == {
        "status": "ok",
        "finished_at": "2026-01-01T00:00:00Z",
        "planned": 1,
        "completed": 1,
        "deferred": 0,
        "reasons": [],
    }
    assert completed["status"] == "ok"


def test_corruption_blocks_all_core_mutators_before_state_or_marker_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    path = cr.state_path(vault)
    path.parent.mkdir(parents=True)
    path.write_text('{"version":', encoding="utf-8")
    log = vault / cr.CURATION_LOG_RELATIVE
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# unchanged\n", encoding="utf-8")
    state_before = path.read_bytes()
    log_before = log.read_bytes()

    def unexpected_store(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("corrupt state must be rejected before storing")

    monkeypatch.setattr(cr, "_store", unexpected_store)
    with pytest.raises(cr.CurationStateError):
        cr.begin_run(vault)
    with pytest.raises(cr.CurationStateError):
        cr.renew_run(vault, holder="holder")
    with pytest.raises(cr.CurationStateError):
        cr.holder_is_live(vault, holder="holder")
    with pytest.raises(cr.CurationStateError):
        cr.record_done(vault, ["unsafe"])
    with pytest.raises(cr.CurationStateError):
        cr.end_run(vault, advance_marker=True)
    with pytest.raises(cr.CurationStateError):
        cr.advance_full_pass(vault)

    assert path.read_bytes() == state_before
    assert log.read_bytes() == log_before


def test_state_read_oserror_is_controlled_and_does_not_leak_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    path.write_text('{"version":', encoding="utf-8")
    original_read_text = Path.read_text

    def unreadable(candidate: Path, *args: Any, **kwargs: Any) -> str:
        if candidate == path:
            raise PermissionError("private read detail")
        return original_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(cr.CurationStateError) as raised:
        cr.load_state(path.parent.parent)

    assert "private read detail" not in str(raised.value)
    assert len(str(raised.value)) < 300


def test_file_not_found_read_with_existing_entry_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_valid_state()), encoding="utf-8")
    original_read_text = Path.read_text

    def missing(candidate: Path, *args: Any, **kwargs: Any) -> str:
        if candidate == path:
            raise FileNotFoundError("synthetic race")
        return original_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", missing)
    with pytest.raises(cr.CurationStateError, match="not readable"):
        cr.load_state(path.parent.parent)


def test_uncertain_lstat_classification_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_valid_state()), encoding="utf-8")
    original_read_text = Path.read_text
    original_lstat = Path.lstat

    def missing(candidate: Path, *args: Any, **kwargs: Any) -> str:
        if candidate == path:
            raise FileNotFoundError("private read detail")
        return original_read_text(candidate, *args, **kwargs)

    def uncertain(candidate: Path, *args: Any, **kwargs: Any):
        if candidate == path:
            raise PermissionError("private lstat detail")
        return original_lstat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", missing)
    monkeypatch.setattr(Path, "lstat", uncertain)
    with pytest.raises(cr.CurationStateError, match="cannot classify") as raised:
        cr.load_state(path.parent.parent)

    assert "private read detail" not in str(raised.value)
    assert "private lstat detail" not in str(raised.value)


def test_state_diagnostic_escapes_and_bounds_adversarial_path(tmp_path: Path) -> None:
    vault = tmp_path / ("root\n\x1b" + "x" * 180)
    path = cr.state_path(vault)
    path.parent.mkdir(parents=True)
    path.write_text('{"secret":"do-not-echo"', encoding="utf-8")

    with pytest.raises(cr.CurationStateError) as raised:
        cr.load_state(vault)

    message = str(raised.value)
    assert "\x1b" not in message
    assert "\n" not in message
    assert "do-not-echo" not in message
    assert len(message) < 250


def test_invalid_utf8_is_rejected_without_changing_the_file(tmp_path: Path) -> None:
    path = cr.state_path(tmp_path / "vault")
    path.parent.mkdir(parents=True)
    original = b'{"version":1,"done_keys":{},"lease":{},"last_run":{}}\xff'
    path.write_bytes(original)

    with pytest.raises(cr.CurationStateError):
        cr.load_state(path.parent.parent)

    assert path.read_bytes() == original


def test_recent_valid_finish_does_not_hide_corrupt_lease(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = cr.state_path(vault)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            _valid_state(
                lease={
                    "holder": "run",
                    "started_at": "2026-01-01T00:00:00Z",
                    "expires_at": "not-an-expiry",
                },
                last_run={
                    "status": "ok",
                    "finished_at": datetime.now(UTC).isoformat(),
                    "planned": 1,
                    "completed": 1,
                    "deferred": 0,
                    "reasons": [],
                },
            )
        ),
        encoding="utf-8",
    )

    reason = cr.unstarted_run_reason(
        vault, datetime.now(UTC).replace(microsecond=0)
    )

    assert "uncertain" in reason
