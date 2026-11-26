from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ciao import care_eval_seed as seed
from ciao import curation_run, memory_receipts
from ciao import note_verification as nv
from ciao.care_eval_fixtures import CARE_FIXTURES, grade_fixture
from ciao.curation_run import CURATION_LOG_RELATIVE, PROPOSALS_RELATIVE
from ciao.task_eval import PROPOSALS_PATH, WORKSPACE

SCENARIOS = ("nightly", "weekly_due", "budget_continuation")
DISPATCH_DATE = date(2026, 11, 24)


def fixture_for(scenario: str) -> Any:
    (match,) = [item for item in CARE_FIXTURES if item.scenario == scenario]
    return match


def clock(day: date = DISPATCH_DATE) -> seed.DispatchClock:
    return seed.DispatchClock(started_at=datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC))


def split(base: Path) -> seed.Layout:
    # The engine's real shape: the guide at the agent root, the vault beneath it.
    agent = base / "personal"
    return seed.Layout(agent_root=agent, vault_root=agent / "memory-vault")


def seeded(base: Path, scenario: str = "nightly") -> tuple[seed.Layout, seed.SeedManifest]:
    layout = split(base)
    manifest = seed.build_seed(scenario, layout, dispatch_date=DISPATCH_DATE)
    return layout, manifest


def on_disk(layout: seed.Layout, relative: str) -> Path:
    # Test-side mirror of where the builder puts each file: the guide lives at the agent root.
    if relative == seed.GUIDE_RELATIVE:
        return layout.agent_root / relative
    return layout.vault_root / relative


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


def test_layout_for_workspace_uses_the_engine_names(tmp_path: Path) -> None:
    layout = seed.layout_for_workspace(tmp_path / "ws")
    assert layout.agent_root == tmp_path / "ws" / WORKSPACE
    assert layout.vault_root == tmp_path / "ws" / WORKSPACE / seed.VAULT_DIRNAME
    # prepare_workspace copies the fixture to <personal>/<vault>, and the proposal queue
    # path it names is relative to the personal folder: the two must agree.
    assert PROPOSALS_PATH == f"{seed.VAULT_DIRNAME}/{PROPOSALS_RELATIVE}"
    assert (layout.agent_root / PROPOSALS_PATH) == layout.vault_root / PROPOSALS_RELATIVE


def test_builder_is_deterministic_and_digest_moves_with_one_byte(tmp_path: Path) -> None:
    first_layout, first = seeded(tmp_path / "a")
    _, second = seeded(tmp_path / "b")
    assert first.fixture_sha256 == second.fixture_sha256
    assert first.files == second.files
    assert len(first.fixture_sha256) == 64

    files = {f.path: on_disk(first_layout, f.path).read_bytes() for f in first.files}
    changed = dict(files)
    victim = sorted(changed)[0]
    changed[victim] = changed[victim][:-1] + bytes([changed[victim][-1] ^ 1])
    assert seed.fixture_digest("nightly", files) != seed.fixture_digest("nightly", changed)


def test_digest_is_independent_of_the_roots_it_was_built_under(tmp_path: Path) -> None:
    # Same relative names and bytes under two unrelated roots: one digest.
    _, here = seeded(tmp_path / "one" / "deep")
    _, there = seeded(tmp_path / "two")
    assert here.fixture_sha256 == there.fixture_sha256
    assert [f.path for f in here.files] == [f.path for f in there.files]


def test_digest_depends_on_scenario_name(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    files = {f.path: on_disk(layout, f.path).read_bytes() for f in manifest.files}
    assert seed.fixture_digest("nightly", files) != seed.fixture_digest("weekly_due", files)


def test_builder_writes_the_guide_under_agent_and_the_rest_under_vault(tmp_path: Path) -> None:
    for scenario in SCENARIOS:
        layout, manifest = seeded(tmp_path / scenario, scenario)
        # The vault sits inside the agent root, so only the agent root's direct files count.
        agent_files = sorted(p.name for p in layout.agent_root.iterdir() if p.is_file())
        vault_files = sorted(
            p.relative_to(layout.vault_root).as_posix()
            for p in layout.vault_root.rglob("*")
            if p.is_file()
        )
        assert agent_files == [seed.GUIDE_RELATIVE]
        expected_vault = [f.path for f in manifest.files if f.path != seed.GUIDE_RELATIVE]
        assert vault_files == sorted(expected_vault)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(SCENARIOS)


def test_builder_refuses_to_overwrite_an_existing_file(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    with pytest.raises(FileExistsError):
        seed.build_seed("nightly", layout, dispatch_date=DISPATCH_DATE)
    assert manifest.files  # the first build is intact


def test_builder_refuses_a_dispatch_date_that_is_not_weekly_due(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        seed.build_seed("weekly_due", split(tmp_path), dispatch_date=date(2026, 11, 2))


def test_single_root_observation_does_not_find_the_split_guide(tmp_path: Path) -> None:
    # Regression guard for the old one-directory layout: the guide sits at the agent
    # root, so observing with agent_root == vault_root must not see it.
    layout, manifest = seeded(tmp_path)
    single_root = seed.Layout(agent_root=layout.vault_root, vault_root=layout.vault_root)
    observed = seed.observe("nightly", single_root, manifest, clock())
    assert observed["guide_bytes"] is None
    assert seed.observe("nightly", layout, manifest, clock())["guide_bytes"] == seed.SYMBOL_GUIDE


def test_untouched_seed_never_earns_effect_credit(tmp_path: Path) -> None:
    # Only the nightly disposition obligation is a preservation check (the human's
    # decide-you row must stay queued), so inaction satisfies it by design. Every
    # effect obligation must fail or stay unknown on a run that did nothing.
    preserved = {"pending_proposal_disposition"}
    for scenario in SCENARIOS:
        layout, manifest = seeded(tmp_path / scenario, scenario)
        observed = seed.observe(scenario, layout, manifest, clock())
        outcomes, safety = grade_fixture(fixture_for(scenario), observed)
        assert safety == "passed"
        for item in fixture_for(scenario).obligations:
            if item.observation_key in preserved:
                assert outcomes[item.id] == "passed"
            else:
                assert outcomes[item.id] != "passed", (scenario, item.id)


def test_weekly_marker_stays_unknown_without_index_and_audit_observations(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path, "weekly_due")
    log = layout.vault_root / CURATION_LOG_RELATIVE
    log.write_bytes(log.read_bytes().replace(b"2026-11-01", b"2026-11-24"))
    observed = seed.observe("weekly_due", layout, manifest, clock())
    assert observed["weekly_marker"] == seed.SYMBOL_WEEKLY_MARKER
    assert "index_refresh_result" not in observed
    outcomes, _ = grade_fixture(fixture_for("weekly_due"), observed)
    assert outcomes["o003"] == "unknown"


def test_guide_unchanged_is_symbolic_and_changed_is_actual_bytes(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    guide = layout.agent_root / seed.GUIDE_RELATIVE
    assert seed.observe("nightly", layout, manifest, clock())["guide_bytes"] == seed.SYMBOL_GUIDE

    guide.write_bytes(b"rewritten guide")
    assert seed.observe("nightly", layout, manifest, clock())["guide_bytes"] == b"rewritten guide"

    guide.unlink()
    assert seed.observe("nightly", layout, manifest, clock())["guide_bytes"] is None


def test_note_without_check_state_is_observed_absent(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    assert seed.observe("nightly", layout, manifest, clock())["note_revision_verified"] is None


def test_note_with_matching_check_is_symbolic(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    _write_check(layout.vault_root, _note_revision(layout.vault_root))
    observed = seed.observe("nightly", layout, manifest, clock())
    assert observed["note_revision_verified"] == seed.SYMBOL_NOTE_REVISION


@pytest.mark.parametrize("outcome", ["failed", "conflict", "unverified", "update", "retire"])
def test_matching_check_that_is_not_still_valid_never_earns_the_verified_bytes(
    tmp_path: Path, outcome: str
) -> None:
    layout, manifest = seeded(tmp_path)
    _write_check(layout.vault_root, _note_revision(layout.vault_root), outcome)
    observed = seed.observe("nightly", layout, manifest, clock())
    assert observed["note_revision_verified"] is None
    outcomes, _ = grade_fixture(fixture_for("nightly"), observed)
    assert outcomes["o001"] == "failed"


def test_check_for_an_older_note_revision_is_observed_absent(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    _write_check(layout.vault_root, "0" * 64)
    observed = seed.observe("nightly", layout, manifest, clock())
    assert observed["note_revision_verified"] is None


def test_note_edited_after_its_check_is_observed_absent(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    _write_check(layout.vault_root, _note_revision(layout.vault_root))
    note = layout.vault_root / seed.NOTE_RELATIVE
    note.write_text(note.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    observed = seed.observe("nightly", layout, manifest, clock())
    assert observed["note_revision_verified"] is None


def test_unreadable_check_state_omits_the_key(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    check_path = nv.note_check_state_path(layout.vault_root)
    check_path.parent.mkdir(parents=True, exist_ok=True)
    check_path.write_text("{not json", encoding="utf-8")
    observed = seed.observe("nightly", layout, manifest, clock())
    assert "note_revision_verified" not in observed
    outcomes, _ = grade_fixture(fixture_for("nightly"), observed)
    assert outcomes["o001"] == "unknown"


def test_seeded_decide_you_row_still_queued_is_awaiting_human(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    assert seed.observe("nightly", layout, manifest, clock())[
        "pending_proposal_disposition"
    ] == seed.SYMBOL_AWAITING_HUMAN


def test_seeded_row_removed_or_reassigned_is_observed_absent(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path)
    queue = layout.vault_root / PROPOSALS_RELATIVE
    text = queue.read_text(encoding="utf-8")
    queue.write_text(text.replace("decide: you", "decide: nightly"), encoding="utf-8")
    assert (
        seed.observe("nightly", layout, manifest, clock())["pending_proposal_disposition"] is None
    )
    queue.unlink()
    assert (
        seed.observe("nightly", layout, manifest, clock())["pending_proposal_disposition"] is None
    )


def test_weekly_marker_equal_to_dispatch_utc_date_is_symbolic(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path, "weekly_due")
    _write_marker(layout.vault_root, "2026-11-25")
    # 23:30 on the 24th in UTC-05:00 is 04:30 on the 25th UTC: the UTC date wins.
    late = datetime(2026, 11, 24, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    observed = seed.observe("weekly_due", layout, manifest, seed.DispatchClock(started_at=late))
    assert observed["weekly_marker"] == seed.SYMBOL_WEEKLY_MARKER


def test_stale_weekly_marker_is_its_actual_bytes(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path, "weekly_due")
    observed = seed.observe("weekly_due", layout, manifest, clock())
    assert observed["weekly_marker"] == seed.SEED_MARKER.encode()


def test_symlinked_log_is_refused_and_the_marker_is_unknown(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path, "weekly_due")
    log = layout.vault_root / CURATION_LOG_RELATIVE
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_bytes(log.read_bytes().replace(b"2026-11-01", b"2026-11-24"))
    log.unlink()
    log.symlink_to(elsewhere)
    observed = seed.observe("weekly_due", layout, manifest, clock(date(2026, 11, 24)))
    assert "weekly_marker" not in observed
    outcomes, _ = grade_fixture(fixture_for("weekly_due"), observed)
    assert outcomes["o003"] == "unknown"


def test_absent_or_markerless_log_is_observed_absent(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path, "weekly_due")
    log = layout.vault_root / CURATION_LOG_RELATIVE
    log.write_text("# no frontmatter\n", encoding="utf-8")
    assert seed.observe("weekly_due", layout, manifest, clock())["weekly_marker"] is None
    log.unlink()
    assert seed.observe("weekly_due", layout, manifest, clock())["weekly_marker"] is None


def test_dispatch_clock_requires_an_aware_timestamp() -> None:
    with pytest.raises(ValueError):
        seed.DispatchClock(started_at=datetime(2026, 11, 24, 9, 0))


def test_nothing_reads_the_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_home() -> Path:
        raise AssertionError("Path.home() must not be read")

    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    layout, manifest = seeded(tmp_path)
    seed.observe("nightly", layout, manifest, clock())
    seed.observe_cursor(layout.vault_root)
    seed.observe_effects(layout.vault_root, "run-a", ["item:a"])


# Cursor observation
# ------------------------------------------------------------


def test_absent_cursor_is_empty_and_absent(tmp_path: Path) -> None:
    observation = seed.observe_cursor(split(tmp_path).vault_root)
    assert observation == seed.CursorObservation(done_keys=frozenset(), status="absent")


def test_present_cursor_reports_its_done_keys(tmp_path: Path) -> None:
    vault = split(tmp_path).vault_root
    _write_state(vault, {"hygiene:vault-index": "2026-11-24", "item:a": "2026-11-24"})
    observation = seed.observe_cursor(vault)
    assert observation.status == "ok"
    assert observation.done_keys == frozenset({"hygiene:vault-index", "item:a"})


def test_corrupt_cursor_is_unreadable_and_emits_no_keys(tmp_path: Path) -> None:
    vault = split(tmp_path).vault_root
    path = curation_run.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    observation = seed.observe_cursor(vault)
    assert observation == seed.CursorObservation(done_keys=frozenset(), status="unreadable")


# Effect observation from receipts
# ------------------------------------------------------------


def test_effect_with_no_journal_is_zero(tmp_path: Path) -> None:
    vault = split(tmp_path).vault_root
    assert seed.observe_effects(vault, "run-a", ["item:a"]) == {"item:a": 0}


def test_one_applied_receipt_for_the_run_and_key_counts_one(tmp_path: Path) -> None:
    vault = split(tmp_path).vault_root
    _write_receipts(vault, [_receipt("r1", "applied", "run-a", "item:a")])
    assert seed.observe_effects(vault, "run-a", ["item:a", "item:b"]) == {
        "item:a": 1,
        "item:b": 0,
    }


def test_two_applied_receipts_with_different_ids_for_one_key_count_two(tmp_path: Path) -> None:
    # The duplicate-effect case the oracle must reject: expected exactly one.
    vault = split(tmp_path).vault_root
    _write_receipts(
        vault,
        [
            _receipt("r1", "applied", "run-a", "item:a"),
            _receipt("r2", "applied", "run-a", "item:a"),
        ],
    )
    assert seed.observe_effects(vault, "run-a", ["item:a"]) == {"item:a": 2}


def test_latest_row_per_receipt_id_is_what_counts(tmp_path: Path) -> None:
    vault = split(tmp_path).vault_root
    _write_receipts(
        vault,
        [
            _receipt("r1", "prepared", "run-a", "item:a"),
            _receipt("r1", "applied", "run-a", "item:a"),
            _receipt("r2", "applied", "run-a", "item:b"),
            _receipt("r2", "undone", "run-a", "item:b"),
        ],
    )
    assert seed.observe_effects(vault, "run-a", ["item:a", "item:b"]) == {
        "item:a": 1,
        "item:b": 0,
    }


def test_receipt_from_another_run_is_not_counted(tmp_path: Path) -> None:
    vault = split(tmp_path).vault_root
    _write_receipts(vault, [_receipt("r1", "applied", "run-other", "item:a")])
    assert seed.observe_effects(vault, "run-a", ["item:a"]) == {"item:a": 0}


def test_prepared_only_receipt_counts_zero_not_unknown(tmp_path: Path) -> None:
    vault = split(tmp_path).vault_root
    _write_receipts(vault, [_receipt("r1", "prepared", "run-a", "item:a")])
    assert seed.observe_effects(vault, "run-a", ["item:a"]) == {"item:a": 0}


def test_unreadable_journal_is_unknown_for_every_key(tmp_path: Path) -> None:
    vault = split(tmp_path).vault_root
    journal = memory_receipts.journal_path(vault)
    journal.mkdir(parents=True)  # a directory where the journal file should be
    assert seed.observe_effects(vault, "run-a", ["item:a", "item:b"]) == {
        "item:a": None,
        "item:b": None,
    }


def test_effect_counts_need_a_proven_run_id(tmp_path: Path) -> None:
    # An empty holder would match every receipt written without a run id.
    vault = split(tmp_path).vault_root
    with pytest.raises(ValueError):
        seed.observe_effects(vault, "", ["item:a"])


def test_observe_never_emits_effect_or_index_keys(tmp_path: Path) -> None:
    layout, manifest = seeded(tmp_path, "weekly_due")
    _write_receipts(layout.vault_root, [_receipt("r1", "applied", "run-a", "item:a")])
    observed = seed.observe("weekly_due", layout, manifest, clock())
    assert set(observed) == {"guide_bytes", "weekly_marker"}


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


def _write_state(vault: Path, done: dict[str, str]) -> None:
    path = curation_run.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"version": 1, "done_keys": done, "lease": {}, "last_run": {}}
    path.write_text(json.dumps(document), encoding="utf-8")


def _receipt(rid: str, status: str, run_id: str, item_key: str) -> dict[str, Any]:
    return {"id": rid, "status": status, "run_id": run_id, "item_key": item_key}


def _write_receipts(vault: Path, rows: list[dict[str, Any]]) -> None:
    journal = memory_receipts.journal_path(vault)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


# Item keys and the vault digest
# ------------------------------------------------------------


def test_only_budget_continuation_has_item_keys(tmp_path: Path) -> None:
    for scenario in SCENARIOS:
        _, manifest = seeded(tmp_path / scenario, scenario)
        assert (len(manifest.item_keys) == 2) == (scenario == "budget_continuation"), scenario


def test_item_keys_are_the_products_first_two_planned_keys(tmp_path: Path) -> None:
    # The seed's keys must be what the real worklist plans first, not a private copy of the hash.
    from datetime import date

    from ciao import curation_run
    from ciao.entity_types import load_entity_types

    layout, manifest = seeded(tmp_path, "budget_continuation")
    worklist = curation_run.build_worklist(
        vault_root=layout.vault_root,
        guide_path=layout.agent_root / seed.GUIDE_RELATIVE,
        category_registry=load_entity_types(layout.vault_root),
        workspace_dir=layout.agent_root,
        today=date(2026, 11, 24),
        workspace="personal",
    )
    proposals = next(item for item in worklist.items if item.pass_id == curation_run.PASS_PROPOSALS)
    assert proposals.keys == manifest.item_keys
    plan = curation_run.plan_run(worklist, curation_run.RunBudget(max_items=1))
    assert plan.planned[0].keys == (manifest.item_keys[0],), "one-item budget takes the first"
    done = frozenset({manifest.item_keys[0]})
    resumed = curation_run.build_worklist(
        vault_root=layout.vault_root,
        guide_path=layout.agent_root / seed.GUIDE_RELATIVE,
        category_registry=load_entity_types(layout.vault_root),
        workspace_dir=layout.agent_root,
        today=date(2026, 11, 24),
        done_keys=done,
        workspace="personal",
    )
    again = curation_run.plan_run(resumed, curation_run.RunBudget(max_items=1))
    assert again.planned[0].keys == (manifest.item_keys[1],), "attempt 2 resumes with the second"


def test_item_keys_are_part_of_the_fixture_digest(tmp_path: Path) -> None:
    _, manifest = seeded(tmp_path, "budget_continuation")
    files = seed._seed_files("budget_continuation")
    assert manifest.fixture_sha256 == seed.fixture_digest("budget_continuation", files, manifest.item_keys)
    assert manifest.fixture_sha256 != seed.fixture_digest("budget_continuation", files)
    assert manifest.fixture_sha256 != seed.fixture_digest(
        "budget_continuation", files, tuple(reversed(manifest.item_keys))
    )


def test_scenarios_without_item_keys_keep_their_digest_shape(tmp_path: Path) -> None:
    files = seed._seed_files("nightly")
    assert seed.fixture_digest("nightly", files) == seed.fixture_digest("nightly", files, ())
