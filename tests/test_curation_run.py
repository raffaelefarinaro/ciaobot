"""The nightly curation worklist, run budget and lease.

Issue #461: the curation skill asked a model to answer questions files already
answer (is the queue empty, is the region at 85%, is the weekly marker seven
days old), and nothing stopped two runs — or a run and an archiving chat —
from rewriting the same region from two stale reads.

These tests pin the three claims that replaced that: an idle workspace is
computable as idle without a model turn, a budget-limited run resumes rather
than restarts, and a held lease is honoured by the second run *and* by
archive-time auto-apply.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ciao import curation_run as cr


MEMORY_START = "<!-- ciao:memory:start -->"
MEMORY_END = "<!-- ciao:memory:end -->"
PROFILE_START = "<!-- ciao:profile:start -->"
PROFILE_END = "<!-- ciao:profile:end -->"


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every lock file out of the shared temp root the install uses."""
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


def _guide(tmp_path: Path, *, memory: str = "", profile: str = "") -> Path:
    from ciao.memory_tool import ensure_regions

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Workspace\n", encoding="utf-8")
    ensure_regions(guide)
    if memory or profile:
        from ciao.memory_tool import write_region

        if memory:
            write_region(guide, "memory", [memory])
        if profile:
            write_region(guide, "profile", [profile])
    return guide


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    return vault


def _fresh_log(vault: Path, *, last_full_pass: str) -> None:
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        f"---\nlast_full_pass: {last_full_pass}\n---\n\n# Curation log\n",
        encoding="utf-8",
    )


# ── Worklist ──────────────────────────────────────────────────────────────


def test_a_fresh_idle_workspace_has_nothing_to_do(tmp_path: Path) -> None:
    """Acceptance: the empty case is decided in code, not by a model turn."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert worklist.empty
    assert worklist.weekly_due is False
    assert worklist.items == ()


def test_every_mechanical_signal_lands_in_the_worklist(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 1).isoformat())

    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "# Memory proposals\n\n- [people Ada] Ada runs the release train.\n",
        encoding="utf-8",
    )
    (vault / cr.LEARNINGS_RELATIVE).write_text(
        "# Learnings\n\n## Active\n"
        "- [pin-the-node-version] [2026-01-01 → 2026-09-01] (x4) Pin the Node version.\n",
        encoding="utf-8",
    )
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    (vault / cr.SKILL_PROPOSALS_RELATIVE / "deploy.md").write_text("x", encoding="utf-8")
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        f"---\nlast_full_pass: 2026-09-01\n---\n\n{'x' * (cr.LOG_ROTATION_BYTES + 1)}",
        encoding="utf-8",
    )

    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    by_pass = {item.pass_id: item for item in worklist.items}
    assert set(by_pass) == {
        cr.PASS_PROPOSALS,
        cr.PASS_LEARNINGS,
        cr.PASS_HYGIENE,
        cr.PASS_GUIDE,
        cr.PASS_LOGS,
        cr.PASS_SKILL_PROPOSALS,
    }
    assert worklist.weekly_due is True
    assert by_pass[cr.PASS_HYGIENE].keys == (cr.HYGIENE_INDEX_KEY, cr.HYGIENE_AUDIT_KEY)
    # Pass order is the skill's order, so a short run always drops the tail.
    assert [item.pass_id for item in worklist.items] == [
        cr.PASS_PROPOSALS,
        cr.PASS_LEARNINGS,
        cr.PASS_HYGIENE,
        cr.PASS_GUIDE,
        cr.PASS_LOGS,
        cr.PASS_SKILL_PROPOSALS,
    ]


def test_guide_review_is_weekly_but_not_a_required_hygiene_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guide-body pass rides the weekly marker, not the vault-hygiene gate.

    The guide review is a model judgment that `curation-begin` cannot score
    mechanically, so it shows up whenever the weekly pass is due. But it must
    not be one of the two keys that gate `last_full_pass`: an over-budget run
    that never reached the guide must not be told it completed the review it
    skipped. Recording it should also keep next week's guide pass due.
    """
    from ciao.cli import _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")

    by_pass = {
        item.pass_id: item
        for item in cr.build_worklist(
            vault_root=vault,
            guide_path=guide,
            workspace_dir=tmp_path,
            today=date(2026, 9, 19),
        ).items
    }
    assert by_pass[cr.PASS_GUIDE].weekly is True
    assert by_pass[cr.PASS_GUIDE].keys == (cr.item_key(cr.PASS_GUIDE, "guide-body"),)
    guide_keys = {key for item in by_pass.values() if item.pass_id == cr.PASS_GUIDE for key in item.keys}
    assert guide_keys.isdisjoint(cr.REQUIRED_HYGIENE_KEYS)
    assert cr.PASS_GUIDE in cr.PASS_ORDER

    cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, [cr.item_key(cr.PASS_GUIDE, "guide-body")])
    _capture(monkeypatch)
    _curation_end_command(_args(tmp_path, vault, guide, holder="nightly", status="ok"))

    # Recording only the guide review never advances the weekly marker.
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"


def test_queued_region_facts_are_not_work(tmp_path: Path) -> None:
    """`[memory]`/`[profile]` rows stay queued for the user by contract.

    Counting them as work would make a workspace with one pending
    cross-project fact look busy every night forever, and the run would spend
    a model turn re-reading a row it is forbidden to act on.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "- [memory] The user prefers uv over pip.\n- [profile] Writes in British English.\n",
        encoding="utf-8",
    )

    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert worklist.empty


def test_proposals_sharing_one_sentence_are_separate_work(tmp_path: Path) -> None:
    """Acceptance: finishing one row must not retire the rows that read alike.

    The work-item key hashed the bullet text alone, so two actionable rows with
    identical text — a different kind, a different destination, or a plain
    duplicate — collided. Completing the first before a failure or a budget
    boundary persisted that one key, and the next `build_worklist` filtered out
    every remaining row sharing it: an empty queue reported with unprocessed
    proposals still in the file.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "- [people Ada] Ships on Fridays.\n"
        "- [project docs/Release.md] Ships on Fridays.\n"
        "- [project docs/Release.md] Ships on Fridays.\n",
        encoding="utf-8",
    )

    def plan() -> cr.Worklist:
        return cr.build_worklist(
            vault_root=vault,
            guide_path=guide,
            workspace_dir=tmp_path,
            today=date(2026, 9, 19),
            done_keys=frozenset(cr.load_state(vault).done_keys),
        )

    first = plan()
    keys = first.items[0].keys
    assert first.items[0].pass_id == cr.PASS_PROPOSALS
    # Three rows, three identities: kind, destination and the duplicate ordinal
    # all separate them even though the sentence is one and the same.
    assert len(set(keys)) == 3

    cr.record_done(vault, [keys[0]])
    remaining = plan()

    assert not remaining.empty
    assert remaining.items[0].keys == keys[1:]


def test_a_missing_or_malformed_marker_leaves_the_weekly_pass_due(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)

    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == ""
    _fresh_log(vault, last_full_pass="not-a-date")
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == ""

    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    assert worklist.weekly_due is True


def test_a_region_over_the_consolidation_threshold_is_work(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    guide = _guide(tmp_path, memory="The user ships on Fridays.")

    clear = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    assert clear.empty

    full = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
        memory_char_limit=30,
    )
    assert [item.pass_id for item in full.items] == [cr.PASS_REGIONS]
    assert "ciao:memory" in full.items[0].reason


def test_an_expired_entry_is_work_even_well_under_cap(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    guide = _guide(tmp_path, memory="Standup is at 9. [expires: 2026-01-01]")

    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert [item.pass_id for item in worklist.items] == [cr.PASS_REGIONS]
    assert "expired" in worklist.items[0].reason


def test_resolved_learnings_are_not_replanned(tmp_path: Path) -> None:
    """A promoted learning must not be promoted again the next night."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.LEARNINGS_RELATIVE).write_text(
        "# Learnings\n\n## Active\n\n"
        "## Promoted / Resolved\n"
        "- [pin-the-node-version] [2026-01-01 → 2026-09-01] (x4) Pin the Node version.\n",
        encoding="utf-8",
    )

    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert worklist.empty


# ── Budget and cursor ─────────────────────────────────────────────────────


def _worklist_of(*counts: int) -> cr.Worklist:
    items = tuple(
        cr.WorklistItem(
            pass_id=pass_id,
            label=pass_id,
            reason="test",
            keys=tuple(f"{pass_id}:{index}" for index in range(count)),
        )
        for pass_id, count in zip(cr.PASS_ORDER, counts)
        if count
    )
    return cr.Worklist(items=items, weekly_due=False, last_full_pass="", generated_at="")


def test_the_budget_splits_a_pass_instead_of_deferring_it_whole() -> None:
    """A 200-item queue must make progress every night, not stall forever."""
    plan = cr.plan_run(_worklist_of(10), cr.RunBudget(max_items=4))

    assert plan.planned_count == 4
    assert plan.deferred_count == 6
    assert plan.planned[0].keys == ("proposals:0", "proposals:1", "proposals:2", "proposals:3")
    assert plan.deferred[0].keys[0] == "proposals:4"
    assert "resumes next run" in plan.deferred[0].reason


def test_the_budget_spends_passes_in_order() -> None:
    plan = cr.plan_run(_worklist_of(2, 2, 2), cr.RunBudget(max_items=3))

    assert [item.pass_id for item in plan.planned] == [cr.PASS_PROPOSALS, cr.PASS_REGIONS]
    assert [item.pass_id for item in plan.deferred] == [cr.PASS_REGIONS, cr.PASS_AUDIT]


def test_a_budget_limited_run_resumes_at_the_remainder(tmp_path: Path) -> None:
    """Acceptance: no duplicate work and no lost items across two runs."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    for name in ("a", "b", "c"):
        (vault / cr.SKILL_PROPOSALS_RELATIVE / f"{name}.md").write_text("x", encoding="utf-8")

    first = cr.build_worklist(
        vault_root=vault, guide_path=guide, workspace_dir=tmp_path, today=date(2026, 9, 19)
    )
    plan = cr.plan_run(first, cr.RunBudget(max_items=2))
    assert plan.planned_count == 2 and plan.deferred_count == 1
    done = [key for item in plan.planned for key in item.keys]
    cr.record_done(vault, done)

    second = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 20),
        done_keys=frozenset(cr.load_state(vault).done_keys),
    )
    remaining = [key for item in second.items for key in item.keys]

    # Nothing repeated, nothing lost: the two runs together cover all three.
    assert set(remaining).isdisjoint(done)
    assert sorted(remaining + done) == sorted(
        key for item in first.items for key in item.keys
    )


def test_a_worklist_whose_every_key_is_done_reports_empty(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    (vault / cr.SKILL_PROPOSALS_RELATIVE / "a.md").write_text("x", encoding="utf-8")

    worklist = cr.build_worklist(
        vault_root=vault, guide_path=guide, workspace_dir=tmp_path, today=date(2026, 9, 19)
    )
    cr.record_done(vault, [key for item in worklist.items for key in item.keys])

    again = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
        done_keys=frozenset(cr.load_state(vault).done_keys),
    )
    assert again.empty


def test_end_run_prunes_keys_whose_work_has_left_the_vault(tmp_path: Path) -> None:
    """An unbounded cursor would also suppress a later item with the same subject."""
    vault = _vault(tmp_path)
    cr.record_done(vault, ["proposals:gone", "proposals:live"])

    cr.end_run(vault, live_keys=frozenset({"proposals:live"}))

    assert set(cr.load_state(vault).done_keys) == {"proposals:live"}


def test_end_run_records_counts_so_a_no_op_is_not_a_failure(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    summary = cr.end_run(
        vault, status="failed", planned=5, completed=2, deferred=3, reasons=["os-audit exit 2"]
    )

    assert summary["status"] == "failed"
    assert (summary["planned"], summary["completed"], summary["deferred"]) == (5, 2, 3)
    assert cr.load_state(vault).last_run["reasons"] == ["os-audit exit 2"]


# ── Lease ─────────────────────────────────────────────────────────────────


def test_a_second_curation_run_is_refused_while_the_first_holds_the_lease(
    tmp_path: Path,
) -> None:
    """Acceptance: the contended path, not just the happy one."""
    vault = _vault(tmp_path)
    cr.begin_run(vault, holder="run-a")

    with pytest.raises(cr.CurationBusy) as excinfo:
        cr.begin_run(vault, holder="run-b")

    assert "run-a" in str(excinfo.value)


def test_the_lease_is_free_again_once_the_first_run_ends(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    lease = cr.begin_run(vault, holder="run-a")
    cr.end_run(vault, holder=lease.holder)

    second = cr.begin_run(vault, holder="run-b")

    assert second.holder == "run-b"


def test_an_expired_lease_does_not_wedge_the_vault(tmp_path: Path) -> None:
    """A crashed run releases its lease by expiring; nothing else can."""
    vault = _vault(tmp_path)
    started = datetime(2026, 9, 19, 0, 1, tzinfo=UTC)
    cr.begin_run(vault, holder="crashed", ttl_s=60, now=started)

    assert cr.active_lease(vault, now=started + timedelta(seconds=30)) is not None
    assert cr.active_lease(vault, now=started + timedelta(seconds=120)) is None
    cr.begin_run(vault, holder="next", now=started + timedelta(seconds=120))


def test_an_unreadable_expiry_reads_as_expired(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    cr.begin_run(vault, holder="run-a")
    path = cr.state_path(vault)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lease"]["expires_at"] = "whenever"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cr.active_lease(vault) is None
    cr.begin_run(vault, holder="run-b")


def test_a_run_that_lost_its_lease_cannot_keep_stamping_work_done(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    started = datetime(2026, 9, 19, 0, 1, tzinfo=UTC)
    cr.begin_run(vault, holder="run-a", ttl_s=60, now=started)
    cr.begin_run(vault, holder="run-b", now=started + timedelta(seconds=120))

    with pytest.raises(cr.CurationBusy):
        cr.record_done(vault, ["proposals:x"], holder="run-a")
    assert cr.load_state(vault).done_keys == {}


def test_renew_extends_only_the_holders_own_lease(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    started = datetime(2026, 9, 19, 0, 1, tzinfo=UTC)
    cr.begin_run(vault, holder="run-a", ttl_s=60, now=started)

    assert cr.renew_run(vault, holder="run-b", ttl_s=60, now=started) is False
    assert cr.renew_run(vault, holder="run-a", ttl_s=600, now=started) is True
    assert cr.active_lease(vault, now=started + timedelta(seconds=300)) is not None


# ── Weekly marker ─────────────────────────────────────────────────────────


def test_the_weekly_marker_advances_only_after_both_required_checks(
    tmp_path: Path,
) -> None:
    """Acceptance: an unreliable scan leaves the full pass due."""
    vault = _vault(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")

    cr.record_done(vault, [cr.HYGIENE_INDEX_KEY])
    assert cr.advance_full_pass(vault, today=date(2026, 9, 19)) is False
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"

    cr.record_done(vault, [cr.HYGIENE_AUDIT_KEY])
    assert cr.advance_full_pass(vault, today=date(2026, 9, 19)) is True
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-19"


def test_advancing_the_marker_keeps_the_existing_log_body(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        "---\nlast_full_pass: 2026-09-01\ntags: [ciao]\n---\n\n# Curation log\n\n## 2026-09-01\nDid things.\n",
        encoding="utf-8",
    )
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))

    cr.advance_full_pass(vault, today=date(2026, 9, 19))

    text = (vault / cr.CURATION_LOG_RELATIVE).read_text(encoding="utf-8")
    assert "tags: [ciao]" in text
    assert "Did things." in text
    assert text.count("last_full_pass") == 1


# ── Serialization against archive-time writes ─────────────────────────────


def test_archive_auto_apply_stands_down_while_curation_holds_the_lease(
    tmp_path: Path,
) -> None:
    """Acceptance: a curation run cannot overlap archive-time region writes.

    The fact is not lost — it falls through to the queue the curation run is
    about to work, which is the path uncertain facts already take.
    """
    from ciao import memory_proposals as mp

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    archive = tmp_path / "chat-2026-09-19.md"
    archive.write_text(
        "# Chat\n\n## Session insights\n\n### User corrections\n"
        "- The user deploys with uv, never pip. [memory]\n",
        encoding="utf-8",
    )

    cr.begin_run(vault, holder="nightly")
    mp.proposals_from_archive(
        archive, vault, auto_promote_memory=True, guide_path=guide
    )

    from ciao.memory_tool import read_region

    entries, _ = read_region(guide, "memory")
    assert entries == []
    queued = mp.list_proposals(vault / cr.PROPOSALS_RELATIVE)
    assert any("uv" in row["text"] for row in queued)


def test_archive_auto_apply_still_writes_when_no_run_holds_the_lease(
    tmp_path: Path,
) -> None:
    """The gate must be the lease, not a blanket disabling of auto-apply."""
    from ciao import memory_proposals as mp

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    archive = tmp_path / "chat-2026-09-19.md"
    archive.write_text(
        "# Chat\n\n## Session insights\n\n### User corrections\n"
        "- The user deploys with uv, never pip. [memory]\n",
        encoding="utf-8",
    )

    mp.proposals_from_archive(
        archive, vault, auto_promote_memory=True, guide_path=guide
    )

    from ciao.memory_tool import read_region

    entries, _ = read_region(guide, "memory")
    assert any("uv" in entry for entry in entries)


def test_the_gate_never_breaks_archiving_when_the_state_is_unreadable(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    cr.state_path(vault).write_text("{not json", encoding="utf-8")

    assert cr.curation_in_progress(vault) is False


# ── CLI ───────────────────────────────────────────────────────────────────


def _args(tmp_path: Path, vault: Path, guide: Path, **overrides: object) -> object:
    import argparse

    base = dict(
        workspace=tmp_path,
        vault_root=vault,
        guide=guide,
        max_items=None,
        max_seconds=None,
        json=True,
        holder="",
        key=[],
        status="ok",
        planned=0,
        completed=0,
        reason=[],
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    printed: list[str] = []
    monkeypatch.setattr("sys.stdout.write", lambda text: printed.append(text) or len(text))
    return printed


def test_curation_begin_exits_temp_fail_when_another_run_holds_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: the scheduled run must be able to tell "busy" from "broken"."""
    from ciao.cli import CURATION_BUSY_EXIT, _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    cr.begin_run(vault, holder="run-a")

    code = _curation_begin_command(_args(tmp_path, vault, guide, holder="run-b"))

    assert code == CURATION_BUSY_EXIT


def test_curation_begin_releases_the_lease_on_a_quiet_night(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle run must not keep archive-time auto-apply standing down."""
    from ciao.cli import _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    printed = _capture(monkeypatch)

    code = _curation_begin_command(_args(tmp_path, vault, guide, holder="nightly"))

    assert code == 0
    assert json.loads("".join(printed))["empty"] is True
    assert cr.active_lease(vault) is None


def test_curation_end_leaves_the_weekly_pass_due_after_a_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    _capture(monkeypatch)

    _curation_end_command(_args(tmp_path, vault, guide, holder="nightly", status="failed"))

    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"
    assert cr.active_lease(vault) is None


def test_curation_end_advances_the_marker_and_forgets_the_weekly_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping the hygiene keys would make next week's pass look already done."""
    from ciao.cli import _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    _capture(monkeypatch)

    _curation_end_command(_args(tmp_path, vault, guide, holder="nightly", status="ok"))

    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) != "2026-09-01"
    assert set(cr.load_state(vault).done_keys) == set()


def _stale_holder_replaced(vault: Path) -> None:
    """Leave `run-a`'s lease expired and `run-b` holding the vault."""
    expired = datetime.now(UTC) - timedelta(hours=2)
    cr.begin_run(vault, holder="run-a", ttl_s=60, now=expired)
    cr.begin_run(vault, holder="run-b")


def test_follow_up_commands_refuse_to_run_without_the_lease_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: an ownership check that is optional in practice is no lease.

    `record_done` and `end_run` skip their check when the holder is empty, and
    the stock workflow used to invoke both without one. An over-budget run
    therefore kept stamping work done after its lease expired, and its
    `curation-end` cleared the lease the *newer* run was holding — exactly the
    overlap the lease exists to prevent.
    """
    from ciao.cli import _curation_end_command, _curation_progress_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _stale_holder_replaced(vault)
    _capture(monkeypatch)

    progress = _curation_progress_command(
        _args(tmp_path, vault, guide, holder="", key=["proposals:deadbeef"])
    )

    # The stale run stamps nothing done: the next run must not be told that
    # items nobody verified were handled.
    assert cr.load_state(vault).done_keys == {}
    assert progress == 2

    ended = _curation_end_command(_args(tmp_path, vault, guide, holder=""))

    # And it cannot release the lease the newer run is holding.
    assert (cr.active_lease(vault) or {})["holder"] == "run-b"
    assert ended == 2


def test_the_parser_will_not_let_a_follow_up_command_omit_the_holder() -> None:
    """The flag is required at parse time, not merely honoured when present."""
    from ciao.cli import build_parser

    parser = build_parser()
    for command in ("curation-progress", "curation-end"):
        with pytest.raises(SystemExit):
            parser.parse_args([command])
        assert parser.parse_args([command, "--holder", "run-a"]).holder == "run-a"


def test_a_rejected_run_cannot_advance_the_weekly_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: the marker moves only for the run that still holds the lease.

    The marker used to be stamped before `end_run` checked ownership, so a run
    whose lease had expired — one `curation-end` then rejected with 75 —
    suppressed the weekly hygiene passes for the next seven days on its way
    out.
    """
    from ciao.cli import CURATION_BUSY_EXIT, _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    _stale_holder_replaced(vault)
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    _capture(monkeypatch)

    code = _curation_end_command(
        _args(tmp_path, vault, guide, holder="run-a", status="ok")
    )

    assert code == CURATION_BUSY_EXIT
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"
    # And the run it was rejected in favour of still holds the lease.
    assert (cr.active_lease(vault) or {})["holder"] == "run-b"


# ── Shipped instructions ──────────────────────────────────────────────────


def _stock(relative: str) -> str:
    from importlib import resources

    return resources.files("ciao.stock").joinpath(relative).read_text(encoding="utf-8")


def test_the_skill_starts_from_the_computed_worklist() -> None:
    """The agent must not re-derive by hand what pass 0 already decided."""
    skill = _stock("skills/memory-curation/SKILL.md")

    assert "ciao curation-begin --json" in skill
    assert "ciao curation-progress" in skill
    assert "ciao curation-end" in skill
    # The three decisions the deterministic path takes away from the model.
    assert "Exit 75 means another run holds the lease" in skill
    assert '`"empty": true`' in skill
    assert "`deferred` is next run's work" in skill
    # The weekly marker is stamped by code, gated on both required checks.
    assert "--key hygiene:vault-index" in skill
    assert "--key hygiene:os-audit" in skill
    assert "You never write that marker by hand." in skill
    # Every follow-up command carries the holder pass 0 returned; without it
    # the ownership check is skipped and the lease serializes nothing.
    assert "ciao curation-progress --holder <lease.holder>" in skill
    assert "ciao curation-end --holder <lease.holder>" in skill
    assert skill.count("ciao curation-progress --key") == 0


def test_the_schedule_prompt_points_at_the_worklist_first() -> None:
    schedules = json.loads(_stock("schedules.json"))
    prompt = next(
        entry["prompt"]
        for entry in schedules["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )

    assert "ciao curation-begin --json" in prompt
    assert "one-line no-op" in prompt
    # The lease only serializes anything if the follow-up commands carry it.
    assert "`lease.holder`" in prompt and "--holder" in prompt
    # test_stock_package pins this ceiling; keep the dispatcher a dispatcher.
    assert len(prompt) < 1200
