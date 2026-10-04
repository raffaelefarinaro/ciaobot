# Task board records

Storage contract for the shared workspace task board (issue #978, part of
#973). This document describes the file format, the `ciao.task_board` API,
and the rules its callers must honor. The production callers are the
session-authenticated `/api/tasks*` routes and the `ciao task …` agent CLI
(#1021), both over the workspace-scoped service in `ciao/control_plane.py`, and
the PWA board itself (`web/src/components/TaskBoardView.vue`, #1028 for the
columns and #1040 for the delegation review surface). The contract is stated in
one place so integration cannot invent a second source of truth.

## Layout

One task is one file:

```
<workspace-vault>/Workspace/Tasks/<id>.md
```

`<id>` is 32 lowercase hex characters (`uuid.uuid4().hex`), minted once at
creation. The filename and the frontmatter `id` must agree, and a title
edit never renames the identity. There is no index file, no ranking file,
and no cache: the board is derived by reading the directory, and `list()`
/ `get()` reread source on every call, so human hand edits are reflected
without a watcher.

## Schema (version 1)

YAML frontmatter, then a free Markdown body (description, links, acceptance
criteria, result notes). All twelve keys are required in the file; the four
nullable ones take `null` (absent reads as `null` for those four only).

| Key            | Type                       | Notes                                                        |
| -------------- | -------------------------- | ------------------------------------------------------------ |
| `schema`       | integer `1`                | Any other integer is `unsupported_schema`; a non-integer is `invalid_task`. |
| `id`           | 32 lowercase hex string    | Must equal the filename stem.                                |
| `title`        | string, 1–200 chars trimmed| Stored trimmed; surrounding whitespace is not significant.   |
| `status`       | `backlog` \| `in_progress` \| `on_hold` \| `done` |                     |
| `project_id`   | string \| null             | Opaque to this store; see "Obligations, resolved".          |
| `due`          | calendar date \| null       | `YYYY-MM-DD`, a real calendar day.                           |
| `assignee`     | `user` \| `agent`          |                                                              |
| `review_state` | `none` \| `ready`          |                                                              |
| `created_at`   | UTC datetime (ISO-8601)    | Immutable. Naive inputs are read as UTC.                     |
| `updated_at`   | UTC datetime (ISO-8601)    | Set by the store's clock on every managed write.             |
| `chat_id`      | string \| null             | Linkage; only hand edits carry it (see below).               |
| `attempt_id`   | string \| null             | Linkage; only hand edits carry it (see below).               |

A raw file is at most `MAX_TASK_BYTES = 65536` bytes. Duplicate mapping
keys, YAML aliases, and YAML merge keys (`<<`) are refused as `invalid_task`
before any record is built — a strict contract, not compatibility code.
Unknown frontmatter fields are allowed and preserved verbatim; they are
data, never executable instructions, and a managed edit never discards them.

### Synthetic example

```markdown
---
schema: 1
id: 9f2c4a1b7e3d4f6a8b5c2d1e0f3a4b6c
title: Draft the migration runbook
status: in_progress
project_id: null
due: 2026-10-20
assignee: agent
review_state: none
created_at: "2026-10-03T12:00:00+00:00"
updated_at: "2026-10-04T09:30:00+00:00"
chat_id: null
attempt_id: null
# hand-written comment: stays where it is
custom: keep me
---
See [the runbook outline](https://example.test/runbook).

- [ ] Outcome and rollback steps agreed
- [ ] Result recorded below
```

## Delegation attempts (#1033, child B5 of #973)

A task record says what is wanted. An **attempt** says whether an agent is
working on it right now, in which chat, and how far it got. They live in a
separate store, `ciao/task_attempts.py`, because they are the engine's own
bookkeeping about a turn it launched rather than the user's Markdown: the
records are in `<runtime>/task-attempts-<workspace>.json`, not in the vault,
and nothing here is recall, review or curation material.

`TaskAttemptStore(*, workspace, runtime_dir, clock)` — one store per logical
workspace. The vocabulary is deliberately **not** the board's:

| State | Meaning | Live? |
| ----- | ------- | ----- |
| `running` | a turn is in flight | yes |
| `needs_you` | the turn is paused on a question or an approval card | yes |
| `ready_for_review` | the provider turn ended; the result waits for the user | yes |
| `failed` | the turn ran and ended in an error | no |
| `interrupted` | the outcome is unknown — a dead launch or a crash | no |
| `stopped` | the user ended it, or detached it | no |

None of these is a column and none of them is a completion. `running`,
`needs_you` and `ready_for_review` are live — the attempt holds the task's
linkage, which is exactly what makes completion and reassignment refused — and
`failed`, `interrupted` and `stopped` are settled: the turn is over and the
attempt is history. A card's badge reports the **current** attempt, live or
settled, because `failed`, `stopped` and `interrupted` are the three states a
user most needs to look at; `live_attempt_id` beside it says whether an attempt
still *holds* the card, which is the difference between a result still awaiting
review and one the user has already approved or detached.

`LIVE_STATES` is what holds the task's linkage and what a second `start`
returns instead of minting a second attempt; `RESUMABLE_STATES`
(`failed`/`interrupted`/`stopped`) is what `resume` may continue in the same
chat. `ready_for_review` is live but not resumable on purpose: a finished turn
is waiting for the user's decision, and continuing it is the reviewer's call.

**Board state is not execution state.** None of these states is a column. A
finished turn sets `review_state: ready` — a badge in *In progress* — and only
the user's own completion action moves the card to `Done`. The store's
`completion_requires_user` rule is unchanged by any of this.

The review badge moves the task's revision, so the watcher rebinds the attempt to
the revision that write left behind (`bind_revision`). Without that, *every*
finished turn would read `changed_since_delegated`, which is a warning about the
user editing a task under the agent and would then mean nothing.

**The changed-since rule.** `changed_since_delegated` compares the revision the
attempt is bound to against the record now, and it is true only for a **real
user edit** after the hand-over. It is `false` right after a clean settle —
`link()` writes linkage and rebinds the attempt, and the review badge write
rebinds it again — and it is `true` once the user changes anything the record
holds, whether that is the description, the title or a move. It is also `false`
for a task nothing is delegated to any more, because a `detach` clears the
linkage and the flag is only derived for a linked task. The flag is a *fact about
revisions*, not a judgement: nothing in the board decides whether the result is
still good, and only the reviewer does.

**Reviewing is one gesture.** `POST /api/tasks/{id}/complete` from the user's own
session releases the linkage and closes the card when the task's attempt is
`ready_for_review` — a revision-checked `unlink` and then the completion, so the
attempt stays as `ready_for_review` history. Routing that through `detach` first
would settle the reviewed attempt as `stopped` and lose the result. A task with a
turn still in flight (`running`, `needs_you`) has no result to approve, so that
completion is still refused and needs `stop` or `detach` first, as does an agent
completion of any kind.

`detach` never rewrites a finished attempt: it releases the task and leaves the
state the turn actually ended in.

### Known limitations

- **A pending permission card does not end the turn, so the attempt reads
  `running`.** `needs_you` is derived from the chat's pending question or
  permission *after* the stream ends, so while the card is actually up and the
  user has not answered it, the badge still says Running. The Needs-you badge
  appears once the turn has ended waiting, not while it waits.
- **An attended turn in a delegated chat that did not *start* there is not
  announced.** Re-attaching (see "Re-attaching the watcher") covers every turn
  `ProjectChatManager.start_stream` begins, which is every route to the model
  from the composer, from **Send update** and from a retry. A follow-up turn the
  drive loop runs *inside* a stream it already began is covered too, because the
  watcher for that stream is still waiting on it. What is not covered is a turn
  the engine runs in a delegated chat with nobody present — a schedule dispatch,
  a between-turns drain — which is announced to nobody and settles no attempt.

Four rules the store holds:

- **One live attempt per task.** `start` takes the workspace lock across its
  whole read-then-write and reports `created`, so a double click, a second tab
  and an agent/UI race all answer with the same attempt — and only the call that
  minted it may follow with `start_stream`.
- **Intent is durable before the side effect.** The attempt and the task's
  linkage are written before the turn starts. A crash in that window leaves a
  record naming a chat nobody ran, which reads `interrupted` and is never
  replayed; the other order would leave an orphan chat nothing points at.
- **A crash is derived, not remembered.** Every record carries the writing
  process's `owner` token. A live attempt owned by another process cannot have a
  turn running in this one, so it reads `interrupted` — which is how a restart
  re-derives it with no startup sweep and nothing held in memory.
  `recover_interrupted()` is the durable half, for a caller that wants the
  ambiguity settled on disk.
- **History is bounded.** `MAX_ATTEMPTS_PER_TASK` (50) per task and
  `MAX_HISTORY` (500) per workspace, both dropping the oldest and never a live
  attempt, so a task retried for ever cannot grow the document without limit.

The prompt and the provenance stamp are server-owned, with no parameter through
which a caller may supply prompt text: a delegation hands over the task the user
filed. `build_prompt` quotes the record's body inside a fixed
`<task-board-task>` fence with the tag escaped inside the text, so a hand-edited
body cannot close its own frame. `task_delegation_helper` stamps the chat with
the task id, its revision and the attempt id, which is what makes the chat
findable again after a reload or a restart. The attempt id is minted **before**
the chat is created and passed to `start(..., attempt_id=...)`, so the stamp and
the store can only ever name the same attempt — `update_chat` has no `helper`
parameter, so a stamp written after the fact would raise and be swallowed.

Linkage mutation is the one thing `ciao/task_board.py` delegates to this child:
`chat_id`/`attempt_id` are refused as ordinary patch fields and get their own
revision-checked `link()`/`unlink()` door instead.

Delegating a task that is already in *Done* is refused with `invalid_task`.
`link()` hands the task over as `in_progress`/`agent`, so delegating a finished
one would reopen it — and an agent delegating over MCP could undo a completion
the user made by hand. Moving the task out of *Done* is the user's own gesture.

The watcher only writes to a task its own attempt still holds. It refuses to
settle an attempt that has already settled (the store refuses the transition) and
checks `task.attempt_id == attempt_id` before flagging a task for review, so a
watcher left over from a detached or retried attempt cannot put a Review badge on
a card another attempt now owns. A stream that ends without a `result` event
settles `interrupted`, not `ready_for_review`: there is no answer to review, and
an empty result dressed as a finished one is the one reading the user cannot
recover from without opening the chat.

The gesture routes (`stop`, `detach`) await `ProjectChatManager.stop_chat`, which
is `async`. `/api/tasks/{task_id}/attempt/{attempt_id}/{action}` checks `task_id`
against the attempt and answers `task_attempt_not_found` on a mismatch, so an
attempt id sent under another task's URL acts on nothing.

## The review surface (#1040, child B6 of #973)

B5 shipped the lifecycle. This is the human loop around it, and it is a **surface
and a contract**: no new store, no new route, and no change to the attempt store.
Four things a card has to be able to say, and what each one is allowed to do.

**Where the result is, and how it is closed.** A `ready_for_review` card names the
linked chat and carries one **Approve Done**. It does not carry Stop — there is no
turn running to stop — and it does not require a Detach first, because that is the
workaround whose whole cost is the result: detaching a finished attempt settles it
`stopped`. Approve Done is `POST /complete` at the revision the card was drawn at,
which is `workspace_task_complete_reviewed`'s revision-checked `unlink` followed by
the completion, so the attempt survives as `ready_for_review` history. A card whose
review has already been released (`live_attempt_id` empty) is not review-ready
whatever its badge says, and offers a plain Done — otherwise one result would have
two completion gestures.

**History.** `GET /api/tasks/{id}/attempts` is the whole read: every attempt,
live one first, with its `chat_id`, `state`, `ended_at` and the engine's
`detail`. The board loads it per card on demand, on the proposal queue's History
pattern (loaded once, invalidated by a gesture, refetched on a workspace switch)
rather than as a new timeline store, because the list rows carry the current
attempt's badge and nothing else — a workspace with fifty retried tasks would
otherwise read fifty histories nobody opened. Each row keeps **its own** chat
link: by the time a retry exists the task's own linkage names the new attempt,
so the old attempt's chat is reachable only from here.

**Send update.** An edit made under a running or finished turn trips
`changed_since_delegated`, and the answer is one ordinary message into the
attempt's own chat carrying the task as it stands now. It is deliberately **not**
a second delegation: `POST /delegate` would mint a new attempt id, re-quote the
description from scratch, re-link the card and leave the attempt whose result is
under review looking abandoned. The preview shows the exact text before it is
sent, and refuses to send at all when the description read failed, because "the
description is now empty" is a fact nobody checked.

**Reconciled visibly.** A row whose fields contradict each other is flagged on the
card with what disagrees *and* the controls that resolve it, never silently
rewritten. Five derivations, all decidable from the row the service already sends:
a settled attempt while the card still reads *In progress* for the agent; a
`review_state: ready` badge with no result waiting; a result waiting with no
badge; a card in *Done* while an attempt still holds it; and a `chat_id` the
browser's own chat list does not contain. The last one is the only one that needs
anything outside the row, and it is deliberately worded as what the browser can
see — an empty chat list means the board knows nothing, and a flag on that basis
would be a guess about every card.

### Send update rebinds and re-attaches (#1047)

`POST /api/tasks/{task_id}/attempt/{attempt_id}/update` is what the PWA calls, and
it does the two things a composer send cannot. It is a route rather than a
`sendMessage` because **neither half is a client concern**.

**The rebind.** The attempt is re-stamped to the revision the update was made at
(`bind_revision`), exactly as the delegation path re-stamps it after its linkage
write. That is what makes `changed_since_delegated` return to `false` and the
control retire instead of offering the same update for ever. The order is the
contract: **a refused send rebinds nothing**, because a cleared flag over an
update the agent never received is the one thing this gesture must not be able to
say. `expected_revision` is required and checked before anything is sent — a
description edited since the preview was read is not the one the message claims to
carry — and the message text travels with the request, because it is an ordinary
attended message the user composed and was shown in full.

**The re-attach.** Whichever turn the message causes is watched by the same
settling watcher the delegation path uses, so the agent's answer advances the card
rather than waiting for the reviewer to notice it by hand. A turn is usually
already in flight in that chat — that is what an edit made *under* the agent
means — so the message is queued into it exactly as the composer queues it, and
that turn runs it as its own follow-up and ends only afterwards; no second watcher
is needed for that, because the stream the first watcher is holding does not end
until the queued turn has run. `start_stream` hands back a turn already running
rather than queueing anything, so a message that landed nowhere is refused
(`task_update_busy`, 409) rather than rebound over.

Only an attempt that still holds the task may be updated. A settled one is
`invalid_action`: continuing a turn that did not finish is `resume` and replacing
it is `retry`, and neither is a message into a chat. One live attempt per task is
unchanged — the update continues the delegation rather than minting a second one,
and no second chat is created.

### Re-attaching the watcher

Both "the answer to the update does not move the card" and "answering a `needs_you`
turn in the chat does not move the attempt" are the same event — *the
conversation in a delegated chat went on* — so they are answered by one path
rather than one special case each.

`ProjectChatManager.on_turn_started` is an injection point of the same shape as
`notify_result_cb`: the manager announces that an attended turn has begun, from
`ChatStreaming.start_drive`, which is the one place a turn begins. The delegation
service subscribes once and re-attaches the attempt behind the chat — found by the
`task_delegation` provenance stamp, which is the only thing that says a chat
belongs to a task — provided that attempt is still live and still holds its task.

Re-attaching is idempotent: an attempt already being watched is left alone, so a
gesture that starts the turn *and* announces it cannot end up with two watchers
settling one attempt and flagging one task twice. Nothing about the attempt's
state changes on re-attach — the same four outcomes are written, and the store
still refuses a transition that would invent one.

Only *attended* turns are announced. A background drain or an unattended dispatch
is the engine talking to itself in a chat, and settling a delegated attempt on
work nobody asked for is exactly the escalation the delegation block refuses
everywhere else.

### What Send update does not do

- **It is not a way to restart a failed turn.** A settled attempt stays settled;
  continuing it is `resume` (same chat, same attempt) or `retry` (a new one), and
  the card's own foot names both rather than borrowing the update's label. A
  settled attempt is refused for an update outright, because rebinding one would
  clear a flag about a turn nothing is watching.
- **It does not change the attempt's state while the turn runs.** Re-attaching is
  the same watcher the delegation path uses, and it moves no state: a card whose
  attempt is `ready_for_review` keeps that badge while the answer to the update is
  being produced, and the card's own text says the turn has ended. The state the
  update *does* advance is the one it advances when it ends.

## API

`TaskBoardStore(*, workspace, vault_root, runtime_dir, clock)` — one store
per logical workspace. `vault_root` and `runtime_dir` must come from
authoritative workspace resolution — the control plane's
`workspace_vault_root` — never from HTTP-supplied arbitrary roots; this
store does not resolve or fall back across workspaces.

- `create(*, title, body="", project_id=None, due=None)` — fresh identity,
  `backlog` / `user` / `none` defaults, null linkage, `created_at ==
  updated_at == clock()`.
- `get(task_id)` — the current document.
- `update(task_id, *, expected_revision, changes, body=None, actor)` —
  managed field edits plus an optional wholesale body replacement. The
  body changes only when `body` is not None.
- `link(task_id, *, expected_revision, chat_id, attempt_id, status="in_progress", assignee="agent")` /
  `unlink(task_id, *, expected_revision)` — the delegation child's own door onto
  the two linkage fields. `update` refuses `chat_id`/`attempt_id` as patch fields
  (an agent naming its own chat would be claiming a delegation it never made), so
  these are the only writes that stamp or clear linkage, and they carry the same
  revision protocol: required, rechecked under the lock, nothing written on a
  conflict. `link` defaults to handing the task over — `in_progress` and `agent` —
  in the same atomic write, because a delegated task left in *Backlog* assigned to
  the user could never reach `review_state: ready`, which needs exactly that pair.
- `delete(task_id, *, expected_revision)` — revision-checked under the
  workspace lock; unlinks only a real task record inside
  `Workspace/Tasks`, never a link target or anything outside it. Raises
  `invalid_task` with no revision, `not_found` when there is no file,
  `unsafe_path` for a malformed id, a link or a non-regular file,
  `revision_conflict` on a stale one and `read_failed` if the removal
  fails (record unchanged). There is no trash: the file is the user's own
  Markdown, and this removes it.
- `list()` — `TaskListResult(tasks, invalid)`: valid documents in board
  order plus one `{relative_path, code, message}` entry per unreadable
  file. A missing `Tasks/` directory reads as empty and creates nothing;
  a directory traversal failure raises `read_failed` instead of returning
  a partial clean result.

Pure helpers: `parse_task(raw, *, expected_id)`, `render_new_task(record,
body)`, `patch_task(document, changes, *, body=None)`. Editable patch
fields are `title`, `status`, `project_id`, `due`, `assignee`,
`review_state` (plus store-managed `updated_at`). `schema`, `id`,
`created_at`, `chat_id` and `attempt_id` are refused as patch fields:
identity and creation time are immutable, and linkage mutation belongs to
the delegation child.

### Errors

`TaskBoardError(code, message)` with one of: `invalid_task`,
`unsupported_schema`, `not_found`, `revision_conflict`, `unsafe_path`,
`completion_requires_user`, `read_failed`. Parse problems are
`invalid_task`/`unsupported_schema`; store I/O, lock and write failures
are `read_failed`.

## Dates, sort, revisions

- `due` is a calendar day, never an instant: a YAML date scalar is
  normalized to ISO for the typed record, a quoted ISO string reads back
  identically, and a datetime in `due` is refused outright. `due` is never
  interpreted as a UTC scheduled start.
- Board order is `(due is None, due, created_at, id)`: dated tasks first
  by day, undated last, ties deterministic. No persisted ordering exists
  to drift from it.
- A document's revision is the SHA-256 hex of its exact raw bytes
  (BOM included). `update` requires the revision it read, rechecks it
  immediately before the rename, and raises `revision_conflict` on a
  mismatch. Stale revisions and parse/write failures leave the prior bytes
  unchanged, and one store object can never drop an unrelated task file.

## Source preservation and confinement

A managed edit replaces only the value spans of the fields it changes
(located with YAML node marks) and appends a missing optional owned key
just before the closing delimiter. Comments, unknown fields, key order,
all body bytes, a BOM, and CRLF endings survive; an idempotent call
returns the identical bytes. Edits needing an ambiguous rewrite (an owned
field stored as a block scalar, collection, or multi-line scalar;
invalid/truncated frontmatter) are refused explicitly — reported, never
normalized silently — with the original bytes untouched.

Confinement and honesty limits: ids are validated before any path is
built; task files and the task directory are never opened through a link;
non-regular and oversized files are refused; only the single `Tasks/`
directory is ever listed. Writes go through a per-workspace keyed plus
advisory lock in the runtime directory, a unique sibling temp file, and an
atomic replace that preserves the prior file's mode — cleaning up only
its own temp. External editors honor no advisory lock, so this store does
not promise a filesystem sandbox or race-free arbitrary external edits;
the revision recheck is the protection, and anything it catches is a
conflict, not a silent overwrite.

## Managed-operation rules (API only)

These bind `update` through the managed API, not direct file editors —
a hand edit may hold any schema-valid state:

- An `agent` caller cannot set status `done` (`completion_requires_user`).
  A `user` can complete a manual (unlinked) task.
- A task linked to a live chat or attempt (`chat_id`/`attempt_id`
  non-null) cannot be completed or reassigned here at all — by **any** actor,
  including the signed-in user's own session, because a turn is in flight. The
  delegation service's `detach` clears the linkage, and only then does
  completion become available; `stop` alone does not, so "stopped" never quietly
  becomes "the user may close this".
- `ready` requires `agent` assignment and `in_progress` status. Leaving
  `in_progress` without an explicit review change clears `ready` to `none`.

## Task records are bookkeeping, not notes

A task record is the system's own paperwork, so #1002 keeps the whole
`Workspace/Tasks/` directory out of every surface that presents the vault as
the operator's memories, and inside the one that preserves them:

- **Excluded from recall.** `Workspace/Tasks/<id>.md` is reserved bookkeeping
  (`vault_index.is_reserved_bookkeeping`), so neither the bulk FTS walk nor a
  forced `index_file` writes a row for it and no task id or status is ever a
  recall result.
- **Excluded from the Memory Map.** `scan_vault` skips it, so a task record is
  not a node and its body links are not edges.
- **Excluded from review and curation.** Vault review already refused any path
  holding a `Workspace` part, and the record is not an orphan candidate.
- **Excluded from the lint's note checks**, by the same shared predicate: it is
  not a note to validate, link-check, or count.
- **Included in the durable backup.** `memory-vault` is a durable scope base, so
  every task record is committed by the unattended backup — the board is
  user-owned data and losing it loses the work.

The rule is keyed on the *directory pair* `Workspace/Tasks`, not on the name
`Tasks`: a user's own folder called `Tasks` anywhere else
(`Other/Tasks/x.md`) is ordinary note content and stays indexed and scannable.

## Obligations, resolved

- Live project membership and completion validation was a later
  application-service obligation: this store keeps the `project_id`
  string without judging it and invents no project registry. (#1033 resolves
  membership in the delegation service before it picks a chat's host, and
  refuses rather than falling back when a named project is gone or foreign.)
- Linkage mutation (`chat_id`/`attempt_id`) belonged to the delegation child, and
  it ships: `link()`/`unlink()` above are that child's door, and this store still
  never creates a live chat or attempt. The fields are delegated-run-owned — the
  attempt and the chat it names are the record of what the engine ran, and a
  caller naming its own chat would be claiming a delegation it never made.
- The board UI ships: #1028 for the four columns, the filters and the detail
  editor, #1033 for the delegation gestures on it, and #1040 for the review
  surface — result review, the linked chat, attempt history, Send update and the
  visible reconciliation of a row that disagrees with itself. There is no
  remaining surface owed by this document.

## Still open

- Nothing here is finished beyond the above. The two watcher gaps recorded by
  **#1047** — a **Send update**'s turn was not watched, and answering a delegated
  chat in the browser did not move the attempt — are closed by the re-attach above,
  and the attempt store is unchanged by it. The narrower limitation it leaves, an
  *unattended* turn in a delegated chat settling nothing, is recorded in "Known
  limitations".
- Drag, priorities, recurring tasks, deadline reminders, multi-user boards and any
  unattended delegation remain out of scope for #973.
