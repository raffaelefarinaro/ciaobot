from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ciao import care_eval_seed as seed
from ciao import note_verification as nv
from ciao.care_eval_fixtures import CARE_FIXTURES, grade_fixture
from ciao.curation_run import CURATION_LOG_RELATIVE, PROPOSALS_RELATIVE

SCENARIOS = ("nightly", "weekly_due", "budget_continuation")
DISPATCH_DATE = date(2026, 11, 24)


def fixture_for(scenario: str) -> Any:
    (match,) = [item for item in CARE_FIXTURES if item.scenario == scenario]
    return match


def clock(day: date = DISPATCH_DATE) -> seed.DispatchClock:
    return seed.DispatchClock(started_at=datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC))


def seeded(tmp_path: Path, scenario: str = "nightly") -> tuple[Path, seed.SeedManifest]:
    root = tmp_path / "vault"
    manifest = seed.build_seed(scenario, root, dispatch_date=DISPATCH_DATE)
    return root, manifest


def test_symbolic_bindings_are_the_oracle_expected_bytes() -> None:
    # The binder emits these symbols; they must be exactly what the oracle expects.
    expected = {
        "guide_bytes": fixture_for("nightly").protected[0].expected_bytes,
        "note_revision_verified": next(
            o.expected_bytes
            for o in fixture_for("nightly").obligations
            if o.observation_key == "note_revision_verified"
        ),
        "awaiting_human": next(
            o.expected_bytes
            for o in fixture_for("nightly").obligations
            if o.observation_key == "pending_proposal_disposition"
        ),
        "weekly_marker": next(
            o.expected_bytes
            for o in fixture_for("weekly_due").obligations
            if o.observation_key == "weekly_marker"
        ),
    }
    assert seed.SYMBOL_GUIDE == expected["guide_bytes"]
    assert seed.SYMBOL_NOTE_REVISION == expected["note_revision_verified"]
    assert seed.SYMBOL_AWAITING_HUMAN == expected["awaiting_human"]
    assert seed.SYMBOL_WEEKLY_MARKER == expected["weekly_marker"]


def test_builder_is_deterministic_and_digest_moves_with_one_byte(tmp_path: Path) -> None:
    _, first = seeded(tmp_path / "a")
    _, second = seeded(tmp_path / "b")
    assert first.fixture_sha256 == second.fixture_sha256
    assert first.files == second.files
    assert len(first.fixture_sha256) == 64

    files = {f.path: (tmp_path / "a" / "vault" / f.path).read_bytes() for f in first.files}
    changed = dict(files)
    victim = sorted(changed)[0]
    changed[victim] = changed[victim][:-1] + bytes([changed[victim][-1] ^ 1])
    assert seed.fixture_digest("nightly", files) != seed.fixture_digest("nightly", changed)


def test_digest_depends_on_scenario_name(tmp_path: Path) -> None:
    _, manifest = seeded(tmp_path)
    files = {f.path: (tmp_path / "vault" / f.path).read_bytes() for f in manifest.files}
    assert seed.fixture_digest("nightly", files) != seed.fixture_digest("weekly_due", files)


def test_builder_writes_only_under_the_given_root(tmp_path: Path) -> None:
    for scenario in SCENARIOS:
        root, manifest = seeded(tmp_path / scenario, scenario)
        written = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        assert written == sorted(f.path for f in manifest.files)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(SCENARIOS)


def test_builder_refuses_to_overwrite_an_existing_file(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path)
    with pytest.raises(FileExistsError):
        seed.build_seed("nightly", root, dispatch_date=DISPATCH_DATE)
    assert manifest.files  # the first build is intact


def test_builder_refuses_a_dispatch_date_that_is_not_weekly_due(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        seed.build_seed("weekly_due", tmp_path / "v", dispatch_date=date(2026, 11, 2))


def test_untouched_seed_never_earns_effect_credit(tmp_path: Path) -> None:
    # Only the nightly disposition obligation is a preservation check (the human's
    # decide-you row must stay queued), so inaction satisfies it by design. Every
    # effect obligation must fail or stay unknown on a run that did nothing.
    preserved = {"pending_proposal_disposition"}
    for scenario in SCENARIOS:
        root, manifest = seeded(tmp_path / scenario, scenario)
        observed = seed.observe(scenario, root, manifest, clock())
        outcomes, safety = grade_fixture(fixture_for(scenario), observed)
        assert safety == "passed"
        for item in fixture_for(scenario).obligations:
            if item.observation_key in preserved:
                assert outcomes[item.id] == "passed"
            else:
                assert outcomes[item.id] != "passed", (scenario, item.id)


def test_weekly_marker_stays_unknown_without_index_and_audit_observations(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path, "weekly_due")
    (root / CURATION_LOG_RELATIVE).write_bytes(
        (root / CURATION_LOG_RELATIVE).read_bytes().replace(b"2026-11-01", b"2026-11-24")
    )
    observed = seed.observe("weekly_due", root, manifest, clock())
    assert observed["weekly_marker"] == seed.SYMBOL_WEEKLY_MARKER
    assert "index_refresh_result" not in observed
    outcomes, _ = grade_fixture(fixture_for("weekly_due"), observed)
    assert outcomes["o003"] == "unknown"


def test_guide_unchanged_is_symbolic_and_changed_is_actual_bytes(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path)
    assert seed.observe("nightly", root, manifest, clock())["guide_bytes"] == seed.SYMBOL_GUIDE

    (root / seed.GUIDE_RELATIVE).write_bytes(b"rewritten guide")
    assert seed.observe("nightly", root, manifest, clock())["guide_bytes"] == b"rewritten guide"

    (root / seed.GUIDE_RELATIVE).unlink()
    assert seed.observe("nightly", root, manifest, clock())["guide_bytes"] is None


def test_note_without_check_state_is_observed_absent(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path)
    assert seed.observe("nightly", root, manifest, clock())["note_revision_verified"] is None


def test_note_with_matching_check_is_symbolic(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path)
    _write_check(root, _note_revision(root))
    observed = seed.observe("nightly", root, manifest, clock())
    assert observed["note_revision_verified"] == seed.SYMBOL_NOTE_REVISION


@pytest.mark.parametrize("outcome", ["failed", "conflict", "unverified", "update", "retire"])
def test_matching_check_that_is_not_still_valid_never_earns_the_verified_bytes(
    tmp_path: Path, outcome: str
) -> None:
    root, manifest = seeded(tmp_path)
    _write_check(root, _note_revision(root), outcome)
    observed = seed.observe("nightly", root, manifest, clock())
    assert observed["note_revision_verified"] is None
    outcomes, _ = grade_fixture(fixture_for("nightly"), observed)
    assert outcomes["o001"] == "failed"


def test_check_for_an_older_note_revision_is_observed_absent(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path)
    _write_check(root, "0" * 64)
    observed = seed.observe("nightly", root, manifest, clock())
    assert observed["note_revision_verified"] is None


def test_note_edited_after_its_check_is_observed_absent(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path)
    _write_check(root, _note_revision(root))
    note = root / seed.NOTE_RELATIVE
    note.write_text(note.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    observed = seed.observe("nightly", root, manifest, clock())
    assert observed["note_revision_verified"] is None


def test_unreadable_check_state_omits_the_key(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path)
    check_path = nv.note_check_state_path(root)
    check_path.parent.mkdir(parents=True, exist_ok=True)
    check_path.write_text("{not json", encoding="utf-8")
    observed = seed.observe("nightly", root, manifest, clock())
    assert "note_revision_verified" not in observed
    outcomes, _ = grade_fixture(fixture_for("nightly"), observed)
    assert outcomes["o001"] == "unknown"


def test_seeded_decide_you_row_still_queued_is_awaiting_human(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path)
    assert seed.observe("nightly", root, manifest, clock())[
        "pending_proposal_disposition"
    ] == seed.SYMBOL_AWAITING_HUMAN


def test_seeded_row_removed_or_reassigned_is_observed_absent(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path)
    queue = root / PROPOSALS_RELATIVE
    text = queue.read_text(encoding="utf-8")
    queue.write_text(text.replace("decide: you", "decide: nightly"), encoding="utf-8")
    assert (
        seed.observe("nightly", root, manifest, clock())["pending_proposal_disposition"] is None
    )
    queue.unlink()
    assert (
        seed.observe("nightly", root, manifest, clock())["pending_proposal_disposition"] is None
    )


def test_weekly_marker_equal_to_dispatch_utc_date_is_symbolic(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path, "weekly_due")
    _write_marker(root, "2026-11-25")
    # 23:30 on the 24th in UTC-05:00 is 04:30 on the 25th UTC: the UTC date wins.
    late = datetime(2026, 11, 24, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    observed = seed.observe("weekly_due", root, manifest, seed.DispatchClock(started_at=late))
    assert observed["weekly_marker"] == seed.SYMBOL_WEEKLY_MARKER


def test_stale_weekly_marker_is_its_actual_bytes(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path, "weekly_due")
    observed = seed.observe("weekly_due", root, manifest, clock())
    assert observed["weekly_marker"] == seed.SEED_MARKER.encode()


def test_symlinked_log_is_refused_and_the_marker_is_unknown(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path, "weekly_due")
    log = root / CURATION_LOG_RELATIVE
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_bytes(log.read_bytes().replace(b"2026-11-01", b"2026-11-24"))
    log.unlink()
    log.symlink_to(elsewhere)
    observed = seed.observe("weekly_due", root, manifest, clock(date(2026, 11, 24)))
    assert "weekly_marker" not in observed
    outcomes, _ = grade_fixture(fixture_for("weekly_due"), observed)
    assert outcomes["o003"] == "unknown"


def test_absent_or_markerless_log_is_observed_absent(tmp_path: Path) -> None:
    root, manifest = seeded(tmp_path, "weekly_due")
    (root / CURATION_LOG_RELATIVE).write_text("# no frontmatter\n", encoding="utf-8")
    assert seed.observe("weekly_due", root, manifest, clock())["weekly_marker"] is None
    (root / CURATION_LOG_RELATIVE).unlink()
    assert seed.observe("weekly_due", root, manifest, clock())["weekly_marker"] is None


def test_dispatch_clock_requires_an_aware_timestamp() -> None:
    with pytest.raises(ValueError):
        seed.DispatchClock(started_at=datetime(2026, 11, 24, 9, 0))


def test_nothing_reads_the_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_home() -> Path:
        raise AssertionError("Path.home() must not be read")

    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    root, manifest = seeded(tmp_path)
    seed.observe("nightly", root, manifest, clock())


def _note_revision(root: Path) -> str:
    from ciao.memory_receipts import content_revision

    return content_revision((root / seed.NOTE_RELATIVE).read_text(encoding="utf-8"))


def _write_check(root: Path, revision: str, outcome: str = "still_valid") -> None:
    path = nv.note_check_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "relative_path": seed.NOTE_RELATIVE,
        "content_revision": revision,
        "outcome": outcome,
        "checked_at": "2026-11-24",
        "retry_after": "2026-12-24",
    }
    path.write_text(
        json.dumps({"schema": 1, "notes": {seed.NOTE_RELATIVE: row}, "entries": {}}),
        encoding="utf-8",
    )


def _write_marker(root: Path, stamp: str) -> None:
    path = root / CURATION_LOG_RELATIVE
    path.write_text(f"---\nlast_full_pass: {stamp}\n---\n\n# Curation log\n", encoding="utf-8")
