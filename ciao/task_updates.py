"""Progress notes a task carries in its own Markdown body.

A task's description is the work that was asked for. Progress is a separate
record: what the user or the working agent did while the task was in progress
or in review. Each note is one item in a fenced section, so the description,
the delegation log and the completion history keep their own rewrite rules.

The section is the engine's. Appending a note inserts one item. Editing a
note rewrites that item's text and its ``edited`` stamp, never its
``recorded_at``, actor, or attempt. Hand-written items the parser cannot read
are left in place by every rewrite.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

OPEN = "<!-- ciao:task-updates -->"
CLOSE = "<!-- /ciao:task-updates -->"
HEADING = "## Progress"

MAX_UPDATE_CHARS = 8000
ACTORS = ("user", "agent")

_SECTION = re.compile(
    "^" + re.escape(OPEN) + r"(?=\r?$).*?^" + re.escape(CLOSE) + r"(?=\r?$)",
    re.DOTALL | re.MULTILINE,
)
_TAIL = re.compile(
    r"<!-- update:([0-9a-f]{32}) -->"
    r"(?:\s*<!-- edited:([^<>]+?) -->)?"
    r"\s*$"
)
_ATTEMPT = re.compile(r" · attempt `([^`]+)`")
_CHAT = re.compile(r" · chat `([^`]+)`")

_ESCAPED_OPEN = "&lt;!-- ciao:task-updates --&gt;"
_ESCAPED_CLOSE = "&lt;!-- /ciao:task-updates --&gt;"


@dataclass(frozen=True, slots=True)
class TaskUpdate:
    """One progress note: when, who wrote it, and the text."""

    id: str
    recorded_at: datetime
    actor: str
    text: str
    edited_at: datetime | None = None
    attempt_id: str = ""
    chat_id: str = ""

    def __post_init__(self) -> None:
        if self.actor not in ACTORS:
            raise ValueError(f"actor must be 'user' or 'agent', not {self.actor!r}")
        if self.recorded_at.tzinfo is None:
            object.__setattr__(self, "recorded_at", self.recorded_at.replace(tzinfo=UTC))
        if self.edited_at is not None and self.edited_at.tzinfo is None:
            object.__setattr__(self, "edited_at", self.edited_at.replace(tzinfo=UTC))


def mint_id() -> str:
    return uuid.uuid4().hex


def _escape(text: str) -> str:
    return text.replace(OPEN, _ESCAPED_OPEN).replace(CLOSE, _ESCAPED_CLOSE)


def _unescape(text: str) -> str:
    return text.replace(_ESCAPED_OPEN, OPEN).replace(_ESCAPED_CLOSE, CLOSE)


def render_update(update: TaskUpdate) -> str:
    """One note as a section item: a headline, then its text indented."""
    when = update.recorded_at.astimezone(UTC).isoformat(timespec="seconds")
    note = _escape(update.text.strip())
    lines = note.splitlines() if note else []
    headline = f"- {when} · {update.actor}"
    if update.attempt_id:
        headline += f" · attempt `{update.attempt_id}`"
    if update.chat_id:
        headline += f" · chat `{update.chat_id}`"
    headline += f" <!-- update:{update.id} -->"
    if update.edited_at is not None:
        headline += (
            " <!-- edited:"
            f"{update.edited_at.astimezone(UTC).isoformat(timespec='seconds')} -->"
        )
    if len(lines) > 1:
        rest = "\n".join(f"  {line}" if line.strip() else "" for line in lines[1:])
        first = lines[0]
        return f"{headline}\n  {first}\n{rest}"
    if lines:
        return f"{headline}\n  {lines[0]}"
    return headline


def strip_updates(body: str) -> str:
    """The body without the progress section."""
    return _SECTION.sub("", body or "").rstrip() + ("\n" if (body or "").strip() else "")


def extract_section(body: str) -> str:
    """The progress section span, or ``""`` when absent."""
    match = _SECTION.search(body or "")
    return match.group(0) if match is not None else ""


def _items(section_inner: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    current: list[str] = []
    for line in section_inner.splitlines():
        if line.startswith("- ") and current:
            items.append(_finish(current))
            current = []
        current.append(line)
    if current:
        items.append(_finish(current))
    return items


def _finish(lines: list[str]) -> tuple[str, str]:
    text = "\n".join(lines).rstrip()
    if not lines or not lines[0].startswith("- "):
        return ("", text)
    tail = _TAIL.search(lines[0])
    return (tail.group(1) if tail else "", text)


def _parse_item(item_id: str, text: str) -> TaskUpdate | None:
    lines = text.split("\n")
    head = lines[0]
    if not head.startswith("- "):
        return None
    tail = _TAIL.search(head)
    if tail is None or tail.group(1) != item_id:
        return None
    core = head[: tail.start()].rstrip()[2:]
    when_text, separator, rest = core.partition(" · ")
    if not separator:
        return None
    actor, _marker, rest = rest.partition(" · ")
    if actor not in ACTORS:
        return None
    if not _marker:
        rest = ""
    try:
        recorded_at = datetime.fromisoformat(when_text.strip())
    except ValueError:
        return None
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)
    edited_at: datetime | None = None
    if tail.group(2) is not None:
        try:
            edited_at = datetime.fromisoformat(tail.group(2).strip())
        except ValueError:
            return None
        if edited_at.tzinfo is None:
            edited_at = edited_at.replace(tzinfo=UTC)
    attempt = _ATTEMPT.search(" · " + rest) if rest else None
    chat = _CHAT.search(" · " + rest) if rest else None
    continuations = [line[2:] if line.startswith("  ") else line for line in lines[1:]]
    body = "\n".join(continuations).strip("\n")
    return TaskUpdate(
        id=item_id,
        recorded_at=recorded_at,
        actor=actor,
        text=_unescape(body),
        edited_at=edited_at,
        attempt_id=attempt.group(1) if attempt else "",
        chat_id=chat.group(1) if chat else "",
    )


def parse_updates(body: str) -> list[TaskUpdate]:
    """Progress notes, newest first. A file with no section parses to ``[]``."""
    match = _SECTION.search(body or "")
    if match is None:
        return []
    inner = match.group(0)[len(OPEN) : -len(CLOSE)].strip("\n")
    found: list[TaskUpdate] = []
    for item_id, text in _items(inner):
        if not item_id:
            continue
        parsed = _parse_item(item_id, text)
        if parsed is not None:
            found.append(parsed)
    return found


def latest_update_id(body: str) -> str:
    """The newest progress note's id, or ``""`` when the task has none."""
    found = parse_updates(body)
    return found[0].id if found else ""


def unseen_updates(body: str, seen_update_id: str) -> list[TaskUpdate]:
    """Notes newer than *seen_update_id*, oldest first.

    An empty cursor, or one that is no longer in the section, means every
    note is unseen: the attempt was never told, or the cursor was lost.
    """
    newest_first = parse_updates(body)
    if not seen_update_id:
        return list(reversed(newest_first))
    unseen: list[TaskUpdate] = []
    for item in newest_first:
        if item.id == seen_update_id:
            return list(reversed(unseen))
        unseen.append(item)
    return list(reversed(newest_first))


def _rebuild(entries: list[tuple[str, str]]) -> str:
    rebuilt = [entry for _, entry in entries]
    lead = [entry for entry in rebuilt if not entry.startswith("- ")]
    listed = [entry for entry in rebuilt if entry.startswith("- ")]
    inner = "\n\n".join(lead) + ("\n\n" if lead else "") + "\n".join(listed)
    return f"{OPEN}\n{inner.strip()}\n{CLOSE}"


def append_update(body: str, update: TaskUpdate) -> str:
    """The body with *update* recorded first in the progress section."""
    if len(update.text) > MAX_UPDATE_CHARS:
        raise ValueError("progress note is too long")
    text = body or ""
    item = render_update(update)
    match = _SECTION.search(text)
    if match is None:
        section = f"{OPEN}\n{HEADING}\n\n{item}\n{CLOSE}"
        head = text.rstrip()
        return f"{head}\n\n{section}\n" if head else f"{section}\n"
    inner = match.group(0)[len(OPEN) : -len(CLOSE)].strip("\n")
    entries = _items(inner)
    if any(found_id == update.id for found_id, _ in entries if found_id):
        raise ValueError(f"update {update.id} is already recorded")
    at = next(
        (index for index, (_, entry) in enumerate(entries) if entry.startswith("- ")),
        len(entries),
    )
    entries.insert(at, (update.id, item))
    section = _rebuild(entries)
    return text[: match.start()] + section + text[match.end() :]


def replace_update(body: str, update_id: str, text: str, edited_at: datetime) -> str:
    """The body with one note's text reworded. Actor and recorded time stay."""
    if len(text) > MAX_UPDATE_CHARS:
        raise ValueError("progress note is too long")
    stamp = edited_at if edited_at.tzinfo is not None else edited_at.replace(tzinfo=UTC)
    document = body or ""
    match = _SECTION.search(document)
    if match is None:
        raise ValueError(f"unknown update {update_id}")
    inner = match.group(0)[len(OPEN) : -len(CLOSE)].strip("\n")
    entries = _items(inner)
    at = next(
        (index for index, (found_id, _) in enumerate(entries) if found_id == update_id),
        None,
    )
    if at is None:
        raise ValueError(f"unknown update {update_id}")
    parsed = _parse_item(update_id, entries[at][1])
    if parsed is None:
        raise ValueError(f"update {update_id} is not readable")
    entries[at] = (
        update_id,
        render_update(
            TaskUpdate(
                id=parsed.id,
                recorded_at=parsed.recorded_at,
                actor=parsed.actor,
                text=text,
                edited_at=stamp,
                attempt_id=parsed.attempt_id,
                chat_id=parsed.chat_id,
            )
        ),
    )
    section = _rebuild(entries)
    return document[: match.start()] + section + document[match.end() :]
