/**
 * The task board's pure half: the four columns, how rows are bucketed and
 * ordered, the filters, and the one envelope reader `/api/tasks*` needs.
 *
 * Vue-free and store-free on purpose. Everything here is a decision about rows
 * that the store and the pane must agree on, so it lives where both can read it
 * and where `taskBoard.test.ts` can pin it without mounting anything.
 */

import { apiErrorMessage, errorPayload } from './errorMessage'
import type {
  Task,
  TaskAttempt,
  TaskAttemptState,
  TaskDetail,
  TaskInvalidRow,
  TaskRow,
  TaskStatus,
} from './types'

/** The board's four fixed columns, in board order. */
export const TASK_COLUMNS: ReadonlyArray<{ status: TaskStatus; label: string }> = [
  { status: 'backlog', label: 'Backlog' },
  { status: 'in_progress', label: 'In progress' },
  { status: 'on_hold', label: 'On hold' },
  { status: 'done', label: 'Done' },
]

export const TASK_STATUSES: ReadonlyArray<TaskStatus> = TASK_COLUMNS.map((c) => c.status)

export function taskStatusLabel(status: TaskStatus): string {
  return TASK_COLUMNS.find((c) => c.status === status)?.label ?? status
}

/** The statuses a `<select>` offers, in board order. */
export const TASK_STATUS_OPTIONS = TASK_COLUMNS

export const TASK_ASSIGNEE_LABELS: Record<string, string> = {
  user: 'For me',
  agent: 'For the agent',
}

export function taskAssigneeLabel(assignee: string): string {
  return TASK_ASSIGNEE_LABELS[assignee] ?? assignee
}

/**
 * Whether a list row is one of the "this file is not a task" entries.
 *
 * A structural check rather than `code in row`: the server decides this by not
 * being able to read the file, so the fields that make a row a task are exactly
 * the ones a refusal does not carry.
 */
export function isTaskInvalidRow(row: TaskRow | null | undefined): row is TaskInvalidRow {
  return Boolean(row) && typeof (row as TaskInvalidRow).code === 'string'
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asStatus(value: unknown): TaskStatus {
  return TASK_STATUSES.includes(value as TaskStatus) ? (value as TaskStatus) : 'backlog'
}

/**
 * One readable task, every field defaulted.
 *
 * The single reader both the list and a detail read go through, so a drifting
 * field set is fixed once. `status` a build does not know becomes `backlog`
 * rather than a fifth column; `revision` is read as the hex string it is,
 * because rounding it into a number would send back a revision the server never
 * issued.
 */
/**
 * The attempt states, as `ciao/task_attempts.py::ATTEMPT_STATES` defines them.
 *
 * Listed rather than inferred from what is live, because the two are different
 * questions: `needs_you` and `ready_for_review` are live states a card draws
 * differently, and a build that has never heard of a state must read it as "this
 * task is not running" rather than as a state with no label.
 */
export const TASK_ATTEMPT_STATES: ReadonlyArray<TaskAttemptState> = [
  'running',
  'needs_you',
  'failed',
  'interrupted',
  'ready_for_review',
  'stopped',
]

/** The badge wording for each attempt state, or the state itself when unknown. */
export const TASK_ATTEMPT_LABELS: Record<string, string> = {
  running: 'Running',
  needs_you: 'Needs you',
  failed: 'Failed',
  interrupted: 'Interrupted',
  ready_for_review: 'Review ready',
  stopped: 'Stopped',
}

export function taskAttemptLabel(state: string): string {
  if (!state) return ''
  return TASK_ATTEMPT_LABELS[state] ?? state
}

/**
 * Whether an attempt state means a turn is still in flight.
 *
 * The server sends `live_attempt_id` alongside the state rather than expecting the
 * client to re-derive this, but the pane needs the same question for the states it
 * offers gestures on, and duplicating the vocabulary in one place beats deriving it
 * twice from a different list.
 */
export function isLiveAttemptState(state: string): boolean {
  return state === 'running' || state === 'needs_you' || state === 'ready_for_review'
}

function asAttemptState(value: unknown): TaskAttemptState | '' {
  return TASK_ATTEMPT_STATES.includes(value as TaskAttemptState)
    ? (value as TaskAttemptState)
    : ''
}

function taskFrom(raw: Partial<Task> | null | undefined): Task {
  const row = raw ?? {}
  return {
    id: asString(row.id),
    title: asString(row.title),
    status: asStatus(row.status),
    project_id: asString(row.project_id),
    due: asString(row.due),
    assignee: row.assignee === 'agent' ? 'agent' : 'user',
    review_state: row.review_state === 'ready' ? 'ready' : 'none',
    chat_id: asString(row.chat_id),
    attempt_id: asString(row.attempt_id),
    created_at: asString(row.created_at),
    updated_at: asString(row.updated_at),
    revision: asString(row.revision),
    relative_path: asString(row.relative_path),
    // A payload with no attempt fields — a row from a build predating delegation,
    // or a record the service answered without them — reads as "not delegated",
    // which is the honest reading of a field that was never set.
    attempt_state: asAttemptState(row.attempt_state),
    live_attempt_id: asString(row.live_attempt_id),
    changed_since_delegated: row.changed_since_delegated === true,
  }
}

/** A task with every field at its empty value: what a payload that is not a task
 *  normalises to, and never a card worth drawing. */
function emptyTask(): Task {
  return taskFrom(null)
}

/**
 * The rows a `GET` answered with, every field defaulted.
 *
 * This is the one place a payload's field set can drift, so each value is read
 * defensively and a missing list is an empty board rather than a throw. A file
 * the server could not read as a task stays a row of its own.
 */
export function taskRowsFrom(json: unknown): TaskRow[] {
  const rows = (json as { tasks?: unknown } | null | undefined)?.tasks
  if (!Array.isArray(rows)) return []
  const out: TaskRow[] = []
  for (const raw of rows) {
    const row = (raw ?? {}) as Partial<Task> & Partial<TaskInvalidRow>
    if (isTaskInvalidRow(row as TaskRow)) {
      out.push({
        id: asString(row.id),
        path: asString(row.path),
        code: asString(row.code),
        message: asString(row.message),
      })
      continue
    }
    out.push(taskFrom(row))
  }
  return out
}

/**
 * One task read or written, with its description.
 *
 * `GET /api/tasks/{id}` and every write answer with `body`; the same drift
 * argument applies, and the editor writes the description back, so a payload
 * without one must read as an empty description rather than as `undefined` the
 * editor would then refuse to trim.
 */
export function taskDetailFrom(json: unknown): TaskDetail {
  const raw = (json ?? {}) as Partial<Task> & { body?: unknown }
  // An unreadable-file row has no task fields at all; reading it as a task would
  // put an empty card on the board.
  const base = isTaskInvalidRow(raw as TaskRow) ? emptyTask() : taskFrom(raw)
  return { ...base, body: typeof raw.body === 'string' ? raw.body : '' }
}

/** The readable tasks in a row list, in the order the server sent them. */
export function readableTasks(rows: TaskRow[]): Task[] {
  return rows.filter((row): row is Task => !isTaskInvalidRow(row))
}

/** The unreadable files in a row list. */
export function invalidTaskRows(rows: TaskRow[]): TaskInvalidRow[] {
  return rows.filter(isTaskInvalidRow)
}

/**
 * A write's record, back to the shape the list serves.
 *
 * Every write answers with `body` included and the list never carries it, so
 * storing the write's own answer verbatim would put prose into every later list
 * render. The fields are listed rather than spread so a field added to `Task`
 * later has to be a deliberate decision here instead of leaking in by default.
 */
export function toTaskListRow(detail: TaskDetail | Task): Task {
  return {
    id: detail.id,
    title: detail.title,
    status: detail.status,
    project_id: detail.project_id,
    due: detail.due,
    assignee: detail.assignee,
    review_state: detail.review_state,
    chat_id: detail.chat_id,
    attempt_id: detail.attempt_id,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
    revision: detail.revision,
    relative_path: detail.relative_path,
    attempt_state: detail.attempt_state,
    live_attempt_id: detail.live_attempt_id,
    changed_since_delegated: detail.changed_since_delegated,
  }
}

/**
 * One attempt, every field defaulted — the same drift argument as `taskFrom`.
 *
 * A payload that is not an attempt reads as an empty record, and callers check
 * `attempt_id` before drawing anything: an attempt with no id is one no gesture
 * could act on, so a badge built from it would be a control that cannot be pressed.
 */
export function taskAttemptFrom(raw: unknown): TaskAttempt {
  const row = (raw ?? {}) as Partial<TaskAttempt>
  return {
    attempt_id: asString(row.attempt_id),
    task_id: asString(row.task_id),
    task_revision: asString(row.task_revision),
    chat_id: asString(row.chat_id),
    state: asAttemptState(row.state) || 'interrupted',
    created_at: asString(row.created_at),
    updated_at: asString(row.updated_at),
    ended_at: asString(row.ended_at),
    detail: asString(row.detail),
    // Both markers are carried rather than re-derived, and only trusted when the
    // server said so: a client that computed either from the state would be a
    // second definition free to disagree with the one every gesture depends on.
    released: row.released === true,
    live: row.live === true,
  }
}

// ── Reconciling a row that disagrees with itself ──────────────────────────

/** One way a card's own fields can contradict each other. */
export type TaskReconcileCode =
  /** The card reads In progress for the agent while its attempt has ended. */
  | 'settled_attempt_holds_task'
  /** A Review badge with no delegation holding a result to review. */
  | 'review_without_result'
  /** A result waiting for review with no Review badge to say so. */
  | 'result_without_review'
  /** The card is Done while an attempt still holds it. */
  | 'done_while_live'
  /** The card names a chat this browser cannot see. */
  | 'chat_not_visible'

/**
 * One inconsistency, in the two halves a card needs to draw it.
 *
 * `text` says what disagrees and `actions` names the controls that resolve it,
 * because a flag the user cannot act on is the same dead end as silently
 * rewriting the row. Nothing here writes: the board is the one place that may
 * *say* a record disagrees with itself, and repairing it is the user's gesture.
 */
export interface TaskReconcileNote {
  code: TaskReconcileCode
  text: string
  actions: string
}

/** A short human label for a reconcile code; the code itself is a `data-` attribute. */
export function taskReconcileLabel(code: TaskReconcileCode | string): string {
  switch (code) {
    case 'settled_attempt_holds_task':
      return 'Out of step'
    case 'review_without_result':
      return 'No result to review'
    case 'result_without_review':
      return 'Result not flagged'
    case 'done_while_live':
      return 'Done but running'
    case 'chat_not_visible':
      return 'Chat not here'
    default:
      return 'Out of step'
  }
}

/**
 * The colour a reconcile note wears.
 *
 * `settled_attempt_holds_task` is the normal B5 resting state — a failed,
 * interrupted or stopped attempt whose card still reads In progress for the
 * agent, with Resume and Retry right there. It is worth saying, but it is not an
 * error, and a red banner on every such card would read as one. Everything else
 * is a genuine contradiction, which does get the error colour.
 */
export function taskReconcileBadgeClass(code: TaskReconcileCode | string): string {
  return code === 'settled_attempt_holds_task' ? 'badge--muted' : 'badge--error'
}

/**
 * What is inconsistent about one row, in the order a card should say it.
 *
 * Every case is decidable from the row plus one external set, so the card and
 * this function cannot disagree about what the badge means. Two things are
 * deliberately *not* flagged:
 *
 * - A released `ready_for_review` (no `live_attempt_id`) is what an approved or
 *   detached card looks like, not a contradiction — that is the state the
 *   approval gesture leaves behind.
 * - A `chat_id` is only reported as unseeable when the caller passes a
 *   non-empty set. An empty set means "this browser has not loaded its chats",
 *   which is true on a fresh mount and on a workspace that holds none, and
 *   flagging every card's chat on that is the kind of lie this section exists
 *   to prevent.
 */
export function taskReconcileNotes(
  task: Task,
  options: { knownChatIds?: ReadonlySet<string> } = {},
): TaskReconcileNote[] {
  const notes: TaskReconcileNote[] = []
  const linked = Boolean(task.attempt_id) || Boolean(task.live_attempt_id)
  const settled = Boolean(task.attempt_state) && !isLiveAttemptState(task.attempt_state)
  if (linked && settled && task.status === 'in_progress' && task.assignee === 'agent') {
    notes.push({
      code: 'settled_attempt_holds_task',
      text:
        `This card is In progress for the agent, but its last attempt ended `
        + `${taskAttemptLabel(task.attempt_state).toLowerCase()}.`,
      actions: 'Resume continues that attempt, Retry starts a new one, Detach releases the card.',
    })
  }
  if (task.review_state === 'ready' && task.attempt_state !== 'ready_for_review') {
    notes.push({
      code: 'review_without_result',
      text: 'The Review badge is set, but no attempt has a result waiting to be reviewed.',
      actions: 'Delegate hands the task to the agent again; nothing is rewritten for you.',
    })
  }
  // A released attempt keeps `attempt_state: ready_for_review` forever, so the
  // `live_attempt_id` guard is what separates "waiting for you" from "you already
  // approved it": without it every approved or detached card raised this flag the
  // moment the gesture cleared its live attempt.
  if (
    task.live_attempt_id
    && task.attempt_state === 'ready_for_review'
    && task.review_state !== 'ready'
  ) {
    notes.push({
      code: 'result_without_review',
      text: 'An attempt has a result waiting, but this card carries no Review badge.',
      actions: 'Open the chat to read it, then Approve Done to close the card.',
    })
  }
  if (task.status === 'done' && task.live_attempt_id) {
    notes.push({
      code: 'done_while_live',
      text: 'This card is in Done while an attempt still holds it.',
      actions: 'Detach releases the card without rewriting the attempt history.',
    })
  }
  const known = options.knownChatIds
  if (task.chat_id && known && known.size > 0 && !known.has(task.chat_id)) {
    notes.push({
      code: 'chat_not_visible',
      text:
        'The chat this card names is not in this browser\'s chat list — it may have '
        + 'been archived or deleted.',
      actions: 'Open chat still tries it; Detach releases the card either way.',
    })
  }
  return notes
}

/** When an attempt ended as a card reads it; empty when the stamp is unusable. */
export function taskAttemptWhen(attempt: TaskAttempt): string {
  const date = new Date(attempt.ended_at || attempt.updated_at)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

/**
 * One attempt as its history row reads it: what happened, when, and why.
 *
 * The `detail` is the engine's own bounded sentence about the outcome, not the
 * agent's answer — a history row says how the turn ended, and the answer itself
 * is in the chat the row links.
 */
export function taskAttemptSummary(attempt: TaskAttempt): string {
  const state = taskAttemptLabel(attempt.state)
  const when = taskAttemptWhen(attempt)
  const head = when ? `${state} · ${when}` : state
  return attempt.detail ? `${head} — ${attempt.detail}` : head
}

/**
 * The message a **Send update** hands to the delegated chat.
 *
 * `chat_send`-shaped rather than a second delegation, and that is the whole
 * contract: it is one ordinary turn inside the attempt's own chat, so the answer
 * that comes back belongs to the attempt already on the card and the linkage,
 * the revision binding and the history all stay exactly as they are. Delegating
 * again instead would mint a second attempt, re-quote the description from
 * scratch and leave the first one's result looking abandoned — the silent
 * re-delegation the card's own words rule out.
 *
 * It carries the task as it stands now — title, column, project, date and the
 * current description — because the agent is holding an older one, and a message
 * that said only "this changed" would leave it to guess what.
 */
export function buildTaskUpdateMessage(task: Task, body: string): string {
  const facts = [
    `Title: ${task.title}`,
    `Column: ${taskStatusLabel(task.status)}`,
    task.project_id ? `Project: ${task.project_id}` : '',
    formatTaskDue(task.due) ? `Due: ${formatTaskDue(task.due)}` : '',
  ].filter(Boolean)
  const prose = body.trim()
  return [
    `The task "${task.title}" changed after you were handed it. Work from this, not`
    + ' from the version you were given.',
    '',
    ...facts,
    '',
    prose || '(the description is now empty)',
    '',
    'Carry on with the same work in this chat rather than starting over.',
  ].join('\n')
}

// ── Filters ───────────────────────────────────────────────────────────────

export const TASK_DUE_FILTERS = [
  { value: 'any', label: 'Any due date' },
  { value: 'overdue', label: 'Overdue' },
  { value: 'today', label: 'Due today' },
  { value: 'week', label: 'Due this week' },
  { value: 'none', label: 'No due date' },
] as const

export type TaskDueFilter = (typeof TASK_DUE_FILTERS)[number]['value']

/** Project filter values are project ids, `''` for every project, or
 *  {@link TASK_NO_PROJECT}. */
export type TaskProjectFilter = string

/**
 * Today's date as `YYYY-MM-DD` in the browser's own timezone.
 *
 * A due date is a calendar day the user reads, so it is compared as a local
 * calendar day. Parsing it as UTC (what `new Date('2026-03-12')` does) reads
 * back as the 11th for anyone west of Greenwich.
 */
export function localDateKey(date: Date = new Date()): string {
  const year = String(date.getFullYear())
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/** Whether a `due` value is a `YYYY-MM-DD` day this pane can compare. */
export function isDueDate(value: unknown): value is string {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
}

function addDays(day: string, delta: number): string {
  const date = new Date(`${day}T00:00:00`)
  if (Number.isNaN(date.getTime())) return day
  date.setDate(date.getDate() + delta)
  return localDateKey(date)
}

/** True for a task dated before today. An undated task is never overdue. */
export function isOverdue(task: Pick<Task, 'due'>, today: string = localDateKey()): boolean {
  return isDueDate(task.due) && task.due < today
}

/** The due date as a card reads it, or `''` when there is none. */
export function formatTaskDue(due: unknown): string {
  if (!isDueDate(due)) return ''
  const date = new Date(`${due}T00:00:00`)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

/** The project filter's value for "filed under no project". */
export const TASK_NO_PROJECT = 'no-project'

/**
 * Whether a task survives a project filter.
 *
 * `''` means every project and {@link TASK_NO_PROJECT} the tasks filed under
 * none, which is a real state on the board (a card reads *General*) rather than
 * an absence, so it gets its own filter value. A project whose id happened to
 * equal the sentinel is unreachable through the filter rather than silently
 * answering for the unfiled tasks.
 */
export function matchesProjectFilter(task: Task, filter: TaskProjectFilter): boolean {
  if (!filter) return true
  if (filter === TASK_NO_PROJECT) return !task.project_id
  return task.project_id === filter
}

export function matchesDueFilter(task: Task, filter: TaskDueFilter, today: string = localDateKey()): boolean {
  switch (filter) {
    case 'any':
      return true
    case 'none':
      return !isDueDate(task.due)
    case 'overdue':
      return isOverdue(task, today)
    case 'today':
      return task.due === today
    case 'week':
      return isDueDate(task.due) && task.due >= today && task.due <= addDays(today, 7)
    default:
      return true
  }
}

// ── Ordering ──────────────────────────────────────────────────────────────

/**
 * One column's order: the dated work by date, then the undated, then by title.
 *
 * A board's question is "what is next", and a due date is the only field that
 * answers it, so it leads. Undated work sinks below every dated card instead of
 * sorting as infinitely overdue, and the title breaks ties so the order does not
 * shift between two renders of the same rows.
 */
export function sortTasks(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => {
    const aDue = isDueDate(a.due)
    const bDue = isDueDate(b.due)
    if (aDue !== bDue) return aDue ? -1 : 1
    if (aDue && bDue && a.due !== b.due) return a.due < b.due ? -1 : 1
    return a.title.localeCompare(b.title) || a.id.localeCompare(b.id)
  })
}

/** One lane of the board: a fixed column, or the one list a filter narrowed it to. */
export interface TaskLane {
  /** The status this lane draws, or `null` when it is the unfiltered list. */
  status: TaskStatus | null
  label: string
  tasks: Task[]
}

/**
 * The lanes the board draws.
 *
 * Wide with no status picked, that is the four fixed columns. Narrow — or with a
 * status picked on either layout — it is one lane holding the filtered tasks, so
 * a card, its status `<select>` and its count are rendered from one code path
 * rather than a second copy of the card for the narrow layout.
 */
export function taskLanes(
  tasks: Task[],
  options: { status: TaskStatus | 'all'; narrow: boolean },
): TaskLane[] {
  if (options.status === 'all' && !options.narrow) {
    return TASK_COLUMNS.map((column) => ({
      status: column.status,
      label: column.label,
      tasks: sortTasks(tasks.filter((task) => task.status === column.status)),
    }))
  }
  const label = options.status === 'all'
    ? 'All tasks'
    : taskStatusLabel(options.status)
  const kept = options.status === 'all'
    ? tasks
    : tasks.filter((task) => task.status === options.status)
  return [{ status: options.status === 'all' ? null : options.status, label, tasks: sortTasks(kept) }]
}

/** The board's counts per status, unreadable files excluded. */
export function statusCounts(tasks: Task[]): Record<TaskStatus, number> {
  const counts = { backlog: 0, in_progress: 0, on_hold: 0, done: 0 } as Record<TaskStatus, number>
  for (const task of tasks) counts[task.status] += 1
  return counts
}

/** How many tasks are still open (everything not `done`). */
export function openTaskCount(tasks: Task[]): number {
  let open = 0
  for (const task of tasks) if (task.status !== 'done') open += 1
  return open
}

// ── The error envelope ────────────────────────────────────────────────────

/**
 * The sentence a `/api/tasks*` refusal carries.
 *
 * This surface answers `{"error": {"code", "message", "retryable"}}` rather than
 * the flat `{"error": "…"}` the rest of the API uses, so `apiErrorMessage` on its
 * own would hand back `[object Object]` — and a stale-revision 409 whose text is
 * `[object Object]` is the one refusal the user most needs to read. The envelope
 * is unwrapped here; everything else falls through to the shared reader.
 */
export function taskApiErrorMessage(error: unknown, fallback: string): string {
  const detail = errorPayload(error)?.error
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
    const code = (detail as { code?: unknown }).code
    if (typeof code === 'string' && code) return code
  }
  return apiErrorMessage(error, fallback)
}