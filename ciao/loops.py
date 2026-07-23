"""In-chat loops: re-dispatch the same prompt into a fixed chat every N minutes.

Loops are the sub-day sibling of schedules. A schedule fires at a wall-clock
time and usually opens a new chat per run; a loop lives inside one existing
chat and re-sends its prompt on a minute-level interval, keeping the
conversation (and its context) going. Loops never override the target chat's
model or mode: each iteration runs with whatever the user configured on the
chat itself.

Runtime state ("is this loop currently running?") is intentionally NOT
persisted per se: on server start, loops with ``autostart`` set begin running,
everything else stays stopped until started from the UI. This is what makes
"starts with the server" vs "manual start" a real distinction instead of a
sticky flag.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from ciao.jsonio import read_json_dict
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 1
TICK_SECONDS = 20


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_iso_utc(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


@dataclass(slots=True)
class LoopEntry:
    """One persisted loop bound to a fixed PWA chat."""

    loop_id: str
    prompt: str
    web_chat_id: str
    created_at: str
    interval_minutes: int = 10
    title: str = ""
    # True = the loop starts running when the server boots. False = it stays
    # stopped until started manually from the UI.
    autostart: bool = False
    # ISO timestamp (UTC) of the last fire through any path (tick or manual
    # "Run now"). The interval is measured from this stamp, so cadence
    # survives restarts instead of every boot firing immediately.
    last_run_at: str = ""
    # Outcome of the most recent iteration: "" (never ran), "running",
    # "ok", "error", "busy" (skipped because the chat had an active turn),
    # or "missing-chat" (target chat gone; the loop was auto-stopped).
    last_status: str = ""
    # "user" for loops the user creates; "system" is reserved for packaged
    # loops (mirrors ScheduleEntry.scope so the UI can group them apart).
    scope: str = "user"

    def interval(self) -> timedelta:
        return timedelta(minutes=max(MIN_INTERVAL_MINUTES, self.interval_minutes))


class LoopStore:
    """JSON-backed storage for loops (``.runtime/loops.json``)."""

    def __init__(self, runtime_root: Path) -> None:
        self._path = runtime_root / "loops.json"
        self._lock = threading.RLock()

    def list(self) -> list[LoopEntry]:
        with self._lock:
            items = [
                self._entry_from_item(item)
                for item in self._load().get("loops", [])
                if isinstance(item, dict)
            ]
            items.sort(key=lambda item: item.created_at)
            return items

    def get(self, loop_id: str) -> LoopEntry | None:
        for item in self.list():
            if item.loop_id == loop_id:
                return item
        return None

    def create(
        self,
        *,
        prompt: str,
        web_chat_id: str,
        interval_minutes: int = 10,
        title: str = "",
        autostart: bool = False,
    ) -> LoopEntry:
        entry = LoopEntry(
            loop_id=f"loop-{uuid.uuid4().hex[:8]}",
            prompt=prompt,
            web_chat_id=web_chat_id,
            created_at=_now_utc().isoformat(timespec="seconds"),
            interval_minutes=max(MIN_INTERVAL_MINUTES, int(interval_minutes)),
            title=title,
            autostart=autostart,
        )
        with self._lock:
            data = self._load()
            data.setdefault("loops", []).append(asdict(entry))
            self._save(data)
        return entry

    def replace(self, entry: LoopEntry) -> None:
        with self._lock:
            data = self._load()
            items = data.setdefault("loops", [])
            for index, item in enumerate(items):
                if item.get("loop_id") == entry.loop_id:
                    items[index] = asdict(entry)
                    self._save(data)
                    return
            items.append(asdict(entry))
            self._save(data)

    def delete(self, loop_id: str) -> bool:
        with self._lock:
            data = self._load()
            before = len(data.setdefault("loops", []))
            data["loops"] = [item for item in data["loops"] if item.get("loop_id") != loop_id]
            if len(data["loops"]) == before:
                return False
            self._save(data)
            return True

    def _load(self) -> dict:
        if not self._path.exists():
            return {"loops": []}
        try:
            data = read_json_dict(self._path)
            return data
        except json.JSONDecodeError:
            return {"loops": []}

    def _save(self, payload: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def _entry_from_item(self, item: dict) -> LoopEntry:
        known = {f.name for f in LoopEntry.__dataclass_fields__.values()}
        filtered = {k: v for k, v in item.items() if k in known}
        return LoopEntry(**filtered)


class LoopManager:
    """Ticks running loops and re-dispatches their prompt into the fixed chat.

    Overlap protection is skip-not-queue: if the target chat still has an
    active turn when a loop comes due, the iteration is skipped and retried
    on the next tick (so it fires as soon as the chat frees up) instead of
    piling queued prompts behind a slow turn.
    """

    def __init__(
        self,
        store: LoopStore,
        *,
        dispatch: Callable[[LoopEntry], Awaitable[dict | None]] | None = None,
        chat_busy: Callable[[str], bool] | None = None,
        chat_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self._store = store
        self._dispatch = dispatch
        self._chat_busy = chat_busy
        self._chat_exists = chat_exists
        self._running: set[str] = set()
        self._inflight: set[str] = set()
        self._tick_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        for entry in self._store.list():
            if entry.autostart:
                self._running.add(entry.loop_id)
        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._loop(), name="loop-manager")

    async def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None

    # ── CRUD passthrough ────────────────────────────────────────────────

    def list(self) -> list[LoopEntry]:
        return self._store.list()

    def get(self, loop_id: str) -> LoopEntry | None:
        return self._store.get(loop_id)

    def create(self, **kwargs) -> LoopEntry:
        return self._store.create(**kwargs)

    def replace(self, entry: LoopEntry) -> None:
        self._store.replace(entry)

    def delete(self, loop_id: str) -> bool:
        self._running.discard(loop_id)
        return self._store.delete(loop_id)

    # ── Runtime state ───────────────────────────────────────────────────

    def is_running(self, loop_id: str) -> bool:
        return loop_id in self._running

    def start_loop(self, loop_id: str) -> LoopEntry:
        entry = self._store.get(loop_id)
        if entry is None:
            raise ValueError(f"Loop '{loop_id}' not found.")
        self._running.add(loop_id)
        return entry

    def stop_loop(self, loop_id: str) -> None:
        self._running.discard(loop_id)

    async def run_now(self, loop_id: str) -> dict:
        """Fire one iteration immediately, even if the loop is stopped.

        Returns ``{"status": "busy"}`` without firing when the target chat
        has an active turn.
        """
        entry = self._store.get(loop_id)
        if entry is None:
            raise ValueError(f"Loop '{loop_id}' not found.")
        if self._chat_exists is not None and not self._chat_exists(entry.web_chat_id):
            return {"loop_id": loop_id, "status": "missing-chat"}
        if loop_id in self._inflight or (
            self._chat_busy is not None and self._chat_busy(entry.web_chat_id)
        ):
            return {"loop_id": loop_id, "status": "busy", "chat_id": entry.web_chat_id}
        self._fire(entry)
        return {"loop_id": loop_id, "status": "started", "chat_id": entry.web_chat_id}

    # ── Ticking ─────────────────────────────────────────────────────────

    def _due(self, entry: LoopEntry, now: datetime) -> bool:
        last = _parse_iso_utc(entry.last_run_at)
        if last is None:
            return True
        return (now - last) >= entry.interval()

    async def tick(self, now: datetime | None = None) -> None:
        current = now or _now_utc()
        entries = {entry.loop_id: entry for entry in self._store.list()}
        # Drop runtime state for loops deleted behind our back (direct file
        # edits are a supported workflow for agents).
        self._running &= set(entries)
        for loop_id in sorted(self._running):
            entry = entries[loop_id]
            if loop_id in self._inflight:
                continue
            if not self._due(entry, current):
                continue
            if self._chat_exists is not None and not self._chat_exists(entry.web_chat_id):
                # Target chat is gone; stop the loop instead of logging every
                # tick forever.
                logger.warning(
                    "Loop %s target chat %s not found; stopping loop",
                    loop_id, entry.web_chat_id,
                )
                self._running.discard(loop_id)
                entry.last_status = "missing-chat"
                self._store.replace(entry)
                continue
            if self._chat_busy is not None and self._chat_busy(entry.web_chat_id):
                # Skip, don't queue: retried on the next tick, so the loop
                # fires as soon as the current turn finishes. last_run_at is
                # deliberately not stamped here.
                if entry.last_status != "busy":
                    entry.last_status = "busy"
                    self._store.replace(entry)
                continue
            self._fire(entry, now=current)

    def _fire(self, entry: LoopEntry, now: datetime | None = None) -> None:
        entry.last_run_at = (now or _now_utc()).isoformat(timespec="seconds")
        entry.last_status = "running"
        self._store.replace(entry)
        self._inflight.add(entry.loop_id)
        asyncio.create_task(self._run_dispatch(entry), name=f"loop-run-{entry.loop_id}")

    async def _run_dispatch(self, entry: LoopEntry) -> None:
        status = "ok"
        try:
            result = await self._dispatch(entry) if self._dispatch is not None else None
            if isinstance(result, dict) and result.get("status"):
                status = str(result["status"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Loop %s dispatch failed", entry.loop_id)
            status = "error"
        finally:
            self._inflight.discard(entry.loop_id)
        # Re-read before stamping: the user may have edited the entry while
        # the iteration was streaming.
        latest = self._store.get(entry.loop_id)
        if latest is not None:
            latest.last_status = status
            self._store.replace(latest)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Loop manager tick failed")
            await asyncio.sleep(TICK_SECONDS)
