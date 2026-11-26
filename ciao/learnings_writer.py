"""The one writer of `Workspace/Learnings.md`.

Reusable lessons are recorded directly through :func:`append_learning` (`ciao memory learn`).
The record model, parser and renderer live in :mod:`ciao.learning_records`; this
module decides which entry a sighting belongs to, validates the path, and writes
the file atomically under the file lock.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ciao.learning_records import (
    LEARNINGS_RELATIVE,
    LEARNINGS_STUB,
    SECTION_ACTIVE,
    SECTION_HEADINGS,
    LearningObservation,
    LearningRecord,
    allocate_learning_id,
    learning_key,
    normalized_statement,
    observe_learning,
    parse_learnings,
    render_learning,
)

logger = logging.getLogger(__name__)


def _one_line(value: str) -> str:
    """Collapse every whitespace run (newlines included) into a single space."""
    return " ".join(value.split())


_ACTIVE_HEADING_RE = re.compile(
    rf"(?m)^#{{1,3}}[ \t]+{re.escape(SECTION_HEADINGS[SECTION_ACTIVE])}[ \t]*"
    r"(?P<eol>\r\n|\n|\Z)"
)
"""The ``## Active`` heading, *including* its line terminator.

Three things this has to get right, each of which was got wrong before.

**The terminator is part of the match.** Ending the pattern at ``\\r?$`` put
``match.end()`` on the ``\\n`` of a CRLF line, so inserting "one newline past
the heading" skipped the ``\\n`` *and* the first byte of the next line: the new
entry began with the existing entry's ``-`` and the owner's line lost its own
bullet. Consuming ``\\r\\n`` or ``\\n`` explicitly means ``match.end()`` is already
the offset to insert at, with no arithmetic to get wrong.

**The heading is matched whole.** The terminator alternation only matches where
the line actually ends, so ``## Active extras and notes`` is not this section and
does not get an entry filed into it. The ``[ \\t]*`` before it tolerates the
trailing whitespace a hand edit leaves behind, which would otherwise be a second
reason for the same heading not to be found.

**``\\Z`` is the unterminated case.** A heading on the final line with no newline
has nothing to insert after, and the entry has to be given a line of its own.
The terminator is a named group so the caller can tell the two apart by asking
the match, rather than by inspecting the two characters before its end — which
is a test that reads one way on an LF file and another on a CRLF one.

Not the literal ``\\n## Active\\n`` this replaced either: that never matched a
CRLF file, so a new entry was filed under a second ``## Active`` heading at the
end of the document. Matched at any level up to three because the model treats a
deeper heading as a group inside the section, and a file that has only ever used
``### Active`` still means the same thing. The heading text is the model's, so
the section the writer opens is the section the parser recognizes.
"""



_rename_at = os.rename
_LEARNING_DIR_FD_OPERATIONS = frozenset(os.supports_dir_fd)
_LEARNING_RENAME_HAS_DIR_FD = _rename_at in _LEARNING_DIR_FD_OPERATIONS


class LearningPathError(ValueError):
    """The canonical learnings file is not a regular file in this vault."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "learning_path_forbidden",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class _LearningWorkspace:
    root_fd: int
    workspace_fd: int


def validated_learnings_path(vault_root: Path) -> Path:
    """Return the learnings path after refusing symlinks and non-files."""
    try:
        root = vault_root.resolve()
    except (OSError, RuntimeError) as exc:
        raise LearningPathError("cannot resolve the vault root") from exc
    relative = Path(LEARNINGS_RELATIVE)
    workspace = root / relative.parent
    path = root / relative
    try:
        workspace_info = workspace.lstat()
    except FileNotFoundError:
        workspace_info = None
    except OSError as exc:
        raise LearningPathError("cannot inspect Workspace") from exc
    if workspace_info is not None and (
        stat.S_ISLNK(workspace_info.st_mode) or not stat.S_ISDIR(workspace_info.st_mode)
    ):
        raise LearningPathError("Workspace must be a directory inside the vault")
    try:
        file_info = path.lstat()
    except FileNotFoundError:
        file_info = None
    except OSError as exc:
        raise LearningPathError("cannot inspect Learnings.md") from exc
    if file_info is not None and (
        stat.S_ISLNK(file_info.st_mode) or not stat.S_ISREG(file_info.st_mode)
    ):
        raise LearningPathError("Learnings.md must be a regular file inside the vault")
    try:
        if not workspace.resolve().is_relative_to(root):
            raise LearningPathError("Workspace resolves outside the vault")
        if path.exists() and not path.resolve().is_relative_to(root):
            raise LearningPathError("Learnings.md resolves outside the vault")
    except (OSError, RuntimeError) as exc:
        raise LearningPathError("cannot resolve the learnings path") from exc
    return path


@contextmanager
def _learning_workspace_fd(
    vault_root: Path, *, create: bool
) -> Iterator[_LearningWorkspace | None]:
    """Open Workspace without following a swapped symlink; fail closed if unsupported."""
    required = (os.open, os.mkdir, os.stat, os.unlink)
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NONBLOCK")
        or not _LEARNING_RENAME_HAS_DIR_FD
        or any(operation not in _LEARNING_DIR_FD_OPERATIONS for operation in required)
    ):
        raise LearningPathError(
            "safe descriptor-relative learning writes are unavailable",
            code="learning_write_unavailable",
        )
    try:
        root = vault_root.resolve()
        root_fd = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except (OSError, RuntimeError) as exc:
        raise LearningPathError("cannot safely open the vault root") from exc
    workspace_fd = -1
    try:
        try:
            workspace_fd = os.open(
                "Workspace",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            if not create:
                yield None
                return
            try:
                os.mkdir("Workspace", dir_fd=root_fd)
            except FileExistsError:
                pass
            workspace_fd = os.open(
                "Workspace",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        yield _LearningWorkspace(root_fd=root_fd, workspace_fd=workspace_fd)
    except (OSError, RuntimeError) as exc:
        raise LearningPathError("Workspace changed or is not a real directory") from exc
    finally:
        if workspace_fd >= 0:
            os.close(workspace_fd)
        os.close(root_fd)


def _read_learning_file_fd(workspace: _LearningWorkspace) -> str:
    try:
        fd = os.open(
            "Learnings.md",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=workspace.workspace_fd,
        )
    except FileNotFoundError:
        return LEARNINGS_STUB
    except OSError as exc:
        raise LearningPathError("Learnings.md is not safely readable") from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise LearningPathError("Learnings.md must be a regular file")
            return handle.read().decode("utf-8")
    except OSError as exc:
        raise LearningPathError("Learnings.md is not safely readable") from exc


def read_learning_file(
    vault_root: Path,
    *,
    secure_destination: bool = False,
    workspace_handle: _LearningWorkspace | None = None,
) -> str:
    """Read Learnings.md, optionally using a no-follow Workspace descriptor."""
    if workspace_handle is not None:
        return _read_learning_file_fd(workspace_handle)
    if not secure_destination:
        path = learnings_path(vault_root)
        return path.read_bytes().decode("utf-8") if path.exists() else LEARNINGS_STUB
    with _learning_workspace_fd(vault_root, create=False) as workspace_fd:
        if workspace_fd is None:
            return LEARNINGS_STUB
        return _read_learning_file_fd(workspace_fd)


def _assert_learning_workspace_current(workspace: _LearningWorkspace) -> None:
    """Refuse to publish if the opened directory no longer occupies Workspace."""
    try:
        current = os.stat("Workspace", dir_fd=workspace.root_fd, follow_symlinks=False)
        opened = os.fstat(workspace.workspace_fd)
    except OSError as exc:
        raise LearningPathError(
            "Workspace changed during the learning write",
            code="learning_path_changed",
            retryable=True,
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise LearningPathError(
            "Workspace changed during the learning write",
            code="learning_path_changed",
            retryable=True,
        )


def _write_learning_for_memory_learn(workspace: _LearningWorkspace, text: str) -> None:
    """Atomically write via the same opened Workspace used for the read."""
    try:
        previous = os.stat(
            "Learnings.md", dir_fd=workspace.workspace_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        mode = 0o600
    except OSError as exc:
        raise LearningPathError("Learnings.md is not safely writable") from exc
    else:
        if not stat.S_ISREG(previous.st_mode):
            raise LearningPathError("Learnings.md must be a regular file")
        mode = stat.S_IMODE(previous.st_mode)

    for _ in range(10):
        temp_name = f".Learnings.md.{secrets.token_hex(12)}.write.tmp"
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=workspace.workspace_fd,
            )
        except FileExistsError:
            continue
        break
    else:
        raise LearningPathError("could not allocate a private learning temp file")

    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text.encode("utf-8"))
        _assert_learning_workspace_current(workspace)
        _rename_at(
            temp_name,
            "Learnings.md",
            src_dir_fd=workspace.workspace_fd,
            dst_dir_fd=workspace.workspace_fd,
        )
    except OSError as exc:
        try:
            os.unlink(temp_name, dir_fd=workspace.workspace_fd)
        except OSError:
            pass
        raise LearningPathError(
            "the learning could not be published; retry",
            code="learning_write_failed",
            retryable=True,
        ) from exc
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=workspace.workspace_fd)
        except OSError:
            pass
        raise


def learnings_path(vault_root: Path) -> Path:
    """Where a ``[learnings]`` accept writes.

    The path and the stub a first write starts from both come from
    :mod:`ciao.learning_records`: the parser, the writer and the migration
    command all have to agree on them, and a stub defined twice is two answers
    to "what does an empty ``Learnings.md`` say".
    """
    return vault_root / LEARNINGS_RELATIVE


def _new_learning(
    text: str, *, workspace: str, stamp: date, observation: LearningObservation
) -> LearningRecord:
    """The record for a learning filed for the first time.

    ``count=1`` and today's date are facts about *this write*, not guesses: the
    file is being created because something was observed now, and the writer
    witnessed it. That is the one place an `x1` is honest — a record *read* off
    an existing line never gets one, because nobody witnessed its history.

    The only field that could be invented is the source, and a source-less
    sighting is not recorded at all — `observe_learning` refuses one, so the line
    renders ``(x1)`` with no citation rather than a chat id nobody supplied. The
    identity is derived from the workspace and the statement, so a preview and
    the accept it precedes mint the same one.
    """
    return LearningRecord(
        learning_id=allocate_learning_id(workspace, text),
        key=learning_key(text),
        text=text,
        first_seen=stamp,
        last_seen=stamp,
        count=1,
        observations=(observation,) if observation.identity else (),
    )


def render_learning_append(
    existing: str,
    text: str,
    *,
    workspace: str,
    source: str = "",
    request: str = "",
    today: str = "",
) -> tuple[str, str]:
    """The file ``append_learning`` would write, and which operation that is.

    Returns ``(updated_text, operation)`` — ``"add"`` for a new Active entry,
    ``"update"`` when an existing entry records a sighting it did not have, and
    ``"none"`` when the observation is one the file already accounts for and
    nothing is written (``updated_text`` is then ``existing``).

    Split out of :func:`append_learning` so a caller can see the exact
    replacement *before* it is written. The write path goes through this same
    function, so a preview and the write it precedes cannot disagree about what
    lands.

    ``source`` and ``request`` are the two ways a sighting identifies where it
    was seen, and they are not interchangeable. ``source`` is a chat or archive:
    a stored transcript somebody can re-read. ``request`` is a user-request
    identifier, for the ``/remember`` that has no archived turn behind it —
    a lesson the user asked to be kept while they were typing it. The second
    exists because the alternative was manufacturing an archive, and a
    manufactured turn is a citation to a conversation that never happened.
    A ``/remember`` inside an archived chat may carry both; the citation shows
    the archive and the dedupe keys on the request, which is the narrower
    identity.

    Five cases, and the section an entry sits in decides the first two:

    * **Active, sighting not already recorded** — the observation is folded in
      and the entry is re-rendered in place, dates and count refreshed.
    * **Promoted / Resolved, sighting not already recorded** — the observation is
      recorded and the line re-rendered *where it already is*. The section does
      not move and the entry is not reactivated: a lesson that was promoted or
      resolved stays decided, and a later sighting of the same statement is
      evidence about a decision, not a reason to reopen it.
    * **Sighting that adds no evidence** — the file does not change, whatever
      the entry looks like. `observe_learning` returns the record untouched for
      two reasons: the source was already counted (a retry re-quoting an episode,
      which is the whole recurrence contract — it cannot inflate a count), or
      there was no source to count at all, which nothing can tell apart from a new
      sighting and is refused. This is also what leaves a legacy plain bullet
      alone: a line the owner wrote is not rewritten into a canonical one on the
      strength of a write that recorded nothing.
    * **Unreadable entry** — a line the parser could not read is not a learning
      to match against, and it is never rewritten. It is kept byte-exact, so a
      shape this code does not implement survives the write untouched rather than
      being guessed at.
    * **Not found** — a new Active entry is filed directly under the
      ``## Active`` heading, creating the section when the file has none. The
      heading is matched as a line and the inserted entry uses the file's own
      line ending, so a CRLF document does not gain a second ``## Active`` at the
      end of the file or a stray LF in a CRLF list.

    A legacy plain bullet *is* found: the model reads it as a record with no
    dates and no count, so a sighting of that statement with a source updates it
    in place and renders it canonically. What it never gets is an invented
    `x1` — a plain bullet has no recurrence history, and the count stays unknown
    until evidence accumulates.

    ``workspace`` scopes the identity a new entry is minted with, and is
    required rather than derived: the caller that renders a preview and the
    caller that performs the write both have the vault in hand, and a default
    here would be a way for the two to mint different identifiers for the same
    statement and disagree about what lands.
    """
    observation = LearningObservation(source=source, request=request)
    stamp = date.fromisoformat(today) if today else date.today()
    statement = _one_line(text)
    wanted = normalized_statement(statement)
    document = parse_learnings(existing, workspace=workspace)

    for entry in document.entries:
        record = entry.record
        if record is None or normalized_statement(record.text) != wanted:
            continue
        updated = observe_learning(record, observation, today=stamp)
        if updated == record:
            return existing, "none"
        rendered = render_learning(updated)
        return existing[: entry.start] + rendered + existing[entry.end :], "update"

    filed = render_learning(
        _new_learning(
            statement, workspace=workspace, stamp=stamp, observation=observation
        )
    )
    return _file_new_entry(existing, filed), "add"


def _newline(existing: str) -> str:
    """The line ending the document already uses.

    A CRLF file that gains ``\\n`` lines is a file this write has partially
    rewritten, which is the same defect as translating the whole thing: mixed
    endings render inconsistently, and every line ending after the insertion is
    now a byte the owner did not write. Read off the document's *first* line
    terminator, so a file whose endings already disagree keeps whatever its
    first line says rather than acquiring a rule from here.
    """
    first = existing.find("\n")
    return "\r\n" if first > 0 and existing[first - 1] == "\r" else "\n"


def _file_new_entry(existing: str, filed: str) -> str:
    """*existing* with *filed* placed directly under its ``## Active`` section.

    The heading's terminator is already consumed by the match, so the insertion
    point is the end of the match and nothing has to be added to it. A heading
    that consumed no terminator — it was the last line and the file does not end
    in a newline — gets one supplied, or the entry would share its line and
    ``## Active- [key] …`` would be neither a heading nor an entry.

    A file with no such heading has the section opened after its last line of
    content, which is where the writer has always put one — a stub that has
    never been written to needs the heading before the entry can be under it.
    """
    newline = _newline(existing)
    heading = _ACTIVE_HEADING_RE.search(existing)
    if heading is None:
        title = SECTION_HEADINGS[SECTION_ACTIVE]
        return f"{existing.rstrip()}{newline * 2}## {title}{newline * 2}{filed}{newline}"
    # `\Z` is zero-width, so an empty group means the heading consumed no
    # terminator: it was the file's last line, and one has to be supplied.
    consumed_eol = heading.group("eol") != ""
    at = heading.end()
    return f"{existing[:at]}{'' if consumed_eol else newline}{filed}{newline}{existing[at:]}"


def read_learnings(vault_root: Path) -> str:
    """The current Learnings file, or the stub a first write would start from.

    Existence, not a swallowed read error, decides: a file that is there but
    unreadable must surface rather than be silently replaced by the stub,
    which a following write would then persist over the real content.

    Decoded from bytes rather than with ``read_text``, which translates newlines:
    a CRLF file would come back all-LF, and the preview the review card shows
    would then be of a different document from the one
    :func:`append_learning` writes. The preview is only worth showing if it is
    what lands.
    """
    path = learnings_path(vault_root)
    if path.exists():
        return path.read_bytes().decode("utf-8")
    return LEARNINGS_STUB


def append_learning(
    vault_root: Path,
    text: str,
    *,
    workspace: str,
    source: str = "",
    request: str = "",
    _secure_destination: bool = False,
    _workspace_handle: _LearningWorkspace | None = None,
) -> bool:
    """File one learning under the Active section of Workspace/Learnings.md.

    Writes through the canonical model: :mod:`ciao.learning_records` parses the
    document, :func:`observe_learning` decides whether this sighting is new
    evidence or a replay of one already counted, and :func:`render_learning`
    writes the single line. The contract that follows from that is the one the
    Workspace care schedule prompt promotes on — a learning recurs because it was
    observed again, counted mechanically rather than judged from prose.

    The read, the render and the write all happen under
    :func:`ciao.file_kernel.queue_lock`, and the default write goes through
    :func:`ciao.file_kernel.write_queue_atomically`. Both are the same ones
    ``ciao learnings-migrate`` takes on this file, and both are load-bearing: the
    memory pass, the care run and the migration are writers of one file, and a read-check-write that is not
    serialized is a read-check-write that races. The atomic rename is what makes
    a crash mid-write leave the old file rather than a truncated one. And the
    read is from bytes, because a CRLF file read through a translating API comes
    back rewritten in every line the writer did not mean to touch — which also
    invalidates the migration's ``--revert`` receipt, whose spans address exact
    offsets.

    The control-plane ``memory_learn`` verb opts into a no-follow,
    descriptor-relative destination and unique temp file. The default keeps
    the existing writer behavior for other callers.

    ``request`` is the ``/remember`` provenance (see
    :func:`render_learning_append`): a user-request identifier for a sighting
    that has no archived turn behind it. It is passed straight into the
    :class:`~ciao.learning_records.LearningObservation`, so the line renders a
    ``req:`` citation rather than an invented chat id.

    ``workspace`` is the *registered* workspace name — the one the registry
    knows this vault by, not the vault directory's own name — and it is the
    scope a new entry's identity is minted under, so the id this write records
    is the one every later reader resolves it by.
    """
    from ciao.file_kernel import queue_lock, write_queue_atomically

    if _workspace_handle is not None:
        if not _secure_destination:
            raise ValueError("a Workspace descriptor requires secure_destination")
        return _append_learning_locked(
            vault_root,
            text,
            workspace=workspace,
            source=source,
            request=request,
            path=learnings_path(vault_root),
            secure_destination=True,
            workspace_handle=_workspace_handle,
            write_queue_atomically=write_queue_atomically,
        )

    path = (
        validated_learnings_path(vault_root)
        if _secure_destination
        else learnings_path(vault_root)
    )
    with queue_lock(path):
        if _secure_destination:
            path = validated_learnings_path(vault_root)
        if _secure_destination:
            with _learning_workspace_fd(vault_root, create=True) as workspace_fd:
                assert workspace_fd is not None
                return _append_learning_locked(
                    vault_root,
                    text,
                    workspace=workspace,
                    source=source,
                    request=request,
                    path=path,
                    secure_destination=True,
                    workspace_handle=workspace_fd,
                    write_queue_atomically=write_queue_atomically,
                )
        else:
            return _append_learning_locked(
                vault_root,
                text,
                workspace=workspace,
                source=source,
                request=request,
                path=path,
                secure_destination=False,
                workspace_handle=None,
                write_queue_atomically=write_queue_atomically,
            )


def _append_learning_locked(
    vault_root: Path,
    text: str,
    *,
    workspace: str,
    source: str,
    request: str,
    path: Path,
    secure_destination: bool,
    workspace_handle: _LearningWorkspace | None,
    write_queue_atomically: Callable[..., None],
) -> bool:
    """Render and write while the caller holds the queue lock."""
    existing = (
        read_learning_file(
            vault_root, secure_destination=True, workspace_handle=workspace_handle
        )
        if secure_destination
        else (path.read_bytes().decode("utf-8") if path.exists() else LEARNINGS_STUB)
    )
    updated, operation = render_learning_append(
        existing,
        text,
        source=source,
        request=request,
        workspace=workspace,
    )
    if operation == "none":
        return True
    if secure_destination:
        assert workspace_handle is not None
        _write_learning_for_memory_learn(workspace_handle, updated)
    else:
        write_queue_atomically(path, updated)
    return True
