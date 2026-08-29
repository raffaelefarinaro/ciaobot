"""Schedule support for chat-dispatched automations.

One primitive covers every cadence. ``frequency`` picks between a wall-clock
slot ("daily", "weekly", "monthly", "once"), no automatic fire at all
("manual"), and a minute-level interval ("interval") measured from the last
dispatch.

``interval`` absorbed what used to be a second primitive (in-chat loops). An
interval entry bound to an existing chat via ``web_chat_id`` keeps that
primitive's properties: it inherits the chat's model/mode instead of imposing
its own, skips rather than queues when the chat still has a turn in flight,
re-homes or stops itself when the target chat is gone, and never replays
intervals missed while the server was down. An interval entry bound to a
``web_project_id`` instead opens a fresh chat per run, which is the
combination neither primitive offered before the merge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
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

# Cadence measured from the last dispatch rather than from a wall-clock slot.
INTERVAL_FREQUENCY = "interval"
FREQUENCIES = {"daily", "weekly", "monthly", "manual", "once", INTERVAL_FREQUENCY}
# Floor on interval cadence. Deliberately whole minutes: the ticker polls every
# 20 seconds, so anything finer would be cadence the scheduler cannot honour.
MIN_INTERVAL_MINUTES = 1
DEFAULT_INTERVAL_MINUTES = 10

# Exactly what ``compute_next_run`` can parse out of ``daily_time_utc``.
_VALID_DAILY_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def wall_clock_time_error(entry: "ScheduleEntry") -> str:
    """Why this entry's time is unusable, or "" when it is fine.

    A wall-clock cadence needs a time ``compute_next_run`` can parse. Without
    one it returns ``None`` and ``tick()`` never matches, so the entry sits
    there reading as enabled and silently never fires — which is exactly where
    an interval entry lands when it is edited to a wall-clock cadence, since
    interval and manual entries carry no ``daily_time_utc`` at all.

    Lives here rather than in a route because both write paths need it: the
    REST ``PATCH /api/schedules/{id}`` and the ``schedule`` MCP tool. Guarding
    only one of them leaves the other door open.
    """
    return wall_clock_time_value_error(
        getattr(entry, "frequency", ""), getattr(entry, "daily_time_utc", "") or ""
    )


def wall_clock_time_value_error(frequency: str, daily_time_utc: str) -> str:
    """:func:`wall_clock_time_error` on the raw values.

    The REST create route validates the request body before any entry exists,
    so it has the two fields and not the object. Same rule, one implementation.
    """
    if frequency in {"manual", INTERVAL_FREQUENCY}:
        return ""
    if _VALID_DAILY_TIME.match(daily_time_utc or ""):
        return ""
    return (
        f"frequency '{frequency}' needs a HH:MM time; got {daily_time_utc!r}"
    )


SYSTEM_STATE_FIELDS = {
    "enabled",
    "last_triggered_on",
    "last_dispatched_at",
    "last_run_chat_id",
    "last_status",
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


def supports_auto_archive(entry: Any) -> bool:
    """False for an interval entry bound to a fixed chat.

    Typed loosely because the dispatcher (``project_chats``) holds the entry as
    an opaque object and reads it the same ``getattr`` way this does.

    The point of that binding is one conversation carried across runs, and
    ``_rehome_interval_chat`` forks a replacement whenever it finds the target
    archived — so archiving after a clean run makes the next run fork, run, and
    archive again, forever. A project-bound interval entry opens a fresh chat
    per run and is exactly what auto-archive is for, so only the fixed-chat
    binding is excluded.
    """
    return not (
        getattr(entry, "frequency", "") == INTERVAL_FREQUENCY
        and getattr(entry, "web_chat_id", "")
        and not getattr(entry, "web_project_id", "")
    )


def normalize_auto_archive(entry: "ScheduleEntry") -> bool:
    """Force ``manual`` where auto-archive cannot work. Returns if it changed.

    The dispatcher already refuses to archive these, so storing ``auto`` only
    produced a setting the UI displayed and honoured nowhere. Normalising at the
    store means every write path — REST, MCP, both legacy loop routes, and a
    retarget that turns a project entry into a fixed-chat one — lands on the
    same answer, rather than each remembering to check.
    """
    if supports_auto_archive(entry) or getattr(entry, "archive_policy", "") != "auto":
        return False
    entry.archive_policy = "manual"
    return True


def stamp_fallback_project(entry: "ScheduleEntry", pcm: Any) -> bool:
    """Keep ``fallback_project_id`` in step with ``web_chat_id``.

    Every path that binds or rebinds an automation's chat calls this — REST
    create/update, the ``schedule`` MCP create/update, and both legacy loop
    routes. The field records where a *fixed-chat* entry re-homes once its
    target chat is deleted, so it can only be captured while that chat still
    exists; ``resolve_automation_project`` cannot derive it later, by which
    point the chat is gone.

    This exists because stamping it per call site did not hold: each creator
    was fixed separately and the next one shipped without it, and no update
    path refreshed it at all — retargeting an entry from a chat in project A to
    one in project B left the fallback on A, so deleting the new chat resumed
    the run in the old project.

    Takes ``pcm`` rather than living on it: the only thing needed is
    ``get_chat``, and a free function keeps the duck-typed managers in the
    tests honest instead of forcing every stub to grow a method. A manager
    without ``get_chat`` is read as "cannot see the chat" rather than raised
    through: this runs inside every schedule write, and turning a stub's
    missing method into a 500 on create would be a worse failure than leaving
    one entry's fallback unstamped.

    A project-bound entry clears the field — ``web_project_id`` already names
    where its runs go, and a stale fallback under it would only mislead.
    Returns whether anything changed, so callers can skip a redundant write.
    """
    web_project_id = getattr(entry, "web_project_id", "") or ""
    web_chat_id = getattr(entry, "web_chat_id", "") or ""
    if web_project_id or not web_chat_id:
        resolved = ""
    else:
        get_chat = getattr(pcm, "get_chat", None)
        chat = get_chat(web_chat_id) if get_chat is not None else None
        # A chat we cannot see right now (already gone, or not local) tells us
        # nothing new — keep whatever was captured while it existed rather than
        # blanking the entry's only re-home target.
        if chat is None:
            return False
        resolved = getattr(chat, "project_id", "") or ""
    if (getattr(entry, "fallback_project_id", "") or "") == resolved:
        return False
    entry.fallback_project_id = resolved
    return True


def publish_automations_changed(pcm) -> None:
    """Nudge every open tab to refetch schedules.

    Schedules are read over REST when a chat or the Automations page mounts, so
    without this an entry created in another tab (or by the model mid-turn)
    stays invisible until a reload. ``loops_changed`` is emitted alongside the
    new event name because a PWA build cached before loops were folded into
    schedules only listens for that one, and its ``/api/loops`` refetch still
    resolves through the compatibility route.

    Fire-and-forget: the events hub has no replay buffer, a missed frame heals
    on the next mount, and a fan-out failure must never fail the operation that
    triggered it.
    """
    events = getattr(pcm, "events", None)
    if events is None:
        return
    for event in ({"type": "schedules_changed"}, {"type": "loops_changed"}):
        try:
            events.publish(event)
        except Exception:  # noqa: BLE001 — never fail an operation on fan-out
            logger.exception("%s publish failed", event["type"])


def migrate_loops(runtime_root: Path) -> int:
    """Fold a legacy ``.runtime/loops.json`` into ``schedules.json``.

    Loops became interval schedules; this converts each stored loop once, on
    startup, and renames the old file aside so a later boot does not re-import
    it. The ``loop-…`` id is kept as the ``schedule_id`` so existing deep links
    (``/schedules/loop-a1b2c3d4``) and any id the model recorded in a chat keep
    resolving to the same automation.

    ``autostart`` becomes ``enabled``. Loops split "runs on boot" from "running
    right now" and only persisted the former, so it is the only durable state
    there was to carry across; a loop the user had started by hand in this
    session resumes as the merged primitive resumes everything — on the next
    tick, provided it was set to survive a restart.

    Returns how many entries were imported.
    """
    source = runtime_root / "loops.json"
    if not source.exists():
        return 0
    try:
        raw = read_json_dict(source)
    except (json.JSONDecodeError, OSError):
        logger.exception("Could not read %s; leaving it in place", source)
        return 0
    loops = [item for item in raw.get("loops", []) if isinstance(item, dict)]

    store = ScheduleStore(runtime_root)
    existing = {entry.schedule_id for entry in store.list_entries()}
    imported = 0
    for item in loops:
        loop_id = str(item.get("loop_id") or "").strip()
        if not loop_id or loop_id in existing:
            continue
        chat_id = str(item.get("web_chat_id") or "").strip()
        if not chat_id:
            # A loop with no target chat could never fire; there is nothing to
            # carry over and inventing a target would run the prompt somewhere
            # the user never chose.
            logger.warning("Skipping loop %s: it names no target chat", loop_id)
            continue
        entry = ScheduleEntry(
            schedule_id=loop_id,
            daily_time_utc="",
            prompt=str(item.get("prompt") or ""),
            chat_id=0,
            created_at=str(item.get("created_at") or _now_utc().isoformat()),
            # Empty model/mode is what makes an interval run inherit the target
            # chat's own settings, which is how loops always behaved.
            model="",
            frequency=INTERVAL_FREQUENCY,
            interval_minutes=max(
                MIN_INTERVAL_MINUTES,
                int(item.get("interval_minutes") or DEFAULT_INTERVAL_MINUTES),
            ),
            web_chat_id=chat_id,
            # Legacy loops always reused their named chat.  Do not carry over
            # the old project hint: on interval schedules that means a fresh
            # chat per run and takes precedence over web_chat_id.
            web_project_id=None,
            # The loop's project was its re-home target when the fixed chat
            # went away (legacy `_resolve_loop_project`). Dropping it entirely
            # sent those runs to the workspace's General instead, so keep it as
            # the fallback it always was.
            fallback_project_id=str(item.get("web_project_id") or ""),
            workspace=str(item.get("workspace") or ""),
            title=str(item.get("title") or ""),
            last_dispatched_at=str(item.get("last_run_at") or ""),
            last_status=str(item.get("last_status") or ""),
            enabled=bool(item.get("autostart")),
            # Deliberately not carried from the loop. `replace` routes a
            # ``scope == "system"`` entry to ``_replace_system_state``, which
            # writes only the overlay fields into system_schedules_state.json
            # and never adds a row to schedules.json — so a loop that named
            # itself system-scoped was counted as imported, logged as imported,
            # and then invisible to ``list_entries`` forever, with loops.json
            # already renamed aside. An imported loop is a user schedule.
            scope="user",
        )
        store.replace(entry)
        imported += 1

    try:
        source.replace(source.with_name("loops.json.migrated"))
    except OSError:
        logger.exception(
            "Imported %d loop(s) but could not rename %s; it will be skipped "
            "on the next boot because its ids now exist as schedules",
            imported, source,
        )
    if imported:
        logger.info("Imported %d loop(s) as interval schedules", imported)
    return imported


def normalize_archive_policy(value: str | None) -> str:
    normalized = (value or "manual").strip() or "manual"
    if normalized not in ARCHIVE_POLICIES:
        raise ValueError(f"unknown archive_policy '{normalized}'")
    return normalized


def is_interval(entry: "ScheduleEntry") -> bool:
    """True when this entry's cadence is an interval, not a wall-clock slot."""
    return getattr(entry, "frequency", "") == INTERVAL_FREQUENCY


def interval_delta(entry: "ScheduleEntry") -> timedelta:
    """The gap between two interval runs, floored at :data:`MIN_INTERVAL_MINUTES`."""
    try:
        minutes = int(getattr(entry, "interval_minutes", 0) or 0)
    except (TypeError, ValueError):
        minutes = 0
    return timedelta(minutes=max(MIN_INTERVAL_MINUTES, minutes))


def normalize_interval_minutes(value: object) -> int:
    """Coerce a caller-supplied cadence to whole minutes at or above the floor.

    Raises ValueError on anything not parseable as an integer, so a typo lands
    as a 400 rather than as a schedule that silently ticks every minute.
    """
    try:
        minutes: int = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError) as exc:
        raise ValueError("interval_minutes must be an integer") from exc
    if minutes < MIN_INTERVAL_MINUTES:
        raise ValueError(
            f"interval_minutes must be >= {MIN_INTERVAL_MINUTES}"
        )
    return minutes


def parse_dispatch_stamp(entry: "ScheduleEntry") -> datetime | None:
    """``last_dispatched_at`` as an aware datetime, or None when never stamped.

    Both formats ever written to that field are accepted: the UTC tz-aware
    string from ``dispatch_now`` and the interval path, and the naive local-time
    string from the wall-clock ``tick``/``catch_up`` branches (localized to the
    entry's timezone here so a comparison never mixes naive and aware values).
    Interval cadence is measured off this stamp, so tolerating both is what
    keeps a migrated or hand-edited entry from firing on every tick.
    """
    raw = getattr(entry, "last_dispatched_at", "") or ""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(entry.timezone_name))
    return dt


def _matches_frequency(entry: "ScheduleEntry", dt_local: datetime) -> bool:
    if entry.frequency == "manual":
        return False  # manual schedules never auto-fire
    if is_interval(entry):
        # Interval cadence has no wall-clock slot to match. Callers branch
        # before reaching here; this guard keeps a stray call from falling
        # through to the "daily" default and reporting every minute as due.
        return False
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

    For an interval entry the next run is ``last_dispatched_at + interval``, and
    "now" when it has never fired — cadence resumes rather than aligning to a
    slot, so there is no wall-clock time to parse.
    """
    if not entry.enabled or entry.frequency == "manual":
        return None
    if is_interval(entry):
        tz = ZoneInfo(entry.timezone_name)
        current = now or _now_utc()
        last = parse_dispatch_stamp(entry)
        if last is None:
            return current.astimezone(tz)
        return (last + interval_delta(entry)).astimezone(tz)
    # Range-checked, not just parseable: `int()` accepts "25:00" and "09:99"
    # and the out-of-range value only blows up later, in `datetime.replace`
    # below — outside this try, so it escaped as a 500 from every caller that
    # serialises a schedule (`_enrich_schedule`, and therefore the whole
    # `GET /api/schedules` list, permanently). An unusable time is "never
    # fires", which is what None already means here.
    if not _VALID_DAILY_TIME.match(entry.daily_time_utc or ""):
        return None
    hh, mm = entry.daily_time_utc.split(":")
    target_h, target_m = int(hh), int(mm)
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

    Also None for interval entries, and deliberately so: they have no expected
    slot to miss. Their cadence is relative, so every skipped tick (a busy
    target chat, a paused entry, a restart) would read as a missed run, and the
    catch-up pass would replay it. An interval entry reports its health through
    ``last_status`` instead.
    """
    if not entry.enabled or entry.frequency == "manual" or is_interval(entry):
        return None
    # Same range check as compute_next_run: `int()` alone lets "25:00" through
    # to a `datetime.replace` that raises outside this guard.
    if not _VALID_DAILY_TIME.match(entry.daily_time_utc or ""):
        return None
    hh, mm = entry.daily_time_utc.split(":")
    target_h, target_m = int(hh), int(mm)
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

    Tolerates both stamp formats written to ``last_dispatched_at`` — see
    :func:`parse_dispatch_stamp`.
    """
    dt = parse_dispatch_stamp(entry)
    if dt is None:
        return False
    return dt >= when


@dataclass(slots=True)
class ScheduleEntry:
    """One persisted schedule (wall-clock, one-off, manual, or interval)."""

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
    # Outcome of the most recent interval run, for entries whose cadence has no
    # expected wall-clock slot to compare against: "" (never ran), "running",
    # "ok", "error", "busy" (skipped, the target chat had a turn in flight) or
    # "missing-chat" (target gone and unrecoverable; the entry was disabled).
    # Wall-clock entries leave this empty and report health through the
    # missed-run check instead.
    last_status: str = ""
    days_of_week: list[str] | None = None  # e.g. ["sun"] or ["mon","wed","fri"]; used when frequency="weekly"
    thread_id: int | None = None           # target topic (None = DM)
    frequency: str = "weekly"              # "daily", "weekly", "monthly", "manual", "once", "interval"
    # Minutes between runs when frequency="interval"; ignored otherwise. 0 on a
    # wall-clock entry means "unset", and interval_delta() floors a stored value
    # at MIN_INTERVAL_MINUTES so a hand-edited 0 cannot become a hot loop.
    interval_minutes: int = 0
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
    # Where to re-home a *fixed-chat* entry whose target chat is gone, without
    # turning it into a project entry. On an interval schedule `web_project_id`
    # is the primary binding and means "a fresh chat per run", which takes
    # precedence over `web_chat_id` — so a loop's project hint could not simply
    # be carried into that field during migration without changing what the
    # automation does. It is kept here instead: consulted only as a fallback,
    # never as a target. Empty means fall back to the workspace's General.
    fallback_project_id: str = ""
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
            # Interval entries carry no fire time, so an empty daily_time_utc
            # would sort them above every timed routine. Group them after
            # instead, ordered by cadence then age.
            items.sort(key=lambda item: (
                is_interval(item),
                item.interval_minutes if is_interval(item) else 0,
                item.daily_time_utc,
                item.created_at,
            ))
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
        interval_minutes: int = 0,
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
            interval_minutes=(
                normalize_interval_minutes(interval_minutes)
                if frequency == INTERVAL_FREQUENCY
                else int(interval_minutes or 0)
            ),
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
            normalize_auto_archive(entry)
            data.setdefault("schedules", []).append(self._serialize_entry(entry))
            self._save(data)
        return entry

    def replace(self, entry: ScheduleEntry) -> None:
        # Both write doors normalise, so no caller can persist an auto-archive
        # policy the dispatcher will refuse to honour.
        normalize_auto_archive(entry)
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
            entry.interval_minutes = int(entry.interval_minutes or 0)
        except (TypeError, ValueError):
            entry.interval_minutes = 0
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
    """Polls schedules of every cadence and dispatches them as chat turns.

    Interval entries bound to an existing chat get overlap protection that
    wall-clock entries do not need: ``chat_busy`` is consulted before each fire
    and a busy target means skip-not-queue, so a slow turn does not accumulate
    queued prompts behind it. ``chat_dispatchable`` reports whether such an
    entry still has somewhere to run; an entry whose target is unrecoverable is
    disabled rather than retried (and logged) every twenty seconds.
    """

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
        chat_busy: Callable[[str], bool] | None = None,
        chat_dispatchable: Callable[[ScheduleEntry], bool] | None = None,
    ) -> None:
        self._store = store
        self._resolve_target = resolve_target
        self._dispatch_to_web = dispatch_to_web
        self._prepare_chat = prepare_chat
        self._is_node_active = is_node_active
        self._chat_busy = chat_busy
        self._chat_dispatchable = chat_dispatchable
        # Interval runs are awaited (not fire-and-forget) so their outcome can
        # be stamped, which means a long run can still be in flight when the
        # next tick comes due. Tracked here so the cadence skips instead of
        # starting a second copy.
        self._inflight: set[str] = set()
        # Chats an interval run has claimed but not yet started streaming on.
        # `_fire_interval` reaches `create_task` without ever awaiting, so the
        # broker is still empty when the same tick evaluates the next entry:
        # two entries bound to one chat both read `chat_busy` as False and both
        # fire, and the second `start_stream` silently returns the first one's
        # stream, dropping the second prompt while recording it as a clean run.
        self._claimed_chats: set[str] = set()
        # Strong references to the in-flight run tasks. The event loop holds
        # only weak ones, and `_inflight` is discarded exclusively from inside
        # the task, so a collected task would strand its schedule id there and
        # the automation would read "run in progress" forever.
        self._run_tasks: set[asyncio.Task[Any]] = set()
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
        interval_minutes: int = 0,
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
            interval_minutes=interval_minutes,
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

    def backfill_fallback_projects(self, pcm) -> int:
        """Stamp the re-home fallback on chat-bound entries that never got one.

        Same window as ``backfill_project_names``: ``fallback_project_id`` can
        only be read off the bound chat while that chat still exists, and the
        entries this repairs are exactly the ones whose chat has not been
        deleted yet. Without it, every chat-bound schedule written before
        ``stamp_fallback_project`` existed — the MCP `schedule`/`loop` creators
        stamped nothing at all — keeps an empty fallback until someone happens
        to edit it, and deleting its chat first re-homes the unattended run
        into the workspace's General instead of the project it lived in.

        Only fills blanks: a non-empty fallback is the user's own binding (or a
        migrated loop's original project) and is left alone, since refreshing
        it here would silently follow a chat that had since moved.
        """
        stamped = 0
        for entry in self.list_entries():
            if entry.fallback_project_id or entry.web_project_id:
                continue
            if not entry.web_chat_id:
                continue
            if stamp_fallback_project(entry, pcm):
                self.replace(entry)
                stamped += 1
        if stamped:
            logger.info("Recorded the re-home fallback on %d schedule(s)", stamped)
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
            task = asyncio.create_task(
                self._dispatch_to_web(
                    entry, model, mode, provider, target_chat_id=target_chat_id
                ),
                name=f"schedule-dispatch-{entry.schedule_id}",
            )
            # The loop keeps only a weak reference; without one of ours the
            # task can be collected mid-run and the dispatch simply vanishes.
            self._run_tasks.add(task)
            task.add_done_callback(self._run_tasks.discard)

    # ── Interval cadence ────────────────────────────────────────────────

    def _binds_existing_chat(self, entry: ScheduleEntry) -> bool:
        """True when this entry posts into one existing chat, not a new one."""
        return bool(entry.web_chat_id) and not entry.web_project_id

    def _interval_busy(self, entry: ScheduleEntry) -> bool:
        """Whether the entry's target chat already has a turn in flight.

        Only meaningful for the fixed-chat binding: a project-bound interval
        entry opens a fresh chat per run, so there is nothing to collide with.
        """
        if entry.schedule_id in self._inflight:
            return True
        if not self._binds_existing_chat(entry):
            return False
        chat_id = str(entry.web_chat_id)
        # Checked before `chat_busy`: a sibling entry that fired earlier in this
        # same tick has not reached the broker yet, so only the claim shows it.
        if chat_id in self._claimed_chats:
            return True
        if self._chat_busy is None:
            return False
        return self._chat_busy(chat_id)

    def _interval_dispatchable(self, entry: ScheduleEntry) -> bool:
        if self._chat_dispatchable is None:
            return True
        return self._chat_dispatchable(entry)

    def _stamp_interval_status(self, entry: ScheduleEntry, status: str) -> None:
        """Record a non-firing outcome, writing only when it actually changed.

        Without the guard a busy target would rewrite schedules.json on every
        twenty-second tick for as long as the turn lasts.
        """
        if entry.last_status == status:
            return
        entry.last_status = status
        self._store.replace(entry)

    def _disable_interval(self, entry: ScheduleEntry, reason: str) -> None:
        logger.warning(
            "Interval schedule %s: %s; disabling it", entry.schedule_id, reason
        )
        entry.enabled = False
        entry.last_status = "missing-chat"
        self._store.replace(entry)

    async def _fire_interval(self, entry: ScheduleEntry, now: datetime) -> str | None:
        """Start one interval run. Returns the target chat id, or None.

        ``prepare_chat`` may re-point ``entry.web_chat_id`` at a replacement
        chat (the target was archived, or gone but its project still resolves),
        so the entry is persisted here — otherwise the entry would forget the
        replacement and build a new chat every interval.
        """
        _, model, mode, provider = (
            self._resolve_target(entry)
            if self._resolve_target is not None
            else ("claude", entry.model, entry.mode, entry.provider)
        )
        # The binding as it stood *before* prepare_chat, which is what
        # `_run_interval` needs to tell a re-home apart from a user edit.
        # Sampling it in there instead read the already-re-pointed value, so
        # the carry-over below could never fire.
        bound_before = entry.web_chat_id
        chat_id: str | None = None
        if self._prepare_chat is not None:
            chat_id = self._prepare_chat(entry, entry.prompt, model, mode, provider)
        if chat_id is None:
            self._disable_interval(entry, "no chat left to dispatch into")
            return None
        # Stamp before dispatching: the interval is measured from this value, so
        # a crash mid-run must not leave the entry due again immediately.
        entry.last_dispatched_at = now.isoformat(timespec="seconds")
        entry.last_run_chat_id = chat_id
        entry.last_status = "running"
        self._store.replace(entry)
        self._inflight.add(entry.schedule_id)
        self._claimed_chats.add(chat_id)
        task = asyncio.create_task(
            self._run_interval(
                entry, model, mode, provider, chat_id, bound_before=bound_before
            ),
            name=f"interval-run-{entry.schedule_id}",
        )
        # Hold a strong reference until the task finishes: the loop keeps only
        # a weak one, and every release of `_inflight` happens inside the task.
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)
        return chat_id

    async def _run_interval(
        self,
        entry: ScheduleEntry,
        model: str,
        mode: BridgeMode,
        provider: str,
        chat_id: str,
        *,
        bound_before: str | None = None,
    ) -> None:
        status = "ok"
        try:
            result = (
                await self._dispatch_to_web(
                    entry, model, mode, provider, target_chat_id=chat_id
                )
                if self._dispatch_to_web is not None
                else None
            )
            if isinstance(result, dict) and result.get("status"):
                status = str(result["status"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Interval schedule %s dispatch failed", entry.schedule_id)
            status = "error"
        finally:
            self._inflight.discard(entry.schedule_id)
            self._claimed_chats.discard(chat_id)
        # Re-read before stamping: the user may have edited the entry while the
        # run was streaming. Only the dispatcher's own re-homing is carried over
        # from our copy; everything else on the stored row is the user's edit.
        latest = self._store.get(entry.schedule_id)
        if latest is None:
            return
        latest.last_status = status
        rehomed = bool(entry.web_chat_id) and entry.web_chat_id != bound_before
        if rehomed and latest.web_chat_id == bound_before:
            # prepare_chat re-pointed us at a replacement chat (the target was
            # archived or gone) and nobody has retargeted the stored row since.
            # Without carrying that across, the entry forgets the replacement
            # and builds a new chat every interval. When `latest` no longer
            # holds `bound_before` the user retargeted it mid-run, and their
            # edit wins.
            latest.web_chat_id = entry.web_chat_id
        latest.last_run_chat_id = chat_id
        self._store.replace(latest)

    async def _tick_interval(self, entry: ScheduleEntry, now: datetime) -> None:
        if entry.schedule_id in self._inflight:
            return
        last = parse_dispatch_stamp(entry)
        if last is not None and (now - last) < interval_delta(entry):
            return
        if not self._interval_dispatchable(entry):
            self._disable_interval(entry, "target chat and project are both gone")
            return
        if self._interval_busy(entry):
            # Skip, don't queue: retried on the next tick, so the run fires as
            # soon as the current turn finishes. last_dispatched_at is
            # deliberately left alone here.
            self._stamp_interval_status(entry, "busy")
            return
        await self._fire_interval(entry, now)

    async def _dispatch_interval_now(self, entry: ScheduleEntry) -> dict:
        """Fire one interval run immediately, even while the entry is disabled."""
        result: dict = {
            "schedule_id": entry.schedule_id,
            "archive_policy": entry.archive_policy,
        }
        if not self._interval_dispatchable(entry):
            return {**result, "status": "missing-chat"}
        if self._interval_busy(entry):
            return {
                **result,
                "status": "busy",
                "chat_id": entry.web_chat_id or "",
            }
        chat_id = await self._fire_interval(entry, _now_utc())
        if chat_id is None:
            return {**result, "status": "missing-chat"}
        return {**result, "status": "started", "chat_id": chat_id}

    async def dispatch_now(self, schedule_id: str) -> dict:
        """Trigger a schedule immediately through the chat pipeline.

        Returns the schedule_id and, when available, the chat_id of the
        created/target chat so the frontend can link to it. Interval entries
        additionally report a ``status`` — a manual run into a chat that is
        already streaming is refused rather than queued.
        """
        entry = self._store.get(schedule_id)
        if entry is None:
            raise ValueError(f"Schedule '{schedule_id}' not found.")
        if is_interval(entry):
            return await self._dispatch_interval_now(entry)
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
            if is_interval(entry):
                # Cadence measured from the last dispatch, with its own
                # overlap and missing-target handling.
                await self._tick_interval(entry, current)
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

    async def catch_up(
        self,
        now: datetime | None = None,
        *,
        skip_system: bool = False,
    ) -> list[str]:
        """Fire each schedule once when its latest expected run was missed.

        Called once on startup so schedules recover after the server was down
        across their target time. Only the most recent missed occurrence is
        dispatched; skipped intervals are not replayed as a backlog, and the
        original prompt is dispatched unchanged (without backdating its
        context to the missed slot).

        With ``skip_system`` the packaged system routines are left out: right
        after a first-time setup the onboarding chat should be the only new
        conversation, so routines wait for their next regular tick instead of
        firing all at once from the catch-up pass (see
        `ciao.setup_marker.SETUP_CATCH_UP_GRACE`). Interval entries are
        excluded regardless: they have no expected slot to have missed —
        resuming the cadence is the correct recovery, and the regular tick does
        that on its own within one interval of boot. Firing them here would
        mean every restart re-runs every interval entry at once.

        Returns the list of schedule_ids that were fired.
        """
        current = now or _now_utc()
        fired: list[str] = []
        for entry in self._store.list_entries():
            # Manual and disabled schedules never auto-fire.
            if entry.frequency == "manual" or not entry.enabled:
                continue
            if is_interval(entry) or (skip_system and entry.scope == "system"):
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
