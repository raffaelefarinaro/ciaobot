"""Simple daily schedule support for chat-dispatched automations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Callable, Coroutine, Protocol
from zoneinfo import ZoneInfo

from ciao.jsonio import read_json_dict
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
    "last_run_chat_id",
    "workspace",
}

# Separator between a packaged system routine's id and the workspace it was
# fanned out for: `system-memory-curation@work`. Chosen because no generated or
# packaged schedule_id contains it, so `system_base_id` is unambiguous.
SYSTEM_ID_SEPARATOR = "@"


def system_base_id(schedule_id: str) -> str:
    """Return the packaged definition id behind a possibly fanned-out id.

    Call this anywhere a literal `system-*` id is compared against a live
    schedule — the fan-out makes the stored id workspace-qualified, and an exact
    match against the base id silently stops finding it.
    """
    return (schedule_id or "").split(SYSTEM_ID_SEPARATOR, 1)[0]


def system_schedule_id(base_id: str, workspace: str) -> str:
    return f"{base_id}{SYSTEM_ID_SEPARATOR}{workspace}"


def _stagger_time(daily_time_utc: str, offset: int, *, step_minutes: int = 7) -> str:
    """Offset a fanned-out routine's fire time by whole minutes.

    All rows inherit one packaged ``daily_time_utc``, so without this every
    workspace's curation run would dispatch in the same minute and contend for
    the same provider capacity. Malformed input is returned untouched;
    ``compute_next_run`` already treats that as "never fires".
    """
    if offset <= 0:
        return daily_time_utc
    try:
        hours, minutes = (int(part) for part in daily_time_utc.split(":", 1))
    except (ValueError, AttributeError):
        return daily_time_utc
    total = (hours * 60 + minutes + offset * step_minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def normalize_archive_policy(value: str | None) -> str:
    normalized = (value or "manual").strip() or "manual"
    if normalized not in ARCHIVE_POLICIES:
        raise ValueError(f"unknown archive_policy '{normalized}'")
    return normalized


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
    last_run_chat_id: str = ""
    days_of_week: list[str] | None = None  # e.g. ["sun"] or ["mon","wed","fri"]; used when frequency="weekly"
    thread_id: int | None = None           # target topic (None = DM)
    frequency: str = "weekly"              # "daily", "weekly", "monthly", "manual", "once"
    day_of_month: int | None = None        # 1-31, used when frequency="monthly"
    run_at_date: str | None = None         # "YYYY-MM-DD" in timezone_name; used when frequency="once" (fires once then deletes)
    web_chat_id: str | None = None         # PWA chat target (e.g. "chat-a1b2c3d4"); when set, dispatches to web instead of Telegram
    web_project_id: str | None = None    # PWA project target; when set, each run creates a NEW chat in this project
    # The target project's name, recorded alongside its id. Project ids are
    # per-instance and regenerate on a fresh init, so the id alone silently
    # decays into "no target" and the run lands in General with the user's
    # choice discarded. The name survives that and lets the resolver re-home
    # to the same project and repair the id. Empty for entries created before
    # this field existed; those still fall back to General.
    web_project_name: str = ""
    # Workspace the schedule belongs to (e.g. "acme" | "home" | "default"). Project IDs
    # regenerate per device on fresh init, so web_project_id goes stale across
    # devices; this field lets the resolver re-target the right General project
    # without guessing from the schedule_id prefix. Empty = fall back to that
    # prefix heuristic for entries created before this field existed.
    workspace: str = ""
    enabled: bool = True                 # False = paused, won't auto-fire but manual dispatch still works
    archive_policy: str = "manual"      # manual | auto
    title: str = ""
    # Plain-language summary of what this routine does for the user, shown in
    # the Automations UI above the raw prompt. Optional for user-created
    # routines; the packaged system routines ship one so their (often
    # command-style) prompts aren't the only thing the user sees.
    description: str = ""
    scope: str = "user"
    editable: bool = True
    removable: bool = True


class ScheduleStore:
    """JSON-backed storage for user schedules plus packaged system schedules."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        include_system: bool = False,
        workspace_names: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self._path = runtime_root / "schedules.json"
        self._system_state_path = runtime_root / "system_schedules_state.json"
        self._include_system = include_system
        # Needed to fan a `per_workspace` system definition out into one entry
        # per registered workspace. Optional so a caller that only reads user
        # schedules (and every existing test) keeps working: without it, a
        # per-workspace definition degrades to the single legacy entry.
        self._workspace_names = workspace_names
        self._lock = threading.RLock()

    def list_entries(self, *, chat_id: int | None = None) -> list[ScheduleEntry]:
        with self._lock:
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
        for item in self.list_entries():
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
        web_project_name: str = "",
        provider: str = "",
        archive_policy: str = "manual",
        workspace: str = "",
        title: str = "",
        description: str = "",
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
            web_project_name=web_project_name or "",
            archive_policy=normalize_archive_policy(archive_policy),
            workspace=workspace or "",
            title=title or "",
            description=description or "",
        )
        with self._lock:
            data = self._load()
            data.setdefault("schedules", []).append(self._serialize_entry(entry))
            self._save(data)
        return entry

    def replace(self, entry: ScheduleEntry) -> None:
        with self._lock:
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
        with self._lock:
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
            data = read_json_dict(self._path)
            return data
        except json.JSONDecodeError:
            return {"schedules": []}

    def _save(self, payload: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

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

    def _fanout_workspaces(self) -> list[str]:
        """Registered workspaces a `per_workspace` definition expands into.

        Empty when no resolver was supplied, which keeps the definition as one
        legacy entry rather than guessing at a workspace.
        """
        if self._workspace_names is None:
            return []
        try:
            names = [str(name).strip() for name in self._workspace_names()]
        except Exception:  # noqa: BLE001 — a broken registry must not hide routines
            logger.exception("Failed to resolve workspaces for system schedules")
            return []
        return [name for name in names if name]

    def _system_entries(self) -> list[ScheduleEntry]:
        """Materialize the packaged system routines.

        A definition marked ``per_workspace`` becomes one entry per registered
        workspace, with a ``<base-id>@<workspace>`` id so each row carries its
        own overlay (enable state, last run) — the inputs and write targets of
        those routines are partitioned by workspace, so one shared run can only
        ever curate one vault. Unmarked definitions stay single: their subject is
        a shared artifact (one ``INDEX.md``, one global skill catalog) and
        running them N times would duplicate the work.

        System routines are derived here on every read rather than persisted:
        ``list_entries`` drops runtime rows with ``scope == "system"``, so the
        set cannot be extended by writing to ``schedules.json``. Deriving also
        means a newly added workspace gets its row with no migration.
        """
        state = self._load_system_state()
        entries: list[ScheduleEntry] = []
        for definition in self._load_system_definitions():
            per_workspace = bool(definition.get("per_workspace"))
            item = {"chat_id": 0, "created_at": "1970-01-01T00:00:00Z", **definition}
            targets: list[str | None] = []
            if per_workspace:
                targets = list(self._fanout_workspaces())
            for offset, workspace in enumerate(targets or [None]):
                entry = self._entry_from_item(item)
                allowed = SYSTEM_STATE_FIELDS
                legacy_id = entry.schedule_id
                if workspace is not None:
                    entry.schedule_id = system_schedule_id(entry.schedule_id, workspace)
                    entry.workspace = workspace
                    # Fanned-out rows share one packaged time; stagger them so N
                    # workspaces don't all dispatch in the same minute.
                    entry.daily_time_utc = _stagger_time(entry.daily_time_utc, offset)
                    if entry.title:
                        entry.title = f"{entry.title} ({workspace})"
                    # The workspace is part of this row's identity, so a stored
                    # overlay must never move it.
                    allowed = SYSTEM_STATE_FIELDS - {"workspace"}
                overlay = self._system_overlay(state, entry.schedule_id, legacy_id)
                for key, value in overlay.items():
                    if key in allowed and hasattr(entry, key):
                        setattr(entry, key, value)
                entry.scope = "system"
                entry.editable = False
                entry.removable = False
                entries.append(entry)
        return entries

    def _system_overlay(
        self, state: dict[str, dict], schedule_id: str, legacy_id: str
    ) -> dict:
        """Overlay for one system row, migrating the pre-fan-out key forward.

        Before the per-workspace fan-out, a routine's overlay was keyed by the
        bare definition id (``system-memory-curation``); it is now keyed
        ``<base>@<workspace>``. The key changed with no migration, so on upgrade
        the new key had no stored state and a routine the user had DISABLED came
        back enabled — the one direction of this bug a user cannot notice until
        the run they switched off happens again.

        Falling back only when the new key is absent keeps every already-migrated
        row authoritative: once anything writes ``<base>@<workspace>``, that row
        owns its state and the legacy key stops influencing it. The read stays
        read-only on purpose — leaving the legacy key in place means a workspace
        registered after the upgrade inherits it too, and no ``list_entries``
        call has to write to disk.
        """
        if schedule_id in state:
            return state[schedule_id]
        if legacy_id == schedule_id:
            return {}
        return state.get(legacy_id, {})

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

    def _normalized_overlay(self, overlay: dict) -> dict:
        """Drop a persisted workspace that no longer names a registered one.

        ``workspace`` is in :data:`SYSTEM_STATE_FIELDS` and
        ``_replace_system_state`` writes every field in that set on any save, so
        the packaged value gets copied into the overlay the first time anything
        about a routine changes. A sentinel like ``"default"`` — or a workspace
        the user has since renamed — then shadows the definition forever, which
        is what pinned memory curation to one vault. Dropping the unresolvable
        value lets the definition (and the fan-out) win instead.
        """
        workspace = str(overlay.get("workspace", "") or "").strip()
        if not workspace:
            return overlay
        if self._workspace_names is None:
            return overlay
        if workspace in self._fanout_workspaces():
            return overlay
        logger.info(
            "Dropping unresolvable workspace %r from system schedule state",
            workspace,
        )
        return {key: value for key, value in overlay.items() if key != "workspace"}

    def _load_system_state(self) -> dict[str, dict]:
        if not self._system_state_path.exists():
            return {}
        try:
            data = json.loads(self._system_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        raw = data.get("schedules", {})
        if not isinstance(raw, dict):
            return {}
        return {
            key: self._normalized_overlay(value)
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    def _save_system_state(self, payload: dict[str, dict]) -> None:
        self._system_state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"schedules": payload}
        tmp = self._system_state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._system_state_path)

    def _reject_cross_workspace_move(self, entry: ScheduleEntry) -> None:
        """Refuse a ``workspace`` change on a fanned-out system row.

        ``workspace`` is in :data:`SYSTEM_STATE_FIELDS`, so it lands in the
        overlay on any save — but ``_system_entries`` deliberately drops it again
        for a fanned-out row, whose workspace comes from the
        ``<base>@<workspace>`` id suffix. A stored value could therefore only be
        written and then ignored: the update APIs accepted ``workspace`` here and
        returned a payload naming the requested one, while the very next
        ``list_entries`` read showed the old one — a move reported as done that
        never happened, with no way for the caller to tell. Say the row cannot
        move instead. An un-fanned-out routine is untouched: there the field is a
        real setting (one shared subject, one row the user may point at any
        workspace) rather than identity.
        """
        base_id, separator, row_workspace = entry.schedule_id.partition(
            SYSTEM_ID_SEPARATOR
        )
        if not separator:
            return
        requested = (entry.workspace or "").strip()
        # Casing alone is not a move: the workspace registry and the callers
        # disagree about it (the control plane lower-cases its target), and
        # refusing a save that changes nothing would break the enable toggle.
        if requested.casefold() == row_workspace.strip().casefold():
            return
        hint = (
            f" Change '{system_schedule_id(base_id, requested)}' instead."
            if requested
            else ""
        )
        raise ValueError(
            f"System routine '{base_id}' runs once per workspace, so the "
            f"workspace is part of its id: '{entry.schedule_id}' is the "
            f"'{row_workspace}' run and cannot be moved to "
            f"'{requested or '(none)'}'.{hint}"
        )

    def _replace_system_state(self, entry: ScheduleEntry) -> None:
        self._reject_cross_workspace_move(entry)
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


class _DispatchToWeb(Protocol):
    """Callback that dispatches a schedule entry through the web pipeline.

    Declared as a Protocol (rather than ``Callable``) so the ``target_chat_id``
    keyword argument is expressible and the coroutine return type is precise
    enough for ``asyncio.create_task``.
    """

    def __call__(
        self,
        entry: ScheduleEntry,
        model: str,
        mode: BridgeMode,
        provider: str,
        *,
        target_chat_id: str | None = None,
    ) -> Coroutine[Any, Any, dict | None]: ...


class ScheduleManager:
    """Polls daily schedules and dispatches them as chat turns."""

    def __init__(
        self,
        store: ScheduleStore,
        resolve_target: Callable[[ScheduleEntry], tuple[str, str, BridgeMode, str]] | None = None,
        dispatch_to_web: _DispatchToWeb | None = None,
        prepare_chat: Callable[
            [ScheduleEntry, str, str, BridgeMode, str], str | None
        ]
        | None = None,
        is_node_active: Callable[[], bool] | None = None,
    ) -> None:
        self._store = store
        self._resolve_target = resolve_target
        self._dispatch_to_web = dispatch_to_web
        self._prepare_chat = prepare_chat
        self._is_node_active = is_node_active
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

    def list_entries(self, *, chat_id: int | None = None) -> list[ScheduleEntry]:
        return self._store.list_entries(chat_id=chat_id)

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
        web_project_name: str = "",
        provider: str = "",
        archive_policy: str = "manual",
        workspace: str = "",
        title: str = "",
        description: str = "",
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
            web_project_name=web_project_name,
            thread_id=thread_id,
            frequency=frequency,
            day_of_month=day_of_month,
            run_at_date=run_at_date,
            archive_policy=archive_policy,
            workspace=workspace,
            title=title,
            description=description,
        )

    def backfill_project_names(self, resolve_name) -> int:
        """Record the target project's name on entries that only have its id.

        Project ids are per-instance, so an entry carrying only an id is one
        fresh init away from silently running in General. Entries written
        before the name was stored — or synced in from another device — are
        still repairable while their id resolves *here*, which is the only
        window in which the intended project is knowable rather than guessed.

        ``resolve_name`` maps a project id to its name, or None when the id
        does not resolve; those are left alone, since inventing a name would
        defeat the point. Returns how many entries were stamped.
        """
        stamped = 0
        for entry in self.list_entries():
            if entry.web_project_name or not entry.web_project_id:
                continue
            name = resolve_name(entry.web_project_id)
            if not name:
                continue
            entry.web_project_name = name
            self.replace(entry)
            stamped += 1
        if stamped:
            logger.info("Recorded the target project name on %d schedule(s)", stamped)
        return stamped

    def delete(self, schedule_id: str) -> bool:
        return self._store.delete(schedule_id)

    def replace(self, entry: ScheduleEntry) -> None:
        """Persist a validated schedule update through the public manager API."""
        self._store.replace(entry)

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
        if chat_id:
            entry.last_run_chat_id = chat_id
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
        if self._is_node_active is not None and not self._is_node_active():
            return
        current = now or _now_utc()
        for entry in self._store.list_entries():
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
            # Prepare the chat synchronously (like dispatch_now) so
            # last_run_chat_id is durable in the same write as
            # last_dispatched_at, instead of depending on the fire-and-forget
            # dispatch task surviving long enough to write it back later.
            chat_id: str | None = None
            if self._prepare_chat is not None:
                chat_id = self._prepare_chat(entry, entry.prompt, model, mode, provider)
            await self._dispatch_entry(
                entry, model, mode, provider, target_chat_id=chat_id
            )
            if chat_id:
                entry.last_run_chat_id = chat_id

            if entry.frequency == "once":
                # One-shot consumed; remove from store rather than mark as
                # triggered. The entry no longer exists for any future tick.
                # Set a sentinel first so that if the delete fails (crash,
                # git-sync race, disk error), catch_up won't refire it.
                entry.last_triggered_on = "done"
                entry.last_dispatched_at = localized.isoformat(timespec="seconds")
                self._store.replace(entry)
                self._store.delete(entry.schedule_id)
            else:
                entry.last_triggered_on = current_day
                entry.last_dispatched_at = localized.isoformat(timespec="seconds")
                self._store.replace(entry)

    async def catch_up(self, now: datetime | None = None) -> list[str]:
        """Fire each schedule once when its latest expected run was missed.

        Called once on startup so schedules recover after the server was down
        across their target time. Only the most recent missed occurrence is
        dispatched; skipped intervals are not replayed as a backlog, and the
        original prompt is dispatched unchanged (without backdating its
        context to the missed slot).

        Returns the list of schedule_ids that were fired.
        """
        current = now or _now_utc()
        fired: list[str] = []
        for entry in self._store.list_entries():
            # Manual and disabled schedules never auto-fire.
            if entry.frequency == "manual" or not entry.enabled:
                continue
            tz = ZoneInfo(entry.timezone_name)
            localized = current.astimezone(tz)

            if entry.frequency == "once":
                # One-shot schedules do not catch up across days. A `once`
                # reminder whose target time was missed while the server was
                # down is stale by the time it would fire — dispatching it now
                # un-backdated would tell the agent nothing is late. The
                # operator-action strip surfaces these as a single collapsed
                # tile (see `operator_actions.detect_actions`), where the
                # operator decides whether the reminder is still relevant.
                # The regular `tick()` still fires a `once` schedule on its
                # exact target date; only the catch-up path is suppressed.
                continue

            last_expected = compute_last_expected_run(entry, now=current)
            if last_expected is None:
                continue
            expected_day = last_expected.date().isoformat()
            if entry.last_triggered_on and entry.last_triggered_on >= expected_day:
                continue
            if was_dispatched_since(entry, last_expected):
                continue
            _, model, mode, provider = (
                self._resolve_target(entry)
                if self._resolve_target is not None
                else ("claude", entry.model, entry.mode, entry.provider)
            )
            logger.info(
                "Schedule %s: catch-up fire (latest missed %s, now %s)",
                entry.schedule_id,
                last_expected.isoformat(),
                localized.isoformat(),
            )
            chat_id = None
            if self._prepare_chat is not None:
                chat_id = self._prepare_chat(entry, entry.prompt, model, mode, provider)
            await self._dispatch_entry(
                entry, model, mode, provider, target_chat_id=chat_id
            )
            if chat_id:
                entry.last_run_chat_id = chat_id
            # Keep the idempotency date tied to the occurrence being caught
            # up. If startup happens before today's target, today's regular
            # tick must still be allowed to run later.
            entry.last_triggered_on = expected_day
            entry.last_dispatched_at = localized.isoformat(timespec="seconds")
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
