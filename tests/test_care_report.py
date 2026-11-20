"""The pure care report fold: a closed plan plus durable receipts, no I/O.

Receipts come from the real writers (``commit_region_change``,
``queue_resolution``, ``undo_receipt``) under ``tmp_path``, so the row shapes
under test are the ones the protocol writes. Rows that only differ in id,
status or operation id are cloned from a real row and appended to the journal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ciao import memory_receipts as mr
from ciao import memory_tool as mt
from ciao.care_report import (
    RECEIPT_KINDS,
    CareReportPlan,
    ItemOutcome,
    ReportItem,
    report_plan_from_run,
    summarize_care_run,
)
from ciao.curation_run import (
    PASS_LOGS,
    PASS_STALE_NOTE,
    RunBudget,
    RunPlan,
    Worklist,
    WorklistItem,
    plan_run_with_blocked,
)
from ciao.note_receipts import NOTE_KINDS

WS = "personal"

OUTCOME_NAMES = {
    "blocked",
    "deferred_budget",
    "unfinished",
    "queued",
    "done_unverified",
    "applied",
    "no_change",
    "failed",
    "conflict",
    "rolled_back",
    "undone",
    "incomplete",
}


# ── Real-shape fixtures ───────────────────────────────────────────────────


def _guide(tmp_path: Path) -> Path:
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n\n", encoding="utf-8")
    mt.ensure_regions(guide)
    return guide


def _journal(tmp_path: Path) -> Path:
    return mr.journal_path(tmp_path, None)


def _receipts(tmp_path: Path) -> list[dict[str, Any]]:
    return mr.read_receipts(_journal(tmp_path))


def _write_region(
    tmp_path: Path, guide: Path, entries: list[str], *, op: str, workspace: str = WS
) -> dict[str, Any]:
    return mr.commit_region_change(
        guide,
        "memory",
        entries=entries,
        actor="agent",
        source="care",
        workspace=workspace,
        vault_root=tmp_path,
        fact_text=entries[-1],
        operation_id=op,
    )


def _resolve_queue(
    tmp_path: Path, *, op: str, text: str = "Keep every fact.", promoted: bool = True,
    workspace: str = WS,
) -> dict[str, Any]:
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "## 2026-08-01T00:00:00\n\n"
        f"- [memory] {text}  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    with mr.queue_resolution(
        queue,
        removed_text=text,
        kind="memory",
        promoted=promoted,
        actor="agent",
        source="care",
        workspace=workspace,
        vault_root=tmp_path,
        operation_id=op,
    ) as base:
        queue.write_text("# Memory Proposals\n\n", encoding="utf-8")
    return base


def _append_row(tmp_path: Path, row: dict[str, Any]) -> None:
    journal = _journal(tmp_path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _clone(template: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A real row with some fields changed. The caller always sets ``id``."""
    assert "id" in changes, "a clone needs its own receipt id"
    return {**template, **changes}


def _run_plan(
    *,
    planned: tuple[WorklistItem, ...] = (),
    deferred: tuple[WorklistItem, ...] = (),
    blocked: tuple[WorklistItem, ...] = (),
) -> RunPlan:
    return RunPlan(
        planned=planned, deferred=deferred, budget=RunBudget(), blocked=blocked
    )


def _item(pass_id: str, *keys: str) -> WorklistItem:
    return WorklistItem(pass_id=pass_id, label=pass_id, reason="due", keys=tuple(keys))


def _report(run_plan: RunPlan, ops: dict[str, list[str]] | None = None) -> CareReportPlan:
    return report_plan_from_run(
        run_plan, run_id="run-1", workspace=WS, operation_ids=ops or {}
    )


def _one_planned(key: str = "note:a") -> CareReportPlan:
    return _report(_run_plan(planned=(_item(PASS_STALE_NOTE, key),)), {key: ["op-a"]})


def _by_key(summary: Any) -> dict[str, ItemOutcome]:
    return {outcome.item_key: outcome for outcome in summary.outcomes}


# ── Partition and plan flattening ─────────────────────────────────────────


def test_blocked_key_is_reported_blocked_and_never_as_deferred() -> None:
    worklist = Worklist(
        items=(
            _item(PASS_STALE_NOTE, "note:a", "note:b", "note:c"),
            _item(PASS_LOGS, "log:1"),
        ),
        weekly_due=False,
        last_full_pass="",
        generated_at="",
    )
    run_plan = plan_run_with_blocked(worklist, RunBudget(max_items=2, max_seconds=60))

    report = _report(run_plan)

    assert {i.item_key: i.disposition for i in report.items} == {
        "note:a": "planned",
        "note:b": "planned",
        "note:c": "deferred",
        "log:1": "blocked",
    }
    assert [i.item_key for i in report.items] == ["note:a", "note:b", "note:c", "log:1"]
    summary = summarize_care_run(report, [])
    assert summary.counts["planned"] == 2
    assert summary.counts["deferred_budget"] == 1
    assert summary.counts["blocked"] == 1
    assert _by_key(summary)["log:1"].status == "blocked"


def test_operation_ids_and_reason_are_carried_onto_each_item() -> None:
    report = _report(
        _run_plan(planned=(_item(PASS_STALE_NOTE, "note:a"),)),
        {"note:a": ["op-1", "op-2"]},
    )

    (item,) = report.items
    assert item == ReportItem(
        pass_id=PASS_STALE_NOTE,
        item_key="note:a",
        disposition="planned",
        reason="due",
        operation_ids=("op-1", "op-2"),
    )


def test_outcome_vocabulary_and_counts_are_closed() -> None:
    summary = summarize_care_run(_one_planned(), [])

    assert set(summary.counts) == OUTCOME_NAMES | {"planned"}
    assert all(isinstance(n, int) for n in summary.counts.values())


# ── Missing and deferred evidence ─────────────────────────────────────────


def test_planned_key_without_a_receipt_is_unfinished_never_applied() -> None:
    summary = summarize_care_run(_one_planned(), [])

    outcome = _by_key(summary)["note:a"]
    assert outcome.status == "unfinished"
    assert outcome.receipt_ids == ()
    assert summary.counts["applied"] == 0


def test_key_done_without_a_receipt_is_done_unverified_not_applied() -> None:
    summary = summarize_care_run(_one_planned(), [], done_keys=frozenset({"note:a"}))

    assert _by_key(summary)["note:a"].status == "done_unverified"
    assert summary.counts["applied"] == 0


def test_receipt_for_a_bound_key_decides_over_done_keys(tmp_path: Path) -> None:
    _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
    _resolve_queue(tmp_path, op="op-a")

    summary = summarize_care_run(
        _one_planned(), _receipts(tmp_path), done_keys=frozenset({"note:a"})
    )

    assert _by_key(summary)["note:a"].status == "applied"


# ── Real writer rows ──────────────────────────────────────────────────────


def test_region_and_queue_from_the_real_writers_fold_to_applied(tmp_path: Path) -> None:
    region = _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
    queue = _resolve_queue(tmp_path, op="op-a")

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    outcome = _by_key(summary)["note:a"]
    assert outcome.status == "applied"
    assert set(outcome.receipt_ids) == {region["id"], queue["id"]}
    assert summary.complete is True


def test_no_op_region_with_a_resolved_queue_is_no_change(tmp_path: Path) -> None:
    guide = _guide(tmp_path)
    _write_region(tmp_path, guide, ["Fact one."], op="op-earlier")
    no_op = _write_region(tmp_path, guide, ["Fact one."], op="op-a")
    assert no_op["changed"] is False
    _resolve_queue(tmp_path, op="op-a")

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "no_change"


def test_undone_wins_over_applied_after_a_real_undo(tmp_path: Path) -> None:
    region = _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
    _resolve_queue(tmp_path, op="op-a")
    mr.undo_receipt(region["id"], vault_root=tmp_path)

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    outcome = _by_key(summary)["note:a"]
    assert outcome.status == "undone"
    assert summary.complete is True


def test_approval_with_a_failed_application_stays_failed(tmp_path: Path) -> None:
    _resolve_queue(tmp_path, op="op-a")
    # The shape the except path of commit_region_change writes, with the
    # operation id a bound care write would carry.
    failed = {
        "id": "region-failed",
        "ts": "2026-11-20T00:00:00Z",
        "actor": "agent",
        "source": "care",
        "workspace": WS,
        "kind": "region_apply",
        "guide": str(tmp_path / "CLAUDE.md"),
        "region": "memory",
        "status": mr.FAILED,
        "error": "guide write refused",
        "operation_id": "op-a",
    }
    _append_row(tmp_path, failed)

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "failed"


def test_region_applied_without_its_queue_receipt_is_incomplete(tmp_path: Path) -> None:
    _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "incomplete"
    assert summary.complete is False


def test_region_applied_with_a_prepared_queue_receipt_is_incomplete(tmp_path: Path) -> None:
    _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
    template = _resolve_queue(tmp_path, op="op-template")
    _append_row(
        tmp_path,
        _clone(template, id="queue-prepared-only", status=mr.PREPARED, operation_id="op-a"),
    )

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "incomplete"


def test_promoted_queue_receipt_without_a_guide_receipt_is_unfinished(tmp_path: Path) -> None:
    _resolve_queue(tmp_path, op="op-a", promoted=True)

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "unfinished"


def test_dismissal_with_only_a_queue_receipt_is_applied(tmp_path: Path) -> None:
    _resolve_queue(tmp_path, op="op-a", promoted=False)

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "applied"


def test_queue_prepared_alone_is_queued_and_not_complete(tmp_path: Path) -> None:
    template = _resolve_queue(tmp_path, op="op-template")
    _append_row(
        tmp_path,
        _clone(template, id="queue-prepared-only", status=mr.PREPARED, operation_id="op-a"),
    )

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "queued"
    assert summary.complete is False


def test_rolled_back_and_conflict_rows_keep_their_own_status(tmp_path: Path) -> None:
    queue = _resolve_queue(tmp_path, op="op-a")
    rolled = _clone(queue, id="queue-rolled", status=mr.ROLLED_BACK, operation_id="op-r")
    _append_row(tmp_path, rolled)
    region = _write_region(tmp_path, _guide(tmp_path), ["Fact two."], op="op-c")
    _append_row(
        tmp_path,
        _clone(region, id="region-conflict", status=mr.CONFLICT, operation_id="op-c2"),
    )
    report = _report(
        _run_plan(
            planned=(
                _item(PASS_STALE_NOTE, "note:r"),
                _item(PASS_STALE_NOTE, "note:c"),
            )
        ),
        {"note:r": ["op-r"], "note:c": ["op-c2"]},
    )

    summary = summarize_care_run(report, _receipts(tmp_path))

    assert _by_key(summary)["note:r"].status == "rolled_back"
    assert _by_key(summary)["note:c"].status == "conflict"


# ── Duplicates, unknown values and binding ────────────────────────────────


def test_compatible_duplicate_collapses_to_one_outcome(tmp_path: Path) -> None:
    region = _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
    _resolve_queue(tmp_path, op="op-a")
    _append_row(tmp_path, _clone(region, id="region-copy"))

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    outcome = _by_key(summary)["note:a"]
    assert outcome.status == "applied"
    assert outcome.receipt_ids.count("region-copy") == 1


def test_conflicting_duplicate_is_incomplete(tmp_path: Path) -> None:
    region = _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
    _resolve_queue(tmp_path, op="op-a")
    _append_row(
        tmp_path, _clone(region, id="region-failed-copy", status=mr.FAILED, error="x")
    )

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "incomplete"


def test_unknown_status_is_incomplete(tmp_path: Path) -> None:
    region = _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
    _resolve_queue(tmp_path, op="op-a")
    _append_row(tmp_path, _clone(region, id="weird", status="pending_review"))

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "incomplete"


def test_unknown_kind_is_incomplete(tmp_path: Path) -> None:
    _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
    _resolve_queue(tmp_path, op="op-a")
    _append_row(
        tmp_path, _clone(_receipts(tmp_path)[0], id="odd-kind", kind="region_teleport")
    )

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "incomplete"


def test_receipts_of_another_operation_do_not_leak_into_a_key(tmp_path: Path) -> None:
    # Another workspace and another operation id: neither may change note:a.
    _write_region(tmp_path, _guide(tmp_path), ["Other fact."], op="op-other", workspace="elsewhere")
    _resolve_queue(tmp_path, op="op-other", workspace="elsewhere")

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "unfinished"
    assert _by_key(summary)["note:a"].receipt_ids == ()


def test_bound_row_from_another_workspace_is_incomplete_not_skipped(tmp_path: Path) -> None:
    _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a", workspace="elsewhere")
    _resolve_queue(tmp_path, op="op-a")

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "incomplete"


def test_bound_row_with_an_empty_workspace_is_accepted(tmp_path: Path) -> None:
    _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a", workspace="")
    _resolve_queue(tmp_path, op="op-a")

    summary = summarize_care_run(_one_planned(), _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "applied"


# ── Passthrough and purity ────────────────────────────────────────────────


def test_weekly_mapping_is_carried_untouched_and_never_inferred() -> None:
    weekly = {"last_full_pass": "2026-11-18", "anything": "kept"}

    summary = summarize_care_run(_one_planned(), [], done_keys=frozenset({"note:a"}), weekly=weekly)

    assert summary.weekly is weekly
    assert _by_key(summary)["note:a"].status == "done_unverified"
    assert summarize_care_run(_one_planned(), [], done_keys=frozenset({"note:a"})).weekly is None


def test_the_fold_reads_no_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the fold must not read the journal")

    monkeypatch.setattr(mr, "read_receipts", _forbidden)

    summary = summarize_care_run(_one_planned(), [])

    assert summary.outcomes[0].status == "unfinished"


def test_receipt_kinds_cover_every_kind_the_protocol_writes() -> None:
    from ciao.memory_receipts import UNDOABLE_KINDS

    assert set(UNDOABLE_KINDS) <= RECEIPT_KINDS
    assert set(NOTE_KINDS) <= RECEIPT_KINDS
    assert "region_undo" in RECEIPT_KINDS


def test_complete_is_false_for_any_incomplete_or_queued_outcome(tmp_path: Path) -> None:
    _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
    report = _report(
        _run_plan(planned=(_item(PASS_STALE_NOTE, "note:a"), _item(PASS_STALE_NOTE, "note:b"))),
        {"note:a": ["op-a"], "note:b": ["op-b"]},
    )
    summary = summarize_care_run(report, _receipts(tmp_path))

    assert _by_key(summary)["note:a"].status == "incomplete"
    assert summary.complete is False


# ── Ordering ──────────────────────────────────────────────────────────────


def test_outcomes_follow_plan_order_and_do_not_depend_on_journal_order(tmp_path: Path) -> None:
    # Thirty-one keys: more than the default item budget, so a filter that
    # kept only the first budget's worth would fail here.
    keys = [f"note:{i:02d}" for i in range(31)]
    region = _write_region(tmp_path, _guide(tmp_path), ["Template fact."], op="op-template")
    queue = _resolve_queue(tmp_path, op="op-template")
    rows: list[dict[str, Any]] = []
    for index, key in enumerate(keys):
        rows.append(_clone(region, id=f"region-{index}", operation_id=f"op-{index}"))
        rows.append(
            _clone(queue, id=f"queue-{index}", operation_id=f"op-{index}", status=mr.APPLIED)
        )
    for row in rows:
        _append_row(tmp_path, row)
    report = _report(
        _run_plan(planned=tuple(_item(PASS_STALE_NOTE, k) for k in keys)),
        {k: [f"op-{i}"] for i, k in enumerate(keys)},
    )

    forward = summarize_care_run(report, _receipts(tmp_path))
    backward = summarize_care_run(report, list(reversed(_receipts(tmp_path))))

    assert [o.item_key for o in forward.outcomes] == keys
    assert [o.item_key for o in backward.outcomes] == keys
    assert forward.outcomes == backward.outcomes
    assert forward.counts["applied"] == 31


# ── Complete truth table ──────────────────────────────────────────────────


def _truth_summary(case: str, tmp_path: Path) -> Any:
    one = _one_planned()
    if case == "applied":
        _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
        _resolve_queue(tmp_path, op="op-a")
        return summarize_care_run(one, _receipts(tmp_path))
    if case == "undone":
        region = _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
        _resolve_queue(tmp_path, op="op-a")
        mr.undo_receipt(region["id"], vault_root=tmp_path)
        return summarize_care_run(one, _receipts(tmp_path))
    if case == "unfinished":
        return summarize_care_run(one, [])
    if case == "done_unverified":
        return summarize_care_run(one, [], done_keys=frozenset({"note:a"}))
    if case == "blocked":
        run_plan = _run_plan(blocked=(_item(PASS_LOGS, "note:a"),))
        return summarize_care_run(_report(run_plan), [])
    if case == "incomplete":
        _write_region(tmp_path, _guide(tmp_path), ["Fact one."], op="op-a")
        return summarize_care_run(one, _receipts(tmp_path))
    if case == "queued":
        template = _resolve_queue(tmp_path, op="op-template")
        _append_row(
            tmp_path,
            _clone(template, id="queue-prepared-only", status=mr.PREPARED, operation_id="op-a"),
        )
        return summarize_care_run(one, _receipts(tmp_path))
    raise AssertionError(case)


@pytest.mark.parametrize(
    ("case", "complete"),
    [
        ("applied", True),
        ("undone", True),
        ("unfinished", True),
        ("done_unverified", True),
        ("blocked", True),
        ("incomplete", False),
        ("queued", False),
    ],
)
def test_complete_truth_table(tmp_path: Path, case: str, complete: bool) -> None:
    assert _truth_summary(case, tmp_path).complete is complete
