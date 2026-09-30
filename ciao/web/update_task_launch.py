"""Turning an eligible "After this update" task into one chat, exactly once.

What this is
------------
``ciao/update_task_catalog.py`` owns the definitions, ``ciao/update_tasks.py``
(#756) owns the per-scope state and the applicability answer, and this module
owns the *launch*: the one place a task stops being a card and becomes a chat
with the packaged prompt already in it. The Home "discuss in chat" button
(``HousekeepingStrip.vue``) mints that chat in the browser and sends a prompt
string the client was holding; for an update task that cannot be how it works.
The instructions are packaged, pinned to a ``(task id, revision)`` pair, and the
chat has to be findable again after a reload, a second device or a restart. So
the binding lives on the record (``TaskState.chat_id``) and the prompt is read
here, on the server, from ``update_task_catalog.read_prompt``.

Two contracts, and everything else is bookkeeping
-------------------------------------------------
**The prompt is server-owned.** There is no parameter through which a caller may
supply prompt text, and adding one would be the bug this module exists to
prevent: a chat launched for "review the legacy rows" that is told to do
something else is worse than no chat, because the record says the task ran. What
travels with the chat instead is ``prompt_digest = sha256(prompt)[:16]``, so a
later reader can tell whether the instructions changed without shipping them
twice.

**Start is idempotent per ``(task id, revision)``.** A double click, two tabs, a
response lost in flight and an engine restart all arrive here as "start", and
all four must return the *same* chat. The check is the state file's own
``keyed_lock`` section, which is the same critical section the rest of
``update_tasks`` uses to read-and-replace a record: read the record, and if it
already names a chat that still exists, hand that chat back
(``resumed: true``) instead of creating a second one. Only a record with no live
chat creates anything, which is also what makes a deleted chat recoverable —
the record still names it, the chat is gone, so a fresh one is created and the
record is re-stamped.

A live chat is not the same as a *dispatched* prompt, so the record's lifecycle
decides what a resume does with it. A ``failed`` attempt names a chat the turn
never reached: the next start sends the prompt into that same chat, under the
same lock, and puts the record back to ``in_progress`` — no second chat, and
``resumed: true`` only ever paired with a prompt that actually went out. A
``dismissed`` record is an operator decision that a start explicitly reverses:
the same chat is kept and the record is written ``in_progress``, but nothing is
re-sent, because the chat a dismissal kept is a chat the prompt has already
been sent to and running twice is the outcome this module exists to prevent.
Every other lifecycle resumes as it is: nothing created, nothing sent.

That last claim is only sound because ``dismiss_task`` keeps ``failed`` out of a
dismissal. ``failed`` is the one lifecycle that means "this chat never got the
prompt", and a dismissal that carried its chat forward would throw the only
record of that away: the next start would find a live chat, send nothing into
it, write ``in_progress`` and report a task as running that nothing was ever
dispatched into — and a reopen of the same record would reach the same answer
through the branch below. So a dismissal of a ``failed`` record writes no chat
at all, and the next start mints a fresh one and dispatches into that.

Why a revision change cannot resume
----------------------------------
A record is keyed by ``"<id>@<revision>"`` and read at the revision the catalog
defines *now*, so a record written at revision 1 is not even visible to a
launch of revision 2. That is the whole answer to "an engine update must not
silently substitute a new task into a running chat": the new revision sees no
record, creates its own chat, and leaves the old chat and its record exactly as
the operator left them. A resume is therefore only ever a resume of the same
revision's own attempt, and there is no code path that hands a revision 2 launch
a revision 1 chat.

Ordering, and why it is that way
--------------------------------
The record is written *before* the turn is dispatched — as ``failed``, which is
the honest pre-dispatch value, not as ``in_progress`` — and the dispatch happens
while the lock is still held. So a process that dies between minting the chat
and writing the record cannot orphan it (there is no such window), and the one
that dies after the write leaves a record that says the prompt was never sent,
which the next start can act on. ``in_progress`` is written only once
``start_stream`` has returned, so a lifecycle in the file means what it says.

That leaves one window it cannot close: a process that dies between
``start_stream`` returning and the ``in_progress`` write leaves a ``failed``
record for a turn that may already be running, and the next start sends the
prompt again. The trade is deliberate — a chat with no record at all is an
orphan nothing points at, whereas a duplicate dispatch is a second turn an
operator can see and close — and it is the same one
``proposal_service.accept_skill_proposal`` makes for the same reason.

A write that *fails* where a process would have died leaves that same record,
and is not answered as a refusal. ``_write_record`` raises
``UpdateTaskStateError``, which is a ``ValueError``, and a route answers a
``ValueError`` with 409 — dropping the ``chat_id`` from a reply whose prompt is
already running, and inviting the retry that sends it a second time. So the
post-dispatch write is logged and the launch answers with the chat: the record
keeps saying ``failed`` (the state a crash there leaves, and one a later start
can still act on) while the operator holds the chat that is doing the work.

``start_stream`` creates an asyncio task, so this function is called from the
event loop (see the route) rather than from a worker thread.

What this path does not do
--------------------------
No model call, no detector, no completion check, no ``eval``, no shell and no
remote fetch. Applicability is a separate, TTL-cached answer
(``update_tasks.evaluate``) that a launch does not need and must not re-ask for:
a start is a decision the operator already made. The only files a launch reads
are packaged ones — the catalog and the prompt — plus the project list it needs a
host from.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ciao import update_tasks
from ciao.update_task_catalog import UpdateTask, load_catalog, read_prompt
from ciao.update_tasks import TaskState, UpdateTaskStateError

logger = logging.getLogger(__name__)

#: The helper kind a launched task's chat carries, and the one
#: ``chat_service._normalize_chat_helper`` recognises. It is what makes the chat
#: findable again after a restart: nothing else records which task a chat is
#: for, and the title is prose a user can retype.
UPDATE_TASK_KIND = "update_task"

#: The project a task's chat is hosted in. The one a workspace gets by default,
#: and the one whose name ``ProjectInfo.is_auto`` reports as auto — so the chat a
#: launch mints here sits beside the ones the browser's own housekeeping button
#: creates rather than in a project somebody made for a piece of work.
GENERAL_PROJECT_NAME = "General"

#: Hex characters kept from the prompt digest, the same width
#: ``update_tasks.FINGERPRINT_CHARS`` uses for an attempt fingerprint. Enough to
#: say "exactly these instructions", short enough to read in a state file.
PROMPT_DIGEST_CHARS = 16

#: The actor recorded with an attempt, when the caller does not name another.
DEFAULT_ACTOR = "operator"

#: An actor is a short token, not a sentence. It goes into a portable evidence
#: entry, and a state file travels between machines, so a caller cannot smuggle
#: a path in here: this is the shape that keeps ``_portable`` happy, and a value
#: outside it is refused rather than silently dropped (an attempt whose actor
#: went missing would be an attempt nobody could attribute).
_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class UpdateTaskLaunchError(RuntimeError):
    """A launch got as far as a chat and could not go further.

    Distinct from the ``ValueError`` a refusal raises because this one is
    *recoverable and already half-done*: the chat exists and the record names
    it as ``failed``, so the caller answers 500 with ``chat_id`` in the body and
    the operator can open that chat, and the next start re-sends the prompt into
    it. Only the dispatch failed (the model turn was never started), never the
    chat, and never the record.
    """

    def __init__(self, message: str, *, chat_id: str) -> None:
        super().__init__(message)
        self.chat_id = chat_id


# ── The task, the prompt, the helper ────────────────────────────────────────


def prompt_digest(prompt: str) -> str:
    """The digest that stands for a prompt without carrying it.

    Over the raw text as the packaged file holds it, so two revisions of a task
    whose prompt is unchanged share a digest and a reworded one does not.
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:PROMPT_DIGEST_CHARS]


def update_task_helper(task: UpdateTask, digest: str) -> dict[str, Any]:
    """The helper a launched chat carries, in the shape the store validates.

    Built here rather than read back from the chat store, because the launch is
    what knows the task identity; ``chat_service._normalize_chat_helper`` re-checks
    the shape on the way in and fails closed, and
    ``tests/test_update_task_launch.py`` pins these two against each other so a
    change to one cannot quietly stop being recognised by the other.
    """
    return {
        "kind": UPDATE_TASK_KIND,
        "task_id": task.id,
        "revision": task.revision,
        "scope": task.scope,
        "prompt_digest": digest,
    }


def resolve_task(task_id: str, *, installed_version: str) -> UpdateTask:
    """The packaged definition of ``task_id`` that this engine supports.

    Refuses an id the catalog does not define, and an id it defines only for a
    newer engine — the two are different problems and the caller is told which.
    Refusing rather than launching anything is the point of the version gate: a
    task whose detector or prompt this engine cannot run is not one this engine
    may start.
    """
    catalog = load_catalog()
    for task in catalog.eligible(installed_version):
        if task.id == task_id:
            return task
    known = catalog.by_id.get(task_id)
    if known is None:
        raise ValueError(
            f"unknown update task {task_id!r} on engine {installed_version}"
        )
    raise ValueError(
        f"update task {task_id!r} needs engine {known.since_version} or newer; "
        f"this install is {installed_version}"
    )


# ── Launch ───────────────────────────────────────────────────────────────────


def launch_task(
    task_id: str,
    *,
    config: Any,
    pcm: Any,
    workspace: str = "",
    installed_version: str,
    actor: str = DEFAULT_ACTOR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Start (or resume) the chat for one task, and return what it decided.

    ``resumed`` is the whole contract in one flag: false means this call created
    the chat, and true means the record already named a live chat, so this call
    created nothing — whether it dispatched the prompt into that chat (a
    ``failed`` attempt being retried) or only handed the chat back (any other
    resume). Both are successes, and a client that gets ``resumed: true`` must
    not claim to have created anything; ``created`` is the same fact.

    Raises ``ValueError`` when the task is refused (unknown, unsupported by this
    engine, no host workspace, no chat manager, a record that cannot be written
    before the turn starts) and :class:`UpdateTaskLaunchError` when the chat
    exists but the turn could not be dispatched. A record that cannot be written
    *after* the turn started is not a refusal and is not raised: the launch
    answers with the chat and the file keeps saying ``failed`` (see
    :func:`_write_after_dispatch`).
    """
    task = resolve_task(task_id, installed_version=installed_version)
    _require_host_workspace(task, workspace)
    if pcm is None:
        raise ValueError("the chat manager is not running")
    if not _ACTOR_RE.fullmatch(actor or ""):
        raise ValueError(
            f"actor {actor!r} is not a short token, so it cannot be recorded in a "
            "state file that travels between machines"
        )
    prompt = read_prompt(task)
    digest = prompt_digest(prompt)

    with update_tasks._record_lock(task, config, workspace) as (path, previous):
        live = _live_chat(pcm, previous)
        if previous is not None and live is not None:
            # The record already names this chat, so nothing is created — but
            # "a live chat" and "a dispatched prompt" are different facts, and
            # only the second one is what the task asked for.
            return _resumed(
                task,
                path,
                previous,
                live,
                pcm=pcm,
                prompt=prompt,
                digest=digest,
                actor=actor,
                now=now,
            )
        project_id = _general_project_id(pcm, workspace)
        chat = pcm.create_chat(
            project_id,
            title=task.title,
            helper=update_task_helper(task, digest),
        )
        # Written before the dispatch, and inside the same lock: a crash
        # between here and the dispatch leaves a record naming this chat, so the
        # next start sends the prompt into it rather than minting a second one —
        # and it is written ``failed`` rather than ``in_progress`` because that
        # is what it can honestly claim until ``start_stream`` comes back.
        update_tasks._write_record(
            path,
            _attempt(
                task,
                chat.chat_id,
                digest=digest,
                actor=actor,
                now=now,
                lifecycle="failed",
            ),
        )
        _dispatch(pcm, task, chat.chat_id, prompt)
        started = _write_after_dispatch(
            path,
            _attempt(task, chat.chat_id, digest=digest, actor=actor, now=now),
            task=task,
        )
    return _outcome(
        task,
        chat,
        digest=digest,
        resumed=False,
        state=started,
        project_id=project_id,
    )


def resume_task(
    task_id: str,
    *,
    config: Any,
    pcm: Any,
    workspace: str = "",
    installed_version: str,
) -> dict[str, Any]:
    """The chat this task's attempt is already in, without starting anything.

    The other half of :func:`launch_task` for a caller that wants to *open* the
    work rather than begin it: it creates nothing and dispatches nothing, so a
    task with no live chat is a refusal rather than a new chat. The record is
    read lock-free (``read_task_state`` does not take the lock, and the document
    is replaced atomically, so a reader sees a whole one).
    """
    task = resolve_task(task_id, installed_version=installed_version)
    if pcm is None:
        raise ValueError("the chat manager is not running")
    state = update_tasks.read_task_state(task, config=config, workspace=workspace)
    chat = _live_chat(pcm, state)
    if state is None or chat is None:
        raise ValueError(
            f"update task {task.id}@{task.revision} has no live chat to resume"
        )
    return _outcome(task, chat, digest=state.prompt_digest, resumed=True, state=state)


# ── Dismiss, reopen, and the check ──────────────────────────────────────────


def dismiss_task(
    task_id: str,
    *,
    config: Any,
    workspace: str = "",
    installed_version: str,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Decline this task at this revision, and return the record that stands.

    What ``update_tasks.record_dismissal`` writes, with the one thing it cannot
    decide for itself, which is why this takes the record lock and writes the
    record rather than delegating: a dismissal carries the chat forward, and
    carrying the *wrong* one loses the only record of whether the prompt ever
    went out.

    A ``failed`` record is that record. Its chat exists, is live, and is empty —
    the dispatch never reached it — and carrying it into the dismissal would
    leave the next start with a live chat to write ``in_progress`` over while
    sending nothing into it: a task reported as running that no prompt was ever
    dispatched into, with nothing left in the file to retry from. So a dismissal
    of a ``failed`` record writes no chat at all, and the next start mints a
    fresh one and dispatches the prompt into that, exactly once. A reopen of
    that record inherits the empty chat, so it cannot reintroduce the hole.

    Every other lifecycle is unchanged, and in particular a dismissal of a task
    that *was* launched keeps its chat: carrying a chat is how a reopen finds
    the work again, and it is only wrong when nothing was ever put in it.

    The read of what it replaces and the write that replaces it are one critical
    section, as they are in the recorder, so a chat id stamped concurrently is
    carried forward rather than lost, and a launch that took the lock first is
    seen for what it did.
    """
    task = resolve_task(task_id, installed_version=installed_version)
    with update_tasks._record_lock(task, config, workspace) as (path, previous):
        carried = update_tasks._carried(previous)
        if previous is not None and previous.lifecycle == "failed":
            carried["chat_id"] = ""
        evidence: dict[str, Any] = {}
        if reason.strip():
            evidence["dismiss_reason"] = reason.strip()
        state = TaskState(
            task_id=task.id,
            revision=task.revision,
            scope=task.scope,
            lifecycle="dismissed",
            updated_at=update_tasks._stamp(now),
            evidence=evidence,
            **carried,
        )
        update_tasks._write_record(path, state)
    return _state_outcome(task, state)


def reopen_task(
    task_id: str,
    *,
    config: Any,
    workspace: str = "",
    installed_version: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Undo a dismissal at this revision, and return the record that stands.

    A thin wrapper over ``update_tasks.reopen_task``. Only ``dismissed`` is
    reopened, so an offered or in-flight task comes back unchanged rather than
    being rewritten as ``offered`` — the operator's decision is the only thing
    this reverses.

    A reopened record carries whatever chat its dismissal carried, and
    :func:`dismiss_task` leaves a chat out of a dismissal whose prompt never
    went out, so reopening one of those offers the task with no chat: the next
    start takes the ordinary path and dispatches the packaged prompt into a
    fresh one. Reopening a ``failed`` record itself writes nothing at all (only
    a dismissal is reversed) and leaves the retry available.
    """
    task = resolve_task(task_id, installed_version=installed_version)
    state = update_tasks.reopen_task(
        task, config=config, workspace=workspace, now=now
    )
    return _state_outcome(task, state)


def record_check(
    task_id: str,
    *,
    config: Any,
    workspace: str = "",
    installed_version: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Run this task's registered completion check and record a verdict.

    A thin wrapper over ``update_tasks.record_completion``, returning ``None``
    when nothing was recorded — the check name has no implementation, it raised,
    it returned the wrong shape, or the postcondition does not hold. ``None`` is
    the honest answer and not an error: opening a chat is not completion, so a
    launch must never come through here to make a task look done.
    """
    task = resolve_task(task_id, installed_version=installed_version)
    state = update_tasks.record_completion(
        task, config=config, workspace=workspace, now=now
    )
    if state is None:
        return None
    return _state_outcome(task, state)


# ── Pieces ───────────────────────────────────────────────────────────────────


def _require_host_workspace(task: UpdateTask, workspace: str) -> None:
    """Refuse a launch that has not said where its chat is hosted.

    A chat lives in a project, a project lives in a workspace, so a launch with
    no workspace has no host — and guessing one is how an install-scoped task
    ends up in somebody's personal project without either of them choosing it.
    An install-scoped task's *state* needs no workspace (it lives in the runtime
    directory), so this is about the chat, and it is why the two scopes fail
    with different words.
    """
    if workspace.strip():
        return
    if task.scope == "install":
        raise ValueError(
            f"update task {task.id}@{task.revision} is install-scoped, so this "
            "install owns it — but its chat still has to be hosted, so name the "
            "workspace whose General project should hold it"
        )
    raise ValueError(
        f"update task {task.id}@{task.revision} is workspace-scoped: name the "
        "workspace it belongs to"
    )


def _general_project_id(pcm: Any, workspace: str) -> str:
    """The workspace's own General project, creating it if the install has none.

    The project the browser's housekeeping button also falls back to, so a chat
    launched by the API and one launched by the button land in the same place and
    neither creates a second home for the work. The lookup is by name rather than
    by ``is_auto``: that flag is also true for the CLI-compatibility project, and
    a task's chat has no business running there.
    """
    for project in pcm.list_projects(workspace):
        if project.name == GENERAL_PROJECT_NAME:
            return str(project.project_id)
    try:
        created = pcm.create_project(GENERAL_PROJECT_NAME, workspace)
    except Exception:  # noqa: BLE001 — reported as "nowhere to host the chat"
        logger.exception("Could not create a General project in %s", workspace)
        # Chained, not swallowed: the operator's refusal is the ValueError, and
        # the log line above is what says why the create failed.
        raise ValueError(
            f"the {workspace} workspace has no General project to host the chat"
        )
    return str(created.project_id)


def _live_chat(pcm: Any, state: TaskState | None) -> Any | None:
    """The chat a record names, if it exists and is not archived; else ``None``.

    ``None`` is the recoverable case, not a failure: the operator deleted (or
    archived) the chat the task was in, so the record is stale and the next start
    creates a fresh one. A lookup that raises is answered the same way, because a
    chat store that cannot be asked must not be the reason a task cannot be
    started.
    """
    if state is None or not state.chat_id:
        return None
    try:
        chat = pcm.get_chat(state.chat_id)
    except Exception:  # noqa: BLE001 — a lookup must not fail the launch
        logger.exception("Could not read chat %s for an update-task launch", state.chat_id)
        return None
    if chat is None or getattr(chat, "archived", False):
        return None
    return chat


def _resumed(
    task: UpdateTask,
    path: Path,
    previous: TaskState,
    chat: Any,
    *,
    pcm: Any,
    prompt: str,
    digest: str,
    actor: str,
    now: datetime | None,
) -> dict[str, Any]:
    """The record already names a live chat: hand it back, and keep it honest.

    Three answers, because a live chat says nothing on its own about whether the
    prompt ever reached it, and the record is the only thing here that does:

    * **``failed``** — the attempt never dispatched, so the prompt goes into this
      same chat now, under the lock, and the record becomes ``in_progress``. No
      second chat, and the reply's ``resumed: true`` is backed by a prompt that
      actually left. A retry that fails again is the same recoverable answer as
      the first attempt, so the record stays ``failed`` and
      :class:`UpdateTaskLaunchError` names the same chat.
    * **``dismissed``** — an operator pressed Start on a task they had declined,
      which is a reopen, so the record is written ``in_progress`` and the chat
      kept. Nothing is dispatched, and the reason it can be sure of that is
      :func:`dismiss_task`: a dismissal only carries a chat whose prompt was
      sent, so a chat reached here has already run the task and must not be
      told to run it again. The rejected ``ValueError`` alternative was
      available and was not taken — a 409 that tells the operator to press
      Reopen, when Reopen already exists and does less, turns an explicit
      decision into a detour.
    * **anything else** — nothing is created and nothing is sent: the turn
      belongs to the attempt this record already describes, and sending the
      prompt twice would run the same task in one chat twice. The digest
      reported is the record's own, not the one this call just computed, so a
      record that carries none says so rather than borrowing the current
      prompt's answer.

    Both writes are of a record whose lifecycle this call has just changed, so
    both report the digest that was written rather than the one that was found.
    """
    if previous.lifecycle == "failed":
        _dispatch(pcm, task, chat.chat_id, prompt)
        retried = _write_after_dispatch(
            path,
            _attempt(task, previous.chat_id, digest=digest, actor=actor, now=now),
            task=task,
        )
        return _outcome(task, chat, digest=digest, resumed=True, state=retried)
    if previous.lifecycle == "dismissed":
        reopened = _attempt(task, previous.chat_id, digest=digest, actor=actor, now=now)
        update_tasks._write_record(path, reopened)
        return _outcome(task, chat, digest=digest, resumed=True, state=reopened)
    return _outcome(
        task, chat, digest=previous.prompt_digest, resumed=True, state=previous
    )


def _write_after_dispatch(
    path: Path, state: TaskState, *, task: UpdateTask
) -> TaskState:
    """Record the lifecycle a dispatched prompt earned, and return it either way.

    The only write here that happens once the turn is already running, so it is
    the one whose failure must not become a refusal. ``_write_record`` raises
    ``UpdateTaskStateError`` — a ``ValueError``, so a route answers it 409 — and
    by then the operator is holding a chat with the packaged prompt in it. A 409
    carries no ``chat_id`` and reads as "not started", which is the answer that
    sends them back to press Start and dispatch the same task twice.

    So the failure is logged and the record this call built is returned: the
    reply says what happened to the chat, and the file keeps saying ``failed``,
    which is the state a crash in the same window leaves and one a later start
    can still act on. That is the trade the ordering above already makes, taken
    deliberately rather than by dying into it.
    """
    try:
        update_tasks._write_record(path, state)
    except (UpdateTaskStateError, OSError):
        logger.exception(
            "The prompt for %s@%s is running, but its record could not be written",
            task.id,
            task.revision,
        )
    return state


def _dispatch(pcm: Any, task: UpdateTask, chat_id: str, prompt: str) -> None:
    """Send the packaged prompt into a chat, and turn a failure into an answer.

    The record is left as the caller wrote it — ``failed``, in both the create
    path and the retry path — so a dispatch that raises leaves a record the next
    start can act on, and the operator gets a chat id they can open and send into
    by hand. One failed dispatch is not a crash: it is the recoverable half this
    function exists to keep recoverable.
    """
    try:
        pcm.start_stream(chat_id, prompt)
    except Exception as exc:  # noqa: BLE001 — one failed dispatch is not a crash
        logger.exception(
            "Failed to start the update-task turn for %s@%s", task.id, task.revision
        )
        raise UpdateTaskLaunchError(
            f"could not start the update-task chat: {exc}", chat_id=chat_id
        ) from exc


def _attempt(
    task: UpdateTask,
    chat_id: str,
    *,
    digest: str,
    actor: str,
    now: datetime | None,
    lifecycle: str = "in_progress",
) -> TaskState:
    """The record a launch writes: this attempt's own identity, whole.

    ``attempted_fingerprint`` is stamped against the *prompt* rather than
    against an applicability answer, and that is deliberate: the launch path
    never asked for one (that is ``update_tasks.evaluate``, TTL-cached and
    separate), and a fingerprint over a status this call did not compute would be
    a claim about the workspace made by a function that never looked. What the
    attempt is against is the packaged instructions at this revision, so that is
    what it digests.

    Nothing is carried from the record this replaces. A dismissal keeps the chat
    it was in and a completion keeps the fingerprint of the attempt before it,
    but a *launch* is a new attempt: its fingerprint is this one's, its chat is
    the one it just created — or the one an earlier attempt created and a retry
    is now sending the prompt into — and its evidence says who started it.

    ``lifecycle`` is passed rather than inferred so the pre-dispatch write can
    say ``failed``: the moment between minting a chat and its turn starting is a
    moment the record has to have an honest word for, and ``failed`` is the word
    that means "no prompt in this chat yet".
    """
    return TaskState(
        task_id=task.id,
        revision=task.revision,
        scope=task.scope,
        lifecycle=lifecycle,
        updated_at=update_tasks._stamp(now),
        attempted_fingerprint=update_tasks.fingerprint(
            task, "launched", {"prompt_digest": digest, "scope": task.scope}
        ),
        evidence={"actor": actor},
        chat_id=chat_id,
        prompt_digest=digest,
    )


def _outcome(
    task: UpdateTask,
    chat: Any,
    *,
    digest: str,
    resumed: bool,
    state: TaskState,
    project_id: str = "",
) -> dict[str, Any]:
    """The launch reply, in the shape the routes hand straight to a client."""
    return {
        "task_id": task.id,
        "revision": task.revision,
        "scope": task.scope,
        "title": task.title,
        "chat_id": chat.chat_id,
        "project_id": project_id or chat.project_id,
        "resumed": resumed,
        "created": not resumed,
        "prompt_digest": digest,
        "lifecycle": state.lifecycle,
        "state": update_tasks._state_payload(state),
    }


def _state_outcome(task: UpdateTask, state: TaskState) -> dict[str, Any]:
    """The same reply for a record-only decision (dismiss, reopen, a check)."""
    return {
        "task_id": task.id,
        "revision": task.revision,
        "scope": task.scope,
        "title": task.title,
        "chat_id": state.chat_id,
        "prompt_digest": state.prompt_digest,
        "lifecycle": state.lifecycle,
        "state": update_tasks._state_payload(state),
    }
