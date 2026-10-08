"""The delegation log a task carries in its own Markdown body (#1064).

The attempt store (``ciao/task_attempts.py``) is the operational record, and it
lives in the install's runtime directory: outside the vault, outside the backup,
and gone with ``.runtime/``. This is the durable half. Each attempt on a task is
one list item in a fenced section at the end of the task's body, so which chats
worked on a task, how each one ended and what the agent said it did survive a
lost runtime directory, travel with the vault, and read as plain Markdown in any
editor.

The section is the engine's, the rest of the body is the user's. Only the item
for the attempt being recorded is rewritten — found by the attempt id in a
trailing HTML comment — and everything else, including hand edits to other
items, is left as it is. The delegation prompt strips the section before quoting
the description (:func:`strip_log`), because the history is handed over on its
own terms rather than as part of "the work to do".
"""

from __future__ import annotations

import re
from datetime import datetime

LOG_OPEN = "<!-- ciao:task-log -->"
LOG_CLOSE = "<!-- /ciao:task-log -->"
LOG_HEADING = "## Delegation log"

_ITEM_MARK = re.compile(r"<!-- attempt:([0-9a-f]{32}) -->\s*$")
_SECTION = re.compile(
    "^" + re.escape(LOG_OPEN) + r"(?=\r?$).*?^" + re.escape(LOG_CLOSE) + r"(?=\r?$)",
    re.DOTALL | re.MULTILINE,
)

#: What each attempt state, or reported outcome, is called in the log and on the
#: board. One vocabulary, so the vault and the card never disagree.
OUTCOME_LABELS = {
    "done": "Agent says done",
    "blocked": "Blocked",
    "needs_input": "Needs input",
}
STATE_LABELS = {
    "running": "Working",
    "needs_you": "Waiting on you",
    "ready_for_review": "Agent says done",
    "failed": "Failed",
    "interrupted": "Interrupted",
    "stopped": "Stopped",
}


def attempt_label(state: str, outcome: str, detail: str = "") -> str:
    """The one word or phrase an attempt is called by.

    A settled engine fact (``failed``, ``interrupted``, ``stopped``) outranks the
    agent's report: a turn that crashed after saying "done" did not finish. While
    the attempt still holds the task, the agent's own report says what it is, and
    a turn that ended with no report is *Unfinished* rather than "for review".
    """
    if state in ("failed", "interrupted", "stopped"):
        return STATE_LABELS[state]
    if state == "running":
        return STATE_LABELS["running"]
    if outcome in OUTCOME_LABELS:
        return OUTCOME_LABELS[outcome]
    if state == "needs_you":
        # An approval card or a question the turn raised leaves no engine note;
        # a turn that simply ended without a report does.
        return "Unfinished" if detail else STATE_LABELS["needs_you"]
    return STATE_LABELS.get(state, state)


def _local(stamp: str) -> str:
    """``YYYY-MM-DD HH:MM`` in the engine's local time, or ``""``."""
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def render_item(
    *,
    attempt_id: str,
    state: str,
    outcome: str,
    summary: str,
    detail: str,
    created_at: str,
    ended_at: str,
    chat_id: str,
    chat_title: str,
    archive_path: str,
) -> str:
    """One attempt as a log item: a headline line, then its summary indented.

    The summary is the agent's own words and may span paragraphs, so it is kept
    whole as an indented continuation of the item rather than squeezed onto the
    headline. An engine note (``detail``) stands in when the agent reported
    nothing.
    """
    started = _local(created_at)
    ended = _local(ended_at)
    when = f"{started} → {ended[11:] if ended[:10] == started[:10] else ended}" if ended else started
    chat = f'chat "{_one_line(chat_title) or "untitled"}" (`{chat_id}`)'
    if archive_path:
        chat += f", archived at `{archive_path}`"
    headline = f"- {when} · **{attempt_label(state, outcome, detail)}** · {chat} <!-- attempt:{attempt_id} -->"
    note = str(summary or "").strip() or _one_line(detail)
    if not note:
        return headline
    body = "\n".join(f"  {line}" if line.strip() else "" for line in note.splitlines())
    return f"{headline}\n{body}"


def strip_log(body: str) -> str:
    """The body without the engine's log section: the description as written."""
    return _SECTION.sub("", body or "").rstrip() + ("\n" if (body or "").strip() else "")


def _items(section_inner: str) -> list[tuple[str, str]]:
    """``(attempt_id or "", text)`` per item, in order, from the section's inside.

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
    match = _ITEM_MARK.search(lines[0]) if lines[0].startswith("- ") else None
    return (match.group(1) if match else "", text)


def upsert_item(body: str, attempt_id: str, item: str) -> str:
    """The body with *item* recorded for *attempt_id* in the log section.

    Replaces that attempt's item in place when it is there, otherwise puts it
    first (newest first, as the board lists attempts). Creates the section at
    the end of the body when the task has none.
    """
    text = body or ""
    match = _SECTION.search(text)
    if match is None:
        section = f"{LOG_OPEN}\n{LOG_HEADING}\n\n{item}\n{LOG_CLOSE}"
        head = text.rstrip()
        return f"{head}\n\n{section}\n" if head else f"{section}\n"
    inner = match.group(0)[len(LOG_OPEN) : -len(LOG_CLOSE)].strip("\n")
    entries = _items(inner)
    replaced = False
    rebuilt: list[str] = []
    for found_id, entry in entries:
        if found_id == attempt_id:
            rebuilt.append(item)
            replaced = True
        else:
            rebuilt.append(entry)
    if not replaced:
        # After the heading (and any other leading prose), before the first item.
        at = next((i for i, (found_id, entry) in enumerate(entries) if entry.startswith("- ")), len(entries))
        rebuilt.insert(at, item)
    lead = [entry for entry in rebuilt if not entry.startswith("- ")]
    listed = [entry for entry in rebuilt if entry.startswith("- ")]
    inner_text = "\n\n".join(lead) + ("\n\n" if lead else "") + "\n".join(listed)
    section = f"{LOG_OPEN}\n{inner_text.strip()}\n{LOG_CLOSE}"
    return text[: match.start()] + section + text[match.end() :]
