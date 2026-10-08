"""The completion history a task carries in its own Markdown body (#1152).

A task marked done has ``updated_at`` and no completion record: ``updated_at``
moves on every later edit, so it cannot serve as a completion date, and the
delegation log (:mod:`ciao.task_log`) is the agent's report, not the user's
resolution. This is the durable half of "done". Each managed transition into
``done`` appends one item to a second engine section beside the delegation
log, so when a task was completed, what the resolution said and which attempt
it closed survive a lost runtime directory, travel with the vault, and read
as plain Markdown in any editor.

The section is the engine's, the rest of the body is the user's. Appending a
completion only inserts one item (matched by its ``<!-- completion:<id> -->``
mark on a rewrite); editing a resolution only re-renders that item's text and
its ``edited`` stamp, never its ``completed_at``. Everything else in the
section, including hand edits to other items, is left as it is. The delegation
prompt strips the section before quoting the description
(:func:`strip_completions`), because a past resolution must not be handed to
the next agent as instructions.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

OPEN = "<!-- ciao:task-completions -->"
CLOSE = "<!-- /ciao:task-completions -->"
HEADING = "## Completion history"

#: Rendered when a completion carries no resolution text.
NO_RESOLUTION = "No resolution"

_SECTION = re.compile(
    re.escape(OPEN) + r".*?" + re.escape(CLOSE), re.DOTALL
)
_ID_MARK = re.compile(r"<!-- completion:([0-9a-f]{32}) -->")
_TAIL = re.compile(
    r"<!-- completion:([0-9a-f]{32}) -->"
    r"\s*(<!-- edited:(.+?) -->)?\s*$"
)
_ATTEMPT_SUFFIX = re.compile(r"^(?P<text>.*) · attempt `(?P<aid>[^`]+)`$")


@dataclass(frozen=True, slots=True)
class Completion:
    """One recorded completion: when, what the resolution said, what it closed.

    ``id`` is 32 lowercase hex minted once with :func:`uuid.uuid4`.
    ``completed_at`` is timezone-aware UTC (naive inputs are assumed UTC,
    mirroring the task store's own coercion). ``resolution`` is ``""`` when
    none was given. ``edited_at`` is None until the resolution is reworded,
    and ``attempt_id`` is ``""`` when no attempt is named.
    """

    id: str
    completed_at: datetime
    resolution: str = ""
    edited_at: datetime | None = None
    attempt_id: str = ""

    def __post_init__(self) -> None:
        if self.completed_at.tzinfo is None:
            object.__setattr__(
                self, "completed_at", self.completed_at.replace(tzinfo=UTC)
            )
        if self.edited_at is not None and self.edited_at.tzinfo is None:
            object.__setattr__(
                self, "edited_at", self.edited_at.replace(tzinfo=UTC)
            )


def mint_id() -> str:
    """A fresh completion id."""
    return uuid.uuid4().hex


#: Rendered forms of the section markers inside resolution text. A resolution
#: is user prose and may name the markers literally; embedding them verbatim
#: would terminate the section early (the section match ends at the first
#: closing marker), so the renderer escapes them and the parser restores
#: them. The doubled form keeps an already-escaped literal unambiguous, so
#: every input round-trips exactly.
_ESCAPED_OPEN = "&lt;!-- ciao:task-completions --&gt;"
_ESCAPED_CLOSE = "&lt;!-- /ciao:task-completions --&gt;"
_DOUBLED_OPEN = "&amp;lt;!-- ciao:task-completions --&gt;"
_DOUBLED_CLOSE = "&amp;lt;!-- /ciao:task-completions --&gt;"


def _escape_markers(text: str) -> str:
    """Resolution text with literal section markers escaped for rendering."""
    for marker, escaped, doubled in (
        (OPEN, _ESCAPED_OPEN, _DOUBLED_OPEN),
        (CLOSE, _ESCAPED_CLOSE, _DOUBLED_CLOSE),
    ):
        text = text.replace(escaped, doubled).replace(marker, escaped)
    return text


def _unescape_markers(text: str) -> str:
    """Parsed item text with escaped section markers restored to literals."""
    for marker, escaped, doubled in (
        (OPEN, _ESCAPED_OPEN, _DOUBLED_OPEN),
        (CLOSE, _ESCAPED_CLOSE, _DOUBLED_CLOSE),
    ):
        text = text.replace(escaped, marker).replace(doubled, escaped)
    return text


def render_completion(completion: Completion) -> str:
    """One completion as a section item: a headline line, then its text indented.

    The headline carries the completion instant, the resolution's first line
    (or ``No resolution``), the attempt id when one is named, and the
    completion mark; a reworded resolution adds the ``edited`` stamp after it.
    The rest of the resolution follows as indented continuation lines, so a
    multi-line resolution never reads as new items. Literal section markers
    in the resolution are escaped, so they stay user prose rather than
    becoming section syntax.
    """
    when = completion.completed_at.astimezone(UTC).isoformat(timespec="seconds")
    note = _escape_markers(completion.resolution.strip())
    lines = note.splitlines() if note else []
    first = lines[0] if lines else NO_RESOLUTION
    headline = f"- {when} · {first}"
    if completion.attempt_id:
        headline += f" · attempt `{completion.attempt_id}`"
    headline += f" <!-- completion:{completion.id} -->"
    if completion.edited_at is not None:
        headline += (
            " <!-- edited:"
            f"{completion.edited_at.astimezone(UTC).isoformat(timespec='seconds')} -->"
        )
    if len(lines) > 1:
        rest = "\n".join(
            f"  {line}" if line.strip() else "" for line in lines[1:]
        )
        return f"{headline}\n{rest}"
    return headline


def strip_completions(body: str) -> str:
    """The body without the engine's completion section: the description as written."""
    return _SECTION.sub("", body or "").rstrip() + ("\n" if (body or "").strip() else "")


def extract_section(body: str) -> str:
    """The completion section span (markers included), or ``""`` when absent."""
    match = _SECTION.search(body or "")
    return match.group(0) if match is not None else ""


def _items(section_inner: str) -> list[tuple[str, str]]:
    """``(completion_id or "", text)`` per item, in order, from the section's inside.

    An item is a line starting with ``- `` plus every line after it that does not
    start a new item. Text in the section that is not an item (the heading, a
    stray note) is carried as an item with no id, so nothing a user wrote there
    is lost by a rewrite.
    """
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
    match = _ID_MARK.search(lines[0]) if lines[0].startswith("- ") else None
    return (match.group(1) if match else "", text)


def _parse_item(item_id: str, text: str) -> Completion | None:
    """One ``_items`` entry as a :class:`Completion`, or None when unreadable.

    An unreadable item is skipped by the parser but kept by every rewrite:
    a hand edit the engine cannot read is still the user's text.
    """
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
    try:
        completed_at = datetime.fromisoformat(when_text.strip())
    except ValueError:
        return None
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    edited_at: datetime | None = None
    if tail.group(3) is not None:
        try:
            edited_at = datetime.fromisoformat(tail.group(3).strip())
        except ValueError:
            return None
        if edited_at.tzinfo is None:
            edited_at = edited_at.replace(tzinfo=UTC)
    attempt_id = ""
    attempt = _ATTEMPT_SUFFIX.match(rest)
    if attempt is not None:
        rest = attempt.group("text")
        attempt_id = attempt.group("aid")
    continuations = [
        line[2:] if line.startswith("  ") else line for line in lines[1:]
    ]
    resolution = rest
    if continuations:
        resolution = (resolution + "\n" + "\n".join(continuations)).rstrip("\n")
    if rest == NO_RESOLUTION and not resolution[len(rest) :].strip():
        # What the renderer writes for an empty resolution reads back as one:
        # a lone "No resolution" headline with no continuation lines is the
        # absence of a resolution, not a resolution saying so.
        resolution = ""
    else:
        resolution = _unescape_markers(resolution)
    return Completion(
        id=item_id,
        completed_at=completed_at,
        resolution=resolution,
        edited_at=edited_at,
        attempt_id=attempt_id,
    )


def parse_completions(body: str) -> list[Completion]:
    """The section's completions, newest first, skipping what is not an item.

    A task file with no section — including a legacy done file — parses to
    ``[]``: nothing here invents a completion date.
    """
    match = _SECTION.search(body or "")
    if match is None:
        return []
    inner = match.group(0)[len(OPEN) : -len(CLOSE)].strip("\n")
    found: list[Completion] = []
    for item_id, text in _items(inner):
        if not item_id:
            continue
        parsed = _parse_item(item_id, text)
        if parsed is not None:
            found.append(parsed)
    return found


def append_completion(body: str, completion: Completion) -> str:
    """The body with *completion* recorded first in the completion section.

    Puts the item first (newest first, as the delegation log does) and creates
    the section at the end of the body when the task has none. A duplicate id
    raises :class:`ValueError` and the body is returned unchanged by the
    caller, never written — a replay must not record twice.
    """
    text = body or ""
    item = render_completion(completion)
    match = _SECTION.search(text)
    if match is None:
        section = f"{OPEN}\n{HEADING}\n\n{item}\n{CLOSE}"
        head = text.rstrip()
        return f"{head}\n\n{section}\n" if head else f"{section}\n"
    inner = match.group(0)[len(OPEN) : -len(CLOSE)].strip("\n")
    entries = _items(inner)
    if any(found_id == completion.id for found_id, _ in entries if found_id):
        raise ValueError(f"completion {completion.id} is already recorded")
    at = next(
        (
            index
            for index, (_, entry) in enumerate(entries)
            if entry.startswith("- ")
        ),
        len(entries),
    )
    entries.insert(at, (completion.id, item))
    rebuilt = [entry for _, entry in entries]
    lead = [entry for entry in rebuilt if not entry.startswith("- ")]
    listed = [entry for entry in rebuilt if entry.startswith("- ")]
    inner_text = "\n\n".join(lead) + ("\n\n" if lead else "") + "\n".join(listed)
    section = f"{OPEN}\n{inner_text.strip()}\n{CLOSE}"
    return text[: match.start()] + section + text[match.end() :]


def replace_resolution(
    body: str, completion_id: str, text: str, edited_at: datetime
    ) -> str:
    """The body with one completion's resolution reworded.

    Only that item's text and its ``edited`` stamp change: ``completed_at``,
    the id and the attempt stay exactly as recorded. An unknown id raises
    :class:`ValueError` with the body untouched.
    """
    stamp = edited_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    document = body or ""
    match = _SECTION.search(document)
    if match is None:
        raise ValueError(f"unknown completion {completion_id}")
    inner = match.group(0)[len(OPEN) : -len(CLOSE)].strip("\n")
    entries = _items(inner)
    at = next(
        (index for index, (found_id, _) in enumerate(entries) if found_id == completion_id),
        None,
    )
    if at is None:
        raise ValueError(f"unknown completion {completion_id}")
    _, current = entries[at]
    parsed = _parse_item(completion_id, current)
    if parsed is None:
        raise ValueError(f"completion {completion_id} is not readable")
    entries[at] = (
        completion_id,
        render_completion(
            Completion(
                id=parsed.id,
                completed_at=parsed.completed_at,
                resolution=text,
                edited_at=stamp,
                attempt_id=parsed.attempt_id,
            )
        ),
    )
    rebuilt = [entry for _, entry in entries]
    lead = [entry for entry in rebuilt if not entry.startswith("- ")]
    listed = [entry for entry in rebuilt if entry.startswith("- ")]
    inner_text = "\n\n".join(lead) + ("\n\n" if lead else "") + "\n".join(listed)
    section = f"{OPEN}\n{inner_text.strip()}\n{CLOSE}"
    return document[: match.start()] + section + document[match.end() :]
