"""Usage statistics for one workspace, computed from the stores that already exist.

Read side only: nothing here records anything new. Every figure comes from a
record another module already writes:

* chats opened, user messages, hours and projects: the chat registry
  (``ChatInfo.created_at`` and ``ChatInfo.user_turn_timings``), person chats only;
* chats archived: the ``ended:`` frontmatter of the chat's archived transcript;
* tokens and models: the per-turn ``usage`` and ``effective_model`` in the live
  transcript JSON and the archived transcript Markdown;
* memories saved and the proposal funnel: the decision sidecar
  (``memory_proposals.read_decisions``) plus the pending queue's batch headers;
* notes updated: applied ``note_apply`` receipts in ``Workspace/Memory-Receipts.jsonl``;
* tasks completed: the completion records in each task file;
* automation runs: ``schedule_dispatch`` rows in ``job_runs.jsonl`` stamped
  with this workspace.

A completed local day never changes once it is over, so its bucket is cached
in memory per ``(workspace, date)``; today is always recomputed.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal

from ciao import job_runs
from ciao.memory_proposals import read_decisions
from ciao.memory_receipts import journal_path, read_receipts
from ciao.models import ChatContext
from ciao.proposal_kinds import parse_bullet
from ciao.task_board import TaskBoardError, TaskBoardStore
from ciao.task_resolution import parse_completions
from ciao.transcripts import parse_archive_turns

logger = logging.getLogger(__name__)

RANGES: tuple[int, ...] = (7, 30, 90, 365)
DEFAULT_RANGE = 30
DAILY_DAYS = 182
AVG_DAYS = 30
TOP_PROJECTS = 8

# Mirrors ``CiaoControlPlane._ENGINE_HELPER_KINDS``: helper chats the engine
# starts from its own prompt, so they are not chats a person opened.
_ENGINE_HELPER_KINDS = frozenset({"memory_pass", "update_task", "proposal"})
_PROPOSALS_RELATIVE = ("Workspace", "Memory-Proposals.md")
_BATCH_HEADER_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2}T\S+)")
_ENDED_RE = re.compile(r"""^ended:\s*['"]?([^'"\s]+)['"]?\s*$""")
_USAGE_LINE_RE = re.compile(
    r"^- (input_tokens|output_tokens|inputTokens|outputTokens): (\d+)\s*$", re.MULTILINE
)
_TOKEN_KEYS = ("input_tokens", "output_tokens", "inputTokens", "outputTokens")

EventKind = Literal[
    "chat_opened",
    "message",
    "chat_archived",
    "proposal_decided",
    "proposal_pending",
    "task_completed",
    "automation_run",
    "turn",
    "note_updated",
]


@dataclass(frozen=True, slots=True)
class ChatFacts:
    """What the stats need from one registry chat, copied on the event loop."""

    chat_id: str
    project_id: str
    created_at: str
    archived: bool
    archive_path: str
    person: bool
    person_sent_at: tuple[str, ...]
    live_transcript_dir: Path
    archive_dirs: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class StatsSources:
    """Everything one computation reads; built by :func:`snapshot_sources`."""

    workspace: str
    chats: tuple[ChatFacts, ...]
    project_names: Mapping[str, str]
    workspace_root: Path
    vault_root: Path
    runtime_dir: Path
    job_runs_path: Path


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind
    at: datetime
    project_id: str = ""
    model: str = ""
    tokens: int = 0
    accepted: bool = False


@dataclass(slots=True)
class DayBucket:
    chats_opened: int = 0
    messages_sent: int = 0
    chats_archived: int = 0
    accepted: int = 0
    proposed: int = 0
    notes_updated: int = 0
    tasks_completed: int = 0
    automation_runs: int = 0
    tokens: int = 0
    project_messages: dict[str, int] = field(default_factory=dict)
    hours: list[int] = field(default_factory=lambda: [0] * 24)
    models: dict[str, int] = field(default_factory=dict)


_DAY_CACHE: dict[tuple[str, date], DayBucket] = {}
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    """Forget every cached day (tests, and nothing else, need this)."""
    with _CACHE_LOCK:
        _DAY_CACHE.clear()


def parse_range(raw: str | None) -> int:
    """The ``range`` query value as days; blank means :data:`DEFAULT_RANGE`."""
    if raw is None or not raw.strip():
        return DEFAULT_RANGE
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value not in RANGES:
        raise ValueError(f"range must be one of {', '.join(str(r) for r in RANGES)}")
    return value


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _local(at: datetime, tz: tzinfo | None) -> datetime:
    return at.astimezone(tz) if tz is not None else at.astimezone()


def _day_start(day: date, tz: tzinfo | None) -> datetime:
    naive = datetime.combine(day, time.min)
    return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()


def _usage_tokens(usage: object) -> int:
    if not isinstance(usage, Mapping):
        return 0
    total = 0
    for key in _TOKEN_KEYS:
        try:
            total += int(str(usage.get(key, 0) or 0))
        except ValueError:
            continue
    return total


def _modified_since(path: Path, start: datetime) -> bool:
    try:
        return path.stat().st_mtime >= start.timestamp()
    except OSError:
        return False


def _failed(failures: list[str] | None, source: str) -> None:
    # A read error that degraded to empty: the day must not be cached as final.
    if failures is not None:
        failures.append(source)


# ── Snapshot (event loop) ────────────────────────────────────────────────


def snapshot_sources(config: Any, pcm: Any, workspace: str) -> StatsSources:
    """Copy the registry facts for *workspace*. Call on the event loop thread."""
    transcripts = pcm._transcripts
    chats_roots = [Path(root) for root in config.chats_roots()]
    chats: list[ChatFacts] = []
    names: dict[str, str] = {}
    for chat in pcm.list_chats():
        project = pcm.get_project(chat.project_id)
        if project is None or project.workspace != workspace:
            continue
        helper = chat.helper if isinstance(chat.helper, dict) else {}
        person = (
            not project.is_system
            and not chat.schedule_id
            and helper.get("kind") not in _ENGINE_HELPER_KINDS
        )
        sent: list[str] = []
        if person:
            names[project.project_id] = project.name
            for key, timing in chat.user_turn_timings.items():
                if not isinstance(timing, dict) or not timing.get("sent_at"):
                    continue
                if chat.user_turn_unattended.get(str(key)) or chat.user_turn_origin.get(str(key)):
                    continue
                sent.append(str(timing["sent_at"]))
        ctx = ChatContext.for_web(chat.chat_id)
        archive_dirs = tuple(dict.fromkeys([
            transcripts.archive_dir(ctx, chat.provider).parent,
            *(root / chat.chat_id for root in chats_roots),
        ]))
        chats.append(ChatFacts(
            chat_id=chat.chat_id,
            project_id=chat.project_id,
            created_at=chat.created_at,
            archived=bool(chat.archived),
            archive_path=chat.archive_path,
            person=person,
            person_sent_at=tuple(sent),
            live_transcript_dir=transcripts.current_path(ctx, chat.provider).parent,
            archive_dirs=archive_dirs,
        ))
    return StatsSources(
        workspace=workspace,
        chats=tuple(chats),
        project_names=names,
        workspace_root=Path(config.workspace_root),
        vault_root=Path(config.workspace_vault_root(workspace)),
        runtime_dir=Path(config.state_path).parent,
        # The recorder's own path: main.py pins it to config.state_path.parent,
        # tests pin it to tmp_path.
        job_runs_path=job_runs._log_path(),
    )


# ── Event collection (worker thread) ─────────────────────────────────────


def _chat_events(
    sources: StatsSources, start: datetime, end: datetime, *, failures: list[str] | None = None
) -> list[Event]:
    events: list[Event] = []
    for chat in sources.chats:
        if not chat.person:
            continue
        opened = _parse_ts(chat.created_at)
        if opened is not None and start <= opened < end:
            events.append(Event("chat_opened", opened, project_id=chat.project_id))
        for raw in chat.person_sent_at:
            sent = _parse_ts(raw)
            if sent is not None and start <= sent < end:
                events.append(Event("message", sent, project_id=chat.project_id))
        if chat.archived and chat.archive_path:
            path = Path(chat.archive_path)
            if not path.is_absolute():
                path = sources.workspace_root / path
            if not _modified_since(path, start):
                continue
            ended = _archive_ended_at(path, failures)
            if ended is not None and start <= ended < end:
                events.append(Event("chat_archived", ended, project_id=chat.project_id))
    return events


def _archive_ended_at(path: Path, failures: list[str] | None = None) -> datetime | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index and line.strip() == "---":
                    return None
                match = _ENDED_RE.match(line.rstrip("\n"))
                if match is not None:
                    return _parse_ts(match.group(1))
    except FileNotFoundError:
        return None
    except OSError:
        _failed(failures, "transcripts")
        return None
    except UnicodeDecodeError:
        # Corrupt content is permanent, not transient: cache the day as read.
        return None
    return None


def _turn_events(
    sources: StatsSources, start: datetime, end: datetime, *, failures: list[str] | None = None
) -> list[Event]:
    events: list[Event] = []
    seen: set[Path] = set()

    def add(raw_ts: object, model: object, tokens: int) -> None:
        at = _parse_ts(raw_ts)
        if at is None or not (start <= at < end):
            return
        events.append(Event("turn", at, model=str(model or ""), tokens=tokens))

    for chat in sources.chats:
        if chat.live_transcript_dir.is_dir():
            for live in sorted(chat.live_transcript_dir.glob("*.json")):
                if not _modified_since(live, start):
                    continue
                try:
                    data = json.loads(live.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    continue
                except OSError:
                    _failed(failures, "transcripts")
                    continue
                except ValueError:
                    # Corrupt content is permanent, not transient: cache the day as read.
                    continue
                turns = data.get("turns") if isinstance(data, dict) else None
                for turn in turns if isinstance(turns, list) else []:
                    if isinstance(turn, dict):
                        add(turn.get("timestamp"), turn.get("effective_model"),
                            _usage_tokens(turn.get("usage")))
        for folder in chat.archive_dirs:
            if not folder.is_dir():
                continue
            for archive in sorted(folder.glob("*/*.md")):
                key = archive.resolve()
                if key in seen or not _modified_since(archive, start):
                    continue
                seen.add(key)
                try:
                    text = archive.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                except OSError:
                    _failed(failures, "transcripts")
                    continue
                except UnicodeDecodeError:
                    # Corrupt content is permanent, not transient: cache the day as read.
                    continue
                for turn in parse_archive_turns(text):
                    tokens = sum(int(m.group(2)) for m in _USAGE_LINE_RE.finditer(turn["trailer"]))
                    add(turn["timestamp"], turn["effective_model"], tokens)
    return events


def _proposal_events(
    sources: StatsSources, start: datetime, end: datetime, *, failures: list[str] | None = None
) -> list[Event]:
    queue = sources.vault_root.joinpath(*_PROPOSALS_RELATIVE)
    events: list[Event] = []
    for row in read_decisions(queue):
        # Upstream skill drafts share this sidecar with kind "skill"; they are
        # not memories.
        if row["kind"] == "skill":
            continue
        at = _parse_ts(row["ts"])
        if at is not None and start <= at < end:
            events.append(Event("proposal_decided", at, accepted=row["action"] == "accepted"))
    try:
        lines = queue.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError:
        _failed(failures, "proposals")
        lines = []
    except UnicodeDecodeError:
        # Corrupt content is permanent, not transient: cache the day as read.
        lines = []
    batch: datetime | None = None
    for line in lines:
        header = _BATCH_HEADER_RE.match(line)
        if header is not None:
            batch = _parse_ts(header.group(1))
            continue
        if batch is not None and start <= batch < end and parse_bullet(line) is not None:
            events.append(Event("proposal_pending", batch))
    return events


def _task_events(
    sources: StatsSources, start: datetime, end: datetime, *, failures: list[str] | None = None
) -> list[Event]:
    store = TaskBoardStore(
        workspace=sources.workspace,
        vault_root=sources.vault_root,
        runtime_dir=sources.runtime_dir,
        clock=lambda: datetime.now(UTC),
    )
    try:
        documents = store.list().tasks
    except FileNotFoundError:
        return []
    except (TaskBoardError, OSError) as exc:
        # An unreadable board is transient; an unparseable one (TaskBoardError) is not.
        if isinstance(exc, OSError):
            _failed(failures, "tasks")
        logger.warning("usage stats: task board unreadable for %s: %s", sources.workspace, exc)
        return []
    events: list[Event] = []
    for document in documents:
        for completion in parse_completions(document.body):
            if start <= completion.completed_at < end:
                events.append(Event("task_completed", completion.completed_at))
    return events


def _automation_events(
    sources: StatsSources, start: datetime, end: datetime, *, failures: list[str] | None = None
) -> list[Event]:
    try:
        lines = sources.job_runs_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        _failed(failures, "automations")
        return []
    except UnicodeDecodeError:
        # Corrupt content is permanent, not transient: cache the day as read.
        return []
    events: list[Event] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("job") != "schedule_dispatch":
            continue
        extra = row.get("extra")
        if not isinstance(extra, dict) or extra.get("workspace") != sources.workspace:
            continue
        at = _parse_ts(row.get("started_at"))
        if at is not None and start <= at < end:
            events.append(Event("automation_run", at))
    return events


def _note_events(
    sources: StatsSources, start: datetime, end: datetime, *, failures: list[str] | None = None
) -> list[Event]:
    events: list[Event] = []
    for row in read_receipts(journal_path(sources.vault_root)):
        if row.get("kind") != "note_apply" or row.get("status") != "applied":
            continue
        if row.get("changed") is False:
            continue
        at = _parse_ts(row.get("ts"))
        if at is not None and start <= at < end:
            events.append(Event("note_updated", at))
    return events


def collect_events(
    sources: StatsSources, start: datetime, end: datetime, *, failures: list[str] | None = None
) -> list[Event]:
    """Every event in ``[start, end)`` from every store.

    A store that exists but could not be read appends its name to *failures*.
    """
    return [
        *_chat_events(sources, start, end, failures=failures),
        *_turn_events(sources, start, end, failures=failures),
        *_proposal_events(sources, start, end, failures=failures),
        *_task_events(sources, start, end, failures=failures),
        *_automation_events(sources, start, end, failures=failures),
        *_note_events(sources, start, end, failures=failures),
    ]


# ── Pure aggregation ─────────────────────────────────────────────────────


def bucket_events(events: Iterable[Event], tz: tzinfo | None) -> dict[date, DayBucket]:
    """Group events into local-date buckets (``tz=None`` is the system zone)."""
    buckets: dict[date, DayBucket] = {}
    for event in events:
        local = _local(event.at, tz)
        bucket = buckets.setdefault(local.date(), DayBucket())
        if event.kind == "chat_opened":
            bucket.chats_opened += 1
        elif event.kind == "message":
            bucket.messages_sent += 1
            bucket.hours[local.hour] += 1
            bucket.project_messages[event.project_id] = (
                bucket.project_messages.get(event.project_id, 0) + 1
            )
        elif event.kind == "chat_archived":
            bucket.chats_archived += 1
        elif event.kind == "proposal_decided":
            bucket.proposed += 1
            if event.accepted:
                bucket.accepted += 1
        elif event.kind == "proposal_pending":
            bucket.proposed += 1
        elif event.kind == "task_completed":
            bucket.tasks_completed += 1
        elif event.kind == "automation_run":
            bucket.automation_runs += 1
        elif event.kind == "turn":
            bucket.tokens += event.tokens
            if event.model:
                bucket.models[event.model] = bucket.models.get(event.model, 0) + 1
        elif event.kind == "note_updated":
            bucket.notes_updated += 1
    return buckets


def _days(last: date, count: int) -> list[date]:
    return [last - timedelta(days=offset) for offset in range(count - 1, -1, -1)]


def _totals(buckets: list[DayBucket]) -> dict[str, int]:
    projects: set[str] = set()
    for bucket in buckets:
        projects.update(bucket.project_messages)
    return {
        "chats_opened": sum(b.chats_opened for b in buckets),
        "messages_sent": sum(b.messages_sent for b in buckets),
        "chats_archived": sum(b.chats_archived for b in buckets),
        "memories_saved": sum(b.accepted for b in buckets),
        "tasks_completed": sum(b.tasks_completed for b in buckets),
        "projects_worked_on": len(projects),
        "automation_runs": sum(b.automation_runs for b in buckets),
        "tokens": sum(b.tokens for b in buckets),
    }


def build_response(
    buckets: Mapping[date, DayBucket],
    *,
    workspace: str,
    today: date,
    range_days: int,
    generated_at: datetime,
    project_names: Mapping[str, str],
) -> dict[str, Any]:
    """The ``GET /api/stats`` payload from day buckets (missing days are empty)."""

    def pick(days: list[date]) -> list[DayBucket]:
        return [buckets.get(day) or DayBucket() for day in days]

    current = pick(_days(today, range_days))
    previous = pick(_days(today - timedelta(days=range_days), range_days))
    prior = pick(_days(today - timedelta(days=1), AVG_DAYS))
    today_totals = _totals(pick([today]))

    project_counts: dict[str, int] = {}
    hours = [0] * 24
    models: dict[str, int] = {}
    for bucket in current:
        for project_id, count in bucket.project_messages.items():
            project_counts[project_id] = project_counts.get(project_id, 0) + count
        for hour, count in enumerate(bucket.hours):
            hours[hour] += count
        for model, turns in bucket.models.items():
            models[model] = models.get(model, 0) + turns
    ranked_projects = sorted(
        (
            (project_id, project_names[project_id], count)
            for project_id, count in project_counts.items()
            if project_id in project_names and count > 0
        ),
        key=lambda row: (-row[2], row[1], row[0]),
    )[:TOP_PROJECTS]

    return {
        "workspace": workspace,
        "range_days": range_days,
        "generated_at": generated_at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "today": {
            "chats_opened": today_totals["chats_opened"],
            "messages_sent": today_totals["messages_sent"],
            "chats_archived": today_totals["chats_archived"],
            "memories_saved": today_totals["memories_saved"],
            "projects_worked_on": today_totals["projects_worked_on"],
            "tasks_completed": today_totals["tasks_completed"],
            "avg_chats_opened_30d": round(sum(b.chats_opened for b in prior) / AVG_DAYS, 1),
            "avg_messages_sent_30d": round(sum(b.messages_sent for b in prior) / AVG_DAYS, 1),
        },
        "totals": _totals(current),
        "previous": _totals(previous),
        "daily_messages": [
            {"date": day.isoformat(), "count": (buckets.get(day) or DayBucket()).messages_sent}
            for day in _days(today, DAILY_DAYS)
        ],
        "projects": [
            {"project_id": project_id, "name": name, "messages": count}
            for project_id, name, count in ranked_projects
        ],
        "memory_pipeline": {
            "proposed": sum(b.proposed for b in current),
            "accepted": sum(b.accepted for b in current),
            "notes_updated": sum(b.notes_updated for b in current),
        },
        "hours": hours,
        "models": [
            {"model": model, "turns": turns}
            for model, turns in sorted(models.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def compute_usage_stats(
    sources: StatsSources,
    range_days: int,
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> dict[str, Any]:
    """The stats payload; past days come from the cache, today is live."""
    if range_days not in RANGES:
        raise ValueError(f"range must be one of {', '.join(str(r) for r in RANGES)}")
    moment = now if now is not None else datetime.now(UTC)
    today = _local(moment, tz).date()
    span = max(2 * range_days, DAILY_DAYS, AVG_DAYS + 1)
    past_days = _days(today - timedelta(days=1), span - 1)
    with _CACHE_LOCK:
        missing = [day for day in past_days if (sources.workspace, day) not in _DAY_CACHE]
        filled: dict[date, DayBucket] = {}
        if missing:
            failures: list[str] = []
            filled = bucket_events(
                collect_events(
                    sources, _day_start(missing[0], tz), _day_start(today, tz), failures=failures
                ),
                tz,
            )
            # A partial read would be cached as final; serve it once, retry next time.
            if not failures:
                for day in missing:
                    _DAY_CACHE[(sources.workspace, day)] = filled.get(day) or DayBucket()
        buckets: dict[date, DayBucket] = {
            day: _DAY_CACHE.get((sources.workspace, day)) or filled.get(day) or DayBucket()
            for day in past_days
        }
    live = bucket_events(collect_events(sources, _day_start(today, tz), moment), tz)
    buckets[today] = live.get(today) or DayBucket()
    return build_response(
        buckets,
        workspace=sources.workspace,
        today=today,
        range_days=range_days,
        generated_at=moment,
        project_names=sources.project_names,
    )
