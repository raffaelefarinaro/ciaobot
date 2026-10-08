"""Portable Markdown task records and a revision-safe task store.

This is the storage contract for the shared workspace task board
(GitHub issue #978, part of #973). One task is one file at
``<workspace-vault>/Workspace/Tasks/<id>.md``: YAML frontmatter carrying the
typed record, followed by a free Markdown body (description, links,
acceptance criteria, result notes). The file is readable and editable by the
user and by later typed agent/UI operations; the board is derived from these
files, and there is no second source of truth — no index file, no ranking
file, no cache.

The four rules everything else follows from:

**Stable identity.** A task id is 32 lowercase hex characters minted once
with :func:`uuid.uuid4`. The filename and the frontmatter ``id`` must agree,
and a title edit never renames the identity.

**Source preservation.** A managed edit replaces only the value spans of
the owned fields it changes (located with YAML node marks) and leaves every
other byte alone: unknown frontmatter fields, comments, key order, the
body, a BOM and CRLF line endings. An edit that would need an ambiguous
rewrite — an owned field stored as a block scalar or a collection, a
truncated frontmatter block — is refused with the original bytes untouched,
and the refusal names the shape instead of normalizing it silently.

**Explicit revisions.** A document's revision is the SHA-256 of its exact
raw bytes. Every update must present the revision it read; a stale one is a
:ref:`revision_conflict <TaskBoardError>` and the file is left unchanged.

**Managed-operation rules are not file restrictions.** The agent-cannot-complete,
and linked-task rules below bind this store's ``update`` path.
A user editing the Markdown directly can write whatever the schema accepts;
this module never polices hand edits, it only refuses to manufacture such
states through the managed API.

Locking and races
-----------------

Read-modify-write runs under a per-workspace process-wide
:func:`ciao.async_reads.keyed_lock` plus an advisory file lock in the
runtime directory, so managed writers in this process and in other
processes serialize against each other. External editors do not honor
advisory locks: a hand edit landing between the revision recheck and the
rename is still possible, and nothing here promises a filesystem sandbox
or race-free arbitrary external edits. What the protocol does promise is
that a detected stale revision, a parse failure and a write failure all
leave the prior bytes unchanged.

Confinement
-----------

Task ids are validated before any path is built, so no id can traverse out
of the task directory. Task files and the task directory itself are never
opened through a link (``O_NOFOLLOW`` semantics on every OS via
:func:`ciao.os_support.files.open_fd`), non-regular files are refused, and
nothing is read outside the explicit vault root passed to the store. There
is no recursive whole-vault traversal: only the single ``Tasks`` directory
is ever listed.

Later obligations (not this module)
------------------------------------

Live project membership and completion validation belong to a later
application service; this store keeps the ``project_id`` string but knows
no project registry. Linkage mutation (``chat_id``/``attempt_id``) belongs
to the delegation child: source hand edits may carry nullable linkage, but
this store never creates a live chat or attempt. Task records are reserved
bookkeeping (#1002): excluded from recall, the Memory Map graph, review and
curation, and inside the durable backup scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterator, Literal, Union

import yaml
from yaml.events import AliasEvent, ScalarEvent
from yaml.nodes import MappingNode, Node, ScalarNode

from ciao.async_reads import keyed_lock
from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import carry_mode
from ciao.task_log import extract_log
from ciao.task_resolution import (
    OPEN as COMPLETIONS_OPEN,
    Completion,
    append_completion,
    extract_section,
    parse_completions,
    replace_resolution,
    strip_completions,
)

SCHEMA_VERSION = 2
"""The only frontmatter schema this store implements.

Schema 2 (#1069) is the four-column board ``backlog | in_progress | in_review |
done``: ``on_hold`` is gone and the separate ``review_state`` flag became the
``in_review`` column. A schema-1 file is rewritten once by
:meth:`TaskBoardStore.migrate_schema_1`; this parser reads only schema 2.
"""

MAX_TASK_BYTES = 65536
"""Largest raw task file this store will read or write, in bytes."""

TASKS_RELATIVE = Path("Workspace") / "Tasks"
"""Vault-relative directory holding one ``<id>.md`` file per task."""

STATUSES = ("backlog", "in_progress", "in_review", "done")
"""Allowed ``status`` values, in board order. ``backlog`` is the *To do* column."""

ASSIGNEES = ("user", "agent")
"""Allowed ``assignee`` values."""

_ID_RE = re.compile(r"[0-9a-f]{32}")
"""A task id: 32 lowercase hex characters, nothing else."""

_DUE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Key order for freshly rendered files. Fixed so two renders of one record
# are byte-identical; hand-edited files may carry any order.
_FIELD_ORDER = (
    "schema",
    "id",
    "title",
    "status",
    "project_id",
    "due",
    "assignee",
    "created_at",
    "updated_at",
    "chat_id",
    "attempt_id",
)

# Fields a managed ``update`` accepts through ``changes``. ``schema``,
# ``id``, ``created_at``, ``chat_id`` and ``attempt_id`` are not ordinary
# user-editable patch fields: identity and creation time are immutable, and
# linkage mutation belongs to the delegation child. ``updated_at`` is set by
# the store from its clock, never by the caller.
_EDITABLE_FIELDS = frozenset(
    {"title", "status", "project_id", "due", "assignee"}
)

#: The two linkage fields, editable only through :meth:`TaskBoardStore.link` and
#: :meth:`TaskBoardStore.unlink`. See :func:`_check_editable`.
_EDITABLE_LINKAGE_FIELDS = frozenset({"chat_id", "attempt_id"})

_REQUIRED_FIELDS = (
    "schema",
    "id",
    "title",
    "status",
    "assignee",
    "created_at",
    "updated_at",
)

# Nullable linkage/optional fields. Absent and explicit-null both read as
# None, and rendering a new task writes them explicitly.
_OPTIONAL_NULLABLE_FIELDS = ("project_id", "due", "chat_id", "attempt_id")

_LOCK_TIMEOUT_S = 30.0

Actor = Literal["user", "agent"]


class TaskBoardError(Exception):
    """A typed task-store failure.

    ``code`` is one of ``invalid_task``, ``unsupported_schema``,
    ``not_found``, ``revision_conflict``, ``unsafe_path``,
    ``completion_requires_user``, ``bad_request`` or ``read_failed``:

    - ``invalid_task``: the file or the requested change does not satisfy
      the schema (bad enum, bad date, bad title, duplicate keys, alias or
      merge ambiguity, missing required keys, truncated frontmatter, an
      unsupported edit shape, a linkage/business-rule refusal).
    - ``unsupported_schema``: an integer ``schema`` this code does not
      implement.
    - ``not_found``: no task file for a well-formed id.
    - ``revision_conflict``: ``expected_revision`` is not the file's
      current revision; nothing was written.
    - ``unsafe_path``: a malformed id, a link or reparse point where a
      real file must be, a non-regular file, or a path escaping the vault.
    - ``completion_requires_user``: the agent caller tried to set status
      ``done`` through the managed API.
    - ``bad_request``: a managed-API argument has the wrong type (a
      non-string ``resolution`` or ``attempt_id`` on ``update``).
    - ``read_failed``: store I/O failed (directory traversal, read, lock
      or write). Parse problems are ``invalid_task``, never this.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """One task as this store understands it.

    ``due`` is a calendar ISO date (``YYYY-MM-DD``) or None — never a
    datetime, never a UTC scheduled start. ``created_at``/``updated_at``
    are timezone-aware UTC datetimes. ``project_id``, ``chat_id`` and
    ``attempt_id`` are nullable strings this foundation stores but never
    validates against a live registry: project membership and linkage
    mutation belong to later children.
    """

    schema: int
    id: str
    title: str
    status: str
    project_id: str | None
    due: str | None
    assignee: str
    created_at: datetime
    updated_at: datetime
    chat_id: str | None
    attempt_id: str | None


@dataclass(frozen=True, slots=True)
class TaskDocument:
    """A task file as read: the typed record, the body, and the raw bytes.

    ``raw`` is the file's exact bytes (BOM included). ``revision`` is the
    SHA-256 hex of ``raw`` and is what ``update`` requires back.
    ``relative_path`` is the vault-relative file path
    (``Workspace/Tasks/<id>.md``). ``body`` is the Markdown after the
    closing frontmatter delimiter, decoded as UTF-8.
    """

    record: TaskRecord
    body: str
    raw: bytes
    revision: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class TaskInvalidEntry:
    """A file in the task directory that is not a readable task.

    Surfaced separately so a malformed file can never masquerade as an
    empty or healthy board. ``code`` is the :class:`TaskBoardError` code
    that reading it produced.
    """

    relative_path: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class TaskListResult:
    """The outcome of :meth:`TaskBoardStore.list`.

    ``tasks`` holds the valid documents in board order; ``invalid`` holds
    one entry per file that could not be read as a task.
    """

    tasks: tuple[TaskDocument, ...]
    invalid: tuple[TaskInvalidEntry, ...]


# ── Small helpers ────────────────────────────────────────────────────


def _revision(raw: bytes) -> str:
    """The revision of one file's exact bytes."""
    return hashlib.sha256(raw).hexdigest()


def _check_id(task_id: str) -> str:
    """A validated task id, or an ``unsafe_path`` refusal.

    Runs before any path is built, so no id string can traverse out of
    the task directory.
    """
    if not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id):
        raise TaskBoardError(
            "unsafe_path", f"not a task id (32 lowercase hex): {task_id!r}"
        )
    return task_id


def _coerce_utc(value: Any, name: str) -> datetime:
    """An ISO moment or datetime as an aware UTC datetime.

    Naive inputs are assumed UTC rather than refused: a hand edit that
    wrote ``2026-10-03T12:00:00`` meant noon UTC on a machine whose vault
    has no other zone. Anything unparseable is ``invalid_task``.
    """
    moment: datetime
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        candidate = text
        if len(text) > 1 and text[-1] in ("Z", "z"):
            candidate = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(candidate)
        except ValueError:
            raise TaskBoardError(
                "invalid_task", f"{name} {value!r} is not an ISO-8601 datetime"
            ) from None
    else:
        raise TaskBoardError(
            "invalid_task", f"{name} must be an ISO-8601 datetime, not {type(value).__name__}"
        )
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _coerce_due(value: Any) -> str | None:
    """A due value as an ISO calendar date string, or None.

    Accepts a valid ``YYYY-MM-DD`` string and a YAML date scalar
    (normalized to ISO for the typed record). A datetime is refused
    outright: a due date is a calendar day, never an instant or a UTC
    scheduled start.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        raise TaskBoardError(
            "invalid_task",
            f"due {value!r} is a datetime, not a calendar date (YYYY-MM-DD)",
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not _DUE_RE.fullmatch(text):
            raise TaskBoardError(
                "invalid_task", f"due {value!r} is not a calendar date (YYYY-MM-DD)"
            )
        try:
            date.fromisoformat(text)
        except ValueError:
            raise TaskBoardError(
                "invalid_task", f"due {value!r} is not a real calendar date"
            ) from None
        return text
    raise TaskBoardError(
        "invalid_task", f"due must be a calendar date or null, not {type(value).__name__}"
    )


def _reject_non_roundtrip_controls(value: str, name: str) -> None:
    """Refuse chars PyYAML would not read back identically.

    Any control or surrogate (Unicode categories Cc/Cs) other than tab,
    plus ``\\x85``/``\\u2028``/``\\u2029``, is ``invalid_task``: such a
    title would otherwise be silently altered or leave a malformed file.
    """
    for char in value:
        if char == "\t":
            continue
        if char in ("\x85", "\u2028", "\u2029"):
            raise TaskBoardError(
                "invalid_task",
                f"{name} must not contain U+{ord(char):04X}",
            )
        if unicodedata.category(char) in ("Cc", "Cs"):
            raise TaskBoardError(
                "invalid_task",
                f"{name} must not contain U+{ord(char):04X}",
            )


def _coerce_optional_id(value: Any, name: str) -> str | None:
    """A nullable linkage/project string: None, or a non-empty string."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TaskBoardError(
            "invalid_task",
            f"{name} must be a string or null, not {value!r}",
        )
    _reject_non_roundtrip_controls(value, name)
    return value


def _coerce_title(value: Any) -> str:
    """A title as its trimmed 1–200 character form."""
    if not isinstance(value, str):
        raise TaskBoardError(
            "invalid_task", f"title must be a string, not {type(value).__name__}"
        )
    title = value.strip()
    if not (1 <= len(title) <= 200):
        raise TaskBoardError(
            "invalid_task",
            f"title must be 1-200 characters after trimming (got {len(title)})",
        )
    _reject_non_roundtrip_controls(title, "title")
    return title


# ── Frontmatter splitting (bytes stay byte-exact) ────────────────────


def _split_lines(text: str) -> list[tuple[str, str]]:
    """Split *text* into ``(content, ending)`` pairs, endings preserved.

    ``\\n`` terminates a line and the ``\\r`` of a ``\\r\\n`` pair belongs
    to the terminator rather than the content, so a CRLF file's values
    never carry a stray carriage return.
    """
    lines: list[tuple[str, str]] = []
    position = 0
    length = len(text)
    while position < length:
        newline = text.find("\n", position)
        if newline == -1:
            lines.append((text[position:], ""))
            position = length
        else:
            if newline > position and text[newline - 1] == "\r":
                lines.append((text[position : newline - 1], "\r\n"))
            else:
                lines.append((text[position:newline], "\n"))
            position = newline + 1
    return lines


def _line_starts(lines: list[tuple[str, str]]) -> list[int]:
    """Character offsets where each line starts in the joined text."""
    starts: list[int] = []
    offset = 0
    for content, ending in lines:
        starts.append(offset)
        offset += len(content) + len(ending)
    return starts


@dataclass(frozen=True, slots=True)
class _SplitDocument:
    """A decoded task file with its frontmatter span located."""

    text: str  # BOM-stripped decoded text
    bom: bool
    lines: list[tuple[str, str]]
    starts: list[int]
    open_index: int  # line index of the opening `---`
    close_index: int  # line index of the closing `---` / `...`
    newline: str  # newline to use when inserting a frontmatter line


def _split_document(raw: bytes) -> _SplitDocument:
    """Decode *raw* and locate its frontmatter block.

    Raises ``invalid_task`` for non-UTF-8 bytes and for missing or
    truncated frontmatter. The body and every line ending are preserved
    untouched for the caller to splice.
    """
    if len(raw) > MAX_TASK_BYTES:
        raise TaskBoardError(
            "invalid_task",
            f"task file is {len(raw)} bytes, over the {MAX_TASK_BYTES} limit",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskBoardError("invalid_task", f"task file is not valid UTF-8: {exc}") from None
    bom = text.startswith("\ufeff")
    if bom:
        text = text[1:]
    lines = _split_lines(text)
    if not lines or lines[0][0].rstrip() != "---":
        raise TaskBoardError("invalid_task", "task file has no opening frontmatter delimiter")
    close_index = -1
    for index in range(1, len(lines)):
        if lines[index][0].rstrip() in ("---", "..."):
            close_index = index
            break
    if close_index == -1:
        raise TaskBoardError("invalid_task", "task file frontmatter is truncated (no closing delimiter)")
    newline = lines[0][1] or "\n"
    return _SplitDocument(
        text=text,
        bom=bom,
        lines=lines,
        starts=_line_starts(lines),
        open_index=0,
        close_index=close_index,
        newline=newline,
    )


def _frontmatter_text(split: _SplitDocument) -> str:
    """The YAML source between the delimiters, endings normalized to LF.

    Normalization is only for the YAML parser: column offsets within a
    line are identical either way, and splicing always addresses the
    original lines, so CRLF files keep CRLF.
    """
    return "\n".join(content for content, _ in split.lines[split.open_index + 1 : split.close_index])


def _body_text(split: _SplitDocument) -> str:
    """The Markdown after the closing delimiter, verbatim."""
    after = split.close_index + 1
    if after >= len(split.lines):
        return ""
    return split.text[split.starts[after] :]


# ── YAML structure checks ────────────────────────────────────────────


def _reject_ambiguous_yaml(frontmatter: str) -> None:
    """Refuse alias and merge-key ambiguity before building a record.

    This is a new strict contract, not fallback compatibility code: a
    frontmatter that aliases or merges is ``invalid_task``, never
    silently resolved.
    """
    try:
        events = list(yaml.parse(frontmatter))
    except yaml.YAMLError as exc:
        raise TaskBoardError("invalid_task", f"task frontmatter does not parse: {exc}") from None
    for event in events:
        if isinstance(event, AliasEvent):
            raise TaskBoardError(
                "invalid_task", "task frontmatter must not use YAML aliases"
            )
        if isinstance(event, ScalarEvent) and event.value == "<<" and event.style is None:
            raise TaskBoardError(
                "invalid_task", "task frontmatter must not use YAML merge keys"
            )


def _compose_mapping(frontmatter: str) -> MappingNode:
    """The frontmatter as a YAML mapping node, duplicates rejected."""
    try:
        node = yaml.compose(frontmatter)
    except yaml.YAMLError as exc:
        raise TaskBoardError("invalid_task", f"task frontmatter does not parse: {exc}") from None
    if node is None:
        raise TaskBoardError("invalid_task", "task frontmatter holds no mapping")
    if not isinstance(node, MappingNode):
        raise TaskBoardError("invalid_task", "task frontmatter must be a mapping")
    _reject_duplicate_keys(node)
    return node


def _reject_duplicate_keys(node: Node) -> None:
    """Refuse duplicate mapping keys anywhere in the frontmatter."""

    def visit(current: Node) -> None:
        if isinstance(current, MappingNode):
            seen: set[str] = set()
            for key_node, value_node in current.value:
                if isinstance(key_node, ScalarNode):
                    if key_node.value in seen:
                        raise TaskBoardError(
                            "invalid_task",
                            f"task frontmatter repeats key {key_node.value!r}",
                        )
                    seen.add(key_node.value)
                visit(key_node)
                visit(value_node)
        else:
            children: list[Node] = []
            value = getattr(current, "value", None)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, Node):
                        children.append(item)
                    elif isinstance(item, tuple):
                        for part in item:
                            if isinstance(part, Node):
                                children.append(part)
            for child in children:
                visit(child)

    visit(node)


def _mapping_values(root: MappingNode) -> dict[str, ScalarNode | None]:
    """Top-level key to value node. Non-scalar keys are refused."""
    found: dict[str, ScalarNode | None] = {}
    for key_node, value_node in root.value:
        if not isinstance(key_node, ScalarNode):
            raise TaskBoardError("invalid_task", "task frontmatter keys must be scalars")
        if isinstance(value_node, ScalarNode):
            found[key_node.value] = value_node
        else:
            found[key_node.value] = None
    return found


# ── Schema 1 → 2 ─────────────────────────────────────────────────────

_FRONTMATTER = re.compile(rb"\A(\xef\xbb\xbf)?---\r?\n(.*?)(\r?\n)---", re.DOTALL)


def _upgrade_schema_1(raw: bytes) -> bytes | None:
    """The file rewritten to schema 2, or ``None`` when it is not schema 1.

    Line edits on the frontmatter only, each anchored to a whole ``key: value``
    line (quoted or not), so a value that merely contains one of these words is
    never touched.
    """
    match = _FRONTMATTER.match(raw)
    if match is None:
        return None
    front = match.group(2)
    if not re.search(rb"(?m)^schema:[ \t]*1[ \t]*\r?$", front):
        return None
    value = rb"[ \t]*[\"']?([a-z_]+)[\"']?[ \t]*(\r?)$"
    status = re.search(rb"(?m)^status:" + value, front)
    review = re.search(rb"(?m)^review_state:" + value, front)
    current = status.group(1) if status else b""
    target = current
    if current == b"on_hold":
        target = b"backlog"
    elif current == b"in_progress" and review is not None and review.group(1) == b"ready":
        target = b"in_review"
    if status is not None and target != current:
        front = re.sub(rb"(?m)^status:" + value, b"status: " + target + rb"\2", front, count=1)
    front = re.sub(rb"(?m)^review_state:.*\n?", b"", front)
    front = re.sub(rb"(?m)^schema:[ \t]*1([ \t]*\r?)$", rb"schema: 2\1", front, count=1)
    return raw[: match.start(2)] + front + raw[match.end(2) :]


# ── Record building ──────────────────────────────────────────────────


def _build_record(data: Mapping[str, Any], expected_id: str) -> TaskRecord:
    """Validate a parsed frontmatter mapping into a :class:`TaskRecord`.

    Unknown fields are ignored here and preserved in the raw bytes; they
    are never treated as executable instructions and never discarded by a
    managed edit.
    """
    if not isinstance(data, dict):
        raise TaskBoardError("invalid_task", "task frontmatter must be a mapping")
    if "schema" in data:
        early_schema = data["schema"]
        if type(early_schema) is not int:
            raise TaskBoardError(
                "invalid_task", f"task schema must be an integer, not {early_schema!r}"
            )
        if early_schema != SCHEMA_VERSION:
            raise TaskBoardError(
                "unsupported_schema",
                f"task schema {early_schema} is not supported (this store implements {SCHEMA_VERSION})",
            )
    missing = [key for key in _REQUIRED_FIELDS if key not in data]
    if missing:
        raise TaskBoardError(
            "invalid_task", f"task frontmatter is missing required keys: {', '.join(missing)}"
        )
    schema = data["schema"]
    if type(schema) is not int:
        raise TaskBoardError(
            "invalid_task", f"task schema must be an integer, not {schema!r}"
        )
    if schema != SCHEMA_VERSION:
        raise TaskBoardError(
            "unsupported_schema",
            f"task schema {schema} is not supported (this store implements {SCHEMA_VERSION})",
        )
    raw_id = data["id"]
    if not isinstance(raw_id, str) or not _ID_RE.fullmatch(raw_id):
        raise TaskBoardError("invalid_task", f"task id {raw_id!r} is not 32 lowercase hex")
    if raw_id != expected_id:
        raise TaskBoardError(
            "invalid_task",
            f"task id {raw_id!r} does not match its filename ({expected_id!r})",
        )
    title = _coerce_title(data["title"])
    status = data["status"]
    if status not in STATUSES:
        raise TaskBoardError("invalid_task", f"task status {status!r} is not one of {list(STATUSES)}")
    assignee = data["assignee"]
    if assignee not in ASSIGNEES:
        raise TaskBoardError(
            "invalid_task", f"task assignee {assignee!r} is not one of {list(ASSIGNEES)}"
        )
    created_at = _coerce_utc(data["created_at"], "created_at")
    updated_at = _coerce_utc(data["updated_at"], "updated_at")
    return TaskRecord(
        schema=SCHEMA_VERSION,
        id=raw_id,
        title=title,
        status=str(status),
        project_id=_coerce_optional_id(data.get("project_id"), "project_id"),
        due=_coerce_due(data.get("due")),
        assignee=str(assignee),
        created_at=created_at,
        updated_at=updated_at,
        chat_id=_coerce_optional_id(data.get("chat_id"), "chat_id"),
        attempt_id=_coerce_optional_id(data.get("attempt_id"), "attempt_id"),
    )


def _parse_document(raw: bytes, expected_id: str) -> tuple[TaskRecord, str]:
    """Parse *raw* into a record and its verbatim body text."""
    _check_id(expected_id)
    split = _split_document(raw)
    frontmatter = _frontmatter_text(split)
    _reject_ambiguous_yaml(frontmatter)
    _compose_mapping(frontmatter)
    try:
        loaded: Any = yaml.safe_load(frontmatter)
    except (yaml.YAMLError, ValueError) as exc:
        raise TaskBoardError("invalid_task", f"task frontmatter does not parse: {exc}") from None
    if loaded is None:
        raise TaskBoardError("invalid_task", "task frontmatter holds no mapping")
    record = _build_record(loaded, expected_id)
    return record, _body_text(split)


def parse_task(raw: bytes, *, expected_id: str) -> TaskDocument:
    """Parse one task file's bytes into a :class:`TaskDocument`.

    ``expected_id`` is the filename stem the frontmatter ``id`` must
    agree with. Raises :class:`TaskBoardError` with ``unsafe_path`` for
    a malformed id, ``invalid_task`` for any schema or shape problem,
    and ``unsupported_schema`` for a schema this store does not
    implement. Unknown frontmatter fields are preserved in ``raw`` and
    reported in the record as nothing — they are data, not instructions.
    """
    record, body = _parse_document(raw, expected_id)
    return TaskDocument(
        record=record,
        body=body,
        raw=raw,
        revision=_revision(raw),
        relative_path=f"Workspace/Tasks/{expected_id}.md",
    )


# ── YAML rendering ───────────────────────────────────────────────────


def _render_string(value: str) -> str:
    """Render a string as a YAML scalar that reads back identically.

    Plain when a plain scalar round-trips to the same string, double-quoted
    JSON otherwise (JSON strings are valid YAML flow scalars). Quoting is a
    rendering choice, never a normalization: the value is unchanged.
    """
    if value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _./+#-]*", value):
        try:
            if yaml.safe_load(value) == value:
                return value
        except (yaml.YAMLError, ValueError):
            pass
    return json.dumps(value, ensure_ascii=False)


def _render_field_value(key: str, record: TaskRecord) -> str:
    """The canonical YAML rendering of one owned record field."""
    value: Any = getattr(record, key)
    if key == "schema":
        return str(SCHEMA_VERSION)
    if value is None:
        return "null"
    if isinstance(value, datetime):
        return _render_string(value.astimezone(UTC).isoformat())
    return _render_string(str(value))


def render_new_task(record: TaskRecord, body: str) -> bytes:
    """Render a new task file from a record and a body.

    Canonical key order, LF endings, no BOM. The body is stored verbatim.
    Raises ``invalid_task`` when the result would exceed
    ``MAX_TASK_BYTES``.
    """
    if not isinstance(body, str):
        raise TaskBoardError("invalid_task", "task body must be a string")
    lines = ["---"]
    for key in _FIELD_ORDER:
        lines.append(f"{key}: {_render_field_value(key, record)}")
    lines.append("---")
    raw = ("\n".join(lines) + "\n" + body).encode("utf-8")
    if len(raw) > MAX_TASK_BYTES:
        raise TaskBoardError(
            "invalid_task",
            f"rendered task is {len(raw)} bytes, over the {MAX_TASK_BYTES} limit",
        )
    return raw


# ── Source-preserving patch ──────────────────────────────────────────


def _check_editable(
    changes: Mapping[str, object], *, allow_linkage: bool = False
) -> None:
    """Refuse non-editable and unknown patch fields explicitly.

    ``allow_linkage`` is set only by :meth:`TaskBoardStore.link` /
    :meth:`TaskBoardStore.unlink`, the delegation child's own writes. Linkage is
    not an ordinary user patch — an agent naming its own ``chat_id`` would be
    claiming a delegation it never made — but it is not immutable either, or the
    board could never be pointed at a turn. So it is a separate door rather than
    a field any caller may pass.
    """
    for key in changes:
        if key in _EDITABLE_FIELDS or key == "updated_at":
            continue
        if key in _EDITABLE_LINKAGE_FIELDS:
            if allow_linkage:
                continue
            raise TaskBoardError(
                "invalid_task",
                f"{key} is not an editable patch field (identity, creation time "
                "and linkage belong to the store and the delegation child)",
            )
        if key in ("schema", "id", "created_at"):
            raise TaskBoardError(
                "invalid_task",
                f"{key} is not an editable patch field (identity, creation time "
                "and linkage belong to the store and the delegation child)",
            )
        raise TaskBoardError("invalid_task", f"unknown task field {key!r}")


def _normalize_change(key: str, value: object) -> Union[str, datetime, None]:
    """Validate one patch value into its normalized form.

    Returns the ISO/string form for scalar fields, the datetime for
    ``updated_at``, or None for an explicit null.
    """
    if key == "title":
        return _coerce_title(value)
    if key == "status":
        if value not in STATUSES:
            raise TaskBoardError(
                "invalid_task", f"task status {value!r} is not one of {list(STATUSES)}"
            )
        return str(value)
    if key == "assignee":
        if value not in ASSIGNEES:
            raise TaskBoardError(
                "invalid_task", f"task assignee {value!r} is not one of {list(ASSIGNEES)}"
            )
        return str(value)
    if key in ("project_id",):
        return _coerce_optional_id(value, key)
    if key in _EDITABLE_LINKAGE_FIELDS:
        return _coerce_optional_id(value, key)
    if key == "due":
        return _coerce_due(value)
    if key == "updated_at":
        return _coerce_utc(value, "updated_at")
    raise TaskBoardError("invalid_task", f"unknown task field {key!r}")


def _render_change(key: str, value: Union[str, datetime, None]) -> str:
    """The YAML rendering of one normalized patch value."""
    if value is None:
        return "null"
    if isinstance(value, datetime):
        return _render_string(value.astimezone(UTC).isoformat())
    return _render_string(value)


def _current_normalized(key: str, record: TaskRecord) -> Union[str, datetime, None]:
    """The record's current value in the same normalized form as a change."""
    if key == "updated_at":
        return record.updated_at
    value: str | None = getattr(record, key)
    return value


def _change_is_noop(key: str, new: Union[str, datetime, None], record: TaskRecord) -> bool:
    """Whether a normalized change already equals the record's value."""
    current = _current_normalized(key, record)
    if isinstance(new, datetime) and isinstance(current, datetime):
        return new.astimezone(UTC) == current.astimezone(UTC)
    return new == current


def patch_task(
    document: TaskDocument, changes: Mapping[str, object], *, body: str | None = None
) -> bytes:
    """Apply owned-field edits to a task file without rewriting it.

    Only the value spans of the fields in *changes* are replaced (located
    with YAML node marks); unknown fields, comments, key order and every
    body byte survive untouched. A missing optional owned key is added
    just before the closing frontmatter delimiter. The body changes only
    when *body* is not None, and then it is replaced wholesale.

    Raises ``invalid_task`` — leaving the original bytes untouched — for
    unknown or non-editable fields, invalid values, and edits requiring
    an ambiguous rewrite (an owned field stored as a block scalar, a
    collection, or a multi-line scalar; invalid or truncated
    frontmatter). A parseable but unsupported edit shape is reported,
    never normalized silently.

    Linkage (``chat_id``/``attempt_id``) is refused here: it is written
    by the delegation child's own :meth:`TaskBoardStore.link` /
    :meth:`TaskBoardStore.unlink` and by nothing else.
    """
    return _patch(document, changes, body=body)


def _patch(
    document: TaskDocument,
    changes: Mapping[str, object],
    *,
    body: str | None = None,
    allow_linkage: bool = False,
) -> bytes:
    """:func:`patch_task`, with the linkage door opened only for the delegation
    child's two store methods."""
    if body is not None and not isinstance(body, str):
        raise TaskBoardError("invalid_task", "task body must be a string or None")
    _check_editable(changes, allow_linkage=allow_linkage)
    normalized: dict[str, Union[str, datetime, None]] = {
        key: _normalize_change(key, value) for key, value in changes.items()
    }
    # Drop edits that change nothing, so an idempotent call returns the
    # identical bytes rather than a requoted equivalent.
    pending = {
        key: value
        for key, value in normalized.items()
        if not _change_is_noop(key, value, document.record)
    }

    split = _split_document(document.raw)
    frontmatter = _frontmatter_text(split)
    _reject_ambiguous_yaml(frontmatter)
    root = _compose_mapping(frontmatter)
    values = _mapping_values(root)

    # Absolute (start, end) character spans in the BOM-stripped text, plus
    # insertions as zero-width spans. All spans address different lines of
    # the original document and never overlap.
    replacements: list[tuple[int, int, str]] = []
    insertions: list[tuple[int, str]] = []
    for key in sorted(pending):
        rendered = _render_change(key, pending[key])
        node = values.get(key)
        if node is None:
            if pending[key] is None:
                # Absent already means null for the optional fields.
                continue
            if key not in _OPTIONAL_NULLABLE_FIELDS and key != "updated_at":
                # Required fields are always present in a parsed document.
                raise TaskBoardError(
                    "invalid_task", f"cannot add missing required field {key!r}"
                )
            insertions.append((split.starts[split.close_index], f"{key}: {rendered}{split.newline}"))
            continue
        if node.start_mark.line != node.end_mark.line:
            raise TaskBoardError(
                "invalid_task",
                f"cannot patch {key}: stored as a multi-line scalar, which has "
                "no unambiguous single value span",
            )
        line_index = split.open_index + 1 + node.start_mark.line
        content, _ = split.lines[line_index]
        start_col = node.start_mark.column
        end_col = node.end_mark.column
        if not (0 <= start_col <= end_col <= len(content)):
            raise TaskBoardError(
                "invalid_task",
                f"cannot patch {key}: stored in a YAML shape with no plain value span",
            )
        absolute = split.starts[line_index]
        if start_col == end_col and start_col > 0 and content[start_col - 1] == ":":
            # An empty value (`due:`) has a zero-width span right after the
            # colon; splicing the rendering there would glue it to the key
            # (`due:"..."`). Prefix one space so the patch stays valid YAML.
            rendered = " " + rendered
        replacements.append((absolute + start_col, absolute + end_col, rendered))

    text = split.text
    for start, end, rendered in sorted(replacements, reverse=True):
        text = text[:start] + rendered + text[end:]
    # Insertions address the closing-delimiter line start in the *original*
    # text; replacements above only touched earlier value spans on other
    # lines, so no offset adjustment is needed between the two groups.
    # Both groups are applied to disjoint spans, verified below by reparse.
    if insertions:
        # Multiple insertions keep sorted-key order before the delimiter.
        block = "".join(rendered for _, rendered in sorted(insertions))
        at = insertions[0][0]
        # Recompute the delimiter offset after replacements: every
        # replacement sits on an earlier line, so shift by their delta.
        delta = len(text) - len(split.text)
        text = text[: at + delta] + block + text[at + delta :]
    if body is not None:
        after = split.close_index + 1
        if after >= len(split.lines):
            body_offset = len(split.text)
            delta = len(text) - len(split.text)
            prefix = split.newline if split.lines[split.close_index][1] == "" else ""
            text = text[: body_offset + delta] + prefix + body
        else:
            body_offset = split.starts[after]
            delta = len(text) - len(split.text)
            text = text[: body_offset + delta] + body
    if split.bom:
        text = "\ufeff" + text
    try:
        new_raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TaskBoardError("invalid_task", f"patched task is not encodable: {exc}") from None
    if len(new_raw) > MAX_TASK_BYTES:
        raise TaskBoardError(
            "invalid_task",
            f"patched task is {len(new_raw)} bytes, over the {MAX_TASK_BYTES} limit",
        )
    # Verify the splice: the patched bytes must parse, keep the identity,
    # and carry every requested value. A span bug surfaces here as an
    # explicit refusal, never as a silently corrupted file.
    record, _ = _parse_document(new_raw, document.record.id)
    for key in sorted(pending):
        want = pending[key]
        got = _current_normalized(key, record)
        if isinstance(want, datetime) and isinstance(got, datetime):
            if want.astimezone(UTC) != got.astimezone(UTC):
                raise TaskBoardError(
                    "invalid_task", f"patch verification failed for {key}: value did not take"
                )
        elif want != got:
            raise TaskBoardError(
                "invalid_task", f"patch verification failed for {key}: value did not take"
            )
    return new_raw


# ── The store ────────────────────────────────────────────────────────


@contextmanager
def _workspace_lock(workspace: str, runtime_dir: Path) -> Iterator[None]:
    """Serialize read-modify-write for one workspace.

    A process-wide :func:`keyed_lock` plus an advisory file lock in the
    runtime directory, so threads and processes running managed writers
    take turns. External editors honor neither; the revision recheck
    before each rename is what protects against those.
    """
    guard = keyed_lock(f"task-board:{workspace}")
    guard.acquire()
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", workspace) or "workspace"
        lock_path = runtime_dir / f"tasks-{safe}.lock"
        try:
            handle = lock_path.open("a+", encoding="utf-8", newline="")
        except OSError as exc:
            raise TaskBoardError(
                "read_failed", f"could not open task lock {lock_path}: {exc}"
            ) from None
        try:
            deadline = time.monotonic() + _LOCK_TIMEOUT_S
            while True:
                try:
                    lock_exclusive(handle.fileno(), blocking=False)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TaskBoardError(
                            "read_failed",
                            f"task lock {lock_path} is held; the write was not applied",
                        ) from None
                    time.sleep(0.05)
                except OSError as exc:
                    raise TaskBoardError(
                        "read_failed", f"could not lock {lock_path}: {exc}"
                    ) from None
            try:
                yield
            finally:
                try:
                    unlock(handle.fileno())
                except OSError:
                    pass
        finally:
            handle.close()
    finally:
        guard.release()


class TaskBoardStore:
    """Revision-safe file store for one workspace's task records.

    ``workspace`` is the logical workspace name (lock identity only).
    ``vault_root`` is that workspace's explicit vault root and
    ``runtime_dir`` the install's runtime directory for the advisory
    lock; both must come from authoritative workspace resolution in a
    later service, never from HTTP-supplied arbitrary roots. ``clock``
    supplies ``now`` for ``created_at``/``updated_at`` (timezone-aware
    UTC; naive values are assumed UTC).

    This child does not resolve or fall back across logical workspaces:
    a store reads exactly its own vault root's ``Workspace/Tasks``.
    """

    def __init__(
        self,
        *,
        workspace: str,
        vault_root: Path,
        runtime_dir: Path,
        clock: Callable[[], datetime],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError("workspace must be a non-empty string")
        self._workspace = workspace
        self._vault_root = Path(vault_root)
        self._runtime_dir = Path(runtime_dir)
        self._clock = clock
        #: Called after every write that landed — a replaced file or a removed
        #: one — so the engine can tell open clients the board moved. Never
        #: called for a refused or failed write.
        self._on_change = on_change

    # -- paths ------------------------------------------------------

    def _tasks_dir(self) -> Path:
        return self._vault_root / TASKS_RELATIVE

    def _task_path(self, task_id: str) -> Path:
        _check_id(task_id)
        return self._tasks_dir() / f"{task_id}.md"

    def _check_tasks_dir_links(self) -> None:
        """Refuse a linked ``Workspace`` or ``Tasks`` directory.

        Either link would let a read or write escape the explicit vault
        root, so any of them is ``unsafe_path``. Filesystem inspection
        failures are ``read_failed``.
        """
        try:
            if (self._vault_root / "Workspace").is_symlink():
                raise TaskBoardError(
                    "unsafe_path",
                    "workspace directory is a link; refusing to follow it",
                )
            if self._tasks_dir().is_symlink():
                raise TaskBoardError(
                    "unsafe_path",
                    "task directory is a link; refusing to follow it",
                )
        except TaskBoardError:
            raise
        except OSError as exc:
            raise TaskBoardError(
                "read_failed", f"could not inspect task directory: {exc}"
            ) from None

    def _read_file_bytes(self, path: Path, task_id: str) -> bytes:
        """Read one task file without following links.

        Refuses symlinks, non-regular files and oversized documents, and
        reports a missing file as ``not_found``. Link checks run before
        any byte is read.
        """
        self._check_tasks_dir_links()
        try:
            if path.is_symlink():
                raise TaskBoardError(
                    "unsafe_path", f"{path.name} is a link; a task read follows no link"
                )
        except OSError as exc:
            if isinstance(exc, TaskBoardError):
                raise
            raise TaskBoardError("read_failed", f"could not inspect {path.name}: {exc}") from None
        try:
            file_stat = path.stat()
        except FileNotFoundError:
            raise TaskBoardError("not_found", f"no such task: {task_id}") from None
        except OSError as exc:
            raise TaskBoardError("read_failed", f"could not stat {path.name}: {exc}") from None
        if not stat.S_ISREG(file_stat.st_mode):
            raise TaskBoardError("unsafe_path", f"{path.name} is not a regular file")
        if file_stat.st_size > MAX_TASK_BYTES:
            raise TaskBoardError(
                "invalid_task",
                f"{path.name} is {file_stat.st_size} bytes, over the {MAX_TASK_BYTES} limit",
            )
        try:
            fd = open_fd(path, os.O_RDONLY, follow_symlinks=False)
        except OSError as exc:
            import errno

            if exc.errno in (errno.ELOOP,):
                raise TaskBoardError(
                    "unsafe_path", f"{path.name} is a link; a task read follows no link"
                ) from None
            raise TaskBoardError("read_failed", f"could not open {path.name}: {exc}") from None
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(MAX_TASK_BYTES + 1)
        except OSError as exc:
            raise TaskBoardError("read_failed", f"could not read {path.name}: {exc}") from None
        if len(data) > MAX_TASK_BYTES:
            raise TaskBoardError(
                "invalid_task",
                f"{path.name} is over the {MAX_TASK_BYTES} byte limit",
            )
        return data

    # -- reads ------------------------------------------------------

    def get(self, task_id: str) -> TaskDocument:
        """Reread one task from source and return its document.

        Hand edits are reflected on the next call: there is no cache and
        no watcher. Raises ``unsafe_path`` for a malformed id,
        ``not_found`` when no file exists, and ``invalid_task`` /
        ``unsupported_schema`` for an unreadable one.
        """
        path = self._task_path(task_id)
        return parse_task(self._read_file_bytes(path, task_id), expected_id=task_id)

    def migrate_schema_1(self) -> list[tuple[str, str, str]]:
        """Rewrite every schema-1 task file in place to schema 2, once (#1069).

        ``on_hold`` becomes ``backlog`` (*To do*); a task flagged ``review_state:
        ready`` while ``in_progress`` becomes ``in_review``; the ``review_state``
        line is dropped and ``schema`` set to 2. Only those frontmatter lines
        change — everything else in the file is byte-for-byte what it was.

        A file that does not parse as schema 2 after the rewrite is left exactly
        as it was (it then lists as an unsupported-schema row, which says why),
        so a hand-edited shape this cannot read is never half-migrated.

        Returns ``(task_id, old_revision, new_revision)`` per rewritten file, so
        the delegation service can rebind an attempt bound to the old bytes.
        """
        tasks_dir = self._tasks_dir()
        try:
            if not tasks_dir.is_dir():
                return []
            names = sorted(entry.name for entry in tasks_dir.iterdir())
        except OSError:
            return []
        migrated: list[tuple[str, str, str]] = []
        for name in names:
            match = re.fullmatch(r"([0-9a-f]{32})\.md", name)
            if not match:
                continue
            task_id = match.group(1)
            path = tasks_dir / name
            with _workspace_lock(self._workspace, self._runtime_dir):
                try:
                    raw = self._read_file_bytes(path, task_id)
                except TaskBoardError:
                    continue
                upgraded = _upgrade_schema_1(raw)
                if upgraded is None:
                    continue
                try:
                    parse_task(upgraded, expected_id=task_id)
                except TaskBoardError:
                    continue
                self._atomic_write(path, upgraded, existing=path)
                migrated.append((task_id, _revision(raw), _revision(upgraded)))
        return migrated

    def list(self) -> TaskListResult:
        """List this workspace's tasks in board order.

        A missing task directory reads as empty and creates nothing. A
        directory traversal failure raises ``read_failed`` rather than
        returning a partial clean result; per-file problems become
        ``invalid`` entries so no file ever vanishes into an empty-board
        success. Valid tasks sort by ``(due is None, due, created_at,
        id)``: dated tasks first, undated last, ties deterministic. No
        second board, index or ranking file is read or written.
        """
        tasks_dir = self._tasks_dir()
        self._check_tasks_dir_links()
        try:
            exists = tasks_dir.exists()
        except OSError as exc:
            raise TaskBoardError(
                "read_failed", f"could not inspect task directory: {exc}"
            ) from None
        if not exists:
            return TaskListResult(tasks=(), invalid=())
        try:
            if not tasks_dir.is_dir():
                raise TaskBoardError("read_failed", "task directory is not a directory")
        except TaskBoardError:
            raise
        except OSError as exc:
            raise TaskBoardError(
                "read_failed", f"could not inspect task directory: {exc}"
            ) from None
        try:
            entries = sorted(tasks_dir.iterdir(), key=lambda entry: entry.name)
        except OSError as exc:
            raise TaskBoardError("read_failed", f"could not list task directory: {exc}") from None
        valid: list[TaskDocument] = []
        invalid: list[TaskInvalidEntry] = []

        def refuse(name: str, code: str, message: str) -> None:
            invalid.append(
                TaskInvalidEntry(
                    relative_path=f"Workspace/Tasks/{name}", code=code, message=message
                )
            )

        for entry in entries:
            name = entry.name
            if re.fullmatch(r"\.[0-9a-f]{32}\.md\..+\.tmp", name):
                # This store's own staged write; never a task or an error.
                continue
            try:
                if entry.is_symlink():
                    refuse(name, "unsafe_path", f"{name} is a link; refusing to follow it")
                    continue
            except OSError as exc:
                refuse(name, "read_failed", f"could not inspect {name}: {exc}")
                continue
            stem, suffix = (name[:-3], name[-3:]) if name.endswith(".md") else (name, "")
            if not suffix or not _ID_RE.fullmatch(stem):
                refuse(name, "invalid_task", f"{name} is not a task file (<32-hex>.md)")
                continue
            try:
                raw = self._read_file_bytes(entry, stem)
            except TaskBoardError as exc:
                refuse(name, exc.code, exc.message)
                continue
            except OSError as exc:
                refuse(name, "read_failed", f"could not read {name}: {exc}")
                continue
            try:
                valid.append(parse_task(raw, expected_id=stem))
            except TaskBoardError as exc:
                refuse(name, exc.code, exc.message)
        valid.sort(
            key=lambda document: (
                document.record.due is None,
                document.record.due or "",
                document.record.created_at,
                document.record.id,
            )
        )
        invalid.sort(key=lambda entry: entry.relative_path)
        return TaskListResult(tasks=tuple(valid), invalid=tuple(invalid))

    # -- writes -----------------------------------------------------

    def _now(self) -> datetime:
        moment = self._clock()
        if not isinstance(moment, datetime):
            raise TaskBoardError("invalid_task", "store clock must return a datetime")
        if moment.tzinfo is None:
            return moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC)

    def _atomic_write(self, target: Path, data: bytes, existing: Path | None) -> None:
        """Replace *target* with *data* via a unique sibling temp file.

        The temp file is unique per call so two concurrent writers cannot
        delete each other's temp on cleanup. An update carries the
        original file's mode across with :func:`carry_mode` before the
        rename; only this call's own temp is ever removed.
        """
        if len(data) > MAX_TASK_BYTES:
            raise TaskBoardError(
                "invalid_task",
                f"task write is {len(data)} bytes, over the {MAX_TASK_BYTES} limit",
            )
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
            )
        except OSError as exc:
            raise TaskBoardError("read_failed", f"could not stage task write: {exc}") from None
        tmp = Path(tmp_name)
        try:
            try:
                with os.fdopen(fd, "wb") as handle:
                    if existing is not None:
                        mode = stat.S_IMODE(existing.stat().st_mode)
                        carry_mode(handle.fileno(), mode, temp=tmp, original=existing)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                replace_file(tmp, target)
                self._fsync_dir(target.parent)
            except TaskBoardError:
                raise
            except OSError as exc:
                raise TaskBoardError(
                    "read_failed", f"task write failed and the prior file is unchanged: {exc}"
                ) from None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            handle = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(handle)
        except OSError:
            pass
        finally:
            os.close(handle)

    def create(
        self,
        *,
        title: str,
        body: str = "",
        project_id: str | None = None,
        due: str | date | None = None,
    ) -> TaskDocument:
        """Create one task with a fresh identity and return its document.

        Defaults: ``backlog`` status, ``user`` assignee, ``none`` review,
        null linkage. The caller supplies only the human fields; identity
        and timestamps come from the store.
        """
        clean_title = _coerce_title(title)
        clean_project = _coerce_optional_id(project_id, "project_id")
        clean_due = _coerce_due(due)
        if not isinstance(body, str):
            raise TaskBoardError("invalid_task", "task body must be a string")
        now = self._now()
        with _workspace_lock(self._workspace, self._runtime_dir):
            tasks_dir = self._tasks_dir()
            self._check_tasks_dir_links()
            try:
                tasks_dir.mkdir(parents=True, exist_ok=True)
            except TaskBoardError:
                raise
            except OSError as exc:
                raise TaskBoardError(
                    "read_failed", f"could not prepare task directory: {exc}"
                ) from None
            for _ in range(3):
                task_id = uuid.uuid4().hex
                target = tasks_dir / f"{task_id}.md"
                if target.exists():
                    continue
                record = TaskRecord(
                    schema=SCHEMA_VERSION,
                    id=task_id,
                    title=clean_title,
                    status="backlog",
                    project_id=clean_project,
                    due=clean_due,
                    assignee="user",
                    created_at=now,
                    updated_at=now,
                    chat_id=None,
                    attempt_id=None,
                )
                raw = render_new_task(record, body)
                parsed = parse_task(raw, expected_id=task_id)
                if (
                    parsed.record.title != clean_title
                    or parsed.record.project_id != clean_project
                    or parsed.record.due != clean_due
                    or parsed.body != body
                ):
                    raise TaskBoardError(
                        "invalid_task",
                        "rendered task did not read back the requested values",
                    )
                self._atomic_write(target, raw, existing=None)
                return parsed
            raise TaskBoardError("read_failed", "could not mint a fresh task id")

    def update(
        self,
        task_id: str,
        *,
        expected_revision: str,
        changes: Mapping[str, object],
        body: str | None = None,
        actor: Actor,
        resolution: str | None = None,
        attempt_id: str = "",
    ) -> TaskDocument:
        """Apply managed field edits (and optionally a body) to one task.

        The file is reread under the workspace lock and ``expected_revision``
        is rechecked immediately before the replacement: a stale revision
        raises ``revision_conflict`` and the prior bytes are unchanged, as
        they are on any parse or write failure. Title edits never rename
        the file.

        ``resolution`` is the user's completion text for this write, and
        ``attempt_id`` the attempt it closes when one is named. A managed
        transition into ``done`` appends one completion to the task's
        completion history (#1152) even when ``resolution`` is None or
        ``""``; replaying an already-``done`` task appends nothing. Passing a
        string ``resolution`` while the task is already ``done`` rewords the
        latest completion instead (its ``edited`` stamp moves, its
        ``completed_at`` does not). A non-string ``resolution`` or
        ``attempt_id`` is ``bad_request`` — values are never stringified.

        A supplied body carries the description only: any completion section
        in it is stripped on the way in and the stored section is appended
        back, so a description save never drops the history. The delegation
        log (#1064) is kept the same way when the body omits it. Reopening
        (``done`` to another status) keeps both sections.

        Managed-operation rules (they bind this API, not direct file
        editors):

        - An agent caller cannot set status ``done``
          (``completion_requires_user``). A user can complete a
          manual/non-live task.
        - A task linked to a live chat or attempt (non-null ``chat_id`` /
          ``attempt_id``) cannot be completed or reassigned here at all:
          that needs the later delegation service's stop/detach workflow.

        Live project membership validation is a later application-service
        obligation; this store keeps the string without judging it.
        """
        if actor not in ("user", "agent"):
            raise TaskBoardError("invalid_task", f"actor must be 'user' or 'agent', not {actor!r}")
        if body is not None and not isinstance(body, str):
            raise TaskBoardError("invalid_task", "task body must be a string or None")
        if resolution is not None and not isinstance(resolution, str):
            raise TaskBoardError(
                "bad_request",
                f"resolution must be a string or null, not {type(resolution).__name__}",
            )
        if attempt_id is None:
            attempt_id = ""
        if not isinstance(attempt_id, str):
            raise TaskBoardError(
                "bad_request",
                f"attempt_id must be a string, not {type(attempt_id).__name__}",
            )
        if not isinstance(changes, Mapping):
            raise TaskBoardError("invalid_task", "changes must be a mapping")
        path = self._task_path(task_id)
        expected = str(expected_revision or "").strip()
        if not expected:
            raise TaskBoardError(
                "invalid_task",
                "an expected revision is required; this store never overwrites a task it has not read",
            )
        with _workspace_lock(self._workspace, self._runtime_dir):
            current_raw = self._read_file_bytes(path, task_id)
            if _revision(current_raw) != expected:
                raise TaskBoardError(
                    "revision_conflict",
                    "the task changed since this edit was planned; nothing was written",
                )
            document = parse_task(current_raw, expected_id=task_id)
            # Only standalone delimiters open managed sections; a description
            # or an indented log report may mention the marker literally.
            if document.body.splitlines().count(COMPLETIONS_OPEN) > 1:
                raise TaskBoardError(
                    "invalid_task",
                    "task body has more than one completion history section; "
                    "refusing to guess which one the write should keep",
                )
            effective = self._plan_changes(document, dict(changes), actor)
            current_status = document.record.status
            patched_status = effective.get("status", current_status)
            working = body
            if working is not None and working != document.body:
                # A description save carries the description only. Strip any
                # completion section the caller sent — forged history on a
                # task with none is still forged — and append back only the
                # stored section, so the history survives the save byte for
                # byte and nothing unrecorded becomes history. A body with no
                # section passes through untouched.
                stripped = (
                    strip_completions(working)
                    if extract_section(working)
                    else working
                )
                # The delegation log is the engine's too: a description read
                # carries no log, so a save that omits it gets the stored one
                # back. A body that does carry a log (the log's own write) is
                # taken as given.
                kept = extract_section(document.body)
                kept_log = "" if extract_log(working) else extract_log(document.body)
                if kept or kept_log:
                    head = stripped.rstrip()
                    working = "\n\n".join(s for s in (head, kept, kept_log) if s) + "\n"
                else:
                    working = stripped
            stamp: datetime | None = None
            if current_status != "done" and patched_status == "done":
                if stamp is None:
                    stamp = self._now()
                completion = Completion(
                    id=uuid.uuid4().hex,
                    completed_at=stamp.replace(microsecond=0),
                    resolution=resolution or "",
                    attempt_id=attempt_id,
                )
                base = working if working is not None else document.body
                working = append_completion(base, completion)
            elif (
                current_status == "done"
                and patched_status == "done"
                and isinstance(resolution, str)
            ):
                base = working if working is not None else document.body
                latest = parse_completions(base)
                if latest:
                    if stamp is None:
                        stamp = self._now()
                    working = replace_resolution(
                        base, latest[0].id, resolution, stamp
                    )
            if working is not None and working == document.body:
                working = None
            body_unchanged = working is None
            if body_unchanged and all(
                _change_is_noop(key, _normalize_change(key, value), document.record)
                for key, value in effective.items()
            ):
                # No effective change other than a clock bump: return the
                # unchanged document without rewriting or advancing.
                return document
            effective["updated_at"] = stamp if stamp is not None else self._now()
            new_raw = patch_task(document, effective, body=working)
            if new_raw == current_raw:
                return document
            reread = self._read_file_bytes(path, task_id)
            if _revision(reread) != expected:
                raise TaskBoardError(
                    "revision_conflict",
                    "the task changed while the edit was being prepared; nothing was written",
                )
            self._atomic_write(path, new_raw, existing=path)
            return parse_task(new_raw, expected_id=task_id)

    # -- linkage (the delegation child, #1033) -------------------------
    #
    # `chat_id` and `attempt_id` are the one pair of fields an ordinary `update`
    # refuses: an agent naming its own chat would be claiming a delegation it
    # never made, and the linked-task rules in `_plan_changes` treat a non-null
    # linkage as "an attempt owns this task". So they get their own revision-
    # checked door below, called only by the delegation service, and each write
    # is one atomic replacement like every other managed write here.

    def link(
        self,
        task_id: str,
        *,
        expected_revision: str,
        chat_id: str,
        attempt_id: str,
        status: str = "in_progress",
        assignee: str = "agent",
    ) -> TaskDocument:
        """Point one task at the chat and attempt working on it, in one write.

        The revision the delegation service read is required and rechecked under
        the workspace lock, exactly as for :meth:`update`: the linkage is written
        before the turn starts, so a stale read must fail here rather than point
        a running attempt at a task somebody else has since changed.

        ``status``/``assignee`` default to handing the task over — ``in_progress``
        and ``agent`` — in the same atomic write, because a delegated task that
        stays in *To do* assigned to the user is not what a hand-over looks like.
        They are parameters so the service, not this method, owns the decision.

        Raises ``invalid_task`` for a malformed id, a missing chat or attempt id,
        a bad status/assignee, or an edit that would need an ambiguous rewrite;
        ``not_found``, ``revision_conflict`` and ``read_failed`` as
        :meth:`update` does.
        """
        if not str(chat_id or "").strip():
            raise TaskBoardError(
                "invalid_task", "linking a task requires the chat the attempt runs in"
            )
        if not str(attempt_id or "").strip():
            raise TaskBoardError(
                "invalid_task", "linking a task requires the attempt that owns it"
            )
        path = self._task_path(task_id)
        expected = str(expected_revision or "").strip()
        if not expected:
            raise TaskBoardError(
                "invalid_task",
                "an expected revision is required; this store never links a task it has not read",
            )
        changes: dict[str, object] = {
            "chat_id": str(chat_id).strip(),
            "attempt_id": str(attempt_id).strip(),
            "status": status,
            "assignee": assignee,
        }
        with _workspace_lock(self._workspace, self._runtime_dir):
            current_raw = self._read_file_bytes(path, task_id)
            if _revision(current_raw) != expected:
                raise TaskBoardError(
                    "revision_conflict",
                    "the task changed since this delegation was planned; nothing was written",
                )
            document = parse_task(current_raw, expected_id=task_id)
            normalized = {
                key: _normalize_change(key, value) for key, value in changes.items()
            }
            if all(
                _change_is_noop(key, value, document.record)
                for key, value in normalized.items()
            ):
                return document
            normalized["updated_at"] = self._now()
            new_raw = _patch(document, normalized, allow_linkage=True)
            if new_raw == current_raw:
                return document
            reread = self._read_file_bytes(path, task_id)
            if _revision(reread) != expected:
                raise TaskBoardError(
                    "revision_conflict",
                    "the task changed while the delegation was being prepared; nothing was written",
                )
            self._atomic_write(path, new_raw, existing=path)
            return parse_task(new_raw, expected_id=task_id)

    def unlink(self, task_id: str, *, expected_revision: str) -> TaskDocument:
        """Release a task from the chat and attempt that were working on it.

        The whole of what makes a delegated task completable again: while
        ``chat_id``/``attempt_id`` are non-null, :meth:`_plan_changes` refuses
        both completion and reassignment, because a task with a turn in flight is
        not one the user should be closing behind its back. Clearing the linkage
        is the gesture that says the attempt no longer speaks for the task — the
        attempt itself stays as history in the delegation child's own store.

        Same protocol as :meth:`update`: revision required, rechecked under the
        lock, nothing written on a conflict or a parse failure.
        """
        path = self._task_path(task_id)
        expected = str(expected_revision or "").strip()
        if not expected:
            raise TaskBoardError(
                "invalid_task",
                "an expected revision is required; this store never unlinks a task it has not read",
            )
        with _workspace_lock(self._workspace, self._runtime_dir):
            current_raw = self._read_file_bytes(path, task_id)
            if _revision(current_raw) != expected:
                raise TaskBoardError(
                    "revision_conflict",
                    "the task changed since this detach was planned; nothing was written",
                )
            document = parse_task(current_raw, expected_id=task_id)
            changes: dict[str, object] = {"chat_id": None, "attempt_id": None}
            if all(
                _change_is_noop(key, _normalize_change(key, value), document.record)
                for key, value in changes.items()
            ):
                return document
            changes["updated_at"] = self._now()
            new_raw = _patch(document, changes, allow_linkage=True)
            if new_raw == current_raw:
                return document
            reread = self._read_file_bytes(path, task_id)
            if _revision(reread) != expected:
                raise TaskBoardError(
                    "revision_conflict",
                    "the task changed while the detach was being prepared; nothing was written",
                )
            self._atomic_write(path, new_raw, existing=path)
            return parse_task(new_raw, expected_id=task_id)

    def delete(self, task_id: str, *, expected_revision: str) -> None:
        """Remove one task record, revision-checked.

        The same protocol as :meth:`update`, for the one write that is not a
        rewrite: ``expected_revision`` is required and rechecked under the
        workspace lock, so a delete planned against an older read is a
        ``revision_conflict`` with the record left in place. Confinement is
        inherited rather than re-derived: the id is validated before any path
        is built (:meth:`_task_path`) and the record is read through
        :meth:`_read_file_bytes` first, which refuses a link where the file
        must be and any non-regular file — so the unlink below can only ever
        remove a real task record inside this workspace's ``Tasks``
        directory, never a link target and never anything outside it.

        There is no trash: the record is the user's own Markdown file, and
        this unlinks it. Raises ``unsafe_path`` for a malformed id, a link or
        non-regular file; ``not_found`` when no file exists;
        ``invalid_task`` when no revision was presented; and ``read_failed``
        when the removal itself fails, with the record still there.
        """
        path = self._task_path(task_id)
        expected = str(expected_revision or "").strip()
        if not expected:
            raise TaskBoardError(
                "invalid_task",
                "an expected revision is required; this store never removes a task it has not read",
            )
        with _workspace_lock(self._workspace, self._runtime_dir):
            current_raw = self._read_file_bytes(path, task_id)
            if _revision(current_raw) != expected:
                raise TaskBoardError(
                    "revision_conflict",
                    "the task changed since this removal was planned; nothing was removed",
                )
            try:
                path.unlink()
            except FileNotFoundError:
                raise TaskBoardError("not_found", f"no such task: {task_id}") from None
            except OSError as exc:
                raise TaskBoardError(
                    "read_failed",
                    f"could not remove task {task_id}; the file is unchanged: {exc}",
                ) from None
            self._fsync_dir(path.parent)
        self._changed()

    def _plan_changes(
        self, document: TaskDocument, changes: dict[str, object], actor: Actor
    ) -> dict[str, object]:
        """Validate requested edits and apply the managed-operation rules."""
        _check_editable(changes)
        current = document.record
        # Validate value shapes first, so a bad enum is reported as such
        # even when a business rule would also refuse the edit.
        for key, value in changes.items():
            _normalize_change(key, value)
        linked = current.chat_id is not None or current.attempt_id is not None
        if changes.get("status") == "done":
            if actor == "agent":
                raise TaskBoardError(
                    "completion_requires_user",
                    "only the user can mark a task done through the managed API",
                )
            if linked:
                raise TaskBoardError(
                    "invalid_task",
                    "a task linked to a live chat or attempt cannot be completed "
                    "here; the delegation service's stop/detach workflow owns that",
                )
        if "assignee" in changes and linked and changes["assignee"] != current.assignee:
            raise TaskBoardError(
                "invalid_task",
                "a task linked to a live chat or attempt cannot be reassigned "
                "here; the delegation service's stop/detach workflow owns that",
            )
        return dict(changes)
