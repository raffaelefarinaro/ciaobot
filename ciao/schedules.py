"""Simple daily schedule support for chat-dispatched automations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from ciao.models import BridgeMode

DEFAULT_TIMEZONE = "Europe/Zurich"
logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(second=0, microsecond=0)


WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

ARCHIVE_POLICIES = {"manual", "auto"}
SYSTEM_STATE_FIELDS = {
    "enabled",
    "last_triggered_on",
    "last_dispatched_at",
}


def normalize_archive_policy(value: str | None) -> str:
    normalized = (value or "manual").strip() or "manual"
    if normalized not in ARCHIVE_POLICIES:
        raise ValueError(f"unknown archive_policy '{normalized}'")
    return normalized


def parse_days_of_week(raw: str) -> list[str]:
    """Parse a comma-separated days string into sorted weekday abbreviations.

    Accepts: "mon,wed,fri", "Sun", "monday", etc.  Returns [] for empty/invalid.
    """
    if not raw or not raw.strip():
        return []
    result: list[str] = []
    for token in raw.lower().replace(" ", "").split(","):
        token = token.strip()[:3]
        if token in WEEKDAY_NAMES:
            result.append(token)
    # Sort by weekday order
    return sorted(set(result), key=WEEKDAY_NAMES.index)


def _matches_frequency(entry: "ScheduleEntry", dt_local: datetime) -> bool:
    if entry.frequency == "manual":
        return False  # manual schedules never auto-fire
    if entry.frequency == "once":
        # Fires only when the local date matches run_at_date exactly.
        return bool(entry.run_at_date) and dt_local.date().isoformat() == entry.run_at_date
    if entry.frequency == "monthly":
        return dt_local.day == entry.day_of_month
    if entry.frequency == "weekly":
        if not entry.days_of_week:
            return True
        return WEEKDAY_NAMES[dt_local.weekday()] in entry.days_of_week
    return True  # daily


def compute_next_run(
    entry: "ScheduleEntry", now: datetime | None = None
) -> datetime | None:
    """Return the next datetime this schedule will fire, in the entry's local tz.

    Returns None for disabled/paused, manual schedules (no auto-fire), on
    malformed ``daily_time_utc``, or if no match is found within a year
    (shouldn't happen with valid input).
    """
    if not entry.enabled or entry.frequency == "manual":
        return None
    try:
        hh, mm = entry.daily_time_utc.split(":")
        target_h, target_m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    tz = ZoneInfo(entry.timezone_name)
    now_local = (now or _now_utc()).astimezone(tz)
    # `once` schedules have a fixed target date; if it's already past we
    # return None instead of rolling forward.
    if entry.frequency == "once":
        if not entry.run_at_date:
            return None
        try:
            target_date = datetime.fromisoformat(entry.run_at_date).date()
        except ValueError:
            return None
        target_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            target_h, target_m, tzinfo=tz,
        )
        return target_dt if target_dt > now_local else None
    candidate = now_local.replace(
        hour=target_h, minute=target_m, second=0, microsecond=0
    )
    already_fired_today = entry.last_triggered_on == now_local.date().isoformat()
    if candidate <= now_local or already_fired_today:
        candidate = candidate + timedelta(days=1)
    for _ in range(400):
        if _matches_frequency(entry, candidate):
            return candidate
        candidate += timedelta(days=1)
    return None


def compute_last_expected_run(
    entry: "ScheduleEntry", now: datetime | None = None
) -> datetime | None:
    """Return the most recent datetime this schedule *should* have fired, in
    the entry's local tz, bounded to on/after ``created_at``.

    This is the mirror of :func:`compute_next_run`, walking backwards instead
    of forwards. It is used to detect "missed" runs: a schedule whose last
    expected fire has passed but which never recorded a trigger for that day.

    Returns None for disabled/paused or manual schedules (no auto-fire), on
    malformed ``daily_time_utc``, for ``once`` schedules still in the future,
    or when there is no past due occurrence after creation.
    """
    if not entry.enabled or entry.frequency == "manual":
        return None
    try:
        hh, mm = entry.daily_time_utc.split(":")
        target_h, target_m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    tz = ZoneInfo(entry.timezone_name)
    now_local = (now or _now_utc()).astimezone(tz)
    # Never report a fire from before the schedule existed.
    created_floor: datetime | None = None
    if entry.created_at:
        try:
            created_floor = datetime.fromisoformat(
                entry.created_at.replace("Z", "+00:00")
            ).astimezone(tz)
        except ValueError:
            created_floor = None
    if entry.frequency == "once":
        if not entry.run_at_date:
            return None
        try:
            target_date = datetime.fromisoformat(entry.run_at_date).date()
        except ValueError:
            return None
        target_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            target_h, target_m, tzinfo=tz,
        )
        return target_dt if target_dt <= now_local else None
    candidate = now_local.replace(
        hour=target_h, minute=target_m, second=0, microsecond=0
    )
    if candidate > now_local:
        candidate -= timedelta(days=1)
    for _ in range(400):
        if created_floor is not None and candidate < created_floor:
            return None
        if _matches_frequency(entry, candidate):
            return candidate
        candidate -= timedelta(days=1)
    return None


def was_dispatched_since(entry: "ScheduleEntry", when: datetime) -> bool:
    """True if the schedule's last dispatch through any path (auto tick,
    catch-up, or manual "Run now") happened at or after ``when``.

    Used by the "missed" health check: a dispatch at or after the most recent
    expected fire means the schedule was attended to (even if the cron path
    didn't stamp ``last_triggered_on``, as with a manual run).

    Tolerates both stamp formats written to ``last_dispatched_at``: the UTC
    tz-aware string from ``dispatch_now`` and the naive local-time string from
    ``tick``/``catch_up`` (localized here to the entry's timezone so the
    comparison never mixes naive and aware datetimes).
    """
    raw = entry.last_dispatched_at
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(entry.timezone_name))
    return dt >= when


@dataclass(slots=True)
class ScheduleEntry:
    """One persisted schedule (daily, weekly, or monthly)."""

    schedule_id: str
    daily_time_utc: str
    prompt: str
    chat_id: int
    created_at: str
    model: str = ""
    # Routing key for the chat this schedule will dispatch into. Empty string
    # means "inherit the target chat's provider (existing web_chat_id) or use
    # the resolver's default (new web_project_id chat)".
    provider: str = ""
    mode: BridgeMode = "auto"
    timezone_name: str = DEFAULT_TIMEZONE
    last_triggered_on: str = ""
    # Full ISO timestamp of the most recent dispatch through any path
    # (auto tick, catch-up, or manual "Run now"). Distinct from
    # ``last_triggered_on`` (date-only), which is the daily-idempotency key
    # for ``tick()``/``catch_up()`` and is intentionally NOT stamped on manual
    # runs -- a manual "Run now" should not suppress the next scheduled fire.
    # Used by schedule health checks to know that something happened today
    # even if the cron path didn't run.
    last_dispatched_at: str = ""
    days_of_week: list[str] | None = None  # e.g. ["sun"] or ["mon","wed","fri"]; used when frequency="weekly"
    thread_id: int | None = None           # target topic (None = DM)
    frequency: str = "weekly"              # "daily", "weekly", "monthly", "manual", "once"
    day_of_month: int | None = None        # 1-31, used when frequency="monthly"
    run_at_date: str | None = None         # "YYYY-MM-DD" in timezone_name; used when frequency="once" (fires once then deletes)
    web_chat_id: str | None = None         # PWA chat target (e.g. "chat-a1b2c3d4"); when set, dispatches to web instead of Telegram
    web_project_id: str | None = None    # PWA project target; when set, each run creates a NEW chat in this project
    # Workspace the schedule belongs to (e.g. "acme" | "home" | "default"). Project IDs
    # regenerate per device on fresh init, so web_project_id goes stale across
    # devices; this field lets the resolver re-target the right General project
    # without guessing from the schedule_id prefix. Empty = fall back to that
    # prefix heuristic for entries created before this field existed.
    workspace: str = ""
    enabled: bool = True                 # False = paused, won't auto-fire but manual dispatch still works
    archive_policy: str = "manual"      # manual | auto
    title: str = ""
    scope: str = "user"
    editable: bool = True
    removable: bool = True


class ScheduleStore:
    """JSON-backed storage for user schedules plus packaged system schedules."""

    def __init__(self, runtime_root: Path, *, include_system: bool = False) -> None:
        self._path = runtime_root / "schedules.json"
        self._system_state_path = runtime_root / "system_schedules_state.json"
        self._include_system = include_system

    def list(self, *, chat_id: int | None = None) -> list[ScheduleEntry]:
        raw_items = self._runtime_items()
        items: list[ScheduleEntry] = []
        for item in raw_items:
            if item.get("scope") == "system":
                continue
            # Strip unknown keys that ScheduleEntry doesn't accept
            items.append(self._entry_from_item(item))
        if self._include_system:
            items.extend(self._system_entries())
        items.sort(key=lambda item: (item.daily_time_utc, item.created_at))
        if chat_id is not None:
            items = [item for item in items if item.chat_id == chat_id]
        return items

    def get(self, schedule_id: str) -> ScheduleEntry | None:
        for item in self.list():
            if item.schedule_id == schedule_id:
                return item
        return None

    def create(
        self,
        *,
        daily_time_utc: str,
        prompt: str,
        model: str,
        mode: BridgeMode,
        chat_id: int,
        timezone_name: str = DEFAULT_TIMEZONE,
        days_of_week: list[str] | None = None,
        thread_id: int | None = None,
        frequency: str = "weekly",
        day_of_month: int | None = None,
        run_at_date: str | None = None,
        web_chat_id: str | None = None,
        web_project_id: str | None = None,
        provider: str = "",
        archive_policy: str = "manual",
        workspace: str = "",
    ) -> ScheduleEntry:
        entry = ScheduleEntry(
            schedule_id=f"sched-{uuid.uuid4().hex[:8]}",
            daily_time_utc=daily_time_utc,
            prompt=prompt,
            model=model,
            provider=provider,
            mode=mode,
            chat_id=chat_id,
            created_at=_now_utc().isoformat().replace("+00:00", "Z"),
            timezone_name=timezone_name,
            days_of_week=days_of_week or None,
            thread_id=thread_id,
            frequency=frequency,
            day_of_month=day_of_month,
            run_at_date=run_at_date or None,
            web_chat_id=web_chat_id or None,
            web_project_id=web_project_id or None,
            archive_policy=normalize_archive_policy(archive_policy),
            workspace=workspace or "",
        )
        data = self._load()
        data.setdefault("schedules", []).append(self._serialize_entry(entry))
        self._save(data)
        return entry

    def replace(self, entry: ScheduleEntry) -> None:
        if entry.scope == "system":
            self._replace_system_state(entry)
            return
        data = self._load()
        items = data.setdefault("schedules", [])
        for index, item in enumerate(items):
            if item.get("schedule_id") == entry.schedule_id:
                items[index] = self._serialize_entry(entry)
                self._save(data)
                return
        items.append(self._serialize_entry(entry))
        self._save(data)

    def delete(self, schedule_id: str) -> bool:
        entry = self.get(schedule_id)
        if entry is not None and entry.scope == "system":
            return False
        data = self._load()
        before = len(data.setdefault("schedules", []))
        data["schedules"] = [item for item in data["schedules"] if item.get("schedule_id") != schedule_id]
        if len(data["schedules"]) == before:
            return False
        self._save(data)
        return True

    def _load(self) -> dict:
        if not self._path.exists():
            return {"schedules": []}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"schedules": []}

    def _save(self, payload: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _runtime_items(self) -> list[dict]:
        return [item for item in self._load().get("schedules", []) if isinstance(item, dict)]

    def _entry_from_item(self, item: dict) -> ScheduleEntry:
        # Strip unknown keys that ScheduleEntry doesn't accept.
        known = {f.name for f in ScheduleEntry.__dataclass_fields__.values()}
        filtered = {k: v for k, v in item.items() if k in known}
        entry = ScheduleEntry(**filtered)
        # Backward compat: infer frequency for entries created before this field existed.
        if "frequency" not in item:
            entry.frequency = "daily" if not entry.days_of_week else "weekly"
        try:
            entry.archive_policy = normalize_archive_policy(entry.archive_policy)
        except ValueError:
            logger.warning(
                "Schedule %s has unknown archive_policy '%s'; defaulting to manual",
                entry.schedule_id,
                entry.archive_policy,
            )
            entry.archive_policy = "manual"
        return entry

    def _system_entries(self) -> list[ScheduleEntry]:
        state = self._load_system_state()
        entries: list[ScheduleEntry] = []
        for item in self._load_system_definitions():
            item = {"chat_id": 0, "created_at": "1970-01-01T00:00:00Z", **item}
            entry = self._entry_from_item(item)
            overlay = state.get(entry.schedule_id, {})
            for key, value in overlay.items():
                if key in SYSTEM_STATE_FIELDS and hasattr(entry, key):
                    setattr(entry, key, value)
            entry.scope = "system"
            entry.editable = False
            entry.removable = False
            entries.append(entry)
        return entries

    def _load_system_definitions(self) -> list[dict]:
        try:
            raw = resources.files("ciao.stock").joinpath("schedules.json").read_text(encoding="utf-8")
            data = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError, ModuleNotFoundError):
            logger.exception("Failed to load stock system schedules")
            return []
        return [
            item for item in data.get("schedules", [])
            if isinstance(item, dict) and item.get("scope") == "system"
        ]

    def _load_system_state(self) -> dict[str, dict]:
        if not self._system_state_path.exists():
            return {}
        try:
            data = json.loads(self._system_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        raw = data.get("schedules", {})
        return raw if isinstance(raw, dict) else {}

    def _save_system_state(self, payload: dict[str, dict]) -> None:
        self._system_state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"schedules": payload}
        self._system_state_path.write_text(
            json.dumps(data, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _replace_system_state(self, entry: ScheduleEntry) -> None:
        state = self._load_system_state()
        current = state.setdefault(entry.schedule_id, {})
        for field in SYSTEM_STATE_FIELDS:
            current[field] = getattr(entry, field)
        self._save_system_state(state)

    def _serialize_entry(self, entry: ScheduleEntry) -> dict:
        payload = asdict(entry)
        # `mode` is a runtime-only binding; `model` is user-configurable
        # (empty string means "use current default at dispatch time").
        payload.pop("mode", None)
        return payload


class ScheduleManager:
    """Polls daily schedules and dispatches them as chat turns."""

    def __init__(
        self,
        store: ScheduleStore,
        resolve_target: Callable[[ScheduleEntry], tuple[str, str, BridgeMode, str]] | None = None,
        dispatch_to_web: Callable[
            [ScheduleEntry, str, BridgeMode, str], Awaitable[dict | None]
        ]
        | None = None,
        prepare_chat: Callable[
            [ScheduleEntry, str, str, BridgeMode, str], str | None
        ]
        | None = None,
    ) -> None:
        self._store = store
        self._resolve_target = resolve_target
        self._dispatch_to_web = dispatch_to_web
        self._prepare_chat = prepare_chat
        self._loop_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop(), name="schedule-loop")

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None

    def list(self, *, chat_id: int | None = None) -> list[ScheduleEntry]:
        return self._store.list(chat_id=chat_id)

    def create(
        self,
        *,
        daily_time_utc: str,
        prompt: str,
        model: str,
        mode: BridgeMode,
        chat_id: int,
        timezone_name: str = DEFAULT_TIMEZONE,
        days_of_week: list[str] | None = None,
        thread_id: int | None = None,
        frequency: str = "weekly",
        day_of_month: int | None = None,
        run_at_date: str | None = None,
        web_chat_id: str | None = None,
        web_project_id: str | None = None,
        provider: str = "",
        archive_policy: str = "manual",
        workspace: str = "",
    ) -> ScheduleEntry:
        return self._store.create(
            daily_time_utc=daily_time_utc,
            prompt=prompt,
            model=model,
            provider=provider,
            mode=mode,
            chat_id=chat_id,
            timezone_name=timezone_name,
            days_of_week=days_of_week,
            web_chat_id=web_chat_id,
            web_project_id=web_project_id,
            thread_id=thread_id,
            frequency=frequency,
            day_of_month=day_of_month,
            run_at_date=run_at_date,
            archive_policy=archive_policy,
            workspace=workspace,
        )

    def delete(self, schedule_id: str) -> bool:
        return self._store.delete(schedule_id)

    async def _dispatch_entry(
        self,
        entry: ScheduleEntry,
        model: str,
        mode: BridgeMode,
        provider: str,
        *,
        target_chat_id: str | None = None,
    ) -> None:
        """Dispatch a schedule entry through the web pipeline."""
        if self._dispatch_to_web is not None:
            asyncio.create_task(
                self._dispatch_to_web(
                    entry, model, mode, provider, target_chat_id=target_chat_id
                )
            )

    async def _dispatch_entry_and_wait(
        self,
        entry: ScheduleEntry,
        model: str,
        mode: BridgeMode,
        provider: str,
        *,
        target_chat_id: str | None = None,
    ) -> dict:
        """Dispatch a schedule entry and await the result (manual run)."""
        if self._dispatch_to_web is None:
            return {}
        result = await self._dispatch_to_web(
            entry, model, mode, provider, target_chat_id=target_chat_id
        )
        return result or {}

    async def dispatch_now(self, schedule_id: str) -> dict:
        """Trigger a schedule immediately through the chat pipeline.

        Returns the schedule_id and, when available, the chat_id of the
        created/target chat so the frontend can link to it.
        """
        entry = self._store.get(schedule_id)
        if entry is None:
            raise ValueError(f"Schedule '{schedule_id}' not found.")
        _, model, mode, provider = (
            self._resolve_target(entry)
            if self._resolve_target is not None
            else ("claude", entry.model, entry.mode, entry.provider)
        )
        # Prepare the chat synchronously so we can return its ID immediately.
        # Pass it through to dispatch so it doesn't create a second chat.
        chat_id: str | None = None
        if self._prepare_chat is not None:
            chat_id = self._prepare_chat(entry, entry.prompt, model, mode, provider)
        # Always dispatch in the background for manual "Run now" so the API can
        # return the prepared chat_id immediately and the PWA can link to the
        # live run while it is still streaming.
        await self._dispatch_entry(
            entry, model, mode, provider, target_chat_id=chat_id
        )
        dispatch_result: dict = {}
        # One-off schedules are consumed by any fire path (auto, catch-up,
        # or "Run now"). Removing the entry here keeps the semantics simple:
        # once it has run, it's gone. Stamp the dispatch timestamp FIRST so
        # the replace-before-delete write actually lands for "once" entries.
        entry.last_dispatched_at = datetime.now(UTC).isoformat(timespec="seconds")
        if entry.frequency == "once":
            self._store.replace(entry)
            self._store.delete(entry.schedule_id)
        result: dict = {
            "schedule_id": schedule_id,
            "archive_policy": entry.archive_policy,
        }
        if dispatch_result:
            result.update(dispatch_result)
        if chat_id and "chat_id" not in result:
            result["chat_id"] = chat_id
        # For non-"once" entries, persist the stamp now. ("once" already
        # replaced above; replace-then-delete to leave a clean store.)
        if entry.frequency != "once":
            self._store.replace(entry)
        return result

    async def tick(self, now: datetime | None = None) -> None:
        current = now or _now_utc()
        for entry in self._store.list():
            # Manual and disabled schedules never auto-fire.
            if entry.frequency == "manual" or not entry.enabled:
                continue
            localized = current.astimezone(ZoneInfo(entry.timezone_name))
            current_time = localized.strftime("%H:%M")
            current_day = localized.date().isoformat()
            if entry.daily_time_utc != current_time:
                continue
            if entry.last_triggered_on == current_day:
                continue
            # Check frequency filter
            if entry.frequency == "once":
                # Fires only on its exact target date.
                if not entry.run_at_date or entry.run_at_date != current_day:
                    continue
            elif entry.frequency == "monthly":
                if localized.day != entry.day_of_month:
                    continue
            elif entry.frequency == "weekly":
                if entry.days_of_week:
                    current_weekday = WEEKDAY_NAMES[localized.weekday()]
                    if current_weekday not in entry.days_of_week:
                        continue
            # frequency == "daily" → no filter, always fires
            _, model, mode, provider = (
                self._resolve_target(entry)
                if self._resolve_target is not None
                else ("claude", entry.model, entry.mode, entry.provider)
            )
            await self._dispatch_entry(entry, model, mode, provider)

            if entry.frequency == "once":
                # One-shot consumed; remove from store rather than mark as
                # triggered. The entry no longer exists for any future tick.
                # Set a sentinel first so that if the delete fails (crash,
                # git-sync race, disk error), catch_up won't refire it.
                entry.last_triggered_on = "done"
                entry.last_dispatched_at = current_day + "T" + localized.strftime("%H:%M:%S")
                self._store.replace(entry)
                self._store.delete(entry.schedule_id)
            else:
                entry.last_triggered_on = current_day
                entry.last_dispatched_at = current_day + "T" + localized.strftime("%H:%M:%S")
                self._store.replace(entry)

    async def catch_up(self, now: datetime | None = None) -> list[str]:
        """Fire schedules whose target time has already passed today but which
        didn't trigger (e.g. because the server was down or in a crash loop
        at the scheduled minute).

        Scope: today only. Missed runs from *previous* days are NOT replayed,
        because a stale "morning briefing" from yesterday is worse than none.

        Called once on startup. Respects the same day-of-week / day-of-month
        filters that `tick` uses.

        Returns the list of schedule_ids that were fired.
        """
        current = now or _now_utc()
        fired: list[str] = []
        for entry in self._store.list():
            # Manual and disabled schedules never auto-fire.
            if entry.frequency == "manual" or not entry.enabled:
                continue
            tz = ZoneInfo(entry.timezone_name)
            localized = current.astimezone(tz)
            current_day = localized.date().isoformat()
            # Parse "HH:MM"
            try:
                hh, mm = entry.daily_time_utc.split(":")
                target_hour, target_minute = int(hh), int(mm)
            except (ValueError, AttributeError):
                continue

            if entry.frequency == "once":
                # One-shot: catch up *across days* (not just today). If the
                # server was down past run_at_date, fire now and consume
                # the entry. If run_at_date is still in the future, skip.
                # Also skip if the sentinel is set (already fired but deletion
                # failed; prevents infinite refires on restart loops).
                if entry.last_triggered_on:
                    continue
                if not entry.run_at_date:
                    continue
                try:
                    target_date = datetime.fromisoformat(entry.run_at_date).date()
                except ValueError:
                    continue
                target_dt = datetime(
                    target_date.year, target_date.month, target_date.day,
                    target_hour, target_minute, tzinfo=tz,
                )
                if localized < target_dt:
                    continue  # not due yet; regular tick will handle it
                _, model, mode, provider = (
                    self._resolve_target(entry)
                    if self._resolve_target is not None
                    else ("claude", entry.model, entry.mode, entry.provider)
                )
                logger.info(
                    "Schedule %s: catch-up fire (once @ %s %s, now %s)",
                    entry.schedule_id, entry.run_at_date, entry.daily_time_utc,
                    localized.isoformat(),
                )
                await self._dispatch_entry(entry, model, mode, provider)
                self._store.delete(entry.schedule_id)
                fired.append(entry.schedule_id)
                continue

            if entry.last_triggered_on == current_day:
                continue
            # Frequency filters (recurring)
            if entry.frequency == "monthly":
                if localized.day != entry.day_of_month:
                    continue
            elif entry.frequency == "weekly":
                if entry.days_of_week:
                    if WEEKDAY_NAMES[localized.weekday()] not in entry.days_of_week:
                        continue
            scheduled_today = localized.replace(
                hour=target_hour, minute=target_minute,
                second=0, microsecond=0,
            )
            if localized < scheduled_today:
                continue  # Not due yet; regular tick will handle it
            _, model, mode, provider = (
                self._resolve_target(entry)
                if self._resolve_target is not None
                else ("claude", entry.model, entry.mode, entry.provider)
            )
            logger.info(
                "Schedule %s: catch-up fire (target %s, now %s %s)",
                entry.schedule_id, entry.daily_time_utc,
                localized.strftime("%H:%M"), entry.timezone_name,
            )
            await self._dispatch_entry(entry, model, mode, provider)
            entry.last_triggered_on = current_day
            entry.last_dispatched_at = current_day + "T" + localized.strftime("%H:%M:%S")
            self._store.replace(entry)
            fired.append(entry.schedule_id)
        return fired

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Schedule loop tick failed")
            await asyncio.sleep(20)
