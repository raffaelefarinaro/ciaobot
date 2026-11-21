"""The managed memory accept: one operation, two effects, one owned decision.

Every case runs on a ``tmp_path`` vault with its own install registry; the
service definition is stubbed so the trusted registry reads that install only.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from ciao import curation_apply as apply
from ciao import curation_run, memory_proposals, memory_receipts
from ciao.memory_proposals import read_decisions
from contextlib import contextmanager

from ciao import cli
from ciao.memory_tool import read_region, ensure_regions
from ciao.proposal_kinds import parse_bullet
from ciao.proposal_predictions import record_prediction
from ciao.workspace_guide import guide_path

FACT = "Prefers uv over pip for installs."
LINE = f"- [memory] {FACT}  _(from: chat-1)_"


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "workspace"
    vault = root / "memory-vault" / "work"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    (root / ".runtime").mkdir()
    (root / ".runtime" / "workspaces.json").write_text(
        json.dumps([{"name": "work", "vault_root": str(vault), "memory_handover": "all"}]),
        encoding="utf-8",
    )
    guide = guide_path(root)
    guide.write_text("# Guide\n", encoding="utf-8")
    ensure_regions(guide)
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(workspace=str(root)),
    )

    def queue_rows(*lines: str) -> None:
        queue.write_text(
            "# Memory Proposals\n\n## 2026-01-01T00:00:00Z\n\n" + "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    queue_rows(LINE)
    return SimpleNamespace(root=root, vault=vault, queue=queue, guide=guide, queue_rows=queue_rows)


def _crash(*_args, **_kwargs):
    raise RuntimeError("simulated crash between the two effects")


def _lease(vault: Path) -> str:
    return curation_run.begin_run(vault, holder="run", ttl_s=3600).holder


def _predict(queue: Path, text: str = FACT, verdict: str = "accept", kind: str = "memory") -> None:
    record_prediction(queue, kind=kind, text=text, verdict=verdict, reason="because")


def _plan(env, line: str = LINE, holder: str = "") -> apply.ManagedMemoryAcceptIntent:
    intent = apply.plan_memory_accept(
        env.vault,
        workspace="work",
        guide=env.guide,
        bullet=parse_bullet(line),
        holder=holder,
    )
    assert isinstance(intent, apply.ManagedMemoryAcceptIntent), intent
    return intent


def _region_text(env) -> str:
    entries, diags = read_region(env.guide, "memory")
    assert not diags
    return "\n".join(entries)


def _accept_ready(env, *, text: str = FACT, line: str = LINE) -> apply.ManagedMemoryAcceptIntent:
    holder = _lease(env.vault)
    _predict(env.queue, text)
    return _plan(env, line, holder=holder)


def test_accept_writes_the_region_settles_the_row_and_owns_one_decision(install) -> None:
    intent = _accept_ready(install)

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(result, apply.Accepted)
    assert result.outcome == "written"
    assert FACT in _region_text(install)
    assert FACT not in install.queue.read_text(encoding="utf-8")
    decisions = [row for row in read_decisions(install.queue) if row["operation_id"] == intent.operation_id]
    assert len(decisions) == 1
    assert decisions[0]["via"] == "agent"
    assert decisions[0]["action"] == "accepted"
    assert decisions[0]["receipt_id"] == result.receipt_id
    assert decisions[0]["reason"] == apply.ACCEPT_DECISION_REASON
    journal = memory_receipts.journal_path(install.vault, install.guide)
    linked = memory_receipts.find_receipt(journal, result.receipt_id)
    assert linked is not None and linked["operation_id"] == intent.operation_id
    assert intent.item_key in curation_run.load_state(install.vault).done_keys


def test_replay_of_the_same_operation_is_already_done(install) -> None:
    intent = _accept_ready(install)
    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)

    again = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(again, apply.AlreadyDone)
    assert _region_text(install).count(FACT) == 1
    owned = [row for row in read_decisions(install.queue) if row["operation_id"] == intent.operation_id]
    assert len(owned) == 1


def test_a_stale_holder_is_refused_and_the_guide_is_unchanged(install) -> None:
    _lease(install.vault)
    _predict(install.queue)
    intent = _plan(install, holder="a-previous-run")
    guide_before = install.guide.read_bytes()

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert result == apply.Refused(apply.REFUSED_WRITE_NOT_AUTHORISED)
    assert install.guide.read_bytes() == guide_before
    assert FACT in install.queue.read_text(encoding="utf-8")
    assert read_decisions(install.queue) == []


def test_no_live_lease_refuses_before_any_write(install) -> None:
    _predict(install.queue)
    intent = _plan(install, holder="run")
    guide_before = install.guide.read_bytes()

    assert apply.apply_memory_accept(intent, vault_root=install.vault) == apply.Refused(
        apply.REFUSED_NO_LEASE
    )
    assert install.guide.read_bytes() == guide_before


def test_handover_lowered_refuses_before_any_write(install) -> None:
    intent = _accept_ready(install)
    registry = install.root / ".runtime" / "workspaces.json"
    entry = json.loads(registry.read_text(encoding="utf-8"))
    entry[0]["memory_handover"] = "dismiss"
    registry.write_text(json.dumps(entry), encoding="utf-8")
    guide_before = install.guide.read_bytes()

    assert apply.apply_memory_accept(intent, vault_root=install.vault) == apply.Refused(
        apply.REFUSED_HANDOVER
    )
    assert install.guide.read_bytes() == guide_before


def test_a_foreign_workspace_is_refused(install) -> None:
    intent = replace(_accept_ready(install), workspace="elsewhere")

    assert apply.apply_memory_accept(intent, vault_root=install.vault) == apply.Refused(
        apply.REFUSED_REGISTRY
    )
    assert FACT not in _region_text(install)


def test_decide_you_and_changed_predictions_refuse(install) -> None:
    decide_you = f"{LINE} _(decide: you; owner call)_"
    install.queue_rows(decide_you)
    holder = _lease(install.vault)
    _predict(install.queue)
    intent = _plan(install, decide_you, holder=holder)
    assert apply.apply_memory_accept(intent, vault_root=install.vault) == apply.Refused(
        apply.REFUSED_DECIDE_YOU
    )

    install.queue_rows(LINE)
    intent = _plan(install, LINE, holder=holder)
    _predict(install.queue, verdict="dismiss")
    assert apply.apply_memory_accept(intent, vault_root=install.vault) == apply.Refused(
        apply.REFUSED_PREDICTION
    )
    assert FACT not in _region_text(install)


def test_an_event_shaped_row_stays_queued_and_writes_nothing(install) -> None:
    event = "The user asked me to check the logs and I found the bug."
    install.queue_rows(f"- [memory] {event}")
    holder = _lease(install.vault)
    _predict(install.queue, event)
    intent = _plan(install, f"- [memory] {event}", holder=holder)
    queue_before = install.queue.read_bytes()

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert result == apply.Refused(apply.REFUSED_UNSHAPED)
    assert install.queue.read_bytes() == queue_before
    assert read_decisions(install.queue) == []


def test_a_destination_moved_after_planning_is_a_conflict(install) -> None:
    intent = _accept_ready(install)
    install.guide.write_text(
        install.guide.read_text(encoding="utf-8").replace("<!-- ciao:memory:end -->", "- Someone else's fact.\n<!-- ciao:memory:end -->"),
        encoding="utf-8",
    )
    guide_before = install.guide.read_bytes()

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert result == apply.Refused(apply.REFUSED_DESTINATION)
    assert install.guide.read_bytes() == guide_before
    assert FACT in install.queue.read_text(encoding="utf-8")


def test_the_cap_admits_exactly_five_sequential_accepts(install) -> None:
    facts = [f"Fact number {n} about the workspace." for n in range(6)]
    install.queue_rows(*[f"- [memory] {fact}" for fact in facts])
    holder = _lease(install.vault)
    for fact in facts:
        _predict(install.queue, fact)
    results = []
    for fact in facts:
        intent = _plan(install, f"- [memory] {fact}", holder=holder)
        results.append(apply.apply_memory_accept(intent, vault_root=install.vault))

    assert [type(r) for r in results[:5]] == [apply.Accepted] * 5
    assert results[5] == apply.Refused(apply.REFUSED_CAP)
    assert facts[5] not in _region_text(install)


def test_a_crash_after_the_guide_write_is_finished_by_the_same_operation(install, monkeypatch) -> None:
    intent = _accept_ready(install)

    real = apply.queue_resolution
    monkeypatch.setattr(apply, "queue_resolution", _crash)
    with pytest.raises(RuntimeError):
        apply.apply_memory_accept(intent, vault_root=install.vault)
    assert FACT in _region_text(install)
    assert FACT in install.queue.read_text(encoding="utf-8")
    monkeypatch.setattr(apply, "queue_resolution", real)

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(result, apply.Accepted)
    assert _region_text(install).count(FACT) == 1
    assert FACT not in install.queue.read_text(encoding="utf-8")
    owned = [row for row in read_decisions(install.queue) if row["operation_id"] == intent.operation_id]
    assert len(owned) == 1


def test_a_crash_after_the_queue_settles_records_the_missing_decision(install, monkeypatch) -> None:
    intent = _accept_ready(install)

    real = apply.record_promotion
    monkeypatch.setattr(apply, "record_promotion", _crash)
    assert apply.apply_memory_accept(intent, vault_root=install.vault) == apply.Failed(
        apply.FAILED_DECISION
    )
    assert FACT not in install.queue.read_text(encoding="utf-8")
    assert not [row for row in read_decisions(install.queue) if row["operation_id"] == intent.operation_id]
    monkeypatch.setattr(apply, "record_promotion", real)

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(result, apply.Accepted)
    assert _region_text(install).count(FACT) == 1
    owned = [row for row in read_decisions(install.queue) if row["operation_id"] == intent.operation_id]
    assert len(owned) == 1


def test_a_row_removed_between_the_two_effects_is_a_partial_conflict(install, monkeypatch) -> None:
    intent = _accept_ready(install)

    real = apply.queue_resolution
    monkeypatch.setattr(apply, "queue_resolution", _crash)
    with pytest.raises(RuntimeError):
        apply.apply_memory_accept(intent, vault_root=install.vault)
    monkeypatch.setattr(apply, "queue_resolution", real)
    install.queue_rows("- [memory] Some other fact entirely.")

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(result, apply.PartialConflict)
    assert _region_text(install).count(FACT) == 1
    assert not [row for row in read_decisions(install.queue) if row["operation_id"] == intent.operation_id]


def test_a_failed_guide_write_leaves_the_row_queued_with_no_progress(install, monkeypatch) -> None:
    intent = _accept_ready(install)
    monkeypatch.setattr(
        memory_proposals, "accept_region_fact", lambda **_kwargs: ("failed", "x")
    )

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(result, apply.Failed)
    assert FACT in install.queue.read_text(encoding="utf-8")
    assert intent.item_key not in curation_run.load_state(install.vault).done_keys
    assert read_decisions(install.queue) == []


def test_undo_of_the_guide_receipt_restores_the_region(install) -> None:
    intent = _accept_ready(install)
    result = apply.apply_memory_accept(intent, vault_root=install.vault)
    assert isinstance(result, apply.Accepted)

    memory_receipts.undo_receipt(
        result.receipt_id,
        vault_root=install.vault,
        journal=memory_receipts.journal_path(install.vault, install.guide),
    )

    assert FACT not in _region_text(install)


# ── cap, decision recording and guards ────────────────────────────────────


def _decide(env, *, operation_id: str, text: str) -> None:
    """A prior agent accept, recorded as a decision of its own operation."""
    memory_proposals.record_promotion(
        env.queue,
        text=text,
        kind="memory",
        via="agent",
        outcome="written",
        operation_id=operation_id,
    )


def _fill_cap(env, count: int) -> None:
    for n in range(count):
        _decide(env, operation_id=f"filler-{n}", text=f"Filler fact number {n}.")


def test_decision_not_recorded_returns_failed_and_a_replay_finishes_it(install, monkeypatch) -> None:
    intent = _accept_ready(install)

    def refuse(*_args, **_kwargs):
        return False

    real = apply.record_promotion
    monkeypatch.setattr(apply, "record_promotion", refuse)
    assert apply.apply_memory_accept(intent, vault_root=install.vault) == apply.Failed(
        apply.FAILED_DECISION
    )
    assert not [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]
    monkeypatch.setattr(apply, "record_promotion", real)

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(result, apply.Accepted)
    assert _region_text(install).count(FACT) == 1
    owned = [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]
    assert len(owned) == 1


def test_decision_raising_returns_failed_and_a_replay_finishes_it(install, monkeypatch) -> None:
    intent = _accept_ready(install)
    real = apply.record_promotion
    monkeypatch.setattr(apply, "record_promotion", _crash)
    assert apply.apply_memory_accept(intent, vault_root=install.vault) == apply.Failed(
        apply.FAILED_DECISION
    )
    monkeypatch.setattr(apply, "record_promotion", real)

    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)
    owned = [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]
    assert len(owned) == 1


def test_the_fifth_slot_is_taken_by_exactly_one_of_two_concurrent_accepts(install, monkeypatch) -> None:
    """Two accepts into different regions both pass preflight at four slots used."""
    profile = "Prefers short answers."
    install.queue_rows(LINE, f"- [profile] {profile}")
    holder = _lease(install.vault)
    _fill_cap(install, 4)
    _predict(install.queue, FACT)
    _predict(install.queue, profile, kind="profile")
    intents = [
        _plan(install, LINE, holder=holder),
        _plan(install, f"- [profile] {profile}", holder=holder),
    ]
    barrier = threading.Barrier(2, timeout=20)
    real_write = apply._write_region

    def wait_then_write(*args, **kwargs):
        barrier.wait()
        return real_write(*args, **kwargs)

    monkeypatch.setattr(apply, "_write_region", wait_then_write)
    results: list[object] = [None, None]

    errors: list[BaseException] = []

    def run(index: int) -> None:
        try:
            results[index] = apply.apply_memory_accept(intents[index], vault_root=install.vault)
        except BaseException as exc:  # surfaced below, not swallowed by the thread
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    accepted = [r for r in results if isinstance(r, apply.Accepted)]
    assert len(accepted) == 1, results
    assert apply._agent_slots(install.vault, install.queue, _lease_started(install.vault), exclude="") == 5
    written = [text for text in (FACT, profile) if text in _region_text(install) or text in _profile_text(install)]
    assert len(written) == 1


def _lease_started(vault: Path):
    from datetime import UTC, datetime

    lease = curation_run.active_lease(vault)
    return datetime.fromisoformat(lease["started_at"]).replace(tzinfo=UTC)


def _profile_text(env) -> str:
    entries, diags = read_region(env.guide, "profile")
    assert not diags
    return "\n".join(entries)


def test_a_retry_of_a_crashed_cli_accept_resumes_the_same_operation(install, monkeypatch, tmp_path) -> None:
    holder = _lease(install.vault)
    _predict(install.queue)
    needle = tmp_path / "needle.txt"
    needle.write_text("Prefers uv over pip", encoding="utf-8")
    argv = [
        "memory-proposal-accept",
        "--workspace", str(install.root),
        "--vault-root", str(install.vault),
        "--text-file", str(needle),
        "--holder", holder,
    ]
    real = apply.queue_resolution
    monkeypatch.setattr(apply, "queue_resolution", _crash)
    with pytest.raises(RuntimeError):
        cli.main(argv)
    monkeypatch.setattr(apply, "queue_resolution", real)
    journal = memory_receipts.journal_path(install.vault, install.guide)
    first = [r for r in memory_receipts.read_receipts(journal) if r.get("kind") == "region_apply"]
    assert len(first) == 1

    assert cli.main(argv) == 0

    receipts = [r for r in memory_receipts.read_receipts(journal) if r.get("kind") == "region_apply"]
    assert [r["id"] for r in receipts] == [first[0]["id"]]
    assert _region_text(install).count(FACT) == 1
    decisions = [r for r in read_decisions(install.queue) if r.get("operation_id")]
    assert len(decisions) == 1
    assert decisions[0]["receipt_id"] == first[0]["id"]
    assert decisions[0]["operation_id"] == first[0]["operation_id"]


def _accept_argv(install, tmp_path: Path, holder: str, text: str = FACT) -> list[str]:
    needle = tmp_path / "needle.txt"
    needle.write_text(text, encoding="utf-8")
    return [
        "memory-proposal-accept",
        "--workspace", str(install.root),
        "--vault-root", str(install.vault),
        "--text-file", str(needle),
        "--holder", holder,
    ]


def _receipts_of(install, kind: str) -> list[dict]:
    journal = memory_receipts.journal_path(install.vault, install.guide)
    return [r for r in memory_receipts.read_receipts(journal) if r.get("kind") == kind]


def _state_of(install) -> tuple:
    return (
        install.queue.read_text(encoding="utf-8"),
        _region_text(install),
        read_decisions(install.queue),
        _receipts_of(install, "region_apply"),
        _receipts_of(install, "queue_resolve"),
    )


def _crash_at_decision(install, monkeypatch, argv: list[str]) -> None:
    """A CLI accept that removes the queue row and then dies before the decision."""
    real = apply.record_promotion
    monkeypatch.setattr(apply, "record_promotion", _crash)
    assert cli.main(argv) == 1
    monkeypatch.setattr(apply, "record_promotion", real)
    assert not [r for r in read_decisions(install.queue) if r.get("operation_id")]


def test_a_retry_after_a_crash_at_the_decision_resumes_the_operation(install, monkeypatch, tmp_path) -> None:
    holder = _lease(install.vault)
    _predict(install.queue)
    argv = _accept_argv(install, tmp_path, holder)
    _crash_at_decision(install, monkeypatch, argv)
    first = _receipts_of(install, "region_apply")
    queued = _receipts_of(install, "queue_resolve")
    assert len(first) == 1 and len(queued) == 1

    assert cli.main(argv) == 0

    assert [r["id"] for r in _receipts_of(install, "region_apply")] == [first[0]["id"]]
    assert _region_text(install).count(FACT) == 1
    decisions = [r for r in read_decisions(install.queue) if r.get("operation_id")]
    assert len(decisions) == 1
    assert decisions[0]["operation_id"] == queued[0]["operation_id"]
    assert decisions[0]["receipt_id"] == first[0]["id"]
    assert queued[0]["item_key"] in curation_run.load_state(install.vault).done_keys


def test_a_retry_after_a_crash_at_progress_records_the_missing_progress_key(install, monkeypatch, tmp_path) -> None:
    holder = _lease(install.vault)
    _predict(install.queue)
    argv = _accept_argv(install, tmp_path, holder)
    real = apply.record_done
    monkeypatch.setattr(apply, "record_done", _crash)
    with pytest.raises(RuntimeError):
        cli.main(argv)
    monkeypatch.setattr(apply, "record_done", real)
    queued = _receipts_of(install, "queue_resolve")
    decided = [r for r in read_decisions(install.queue) if r.get("operation_id")]
    assert len(decided) == 1
    assert queued[0]["item_key"] not in curation_run.load_state(install.vault).done_keys

    assert cli.main(argv) == 0

    assert _region_text(install).count(FACT) == 1
    assert len([r for r in read_decisions(install.queue) if r.get("operation_id")]) == 1
    assert queued[0]["item_key"] in curation_run.load_state(install.vault).done_keys


def test_a_retry_of_a_finished_operation_is_already_done(install, monkeypatch, tmp_path) -> None:
    holder = _lease(install.vault)
    _predict(install.queue)
    argv = _accept_argv(install, tmp_path, holder)
    _crash_at_decision(install, monkeypatch, argv)
    assert cli.main(argv) == 0
    before = _state_of(install)

    assert cli.main(argv) == 0

    assert _state_of(install) == before


def test_a_finished_operation_is_not_resumed_by_another_run_or_holder(install, monkeypatch, tmp_path) -> None:
    holder = _lease(install.vault)
    _predict(install.queue)
    argv = _accept_argv(install, tmp_path, holder)
    _crash_at_decision(install, monkeypatch, argv)
    assert cli.main(argv) == 0
    before = _state_of(install)

    assert cli.main(_accept_argv(install, tmp_path, "someone-else")) == 1
    curation_run.end_run(install.vault, holder=holder)
    other = curation_run.begin_run(install.vault, holder="run-b", ttl_s=3600).holder

    assert cli.main(_accept_argv(install, tmp_path, other)) == 1

    assert _state_of(install) == before


def test_run_b_does_not_resume_run_a_unfinished_receipt_with_the_same_text(install, monkeypatch, tmp_path) -> None:
    holder_a = _lease(install.vault)
    _predict(install.queue)
    _crash_at_decision(install, monkeypatch, _accept_argv(install, tmp_path, holder_a))
    curation_run.end_run(install.vault, holder=holder_a)
    holder_b = curation_run.begin_run(install.vault, holder="run-b", ttl_s=3600).holder
    before = _state_of(install)

    assert cli.main(_accept_argv(install, tmp_path, holder_b)) == 1

    assert _state_of(install) == before


def test_a_receipt_from_an_earlier_lease_is_not_resumed(install, monkeypatch, tmp_path) -> None:
    """An old unfinished receipt, named by its old holder under a new lease, is not resumed.

    Each lease has its own holder string, so only the lease-start bound separates
    the two: the holder match alone would accept it.
    """
    holder = _lease(install.vault)
    _predict(install.queue)
    argv = _accept_argv(install, tmp_path, holder)
    _crash_at_decision(install, monkeypatch, argv)
    curation_run.end_run(install.vault, holder=holder)
    # Stamps are whole seconds, so the new lease must start in a later second.
    time.sleep(1.1)
    assert _lease(install.vault) != holder
    before = _state_of(install)

    assert apply.find_resumable_accept(install.vault, workspace="work", holder=holder, text=FACT) is None
    assert cli.main(argv) == 1

    assert _state_of(install) == before


def test_the_finder_reads_only_the_holders_own_receipts(install, monkeypatch, tmp_path) -> None:
    """Run A's unfinished receipt, inside A's live lease, is not offered to holder B."""
    holder = _lease(install.vault)
    _predict(install.queue)
    _crash_at_decision(install, monkeypatch, _accept_argv(install, tmp_path, holder))
    before = _state_of(install)

    assert apply.find_resumable_accept(install.vault, workspace="work", holder="run-b", text=FACT) is None
    assert apply.find_resumable_accept(install.vault, workspace="work", holder=holder, text=FACT) is not None
    assert _state_of(install) == before


def test_a_resume_is_refused_at_the_cap_even_with_an_unfinished_operation(install, monkeypatch, tmp_path) -> None:
    holder = _lease(install.vault)
    _predict(install.queue)
    argv = _accept_argv(install, tmp_path, holder)
    _crash_at_decision(install, monkeypatch, argv)
    _fill_cap(install, apply.HANDOVER_ACCEPT_CAP)
    before = _state_of(install)

    assert cli.main(argv) == 1

    assert _state_of(install) == before


def test_a_decision_is_not_written_once_the_lease_is_gone(install, monkeypatch) -> None:
    intent = _accept_ready(install)
    real_settle = apply._settle_queue

    def settle_then_lose_lease(vault, queue, intent_, **kwargs):
        settled = real_settle(vault, queue, intent_, **kwargs)
        curation_run.end_run(vault, holder=intent_.holder)
        return settled

    monkeypatch.setattr(apply, "_settle_queue", settle_then_lose_lease)

    assert apply.apply_memory_accept(intent, vault_root=install.vault) == apply.Failed(apply.FAILED_DECISION)

    assert not [r for r in read_decisions(install.queue) if r.get("operation_id") == intent.operation_id]
    assert _region_text(install).count(FACT) == 1


def test_a_concurrent_decision_is_not_recorded_twice(install, monkeypatch) -> None:
    """Two retries pass the first check together; the second sees the first's decision under the lock."""
    intent = _accept_ready(install)
    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)
    real_decision_for = apply._decision_for
    calls = {"n": 0}

    def first_check_sees_nothing(queue, operation_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_decision_for(queue, operation_id)

    monkeypatch.setattr(apply, "_decision_for", first_check_sees_nothing)

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(result, apply.AlreadyDone)
    assert len([r for r in read_decisions(install.queue) if r.get("operation_id") == intent.operation_id]) == 1


@pytest.mark.parametrize(
    "refusal",
    ["other_holder", "no_lease", "handover_lowered", "other_workspace"],
)
def test_a_retry_that_no_longer_has_authority_resumes_nothing(install, monkeypatch, tmp_path, refusal) -> None:
    holder = _lease(install.vault)
    _predict(install.queue)
    argv = _accept_argv(install, tmp_path, holder)
    _crash_at_decision(install, monkeypatch, argv)
    before = _state_of(install)
    if refusal == "other_holder":
        argv[argv.index("--holder") + 1] = "someone-else"
    elif refusal == "no_lease":
        curation_run.end_run(install.vault, holder=holder)
    elif refusal == "handover_lowered":
        registry = install.root / ".runtime" / "workspaces.json"
        registry.write_text(
            json.dumps([{"name": "work", "vault_root": str(install.vault), "memory_handover": "off"}]),
            encoding="utf-8",
        )
    elif refusal == "other_workspace":
        # The same receipt, as another workspace wrote it; the journal folds by id, last row wins.
        journal = memory_receipts.journal_path(install.vault, install.guide)
        row = dict(_receipts_of(install, "queue_resolve")[0], workspace="elsewhere")
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        before = _state_of(install)

    assert cli.main(argv) == 1

    assert _state_of(install) == before


def test_an_ambiguous_retry_resumes_nothing(install, monkeypatch, tmp_path) -> None:
    holder = _lease(install.vault)
    install.queue_rows(f"- [memory] {FACT}  _(from: chat-1)_", f"- [memory] {FACT}  _(from: chat-2)_")
    _predict(install.queue)
    _crash_at_decision(install, monkeypatch, _accept_argv(install, tmp_path, holder, "chat-1"))
    _crash_at_decision(install, monkeypatch, _accept_argv(install, tmp_path, holder, "chat-2"))
    before = _state_of(install)

    assert cli.main(_accept_argv(install, tmp_path, holder)) == 1

    assert _state_of(install) == before


def test_a_lease_lost_between_the_two_effects_is_a_partial_conflict(install, monkeypatch) -> None:
    """The lease check under the queue lock is what stops the settlement."""
    intent = _accept_ready(install)
    real_write = apply._write_region
    lost = {"value": False}

    def write_then_lose(*args, **kwargs):
        result = real_write(*args, **kwargs)
        lost["value"] = True
        return result

    def authority(vault, *, holder):
        @contextmanager
        def held():
            yield not lost["value"]
        return held()

    monkeypatch.setattr(apply, "_write_region", write_then_lose)
    monkeypatch.setattr(apply, "authorize_holder", authority)

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert result == apply.PartialConflict(result.guide_receipt_id)
    assert _region_text(install).count(FACT) == 1
    assert FACT in install.queue.read_text(encoding="utf-8")
    assert not [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]


def test_handover_lowered_between_the_two_effects_stops_the_settlement(install, monkeypatch) -> None:
    intent = _accept_ready(install)
    real_write = apply._write_region
    registry = install.root / ".runtime" / "workspaces.json"

    def write_then_lower(*args, **kwargs):
        result = real_write(*args, **kwargs)
        entry = json.loads(registry.read_text(encoding="utf-8"))
        entry[0]["memory_handover"] = "dismiss"
        registry.write_text(json.dumps(entry), encoding="utf-8")
        return result

    monkeypatch.setattr(apply, "_write_region", write_then_lower)

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(result, apply.PartialConflict)
    assert _region_text(install).count(FACT) == 1
    assert FACT in install.queue.read_text(encoding="utf-8")
    assert not [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]


def test_a_slot_taken_after_preflight_refuses_the_guide_write(install, monkeypatch) -> None:
    intent = _accept_ready(install)
    real_write = apply._write_region

    def fill_then_write(*args, **kwargs):
        _fill_cap(install, 5)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(apply, "_write_region", fill_then_write)
    guide_before = install.guide.read_bytes()

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert result == apply.Refused(apply.REFUSED_WRITE_NOT_AUTHORISED)
    assert install.guide.read_bytes() == guide_before
    assert FACT in install.queue.read_text(encoding="utf-8")


def test_a_row_whose_revision_changed_after_planning_is_refused(install) -> None:
    line = f"{LINE} _(decide: nightly; first reason)_"
    install.queue_rows(line)
    holder = _lease(install.vault)
    _predict(install.queue)
    intent = _plan(install, line, holder=holder)
    install.queue_rows(f"{LINE} _(decide: nightly; second reason)_")
    guide_before = install.guide.read_bytes()

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert result == apply.Refused(apply.REFUSED_CHANGED)
    assert install.guide.read_bytes() == guide_before


# ── S2: the accept stamps the run and the item on every row it writes ──


def _operation_rows(env, operation_id: str) -> list[dict]:
    journal = memory_receipts.journal_path(env.vault, env.guide)
    return [r for r in memory_receipts.read_receipts(journal) if r.get("operation_id") == operation_id]


def test_an_accept_stamps_run_and_item_on_both_receipts_and_the_decision(install) -> None:
    intent = _accept_ready(install)

    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)

    rows = _operation_rows(install, intent.operation_id)
    assert sorted(r["kind"] for r in rows) == ["queue_resolve", "region_apply"]
    assert {(r["run_id"], r["item_key"]) for r in rows} == {(intent.holder, intent.item_key)}
    (decision,) = [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]
    assert (decision["run_id"], decision["item_key"]) == (intent.holder, intent.item_key)


def test_a_replay_in_a_later_run_adds_no_rows_and_keeps_the_first_stamps(install) -> None:
    intent = _accept_ready(install)
    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)
    before = _operation_rows(install, intent.operation_id)
    curation_run.end_run(install.vault, holder=intent.holder)
    later = replace(intent, holder=_lease(install.vault))

    assert isinstance(apply.apply_memory_accept(later, vault_root=install.vault), apply.AlreadyDone)

    assert _operation_rows(install, intent.operation_id) == before
    decisions = [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]
    assert [(d["run_id"], d["item_key"]) for d in decisions] == [(intent.holder, intent.item_key)]


def test_a_writer_without_a_run_leaves_both_keys_empty(install) -> None:
    memory_proposals.record_promotion(
        install.queue, text=FACT, kind="memory", via="cli", outcome="written"
    )

    (row,) = [r for r in read_decisions(install.queue) if r["text"] == FACT]
    assert row["run_id"] == "" and row["item_key"] == ""


def test_a_crash_between_the_effects_keeps_the_stamps_on_the_finishing_rows(install, monkeypatch) -> None:
    intent = _accept_ready(install)
    real = apply.queue_resolution
    monkeypatch.setattr(apply, "queue_resolution", _crash)
    with pytest.raises(RuntimeError):
        apply.apply_memory_accept(intent, vault_root=install.vault)
    monkeypatch.setattr(apply, "queue_resolution", real)

    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)

    rows = _operation_rows(install, intent.operation_id)
    assert {(r["run_id"], r["item_key"]) for r in rows} == {(intent.holder, intent.item_key)}


def _run_night(env, capsys: pytest.CaptureFixture[str]) -> dict:
    capsys.readouterr()
    assert cli.main(["curation-begin", "--vault-root", str(env.vault), "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_curation_end_derives_counts_from_the_begin_plan_and_the_runs_own_receipts(install, capsys) -> None:
    payload = _run_night(install, capsys)
    holder = payload["lease"]["holder"]
    planned_keys = {key for item in payload["planned"] for key in item["keys"]}
    assert curation_run.HYGIENE_INDEX_KEY in planned_keys
    _predict(install.queue)
    intent = _plan(install, holder=holder)
    # Planned at its predict stage; the accept settles under act:accept, which the
    # begin plan never named, so it counts as extra work (see the next test).
    assert intent.item_key not in planned_keys
    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)
    assert cli.main(
        ["curation-progress", "--vault-root", str(install.vault), "--holder", holder,
         "--key", curation_run.HYGIENE_INDEX_KEY]
    ) == 0
    capsys.readouterr()

    # The counts are no longer the agent's to state: the flags are gone.
    with pytest.raises(SystemExit):
        cli.main(["curation-end", "--vault-root", str(install.vault), "--holder", holder,
                  "--status", "ok", "--completed", "99"])
    capsys.readouterr()
    assert cli.main(
        ["curation-end", "--vault-root", str(install.vault), "--holder", holder,
         "--status", "ok", "--json"]
    ) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["planned"] == len(planned_keys)
    assert summary["completed"] == 2  # the accept (applied) and the index check (done, unverified)
    assert summary["counts_complete"] is True
    stored = curation_run.load_state(install.vault).last_run
    assert (stored["planned"], stored["completed"]) == (summary["planned"], 2)


def test_a_planned_accept_with_no_receipt_is_not_counted_as_completed(install, capsys) -> None:
    payload = _run_night(install, capsys)
    holder = payload["lease"]["holder"]
    planned_keys = {key for item in payload["planned"] for key in item["keys"]}
    capsys.readouterr()

    assert cli.main(
        ["curation-end", "--vault-root", str(install.vault), "--holder", holder,
         "--status", "ok", "--json"]
    ) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["planned"] == len(planned_keys)
    assert summary["completed"] == 0


@contextmanager
def _guide_write_fails(monkeypatch):
    """Patch the guide write to fail, restored at exit without touching other fixtures."""
    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr("ciao.memory_tool.write_guide_atomically", disk_full)
        yield


def _end(env, capsys: pytest.CaptureFixture[str], holder: str, status: str = "ok") -> dict:
    capsys.readouterr()
    assert cli.main(
        ["curation-end", "--vault-root", str(env.vault), "--holder", holder,
         "--status", status, "--json"]
    ) == 0
    return json.loads(capsys.readouterr().out)


def test_a_planned_key_accepted_under_its_planned_key_is_counted_once(install, capsys) -> None:
    _predict(install.queue)
    payload = _run_night(install, capsys)
    holder = payload["lease"]["holder"]
    intent = _plan(install, holder=holder)
    assert intent.item_key in {key for item in payload["planned"] for key in item["keys"]}
    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)

    summary = _end(install, capsys, holder)

    assert summary["completed"] == 1
    assert summary["counts_complete"] is True


def test_a_row_predicted_and_accepted_in_one_run_is_unplanned_extra_work(install, capsys) -> None:
    payload = _run_night(install, capsys)
    holder = payload["lease"]["holder"]
    planned_keys = {key for item in payload["planned"] for key in item["keys"]}
    _predict(install.queue)
    intent = _plan(install, holder=holder)
    assert intent.item_key not in planned_keys
    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)

    summary = _end(install, capsys, holder)

    assert summary["planned"] == len(planned_keys)
    assert summary["completed"] == 1
    assert summary["counts_complete"] is True


def test_a_failed_accept_of_a_row_found_during_the_run_is_not_completed(install, capsys, monkeypatch) -> None:
    payload = _run_night(install, capsys)
    holder = payload["lease"]["holder"]
    _predict(install.queue)
    intent = _plan(install, holder=holder)

    with _guide_write_fails(monkeypatch):
        assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Failed)

    summary = _end(install, capsys, holder)

    assert summary["completed"] == 0
    assert summary["failed"] == 1
    assert summary["counts_complete"] is True
    assert any("1 accept(s) failed" in reason for reason in summary["reasons"])


def test_an_accept_resumed_by_a_later_run_is_matched_by_operation_and_credited_once(
    install, capsys, monkeypatch
) -> None:
    holder_a = _lease(install.vault)
    _predict(install.queue)
    intent_a = _plan(install, holder=holder_a)
    real = apply.queue_resolution
    monkeypatch.setattr(apply, "queue_resolution", _crash)
    with pytest.raises(RuntimeError):
        apply.apply_memory_accept(intent_a, vault_root=install.vault)
    monkeypatch.setattr(apply, "queue_resolution", real)
    curation_run.end_run(install.vault, holder=holder_a)

    payload = _run_night(install, capsys)
    holder_b = payload["lease"]["holder"]
    intent_b = _plan(install, holder=holder_b)
    assert intent_b.operation_id == intent_a.operation_id
    assert isinstance(apply.apply_memory_accept(intent_b, vault_root=install.vault), apply.Accepted)

    summary = _end(install, capsys, holder_b)

    assert summary["completed"] == 1
    assert summary["counts_complete"] is True


def test_the_plan_is_cut_to_the_budget_and_the_end_reports_that_cut(install, capsys) -> None:
    capsys.readouterr()
    assert cli.main(
        ["curation-begin", "--vault-root", str(install.vault), "--max-items", "2", "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    planned = sum(len(item["keys"]) for item in payload["planned"])
    assert planned == 2 and payload["deferred_count"] >= 1

    summary = _end(install, capsys, payload["lease"]["holder"])

    assert summary["planned"] == planned
    assert summary["deferred"] == payload["deferred_count"]


def test_the_fold_is_handed_only_this_runs_operation_ids_by_key(install, capsys, monkeypatch) -> None:
    from ciao import care_report

    payload = _run_night(install, capsys)
    holder = payload["lease"]["holder"]
    _predict(install.queue)
    intent = _plan(install, holder=holder)
    assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Accepted)
    memory_receipts._append(
        memory_receipts.journal_path(install.vault),
        {
            "id": "foreign-1", "ts": "2026-11-21T00:00:00Z", "actor": "agent", "source": "cli",
            "workspace": "work", "kind": "region_apply", "status": "applied",
            "operation_id": "cop_foreign", "run_id": "other-run", "item_key": intent.item_key,
            "changed": True, "before_revision": "a", "after_revision": "b",
        },
    )
    handed: list[tuple[str, tuple[str, ...]]] = []
    real = care_report.summarize_care_run

    def spy(plan, receipts, **kwargs):
        handed.extend((item.item_key, item.operation_ids) for item in plan.items)
        return real(plan, receipts, **kwargs)

    monkeypatch.setattr(care_report, "summarize_care_run", spy)
    _end(install, capsys, holder)

    assert (intent.item_key, (intent.operation_id,)) in handed
    assert all("cop_foreign" not in operations for _, operations in handed)


def test_a_planned_key_whose_only_receipt_is_another_runs_is_unfinished_for_this_run(
    install, capsys, monkeypatch
) -> None:
    from ciao import care_report

    _predict(install.queue)
    holder_a = _lease(install.vault)
    intent_a = _plan(install, holder=holder_a)

    # A's guide write fails: a FAILED receipt stamped with A's run, and the row
    # stays queued, so the same key is planned again by the next run.
    with _guide_write_fails(monkeypatch):
        assert isinstance(apply.apply_memory_accept(intent_a, vault_root=install.vault), apply.Failed)
    curation_run.end_run(install.vault, holder=holder_a)

    payload = _run_night(install, capsys)
    holder_b = payload["lease"]["holder"]
    assert intent_a.item_key in {key for item in payload["planned"] for key in item["keys"]}
    handed: list[tuple[str, tuple[str, ...]]] = []
    real = care_report.summarize_care_run

    def spy(plan, receipts, **kwargs):
        handed.extend((item.item_key, item.operation_ids) for item in plan.items)
        return real(plan, receipts, **kwargs)

    monkeypatch.setattr(care_report, "summarize_care_run", spy)
    summary = _end(install, capsys, holder_b)

    assert (intent_a.item_key, ()) in handed
    assert all(intent_a.operation_id not in operations for _, operations in handed)
    assert summary["completed"] == 0


def test_a_failed_planned_accept_is_counted_as_failed_and_leaves_the_run_complete(
    install, capsys, monkeypatch
) -> None:
    _predict(install.queue)
    payload = _run_night(install, capsys)
    holder = payload["lease"]["holder"]
    intent = _plan(install, holder=holder)
    assert intent.item_key in {key for item in payload["planned"] for key in item["keys"]}

    with _guide_write_fails(monkeypatch):
        assert isinstance(apply.apply_memory_accept(intent, vault_root=install.vault), apply.Failed)

    summary = _end(install, capsys, holder)

    assert summary["failed"] == 1
    assert summary["completed"] == 0
    assert summary["counts_complete"] is True
    assert any("1 accept(s) failed" in reason for reason in summary["reasons"])


@pytest.mark.parametrize("bad_plan", [
    "garbage",
    ["planned"],
    {"planned": "x", "blocked": [], "blocked_overflow": 0, "deferred_count": 0},
    {"planned": [["proposals", 7]], "blocked": [], "blocked_overflow": 0, "deferred_count": 0},
    {"planned": [["proposals", "k"]] * 20_001, "blocked": [], "blocked_overflow": 0, "deferred_count": 0},
    {"planned": [], "blocked": [], "blocked_overflow": -1, "deferred_count": 0},
    {"planned": [], "blocked": []},
])
def test_a_malformed_stored_plan_exits_one_without_a_traceback_and_keeps_the_lease(
    install, capsys, bad_plan
) -> None:
    payload = _run_night(install, capsys)
    holder = payload["lease"]["holder"]
    path = curation_run.state_path(install.vault)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["lease"]["plan"] = bad_plan
    path.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()

    code = cli.main(
        ["curation-end", "--vault-root", str(install.vault), "--holder", holder, "--status", "ok"]
    )

    assert code == 1
    assert "curation state error" in capsys.readouterr().err
    assert curation_run.state_path(install.vault).exists()
    assert json.loads(path.read_text(encoding="utf-8"))["lease"]["holder"] == holder



# ── per-kind acceptance (memory_approval) at the engine seam ────────────────

PROFILE_LINE = "- [profile] Writes in short sentences."
PROFILE_FACT = "Writes in short sentences."


def _set_approval(env, mapping: dict | None) -> None:
    registry = env.root / ".runtime" / "workspaces.json"
    entry = json.loads(registry.read_text(encoding="utf-8"))
    if mapping is None:
        entry[0].pop("memory_approval", None)
    else:
        entry[0]["memory_approval"] = mapping
    registry.write_text(json.dumps(entry), encoding="utf-8")


def _profile_text(env) -> str:
    entries, diags = read_region(env.guide, "profile")
    assert not diags
    return "\n".join(entries)


def _cli_accept(env, holder: str, needle_file: Path) -> int:
    return cli.main(
        [
            "memory-proposal-accept",
            "--workspace", str(env.root),
            "--vault-root", str(env.vault),
            "--text-file", str(needle_file),
            "--holder", holder,
        ]
    )


def test_explicit_approve_refuses_the_direct_cli_accept_under_legacy_all(install, capsys, tmp_path) -> None:
    _set_approval(install, {"memory": "approve"})
    holder = _lease(install.vault)
    _predict(install.queue)
    needle = tmp_path / "needle.txt"
    needle.write_text(FACT, encoding="utf-8")
    guide_before = install.guide.read_bytes()
    queue_before = install.queue.read_bytes()

    assert _cli_accept(install, holder, needle) == 1

    assert "needs your approval" in capsys.readouterr().err
    assert install.guide.read_bytes() == guide_before
    assert install.queue.read_bytes() == queue_before
    journal = memory_receipts.journal_path(install.vault, install.guide)
    assert not [r for r in memory_receipts.read_receipts(journal) if r.get("kind") == "region_apply"]
    assert read_decisions(install.queue) == []


def test_policy_changed_to_approve_is_refused_at_the_guide_lock_seam(install, monkeypatch) -> None:
    """The value handed to ``accept_region_fact``'s authorize is what refuses, under the guide lock."""
    intent = _accept_ready(install)
    real = memory_proposals.accept_region_fact
    seen: list[bool] = []

    def flip_then_write(**kwargs):
        # After the pre-write check passed, before the guide lock is taken.
        _set_approval(install, {"memory": "approve"})
        real_authorize = kwargs["authorize"]

        @contextmanager
        def observed():
            with real_authorize() as allowed:
                seen.append(allowed)
                yield allowed

        kwargs["authorize"] = observed
        return real(**kwargs)

    monkeypatch.setattr(memory_proposals, "accept_region_fact", flip_then_write)
    guide_before = install.guide.read_bytes()

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert seen == [False]
    assert result == apply.Refused(apply.REFUSED_WRITE_NOT_AUTHORISED)
    assert install.guide.read_bytes() == guide_before
    assert FACT in install.queue.read_text(encoding="utf-8")
    assert not [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]


def test_policy_changed_to_approve_between_the_two_effects_is_a_partial_conflict_then_recovers(
    install, monkeypatch
) -> None:
    intent = _accept_ready(install)
    real_write = apply._write_region

    def write_then_approve(*args, **kwargs):
        result = real_write(*args, **kwargs)
        _set_approval(install, {"memory": "approve"})
        return result

    monkeypatch.setattr(apply, "_write_region", write_then_approve)

    result = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(result, apply.PartialConflict)
    assert _region_text(install).count(FACT) == 1
    assert FACT in install.queue.read_text(encoding="utf-8")
    assert not [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]

    # The operator restores the default and the same operation finishes it.
    _set_approval(install, None)
    monkeypatch.setattr(apply, "_write_region", real_write)
    finished = apply.apply_memory_accept(intent, vault_root=install.vault)

    assert isinstance(finished, apply.Accepted)
    assert _region_text(install).count(FACT) == 1
    assert FACT not in install.queue.read_text(encoding="utf-8")
    owned = [r for r in read_decisions(install.queue) if r["operation_id"] == intent.operation_id]
    assert len(owned) == 1


def test_auto_under_legacy_off_accepts_only_its_kind(install) -> None:
    install.queue_rows(LINE, PROFILE_LINE)
    _set_approval(install, {"profile": "auto"})
    holder = _lease(install.vault)
    _predict(install.queue, FACT, kind="memory")
    _predict(install.queue, PROFILE_FACT, kind="profile")
    registry = install.root / ".runtime" / "workspaces.json"
    entry = json.loads(registry.read_text(encoding="utf-8"))
    entry[0]["memory_handover"] = "off"
    registry.write_text(json.dumps(entry), encoding="utf-8")

    profile = apply.apply_memory_accept(
        _plan(install, PROFILE_LINE, holder=holder), vault_root=install.vault
    )
    memory = apply.apply_memory_accept(_plan(install, LINE, holder=holder), vault_root=install.vault)

    assert isinstance(profile, apply.Accepted)
    assert PROFILE_FACT in _profile_text(install)
    assert memory == apply.Refused(apply.REFUSED_HANDOVER)
    assert FACT not in _region_text(install)
    assert FACT in install.queue.read_text(encoding="utf-8")


def test_dismissal_follows_the_legacy_setting_only(install, capsys) -> None:
    _set_approval(install, {"memory": "approve"})
    registry = install.root / ".runtime" / "workspaces.json"
    entry = json.loads(registry.read_text(encoding="utf-8"))
    entry[0]["memory_handover"] = "dismiss"
    registry.write_text(json.dumps(entry), encoding="utf-8")
    _lease(install.vault)
    _predict(install.queue, verdict="dismiss")
    bullet = parse_bullet(LINE)

    # Approve for accept does not stop a dismissal the legacy setting allows.
    assert cli._handover_gate(install.root, "work", install.queue, bullet) == ""

    _set_approval(install, {"memory": "auto"})
    entry = json.loads(registry.read_text(encoding="utf-8"))
    entry[0]["memory_handover"] = "off"
    registry.write_text(json.dumps(entry), encoding="utf-8")

    # An explicit accept choice never grants a dismissal the legacy setting refuses.
    assert "does not allow dismiss" in cli._handover_gate(install.root, "work", install.queue, bullet)


def test_the_refusal_reason_names_an_explicit_approve_and_the_legacy_refusal(install) -> None:
    intent = _accept_ready(install)
    holder = intent.holder

    _set_approval(install, {"memory": "approve"})
    _, _, reason = apply._check_row(install.vault, install.queue, intent)
    assert reason == apply.REFUSED_APPROVAL
    with apply._holder_may_accept(
        install.vault, install.queue, workspace="work", kind="memory",
        holder=holder, started=datetime.now(UTC), operation_id=intent.operation_id,
    ) as allowed:
        assert allowed is False
    assert apply._accept_refusal(install.vault, "work", "memory") == apply.REFUSED_APPROVAL

    _set_approval(install, None)
    registry = install.root / ".runtime" / "workspaces.json"
    entry = json.loads(registry.read_text(encoding="utf-8"))
    entry[0]["memory_handover"] = "off"
    registry.write_text(json.dumps(entry), encoding="utf-8")
    _, _, reason = apply._check_row(install.vault, install.queue, intent)
    assert reason == apply.REFUSED_HANDOVER
    with apply._holder_may_accept(
        install.vault, install.queue, workspace="work", kind="memory",
        holder=holder, started=datetime.now(UTC), operation_id=intent.operation_id,
    ) as allowed:
        assert allowed is False
    assert apply._accept_refusal(install.vault, "work", "memory") == apply.REFUSED_HANDOVER


# ── the resume path reads the per-kind policy of the receipt's kind ─────────


def _crashed_accept(install, monkeypatch, tmp_path):
    holder = _lease(install.vault)
    _predict(install.queue)
    argv = _accept_argv(install, tmp_path, holder)
    _crash_at_decision(install, monkeypatch, argv)
    return holder, argv


def _set_policy(install, *, handover: str, approval: dict | None = None) -> None:
    entry: dict = {"name": "work", "vault_root": str(install.vault), "memory_handover": handover}
    if approval is not None:
        entry["memory_approval"] = approval
    (install.root / ".runtime" / "workspaces.json").write_text(json.dumps([entry]), encoding="utf-8")


def _owned_decisions(install, operation_id: str) -> list[dict]:
    return [r for r in read_decisions(install.queue) if r.get("operation_id") == operation_id]


def test_a_resume_is_refused_when_the_operator_flips_the_kind_to_approve(install, monkeypatch, tmp_path) -> None:
    holder, argv = _crashed_accept(install, monkeypatch, tmp_path)
    receipt = _receipts_of(install, "queue_resolve")[0]
    _set_policy(install, handover="all", approval={"memory": "approve"})
    before = _state_of(install)

    assert apply.resume_memory_accept(
        install.vault, workspace="work", holder=holder, receipt=receipt
    ) == apply.Refused(apply.REFUSED_APPROVAL)
    assert cli.main(argv) == 1
    assert _state_of(install) == before
    assert _owned_decisions(install, receipt["operation_id"]) == []


def test_a_resume_is_allowed_for_an_explicit_auto_under_legacy_off(install, monkeypatch, tmp_path) -> None:
    holder, argv = _crashed_accept(install, monkeypatch, tmp_path)
    _set_policy(install, handover="off", approval={"memory": "auto"})

    assert cli.main(argv) == 0

    receipt = _receipts_of(install, "queue_resolve")[0]
    owned = _owned_decisions(install, receipt["operation_id"])
    assert len(owned) == 1 and owned[0]["via"] == "agent"


def test_a_resume_is_refused_under_legacy_off_without_an_explicit_choice(install, monkeypatch, tmp_path) -> None:
    holder, argv = _crashed_accept(install, monkeypatch, tmp_path)
    receipt = _receipts_of(install, "queue_resolve")[0]
    _set_policy(install, handover="off")

    assert apply.resume_memory_accept(
        install.vault, workspace="work", holder=holder, receipt=receipt
    ) == apply.Refused(apply.REFUSED_HANDOVER)
    assert cli.main(argv) == 1


def test_a_resume_reads_only_the_receipts_own_kind(install, monkeypatch, tmp_path) -> None:
    holder, argv = _crashed_accept(install, monkeypatch, tmp_path)
    _set_policy(install, handover="all", approval={"profile": "approve"})

    assert cli.main(argv) == 0
    receipt = _receipts_of(install, "queue_resolve")[0]
    assert len(_owned_decisions(install, receipt["operation_id"])) == 1


def test_a_resume_under_legacy_all_is_unchanged(install, monkeypatch, tmp_path) -> None:
    holder, argv = _crashed_accept(install, monkeypatch, tmp_path)
    receipt = _receipts_of(install, "queue_resolve")[0]

    assert apply.resume_memory_accept(
        install.vault, workspace="work", holder=holder, receipt=receipt
    ) != apply.Refused(apply.REFUSED_HANDOVER)
    assert cli.main(argv) == 0


def test_a_resume_with_an_unknown_receipt_kind_is_refused(install, monkeypatch, tmp_path) -> None:
    holder, _argv = _crashed_accept(install, monkeypatch, tmp_path)
    receipt = dict(_receipts_of(install, "queue_resolve")[0], proposal_kind="project")
    before = _state_of(install)

    assert apply.resume_memory_accept(
        install.vault, workspace="work", holder=holder, receipt=receipt
    ) == apply.Refused(apply.REFUSED_KIND)
    assert _state_of(install) == before
