# Task board records

Storage contract for the shared workspace task board (issue #978, part of
#973). This document describes the file format, the `ciao.task_board` API,
and the rules its callers must honor. The production callers are the
session-authenticated `/api/tasks*` routes and the `ciao task …` agent CLI
(#1021), both over the workspace-scoped service in `ciao/control_plane.py`;
there is still no board UI. The contract is stated in one place so
integration cannot invent a second source of truth.

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
| `project_id`   | string \| null             | Opaque to this store; see "Future obligations".              |
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
  non-null) cannot be completed or reassigned here at all; that needs the
  delegation service's stop/detach workflow.
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

## Future obligations

- Live project membership and completion validation is a later
  application-service obligation: this store keeps the `project_id`
  string without judging it and invents no project registry.
- Linkage mutation (`chat_id`/`attempt_id`) belongs to the delegation
  child. Source hand edits may carry nullable linkage, but this store
  never creates a live chat or attempt.
- No board UI is shipped yet (B4), and #973 stays open for the board and
  delegation children.
