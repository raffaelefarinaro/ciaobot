"""The managed memory accept: one operation, two effects, one owned decision.

Every case runs on a ``tmp_path`` vault with its own install registry; the
service definition is stubbed so the trusted registry reads that install only.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ciao import curation_apply as apply
from ciao import curation_run, memory_proposals, memory_receipts
from ciao.memory_proposals import read_decisions
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


def _predict(queue: Path, text: str = FACT, verdict: str = "accept") -> None:
    record_prediction(queue, kind="memory", text=text, verdict=verdict, reason="because")


def _plan(env, line: str = LINE, holder: str = "", operation_id: str = "") -> apply.ManagedMemoryAcceptIntent:
    intent = apply.plan_memory_accept(
        env.vault,
        workspace="work",
        guide=env.guide,
        bullet=parse_bullet(line),
        holder=holder,
        operation_id=operation_id or uuid4().hex,
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
    with pytest.raises(RuntimeError):
        apply.apply_memory_accept(intent, vault_root=install.vault)
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
