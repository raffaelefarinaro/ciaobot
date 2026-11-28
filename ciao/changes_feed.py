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

# A real undo-log line is a few hundred characters. A longer line is dropped
# before anything looks at it, so a model-written wall of text (the log copies
# note lines verbatim) cannot make the reader slow. Every check below runs on a
# line already under this cap, flattened to single spaces, with plain string
# searches or regexes that do not backtrack.
MAX_LINE_CHARS = 4000

# The separators the guide writes (an em dash) and the stand-ins a model uses,
# as they read once whitespace is flattened.
_SEPS = (" \u2014 ", " -- ", " \u2013 ")
_BY_WORDS = ("by:", "By:", "BY:")
_OPEN_QUOTES = ('"', "\u201c")
_CLOSE_QUOTES = ('"', "\u201d")
_FENCES = ("```", "~~~")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_BY_KIND = re.compile(r"(memory pass|care|chat) `?([A-Za-z0-9][A-Za-z0-9_.:-]{0,79})", re.IGNORECASE)
_REGION = re.compile(
    r"^(?:ciao:)?(memory|profile)(?: region)?$|\bciao:(memory|profile)\b|\b(memory|profile) region\b", re.IGNORECASE
)
_MERGE = re.compile(r"\bmerg|\bduplicate", re.IGNORECASE)
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
_PROPOSAL_STATUSES = ("draft", "ready", "applied", "rejected")
_CARE_REPORT_REF = "Workspace/Care-Report.md"
MAX_EVIDENCE_ITEMS = 20

# Which filter a care step's line belongs under; any other step is about notes.
STEP_AREAS = {"regions": "memory", "unsure": "memory", "closed-tasks": "tasks", "learnings": "skills"}


def _clip(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    text = " ".join(redact_text(str(value or "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _entry_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _first_sep(text: str, after: tuple[str, ...] = ("",)) -> tuple[int, int]:
    """`(start, end)` of the earliest separator followed by one of `after`, or `(-1, -1)`."""
    best = (-1, -1)
    for sep in _SEPS:
        for word in after:
            at = text.find(sep + word)
            if at >= 0 and (best[0] < 0 or at < best[0]):
                best = (at, at + len(sep) + len(word))
    return best


def _split_quote(tail: str) -> tuple[str, str]:
    """The original text and the reason, from what follows `by: …`.

    A quoted original runs to the last closing quote that is followed by a
    separator, so a quote holding a dash keeps it; an unquoted one runs to the
    last separator.
    """
    tail = tail.strip()
    if tail[:1] in _OPEN_QUOTES:
        cut, cut_end = -1, -1
        for close in _CLOSE_QUOTES:
            for sep in _SEPS:
                at = tail.rfind(close + sep, 1)
                if at > cut:
                    cut, cut_end = at, at + len(close) + len(sep)
        if cut >= 0:
            return tail[1:cut], tail[cut_end:]
        if tail[-1:] in _CLOSE_QUOTES and len(tail) > 1:
            return tail[1:-1], ""
        return tail[1:], ""
    cut, cut_end = -1, -1
    for sep in _SEPS:
        at = tail.rfind(sep)
        if at > cut:
            cut, cut_end = at, at + len(sep)
    if cut >= 0:
        return tail[:cut], tail[cut_end:]
    return tail, ""


def _heading(line: str) -> tuple[int, str] | None:
    """`(level, words)` for a Markdown heading line, else None."""
    text = line.lstrip()
    level = len(text) - len(text.lstrip("#"))
    if not 1 <= level <= 6 or len(text) == level or text[level] not in " \t":
        return None
    return level, text[level:].strip().rstrip("#").strip()


def _bullet(line: str) -> str:
    """A bullet's text with whitespace flattened, or "" for any other line."""
    text = line.strip()
    if len(text) < 3 or text[0] not in "-*+" or text[1] not in " \t":
        return ""
    return " ".join(text[2:].split())


def _note_path(target: str) -> str:
    """`target` as a confined vault path to a Markdown note, or "" (never a whole sentence)."""
    if len(target) > MAX_TARGET_CHARS or not target.lower().endswith(".md"):
        return ""
    return vault_relative_path(target)


def parse_undo_log(text: str) -> list[dict[str, Any]]:
    """Every entry of the undo log, in file order. Pure: never raises.

    A bullet that does not carry `by:` is not an entry; a bullet before any
    dated `##` heading has an empty `date`. A line longer than
    :data:`MAX_LINE_CHARS` is skipped. A line opening with three backticks or
    tildes toggles fenced code, which is skipped; an unclosed fence hides the
    rest of the file, as it does when the file is rendered.
    """
    entries: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    day = ""
    fenced = False
    for line in text.split("\n"):
        if len(line) > MAX_LINE_CHARS:
            continue
        line = line.rstrip("\r")
        if line.lstrip().startswith(_FENCES):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = _heading(line)
        if heading is not None:
            # A `##` (or deeper) heading starts a day; one with no date ends it.
            if heading[0] >= 2:
                found = _DATE.search(heading[1])
                day = found.group(1) if found else ""
            continue
        body = _bullet(line)
        if not body:
            continue
        start, end = _first_sep(body, _BY_WORDS)
        if start < 0:
            continue
        target_raw = body[:start].strip().strip("`").strip()
        rest = body[end:].strip()
        by_start, by_end = _first_sep(rest)
        by_raw, tail = (rest[:by_start], rest[by_end:]) if by_start >= 0 else (rest, "")
        quote, why = _split_quote(tail) if tail else ("", "")
        by_kind, by_id = "other", ""
        by_match = _BY_KIND.match(by_raw.strip().strip("`"))
        if by_match:
            by_kind = by_match.group(1).lower().replace(" ", "_")
            by_id = by_match.group(2)
        region_match = _REGION.search(target_raw)
        region = next((g for g in region_match.groups() if g), "").lower() if region_match else ""
        path = "" if region else _note_path(target_raw)
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


def _front_matter(text: str) -> dict[str, Any]:
    """The few scalar fields and the evidence list of a proposal's front matter.

    Read line by line with plain string operations, not a YAML loader: the
    file is model-written, and only `type`, `target`, `title`, `created`,
    `status` and `evidence` are needed. A block scalar (`|` or `>`) keeps its
    indented lines; an evidence item is an indented `- item` line.
    """
    if not text.startswith("---"):
        return {}
    lines = text.split("\n")
    fields: dict[str, Any] = {"evidence": []}
    current = ""
    for raw in lines[1:]:
        line = raw.rstrip("\r")
        if line.strip() == "---":
            return fields
        if len(line) > MAX_LINE_CHARS:
            current = ""
            continue
        if line[:1] in (" ", "\t"):
            item = line.strip()
            if current == "evidence" and item.startswith("- ") and len(fields["evidence"]) < MAX_EVIDENCE_ITEMS:
                fields["evidence"].append(item[2:].strip().strip("'\""))
            elif current and current != "evidence" and isinstance(fields.get(current), str):
                fields[current] = (fields[current] + " " + item).strip()
            continue
        key, colon, value = line.partition(":")
        key = key.strip()
        if not colon or not key:
            current = ""
            continue
        current = key
        value = value.strip()
        if key == "evidence":
            if value.startswith("[") and value.endswith("]"):
                items = [v.strip().strip("'\"") for v in value[1:-1].split(",")]
                fields["evidence"] = [v for v in items if v][:MAX_EVIDENCE_ITEMS]
            continue
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            value = ""
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        fields[key] = value
    return {}


def read_skill_suggestions(vault_root: Path) -> list[dict[str, Any]]:
    """Skill-edit proposals in `Workspace/Proposals/`, as entries. Never raises.

    `cites_care_report` is true when the proposal's evidence lists the care
    report: the run that wrote it says so there.
    """
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
        fields = _front_matter(raw.decode("utf-8", errors="replace"))
        if fields.get("type") != "skill-edit":
            continue
        target = str(fields.get("target") or "")
        status_field = fields.get("status")
        found.append({
            "id": f"p-{_entry_id('proposal', name)}",
            "date": _as_date(str(fields.get("created") or "")[:MAX_TARGET_CHARS]),
            "kind": "skill-suggestion",
            "area": "skills",
            "target": target if _SKILL_NAME.fullmatch(target) else "",
            "title": _clip(fields.get("title"), 200),
            "status": status_field if status_field in _PROPOSAL_STATUSES else "",
            "path": "/".join((*PROPOSALS_PARTS, name)),
            "cites_care_report": any(_CARE_REPORT_REF in str(item) for item in fields.get("evidence") or []),
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
    entries a care step wrote on the run's date, and the skill suggestions the
    run itself names (a step's evidence names the proposal file, or the
    proposal's evidence lists the care report and it is dated that day). A
    proposal a memory pass or a chat wrote is filed under its day.

    Caveats, from what the records hold: the undo log has dates, not times, so
    two care runs on one day (a re-run) both credit their lines to the last
    report's run, and a run that crosses midnight files the lines it dated the
    next day under that day. Within a day, undo-log entries are newest first by
    their order in the file; the care group comes before the day group of the
    same date whatever its time.
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
    care_words = " ".join(
        f"{step.get('evidence') or ''} {step.get('what') or ''}" for step in (report or {}).get("steps") or []
    ) if care is not None else ""
    for proposal in proposals:
        named = care is not None and (
            proposal["path"] in care_words
            or (proposal.get("cites_care_report") and proposal["date"] == care["date"])
        )
        if named and care is not None:
            care["entries"].append(proposal)
        elif proposal["date"]:
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


def _note_exists(vault_root: Path, path: str) -> bool:
    try:
        status, _raw, _mtime = read_vault_file(vault_root, tuple(path.split("/")), limit=1, tail=True)
    except (OSError, ValueError, RuntimeError):
        return False
    return status in ("ok", "tail")


def read_changes(vault_root: Path, passes: Iterable[dict[str, Any]], *, today: date | None = None) -> dict[str, Any]:
    """The whole feed for one vault. Never raises.

    An undo-log entry's path is kept only when that note is there now (a
    regular file reached without following a link), so Undo never pins a file
    that does not exist.
    """
    undo_status, undo = read_undo_log(vault_root)
    for entry in undo:
        if entry["path"] and not _note_exists(vault_root, entry["path"]):
            entry["path"] = ""
    feed = build_feed(
        undo=undo,
        report=read_care_report(vault_root),
        proposals=read_skill_suggestions(vault_root),
        passes=pass_entries(passes),
        today=today or datetime.now().astimezone().date(),
    )
    feed["undo_log"] = undo_status
    return feed
