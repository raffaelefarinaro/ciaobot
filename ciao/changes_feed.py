"""What Ciaobot changed on its own, read for Memory · Changes. Read-only.

There is no change journal: this module reads the records that already exist
and says only what they say.

- The undo log, `<vault>/Workspace/Memory-Consolidations.md`. Every writer
  (a memory pass, a care step, an attended chat) copies a line there before
  replacing or removing it: `## YYYY-MM-DD` headings over bullets
  `- <note path or region> — by: <memory pass <id> | care <step> | chat <id>> —
  "<original text>" — <why>` (the stock memory guide's undo log rule). A pure
  addition has no entry, so nothing here counts what was added.
- The last care report (:mod:`ciao.care_report`): each finished step's own
  evidence line, as the run wrote it.
- Skill suggestions: `Workspace/Proposals/<slug>.md` files of type
  `skill-edit`, by their `created` date.
- Memory passes that finished: the pass chats themselves (the route hands them
  in), one entry per archived chat that was read.

All of it is model-written text: every field is flattened, clipped and passed
through :func:`ciao.redact.redact_text`, and the page renders it as plain
text. Paths are kept only once :func:`ciao.care_report.vault_relative_path`
accepts them. Files are opened without following a link at any step and read
under a size cap (the undo log's newest bytes when it is larger). Reading never
raises.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ciao.care_report import list_vault_folder, read_care_report, read_vault_file, vault_relative_path
from ciao.redact import redact_text

UNDO_LOG_PARTS = ("Workspace", "Memory-Consolidations.md")
UNDO_LOG_RELATIVE = "/".join(UNDO_LOG_PARTS)
PROPOSALS_PARTS = ("Workspace", "Proposals")
# The log is append-only and grows for ever; past this only its newest bytes are read.
MAX_LOG_BYTES = 512 * 1024
# Newest undo-log entries kept, proposal files read and pass chats taken.
MAX_UNDO_ENTRIES = 400
MAX_PROPOSALS = 200
MAX_PASSES = 400
# What one answer holds, newest first.
MAX_GROUPS = 60
MAX_ENTRIES = 300
MAX_TEXT_CHARS = 600
MAX_TARGET_CHARS = 240
MAX_ID_CHARS = 80
WEEK_DAYS = 7

# The separators the guide writes (an em dash) and the stand-ins a model uses.
_SEP = r"(?:\u2014|--|\u2013)"
_OPEN_QUOTES = ('"', "\u201c")
_CLOSE_QUOTES = ('"', "\u201d")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*\S)\s*$")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_FENCE = re.compile(r"^\s*(```|~~~)")
_BY_SPLIT = re.compile(r"^(?P<target>.+?)\s+" + _SEP + r"\s+by:\s*(?P<rest>.+)$", re.IGNORECASE)
_BY_TAIL = re.compile(r"^(?P<by>.+?)\s+" + _SEP + r"\s+(?P<tail>.+)$")
_BY_KIND = re.compile(r"^(memory pass|care|chat)\s+`?([A-Za-z0-9][A-Za-z0-9_.:-]*)`?", re.IGNORECASE)
_CLOSE_THEN_SEP = re.compile(r"[\"\u201d]\s+" + _SEP + r"\s+")
_LAST_SEP = re.compile(r"\s+" + _SEP + r"\s+")
_REGION = re.compile(r"^(?:ciao:)?(memory|profile)(?:\s+region)?$|\bciao:(memory|profile)\b", re.IGNORECASE)
_MERGE = re.compile(r"\bmerg|\bduplicate", re.IGNORECASE)
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
_PROPOSAL_STATUSES = ("draft", "ready", "applied", "rejected")

# Which filter a care step's line belongs under; any other step is about notes.
STEP_AREAS = {"regions": "memory", "unsure": "memory", "closed-tasks": "tasks", "learnings": "skills"}


def _clip(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    text = " ".join(redact_text(str(value or "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _entry_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _split_quote(tail: str) -> tuple[str, str]:
    """The original text and the reason, from what follows `by: …`."""
    tail = tail.strip()
    if tail[:1] in _OPEN_QUOTES:
        closes = list(_CLOSE_THEN_SEP.finditer(tail, 1))
        if closes:
            last = closes[-1]
            return tail[1: last.start()], tail[last.end():]
        if tail[-1:] in _CLOSE_QUOTES and len(tail) > 1:
            return tail[1:-1], ""
        return tail[1:], ""
    seps = list(_LAST_SEP.finditer(tail))
    if seps:
        return tail[: seps[-1].start()], tail[seps[-1].end():]
    return tail, ""


def parse_undo_log(text: str) -> list[dict[str, Any]]:
    """Every entry of the undo log, in file order. Pure: never raises.

    A bullet that does not carry `by:` is not an entry; a bullet before any
    dated `##` heading has an empty `date`. Fenced code is skipped.
    """
    entries: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    day = ""
    fenced = False
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = _HEADING.match(line)
        if heading:
            # A `##` (or deeper) heading starts a day; one with no date ends it.
            if len(heading.group(1)) >= 2:
                found = _DATE.search(heading.group(2))
                day = found.group(1) if found else ""
            continue
        bullet = _BULLET.match(line)
        if not bullet:
            continue
        split = _BY_SPLIT.match(bullet.group(1))
        if not split:
            continue
        target_raw = split.group("target").strip().strip("`").strip()
        rest = split.group("rest")
        by_tail = _BY_TAIL.match(rest)
        by_raw, tail = (by_tail.group("by"), by_tail.group("tail")) if by_tail else (rest, "")
        quote, why = _split_quote(tail) if tail else ("", "")
        by_kind, by_id = "other", ""
        by_match = _BY_KIND.match(by_raw.strip().strip("`"))
        if by_match:
            by_kind = by_match.group(1).lower().replace(" ", "_")
            by_id = by_match.group(2)[:MAX_ID_CHARS]
        region_match = _REGION.search(target_raw)
        region = (region_match.group(1) or region_match.group(2)).lower() if region_match else ""
        path = "" if region else vault_relative_path(target_raw)
        if region:
            kind, area = "memory-remove", "memory"
        elif _MERGE.search(why):
            kind, area = "merge", "notes"
        else:
            kind, area = "note-edit", "notes"
        key = _entry_id("undo", day, line)
        seen[key] = seen.get(key, -1) + 1
        quote_text = _clip(quote)
        entries.append({
            "id": f"u-{key}-{seen[key]}",
            "date": day,
            "kind": kind,
            "area": area,
            "target": _clip(target_raw, MAX_TARGET_CHARS),
            "path": path,
            "region": region,
            "quote": quote_text,
            "why": _clip(why),
            "by": {"kind": by_kind, "id": by_id, "text": "" if by_match else _clip(by_raw, MAX_TARGET_CHARS)},
            "undo": bool(quote_text),
        })
    return entries


def read_undo_log(vault_root: Path) -> tuple[str, list[dict[str, Any]]]:
    """`(status, entries)`: the newest :data:`MAX_UNDO_ENTRIES` entries, in file order.

    status is `ok`, `missing`, `tail` (only the newest bytes were read) or
    `unreadable` (a link at any step, not a regular file, an I/O error).
    """
    try:
        status, raw, _mtime = read_vault_file(vault_root, UNDO_LOG_PARTS, limit=MAX_LOG_BYTES, tail=True)
    except FileNotFoundError:
        return "missing", []
    except (OSError, ValueError, RuntimeError):
        return "unreadable", []
    if status not in ("ok", "tail"):
        return "unreadable", []
    text = raw.decode("utf-8", errors="replace")
    if status == "tail":
        # The first line was cut part-way and the heading above it is gone:
        # entries before the first heading read have no date and are dropped.
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return status, parse_undo_log(text)[-MAX_UNDO_ENTRIES:]


def _as_date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    found = _DATE.search(str(value or ""))
    return found.group(1) if found else ""


def read_skill_suggestions(vault_root: Path) -> list[dict[str, Any]]:
    """Skill-edit proposals in `Workspace/Proposals/`, as entries. Never raises."""
    try:
        names = sorted(name for name in list_vault_folder(vault_root, PROPOSALS_PARTS) if _SLUG.fullmatch(name))
    except (OSError, ValueError, RuntimeError):
        return []
    found: list[dict[str, Any]] = []
    for name in names[:MAX_PROPOSALS]:
        try:
            status, raw, _mtime = read_vault_file(vault_root, (*PROPOSALS_PARTS, name))
        except (OSError, ValueError, RuntimeError):
            continue
        if status != "ok":
            continue
        front = _FRONT_MATTER.match(raw.decode("utf-8", errors="replace"))
        if front is None:
            continue
        try:
            fields = yaml.safe_load(front.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(fields, dict) or fields.get("type") != "skill-edit":
            continue
        target = str(fields.get("target") or "")
        status_field = fields.get("status")
        found.append({
            "id": f"p-{_entry_id('proposal', name)}",
            "date": _as_date(fields.get("created")),
            "kind": "skill-suggestion",
            "area": "skills",
            "target": target if _SKILL_NAME.fullmatch(target) else "",
            "title": _clip(fields.get("title"), 200),
            "status": status_field if status_field in _PROPOSAL_STATUSES else "",
            "path": "/".join((*PROPOSALS_PARTS, name)),
            "undo": False,
        })
    return found


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def pass_entries(passes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per finished memory pass: `{chat_id, source_chat_id, source_title, finished_at}`.

    `finished_at` is an ISO time; its local date files the entry. Oldest first.
    """
    out: list[dict[str, Any]] = []
    for item in passes:
        when = _parse_time(str(item.get("finished_at") or ""))
        if when is None:
            continue
        chat_id = str(item.get("chat_id") or "")[:MAX_ID_CHARS]
        out.append({
            "id": f"c-{_entry_id('pass', chat_id)}",
            "date": when.astimezone().date().isoformat(),
            "at": when.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "kind": "chat-processed",
            "area": "memory",
            "pass_chat_id": chat_id,
            "chat_id": str(item.get("source_chat_id") or "")[:MAX_ID_CHARS],
            "title": _clip(item.get("source_title"), 200),
            "undo": False,
        })
    out.sort(key=lambda e: e["at"])
    return out[-MAX_PASSES:]


def _care_entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in report.get("steps") or []:
        if step.get("state") != "done":
            continue
        text = step.get("evidence") or step.get("what") or ""
        if not text:
            continue
        step_id = str(step.get("id") or "")
        out.append({
            "id": f"s-{_entry_id('step', step_id, text)}",
            "kind": "task-closed" if step_id == "closed-tasks" else "care-step",
            "area": STEP_AREAS.get(step_id, "notes"),
            "step": step_id,
            "text": _clip(text),
            "undo": False,
        })
    return out


def build_feed(
    *,
    undo: list[dict[str, Any]],
    report: dict[str, Any] | None,
    proposals: list[dict[str, Any]],
    passes: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    """Group everything by care run and by day, newest first, with the counts. Pure.

    The care group is the last report's run: its finished steps, the undo-log
    entries its steps wrote on its date and the skill suggestions dated that
    day. Everything else is filed under its day.
    """
    care: dict[str, Any] | None = None
    if report and report.get("status") == "ok":
        when = _parse_time(str(report.get("modified_at") or ""))
        care_date = str(report.get("updated") or "") or (when.astimezone().date().isoformat() if when else "")
        steps = _care_entries(report)
        if care_date and steps:
            care = {
                "id": f"care-{care_date}",
                "kind": "care",
                "date": care_date,
                "at": str(report.get("modified_at") or ""),
                "steps": len(steps),
                "entries": [],
            }
    days: dict[str, dict[str, Any]] = {}

    def day_group(day: str) -> dict[str, Any]:
        if day not in days:
            days[day] = {"id": f"day-{day}", "kind": "day", "date": day, "at": "", "entries": []}
        return days[day]

    for entry in reversed(undo):
        if not entry["date"]:
            continue
        if care is not None and entry["by"]["kind"] == "care" and entry["date"] == care["date"]:
            care["entries"].append(entry)
        else:
            day_group(entry["date"])["entries"].append(entry)
    if care is not None and report is not None:
        care["entries"].extend(_care_entries(report))
    for proposal in proposals:
        if not proposal["date"]:
            continue
        if care is not None and proposal["date"] == care["date"]:
            care["entries"].append(proposal)
        else:
            day_group(proposal["date"])["entries"].append(proposal)
    for chat in reversed(passes):
        day_group(chat["date"])["entries"].append(chat)

    groups = sorted(days.values(), key=lambda g: g["date"], reverse=True)
    if care is not None:
        at = next((i for i, g in enumerate(groups) if g["date"] <= care["date"]), len(groups))
        groups.insert(at, care)
    kept: list[dict[str, Any]] = []
    budget = MAX_ENTRIES
    for group in groups[:MAX_GROUPS]:
        if budget <= 0:
            break
        group["entries"] = group["entries"][:budget]
        budget -= len(group["entries"])
        kept.append(group)

    iso_today = today.isoformat()
    since = (today - timedelta(days=WEEK_DAYS - 1)).isoformat()

    def in_week(day: str) -> bool:
        return bool(day) and since <= day <= iso_today

    week_lines = [e for e in undo if in_week(e["date"])]
    week = {
        "since": since,
        "lines_changed": len(week_lines),
        "memory_lines": sum(1 for e in week_lines if e["region"]),
        "note_lines": sum(1 for e in week_lines if not e["region"]),
        "skill_suggestions": sum(1 for p in proposals if in_week(p["date"])),
        "chats_processed": sum(1 for c in passes if in_week(c["date"])),
    }
    today_counts = {
        "date": iso_today,
        "lines_changed": sum(1 for e in undo if e["date"] == iso_today),
        "skill_suggestions": sum(1 for p in proposals if p["date"] == iso_today),
        "chats_processed": sum(1 for c in passes if c["date"] == iso_today),
    }
    care_run = None
    if care is not None:
        care_run = {
            "date": care["date"],
            "at": care["at"],
            "lines_changed": sum(1 for e in care["entries"] if e["id"].startswith("u-")),
            "skill_suggestions": sum(1 for e in care["entries"] if e["kind"] == "skill-suggestion"),
        }
    return {"groups": kept, "week": week, "today": today_counts, "care_run": care_run}


def read_changes(vault_root: Path, passes: Iterable[dict[str, Any]], *, today: date | None = None) -> dict[str, Any]:
    """The whole feed for one vault. Never raises."""
    undo_status, undo = read_undo_log(vault_root)
    feed = build_feed(
        undo=undo,
        report=read_care_report(vault_root),
        proposals=read_skill_suggestions(vault_root),
        passes=pass_entries(passes),
        today=today or datetime.now().astimezone().date(),
    )
    feed["undo_log"] = undo_status
    return feed
