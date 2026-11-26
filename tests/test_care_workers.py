"""Tests for the care worker planner and pool (no provider, no I/O)."""

from __future__ import annotations

import asyncio
import json
import random

import pytest

from ciao import care_usage, care_workers
from ciao.care_workers import (
    MAX_BATCHES,
    MAX_IN_FLIGHT,
    WorkerPool,
    WorkItem,
    advance,
    parse_response,
    plan_batches,
)

WS = "ws1"
RUN = "11111111-1111-4111-8111-111111111111"
LEASE = "lease=L3ASE-SECRET-VALUE"
TOKEN = "a" * 40
PATH = "/Users/someone/vault/People/x.md"


def item(n: int, target: str | None = None, **kw: object) -> WorkItem:
    return WorkItem(f"People/n{n:03d}.md#{n}", target or f"People/n{n:03d}.md", f"evidence {n}", **kw)  # type: ignore[arg-type]


def reply(prompt: str, outcome: str = "no_change") -> str:
    keys = [i["item_key"] for i in json.loads(prompt)["items"]]
    return json.dumps({"results": [{"item_key": k, "outcome": outcome, "note": "ok"} for k in keys]})


def pool(call, *, max_calls: int = 100, **kw) -> WorkerPool:  # type: ignore[no-untyped-def]
    return WorkerPool(workspace_id=WS, run_id=RUN, provider="claude", model="claude-haiku-5-5",
                      call=call, max_calls=max_calls, **kw)


def test_grouping_by_target_is_stable_across_input_order() -> None:
    items = [item(i, f"T{i % 4}.md") for i in range(12)]
    shuffled = items[:]
    random.Random(1).shuffle(shuffled)
    a, b = plan_batches(items), plan_batches(shuffled)
    assert a == b
    assert [bt.target for bt in a.batches] == ["T0.md", "T1.md", "T2.md", "T3.md"]
    assert all(len({m.target for m in bt.items}) == 1 for bt in a.batches)
    assert [bt.batch_id for bt in a.batches] == ["b1", "b2", "b3", "b4"]


def test_large_target_splits_into_bounded_batches() -> None:
    plan = plan_batches([item(i, "Same.md") for i in range(12)])
    assert [len(b.items) for b in plan.batches] == [5, 5, 2]


def test_empty_and_deterministic_items_make_no_batch() -> None:
    plan = plan_batches([item(1, deterministic=True), WorkItem("k#e", "E.md", "   ")])
    assert plan.batches == ()
    assert sorted(r for _, r in plan.skipped) == ["deterministic", "empty"]


async def test_no_model_call_when_nothing_to_judge() -> None:
    calls: list[str] = []

    async def call(prompt: str) -> str:
        calls.append(prompt)
        return ""

    p = pool(call)
    result = await p.run(plan_batches([item(1, deterministic=True), WorkItem("k#e", "E.md", "")]))
    assert calls == [] and p.attempted == 0 and result.records == ()


def test_cap_applies_after_filter_so_skipped_items_do_not_use_it() -> None:
    # Deterministic targets sort first; filter-then-limit keeps all 20 model batches.
    items = [item(i, f"A{i:03d}.md", deterministic=True) for i in range(10)]
    items += [item(100 + i, f"Z{i:03d}.md") for i in range(MAX_BATCHES + 10)]
    plan = plan_batches(items)
    assert len(plan.batches) == MAX_BATCHES
    assert plan.skipped_total == 10
    assert plan.deferred_total == 10
    assert plan.deferred == tuple(f"People/n{100 + i:03d}.md#{100 + i}" for i in range(MAX_BATCHES, MAX_BATCHES + 10))


def test_listed_skipped_is_bounded_but_total_is_exact() -> None:
    plan = plan_batches([item(i, deterministic=True) for i in range(care_workers.MAX_LISTED + 50)])
    assert len(plan.skipped) == care_workers.MAX_LISTED
    assert plan.skipped_total == care_workers.MAX_LISTED + 50


def test_duplicate_and_bad_keys_are_rejected() -> None:
    with pytest.raises(ValueError):
        plan_batches([item(1), item(1)])
    for bad in ("/abs/path.md", "a/../b.md", "", "a\\b", "C:\\x"):
        with pytest.raises(ValueError):
            WorkItem(bad, "T.md", "e")
        with pytest.raises(ValueError):
            WorkItem("k", bad, "e")


async def test_peak_in_flight_is_at_most_three_across_seven_batches() -> None:
    live = peak = 0

    async def call(prompt: str) -> str:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return reply(prompt)

    p = pool(call)
    result = await p.run(plan_batches([item(i) for i in range(7)]))
    assert MAX_IN_FLIGHT == 3
    assert peak == 3
    assert [r.state for r in result.records] == ["validated"] * 7
    assert p.attempted == 7


async def test_request_seam_carries_only_projected_evidence() -> None:
    seen: list[str] = []

    async def call(prompt: str) -> str:
        seen.append(prompt)
        return reply(prompt)

    evidence = f"note says {LEASE} and token: {TOKEN} at {PATH} plus ~/vault/x.md and C:\\Users\\a\\b.md"
    p = pool(call)
    await p.run(plan_batches([WorkItem("People/x.md#1", "People/x.md", evidence)]))
    assert len(seen) == 1
    sent = seen[0]
    for secret in ("L3ASE", TOKEN, "/Users", "someone", "~/vault", "C:\\\\Users", "vault/People"):
        assert secret not in sent
    assert "People/x.md#1" in sent
    assert json.loads(sent)["items"][0]["evidence"].startswith("note says")
    assert not hasattr(p, "lease") and "lease" not in WorkerPool.__init__.__code__.co_varnames


async def test_records_and_outcomes_hold_no_secret_or_path() -> None:
    async def call(prompt: str) -> str:
        key = json.loads(prompt)["items"][0]["item_key"]
        note = f"see {PATH} {LEASE} " + "x" * 500
        return json.dumps({"results": [{"item_key": key, "outcome": "unsure", "note": note}]})

    p = pool(call)
    result = await p.run(plan_batches([item(1)]))
    text = repr(result)
    assert "someone" not in text and "L3ASE" not in text
    assert len(result.outcomes[0].note) <= care_workers.MAX_NOTE_CHARS


async def test_results_in_shuffled_completion_order_keep_plan_order() -> None:
    finished: list[str] = []

    async def call(prompt: str) -> str:
        key = json.loads(prompt)["items"][0]["item_key"]
        n = int(key.rsplit("#", 1)[1])
        await asyncio.sleep(0.002 * (7 - n))  # later batches finish first
        finished.append(key)
        return reply(prompt)

    p = pool(call)
    result = await p.run(plan_batches([item(i) for i in range(7)]))
    assert finished != sorted(finished)
    assert [o.item_key for o in result.outcomes] == [f"People/n{i:03d}.md#{i}" for i in range(7)]
    assert [r.batch_id for r in result.records] == [f"b{i}" for i in range(1, 8)]


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[]", "{}", '{"results": []}', '{"results": [], "x": 1}', '{"results": NaN}',
     '{"results": [{"item_key": "People/n001.md#1", "outcome": "bogus", "note": ""}]}',
     '{"results": [{"item_key": "other", "outcome": "unsure", "note": ""}]}',
     '{"results": [{"item_key": "People/n001.md#1", "outcome": "unsure", "note": "", "extra": 1}]}',
     "x" * (care_workers.MAX_RESPONSE_CHARS + 1)],
)
async def test_malformed_result_is_invalid_and_counted(text: str) -> None:
    async def call(prompt: str) -> str:
        return text

    p = pool(call)
    result = await p.run(plan_batches([item(1)]))
    rec = result.records[0]
    assert (rec.state, rec.error, rec.output) == ("invalid", "malformed", None)
    assert result.outcomes == () and p.attempted == 1
    assert [o.outcome for o in p.observations()] == ["completed"]


async def test_partial_success_keeps_good_batches() -> None:
    async def call(prompt: str) -> str:
        return "garbage" if "evidence 1\"" in prompt else reply(prompt)

    p = pool(call)
    result = await p.run(plan_batches([item(0), item(1), item(2)]))
    assert [r.state for r in result.records] == ["validated", "invalid", "validated"]
    assert [o.item_key.rsplit("#", 1)[1] for o in result.outcomes] == ["0", "2"]


async def test_call_exception_is_invalid_without_leaking_message() -> None:
    async def call(prompt: str) -> str:
        raise RuntimeError(f"boom {PATH} {LEASE}")

    p = pool(call)
    result = await p.run(plan_batches([item(1)]))
    assert (result.records[0].state, result.records[0].error) == ("invalid", "call_failed")
    assert "someone" not in repr(result) and p.attempted == 1


async def test_timeout_counts_the_attempt() -> None:
    async def call(prompt: str) -> str:
        await asyncio.sleep(5)
        return reply(prompt)

    p = pool(call, call_timeout_s=0.02)
    result = await p.run(plan_batches([item(1), item(2)]))
    assert [(r.state, r.error) for r in result.records] == [("timed_out", "timeout")] * 2
    assert p.attempted == 2
    assert [o.outcome for o in p.observations()] == ["failed", "failed"]


async def test_cancellation_still_counts_attempts_and_marks_records() -> None:
    started = asyncio.Event()

    async def call(prompt: str) -> str:
        started.set()
        await asyncio.sleep(30)
        return reply(prompt)

    p = pool(call)
    task = asyncio.ensure_future(p.run(plan_batches([item(i) for i in range(5)])))
    await started.wait()
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    states = sorted(p.record(WS, f"b{i}.a1").state for i in range(1, 6))  # type: ignore[union-attr]
    assert states == ["interrupted"] * 5
    assert p.attempted == 3  # only the three that were dispatched were reserved
    assert len(p.observations()) == 3
    assert {o.outcome for o in p.observations()} == {"failed"}


async def test_call_budget_is_reserved_before_dispatch_and_defers_the_rest() -> None:
    calls = 0

    async def call(prompt: str) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)
        return reply(prompt)

    p = pool(call, max_calls=4)
    result = await p.run(plan_batches([item(i) for i in range(7)]))
    assert calls == 4 and p.attempted == 4
    assert result.deferred_batches == ("b5", "b6", "b7")
    assert [r.state for r in result.records[4:]] == ["planned"] * 3


async def test_elapsed_budget_stops_dispatch() -> None:
    now = [0.0]

    async def call(prompt: str) -> str:
        now[0] += 10
        return reply(prompt)

    p = pool(call, time_budget_s=15, clock=lambda: now[0])
    result = await p.run(plan_batches([item(i) for i in range(7)]))
    # Dispatch is cooperative here: the first calls finish, then the clock is past the deadline.
    assert p.attempted < 7 and result.deferred_batches


def test_state_machine_rejects_late_results_and_bad_transitions() -> None:
    rec = care_workers.WorkerRecord(WS, RUN, "b1", "b1.a1", 1, "planned", ("k",))
    running = advance(rec, "running")
    timed = advance(running, "timed_out", error="timeout")
    with pytest.raises(ValueError):
        advance(timed, "validated", output=())  # a late result cannot overwrite a timeout
    with pytest.raises(ValueError):
        advance(rec, "validated")
    with pytest.raises(ValueError):
        care_workers.WorkerRecord(WS, RUN, "b1", "b1.a1", 1, "planned", ("k",), error="Traceback ...")
    with pytest.raises(ValueError):
        care_workers.WorkerRecord("../x", RUN, "b1", "b1.a1", 1, "planned", ("k",))


async def test_records_are_workspace_bound() -> None:
    async def call(prompt: str) -> str:
        return reply(prompt)

    p = pool(call)
    await p.run(plan_batches([item(1)]))
    assert p.record(WS, "b1.a1") is not None
    assert p.record("other", "b1.a1") is None
    assert p.record(WS, "b9.a1") is None
    rec = p.record(WS, "b1.a1")
    assert rec is not None and (rec.workspace_id, rec.run_id, rec.batch_id, rec.attempt) == (WS, RUN, "b1", 1)


async def test_observations_feed_care_usage_with_cancelled_calls_counted() -> None:
    async def call(prompt: str) -> str:
        return reply(prompt)

    p = pool(call, max_calls=2)
    await p.run(plan_batches([item(i) for i in range(3)]))
    report = care_usage.collect(p.inventory(), p.observations())
    assert report.proven and report.unbound == 0 and report.mismatched == 0
    calls = report.partitions["care_worker"]["model_calls"]
    assert calls.status == "partial"  # b3 was never dispatched, so its usage is unknown


async def test_pool_run_twice_is_rejected() -> None:
    async def call(prompt: str) -> str:
        return reply(prompt)

    p = pool(call)
    await p.run(plan_batches([item(1)]))
    with pytest.raises(ValueError):
        await p.run(plan_batches([item(1)]))


def test_parse_response_orders_by_batch_not_model() -> None:
    batch = plan_batches([item(1, "S.md"), item(2, "S.md")]).batches[0]
    k1, k2 = (m.item_key for m in batch.items)
    text = json.dumps({"results": [
        {"item_key": k2, "outcome": "unsure", "note": "b"},
        {"item_key": k1, "outcome": "no_change", "note": "a"},
    ]})
    out = parse_response(batch, text)
    assert out is not None and [o.item_key for o in out] == [k1, k2]


def test_worker_links_carry_only_ids_and_state() -> None:
    rec = care_workers.WorkerRecord(WS, RUN, "b1", "b1.a1", 1, "planned", ("k",))
    res = care_workers.RunResult((rec,), (), ("b1",), plan_batches([]))
    assert care_workers.worker_links(res) == [{"batch_id": "b1", "attempt_id": "b1.a1", "state": "planned"}]


def test_production_call_is_one_attempt_with_the_closed_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake(prompt: str, **kw: object) -> str:
        seen.update(kw, prompt=prompt)
        return "x"

    monkeypatch.setattr(care_workers, "run_oneshot", fake)
    assert asyncio.run(care_workers.oneshot_call(provider="claude", model="m", timeout_s=9)("p")) == "x"
    assert seen["max_retries"] == 0 and seen["system_prompt"] == care_workers.SYSTEM_PROMPT
    assert seen["timeout_s"] == 9 and "cwd" not in seen and "env" not in seen
