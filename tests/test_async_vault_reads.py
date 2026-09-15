"""SYS-01: bounded off-loop vault reads (heartbeat, concurrency, recovery).

The heavy vault reads — FTS index/search, backlink traversal, markdown-path
enumeration — are synchronous disk/SQLite work. These tests pin the contract
that moved them off the event loop:

- a deliberately slow read cannot stall a heartbeat coroutine or an unrelated
  health response;
- concurrent reads share a bounded worker pool and identical in-flight reads
  coalesce, while SQLite connections stay inside one worker thread;
- cancelling an awaiter detaches it without losing the worker's completion or
  error;
- p50/p95 latency of unrelated requests under a fixed synthetic load is
  reported before (inline) and after (off-loop).
"""

from __future__ import annotations

import asyncio
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from ciao import async_reads
from ciao import control_plane as cp_module
from ciao.async_reads import run_read
from ciao.control_plane import CiaoControlPlane, McpPrincipal
from ciao.fts_search import NO_MATCH_KEY_PREFIX
from ciao.web import routes_api

SLOW_READ_SECONDS = 0.4
HEARTBEAT_INTERVAL = 0.01


@pytest.fixture(autouse=True)
def _fresh_executor():
    async_reads.reset_vault_read_executor()
    yield
    async_reads.reset_vault_read_executor()


def _note(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: note\n---\n# {title}\n", encoding="utf-8")


def _plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[CiaoControlPlane, McpPrincipal]:
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".ciao"))
    vault = tmp_path / "memory-vault"
    _note(vault / "personal" / "People" / "Alba.md", "Alba")
    _note(vault / "work" / "People" / "Aymen.md", "Aymen")
    config = SimpleNamespace(
        workspace=lambda name: object() if name in {"personal", "work"} else None,
        vault_root=vault,
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        agent_vault_root=lambda name: vault,
        state_path_parent=tmp_path / ".runtime",
    )
    pcm = SimpleNamespace(_workspace_vault_root=lambda ws: vault / ws)
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
    )
    principal = McpPrincipal(
        token_id="token-1",
        chat_id="chat-1",
        project_id="proj-1",
        workspace="personal",
        provider="claude",
    )
    return control_plane, principal


class _Heartbeat:
    """Counts ticks of a 10ms coroutine while an operation is in flight."""

    def __init__(self) -> None:
        self.beats = 0
        self.started = False
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        self.started = True
        while True:
            self.beats += 1
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await self._task


async def _run_while_heartbeating(coro, heartbeat: _Heartbeat):
    heartbeat.start()
    # Wait until the heartbeat coroutine actually starts ticking.
    while not heartbeat.started:
        await asyncio.sleep(0)
    before = heartbeat.beats
    result = await coro
    during = heartbeat.beats - before
    await heartbeat.stop()
    return result, during


# -- acceptance: slow scan cannot stop a heartbeat --------------------------


def test_a_slow_vault_search_does_not_stop_a_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control_plane, principal = _plane(tmp_path, monkeypatch)

    def _slow_index(conn, root, *, path_base=None):
        time.sleep(SLOW_READ_SECONDS)
        return (0, 0)

    monkeypatch.setattr(cp_module, "index_vault", _slow_index)

    async def _scenario() -> tuple[dict, int]:
        heartbeat = _Heartbeat()
        return await _run_while_heartbeating(
            control_plane.vault_search(principal, "Alba"), heartbeat
        )

    result, beats_during = asyncio.run(_scenario())

    assert result["ok"] is True
    # The loop was free for the whole slow read: ~40 ticks at 10ms.
    assert beats_during >= SLOW_READ_SECONDS / HEARTBEAT_INTERVAL / 2, beats_during


def test_a_slow_backlink_scan_does_not_stop_a_health_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "memory-vault"
    _note(vault / "DocA.md", "Doc A")
    _note(vault / "DocB.md", "Doc B")

    def _slow_backlinks(config, target_path):
        time.sleep(SLOW_READ_SECONDS)
        return []

    monkeypatch.setattr(routes_api, "_collect_vault_backlinks", _slow_backlinks)

    app = _routes_app(tmp_path, vault)

    async def _scenario() -> tuple[float, list[int]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            heartbeat = _Heartbeat()
            heartbeat.start()
            while not heartbeat.started:
                await asyncio.sleep(0)

            async def _slow_read():
                return await client.get("/api/vault/backlinks?path=memory-vault/DocB.md")

            async def _health_probe() -> list[float]:
                latencies: list[float] = []
                for _ in range(5):
                    start = time.perf_counter()
                    resp = await client.get("/api/health")
                    latencies.append(time.perf_counter() - start)
                    assert resp.status_code == 200
                    await asyncio.sleep(0.02)
                return latencies

            slow_task = asyncio.ensure_future(_slow_read())
            probe_task = asyncio.ensure_future(_health_probe())
            await slow_task
            health_latencies = await probe_task
            await heartbeat.stop()
            return max(health_latencies), [heartbeat.beats]

    worst_health, beats = asyncio.run(_scenario())
    assert worst_health < SLOW_READ_SECONDS / 3, worst_health
    assert beats[0] >= 5


# -- acceptance: bounded concurrency, coalescing, no cross-thread SQLite -----


def test_concurrent_identical_reads_coalesce_into_one_worker_call() -> None:
    calls = 0
    started = threading.Event()
    release = threading.Event()

    def _operation() -> str:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(3)
        return "value"

    async def _scenario() -> list[str]:
        tasks = [asyncio.ensure_future(run_read("same-key", _operation)) for _ in range(5)]
        while not started.is_set():
            await asyncio.sleep(0.005)
        release.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(_scenario())

    assert results == ["value"] * 5
    assert calls == 1


def test_worker_pool_width_is_bounded() -> None:
    assert async_reads.MAX_VAULT_READ_WORKERS >= 1
    executor = async_reads.vault_read_executor()
    assert executor.max_workers == async_reads.MAX_VAULT_READ_WORKERS


def test_concurrent_distinct_reads_are_bounded_and_do_not_cross_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control_plane, principal = _plane(tmp_path, monkeypatch)

    connect_threads: list[int] = []
    close_threads: list[int] = []
    operation_threads: list[int] = []
    real_connect = sqlite3.connect

    class _RecordingConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def close(self) -> None:
            close_threads.append(threading.get_ident())
            self._inner.close()

    def _connect(*args, **kwargs):
        connect_threads.append(threading.get_ident())
        return _RecordingConnection(real_connect(*args, **kwargs))

    def _slow_index(conn, root, *, path_base=None):
        operation_threads.append(threading.get_ident())
        time.sleep(0.05)
        return (0, 0)

    monkeypatch.setattr(cp_module.sqlite3, "connect", _connect)
    monkeypatch.setattr(cp_module, "index_vault", _slow_index)

    async def _scenario() -> None:
        await asyncio.gather(
            *(
                control_plane.vault_search(principal, f"query-{i}")
                for i in range(12)
            )
        )

    loop_thread = threading.get_ident()
    asyncio.run(_scenario())

    assert len(connect_threads) == 12
    # Every connection is opened and closed on a worker thread, never the loop.
    assert all(tid != loop_thread for tid in connect_threads)
    # A connection, its index pass, and its close all happen on one thread per
    # call; across calls the pool interleaves, so compare as multisets.
    assert sorted(connect_threads) == sorted(operation_threads)
    assert sorted(connect_threads) == sorted(close_threads)
    # All 12 connections were opened/closed by at most the pool width at once;
    # the set of distinct worker threads never exceeds the bound.
    assert len(set(connect_threads)) <= async_reads.MAX_VAULT_READ_WORKERS


def test_workspace_scope_is_stable_under_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control_plane, principal = _plane(tmp_path, monkeypatch)

    def _slow_index(conn, root, *, path_base=None):
        time.sleep(0.02)
        return (0, 0)

    monkeypatch.setattr(cp_module, "index_vault", _slow_index)
    real_search = cp_module.search_vault
    prefixes: list[str] = []

    def _recording_search(conn, query, limit=10, *, path_prefix=""):
        prefixes.append(path_prefix)
        return real_search(conn, query, limit=limit, path_prefix=path_prefix)

    monkeypatch.setattr(cp_module, "search_vault", _recording_search)

    async def _scenario() -> None:
        await asyncio.gather(
            *(control_plane.vault_search(principal, f"q{i}") for i in range(8))
        )

    asyncio.run(_scenario())

    # The personal workspace's own vault prefix for every call, never a sibling.
    expected = cp_module.vault_key_prefix(
        control_plane._vault_root(principal), control_plane._search_key_base()
    )
    assert prefixes and set(prefixes) == {expected}
    assert expected not in {"", NO_MATCH_KEY_PREFIX}


# -- acceptance: serialized shared-database writes ---------------------------


def test_concurrent_distinct_searches_serialize_their_index_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #467 review: distinct query keys must not index the same DB at once.

    Each search's ``_search`` closure calls ``index_vault`` against the shared
    ``vault-fts.db``. Different query strings mean different coalescing keys, so
    without serialization several index passes run in parallel and race SQLite's
    one file-level write lock. Only one writer may be inside the critical
    section at a time, while the read-only search phase stays concurrent.
    """
    control_plane, principal = _plane(tmp_path, monkeypatch)

    active_writers = 0
    max_active_writers = 0
    writer_guard = threading.Lock()

    def _counting_index(conn, root, *, path_base=None):
        nonlocal active_writers, max_active_writers
        with writer_guard:
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
        time.sleep(0.05)
        with writer_guard:
            active_writers -= 1
        return (0, 0)

    monkeypatch.setattr(cp_module, "index_vault", _counting_index)

    async def _scenario() -> None:
        await asyncio.gather(
            *(control_plane.vault_search(principal, f"distinct-{i}") for i in range(6))
        )

    asyncio.run(_scenario())

    # One writer inside the index critical section at a time, never two.
    assert max_active_writers == 1, max_active_writers


def test_vault_index_refresh_and_search_share_one_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh path writes the same database, so it takes the same lock."""
    control_plane, principal = _plane(tmp_path, monkeypatch)

    active_writers = 0
    max_active_writers = 0
    writer_guard = threading.Lock()

    def _counting_index(conn, root, *, path_base=None):
        nonlocal active_writers, max_active_writers
        with writer_guard:
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
        time.sleep(0.05)
        with writer_guard:
            active_writers -= 1
        return (0, 0)

    monkeypatch.setattr(cp_module, "index_vault", _counting_index)

    async def _scenario() -> None:
        await asyncio.gather(
            control_plane.vault_index_refresh(principal),
            control_plane.vault_search(principal, "some-query"),
            control_plane.vault_search(principal, "other-query"),
        )

    asyncio.run(_scenario())

    assert max_active_writers == 1, max_active_writers


# -- acceptance: bounded pending queue / backpressure ------------------------


def test_uncapped_submissions_cannot_create_an_unbounded_backlog() -> None:
    """PR #467 review: outstanding work is capped, not just worker width.

    ``ThreadPoolExecutor``'s queue is unbounded and a cancelled caller leaves
    its read running, so a burst of distinct keys would otherwise pile up
    full-vault scans. More submissions than the cap must wait for a slot; when
    the blockers are released all of them still run.
    """
    executor = async_reads.vault_read_executor()
    blockers: list[threading.Event] = []
    for _ in range(executor.max_workers):
        event = threading.Event()
        executor._executor.submit(lambda e=event: e.wait(5))
        blockers.append(event)

    async def _scenario() -> list[int]:
        results: list[int] = []

        async def _submit(index: int) -> None:
            results.append(await run_read(f"backlog-{index}", lambda i=index: i))

        tasks = [
            asyncio.ensure_future(_submit(i))
            for i in range(executor.max_backlog + 5)
        ]
        # Let every admission attempt run against the saturated pool.
        for _ in range(50):
            await asyncio.sleep(0.01)
        # Outstanding is capped: no more than the backlog, so no unbounded queue.
        assert executor.outstanding() <= executor.max_backlog
        # Nothing completed, because all workers are held by the blockers.
        assert results == []

        for event in blockers:
            event.set()
        await asyncio.gather(*tasks)
        return sorted(results)

    results = asyncio.run(_scenario())

    # Backpressure delayed work, it did not drop it.
    assert results == list(range(executor.max_backlog + 5))


def test_waiter_resumes_without_blocking_the_event_loop() -> None:
    """A caller waiting for a slot must still let the loop serve other work."""
    executor = async_reads.vault_read_executor()
    blockers: list[threading.Event] = []
    for _ in range(executor.max_workers):
        event = threading.Event()
        executor._executor.submit(lambda e=event: e.wait(5))
        blockers.append(event)

    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.005)

    async def _scenario() -> int:
        heartbeat = asyncio.ensure_future(_heartbeat())
        tasks = [
            asyncio.ensure_future(run_read(f"wait-{i}", lambda i=i: i))
            for i in range(executor.max_backlog + 3)
        ]
        await asyncio.sleep(0.1)
        ticks_while_waiting = ticks
        for event in blockers:
            event.set()
        await asyncio.gather(*tasks)
        heartbeat.cancel()
        with pytest.raises(asyncio.CancelledError):
            await heartbeat
        return ticks_while_waiting

    ticks_while_waiting = asyncio.run(_scenario())

    # The loop kept ticking while callers were parked on the backlog.
    assert ticks_while_waiting >= 5


# -- acceptance: cancellation and recovery ----------------------------------


def test_cancelling_an_awaiter_detaches_but_observes_completion() -> None:
    started = threading.Event()
    release = threading.Event()
    finished: list[bool] = []

    def _operation() -> str:
        started.set()
        release.wait(3)
        finished.append(True)
        return "late-value"

    async def _scenario() -> tuple[str, int]:
        task = asyncio.ensure_future(run_read("cancel-key", _operation))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The cancelled awaiter leaves the worker running; a later caller joins
        # the same in-flight work instead of starting a duplicate scan. Yield so
        # the rejoin actually submits before the worker is released.
        rejoined = asyncio.ensure_future(run_read("cancel-key", _operation))
        await asyncio.sleep(0)
        release.set()
        value = await rejoined
        return value, len(finished)

    value, completions = asyncio.run(_scenario())

    assert value == "late-value"
    assert completions == 1


def test_a_worker_error_is_reported_and_not_cached() -> None:
    attempts = 0

    def _operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("disk exploded")
        return "recovered"

    async def _scenario() -> tuple[type[BaseException], str]:
        with pytest.raises(RuntimeError):
            await run_read("error-key", _operation)
        # A failed read is not remembered as in-flight, so the next call retries.
        return RuntimeError, await run_read("error-key", _operation)

    _expected, recovered = asyncio.run(_scenario())

    assert recovered == "recovered"
    assert attempts == 2


def test_cancellation_does_not_grow_the_in_flight_registry() -> None:
    started = threading.Event()
    release = threading.Event()

    def _operation() -> str:
        started.set()
        release.wait(3)
        return "value"

    async def _scenario() -> list[str]:
        task = asyncio.ensure_future(run_read("cancel-registry", _operation))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Still exactly the one real worker, not one entry per cancelled caller.
        keys = async_reads.vault_read_executor().inflight_keys()
        release.set()
        return keys

    keys = asyncio.run(_scenario())

    assert keys == ["cancel-registry"]


def test_cancelling_one_waiter_does_not_cancel_a_shared_queued_read() -> None:
    """PR #467 review: a shared future must survive one caller's cancellation.

    With all workers busy a coalesced read sits in the queue. Before the fix the
    caller awaited the shared concurrent future directly, so cancelling one
    waiter propagated into it: the operation never ran and every other same-key
    waiter got ``CancelledError`` too.
    """
    executor = async_reads.vault_read_executor()
    blockers: list[threading.Event] = []
    for _ in range(executor.max_workers):
        event = threading.Event()
        executor._executor.submit(lambda e=event: e.wait(5))
        blockers.append(event)

    started = threading.Event()

    def _operation() -> str:
        started.set()
        return "value"

    async def _waiter() -> str:
        return await run_read("shared-queued", _operation)

    async def _scenario() -> tuple[str, bool, list[str]]:
        first = asyncio.ensure_future(_waiter())
        second = asyncio.ensure_future(_waiter())
        await asyncio.sleep(0.05)
        # The queued read has not started: all workers are occupied.
        assert started.is_set() is False

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        for event in blockers:
            event.set()
        try:
            value = await second
        except asyncio.CancelledError:  # pragma: no cover - regression guard
            raise AssertionError("cancelling one waiter cancelled the shared read")
        return value, started.is_set(), executor.inflight_keys()

    value, ran, keys = asyncio.run(_scenario())

    assert value == "value"
    assert ran is True
    assert keys == []


def test_each_waiter_awaits_its_own_future() -> None:
    """A bridge future per caller, not the shared one, is what shielding needs."""
    executor = async_reads.vault_read_executor()
    started = threading.Event()
    release = threading.Event()

    def _operation() -> str:
        started.set()
        release.wait(3)
        return "value"

    async def _scenario() -> tuple[object, object]:
        first = executor.submit("bridge-key", _operation, coalesce=True)
        second = executor.submit("bridge-key", _operation, coalesce=True)
        while not started.is_set():
            await asyncio.sleep(0.005)
        assert first is not second
        # Cancelling one caller's own future leaves the shared work pending.
        first.cancel()
        release.set()
        return first, second

    first, second = asyncio.run(_scenario())

    assert first.cancelled() is True
    assert second.result() == "value"


# -- acceptance: p50/p95 before and after -----------------------------------


def _percentiles(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return p50, p95


def test_reports_p50_p95_latency_before_and_after(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fixed synthetic workload: 20 reads of 30ms plus a 10ms heartbeat.

    "Before" runs each read inline on the event loop — the heartbeat coroutine
    observes the read's full duration every iteration. "After" runs it through
    ``run_read`` and the heartbeat keeps its own cadence. The printed figures are
    the artifact the ticket asks to report; the assertion is deliberately loose.
    """
    read_seconds = 0.03
    iterations = 20
    heartbeat_interval = 0.01

    def _sync_read() -> None:
        time.sleep(read_seconds)

    async def _heartbeat_latencies(inline: bool) -> list[float]:
        loop = asyncio.get_running_loop()
        samples: list[float] = []

        async def _heartbeat() -> None:
            next_tick = loop.time()
            for _ in range(iterations):
                next_tick += heartbeat_interval
                delay = max(0.0, next_tick - loop.time())
                await asyncio.sleep(delay)
                samples.append(max(0.0, loop.time() - next_tick))

        async def _workload() -> None:
            for _ in range(iterations):
                if inline:
                    _sync_read()
                else:
                    await run_read("latency-blocker", _sync_read)

        heartbeat = asyncio.ensure_future(_heartbeat())
        await asyncio.sleep(0)
        workload = asyncio.ensure_future(_workload())
        await asyncio.gather(heartbeat, workload)
        return samples

    before = asyncio.run(_heartbeat_latencies(inline=True))
    after = asyncio.run(_heartbeat_latencies(inline=False))
    before_p50, before_p95 = _percentiles(before)
    after_p50, after_p95 = _percentiles(after)

    with capsys.disabled():
        print(
            "\nSYS-01 synthetic heartbeat latency "
            f"(read={read_seconds * 1000:.0f}ms x{iterations}, "
            f"heartbeat={heartbeat_interval * 1000:.0f}ms): "
            f"before p50={before_p50 * 1000:.1f}ms p95={before_p95 * 1000:.1f}ms | "
            f"after p50={after_p50 * 1000:.1f}ms p95={after_p95 * 1000:.1f}ms"
        )

    # Inline, every heartbeat is parked behind a full read.
    assert before_p50 >= read_seconds / 2, before_p50
    # Off-loop, the heartbeat keeps its own schedule.
    assert after_p50 < read_seconds / 2, after_p50
    assert after_p50 < before_p50
    assert after_p95 < before_p95


def _routes_app(tmp_path: Path, vault: Path) -> Starlette:
    config = SimpleNamespace(workspace_root=tmp_path, vault_root=vault)

    async def _health(_request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/api/vault/backlinks", routes_api.vault_backlinks, methods=["GET"]),
            Route("/api/vault-markdown-paths", routes_api.vault_markdown_paths, methods=["GET"]),
            Route("/api/health", _health, methods=["GET"]),
        ]
    )
    app.state.config = config
    return app
