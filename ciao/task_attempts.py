"""Durable delegation attempts: one live attempt per board task (#1033).

Child B5 of #973, sitting on the task record store (``ciao/task_board.py``).
A task record says *what* is wanted; this module says *whether an agent is
working on it right now, in which chat, and how far it got*. The board's four
columns and this module's states are deliberately different vocabularies: a
column is a place a task sits, a state is the execution of one attempt, and a
finished turn is ``ready_for_review`` rather than ``done`` — completion is the
user's act, and the store refuses it (``completion_requires_user``).

Five rules, and each of them is a refusal somebody could otherwise have taken:

* **One live attempt per task.** :meth:`TaskAttemptStore.start` holds the store's
  workspace lock across the whole read-then-write, so a double click, two tabs
  and an agent/UI race all answer with the *same* attempt and the *same* chat.
  The caller is told which happened (:attr:`AttemptStart.created`), because "here
  is your attempt" and "I launched a turn for you" are different answers and only
  the second one may be followed by ``start_stream``.
* **Board state is not execution state.** ``running``, ``needs_you``,
  ``ready_for_review`` are :data:`LIVE_STATES` — the task is delegated and the
  linkage holds. ``failed``, ``interrupted`` and ``stopped`` are settled: the
  attempt is history, and it is what a retry or a resume reads.
* **A reviewed attempt is released, not rewritten.** The turn ending
  ``ready_for_review`` leaves the linkage in place on purpose: the result is
  waiting for the user's decision, and that decision is the user's gesture.
  Approving it or detaching it takes the task back without touching how the turn
  ended — :meth:`TaskAttemptStore.release` marks the record released, which is
  what takes it out of the live set (:attr:`TaskAttempt.is_live`) while the
  history row still reads ``ready_for_review``. The alternative — rewriting the
  state to say the release — would record "stopped" for a turn that finished, and
  a released attempt that still counted as live would hold a task nobody could
  ever delegate again.
* **Intent is durable before the side effect.** The attempt and its ``chat_id``
  are written before ``start_stream`` is called, so a crash in that window leaves
  a record naming a chat nobody ran. :meth:`recover_interrupted` (and every read,
  which derives the same answer) records it ``interrupted``: never replayed,
  because re-running a turn nobody confirmed is worse than asking.
* **A crash is derived, not remembered.** Every record carries the
  ``owner`` token of the process that wrote it. A live attempt owned by another
  process cannot have a turn running in this one, so it *is* ``interrupted`` —
  which is how a restart re-derives it without a startup sweep and without any
  state this module would have to keep in memory.
* **A finished turn is not a finished task.** Settling here writes
  ``ready_for_review`` and nothing else. Moving the card to Done is the user's
  gesture, through the same ``/complete`` route as any other task, and only
  after the linkage is released by stop/detach.

The file lives in the install's runtime directory beside the board's advisory
lock (``<runtime>/task-attempts-<workspace>.json``) and follows the same private,
locked, atomic discipline as ``ciao/webhooks.py``: owner-private, a unique
sibling temp, fsync, one atomic replace, and a reader that either sees the old
document or the new one. Records are keyed by ``attempt_id`` and a task's
history is bounded by :data:`MAX_ATTEMPTS_PER_TASK`, so a task nobody ever
completes cannot grow this file without limit.

Reads take no lock. They are atomic-replace reads plus a derivation that is a
pure function of the document, so a read can never queue behind a writer's
read-modify-write, and the derived state is persisted by the next mutation —
which is the same moment it would have been written anyway.
"""

from __future__ import annotations

import errno
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.async_reads import keyed_lock
from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import mkstemp_private
from ciao.task_log import attempt_label, strip_log

SCHEMA_VERSION = 1
"""The only attempt-document schema this store implements."""

ATTEMPT_STATES = (
    "running",
    "needs_you",
    "failed",
    "interrupted",
    "ready_for_review",
    "stopped",
)
"""Every state an attempt can hold, in no particular order.

``running`` and ``needs_you`` mean a turn is in flight (the second with an
approval card or a question waiting on the user). ``ready_for_review`` means the
provider turn ended and the result is waiting to be looked at — the board draws
it as a badge in *In progress*, never as ``done``. ``failed`` is a turn that ran
and ended in an error, ``interrupted`` one whose outcome is unknown, ``stopped``
one the user ended or detached.
"""

LIVE_STATES = frozenset({"running", "needs_you", "ready_for_review"})
"""States in which an attempt still owns its task's linkage.

:meth:`TaskAttemptStore.get_live` returns only these, and a ``start`` against a
task that has one returns it rather than minting a second. They are also exactly
the states a derived ``interrupted`` replaces, because a live attempt written by
another process cannot have a turn running in this one.

A *released* record is not live whatever state it is in — see
:attr:`TaskAttempt.released` and :attr:`TaskAttempt.is_live`, which is the whole
of what this module branches on rather than this set alone.
"""

SETTLED_STATES = frozenset(ATTEMPT_STATES) - LIVE_STATES
"""States that are history. A new attempt may be started against the task."""

RESUMABLE_STATES = frozenset({"failed", "interrupted", "stopped"})
"""Settled states a ``resume`` may continue in the same chat.

``ready_for_review`` is deliberately absent: a finished turn is waiting for the
user's review decision, and continuing it is the reviewer's call, not a retry of
an attempt that did not finish.
"""

MAX_ATTEMPTS_PER_TASK = 50
"""How many attempts one task's history keeps.

Old enough to answer "what did we try", small enough that a task retried forever
cannot grow the document without bound. The oldest are dropped, never a live one.
"""

MAX_DETAIL_CHARS = 400
"""Characters of outcome note one record keeps.

It is a sentence about the engine's own outcome ("the turn could not be started
(…)"), not something the user wrote, so it is bounded far tighter than the task
body it may quote.
"""

OUTCOMES = ("done", "blocked", "needs_input")
"""What the agent may report about its own attempt (#1064).

The agent's word about the work, not the engine's about the turn: ``done`` puts
the task in front of the user as *Agent says done* (still the user's gesture to
close), ``blocked`` and ``needs_input`` say the turn ended waiting on somebody.
A turn that ends with nothing reported is not taken as finished.
"""

MAX_SUMMARY_CHARS = 4000
"""Characters of the agent's own summary one record keeps.

What the agent did and what is left, in its words. Longer than an engine note
because it is the hand-off the next attempt reads, and bounded because the task
file's log quotes it.
"""

MAX_HISTORY = 500
"""Records one workspace's document keeps in total.

A board with thousands of delegated tasks would otherwise keep every attempt
forever. Oldest first out, and never a live one.
"""

_LOCK_TIMEOUT_S = 30.0

_TASK_ID_RE = re.compile(r"[0-9a-f]{32}")
"""A task id: 32 lowercase hex, the shape ``task_board`` mints."""

_REVISION_RE = re.compile(r"[0-9a-f]{64}")
"""A task revision: the SHA-256 hex of the record's exact bytes."""

_ATTEMPT_ID_RE = re.compile(r"[0-9a-f]{32}")
"""An attempt id: 32 lowercase hex, minted with :func:`uuid.uuid4`."""

#: This process's token. Stamped with every live attempt, and compared on read:
#: an attempt in a live state whose owner is not this token cannot have a turn
#: running here, so it is ``interrupted``. Process id alone would not do — a
#: recycled pid would let an old attempt look live — so the random half is what
#: makes the token unguessable by accident rather than merely unique per boot.
_PROCESS_TOKEN = f"{os.getpid()}-{uuid.uuid4().hex}"


class TaskAttemptError(Exception):
    """A typed attempt-store refusal.

    ``code`` is one of ``invalid_attempt``, ``not_found`` or ``read_failed``:

    - ``invalid_attempt``: a malformed id, a state this store does not hold, or a
      transition it refuses to manufacture.
    - ``not_found``: no attempt record for a well-formed id.
    - ``read_failed``: the document could not be read or written (I/O, lock,
      corrupt JSON, or a newer schema). Never a parse problem in a task record —
      that is ``ciao.task_board``'s error and its own code.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    """One delegation attempt, as this store understands it.

    ``task_revision`` is the revision of the task record the attempt was minted
    against, which is what makes "changed since delegated" answerable without
    re-reading history: a live attempt whose ``task_revision`` differs from the
    task's current revision means somebody edited the task after the agent was
    handed it.

    ``released`` is the marker that says the task no longer belongs to this
    attempt, set by :meth:`TaskAttemptStore.release` when the user approves a
    reviewed result or detaches the card. It is a marker and not a state on
    purpose: what the turn ended as is the record, and a release must not rewrite
    it — only say that nothing owns the task any more. :attr:`is_live` is what
    every read and write branches on, so a released ``ready_for_review`` attempt
    stops holding its task while its history row still reads as the review the
    user actually looked at.

    ``owner`` is the process token of whoever wrote the record; see
    :data:`_PROCESS_TOKEN`. It is bookkeeping for the crash derivation and is
    never shown to a user.
    """

    attempt_id: str
    task_id: str
    task_revision: str
    chat_id: str
    state: str
    created_at: str
    updated_at: str
    ended_at: str = ""
    detail: str = ""
    owner: str = ""
    released: bool = False
    #: The agent's own report for the turn in flight or the last one (#1064):
    #: one of :data:`OUTCOMES`, or ``""`` when nothing has been reported since the
    #: last turn started. Cleared when a turn starts; :attr:`summary` is kept so a
    #: card still says what the last report said.
    outcome: str = ""
    summary: str = ""

    @property
    def is_live(self) -> bool:
        """Whether this attempt still owns its task's linkage.

        The one question every read here asks, and it is *not* ``state in
        LIVE_STATES``: a released attempt does not own its task whatever it ended
        as, and answering "still live" for a ``ready_for_review`` record whose
        card has moved on is what would leave a task undelegatable forever.
        """
        return self.state in LIVE_STATES and not self.released

    def to_dict(self) -> dict[str, Any]:
        """The record as a transport payload, ``owner`` excluded.

        The owner token is a process fact, not a user fact: it says which boot
        wrote this row so a later boot can tell a crash from a running turn, and
        there is nothing a caller could do with it. ``live`` is :attr:`is_live`
        rather than a set membership, so a released attempt is reported the way
        every read sees it.
        """
        return {
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "chat_id": self.chat_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "detail": self.detail,
            "released": self.released,
            "live": self.is_live,
            "outcome": self.outcome,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class AttemptStart:
    """What one :meth:`TaskAttemptStore.start` did.

    ``created`` is the whole contract in one flag. False means a live attempt
    already existed and this call minted nothing — a double click, a second tab
    or a race with the agent all land here — and the caller must not create a
    chat or start a turn. True means this call minted the attempt, so exactly one
    turn may follow.
    """

    attempt: TaskAttempt
    created: bool


def _check_task_id(task_id: str) -> str:
    """A validated task id, before any path or lookup is built from it."""
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise TaskAttemptError(
            "invalid_attempt", f"not a task id (32 lowercase hex): {task_id!r}"
        )
    return task_id


def _check_revision(revision: str) -> str:
    """A validated task revision (the SHA-256 hex a task record hands out)."""
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise TaskAttemptError(
            "invalid_attempt", "a delegation attempt must name the task revision it read"
        )
    return revision


def _check_chat_id(chat_id: str) -> str:
    """A validated chat id: non-empty and short enough to be a chat's own id."""
    value = str(chat_id or "")
    if not value or len(value) > 128:
        raise TaskAttemptError(
            "invalid_attempt", f"a delegation attempt must name the chat it ran in: {chat_id!r}"
        )
    return value


def _check_state(state: str) -> str:
    """A validated state name, refused explicitly rather than defaulted."""
    if state not in ATTEMPT_STATES:
        raise TaskAttemptError(
            "invalid_attempt", f"state must be one of {list(ATTEMPT_STATES)}, not {state!r}"
        )
    return state


def _check_attempt_id(attempt_id: str) -> str:
    """A validated attempt id (32 lowercase hex), before it is used as a key.

    Same rule the decoder applies to a stored row's key, and for the same reason:
    an attempt id that is not one of these would be a key nothing else in this
    module can match, so the record it names could never be read back.
    """
    if not isinstance(attempt_id, str) or not _ATTEMPT_ID_RE.fullmatch(attempt_id):
        raise TaskAttemptError(
            "invalid_attempt",
            f"not an attempt id (32 lowercase hex): {attempt_id!r}",
        )
    return attempt_id


def _detail(value: str) -> str:
    """An outcome note, trimmed to :data:`MAX_DETAIL_CHARS`."""
    return str(value or "").strip()[:MAX_DETAIL_CHARS]


def _summary(value: str) -> str:
    """The agent's summary, trimmed to :data:`MAX_SUMMARY_CHARS`."""
    return str(value or "").strip()[:MAX_SUMMARY_CHARS]


def _check_outcome(outcome: str) -> str:
    """A validated outcome, refused explicitly rather than defaulted."""
    if outcome not in OUTCOMES:
        raise TaskAttemptError(
            "invalid_attempt", f"outcome must be one of {list(OUTCOMES)}, not {outcome!r}"
        )
    return outcome


@contextmanager
def _workspace_lock(workspace: str, runtime_dir: Path) -> Iterator[None]:
    """Serialize a read-modify-write for one workspace.

    A process-wide :func:`keyed_lock` plus an advisory file lock in the runtime
    directory, exactly as ``task_board`` takes its own: managed writers in this
    process and in another process take turns, and only this store's own files
    are ever locked. External editors are not stopped by either — the file is
    replaced atomically, so a reader never sees half a document.
    """
    guard = keyed_lock(f"task-attempts:{workspace}")
    guard.acquire()
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", workspace) or "workspace"
        lock_path = runtime_dir / f"task-attempts-{safe}.lock"
        try:
            handle = lock_path.open("a+", encoding="utf-8", newline="")
        except OSError as exc:
            raise TaskAttemptError(
                "read_failed", f"could not open task attempt lock {lock_path}: {exc}"
            ) from None
        try:
            deadline = time.monotonic() + _LOCK_TIMEOUT_S
            while True:
                try:
                    lock_exclusive(handle.fileno(), blocking=False)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TaskAttemptError(
                            "read_failed",
                            f"task attempt lock {lock_path} is held; the write was not applied",
                        ) from None
                    time.sleep(0.05)
                except OSError as exc:
                    raise TaskAttemptError(
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


class TaskAttemptStore:
    """One workspace's delegation attempts, in one private JSON document.

    ``workspace`` is the logical workspace name (lock and filename only),
    ``runtime_dir`` the install's runtime directory — where the advisory lock
    lives and where this store's file is written — and ``clock`` supplies the
    ``created_at``/``updated_at`` stamps (timezone-aware UTC; naive values are
    assumed UTC).

    There is no vault root here on purpose: an attempt is the *engine's*
    bookkeeping about a turn it launched, not the user's own Markdown, and it
    must be readable by the service that launched the turn without resolving a
    workspace's files.
    """

    def __init__(
        self,
        *,
        workspace: str,
        runtime_dir: Path,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError("workspace must be a non-empty string")
        self._workspace = workspace
        self._runtime_dir = Path(runtime_dir)
        self._clock = clock
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", workspace) or "workspace"
        self._path = self._runtime_dir / f"task-attempts-{safe}.json"

    @property
    def path(self) -> Path:
        """The document this store reads and writes."""
        return self._path

    # -- clock and stamps ---------------------------------------------

    def _now(self) -> str:
        moment = self._clock()
        if not isinstance(moment, datetime):
            raise TaskAttemptError("read_failed", "store clock must return a datetime")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()

    # -- reading and writing ------------------------------------------

    def _read(self) -> dict[str, TaskAttempt]:
        """Every record, newest derivation applied, or ``{}`` when unwritten.

        A missing file is a store that has never been written, and reading it
        creates nothing: a read must not leave a file behind that a later read
        then treats as state.

        :meth:`_derive` runs here rather than in the callers, so a stale
        ``running`` attempt reads ``interrupted`` from every entry point — the
        board's badge, the service's live check and a resume's precondition all
        see the same answer.
        """
        return self._derive(self._decode(self._read_text()))

    def _read_text(self) -> str:
        """The document's bytes as text, or ``""`` when it has never been written."""
        try:
            if self._path.is_dir():
                raise TaskAttemptError(
                    "read_failed", f"the task attempt store at {self._path.name} is a directory"
                )
            descriptor = open_fd(self._path, os.O_RDONLY, follow_symlinks=False)
        except FileNotFoundError:
            return ""
        except TaskAttemptError:
            raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise TaskAttemptError(
                    "read_failed",
                    f"refusing to read the task attempt store at {self._path.name}: it is a link",
                ) from None
            raise TaskAttemptError(
                "read_failed",
                f"the task attempt store at {self._path.name} cannot be read: {exc}",
            ) from None
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except (OSError, ValueError):
            # A fixed message, not `{exc}`: the UnicodeDecodeError this branch
            # usually catches renders the offending byte, and this is the one
            # refusal whose cause could quote the file's own contents back out.
            raise TaskAttemptError(
                "read_failed",
                f"the task attempt store at {self._path.name} is not valid UTF-8",
            ) from None

    def _read_raw(self) -> dict[str, TaskAttempt]:
        """The document exactly as stored, with no crash derivation applied.

        Only :meth:`recover_interrupted` needs this: it has to know which rows were
        *live on disk* before deriving them, and reading the derived view would
        answer a question about the derivation rather than about the file.
        """
        return self._decode(self._read_text())

    def _decode(self, raw: str) -> dict[str, TaskAttempt]:
        if not raw.strip():
            return {}
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise TaskAttemptError(
                "read_failed",
                f"the task attempt store at {self._path.name} is not valid JSON: {exc}",
            ) from None
        if not isinstance(document, dict):
            raise TaskAttemptError(
                "read_failed",
                f"the task attempt store at {self._path.name} is not a JSON object",
            )
        schema = document.get("schema")
        if schema != SCHEMA_VERSION:
            raise TaskAttemptError(
                "read_failed",
                f"the task attempt store is schema {schema!r}; this store implements "
                f"{SCHEMA_VERSION}",
            )
        entries = document.get("attempts")
        if not isinstance(entries, dict):
            raise TaskAttemptError(
                "read_failed",
                f"the task attempt store at {self._path.name} has no attempts mapping",
            )
        records: dict[str, TaskAttempt] = {}
        for attempt_id, entry in entries.items():
            record = _decode_entry(attempt_id, entry, path_name=self._path.name)
            records[record.attempt_id] = record
        return records

    def _write(self, records: dict[str, TaskAttempt]) -> None:
        """Replace the whole document atomically, owner-private.

        A unique sibling temp (``mkstemp_private``: 0600 on POSIX, a protected
        DACL on Windows), flushed and fsynced, then one ``replace_file``. A
        reader therefore sees the old document or the new one, never half of
        either. Only *this* call's temp is cleaned up, so another writer's temp
        is never deleted out from under its rename.

        The replaced file's mode is deliberately not carried over: this document
        names chats and turns, so a file left group- or world-readable by anything
        else is tightened to owner-only by the next write rather than preserved.
        """
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        document = {
            "schema": SCHEMA_VERSION,
            "attempts": {
                attempt_id: _encode(record)
                for attempt_id, record in sorted(records.items())
            },
        }
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        try:
            descriptor, temp_name = mkstemp_private(
                dir=self._runtime_dir, prefix=f".{self._path.name}.", suffix=".tmp"
            )
        except OSError as exc:
            raise TaskAttemptError(
                "read_failed", f"could not stage the task attempt write: {exc}"
            ) from None
        temporary = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(temporary, self._path)
        except OSError as exc:
            raise TaskAttemptError(
                "read_failed",
                f"the task attempt write failed and the prior document is unchanged: {exc}",
            ) from None
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _derive(self, records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
        """Every attempt owned by another process is ``interrupted``.

        A live attempt is a claim that a turn is running in the process that
        wrote it. There is one engine, so that claim is checkable rather than
        assumed: written by this process it is still running, written by any
        other it cannot be, and the honest state for a turn whose fate this
        process does not know is ``interrupted``.

        Derived in memory only. A read stays lock-free, and the derivation is
        written by the next mutation — which is the moment the record would have
        been rewritten anyway, and the derivation is idempotent, so a second
        restart derives the same answer rather than a new one.
        """
        derived: dict[str, TaskAttempt] = {}
        for attempt_id, record in records.items():
            if record.is_live and record.owner != _PROCESS_TOKEN:
                record = _interrupted(record)
            derived[attempt_id] = record
        return derived

    def _trim(self, records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
        """Apply the two history bounds, never dropping a live attempt.

        Both bounds drop the oldest first and skip anything live, so a board that
        is being delegated right now cannot lose the record of the turn in
        flight to keep a history small.
        """
        kept = dict(records)
        for task_id, count in _per_task_counts(kept).items():
            if count <= MAX_ATTEMPTS_PER_TASK:
                continue
            # Oldest first, so the rows dropped are the ones a retry has already
            # superseded rather than the attempt the user is looking at.
            for attempt in sorted(
                (record for record in kept.values() if record.task_id == task_id),
                key=_age_key,
            ):
                if count <= MAX_ATTEMPTS_PER_TASK:
                    break
                if attempt.is_live:
                    continue
                kept.pop(attempt.attempt_id, None)
                count -= 1
        if len(kept) > MAX_HISTORY:
            live = {
                record.attempt_id: record
                for record in kept.values()
                if record.is_live
            }
            settled = sorted(
                (record for record in kept.values() if not record.is_live),
                key=_age_key,
                reverse=True,
            )
            room = MAX_HISTORY - len(live)
            kept = {
                **live,
                **{record.attempt_id: record for record in settled[: max(room, 0)]},
            }
        return kept

    def _mutate(self, change: Callable[[dict[str, TaskAttempt]], dict[str, TaskAttempt]]) -> dict[str, TaskAttempt]:
        """One read-modify-write under both locks, then the write.

        *change* sees the derived records and returns the whole new set; the
        trimmed set is what lands on disk, so the bound can never be bypassed by
        a caller that forgets it.
        """
        with _workspace_lock(self._workspace, self._runtime_dir):
            records = change(self._read())
            records = self._trim(records)
            self._write(records)
            return records

    # -- reads --------------------------------------------------------

    def get(self, attempt_id: str) -> TaskAttempt:
        """One attempt, with the crash derivation applied."""
        clean = str(attempt_id or "").strip()
        records = self._read()
        record = records.get(clean)
        if record is None:
            raise TaskAttemptError(
                "not_found", f"no delegation attempt: {clean or '(empty)'}"
            )
        return record

    def get_live(self, task_id: str) -> TaskAttempt | None:
        """The live attempt on *task_id*, or ``None``.

        ``None`` is the ordinary answer for a task nobody delegated, and it is
        also the answer for one whose last attempt settled — a second delegation
        is a new attempt then, and the previous one stays as history. A
        *released* attempt answers it too: the user took the card back, so the
        task is delegatable again even though the attempt it delegated says
        ``ready_for_review``.
        """
        clean = _check_task_id(task_id)
        live = [
            record
            for record in self._read().values()
            if record.task_id == clean and record.is_live
        ]
        return _newest(live)

    def live_by_task(self) -> dict[str, TaskAttempt]:
        """Every live attempt keyed by task id, for one board render.

        A board reads this once rather than asking per row: the attempts are one
        document, so N per-row reads would be N reads of the same bytes, and a
        board is the surface that draws the most cards.
        """
        return self._by_task(live_only=True)

    def newest_by_task(self) -> dict[str, TaskAttempt]:
        """Every task's current attempt keyed by task id, live or settled.

        What a badge needs, and the difference from :meth:`live_by_task`: a settled
        attempt is not holding the task any more, but its state is still what the
        card has to say — ``failed``, ``stopped`` and ``interrupted`` are exactly
        the three a user needs to look at. A live attempt always wins over a
        settled one, so a task that is being worked on is never badged with an
        earlier attempt's failure.
        """
        return self._by_task(live_only=False)

    def _by_task(self, *, live_only: bool) -> dict[str, TaskAttempt]:
        grouped: dict[str, list[TaskAttempt]] = {}
        for record in self._read().values():
            if live_only and not record.is_live:
                continue
            grouped.setdefault(record.task_id, []).append(record)
        found: dict[str, TaskAttempt] = {}
        for task_id, rows in grouped.items():
            chosen = _newest(rows)
            if chosen is not None:
                found[task_id] = chosen
        return found

    def list_for_task(self, task_id: str) -> tuple[TaskAttempt, ...]:
        """Every attempt on *task_id*, newest first, live and settled alike.

        The history, not the live one: a retry is a new row and the row before it
        is the record of what was tried. Live attempts sort first so the first
        row is always the one a gesture would act on — and a released attempt is
        history, so it sorts with the rest rather than leading a card nobody owns
        any more.
        """
        clean = _check_task_id(task_id)
        rows = [record for record in self._read().values() if record.task_id == clean]
        # Two passes rather than one reversed key: a live attempt must lead even
        # when it is the *older* of the two, and a single `reverse=True` over
        # (settled, age) would put settled rows ahead of the running one.
        rows.sort(key=_age_key, reverse=True)
        rows.sort(key=lambda record: not record.is_live)
        return tuple(rows)

    def recover_interrupted(self) -> tuple[TaskAttempt, ...]:
        """Record every derived ``interrupted`` attempt on disk, and return them.

        The derivation in :meth:`_derive` already makes every read honest; this
        is the durable half, for a caller that wants the ambiguity settled on
        disk before it reads anything (a boot log, or the board's first read).
        Each row returned needed a person and none is replayed.
        """
        stranded = [
            record
            for record in self._read_raw().values()
            if record.is_live and record.owner != _PROCESS_TOKEN
        ]
        if not stranded:
            return ()
        self._mutate(lambda records: records)
        return tuple(_interrupted(record) for record in stranded)

    # -- writes -------------------------------------------------------

    def start(
        self,
        *,
        task_id: str,
        task_revision: str,
        chat_id: str,
        attempt_id: str = "",
        state: str = "running",
    ) -> AttemptStart:
        """Begin one attempt, or hand back the live one this task already has.

        The whole read-then-write runs under the workspace lock, so the "one live
        attempt per task" rule is enforced by the store rather than by every
        caller remembering it: a double click, two tabs and an agent/UI race all
        answer with the same attempt, and only the call that minted it is told
        ``created``.

        ``task_revision`` is the revision of the task record this attempt reads,
        which is what lets a later reader say "changed since delegated" without
        reconstructing history. ``chat_id`` is required: an attempt with no chat
        has nothing a ``resume`` could continue, so storing one would manufacture
        a record that no gesture can act on.

        ``attempt_id`` is the caller's own id, and it exists because the chat is
        created *before* this store writes: the provenance stamp on the chat names
        the attempt, so the id has to exist first rather than be learned here and
        patched in afterwards. Minted with :func:`uuid.uuid4` when the caller names
        none, which is the only caller that can. A call that loses the one-live-
        attempt race mints nothing and its proposed id is discarded with the rest
        of its write, exactly as the chat it made is left empty.
        """
        clean_task = _check_task_id(task_id)
        clean_revision = _check_revision(task_revision)
        clean_chat = _check_chat_id(chat_id)
        clean_state = _check_state(state)
        minted_id = _check_attempt_id(attempt_id) if str(attempt_id or "").strip() else uuid.uuid4().hex
        now = self._now()
        # Captured inside the mutation rather than inferred afterwards: "did this
        # call mint the attempt" is a fact about the write, and inferring it from
        # the returned document would be guessing at a store that just trimmed.
        minted: list[str] = []

        def change(records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
            live = _newest(
                [
                    record
                    for record in records.values()
                    if record.task_id == clean_task and record.is_live
                ]
            )
            if live is not None:
                return records
            attempt = TaskAttempt(
                attempt_id=minted_id,
                task_id=clean_task,
                task_revision=clean_revision,
                chat_id=clean_chat,
                state=clean_state,
                created_at=now,
                updated_at=now,
                owner=_PROCESS_TOKEN,
            )
            records[attempt.attempt_id] = attempt
            minted.append(attempt.attempt_id)
            return records

        self._mutate(change)
        if not minted:
            existing = _newest(
                [
                    record
                    for record in self._read().values()
                    if record.task_id == clean_task
                ]
            )
            if existing is None:  # pragma: no cover - only a lost lock could do this
                raise TaskAttemptError(
                    "read_failed", "the live attempt for this task disappeared under the lock"
                )
            return AttemptStart(attempt=existing, created=False)
        return AttemptStart(attempt=_require(self._read(), minted[0]), created=True)

    def bind_revision(self, attempt_id: str, task_revision: str) -> TaskAttempt:
        """Re-stamp the task revision one attempt is bound to.

        Delegation hands the attempt the revision it *read*, and then writes the
        linkage into the task file — which necessarily moves the revision, because
        the attempt id and chat id are now part of the record. Without this the
        attempt would be permanently "changed since delegated" the instant it was
        made, and the flag would mean nothing.

        So the delegation service rebinds the attempt to the revision the linkage
        write left behind. That is the honest binding: the attempt belongs to the
        task as it stands after the hand-off (title, body and description
        untouched — only status, assignee, linkage and ``updated_at`` changed), and
        any *later* edit moves the revision again, which is exactly what
        ``changed_since_delegated`` reports.
        """
        clean = _check_revision(task_revision)
        now = self._now()

        def change(records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
            record = _require(records, attempt_id)
            # Re-stamping a revision says nothing about ownership, so `released`
            # is carried (by `replace`): a released attempt rebinding would
            # otherwise be silently handed back to the live set.
            rebound = replace(record, task_revision=clean, updated_at=now)
            records[record.attempt_id] = rebound
            rebound_attempts.append(rebound)
            return records

        rebound_attempts: list[TaskAttempt] = []
        self._mutate(change)
        return rebound_attempts[0]

    def continue_turn(self, attempt_id: str) -> TaskAttempt:
        """Record a new turn on a still-live conversation, not a new attempt."""
        continued: list[TaskAttempt] = []

        def change(records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
            record = _require(records, attempt_id)
            if not record.is_live:
                raise TaskAttemptError("invalid_attempt", "only a live attempt can continue")
            # A new turn owes a new report: the last one described the turn
            # before. The summary stays, so the card keeps saying what it said.
            running = replace(
                record, state="running", updated_at=self._now(), ended_at="",
                detail="", owner=_PROCESS_TOKEN, outcome="",
            )
            records[record.attempt_id] = running
            continued.append(running)
            return records

        self._mutate(change)
        return continued[0]

    def reopen(self, attempt_id: str, *, detail: str = "") -> TaskAttempt:
        """Put one settled attempt back to ``running``, in its own chat.

        The one door through which a settled attempt becomes live again, and it
        exists because ``resume`` is a real gesture with a real meaning: the turn
        was cut off or failed, and continuing the *same* chat is the recovery.
        :meth:`start` cannot express it — it mints a new attempt, which is what
        ``retry`` means — and :meth:`update_state` refuses it by design, so a
        settled row never drifts back to running through a path nobody chose.

        Only a settled state in :data:`RESUMABLE_STATES` may be reopened. A
        ``ready_for_review`` result is not one: the turn finished, and continuing it
        is the reviewer's decision rather than a recovery from an attempt that did
        not finish.
        """
        now = self._now()
        note = _detail(detail) or "resumed"

        def change(records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
            record = _require(records, attempt_id)
            if record.state not in RESUMABLE_STATES:
                raise TaskAttemptError(
                    "invalid_attempt",
                    f"attempt {record.attempt_id} is {record.state!r}; only an attempt "
                    "that did not finish can be resumed",
                )
            reopened = replace(
                record,
                state="running",
                updated_at=now,
                ended_at="",
                detail=note,
                owner=_PROCESS_TOKEN,
                # Only a settled state gets here, and a released record is always a
                # live one, so this is the store's own invariant rather than a
                # guess: reopening is how an attempt holds its task again.
                released=False,
                outcome="",
            )
            records[record.attempt_id] = reopened
            reopened_attempts.append(reopened)
            return records

        reopened_attempts: list[TaskAttempt] = []
        self._mutate(change)
        return reopened_attempts[0]

    def update_state(self, attempt_id: str, state: str, *, detail: str = "") -> TaskAttempt:
        """Move one attempt to *state*, stamping ``updated_at``.

        Refuses a transition that would invent a record nobody observed: a
        settled attempt cannot go back to ``running`` (that is a new attempt, and
        :meth:`start` is how one is made), and a live attempt cannot jump straight
        to ``failed`` — a turn that ended is either stopped, failed, waiting on
        the user, or ready for review, and the caller says which.
        """
        clean_state = _check_state(state)
        note = _detail(detail)
        now = self._now()
        settled: list[TaskAttempt] = []

        def change(records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
            record = _require(records, attempt_id)
            moved = _with_state(record, clean_state, now, detail=note)
            records[record.attempt_id] = moved
            settled.append(moved)
            return records

        self._mutate(change)
        return settled[0]

    def finish(self, attempt_id: str, state: str, *, detail: str = "") -> TaskAttempt:
        """Settle one attempt, stamping ``ended_at`` as well as ``updated_at``.

        A distinct verb from :meth:`update_state` because the two write different
        facts. ``update_state`` records what the attempt is doing now; ``finish``
        records that it stopped doing anything, which is what ``ended_at`` means
        and what the board's badge needs to tell a settled attempt from a running
        one. ``state`` may be any state — a turn can settle as ``needs_you`` — but
        the row is closed either way.
        """
        clean_state = _check_state(state)
        note = _detail(detail)
        now = self._now()
        settled: list[TaskAttempt] = []

        def change(records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
            record = _require(records, attempt_id)
            moved = _with_state(record, clean_state, now, detail=note)
            closed = replace(moved, ended_at=moved.ended_at or now)
            records[record.attempt_id] = closed
            settled.append(closed)
            return records

        self._mutate(change)
        return settled[0]

    def release(self, attempt_id: str) -> TaskAttempt:
        """Take one live attempt out of the live set, without rewriting it.

        The release gesture: the user approved the result or took the card back,
        so the task no longer belongs to this attempt. Only the marker moves —
        ``state`` stays whatever the turn ended as, because that is the record of
        what happened and the release is a fact about the *task*, not about the
        turn. A released ``ready_for_review`` attempt therefore stays in history
        as the review it was while :attr:`TaskAttempt.is_live` answers False, so
        the task can be delegated again.

        The other door out of a live state is :meth:`finish`, and the difference
        is the whole point: finishing says the turn ended, releasing says it did
        not change. A released attempt cannot be reopened (only
        :data:`RESUMABLE_STATES` may be, and a reviewed result never is), so the
        marker cannot be a way back into a live state either.

        Refuses an attempt that was not holding its task: a settled one never was,
        and a release that finds nothing to release is a caller reading a stale
        attempt rather than a release anybody asked for.
        """
        now = self._now()
        freed: list[TaskAttempt] = []

        def change(records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
            record = _require(records, attempt_id)
            if not record.is_live:
                raise TaskAttemptError(
                    "invalid_attempt",
                    f"attempt {record.attempt_id} is {record.state!r}"
                    + (" and already released" if record.released else "")
                    + "; it does not hold its task, so there is nothing to release",
                )
            freed_attempt = replace(record, updated_at=now, released=True)
            records[record.attempt_id] = freed_attempt
            freed.append(freed_attempt)
            return records

        self._mutate(change)
        return freed[0]

    def report(self, attempt_id: str, outcome: str, summary: str) -> TaskAttempt:
        """Record the agent's own outcome and summary on one live attempt (#1064).

        Only a live attempt takes a report: a settled one is history, and a
        report against it would be rewriting what a finished turn said. A second
        report in the same turn replaces the first — the agent's latest word is
        the one the settle reads. Nothing here moves the state: the turn is still
        running, and how it settles is decided when it ends.
        """
        clean_outcome = _check_outcome(outcome)
        clean_summary = _summary(summary)
        if not clean_summary:
            raise TaskAttemptError(
                "invalid_attempt", "a report needs a summary of what was done and what is left"
            )
        now = self._now()
        reported: list[TaskAttempt] = []

        def change(records: dict[str, TaskAttempt]) -> dict[str, TaskAttempt]:
            record = _require(records, attempt_id)
            if not record.is_live:
                raise TaskAttemptError(
                    "invalid_attempt",
                    f"attempt {record.attempt_id} is {record.state!r} and no longer holds "
                    "its task; only the attempt working on it can report",
                )
            updated = replace(
                record, outcome=clean_outcome, summary=clean_summary, updated_at=now
            )
            records[record.attempt_id] = updated
            reported.append(updated)
            return records

        self._mutate(change)
        return reported[0]


# ── Record helpers ─────────────────────────────────────────────────────


def _encode(record: TaskAttempt) -> dict[str, Any]:
    return {
        "attempt_id": record.attempt_id,
        "task_id": record.task_id,
        "task_revision": record.task_revision,
        "chat_id": record.chat_id,
        "state": record.state,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "ended_at": record.ended_at,
        "detail": record.detail,
        "owner": record.owner,
        "released": record.released,
        "outcome": record.outcome,
        "summary": record.summary,
    }


def _decode_entry(attempt_id: Any, entry: Any, *, path_name: str) -> TaskAttempt:
    """One stored row, validated field by field.

    Every field is required and typed rather than defaulted: a half-valid row
    would put a live attempt on a board with no chat, or a ``running`` attempt
    with a revision nobody ever issued, and the store's whole job is that a
    recorded attempt means a turn really was handed over.
    """
    if not isinstance(entry, dict):
        raise TaskAttemptError(
            "read_failed", f"the task attempt store at {path_name} holds a non-object attempt"
        )
    if not isinstance(attempt_id, str) or not _ATTEMPT_ID_RE.fullmatch(attempt_id):
        raise TaskAttemptError(
            "read_failed",
            f"the task attempt store at {path_name} holds an attempt under a non-id key",
        )
    task_id = entry.get("task_id")
    task_revision = entry.get("task_revision")
    chat_id = entry.get("chat_id")
    state = entry.get("state")
    created_at = entry.get("created_at")
    updated_at = entry.get("updated_at")
    try:
        clean_task = _check_task_id(str(task_id))
        clean_revision = _check_revision(str(task_revision))
        clean_chat = _check_chat_id(str(chat_id))
        clean_state = _check_state(str(state))
    except TaskAttemptError as exc:
        raise TaskAttemptError(
            "read_failed", f"the task attempt store at {path_name} holds an invalid attempt: {exc}"
        ) from None
    for field, value in (("created_at", created_at), ("updated_at", updated_at)):
        if not isinstance(value, str) or not value:
            raise TaskAttemptError(
                "read_failed",
                f"the task attempt store at {path_name} holds an attempt with no {field}",
            )
    ended_at = entry.get("ended_at")
    detail = entry.get("detail")
    owner = entry.get("owner")
    # `released` is read as the literal `True` it is written as, so a row that
    # does not carry the marker is an attempt that was never released — which is
    # exactly what it says, rather than a guess at what a missing field meant.
    released = entry.get("released") is True
    # Absent on rows written before #1064, and a row with nothing reported is
    # exactly what an empty outcome says. An unknown outcome is dropped rather
    # than refused: it is the agent's word, not the attempt's identity.
    outcome = entry.get("outcome")
    summary = entry.get("summary")
    return TaskAttempt(
        attempt_id=str(attempt_id),
        task_id=clean_task,
        task_revision=clean_revision,
        chat_id=clean_chat,
        state=clean_state,
        created_at=str(created_at),
        updated_at=str(updated_at),
        ended_at=str(ended_at) if isinstance(ended_at, str) else "",
        detail=str(detail) if isinstance(detail, str) else "",
        owner=str(owner) if isinstance(owner, str) else "",
        released=released,
        outcome=outcome if outcome in OUTCOMES else "",
        summary=_summary(summary) if isinstance(summary, str) else "",
    )


def _interrupted(record: TaskAttempt) -> TaskAttempt:
    """One live record read as ``interrupted``.

    A pure function of the record, so the derivation the read path applies and the
    one :meth:`TaskAttemptStore.recover_interrupted` persists cannot disagree.
    ``owner`` is deliberately left as it was: this is still a record of what the
    process that died had claimed, and rewriting it would make a later read
    describe the derivation rather than the turn.
    """
    return replace(
        record,
        state="interrupted",
        ended_at=record.ended_at or record.updated_at,
        detail=record.detail or "the engine restarted while this attempt was running",
    )


def _with_state(
    record: TaskAttempt, state: str, now: str, *, detail: str
) -> TaskAttempt:
    """One record with its state moved, refusing a transition that lies."""
    if record.state in SETTLED_STATES and state in LIVE_STATES:
        raise TaskAttemptError(
            "invalid_attempt",
            f"attempt {record.attempt_id} already settled as {record.state!r}; a new "
            "attempt is a new start, not a state change on this one",
        )
    if record.state in LIVE_STATES and state == "failed" and record.state != "running":
        raise TaskAttemptError(
            "invalid_attempt",
            f"attempt {record.attempt_id} is {record.state!r}; a failed attempt is one "
            "that ran and ended in an error",
        )
    if state == record.state and detail == record.detail:
        return record
    # `released` is carried, never cleared: a state change is a fact about the
    # turn, and nothing here may hand a released attempt back to the live set.
    return replace(
        record,
        state=state,
        updated_at=now,
        detail=detail or record.detail,
        owner=_PROCESS_TOKEN if state in LIVE_STATES else record.owner,
    )


def _require(records: dict[str, TaskAttempt], attempt_id: Any) -> TaskAttempt:
    """The named record, or a typed ``not_found``."""
    clean = str(attempt_id or "").strip()
    record = records.get(clean)
    if record is None:
        raise TaskAttemptError(
            "not_found", f"no delegation attempt: {clean or '(empty)'}"
        )
    return record


def _newest(records: list[TaskAttempt]) -> TaskAttempt | None:
    """The newest of *records*, or ``None``. Holding the task outranks age."""
    if not records:
        return None
    return max(records, key=lambda record: (record.is_live, _age_key(record)))


def _age_key(record: TaskAttempt) -> tuple[str, str]:
    """One record's age, total and deterministic.

    ``created_at`` alone would not be: the store's clock is the operator's, and
    two attempts minted inside the same second are ordered by their id so the
    oldest is still decidable. Reversing this key therefore always means "newest
    first", which is the order both the bounds and :meth:`list_for_task` want.
    """
    return (record.created_at, record.attempt_id)


# ── The delegated turn ──────────────────────────────────────────────────
#
# The prompt and the provenance stamp are server-owned. There is no parameter
# through which a caller may supply prompt text: a task hand-edited into the vault
# is the task, and a delegation that let a request body rewrite the instruction
# would hand the agent work nobody filed. Both builders take the record's own
# fields and nothing else.


#: The fence the task's Markdown body is quoted inside, and the tag that closes
#: it. Fixed here rather than by the record, for the reason ``webhook_dispatch``
#: fixes its own: the framing is the engine's statement about what the text is,
#: and text able to write its own framing would be writing the instruction it is
#: about to be given.
DELEGATION_FENCE_OPEN = "<task-board-task>"
DELEGATION_FENCE_CLOSE = "</task-board-task>"

#: How the agent says how far it got (#1064). One command, named in full in
#: every prompt, because a turn that ends without it is read as unfinished.
REPORT_COMMAND = (
    "ciao task report {task_id} --outcome done|blocked|needs_input --summary-file <file.md>"
)

#: The instruction, in full. Explicit about the two things a finishing agent gets
#: wrong: the turn's end is not the report, and closing the task is the user's
#: gesture. ``{report}`` is :data:`REPORT_COMMAND` for this task.
DELEGATION_INSTRUCTION = (
    "You have been handed one task from this workspace's task board. Do the work "
    "the task describes.\n"
    "\n"
    "Before you end your turn, report how far you got, once, with:\n"
    "  {report}\n"
    "- `done`: the work is finished. The user reviews it and closes the task.\n"
    "- `blocked`: you cannot go on (missing access, a failing dependency, a "
    "decision that is not yours).\n"
    "- `needs_input`: you need an answer from the user before you can go on.\n"
    "The summary file is Markdown: what you did, where the results are, and what "
    "is left. It is saved on the task and is what the next attempt starts from. "
    "A turn that ends without a report is shown to the user as unfinished.\n"
    "\n"
    "Do not mark the task done, and do not try to: closing it is the user's "
    "decision. Ask for whatever approval you need in the ordinary way — an "
    "approval card raised here is answered in this chat like any other."
)

#: How many earlier attempts a new one is told about. The newest are the ones
#: that matter, and the log in the task keeps the rest.
HANDOFF_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class PreviousAttempt:
    """What a new attempt is told about one earlier attempt on the same task."""

    state: str
    outcome: str
    summary: str
    detail: str
    created_at: str
    ended_at: str
    chat_id: str
    chat_title: str
    archive_path: str


def _neutralize(text: str) -> str:
    """Escape the fence inside task prose so a body cannot close its own frame.

    Escaped rather than dropped: the text is the task, and deleting a span of it
    would make the record of what was handed over differ from what was run. A body
    carrying a literal ``</task-board-task>`` would otherwise close the fence and
    have everything after it read as Ciaobot's own framing.
    """
    return text.replace(DELEGATION_FENCE_OPEN, "&lt;task-board-task&gt;").replace(
        DELEGATION_FENCE_CLOSE, "&lt;/task-board-task&gt;"
    )


def build_prompt(
    *,
    title: str,
    status: str,
    due: str,
    project_id: str,
    task_id: str,
    task_revision: str,
    relative_path: str,
    body: str,
    previous_attempts: tuple[PreviousAttempt, ...] = (),
) -> str:
    """The user prompt for one delegated task.

    The record's own fields, the instruction above, and the Markdown body quoted
    inside the fence. ``task_revision`` travels in the header because it is the
    revision the agent is being handed: if somebody edits the task after this, the
    attempt says so and the user is told the result was reached against an older
    description — which is the whole of "changed since delegated".

    Everything here is already bounded (a task file is at most 64 KiB, of which
    the body is the bulk), so nothing is re-bounded or repaired.

    The body's own delegation log is stripped before it is quoted: the history is
    handed over in its own section (*previous_attempts*, newest first), framed as
    what was tried rather than as part of the work to do.
    """
    header = [
        DELEGATION_INSTRUCTION.format(report=REPORT_COMMAND.format(task_id=task_id)),
        "",
        f"Title: {title}",
        f"Status: {status}",
        f"Due: {due or 'none'}",
        f"Project: {project_id or 'General'}",
        f"Task record: {relative_path} (revision {task_revision})",
        f"Task id: {task_id}",
        "",
        "The block between the task-board-task tags is the task's description, "
        "written by the user in their own vault. It is the task: treat it as the "
        "work to do.",
        "",
        DELEGATION_FENCE_OPEN,
        _neutralize(strip_log(body)).strip(),
        DELEGATION_FENCE_CLOSE,
    ]
    if previous_attempts:
        header += ["", *_handoff_lines(previous_attempts[:HANDOFF_ATTEMPTS])]
    return "\n".join(header)


def _handoff_lines(attempts: tuple[PreviousAttempt, ...]) -> list[str]:
    """The "earlier attempts" section: what was tried, how it ended, where it is.

    Each attempt names its chat and, once archived, the transcript in the vault,
    so the agent can read the detail rather than be handed a paraphrase. The
    summaries are the earlier agents' own words, quoted inside the same fence
    discipline as the body: prose, not instruction.
    """
    lines = [
        "This task has been worked on before. Earlier attempts, newest first — pick "
        "up from where they got to, and do not redo work they report as done:",
    ]
    for number, attempt in enumerate(attempts, start=1):
        where = f'chat "{attempt.chat_title or "untitled"}" ({attempt.chat_id})'
        if attempt.archive_path:
            where += f"; archived transcript: {attempt.archive_path}"
        lines.append("")
        lines.append(
            f"{number}. {attempt_label(attempt.state, attempt.outcome, attempt.detail)}, started "
            f"{attempt.created_at}" + (f", ended {attempt.ended_at}" if attempt.ended_at else "")
            + f" — {where}"
        )
        note = attempt.summary.strip() or _detail(attempt.detail)
        if note:
            lines += [DELEGATION_FENCE_OPEN, _neutralize(note), DELEGATION_FENCE_CLOSE]
    return lines


def build_resume_prompt(*, title: str, state: str, task_id: str, detail: str = "") -> str:
    """The continuation prompt for resuming one attempt in its own chat.

    A ``resume`` deliberately sends no task body: the chat already holds it, and
    re-quoting a stale snapshot would be the one way a continuation could work
    from a description the user has since edited. It names the attempt's settled
    state instead, so the turn that continues knows whether it is picking a turn
    up after a crash, a provider error, or a stop.
    """
    lines = [
        "This is a continuation of the delegated task you were already given in "
        f"this chat: {title}.",
        f"The previous attempt ended as {state}"
        + (f" ({_detail(detail)})" if detail.strip() else "")
        + ".",
        "",
        "Read the task and this conversation, then carry on from where the last "
        "turn stopped. Do not repeat work it already completed, and do not mark "
        "the task done — the user reviews the result and closes it themselves.",
        "",
        "Before you end your turn, report how far you got with "
        + REPORT_COMMAND.format(task_id=task_id)
        + ". A turn that ends without a report is shown as unfinished.",
    ]
    return "\n".join(lines)


def task_delegation_helper(
    *, task_id: str, task_revision: str, attempt_id: str
) -> dict[str, str]:
    """The provenance a delegated chat carries, in the shape the store validates.

    This kind is what makes a delegated task's chat findable again after a
    reload, a second device or a restart: nothing else records which task and
    which attempt a chat belongs to, and the title is prose a user can retype.
    ``chat_service._normalize_chat_helper`` re-checks the shape on the way in and
    fails closed, so a half-valid stamp is dropped rather than stored — a chat the
    board cannot recognise is a chat the board cannot link back to.
    """
    return {
        "kind": "task_delegation",
        "task_id": task_id,
        "task_revision": task_revision,
        "attempt_id": attempt_id,
    }


def _per_task_counts(records: dict[str, TaskAttempt]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records.values():
        counts[record.task_id] = counts.get(record.task_id, 0) + 1
    return counts
