"""The dormant care-cycle codec, schema v2 (#3061 phase A).

These pin the pure contract that a later phase will persist: strict encode and
decode, obligation projection that never lets an old global hygiene key certify
a new cycle, a dry-run v1 migration that writes nothing, and the recovery
decision across the marker publication and the state save. Every date is fixed
and every vault lives under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from ciao import curation_cycle as cc
from ciao import curation_logs
from ciao import curation_run as cr
from ciao.curation_run import (
    HYGIENE_AUDIT_KEY,
    HYGIENE_INDEX_KEY,
    PASS_GUIDE,
    PASS_HYGIENE,
    PASS_OBSERVATIONS,
    PASS_PROPOSALS,
    PASS_RULES,
    CurationState,
    WorklistItem,
    item_key,
)

PRIOR_BYTES = b"---\nlast_full_pass: 2026-11-09\n---\n\n# Curation log\n"
PRIOR = cc.marker_identity_of(PRIOR_BYTES)
TARGET_BYTES = curation_logs.with_full_pass_marker(PRIOR_BYTES, "2026-11-16")
TARGET = cc.marker_identity_of(TARGET_BYTES)
CID = cc.cycle_identity(PRIOR, "")

GUIDE = cc.GUIDE_KEY
MERGE = cc.MERGE_KEY
RULE = item_key(PASS_RULES, "decisions-a")
OTHER_CID = cc.cycle_identity(TARGET, "")
PROPOSE = item_key(PASS_PROPOSALS, "queued-1")
INDEX = HYGIENE_INDEX_KEY
AUDIT = HYGIENE_AUDIT_KEY

PENDING = cc.PreparedTransition(
    cycle_id=CID,
    prior=PRIOR,
    target_stamp="2026-11-16",
    target_sha256=TARGET.sha256 or "",
    proven_required=(INDEX,),
    phase="prepared",
)


def _state(**overrides: object) -> cc.CycleState:
    base: dict[str, object] = {
        "cycle_id": CID,
        "prior_marker": PRIOR,
        "required": {},
        "optional": {},
        "carried": (),
        "legacy_historical": {},
        "daily": {},
        "lease": {},
        "last_run": {},
        "pending": None,
    }
    base.update(overrides)
    return cc.CycleState(**base)  # type: ignore[arg-type]


def _full_state() -> cc.CycleState:
    return _state(
        required={INDEX: "2026-11-16"},
        optional={GUIDE: "2026-11-16"},
        carried=(RULE,),
        legacy_historical={AUDIT: "2026-11-09"},
        daily={PROPOSE: "2026-11-16"},
        lease={
            "holder": "holder-1",
            "started_at": "2026-11-16T00:01:00+00:00",
            "expires_at": "2026-11-16T02:01:00+00:00",
        },
        last_run={
            "status": "ok",
            "finished_at": "2026-11-16T00:30:00+00:00",
            "planned": 3,
            "completed": 3,
            "deferred": 0,
            "reasons": [],
        },
        pending=PENDING,
    )


def _doc() -> dict[str, object]:
    loaded: dict[str, object] = json.loads(cc.encode_state(_full_state()))
    return loaded


def _text(doc: dict[str, object]) -> str:
    return json.dumps(doc)


# ── Strict codec ──────────────────────────────────────────────────────────


def test_round_trip_is_exact_and_canonical() -> None:
    state = _full_state()
    text = cc.encode_state(state)
    assert cc.decode_state(text) == state
    assert cc.encode_state(cc.decode_state(text)) == text


def test_decode_accepts_the_document_it_encodes_with_no_pending_transition() -> None:
    state = _state(required={INDEX: "2026-11-16"})
    assert cc.decode_state(cc.encode_state(state)) == state


def test_unknown_field_is_refused() -> None:
    doc = _doc()
    doc["extra"] = 1
    with pytest.raises(cc.CycleError):
        cc.decode_state(_text(doc))


def test_missing_field_is_refused() -> None:
    doc = _doc()
    del doc["daily"]
    with pytest.raises(cc.CycleError):
        cc.decode_state(_text(doc))


def test_duplicate_key_is_refused() -> None:
    text = cc.encode_state(_full_state())
    duplicated = text.replace('"version": 2', '"version": 2,\n  "version": 2', 1)
    assert duplicated != text
    with pytest.raises(cc.CycleError):
        cc.decode_state(duplicated)


@pytest.mark.parametrize("bad", ["2026-13-01", "20261116", "2026-11-6", "yesterday"])
def test_bad_or_noncanonical_date_is_refused(bad: str) -> None:
    doc = _doc()
    doc["required"] = {INDEX: bad}  # type: ignore[index]
    with pytest.raises(cc.CycleError):
        cc.decode_state(_text(doc))


def test_bool_is_not_an_int_in_last_run() -> None:
    doc = _doc()
    doc["last_run"]["planned"] = True  # type: ignore[index]
    with pytest.raises(cc.CycleError):
        cc.decode_state(_text(doc))


def test_bool_is_not_an_int_for_the_version() -> None:
    doc = _doc()
    doc["version"] = True
    with pytest.raises(cc.CycleError):
        cc.decode_state(_text(doc))


def test_v1_document_is_refused_with_a_migration_pointer() -> None:
    v1 = {"version": 1, "done_keys": {}, "lease": {}, "last_run": {}}
    with pytest.raises(cc.CycleError, match="migrate"):
        cc.decode_state(json.dumps(v1))


def test_pending_transition_owned_by_another_cycle_is_refused() -> None:
    foreign = cc.PreparedTransition(
        cycle_id=OTHER_CID,
        prior=PRIOR,
        target_stamp="2026-11-16",
        target_sha256=TARGET.sha256 or "",
        proven_required=(INDEX,),
        phase="prepared",
    )
    with pytest.raises(cc.CycleError, match="another cycle"):
        cc.CycleState(
            cycle_id=CID, prior_marker=PRIOR, required={}, optional={}, carried=(),
            legacy_historical={}, daily={}, lease={}, last_run={}, pending=foreign,
        )


def test_pending_transition_owned_by_another_cycle_is_refused_on_decode() -> None:
    doc = _doc()
    doc["pending"]["cycle_id"] = OTHER_CID  # type: ignore[index]
    with pytest.raises(cc.CycleError, match="another cycle"):
        cc.decode_state(_text(doc))


UPPER = "A" + "0" * 63


def test_uppercase_digest_is_refused_for_marker_identity() -> None:
    with pytest.raises(cc.CycleError):
        cc.MarkerIdentity("present", UPPER)


def test_uppercase_digest_is_refused_for_the_target_of_a_transition() -> None:
    with pytest.raises(cc.CycleError):
        cc.PreparedTransition(
            cycle_id=CID, prior=PRIOR, target_stamp="2026-11-16",
            target_sha256=UPPER, proven_required=(INDEX,), phase="prepared",
        )


def test_uppercase_digest_is_refused_by_decode_state() -> None:
    doc = _doc()
    doc["prior_marker"]["sha256"] = UPPER  # type: ignore[index]
    with pytest.raises(cc.CycleError):
        cc.decode_state(_text(doc))
    doc = _doc()
    doc["pending"]["target_sha256"] = UPPER  # type: ignore[index]
    with pytest.raises(cc.CycleError):
        cc.decode_state(_text(doc))


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_numbers_are_refused_on_decode(token: str) -> None:
    text = cc.encode_state(_full_state()).replace('"planned": 3', f'"planned": {token}', 1)
    assert token in text
    with pytest.raises(cc.CycleError):
        cc.decode_state(text)


def test_non_finite_values_never_reach_encode() -> None:
    # A NaN in a timestamp slot is refused when the state is built, so encode
    # only ever sees finite values.
    with pytest.raises(cc.CycleError):
        _state(lease={"holder": "h", "started_at": "2026-11-16T00:01:00+00:00",
                      "expires_at": float("nan")})  # type: ignore[dict-item]


@pytest.mark.parametrize("section", ["lease", "last_run"])
def test_extra_field_in_lease_or_last_run_is_refused(section: str) -> None:
    doc = _doc()
    doc[section]["extra"] = "x"  # type: ignore[index]
    with pytest.raises(cc.CycleError):
        cc.decode_state(_text(doc))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["prior_marker"].update(kind="maybe"),  # type: ignore[union-attr]
        lambda d: d["prior_marker"].update(sha256=None),  # type: ignore[union-attr]
        lambda d: d.update(pending={**d["pending"], "phase": "done"}),  # type: ignore[dict-item, arg-type]
        lambda d: d.update(cycle_id="not-a-cycle"),
        lambda d: d.update(carried=[RULE, RULE]),
    ],
    ids=["marker-kind", "present-without-digest", "pending-phase", "cycle-id", "duplicate-carried"],
)
def test_malformed_nested_values_are_refused(mutate: object) -> None:
    doc = _doc()
    mutate(doc)  # type: ignore[operator]
    with pytest.raises(cc.CycleError):
        cc.decode_state(_text(doc))


def test_invalid_json_is_a_cycle_error_not_a_crash() -> None:
    with pytest.raises(cc.CycleError):
        cc.decode_state("{not json")


# ── Obligations ───────────────────────────────────────────────────────────


def test_old_global_hygiene_keys_do_not_certify_a_new_cycle() -> None:
    state = _state(legacy_historical={INDEX: "2026-11-09", AUDIT: "2026-11-09"})
    projection = cc.project_obligations([], state)
    assert set(projection.required_outstanding) == {INDEX, AUDIT}
    assert not projection.required_complete


def test_cycle_scoped_hygiene_evidence_is_what_completes_required() -> None:
    state = _state(required={INDEX: "2026-11-16", AUDIT: "2026-11-16"})
    projection = cc.project_obligations([], state)
    assert projection.required_outstanding == ()
    assert projection.required_complete


def test_partial_progress_and_cycle_id_survive_a_week_boundary() -> None:
    # Monday 2026-11-16 (ISO week 47): the index is recorded, the audit is not.
    monday = _state(required={INDEX: "2026-11-16"})
    saved = cc.encode_state(monday)
    # Tuesday 2026-11-17 is still week 47; Monday 2026-11-23 is week 48. The
    # codec and projection take no date, so the reload is the whole test.
    next_week = cc.decode_state(saved)
    assert next_week.cycle_id == CID
    assert cc.cycle_identity(PRIOR, next_week.cycle_id) == CID
    projection = cc.project_obligations([], next_week)
    assert projection.required_outstanding == (AUDIT,)


def test_cycle_identity_is_deterministic_and_follows_the_prior_marker() -> None:
    assert cc.cycle_identity(PRIOR, "") == cc.cycle_identity(PRIOR, "")
    assert cc.cycle_identity(PRIOR, "") != cc.cycle_identity(TARGET, "")
    assert cc.cycle_identity(PRIOR, CID) == CID


def test_cycle_identity_refuses_a_malformed_active_id() -> None:
    with pytest.raises(cc.CycleError):
        cc.cycle_identity(PRIOR, "week-47")


def test_optional_work_stays_outstanding_after_the_marker_advances() -> None:
    advanced = _state(
        cycle_id=cc.cycle_identity(TARGET, ""),
        prior_marker=TARGET,
        required={INDEX: "2026-11-16", AUDIT: "2026-11-16"},
        carried=(RULE,),
    )
    items = [
        WorklistItem(PASS_GUIDE, "Guide", "weekly", (GUIDE,), weekly=True),
        WorklistItem(PASS_RULES, "Rules", "weekly", (RULE,), weekly=True),
    ]
    projection = cc.project_obligations(items, advanced)
    assert projection.required_outstanding == ()
    assert set(projection.optional_outstanding) == {GUIDE, RULE}
    assert projection.daily_outstanding == ()


def test_optional_evidence_in_the_cycle_closes_optional_work() -> None:
    state = _state(optional={GUIDE: "2026-11-16"})
    items = [WorklistItem(PASS_GUIDE, "Guide", "weekly", (GUIDE,), weekly=True)]
    assert cc.project_obligations(items, state).optional_outstanding == ()


def test_subject_keys_are_the_keys_the_real_builders_emit(tmp_path: Path) -> None:
    from ciao import observations
    from ciao.learning_records import LearningObservation, LearningRecord, allocate_learning_id, render_learning

    # The guide review and the observation merge are built from subject strings
    # that curation_run does not export; pin the constants against its builders.
    assert [item.keys for item in cr._guide_items(weekly_due=True)] == [(cc.GUIDE_KEY,)]

    def record(text: str, chat: str) -> LearningRecord:
        return LearningRecord(
            learning_id=allocate_learning_id("work", f"obs:{text}"),
            key=text.split()[-1],
            text=f"About the user: {text}",
            first_seen=date(2026, 9, 10),
            last_seen=date(2026, 9, 10),
            count=1,
            observations=(LearningObservation(source=chat),),
        )

    vault = tmp_path / "vault"
    target = vault / observations.OBSERVATIONS_RELATIVE
    target.parent.mkdir(parents=True)
    body = "".join(
        f"{render_learning(r)}\n"
        for r in (record("prefers one line", "a"), record("wants a short summary first", "b"))
    )
    target.write_text(f"{observations.OBSERVATIONS_STUB}{body}\n## Promoted / Resolved\n", encoding="utf-8", newline="")
    items, _note = cr._observation_items(vault, workspace="work", today=date(2026, 9, 19), weekly_due=True)
    keys = [key for item in items for key in item.keys]
    assert cc.MERGE_KEY in keys


def test_merge_key_is_projected_as_weekly_even_when_item_is_not_weekly() -> None:
    items = [
        WorklistItem(
            PASS_OBSERVATIONS,
            "Observations",
            "mixed",
            (MERGE, item_key(PASS_OBSERVATIONS, "propose:abc")),
            weekly=False,
        )
    ]
    projection = cc.project_obligations(items, _state())
    assert MERGE in projection.optional_outstanding
    assert MERGE not in projection.daily_outstanding
    assert item_key(PASS_OBSERVATIONS, "propose:abc") in projection.daily_outstanding


def test_daily_work_is_daily_and_hygiene_items_do_not_leak_into_daily() -> None:
    items = [WorklistItem(PASS_HYGIENE, "Hygiene", "due", (INDEX, AUDIT), weekly=True)]
    projection = cc.project_obligations(items, _state())
    assert projection.daily_outstanding == ()
    assert set(projection.required_outstanding) == {INDEX, AUDIT}


# ── Marker identity and the byte-preserving target ────────────────────────


def test_absent_and_malformed_markers_are_distinguished() -> None:
    absent = cc.marker_identity_of(None)
    malformed = cc.marker_identity_of(b"no frontmatter here\n")
    empty = cc.marker_identity_of(b"")
    assert absent.kind == "absent"
    assert absent.sha256 is None
    assert malformed.kind == "present"
    assert empty.kind == "present"
    assert len({absent, malformed, empty}) == 3


def test_marker_identity_refuses_inconsistent_kinds() -> None:
    with pytest.raises(cc.CycleError):
        cc.MarkerIdentity("absent", "0" * 64)
    with pytest.raises(cc.CycleError):
        cc.MarkerIdentity("present", None)


def test_expected_target_digest_equals_what_the_real_writer_publishes() -> None:
    existing = "---\r\ntitle: log\r\n---\r\n\r\n# Curation log\r\nkept bytes\r\n".encode()
    written = curation_logs.with_full_pass_marker(existing, "2026-11-16")
    expected = cc.expected_target(existing, "2026-11-16")
    assert expected.kind == "present"
    assert expected.sha256 == hashlib.sha256(written).hexdigest()


def test_expected_target_for_an_absent_log_matches_the_writer() -> None:
    written = curation_logs.with_full_pass_marker(None, "2026-11-16")
    assert cc.expected_target(None, "2026-11-16").sha256 == hashlib.sha256(written).hexdigest()


def test_expected_target_refuses_an_invalid_stamp_and_a_repeated_marker() -> None:
    with pytest.raises(cc.CycleError):
        cc.expected_target(None, "2026-02-30")
    repeated = b"---\nlast_full_pass: 2026-11-01\nlast_full_pass: 2026-11-02\n---\n"
    with pytest.raises(cc.CycleError):
        cc.expected_target(repeated, "2026-11-16")


# ── Migration dry-run ─────────────────────────────────────────────────────


def _v1() -> CurationState:
    return CurationState(
        done_keys={
            PROPOSE: "2026-11-15",
            "stale_note:abc": "2026-11-17",
            "stale_note:future": "2026-11-20",
            INDEX: "2026-11-09",
            AUDIT: "2026-11-17",
            GUIDE: "2026-11-09",
        },
        lease={},
        last_run={
            "status": "ok",
            "finished_at": "2026-11-17T00:30:00+00:00",
            "planned": 2,
            "completed": 2,
            "deferred": 0,
            "reasons": [],
        },
    )


def test_migration_keeps_daily_progress_and_last_run_and_drops_future_daily() -> None:
    plan = cc.plan_migration(
        _v1(), last_full_pass="2026-11-09", prior_marker=PRIOR, today=date(2026, 11, 18)
    )
    assert plan.state.daily == {PROPOSE: "2026-11-15", "stale_note:abc": "2026-11-17"}
    assert plan.dropped == ("stale_note:future",)
    assert plan.state.last_run == _v1().last_run


def test_migration_keeps_weekly_keys_as_history_and_never_as_proof() -> None:
    plan = cc.plan_migration(
        _v1(), last_full_pass="2026-11-09", prior_marker=PRIOR, today=date(2026, 11, 18)
    )
    assert plan.state.required == {}
    assert plan.state.optional == {}
    assert plan.state.legacy_historical == {INDEX: "2026-11-09", AUDIT: "2026-11-17", GUIDE: "2026-11-09"}
    # Recorded after the last marker, so it may belong to the unfinished cycle.
    assert plan.ambiguous == (AUDIT,)


def test_migration_without_a_marker_treats_every_weekly_key_as_ambiguous() -> None:
    plan = cc.plan_migration(_v1(), last_full_pass="", prior_marker=cc.marker_identity_of(None), today=date(2026, 11, 18))
    assert set(plan.ambiguous) == {INDEX, AUDIT, GUIDE}


def test_migration_is_deterministic_regardless_of_insertion_order() -> None:
    forward = _v1()
    backward = CurationState(
        done_keys=dict(reversed(list(forward.done_keys.items()))),
        lease=dict(forward.lease),
        last_run=dict(forward.last_run),
    )
    first = cc.plan_migration(forward, last_full_pass="2026-11-09", prior_marker=PRIOR, today=date(2026, 11, 18))
    second = cc.plan_migration(backward, last_full_pass="2026-11-09", prior_marker=PRIOR, today=date(2026, 11, 18))
    assert first == second
    assert cc.encode_state(first.state) == cc.encode_state(second.state)


def test_migration_refuses_a_malformed_marker_stamp() -> None:
    with pytest.raises(cc.CycleError):
        cc.plan_migration(_v1(), last_full_pass="11/09/2026", prior_marker=PRIOR, today=date(2026, 11, 18))


def test_migration_dry_run_creates_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "vault" / "Workspace"
    workspace.mkdir(parents=True)
    (workspace / "Curation-State.json").write_text("{}\n", encoding="utf-8")
    (workspace / "Curation-Log.md").write_bytes(PRIOR_BYTES)

    def snapshot() -> dict[str, bytes | None]:
        return {
            str(p.relative_to(tmp_path)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(tmp_path.rglob("*"))
        }

    before = snapshot()
    cc.plan_migration(_v1(), last_full_pass="2026-11-09", prior_marker=PRIOR, today=date(2026, 11, 18))
    assert snapshot() == before


# ── Recovery across the marker publication and the state save ─────────────


def test_recovery_resumes_when_the_prior_bytes_are_still_there() -> None:
    assert cc.decide_recovery(PENDING, PRIOR) == "resume"


def test_recovery_commits_when_the_target_bytes_are_published() -> None:
    assert cc.decide_recovery(PENDING, TARGET) == "commit"


def test_recovery_blocks_on_drift_truncation_and_unreadable_bytes() -> None:
    drifted = cc.marker_identity_of(PRIOR_BYTES + b"edited by someone else\n")
    truncated = cc.marker_identity_of(TARGET_BYTES[:-7])
    assert cc.decide_recovery(PENDING, drifted) == "block"
    assert cc.decide_recovery(PENDING, truncated) == "block"
    assert cc.decide_recovery(PENDING, None) == "block"


def test_recovery_blocks_an_absent_marker_when_the_prior_was_present() -> None:
    assert cc.decide_recovery(PENDING, cc.marker_identity_of(None)) == "block"


def test_recovery_resumes_an_absent_prior_marker() -> None:
    absent_pending = cc.PreparedTransition(
        cycle_id=CID,
        prior=cc.marker_identity_of(None),
        target_stamp="2026-11-16",
        target_sha256=TARGET.sha256 or "",
        proven_required=(),
        phase="prepared",
    )
    assert cc.decide_recovery(absent_pending, cc.marker_identity_of(None)) == "resume"


def test_recovery_commits_first_when_prior_and_target_digests_are_equal() -> None:
    # Commit is checked before resume, so an identical digest reads as already
    # published. A transition whose target equals its prior is never resumed.
    same = cc.PreparedTransition(
        cycle_id=CID,
        prior=PRIOR,
        target_stamp="2026-11-16",
        target_sha256=PRIOR.sha256 or "",
        proven_required=(INDEX,),
        phase="prepared",
    )
    assert cc.decide_recovery(same, PRIOR) == "commit"


def test_committed_transition_only_accepts_the_target() -> None:
    committed = cc.PreparedTransition(
        cycle_id=CID,
        prior=PRIOR,
        target_stamp="2026-11-16",
        target_sha256=TARGET.sha256 or "",
        proven_required=(AUDIT, INDEX),
        phase="committed",
    )
    assert cc.decide_recovery(committed, TARGET) == "commit"
    assert cc.decide_recovery(committed, PRIOR) == "block"


def test_prepared_transition_refuses_an_unknown_phase_or_unproven_key() -> None:
    with pytest.raises(cc.CycleError):
        cc.PreparedTransition(
            cycle_id=CID, prior=PRIOR, target_stamp="2026-11-16",
            target_sha256=TARGET.sha256 or "", proven_required=(INDEX,), phase="done",  # type: ignore[arg-type]
        )
    with pytest.raises(cc.CycleError):
        cc.PreparedTransition(
            cycle_id=CID, prior=PRIOR, target_stamp="2026-11-16",
            target_sha256=TARGET.sha256 or "", proven_required=(RULE,), phase="prepared",
        )
