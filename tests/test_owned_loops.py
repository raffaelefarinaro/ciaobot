"""Chat-owned loops: a bounded interval run that lives in one chat.

A loop is an interval schedule with an owner chat, a hard expiry and a run
cap. The scheduler ends it by deleting its row. These tests pin the store
door (the only way to create one), the invariants every write door enforces,
the scheduler's cadence and counting, and the restore side.
"""

from __future__ import annotations

import asyncio
import itertools
import dataclasses
import hashlib
import json
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from ciao.schedules import (
    LOOP_MAX_GLOBAL,
    LOOP_MAX_PER_CHAT,
    LOOP_MAX_PROMPT_CHARS,
    LOOP_MAX_RUNS,
    LoopRefused,
    ScheduleManager,
    ScheduleStore,
    compute_next_run,
    is_owned_loop,
    loop_end_reason,
    restorable_user_schedule,
)
from ciao.web.schedule_dispatch import ScheduleDispatcher, ScheduleDispatchHost
from tests.test_schedule_dispatch_boundary import (
    _RealManagerHost,
    _workspace_pair_manager,
)

T0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)

# Fields a loop adds to a row. A row written before loops existed has none.
LOOP_KEYS = ("owner_chat_id", "expires_at", "max_runs", "run_count")


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path)


_PROMPTS = itertools.count(1)


def _loop(store: ScheduleStore, *, chat: str = "chat-a", now: datetime = T0, **kwargs: Any):
    # A fresh prompt per loop: an identical prompt and cadence in one chat is a duplicate, refused by design.
    params: dict[str, Any] = {"prompt": f"check PRs {next(_PROMPTS)}", "interval_minutes": 10}
    params.update(kwargs)
    return store.create_owned_loop(owner_chat_id=chat, now=now, **params)


def _edit_raw(tmp_path: Path, schedule_id: str, **changes: Any) -> None:
    """Change a stored row directly, the way a hand edit or an old file would."""
    path = tmp_path / "schedules.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload["schedules"]:
        if row["schedule_id"] == schedule_id:
            row.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _manager(store: ScheduleStore, *, gate: asyncio.Event | None = None, **kwargs: Any):
    dispatched: list[str] = []

    async def dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)
        if gate is not None:
            await gate.wait()
        return {"status": "ok", "chat_id": target_chat_id}

    params: dict[str, Any] = {
        "store": store,
        "dispatch_to_web": dispatch_to_web,
        "prepare_chat": lambda entry, prompt, model, mode, provider: entry.web_chat_id,
        "chat_busy": lambda chat_id: False,
        "chat_dispatchable": lambda entry: True,
    }
    params.update(kwargs)
    return ScheduleManager(**params), dispatched


async def _settle() -> None:
    """Let the fire-and-forget run task finish."""
    await asyncio.sleep(0.05)


# ── Existing rows and digests ───────────────────────────────────────────


def test_a_row_written_before_loops_loads_as_an_ordinary_schedule(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="interval", interval_minutes=10, web_chat_id="chat-x",
    )
    payload = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))
    for row in payload["schedules"]:
        for key in LOOP_KEYS:
            row.pop(key)
    (tmp_path / "schedules.json").write_text(json.dumps(payload), encoding="utf-8")

    old = store.get(entry.schedule_id)
    assert old is not None
    assert not is_owned_loop(old)
    assert (old.expires_at, old.max_runs, old.run_count) == ("", 0, 0)


def test_an_ordinary_row_keeps_the_revision_it_had_before_loops(tmp_path: Path) -> None:
    """The digest an editor holds across this upgrade must still match."""
    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="interval", interval_minutes=10, web_chat_id="chat-x",
    )
    payload = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))
    for row in payload["schedules"]:
        for key in LOOP_KEYS:
            row.pop(key)
    (tmp_path / "schedules.json").write_text(json.dumps(payload), encoding="utf-8")

    old = store.get(entry.schedule_id)
    assert old is not None
    # The content digest as the release before loops computed it: every field
    # except the four loop fields and the run-state and derived fields.
    excluded = {
        "last_triggered_on", "last_dispatched_at", "last_run_chat_id", "last_status",
        "last_recovered_on", "last_dispatch_id", "last_completed_on",
        "created_at", "scope", "editable", "removable", *LOOP_KEYS,
    }
    content = {k: v for k, v in asdict(old).items() if k not in excluded}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert store.revision(entry.schedule_id) == f"0-{expected}"


def test_a_loop_round_trips_through_the_file(store: ScheduleStore) -> None:
    loop = _loop(store)
    reloaded = store.get(loop.schedule_id)
    assert reloaded == loop


def test_a_run_count_bump_keeps_the_revision_and_an_edit_changes_it(
    store: ScheduleStore,
) -> None:
    loop = _loop(store)
    before = store.revision(loop.schedule_id)
    store.update_fields(loop.schedule_id, {"run_count": 3})
    assert store.revision(loop.schedule_id) == before
    current = store.get(loop.schedule_id)
    assert current is not None
    store.replace(dataclasses.replace(current, prompt="check issues"), base=current)
    assert store.revision(loop.schedule_id) != before


# ── Creation: the one door ──────────────────────────────────────────────


def test_a_loop_is_created_with_its_fixed_shape(store: ScheduleStore) -> None:
    loop = _loop(store)
    assert is_owned_loop(loop)
    assert loop.frequency == "interval"
    assert loop.web_chat_id == "chat-a"
    assert loop.web_project_id is None
    assert loop.daily_time_utc == ""
    assert loop.command == ""
    assert loop.fallback_project_id == ""
    assert loop.archive_policy == "manual"
    assert loop.enabled is True
    assert loop.created_at == "2026-07-11T12:00:00Z"
    assert loop.expires_at == "2026-07-11T13:00:00Z"
    assert loop.max_runs == 7  # 60 minutes at 10 minutes, plus the first run
    assert loop.run_count == 0


def test_an_explicit_duration_and_cap_are_kept(store: ScheduleStore) -> None:
    loop = _loop(store, duration_minutes=24 * 60, max_runs=LOOP_MAX_RUNS)
    assert loop.expires_at == "2026-07-12T12:00:00Z"
    assert loop.max_runs == LOOP_MAX_RUNS


@pytest.mark.parametrize(
    "overrides",
    [
        {"interval_minutes": 0},
        {"interval_minutes": True},
        {"duration_minutes": 0},
        {"duration_minutes": 24 * 60 + 1},
        {"max_runs": 0},
        {"max_runs": LOOP_MAX_RUNS + 1},
        {"prompt": "   "},
        {"prompt": "x" * (LOOP_MAX_PROMPT_CHARS + 1)},
    ],
)
def test_a_request_outside_the_caps_is_refused_not_clamped(
    store: ScheduleStore, overrides: dict[str, Any]
) -> None:
    with pytest.raises(LoopRefused) as refused:
        _loop(store, **overrides)
    assert refused.value.code == "loop_invalid"
    assert store.list_entries() == []


def test_a_chat_holds_three_loops_and_the_engine_twenty(store: ScheduleStore) -> None:
    for _ in range(LOOP_MAX_PER_CHAT):
        _loop(store, chat="chat-a")
    with pytest.raises(LoopRefused) as per_chat:
        _loop(store, chat="chat-a")
    assert per_chat.value.code == "loop_limit_chat"

    for index in range(LOOP_MAX_GLOBAL - LOOP_MAX_PER_CHAT):
        _loop(store, chat=f"chat-{index // 3 + 1}")
    with pytest.raises(LoopRefused) as global_cap:
        _loop(store, chat="chat-6")  # chat-6 has two loops, so only the engine cap binds
    assert global_cap.value.code == "loop_limit_global"


def test_an_ended_loop_frees_its_slot(store: ScheduleStore) -> None:
    for _ in range(LOOP_MAX_PER_CHAT):
        _loop(store, chat="chat-a")
    victim = next(e for e in store.list_entries() if e.owner_chat_id == "chat-a")
    store.delete(victim.schedule_id)
    assert _loop(store, chat="chat-a").owner_chat_id == "chat-a"


def test_two_creators_cannot_both_take_the_last_slot(store: ScheduleStore) -> None:
    for _ in range(LOOP_MAX_PER_CHAT - 1):
        _loop(store, chat="chat-a")
    outcomes: list[str] = []
    guard = threading.Lock()
    barrier = threading.Barrier(4)

    def create() -> None:
        barrier.wait()
        try:
            _loop(store, chat="chat-a")
            outcome = "ok"
        except LoopRefused as exc:
            outcome = exc.code
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=create) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert outcomes.count("loop_limit_chat") == 3


# ── Invariants on every write door ──────────────────────────────────────


@pytest.mark.parametrize(
    "change",
    [
        {"web_chat_id": "chat-b"},
        {"web_project_id": "proj-1"},
        {"command": "echo hi"},
        {"fallback_project_id": "proj-1"},
        {"enabled": False},
        {"interval_minutes": 0},
    ],
)
def test_an_edit_cannot_turn_a_loop_into_something_else(
    store: ScheduleStore, change: dict[str, Any]
) -> None:
    _loop(store)
    current = store.list_entries()[0]
    before = asdict(current)
    with pytest.raises(LoopRefused):
        store.replace(dataclasses.replace(current, **change), base=current)
    assert asdict(store.get(current.schedule_id)) == before


def test_a_loop_cannot_change_frequency(store: ScheduleStore) -> None:
    _loop(store)
    current = store.list_entries()[0]
    with pytest.raises(ValueError):
        store.replace(dataclasses.replace(current, frequency="daily"), base=current)


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": "2030-01-01T00:00:00Z"},
        {"max_runs": LOOP_MAX_RUNS},
        {"owner_chat_id": "chat-b", "web_chat_id": "chat-b"},
    ],
)
def test_a_loop_lifetime_is_fixed_at_creation(
    store: ScheduleStore, change: dict[str, Any]
) -> None:
    _loop(store)
    current = store.list_entries()[0]
    with pytest.raises(LoopRefused) as refused:
        store.replace(dataclasses.replace(current, **change), base=current)
    assert refused.value.code == "loop_invalid"
    with pytest.raises(LoopRefused):
        store.replace(dataclasses.replace(current, **change))  # the whole-row door


def test_a_loop_run_count_cannot_be_lowered_to_regain_runs(store: ScheduleStore) -> None:
    loop = _loop(store, max_runs=2)
    store.update_fields(loop.schedule_id, {"run_count": 2})
    current = store.get(loop.schedule_id)
    with pytest.raises(LoopRefused):
        store.replace(dataclasses.replace(current, run_count=0), base=current)
    with pytest.raises(LoopRefused):
        store.replace(dataclasses.replace(current, run_count=0))
    assert store.get(loop.schedule_id).run_count == 2


def test_an_ordinary_schedule_cannot_be_made_a_loop(store: ScheduleStore) -> None:
    ordinary = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="interval", interval_minutes=10, web_chat_id="chat-x",
    )
    with pytest.raises(LoopRefused):
        store.replace(
            dataclasses.replace(ordinary, owner_chat_id="chat-x"), base=ordinary
        )


def test_a_loop_cannot_be_inserted_by_the_plain_write_doors(store: ScheduleStore) -> None:
    loop = _loop(store)
    store.delete(loop.schedule_id)
    with pytest.raises(LoopRefused):
        store.replace(loop)
    with pytest.raises(LoopRefused):
        store.add_if_absent(loop)


def test_a_loop_is_never_auto_archived(store: ScheduleStore) -> None:
    loop = _loop(store)
    current = store.list_entries()[0]
    store.replace(dataclasses.replace(current, archive_policy="auto"), base=current)
    assert store.get(loop.schedule_id).archive_policy == "manual"


# ── Scheduler: cadence and counting ─────────────────────────────────────


async def test_the_first_run_waits_one_interval_from_creation(store: ScheduleStore) -> None:
    loop = _loop(store, interval_minutes=10)
    manager, dispatched = _manager(store)

    await manager.tick(now=T0 + timedelta(minutes=5))
    await _settle()
    assert dispatched == []
    assert compute_next_run(store.get(loop.schedule_id), now=T0 + timedelta(minutes=5)) == (
        T0 + timedelta(minutes=10)
    )

    await manager.tick(now=T0 + timedelta(minutes=10))
    await _settle()
    assert dispatched == [loop.schedule_id]
    assert store.get(loop.schedule_id).run_count == 1


async def test_a_busy_chat_skips_without_counting(store: ScheduleStore) -> None:
    loop = _loop(store)
    manager, dispatched = _manager(store, chat_busy=lambda chat_id: True)
    await manager.tick(now=T0 + timedelta(minutes=10))
    await _settle()
    assert dispatched == []
    latest = store.get(loop.schedule_id)
    assert latest.run_count == 0
    assert latest.last_status == "busy"


async def test_an_admission_deferral_does_not_count(store: ScheduleStore) -> None:
    loop = _loop(store)
    manager, dispatched = _manager(store, admission_closed=lambda: True)
    await manager.tick(now=T0 + timedelta(minutes=10))
    await _settle()
    assert dispatched == []
    latest = store.get(loop.schedule_id)
    assert latest.run_count == 0
    assert latest.last_status == ""


async def test_the_run_cap_ends_the_loop_after_its_last_run(store: ScheduleStore) -> None:
    loop = _loop(store, interval_minutes=1, max_runs=2)
    manager, dispatched = _manager(store)
    for minute in (1, 2):
        await manager.tick(now=T0 + timedelta(minutes=minute))
        await _settle()
    assert dispatched == [loop.schedule_id, loop.schedule_id]

    await manager.tick(now=T0 + timedelta(minutes=3))
    await _settle()
    assert dispatched == [loop.schedule_id, loop.schedule_id]
    assert store.get(loop.schedule_id) is None


async def test_a_tick_past_expiry_deletes_without_firing(store: ScheduleStore) -> None:
    loop = _loop(store, duration_minutes=60, interval_minutes=10)
    manager, dispatched = _manager(store)
    await manager.tick(now=T0 + timedelta(minutes=10))
    await _settle()
    assert dispatched == [loop.schedule_id]

    await manager.tick(now=T0 + timedelta(minutes=60, seconds=1))
    await _settle()
    assert dispatched == [loop.schedule_id]
    assert store.get(loop.schedule_id) is None


async def test_a_paused_row_past_expiry_is_still_swept(
    store: ScheduleStore, tmp_path: Path
) -> None:
    loop = _loop(store, duration_minutes=5)
    _edit_raw(tmp_path, loop.schedule_id, enabled=False)
    manager, dispatched = _manager(store)
    await manager.tick(now=T0 + timedelta(minutes=6))
    await _settle()
    assert dispatched == []
    assert store.get(loop.schedule_id) is None


async def test_a_run_in_flight_at_expiry_is_not_cut_off(store: ScheduleStore) -> None:
    loop = _loop(store, duration_minutes=15, interval_minutes=10)
    gate = asyncio.Event()
    manager, dispatched = _manager(store, gate=gate)
    await manager.tick(now=T0 + timedelta(minutes=10))
    await manager.tick(now=T0 + timedelta(minutes=16))  # past expiry, run still open
    assert store.get(loop.schedule_id) is not None

    gate.set()
    await _settle()
    await manager.tick(now=T0 + timedelta(minutes=16))
    assert dispatched == [loop.schedule_id]
    assert store.get(loop.schedule_id) is None


async def test_run_now_counts_as_a_run(store: ScheduleStore) -> None:
    loop = _loop(store)
    manager, _dispatched = _manager(store)
    result = await manager.dispatch_now(loop.schedule_id, now=T0)
    await _settle()
    assert result["status"] == "started"
    assert store.get(loop.schedule_id).run_count == 1


async def test_two_run_nows_start_one_run(store: ScheduleStore) -> None:
    loop = _loop(store)
    gate = asyncio.Event()
    manager, dispatched = _manager(store, gate=gate)
    first, second = await asyncio.gather(
        manager.dispatch_now(loop.schedule_id, now=T0),
        manager.dispatch_now(loop.schedule_id, now=T0),
    )
    assert sorted([first["status"], second["status"]]) == ["busy", "started"]
    assert store.get(loop.schedule_id).run_count == 1
    gate.set()
    await _settle()
    assert len(dispatched) == 1


async def test_run_now_on_an_ended_loop_is_refused_and_removed(store: ScheduleStore) -> None:
    loop = _loop(store, duration_minutes=5)
    manager, dispatched = _manager(store)
    result = await manager.dispatch_now(loop.schedule_id, now=T0 + timedelta(minutes=6))
    assert result["status"] == "refused"
    assert result["error"] == "the loop has ended"
    assert store.get(loop.schedule_id) is None
    assert dispatched == []


async def test_a_loop_whose_chat_cannot_be_prepared_ends(store: ScheduleStore) -> None:
    loop = _loop(store)
    ended: list[int] = []

    def prepare(*args: object) -> str:
        raise ValueError("the chat is gone")

    manager, dispatched = _manager(
        store, prepare_chat=prepare, on_change=lambda: ended.append(1)
    )
    await manager.tick(now=T0 + timedelta(minutes=10))
    await _settle()
    assert dispatched == []
    assert store.get(loop.schedule_id) is None
    assert ended == [1]


async def test_an_ordinary_interval_whose_chat_cannot_be_prepared_is_disabled(
    store: ScheduleStore,
) -> None:
    entry = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="interval", interval_minutes=10, web_chat_id="chat-x",
    )

    def prepare(*args: object) -> str:
        raise ValueError("the chat is gone")

    manager, dispatched = _manager(store, prepare_chat=prepare)
    await manager.tick(now=T0)
    await _settle()
    assert dispatched == []
    latest = store.get(entry.schedule_id)
    assert latest.enabled is False
    assert latest.last_status == "missing-chat"


async def test_a_loop_whose_owner_chat_is_gone_ends(store: ScheduleStore) -> None:
    loop = _loop(store)
    manager, dispatched = _manager(store, owner_alive=lambda chat_id: False)
    await manager.tick(now=T0 + timedelta(minutes=10))
    await _settle()
    assert dispatched == []
    assert store.get(loop.schedule_id) is None


async def test_each_end_notifies_once(store: ScheduleStore) -> None:
    _loop(store, duration_minutes=5)
    changes: list[int] = []
    manager, _dispatched = _manager(store, on_change=lambda: changes.append(1))
    await manager.tick(now=T0 + timedelta(minutes=6))
    await manager.tick(now=T0 + timedelta(minutes=7))
    assert changes == [1]


@pytest.mark.parametrize(
    ("field", "value"),
    [("expires_at", "not-a-time"), ("max_runs", "x"), ("run_count", None)],
)
async def test_an_unreadable_loop_is_ended_and_never_fired(
    store: ScheduleStore, tmp_path: Path, field: str, value: Any
) -> None:
    loop = _loop(store)
    _edit_raw(tmp_path, loop.schedule_id, **{field: value})
    manager, dispatched = _manager(store)
    await manager.tick(now=T0 + timedelta(minutes=30))
    await _settle()
    assert dispatched == []
    assert store.get(loop.schedule_id) is None


# ── Restore and the mode regression ─────────────────────────────────────


def test_an_archived_loop_is_not_restored(store: ScheduleStore) -> None:
    loop = _loop(store)
    ordinary = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="interval", interval_minutes=10, web_chat_id="chat-x",
    )
    assert restorable_user_schedule(
        asdict(loop), workspace="personal", providers=("claude",)
    ) is None
    assert restorable_user_schedule(
        asdict(ordinary), workspace="personal", providers=("claude",)
    ) is not None


async def test_a_loop_run_keeps_the_chat_mode_and_runs_unattended(tmp_path: Path) -> None:
    """The chat stays in its own mode, whatever the provider default is."""
    chats = _workspace_pair_manager(tmp_path)
    project = chats.create_project("Loops", workspace="personal")
    chat = chats.create_chat(
        project.project_id, title="Loop chat", provider="claude", mode="normal"
    )
    store = ScheduleStore(tmp_path / ".runtime")
    loop = store.create_owned_loop(
        owner_chat_id=chat.chat_id, prompt="check", interval_minutes=10, now=T0
    )
    host = _RealManagerHost(
        chats, [{"type": "result", "text": "done", "is_error": False}]
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    async def dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        return await dispatcher.dispatch_schedule(
            entry, entry.prompt, model, mode, provider, target_chat_id=target_chat_id
        )

    manager = ScheduleManager(
        store=store,
        # The provider default for this run is bypass. A loop must not take it.
        resolve_target=lambda entry: ("claude", "", "bypass", "claude"),
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda entry, prompt, model, mode, provider: (
            chats.prepare_schedule_chat(entry, prompt, model, mode, provider)
        ),
    )
    await manager.tick(now=T0 + timedelta(minutes=10))
    await _settle()

    ((chat_id, wake, unattended),) = host.started
    assert (chat_id, unattended) == (chat.chat_id, True)
    assert wake.startswith("[Ciaobot] Loop wake, run 1 of ")
    assert "<loop-prompt>\ncheck\n</loop-prompt>" in wake
    assert chats.get_chat(chat.chat_id).mode == "normal"
    assert store.get(loop.schedule_id).run_count == 1


# ── Hand-written rows: the shape caps hold on tick and on write ─────────


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"expires_at": "9999-01-01T00:00:00Z"}, "far-future expiry"),
        ({"interval_minutes": 10**6}, "huge interval"),
        ({"max_runs": LOOP_MAX_RUNS + 1}, "run cap above the cap"),
    ],
)
def test_a_hand_written_loop_beyond_the_caps_is_invalid(
    store: ScheduleStore, changes: dict[str, Any], label: str
) -> None:
    loop = _loop(store)
    row = dataclasses.replace(loop, **changes)
    assert loop_end_reason(row, T0 + timedelta(minutes=1)) == "invalid", label
    with pytest.raises(LoopRefused):
        ScheduleStore._finalize_entry(row)


@pytest.mark.parametrize(
    ("field", "value"),
    [("expires_at", "9999-01-01T00:00:00Z"), ("interval_minutes", 10**6), ("max_runs", LOOP_MAX_RUNS + 1)],
)
async def test_a_hand_written_loop_beyond_the_caps_is_ended_by_the_tick(
    store: ScheduleStore, tmp_path: Path, field: str, value: Any
) -> None:
    loop = _loop(store)
    _edit_raw(tmp_path, loop.schedule_id, **{field: value})
    manager, dispatched = _manager(store)
    await manager.tick(now=T0 + timedelta(minutes=10))
    await _settle()
    assert dispatched == []
    assert store.get(loop.schedule_id) is None


# ── Owner gone: no chat is created for it ───────────────────────────────


async def test_a_loop_whose_owner_is_gone_creates_no_chat(store: ScheduleStore) -> None:
    ticked = _loop(store, chat="chat-a")
    run_now = _loop(store, chat="chat-b")
    prepared: list[str] = []

    def prepare(entry, prompt, model, mode, provider):
        prepared.append(entry.schedule_id)
        return entry.web_chat_id

    manager, dispatched = _manager(
        store,
        prepare_chat=prepare,
        owner_alive=lambda chat_id: False,
    )
    # Run now first: the owner is gone, so it must end without preparing a chat.
    result = await manager.dispatch_now(run_now.schedule_id, now=T0)
    await _settle()
    assert result["status"] != "started"
    assert store.get(run_now.schedule_id) is None

    await manager.tick(now=T0 + timedelta(minutes=10))
    await _settle()
    assert store.get(ticked.schedule_id) is None
    assert prepared == []
    assert dispatched == []


# ── A refused write on an ordinary row still raises ─────────────────────


def test_persist_still_raises_a_loop_refusal_for_an_ordinary_row(store: ScheduleStore) -> None:
    ordinary = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="interval", interval_minutes=10, web_chat_id="chat-x",
    )
    manager, _dispatched = _manager(store)
    with pytest.raises(LoopRefused):
        manager._persist(dataclasses.replace(ordinary, owner_chat_id="chat-x"), ordinary)
