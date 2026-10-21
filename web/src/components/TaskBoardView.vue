<script setup lang="ts">
/**
 * The workspace task board (`/tasks`).
 *
 * Four fixed columns — To do, In progress, In review, Done — side by side at every
 * width. A narrow pane scrolls them sideways inside the board; the page itself does
 * not scroll sideways. A card is dragged to another lane, or moved one lane over with
 * Shift+←/→ while its title has focus, at any width. The editor's status control is
 * the path a screen reader and a phone use: a card carries no status control of its
 * own, because its column already says where it is.
 *
 * Every write goes through `stores/taskBoard.ts` at the `revision` this pane read,
 * so a board drawn from an older read gets the server's 409 with its rows intact
 * instead of overwriting what is on disk now.
 *
 * **Delegation** starts in the editor's Agent block, where the preview is, and
 * adds an attempt badge, a link to the chat an attempt ran in, and the gestures
 * that own a turn — Stop and Detach for a live one, Resume and Retry for a
 * settled one, on the card's foot and in the editor. Four things this pane is careful not to imply:
 *
 * - A finished turn is a **badge**, never *Done*. `ready_for_review` sits in
 *   *In review* until approved, and only the user's own Done moves it to *Done*.
 * - The delegated turn is **attended**. The preview says so, and the server holds
 *   to it: an approval card the turn raises is an ordinary Needs-you card in that
 *   chat, not something swallowed because nobody was there.
 * - **Stop is not detach.** Stopping ends the turn; the task keeps its linkage and
 *   stays uncompletable until it is detached, so "stopped" never quietly becomes
 *   "the user may close this".
 *
 * The review loop is the rest of it: a **Ready to review** card's band offers
 * **Open chat**, because the user reads the result before closing it. The card's
 * dashed checkbox, the chat and the editor approve it, which is the user's completion —
 * not a Detach and then a Done, and not a gesture that would settle the attempt
 * as `stopped` and lose the answer. **History** reads
 * one task's attempts so a retried or stopped one is still readable, and a row
 * that disagrees with itself is **flagged with what disagrees and the controls
 * that resolve it** rather than silently rewritten.
 */
import SkeletonLoader from './SkeletonLoader.vue'
import TaskNoteFeed from './TaskNoteFeed.vue'
import { computed, inject, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import { DropdownMenuContent, DropdownMenuItem, DropdownMenuPortal, DropdownMenuRoot, DropdownMenuTrigger } from 'reka-ui'
import PaneHeader from './PaneHeader.vue'
import { useModalFocus } from '../composables/useModalFocus'
import { useProjectStore } from '../stores/projects'
import { useTaskBoardStore, type TaskChanges } from '../stores/taskBoard'
import { useTaskSignalsStore } from '../stores/taskSignals'
import { askConfirm, askConfirmReason, pendingConfirm } from '../lib/confirm'
import { cleanReason } from '../lib/decisionReasons'
import DecisionReason from './DecisionReason.vue'
import { renderUserMarkdown } from '../lib/safeMarkdown'
import {
  TASK_DUE_FILTERS,
  TASK_NO_PROJECT,
  TASK_STATUS_OPTIONS,
  cardAgentStatus,
  formatCardDue,
  formatCompletedAt,
  formatShortDate,
  invalidTaskRows,
  isLiveAttemptState,
  isOverdue,
  localDateKey,
  matchesDueFilter,
  matchesProjectFilter,
  openTaskCount,
  readableTasks,
  statusCounts,
  taskAttemptLabel,
  taskAttemptWhen,
  taskLanes,
  taskReconcileBadgeClass,
  taskReconcileLabel,
  taskReconcileNotes,
  titleSegments,
  splitTaskLog,
  joinTaskLog,
  type TaskDueFilter,
  type TaskReconcileNote,
} from '../lib/taskBoard'
import type { Task, TaskAttempt, TaskDetail, TaskStatus, TaskUpdate } from '../lib/types'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const projectStore = useProjectStore()
const board = useTaskBoardStore()
const workspace = computed(() => projectStore.activeWorkspace)

/**
 * Where the skeleton switches from column rows to card rows.
 *
 * Only the loading placeholder reads this. The lanes stay side by side at every
 * width and scroll inside the board, so no gesture is gated on it. The pane root is
 * a block that fills the `chat-pane` container, so observing it measures the pane;
 * the window keeps a listener off this component entirely, since the pane resizes
 * when the sidebar drags.
 */
const NARROW_PANE_PX = 940
const paneEl = ref<HTMLElement | null>(null)
const paneWidth = ref<number | null>(null)
const isNarrow = computed(() => paneWidth.value !== null && paneWidth.value <= NARROW_PANE_PX)

let paneObserver: ResizeObserver | null = null
onMounted(() => {
  const node = paneEl.value
  if (!node) return
  // jsdom has no ResizeObserver; the width stays null and the board draws its
  // columns, which is what a test reads.
  if (typeof ResizeObserver === 'undefined') return
  paneObserver = new ResizeObserver((entries) => {
    const entry = entries[entries.length - 1]
    if (entry) paneWidth.value = entry.contentRect.width
  })
  paneObserver.observe(node)
})
onBeforeUnmount(() => {
  paneObserver?.disconnect()
  paneObserver = null
})

// ── Load states ───────────────────────────────────────────────────────────
//
// Five states, each read off its own slot, so a failed or pending GET can never
// read as "this workspace has no tasks": a first load in flight, a first load
// that failed (the server's sentence and a Retry, never an empty board), a
// failed refresh over rows that are still on screen (keep them, mark them stale,
// offer Retry), a filter hiding a non-empty set (offer to clear it), and a
// genuinely empty set.

const firstLoad = computed(() => !board.loadedWorkspace && !board.loadError)
const loadFailed = computed(() => Boolean(board.loadError) && !board.loadedWorkspace)
const loadStale = computed(() => Boolean(board.loadError) && Boolean(board.loadedWorkspace))
const boardEmpty = computed(
  () => Boolean(board.loadedWorkspace) && !board.loadError && tasks.value.length === 0,
)

const tasks = computed(() => readableTasks(board.rows))
const unreadable = computed(() => invalidTaskRows(board.rows))
const counts = computed(() => statusCounts(tasks.value))
const openCount = computed(() => openTaskCount(tasks.value))

/**
 * Re-read the board: on mount, on a workspace switch, and after a refused write.
 *
 * On mount it is a refresh rather than a first load: coming back to the board
 * should show what an agent or `ciao task` did while the user was elsewhere, so
 * the rows stay on screen and only `loading` is set. The same call is the way
 * out of a 409 — the write was refused precisely because the record moved on, so
 * re-reading is the only correct next step.
 */
function load() {
  void board.reload(workspace.value)
}

/**
 * What the Retry and Reload buttons call.
 *
 * Identical to `load` plus clearing the message being recovered from. The
 * automatic reload above must *not* do that: it runs while the server's refusal
 * is the sentence the user still needs to read, and the re-read does not clear
 * the action slot.
 */
function reloadBoard() {
  board.clearError()
  load()
}

// Started in setup, not on mount: `reload` marks the read in flight before its
// first await, so the `/tasks/<id>` watcher below (immediate) waits for the fresh
// rows instead of judging a linked task against the previous visit's.
load()
// The engine announced a change (`tasks_changed`) and the app-wide signals
// re-read: an agent reported, a turn settled, `ciao task` wrote. Re-read the
// board too, so its columns agree with the sidebar count while it is open.
const taskSignals = useTaskSignalsStore()
watch(() => taskSignals.tasks, () => {
  if (taskSignals.loadedWorkspace === workspace.value) load()
})
// A switch is the one case that must not keep the old rows: `reload` drops them
// first, so the new workspace's own first-load and failed states are what shows.
//
// Every dialog closes with it, and that is a correctness point rather than a
// gesture one: `1`–`9` keep switching workspaces while the board is open, so an
// open editor would otherwise be editing a task id from the workspace the user
// just left, and its next Save would present that task's revision to a
// workspace that has never heard of it. The delegation preview is the same case
// with a worse outcome — its confirm would launch a turn in the workspace that
// was left, described by a task the new one has never seen.
watch(workspace, () => {
  closeCreate()
  resetResolution()
  void closeDetail({ discard: true })
  // Not `closeDelegate`: a gesture already in flight makes that one refuse, which
  // would leave the sheet marked open with nothing drawn and the focus trap still
  // holding. `resetDelegate` closes it either way, and the late answer is dropped
  // by the store's own `drawingWorkspace` guard.
  resetDelegate()
  load()
})

// ── Filters ───────────────────────────────────────────────────────────────

const statusFilter = ref<TaskStatus | 'all'>('all')
const projectFilter = ref('')
const dueFilter = ref<TaskDueFilter>('any')

const filtersActive = computed(
  () => statusFilter.value !== 'all' || !!projectFilter.value || dueFilter.value !== 'any',
)

function clearFilters() {
  statusFilter.value = 'all'
  projectFilter.value = ''
  dueFilter.value = 'any'
}

const filtered = computed(() =>
  tasks.value.filter(
    (task) =>
      matchesProjectFilter(task, projectFilter.value, generalId.value) && matchesDueFilter(task, dueFilter.value),
  ),
)

/** A filter hiding a non-empty set, offered the way back rather than an empty board. */
const filteredEmpty = computed(
  () =>
    !firstLoad.value
    && !loadFailed.value
    && !boardEmpty.value
    && tasks.value.length > 0
    && filtered.value.length === 0,
)

const lanes = computed(() =>
  taskLanes(filtered.value, { status: statusFilter.value }),
)
/**
 * Whether the four status lanes are drawn (no status filter), which is where drag
 * and Shift+←/→ move a card between columns. Independent of pane width: the lanes
 * scroll sideways instead of stacking, so the gestures hold at every width.
 */
const columnsShown = computed(() => lanes.value.length > 1)

// Done is the lane nobody needs open all day, so the unfiltered board keeps it
// behind a tab at the end of the row. The choice is remembered per browser.
const DONE_SHOWN_KEY = 'ciao.tasks.doneShown'

function readDoneShown(): boolean {
  try {
    return localStorage.getItem(DONE_SHOWN_KEY) === '1'
  } catch {
    return false
  }
}

const doneShown = ref(readDoneShown())
watch(doneShown, (shown) => {
  try {
    localStorage.setItem(DONE_SHOWN_KEY, shown ? '1' : '0')
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
})

const boardLanes = computed(() =>
  columnsShown.value && !doneShown.value ? lanes.value.filter((lane) => lane.status !== 'done') : lanes.value,
)
const doneLane = computed(() => lanes.value.find((lane) => lane.status === 'done') ?? null)
/** The tab's name: the badge's count (today's), and earlier ones only when there are some. */
const doneTabLabel = computed(() => {
  const lane = doneLane.value
  if (!lane) return ''
  return lane.earlierDone
    ? `Show done tasks (${lane.tasks.length} today, ${lane.earlierDone} earlier)`
    : `Show done tasks (${lane.tasks.length})`
})

/**
 * Open the Done lane from its tab. The lane may sit past the board's sideways
 * scroll, and the tab unmounts on click, so bring the lane into view and hand
 * focus to its Hide button rather than let it fall to the page.
 */
async function showDone() {
  doneShown.value = true
  await nextTick()
  const lane = paneEl.value?.querySelector<HTMLElement>('[data-lane="done"]')
  const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
  lane?.scrollIntoView?.({ inline: 'nearest', block: 'nearest', behavior: reduced ? 'auto' : 'smooth' })
  lane?.querySelector<HTMLElement>('.task-lane-hide')?.focus({ preventScroll: true })
}

/**
 * The workspace's auto-managed General project, which the board treats as the
 * same General as a task filed under no project: one name, one filter entry,
 * one option in the editor, rather than "General", "No project" and a second
 * "General" for the same place.
 */
const generalId = computed(() => projectStore.generalProject(workspace.value)?.project_id ?? '')

/** A task's project with the auto-managed General folded into `''`. */
function ownProject(projectId: string): string {
  return projectId === generalId.value ? '' : projectId
}

/**
 * The projects a task can name besides General: this workspace's own projects,
 * then any project id the board has seen that the registry no longer lists.
 *
 * That last group is load-bearing rather than tidy: a `<select>` with no matching
 * `<option>` renders blank, and a save would then send `project_id: null` and
 * quietly detach the task from a project that exists. Naming the unknown id is
 * the difference between "I cannot see that project" and "I cleared it".
 */
const projectOptions = computed(() => {
  const options: Array<{ value: string; label: string }> = []
  const listed = new Set<string>([generalId.value, TASK_NO_PROJECT])
  for (const project of projectStore.workspaceProjects) {
    if (listed.has(project.project_id)) continue
    listed.add(project.project_id)
    options.push({ value: project.project_id, label: project.name })
  }
  for (const task of tasks.value) {
    if (!task.project_id || listed.has(task.project_id)) continue
    listed.add(task.project_id)
    options.push({ value: task.project_id, label: `${task.project_id} (not in this workspace)` })
  }
  return options
})

/** The filter's list: General first, then the same projects. */
const filterProjectOptions = computed(() => [
  { value: TASK_NO_PROJECT, label: 'General' },
  ...projectOptions.value,
])

const projectName = (projectId: string): string => {
  if (!ownProject(projectId)) return 'General'
  return projectStore.workspaceProjects.find((p) => p.project_id === projectId)?.name || projectId
}

/**
 * The description this dialog may show and write back, or `null`.
 *
 * A list row carries no `body`, so the description is what `get` read for it, and
 * without one the field stays absent and a Save omits `body`: sending an empty
 * one would erase prose nobody was shown, which is the one failure an editor
 * cannot undo.
 *
 * It has to be the record at the revision the row carries, and those two
 * revisions have to be the same. The row is the newest read of what is on disk,
 * and a `described` slot left over from an earlier open is the record as it was
 * then. Another writer's edit moves the row's revision on while the slot keeps
 * the prose it was read with, and a body written at that newer revision goes out
 * with no 409 to stop it: the write succeeds and their prose is gone. So a stale
 * slot is not a prefill and never rides on a Save — {@link loadDescription} reads
 * it again, and the read is the only thing that can make it current.
 */
function heldDescription(task: Task | null): TaskDetail | null {
  const held = board.described
  if (!task || !held || held.id !== task.id) return null
  return held.revision === task.revision ? held : null
}

// ── Writes ────────────────────────────────────────────────────────────────

const busyTaskId = ref('')

/**
 * Move one card to another column.
 *
 * Moving *to* Done is the completion gesture, which is why it goes to `complete`
 * rather than through the status field: the store enforces completion as the
 * signed-in user's own act.
 */
async function moveTo(task: Task, next: TaskStatus) {
  if (next === task.status || busyTaskId.value === task.id) return
  const revision = board.revisionOf(task.id)
  board.clearError()
  if (!revision) return
  if (next === 'done') {
    openResolution(task, 'complete')
    return
  }
  busyTaskId.value = task.id
  await board.update(workspace.value, task.id, revision, { status: next })
  busyTaskId.value = ''
  // A refused write means the record moved on, so re-read rather than leave the
  // user holding a conflict they can only clear by switching workspace.
  if (board.error) load()
}

// ── Drag between columns ──────────────────────────────────────────────────
//
// Pointer only, and only while the four columns are drawn. Stacked groups use
// the editor's status controls. The id rides in the component rather than read
// back from `dataTransfer`, which browsers hide until the drop.

const dragTaskId = ref('')
const dropStatus = ref<TaskStatus | ''>('')

const dragTask = computed(() => tasks.value.find((task) => task.id === dragTaskId.value) ?? null)

function onDragStart(task: Task, event: DragEvent) {
  if (!columnsShown.value) { event.preventDefault(); return }
  dragTaskId.value = task.id
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    // Firefox starts no drag without data.
    event.dataTransfer.setData('text/plain', task.title)
  }
}

function onDragEnd() {
  dragTaskId.value = ''
  dropStatus.value = ''
}

function onDragOver(status: TaskStatus | null, event: DragEvent) {
  const task = dragTask.value
  if (!columnsShown.value || !task || !status || status === task.status) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  dropStatus.value = status
}

function onDragLeave(status: TaskStatus | null, event: DragEvent) {
  const into = event.relatedTarget as Node | null
  if (into && (event.currentTarget as HTMLElement).contains(into)) return
  if (dropStatus.value === status) dropStatus.value = ''
}

function onDrop(status: TaskStatus | null) {
  const task = dragTask.value
  onDragEnd()
  if (columnsShown.value && task && status) void moveTo(task, status)
}

/**
 * The keyboard's drag: Shift+←/→ on a focused card moves it one column over.
 *
 * Not Alt: Option+Arrow is the app-wide section switch. Only on the columns,
 * where left and right mean something on screen. Focus
 * follows the card into its new lane once the move has landed.
 */
async function onCardKeydown(task: Task, event: KeyboardEvent) {
  if (!event.shiftKey || event.altKey || event.metaKey || event.ctrlKey || !columnsShown.value) return
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
  const order = TASK_STATUS_OPTIONS.map((option) => option.status)
  const next = order[order.indexOf(task.status) + (event.key === 'ArrowLeft' ? -1 : 1)]
  if (!next) return
  event.preventDefault()
  await moveTo(task, next)
  await nextTick()
  const moved = paneEl.value?.querySelector<HTMLElement>(`[data-task-id="${task.id}"] .task-open`)
  // The destination lane may be off the edge of the board's sideways scroll.
  moved?.scrollIntoView?.({ inline: 'nearest', block: 'nearest' })
  moved?.focus()
}

/**
 * Free a task an attempt still holds, so Done can complete it.
 *
 * The store refuses to complete a task linked to an attempt — an open one
 * (running, waiting on the user) or a settled one still linked (interrupted,
 * failed, stopped). Approving a result waiting for review is the one case it
 * completes in a single gesture. For the rest Done is still what the user
 * asked for, so it asks once and detaches: the chat and its transcript stay,
 * only the link goes. Resolves whether the completion may go ahead.
 */
async function releaseForDone(task: Task): Promise<boolean> {
  if (!task.attempt_id) return true
  if (task.live_attempt_id && task.attempt_state === 'ready_for_review') return true
  const open = Boolean(task.live_attempt_id)
  const ok = await askConfirm(
    open
      ? 'The agent\'s attempt on this task is still open. Detach it and mark the task done? The chat and its transcript stay as they are.'
      : `This task is still linked to its last attempt (${taskAttemptLabel(task.attempt_state, task.attempt_outcome, task.attempt_detail)}). Unlink it and mark the task done? The chat and its transcript stay as they are.`,
    { title: 'Mark done', confirmLabel: open ? 'Detach and mark done' : 'Unlink and mark done' },
  )
  if (!ok) return false
  board.clearError()
  const detached = await board.attemptAction(
    workspace.value, task.id, task.live_attempt_id || task.attempt_id, 'detach',
  )
  return Boolean(detached)
}

function markDone(task: Task) {
  openResolution(task, 'complete')
}

/**
 * Which badge an attempt state wears.
 *
 * Four classes, because four things are being said: in flight, waiting on the user,
 * ready to be reviewed, and over. `needs_you` is deliberately the accent colour
 * rather than `running`'s quiet one — a paused turn needs the user, and a badge
 * that looks like every other running task would hide that.
 *
 * Takes a *state*, not a task, because a history row must wear its own attempt's
 * colour rather than the current attempt's — a `failed` retry under a
 * `ready_for_review` current one otherwise rendered as a green "Failed".
 */
function attemptStateBadgeClass(state: string): string {
  switch (state) {
    case 'failed':
    case 'interrupted':
      return 'badge--error'
    case 'needs_you':
      return 'badge--accent2'
    case 'ready_for_review':
      return 'badge--success'
    default:
      return 'badge--muted'
  }
}

/** The current attempt's badge: the state the task itself carries. */
function attemptBadgeClass(task: Task): string {
  return attemptStateBadgeClass(task.attempt_state)
}

// ── Delegation ──────────────────────────────────────────────────────────
//
// One gesture opens the preview, and the preview is where the decision is
// actually made: the task's own project or General, the model and permissions the
// chat will run with, and the fact that the turn is *attended* — an approval card
// it raises is an ordinary Needs-you card in that chat, not something swallowed
// because nobody was there.
//
// Every write here presents the revision the card was drawn at, so a board from an
// older read gets the server's 409 with the rows intact rather than delegating a
// description that has moved on.

type DelegateMode = 'delegate' | 'resume' | 'retry' | 'open_chat'

const delegateOpen = ref(false)
const delegateEl = ref<HTMLElement | null>(null)
const delegateBusy = ref(false)
const delegateTaskId = ref('')
/**
 * The description this preview will quote, read by `openDelegate`.
 *
 * A list row carries none, so this is the only way the preview can show the prose
 * that goes into the chat. It is a *separate* slot from the detail dialog's
 * description rather than a shared one: two dialogs can be open on two tasks, and
 * the store's single `described` slot is written by whichever read answered last —
 * sharing it here would let the preview show one task's prose under another's title.
 */
const delegateBody = ref('')
/**
 * The revision the description on screen was read at.
 *
 * `read.revision` from the same `board.get`, held for the length of the sheet. The
 * confirm presents *this* rather than whatever `revisionOf` says at the time it is
 * pressed, because those are two different facts: a reload, an adopted write or a
 * move from another tab can put a newer revision on the row while the sheet is
 * open, and the user has read the body at the older one. Sending the row's
 * revision would let a turn be launched from a description nobody saw, defeating
 * the 409 guard — so a body that has moved on is refused and the preview re-reads.
 */
const delegateRevision = ref('')
/** The description read is in flight, or failed and wants a retry. */
const delegateBodyState = ref<'idle' | 'loading' | 'failed'>('idle')
/** Ticket for the newest preview read; older answers are dropped. */
let delegateSeq = 0
/** What the sheet's confirm does, chosen when the sheet opens. */
const delegateMode = ref<DelegateMode>('delegate')
/**
 * The user's note for this one hand-over: how to go about it, what they expect
 * back. Sent with a new attempt (`delegate`, `retry`) and quoted after the
 * description; never saved on the task.
 */
const delegateInstructions = ref('')
/** The server's own bound, so the field stops where the request would be refused. */
const MAX_DELEGATE_INSTRUCTIONS = 4000
const delegateTakesInstructions = computed(
  () => delegateMode.value === 'delegate' || delegateMode.value === 'retry',
)

/** The task the preview is for, or undefined once it is gone. */
const delegateTask = computed(
  () => tasks.value.find((task) => task.id === delegateTaskId.value) ?? null,
)

/**
 * What the delegated turn will run with, named rather than implied.
 *
 * The provider and model are the operator's own workspace defaults — the board has
 * no reach into them, exactly as a webhook trigger has none — so the preview says
 * so rather than showing a value it cannot promise. The permission mode is the
 * chat's, taken from those defaults and *never* escalated: a delegated turn runs
 * with the default attendance, which is the whole reason an approval card it asks
 * for can be answered.
 */
const delegatePreview = computed(() => {
  const task = delegateTask.value
  if (!task) return null
  return {
    project: projectName(task.project_id),
    provider: 'Your workspace default',
    model: 'Your workspace default',
    // Named as the fact it is, since the client cannot read the chat's mode before
    // the chat exists.
    attendance: 'Attended — approval cards ask you in the chat',
  }
})

const delegateFocusActive = computed(
  () => delegateOpen.value && !pendingConfirm.value,
)

/**
 * The mode a sheet opens in, from the row alone.
 *
 * The four cases are four different things a card can need, and the old single
 * branch got two of them wrong. A **live** attempt (`running`, `needs_you`,
 * `ready_for_review`) is never resumable — the server refuses `resume` for every
 * live state — so offering "Continue this chat" was a button that always
 * errored; what a live attempt needs is to be *read*, in the chat it is running
 * in. A **settled** attempt still linked to the task (`failed`, `interrupted`,
 * `stopped`) is the only resumable case, and it is the one that had no UI at all:
 * the card showed a plain Delegate, which starts a new attempt in a new chat
 * rather than continuing the one that failed. So resume and retry are separate,
 * labelled gestures there, and neither is a second delegation.
 */
function delegateModeFor(task: Task): DelegateMode {
  if (task.live_attempt_id) return 'open_chat'
  // An archived chat cannot be resumed; a new one is handed what it did.
  if (isSettledLinked(task)) return chatArchived(task) ? 'retry' : 'resume'
  return 'delegate'
}

/**
 * Whether this row's attempt settled and is still linked: the resumable case.
 *
 * `attempt_id` names the attempt the gestures act on, and its state says whether it
 * is still live. The server sends `live_attempt_id` alongside as its own answer to
 * "does an attempt hold this task", and the two always agree — this reads the
 * state through the one place the vocabulary is written down rather than
 * re-deriving it here.
 */
function isSettledLinked(task: Task): boolean {
  return Boolean(task.attempt_id) && !isLiveAttemptState(task.attempt_state)
}

/**
 * What the confirm button says, which is the whole of the already-delegated case.
 *
 * Each mode names the gesture it will perform rather than a generic "Continue":
 * "Open chat" starts nothing, "Resume" continues one attempt's chat, "Start a new
 * attempt" mints another one, and "Delegate" is a first hand-over. A button whose
 * label could mean two of those is a button the user has to guess at.
 */
const delegateButtonLabel = computed(() => {
  if (delegateBusy.value) return 'Working…'
  switch (delegateMode.value) {
    case 'open_chat':
      return 'Open chat'
    case 'resume':
      return 'Resume'
    case 'retry':
      return 'Start a new attempt'
    default:
      return 'Delegate'
  }
})

/**
 * The description as it will read, when the dialog offers to show it.
 *
 * `renderUserMarkdown`, for the same reason the detail dialog uses it: the body is
 * prose the user wrote in their own vault, so raw HTML in it is escaped and shown
 * as typed rather than parsed.
 */
const delegateBodyPreview = computed(() => renderUserMarkdown(delegateBody.value))

/** Re-read the description without closing the preview. */
function retryDelegateBody() {
  const task = delegateTask.value
  if (task) void openDelegate(task, delegateMode.value)
}

useModalFocus(delegateEl, delegateFocusActive, {
  onEscape: () => { if (!delegateBusy.value) closeDelegate() },
})

/**
 * Open the delegation preview, and read the description it will quote.
 *
 * The read is not optional. A list row carries no `body`, so without it the
 * preview would say "your description is quoted into the chat" without showing the
 * description — which is exactly the decision the user is being asked to make. The
 * read is answered only while this dialog is still the one that asked, by the same
 * rule the detail dialog uses, so a read that lands after the user has moved on
 * writes nothing.
 *
 * Its revision is kept for the confirm rather than read from the row then: see
 * {@link delegateRevision}.
 */
async function openDelegate(task: Task, mode?: DelegateMode) {
  board.clearError()
  // A retry of the body read keeps what was typed; a new sheet starts empty.
  if (delegateTaskId.value !== task.id || !delegateOpen.value) delegateInstructions.value = ''
  delegateTaskId.value = task.id
  delegateMode.value = mode ?? delegateModeFor(task)
  delegateBody.value = ''
  delegateRevision.value = ''
  delegateBodyState.value = 'loading'
  delegateOpen.value = true
  const seq = ++delegateSeq
  const read = await board.get(workspace.value, task.id)
  if (seq !== delegateSeq || !delegateOpen.value || delegateTaskId.value !== task.id) {
    return
  }
  delegateBodyState.value = read ? 'idle' : 'failed'
  // Only the prose the board actually read. A `null` answer means either a refused
  // read or a write that landed in between, and neither may put stale prose in the
  // sheet the user is about to confirm.
  // The description alone: the history is handed over in its own section.
  delegateBody.value = read ? splitTaskLog(read.body).description : ''
  delegateRevision.value = read ? read.revision : ''
}

function closeDelegate() {
  if (delegateBusy.value) return
  resetDelegate()
}

/**
 * Put the preview back to its closed state, ignoring an in-flight gesture.
 *
 * {@link closeDelegate} refuses while a gesture is running, which is right for a
 * user pressing Cancel and wrong for a workspace switch: the watch that drops the
 * old rows would then leave the sheet marked open with nothing drawn and the focus
 * trap still holding. A gesture that lands after this is dropped by the store's own
 * `drawingWorkspace` guard, so closing early loses nothing but a stale answer.
 */
function resetDelegate() {
  delegateBusy.value = false
  delegateOpen.value = false
  delegateTaskId.value = ''
  delegateBody.value = ''
  delegateRevision.value = ''
  delegateBodyState.value = 'idle'
  delegateMode.value = 'delegate'
  delegateInstructions.value = ''
  // Invalidate any read still in flight, so its answer cannot fill a later dialog.
  delegateSeq++
  board.clearError()
}

/**
 * Confirm the sheet, which is four gestures rather than one.
 *
 * `open_chat` starts nothing and opens the chat the attempt is running in — the
 * honest action for a live attempt, which cannot be resumed and must not be
 * delegated a second time. `resume` continues that attempt's own chat under its
 * own id. `retry` is a fresh delegation, and `delegate` is the first hand-over;
 * both present the revision the description on screen was read at.
 */
async function submitDelegate() {
  const task = delegateTask.value
  if (delegateBusy.value || !task) return
  // The revision the *previewed description* was read at, not the row's newest: a
  // body that moved while the sheet was open must be refused, not delegated.
  const revision = delegateRevision.value
  if (delegateMode.value !== 'open_chat' && !revision) {
    resetDelegate()
    return
  }
  board.clearError()
  delegateBusy.value = true
  const originWorkspace = workspace.value
  const seq = delegateSeq
  let outcome: TaskAttempt | null = null
  if (delegateMode.value === 'open_chat') {
    delegateBusy.value = false
    openAttemptChat(task)
    resetDelegate()
    return
  }
  if (delegateMode.value === 'resume' && task.attempt_id) {
    outcome = await board.attemptAction(
      workspace.value, task.id, task.attempt_id, 'resume',
    )
  } else {
    outcome = await board.delegate(
      workspace.value, task.id, revision, undefined, delegateInstructions.value,
    )
  }
  if (seq !== delegateSeq || workspace.value !== originWorkspace) return
  delegateBusy.value = false
  if (!outcome) return
  board.clearError()
  // The turn is running; the badge now says so, and there is nothing left to
  // confirm. Closing is the honest end of the gesture.
  closeDelegate()
}

/** One lifecycle gesture on a card's attempt. */
async function actOnAttempt(
  task: Task,
  action: 'stop' | 'detach' | 'resume' | 'retry',
  reason?: string,
) {
  // A settled attempt still linked to its task is acted on through its own id,
  // which is the one `live_attempt_id` deliberately does not carry.
  const attemptId = task.live_attempt_id || task.attempt_id
  if (!attemptId || busyTaskId.value === task.id) return
  board.clearError()
  busyTaskId.value = task.id
  await board.attemptAction(workspace.value, task.id, attemptId, action, reason)
  busyTaskId.value = ''
  if (board.error) load()
}

/**
 * Release a result that is ready for review without approving it, with an
 * optional reason. The plain Detach stays one click; this is the variant that
 * asks "why" first (#1730), and the field is skippable.
 */
async function rejectResult(task: Task) {
  const { confirmed, reason } = await askConfirmReason(
    'Release this result without approving it? The chat and the agent\'s report stay; the task goes back to In progress.',
    { title: 'Reject result', confirmLabel: 'Reject' },
  )
  if (confirmed) await actOnAttempt(task, 'detach', reason)
}

/**
 * Open the chat an attempt ran in.
 *
 * `switchChat` on the project store, not a route push: that is the same call the
 * sidebar row and the in-app toast make, so the chat resolves, loads its history
 * and marks itself read the one way the app already has. A delegation-specific
 * navigation here would be a second path to a chat and would drift the first time
 * either changed.
 *
 * The board cannot know whether that chat still exists — chats get archived and
 * deleted routinely — so the button is drawn only for a chat the row actually
 * names, and `switchChat` handles a gone chat as it does everywhere else.
 */
function openAttemptChat(task: Task) {
  if (!task.chat_id) return
  void projectStore.switchChat(task.chat_id)
}

/** The title a linked chat is listed under; a chat the store no longer knows reads as "Chat". */
function linkedChatTitle(chatId: string): string {
  const chat = projectStore.chats.find((entry) => entry.chat_id === chatId)
  if (!chat) return 'Chat'
  return chat.archived ? `${chat.title} (archived)` : chat.title
}

function openLinkedChat(chatId: string) {
  void projectStore.switchChat(chatId)
}

const linkedChatsError = ref('')

/** Unlink one chat from the task. Linking is only ever a record, so this never touches status. */
async function unlinkChat(chatId: string) {
  const task = detailTask.value
  if (!task || detailSaving.value) return
  linkedChatsError.value = ''
  const saved = await board.linkChat(workspace.value, task.id, task.revision, chatId, true)
  if (!saved) {
    linkedChatsError.value = board.error || 'Could not update the linked chats.'
    board.clearError()
  }
}

// ── Review loop, history, reconcile ────────────────────────────────────────
//
// Three things the review needs that a badge cannot carry: where the result is,
// what was tried before, and whether this row agrees with itself.

//: The chats this browser can see, for the reconcile check below.
const knownChatIds = computed<ReadonlySet<string>>(
  () => new Set(projectStore.chats.map((chat) => chat.chat_id)),
)

/**
 * What this card disagrees with itself about.
 *
 * Flagged, never repaired: the board is the one place allowed to *say* a record
 * contradicts itself, and every case names the controls that resolve it. See
 * `taskReconcileNotes` for what is deliberately not a contradiction.
 */
function reconcile(task: Task): TaskReconcileNote[] {
  return taskReconcileNotes(task, { knownChatIds: knownChatIds.value })
}

/**
 * Whether this card's completion gesture is an **approval**, not a plain Done.
 *
 * `live_attempt_id` is the discriminator, not the badge: an approved or detached
 * card keeps reading `ready_for_review` forever, and offering the approve checkbox on a
 * result the user already approved would be a second completion gesture for the
 * same review.
 */
function isReviewReady(task: Task): boolean {
  return task.attempt_state === 'ready_for_review' && Boolean(task.live_attempt_id)
}

/** A result the user has not approved yet: the card's whole job is to send them to it. */
function awaitsReview(task: Task): boolean {
  return isReviewReady(task) && task.status !== 'done'
}

/** The date a card's meta line shows: when a done task finished, else when an open one is due. */
function cardDate(task: Task): string {
  if (task.status === 'done') return formatShortDate(task.completed_at)
  return formatCardDue(task.due, today)
}

/** The one action pinned to a card's agent band, or null when the state offers none. */
function bandAction(task: Task): { label: string; aria: string; run: () => void } | null {
  const status = cardAgentStatus(task)
  if (!status) return null
  if (status.tone === 'settled') {
    if (!isSettledLinked(task)) return null
    if (!chatArchived(task)) {
      return {
        label: 'Resume',
        aria: `Resume the attempt on ${task.title} in its own chat`,
        run: () => { void actOnAttempt(task, 'resume') },
      }
    }
    return {
      label: 'Continue in new chat',
      aria: `Start a new attempt at ${task.title}`,
      run: () => { void actOnAttempt(task, 'retry') },
    }
  }
  if (!task.chat_id) return null
  return {
    label: 'Open chat',
    aria: awaitsReview(task)
      ? `Review the agent's result for ${task.title} in its chat`
      : `Open the chat working on ${task.title}`,
    run: () => openAttemptChat(task),
  }
}

/** The attempts this board holds for one card, the live one first. */
function heldAttempts(task: Task): TaskAttempt[] {
  return board.attemptsTaskId === task.id ? board.attempts : []
}

// ── Attempts, inline in the editor ───────────────────────────────────────
//
// Every attempt at the open task, newest first, read when the editor opens on a
// task that was ever delegated (#1064). Lazy for the reason it always was: a
// board of fifty retried tasks must not read fifty histories nobody opened.

const detailAttempts = computed<TaskAttempt[]>(() =>
  detailTask.value ? heldAttempts(detailTask.value) : [],
)

/** Re-read the open task's attempts after a failed read. */
function retryHistory() {
  if (detailId.value) void board.ensureAttempts(workspace.value, detailId.value)
}

/**
 * Whether a task's or attempt's chat is archived.
 *
 * An archived chat still opens, read-only, from its vault transcript, but no
 * turn can run in it: Resume is not offered, and the way on is a new chat that
 * is handed what this one did. Two sources answer it, and each covers a gap in
 * the other. The server's `chat_archived` holds across a reload, when the chat
 * list no longer carries archived chats. The live chat list covers an archive
 * made in this session after the board loaded: archiving does not re-publish
 * the board's rows, so the flag is stale until the next load.
 */
function chatArchived(row: { chat_id: string; chat_archived?: boolean }): boolean {
  if (!row.chat_id) return false
  if (row.chat_archived === true) return true
  return projectStore.chats.some((chat) => chat.chat_id === row.chat_id && chat.archived)
}

/** Whether a card title carries a link, which the reader must be able to reach. */
function titleHasLinks(task: Task): boolean {
  return titleSegments(task.title).some((segment) => segment.href)
}

/**
 * What the card says under its badge: the agent's own summary, or, when it gave
 * none, the engine's note on why the attempt stopped where it did.
 */
function attemptNote(task: Task): string {
  if (!task.attempt_state) return ''
  return task.attempt_summary || (task.attempt_state === 'running' ? '' : task.attempt_detail)
}

// ── Create ────────────────────────────────────────────────────────────────

const createOpen = ref(false)
const createEl = ref<HTMLElement | null>(null)
const createTitleField = ref<HTMLInputElement | null>(null)
const createSaving = ref(false)
const createForm = reactive({ title: '', project_id: '', due: '', body: '' })
const createValid = computed(() => createForm.title.trim() !== '')

useModalFocus(createEl, createOpen, { initialFocus: createTitleField, onEscape: () => { if (!createSaving.value) closeCreate() } })

function openCreate() {
  board.clearError()
  createForm.title = ''
  createForm.project_id = ''
  createForm.due = ''
  createForm.body = ''
  createOpen.value = true
}

function closeCreate() {
  if (createSaving.value) return
  createOpen.value = false
  board.clearError()
}

async function submitCreate() {
  if (createSaving.value || !createValid.value) return
  createSaving.value = true
  const created = await board.create(workspace.value, {
    title: createForm.title,
    body: createForm.body,
    project_id: createForm.project_id,
    due: createForm.due,
  })
  createSaving.value = false
  // A refusal keeps the form open with what was typed; the server's sentence
  // sits under the buttons.
  if (!created) return
  createOpen.value = false
  board.clearError()
}

// ── Detail ────────────────────────────────────────────────────────────────

// ── Resolution ────────────────────────────────────────────────────────────
//
// A completion's note. Every Done gesture opens this sheet first. The note is
// optional: an empty or whitespace-only draft completes without a resolution, and
// nonblank text is sent as typed. Submit sends the status and the note in one
// POST, so a note never lands without the completion it belongs to. Cancel posts
// nothing. A failed write keeps the draft.

type ResolutionMode = 'complete' | 'edit'

const resolutionOpen = ref(false)
const resolutionEl = ref<HTMLElement | null>(null)
const resolutionField = ref<HTMLTextAreaElement | null>(null)
const resolutionMode = ref<ResolutionMode>('complete')
const resolutionTaskId = ref('')
const resolutionDraft = ref('')
const resolutionReason = ref('')
const resolutionError = ref('')
const resolutionBusy = ref(false)
/** The result of the last "Review resolution for learnings", said beside the detail. */
const reviewNote = ref('')
const reviewBusy = ref(false)

const resolutionTask = computed(() => tasks.value.find((task) => task.id === resolutionTaskId.value) ?? null)
const resolutionFocusActive = computed(() => resolutionOpen.value && !pendingConfirm.value)

useModalFocus(resolutionEl, resolutionFocusActive, {
  initialFocus: resolutionField,
  onEscape: () => { if (!resolutionBusy.value) closeResolution() },
})

const resolutionTitle = computed(() => {
  if (resolutionMode.value === 'edit') return 'Edit resolution'
  return resolutionTask.value && isReviewReady(resolutionTask.value)
    ? 'Approve with resolution'
    : 'Complete with resolution'
})
const resolutionSubmitLabel = computed(() => {
  if (resolutionMode.value === 'edit') return 'Save resolution'
  return resolutionTask.value && isReviewReady(resolutionTask.value) ? 'Approve done' : 'Mark done'
})

/** Open the sheet for one task. `draft` is what the field starts with. */
function openResolution(task: Task, mode: ResolutionMode, draft = '') {
  board.clearError()
  resolutionTaskId.value = task.id
  resolutionMode.value = mode
  resolutionDraft.value = draft
  resolutionReason.value = ''
  resolutionError.value = ''
  resolutionOpen.value = true
}

/**
 * Open the sheet to reword a done task's resolution.
 *
 * The field starts with the stored text, read first: a sheet opened empty over a
 * note that exists would let a blank Save erase it. A failed read opens nothing;
 * the board's own message says why.
 */
async function editResolution(task: Task) {
  const read = await board.get(workspace.value, task.id)
  if (!read) return
  openResolution(task, 'edit', read.resolution)
}

/** Close without writing. Refused while a write is in flight. */
function closeResolution() {
  if (resolutionBusy.value) return
  resetResolution()
}

function resetResolution() {
  resolutionOpen.value = false
  resolutionTaskId.value = ''
  resolutionDraft.value = ''
  resolutionReason.value = ''
  resolutionError.value = ''
}

/**
 * Submit the sheet: one write, at the revision read at the moment of the write.
 *
 * A linked task is released first, through the same confirm Done uses, because the
 * store refuses to complete a task an attempt still holds. The revision is read
 * after that release, since the release itself moves it. On any refusal the draft
 * stays and the server's sentence is shown, with Refresh task beside it.
 */
async function submitResolution() {
  const task = resolutionTask.value
  if (!task || resolutionBusy.value) return
  // Completion's note is optional; edit-resolution keeps the draft exactly as
  // typed, including an empty or whitespace-only note.
  const text = resolutionMode.value === 'complete' && !resolutionDraft.value.trim()
    ? ''
    : resolutionDraft.value
  // With Done collapsed the completed card leaves the view, and its focus with it:
  // read its lane neighbour now, before the write re-renders the lane.
  const handoff = resolutionMode.value === 'complete' && !doneShown.value && columnsShown.value
    ? focusHandoffFor(task.id)
    : null
  resolutionBusy.value = true
  resolutionError.value = ''
  board.clearError()
  let saved: TaskDetail | null = null
  try {
    if (detailOpen.value && detailId.value === task.id && !(await flushDetail())) {
      resolutionError.value = board.error || 'Could not save the task edits.'
      board.clearError()
      return
    }
    if (resolutionMode.value === 'complete' && !(await releaseForDone(task))) {
      resolutionError.value = board.error || ''
      board.clearError()
      return
    }
    const revision = board.revisionOf(task.id)
    if (!revision) {
      resolutionError.value = 'Refresh the task before saving this.'
      return
    }
    saved = resolutionMode.value === 'edit'
      ? await board.update(workspace.value, task.id, revision, { resolution: text })
      : await board.complete(
        workspace.value, task.id, revision, text,
        isReviewReady(task) ? cleanReason(resolutionReason.value) : undefined,
      )
  } finally {
    resolutionBusy.value = false
  }
  if (!saved) {
    resolutionError.value = board.error || 'Could not save the resolution.'
    board.clearError()
    return
  }
  const completed = resolutionMode.value === 'complete'
  const closeCompletedDetail = completed && detailOpen.value && detailId.value === saved.id
  if (detailOpen.value && detailId.value === saved.id) detailForm.status = saved.status
  resetResolution()
  if (handoff !== null && !closeCompletedDetail) {
    await nextTick()
    const target = (handoff && paneEl.value?.querySelector<HTMLElement>(`[data-task-id="${handoff}"] .task-open`))
      || paneEl.value?.querySelector<HTMLElement>('.task-done-toggle')
    target?.focus()
  }
  if (closeCompletedDetail) {
    await closeDetail()
    if (!detailOpen.value) {
      await nextTick()
      const card = paneEl.value?.querySelector<HTMLElement>(`[data-task-id="${saved.id}"] .task-open`)
      // Done collapsed: the card is out of view, so its lane neighbour or the Done tab takes focus.
      const near = handoff !== null
        ? (handoff && paneEl.value?.querySelector<HTMLElement>(`[data-task-id="${handoff}"] .task-open`))
          || paneEl.value?.querySelector<HTMLElement>('.task-done-toggle')
        : null
      const target = card ?? near ?? paneEl.value?.querySelector<HTMLElement>('.task-new')
      target?.scrollIntoView?.({ inline: 'nearest', block: 'nearest' })
      target?.focus()
    }
  }
  board.clearError()
}

/** The id of the card beside `taskId` in its lane (next, else previous), or '' when it is alone. */
function focusHandoffFor(taskId: string): string {
  const item = paneEl.value?.querySelector(`li[data-task-id="${taskId}"]`)
  const neighbour = item?.nextElementSibling ?? item?.previousElementSibling
  return neighbour?.getAttribute('data-task-id') ?? ''
}

/**
 * Refresh task: the record moved on under a conflict. Re-read the board and the
 * task, so the next write presents the current revision. The draft is not touched.
 */
async function refreshResolutionTask() {
  const id = resolutionTaskId.value
  if (!id || resolutionBusy.value) return
  resolutionError.value = ''
  await board.reload(workspace.value)
  const read = await board.get(workspace.value, id)
  if (!read) {
    resolutionError.value = board.error || board.loadError || 'Could not re-read the task.'
    board.clearError()
    return
  }
  board.clearError()
}

const progressDraft = ref('')
const progressError = ref('')
const progressBusy = ref(false)
const editingUpdateId = ref('')
const editingUpdateDraft = ref('')
const canAddProgress = computed(() => {
  const status = detailTask.value?.status
  return status === 'in_progress' || status === 'in_review'
})

function beginEditProgress(item: TaskUpdate) {
  editingUpdateId.value = item.id
  editingUpdateDraft.value = item.text
  progressError.value = ''
}

async function saveProgress(send: boolean) {
  const task = detailTask.value
  const text = progressDraft.value.trim()
  if (!task || !text || progressBusy.value) return
  const revision = board.revisionOf(task.id)
  if (!revision) {
    progressError.value = 'Refresh the task before saving this.'
    return
  }
  progressBusy.value = true
  progressError.value = ''
  board.clearError()
  const saved = await board.addUpdate(workspace.value, task.id, revision, text, send)
  progressBusy.value = false
  if (!saved) {
    progressError.value = board.error || 'Could not save the progress note.'
    board.clearError()
    return
  }
  progressDraft.value = ''
  board.clearError()
}

async function saveEditedProgress() {
  const task = detailTask.value
  const updateId = editingUpdateId.value
  const text = editingUpdateDraft.value.trim()
  if (!task || !updateId || !text || progressBusy.value) return
  const revision = board.revisionOf(task.id)
  if (!revision) {
    progressError.value = 'Refresh the task before saving this.'
    return
  }
  progressBusy.value = true
  progressError.value = ''
  board.clearError()
  const saved = await board.editUpdate(workspace.value, task.id, updateId, revision, text)
  progressBusy.value = false
  if (!saved) {
    progressError.value = board.error || 'Could not save the progress note.'
    board.clearError()
    return
  }
  editingUpdateId.value = ''
  board.clearError()
}

/** Queue the agent's read of a done task's resolution for learnings. */
async function reviewTaskResolution(task: Task) {
  const revision = board.revisionOf(task.id)
  if (!revision || reviewBusy.value) return
  board.clearError()
  reviewNote.value = ''
  reviewBusy.value = true
  const result = await board.reviewResolution(workspace.value, task.id, revision)
  reviewBusy.value = false
  if (!result) return
  reviewNote.value = result.queued
    ? 'Review queued.'
    : result.reason || 'The review was not queued.'
}

const detailOpen = ref(false)
/** A status write the server refused, said beside the control that made it. */
const statusError = ref('')
const detailEl = ref<HTMLElement | null>(null)
const detailTitleField = ref<HTMLTextAreaElement | null>(null)
const detailSaving = ref(false)
const detailId = ref('')
/** The description read is in flight, or failed and wants a retry. */
const descriptionState = ref<'idle' | 'loading' | 'failed'>('idle')
/** A successful Save, so the dialog says so rather than only going quiet. */
const savedAt = ref(0)
/**
 * The description reads rendered until the user asks to edit it. An empty one
 * has nothing to read, so it opens straight in the editor.
 */
const editingBody = ref(false)
const bodyEditorShown = computed(() => editingBody.value || !detailForm.body.trim())
const bodyField = ref<HTMLTextAreaElement | null>(null)

async function editBody() {
  editingBody.value = true
  await nextTick()
  bodyField.value?.focus()
}

/**
 * The rendered description is the editor's own surface: a click puts the caret
 * in it. A link still opens, and a drag that selected text is a copy, not an edit.
 */
function onBodyClick(event: MouseEvent) {
  if ((event.target as Element | null)?.closest('a')) return
  if (window.getSelection()?.toString()) return
  void editBody()
}

/** Leaving the field saves it and shows it rendered again, unless the save was refused. */
async function onBodyBlur() {
  if (await flushDetail()) editingBody.value = false
}

/**
 * The title wraps to as many lines as it needs rather than scrolling inside a
 * one-line box, so a long one is read whole. Enter still ends the edit.
 */
function fitTitle() {
  const field = detailTitleField.value
  if (!field) return
  field.style.height = 'auto'
  field.style.height = `${field.scrollHeight}px`
}
const detailForm = reactive({
  title: '',
  status: 'backlog' as TaskStatus,
  due: '',
  project_id: '',
  body: '',
})
watch(() => [detailOpen.value, detailForm.title], () => { void nextTick(fitTitle) })

/**
 * The detail dialog's focus and Escape handling, while a confirm is over it.
 *
 * `useModalFocus` claims Escape in the capture phase and stops propagation, so
 * with both up the *detail* dialog answered it — closing the editor behind a
 * delete confirm the user was still being asked about, and leaving the confirm
 * with no Escape of its own. The active ref is therefore false while the
 * confirm is pending: this dialog claims nothing, and the confirm above it owns
 * the key. `detailSaving` is guarded separately, since an in-flight write is
 * this dialog's own business and the confirm is not involved.
 */
const detailFocusActive = computed(
  () => detailOpen.value && !pendingConfirm.value && !resolutionOpen.value,
)

useModalFocus(detailEl, detailFocusActive, {
  initialFocus: detailTitleField,
  onEscape: () => { if (pendingConfirm.value || detailSaving.value) return; void closeDetail() },
})

/** The row a dialog is editing, or undefined once it has been deleted. */
const detailTask = computed(() => tasks.value.find((task) => task.id === detailId.value) ?? null)

/**
 * Whether this dialog is offering the description, and may therefore write it.
 *
 * Offered only while it is the record at the revision being presented — see
 * {@link heldDescription}. A slot the dialog has not re-read at this revision
 * keeps the field absent, which is what stops a Save from clearing prose nobody
 * was shown.
 */
const detailDescribed = computed(() => Boolean(heldDescription(detailTask.value)))

/** The open task's read, when it is the record at the row's revision: resolution, history, log. */
const detailHeld = computed(() => heldDescription(detailTask.value))

/** "Add" only when the read says there is no note; otherwise, or until read, "Edit". */
const resolutionEditLabel = computed(() =>
  detailHeld.value && !detailHeld.value.resolution ? 'Add resolution' : 'Edit resolution',
)

const detailValid = computed(() => detailForm.title.trim() !== '')

/**
 * The editor's delegation summary, read off the row it is editing.
 *
 * `detailTask` is the row, not the description slot, so the badge here is the
 * record's own current state: a delegation that started while this dialog was open
 * (the store adopts the answer onto the row) updates this without a second read.
 */
const detailAttemptState = computed(() => detailTask.value?.attempt_state ?? '')
const detailAttemptBadgeClass = computed(() =>
  detailTask.value ? attemptBadgeClass(detailTask.value) : 'badge--muted',
)
/**
 * What the editor's delegation control offers, from the row it is editing.
 *
 * The same four cases the preview's confirm reads, named the same way: a live
 * attempt is read in its chat rather than continued (the server refuses `resume`
 * for every live state), a settled attempt is continued or retried, and a task
 * nobody delegated is handed over.
 */
const detailDelegateLabel = computed(() => {
  const task = detailTask.value
  if (!task) return 'Delegate to agent'
  switch (delegateModeFor(task)) {
    case 'open_chat':
      return 'Open chat'
    case 'resume':
      return 'Resume'
    case 'retry':
      return 'Continue in a new chat'
    default:
      return 'Delegate to agent'
  }
})

/**
 * Read the description the list never carries, then fill the dialog from it.
 *
 * The answer is applied only while this dialog is still the one that asked:
 * `detailId` is cleared when it closes and set to whoever is open next, and a
 * late answer landing on a task the user has moved on to would put the previous
 * task's prose — and the failure state of a read they are no longer waiting for —
 * under their next edit. A dialog can also ask twice for the same task (close and
 * reopen, or a Retry behind a first answer still in flight), so the newest ask
 * wins over an older one for the same id too.
 */
let descriptionSeq = 0
async function loadDescription(taskId: string) {
  const seq = ++descriptionSeq
  descriptionState.value = 'loading'
  const read = await board.get(workspace.value, taskId)
  if (seq !== descriptionSeq || !detailOpen.value || detailId.value !== taskId) return
  if (read) {
    descriptionState.value = 'idle'
    detailForm.body = splitTaskLog(read.body).description
    return
  }
  // No answer: either the read was refused, or the board was told something newer
  // while it was in flight — a write of this dialog's own, say. The slot decides,
  // by the same rule as everywhere else in this dialog: a write's answer is the
  // record at the revision now presented, so a Save in the middle of a read keeps
  // the description the form was filled with, and a refused read has nothing to
  // offer and leaves the field absent.
  descriptionState.value = heldDescription(detailTask.value) ? 'idle' : 'failed'
}

/**
 * Open one task's editor.
 *
 * Every open reads. The description slot outlives both the dialog and the pane —
 * it is store state — so "the board already has this body" is a claim about an
 * instant that has passed: `ciao task`, an agent or a second browser can have
 * edited the task since, and the reload that reported it did not carry the prose.
 * Skipping the read on a slot is how a stale body gets written back at a fresh
 * revision, with no 409 to stop it.
 *
 * What the slot still buys is the form being filled the instant the dialog
 * appears rather than empty for the length of a GET, so a Save in that window
 * writes the other fields and leaves the description alone. It is a prefill, and
 * only while it is the record at the row's revision — {@link heldDescription}.
 */
function openDetail(task: Task) {
  statusError.value = ''
  board.clearError()
  detailId.value = task.id
  const held = heldDescription(task)
  detailForm.title = task.title
  detailForm.status = task.status
  detailForm.due = task.due
  detailForm.project_id = ownProject(task.project_id)
  detailForm.body = held ? splitTaskLog(held.body).description : ''
  savedAt.value = 0
  reviewNote.value = ''
  editingBody.value = false
  detailOpen.value = true
  syncTaskAddress(task.id)
  void loadDescription(task.id)
  if (task.attempt_state) {
    // Re-read on every open: a turn may have reported since the last read.
    board.invalidateAttempts()
    void board.ensureAttempts(workspace.value, task.id)
  }
}

/**
 * Close the editor, saving what is still pending first.
 *
 * A refused save keeps the dialog open: the server's sentence is in it, and
 * closing would leave the user believing an edit landed that did not.
 * `discard` is the workspace switch, where a pending edit belongs to the
 * workspace being left and must not be written into the one arrived at.
 */
async function closeDetail(options: { discard?: boolean } = {}) {
  if (!detailOpen.value) return
  if (options.discard || (!hasUnsavedEdits() && !autosaving.value)) {
    // Nothing to write: close now, so a dialog opened straight after is not
    // closed by this one finishing late.
    cancelAutosave()
  } else {
    if (detailSaving.value) return
    const closing = detailId.value
    if (!(await flushDetail())) return
    if (detailId.value !== closing) return
  }
  const closed = detailId.value
  detailOpen.value = false
  detailId.value = ''
  descriptionState.value = 'idle'
  descriptionSeq++
  board.clearError()
  // Only the address of the task that closed: one the user navigated to
  // while this dialog was still open is theirs to keep.
  if (routeTaskId.value === closed) syncTaskAddress('')
}

// ── Address ───────────────────────────────────────────────────────────────

/**
 * `/tasks/<id>` names the task open in the detail dialog.
 *
 * It is the address an agent links to from a chat or a note
 * (lib/appLinks.ts), so arriving at it opens that task once the board holding
 * it has loaded, and the dialog keeps it while open so the link can be copied
 * back out. Both directions use `replace`: opening and closing a dialog is not
 * a page the Back button should step through. Injected rather than
 * `useRoute()` so a bare mount (the component tests) renders without a router.
 */
const router = inject(routerKey, null)
const currentRoute = inject(routeLocationKey, null)
const routeTaskId = computed(() => {
  const id = currentRoute?.params.taskId
  return typeof id === 'string' ? id : ''
})
/** The linked task is not on this workspace's board. */
const missingTaskId = ref('')

function syncTaskAddress(taskId: string): void {
  if (!router || routeTaskId.value === taskId) return
  void router.replace(taskId ? { name: 'task-detail', params: { taskId } } : { name: 'tasks' })
}

watch(
  [routeTaskId, () => board.loadedWorkspace, tasks, () => board.loading],
  async () => {
    const id = routeTaskId.value
    if (!id || board.loadedWorkspace !== workspace.value) return
    if (detailOpen.value && detailId.value === id) return
    const task = tasks.value.find(t => t.id === id)
    if (!task) {
      // A refresh keeps the previous rows on screen; a task created since then
      // is only missing once the fresh read has answered.
      if (board.loading) return
      missingTaskId.value = id
      syncTaskAddress('')
      return
    }
    missingTaskId.value = ''
    // Another task's pending edit is written before this one replaces it.
    if (detailOpen.value) {
      await closeDetail()
      if (detailOpen.value) return
    }
    openDetail(task)
  },
  { immediate: true },
)

/**
 * The editor's status control is a move, written the moment it is pressed.
 *
 * Only Done is the completion gesture. Any other status is a plain move, and
 * routing it through `complete` would mark a task done while the user chose
 * "In review" — a write to the record, not just to this dialog.
 */
/**
 * The editor's delegation control.
 *
 * The preview is a dialog of its own, and two modal sheets on one layer draw
 * the later one on top: opened over the editor it sat behind it, and the user
 * saw only the backdrop darken. So the editor closes first — writing any edit
 * still waiting — and *Open chat*, which has nothing to confirm, goes straight
 * to the chat.
 */
async function delegateFromDetail(task: Task) {
  await closeDetail()
  if (detailOpen.value) return
  if (delegateModeFor(task) === 'open_chat') {
    openAttemptChat(task)
    return
  }
  void openDelegate(task)
}

async function setDetailStatus(next: TaskStatus) {
  if (!detailTask.value || next === detailForm.status || detailSaving.value) return
  // A field edit still waiting goes first, so the two writes do not race for
  // the same revision.
  if (!(await flushDetail())) return
  const task = detailTask.value
  if (!task) return
  statusError.value = ''
  if (next === 'done') {
    openResolution(task, 'complete')
    return
  }
  const revision = board.revisionOf(task.id)
  if (!revision) return
  board.clearError()
  detailSaving.value = true
  const saved = await board.update(workspace.value, task.id, revision, { status: next })
  detailSaving.value = false
  if (!saved) {
    statusError.value = board.error
    load()
    return
  }
  // Follow the record the write actually stored.
  detailForm.status = saved.status
  board.clearError()
}

/** Arrow keys walk the status radios, as a native radio group does. */
function onStatusKeydown(event: KeyboardEvent) {
  const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[event.key]
  if (!step) return
  event.preventDefault()
  const order = TASK_STATUS_OPTIONS.map((option) => option.status)
  const at = order.indexOf(detailForm.status)
  const next = order[(at + step + order.length) % order.length]!
  void setDetailStatus(next)
  void nextTick(() => {
    detailEl.value?.querySelector<HTMLElement>(`[data-status="${next}"]`)?.focus()
  })
}

/**
 * The fields the Save sends: only what this dialog changed.
 *
 * A `PATCH` writes every key it is given, so sending the form wholesale would
 * make every Save an edit — and a `project_id` the workspace no longer has is a
 * 400 `project_not_found`, which would block a title-only Save on a task whose
 * project was deleted underneath the board. Nothing changed and no description
 * was read means there is nothing to send at all.
 */
function detailChanges(task: Task): TaskChanges {
  const changes: TaskChanges = {}
  const title = detailForm.title.trim()
  if (title !== task.title) changes.title = title
  const due = detailForm.due || null
  if ((due ?? '') !== (task.due || '')) changes.due = due
  const project = detailForm.project_id || null
  if ((project ?? '') !== ownProject(task.project_id)) changes.project_id = project
  return changes
}

// ── Autosave ──────────────────────────────────────────────────────────────
//
// The editor has no Save button. A select or the date writes as soon as it
// changes; the title and the description write once typing pauses, and on
// blur, Enter and close. Every write is queued behind the one before it, so
// each presents the revision the previous one returned instead of racing it
// into a 409.

/** How long typing has to pause before the title or description is written. */
const AUTOSAVE_DELAY_MS = 700

let autosaveTimer: ReturnType<typeof setTimeout> | null = null
let detailWrites: Promise<unknown> = Promise.resolve()

/** Typing has scheduled a write that has not started yet. */
const autosavePending = ref(false)

function cancelAutosave() {
  if (autosaveTimer) clearTimeout(autosaveTimer)
  autosaveTimer = null
  autosavePending.value = false
}

function scheduleAutosave() {
  if (!detailOpen.value) return
  cancelAutosave()
  // Only an edit is pending: a fill that left the form equal to the record has
  // nothing to say "Saving…" about.
  autosavePending.value = hasUnsavedEdits()
  autosaveTimer = setTimeout(() => {
    autosaveTimer = null
    void flushDetail()
  }, AUTOSAVE_DELAY_MS)
}

/**
 * The body a save would write: the edited description with the engine's log,
 * as the record holds it, put back after it.
 */
function composedBody(stored: string): string {
  return joinTaskLog(detailForm.body, splitTaskLog(stored).log)
}

/** Whether the editor holds anything the record does not. */
function hasUnsavedEdits(): boolean {
  const task = detailTask.value
  if (!task) return false
  if (Object.keys(detailChanges(task)).length) return true
  const held = heldDescription(task)
  return Boolean(held) && held!.body !== composedBody(held!.body)
}

/**
 * Write whatever the editor holds that the record does not, now.
 *
 * Resolves `false` when the write was refused, so a caller about to close or
 * move on can stop and leave the server's sentence on screen.
 */
async function flushDetail(): Promise<boolean> {
  cancelAutosave()
  const run = detailWrites.then(() => saveDetail())
  detailWrites = run.catch(() => undefined)
  return (await run) !== false
}

// Programmatic fills (open, the description read) land here too; they leave the
// form equal to the record, so the write they schedule finds nothing to send.
watch(
  () => [detailForm.title, detailForm.body],
  () => scheduleAutosave(),
)
watch(
  () => [detailForm.due, detailForm.project_id],
  () => { if (detailOpen.value) void flushDetail() },
)
onBeforeUnmount(() => {
  if (autosaveTimer && detailOpen.value) void flushDetail()
  cancelAutosave()
})

/** A write in flight from the editor's fields, for the "Saving…" line. */
const autosaving = ref(false)

/**
 * Write the fields this editor changed. `false` only when the server refused.
 *
 * Nothing is written back into the form from the answer: the user may have
 * typed on while it was in flight, and the row the store adopted is what the
 * next comparison runs against.
 */
async function saveDetail(): Promise<boolean> {
  const task = detailTask.value
  if (!detailOpen.value || !task || !detailValid.value) return true
  // The revision is read first and the slot asked about *it* below:
  // `revisionOf` drops a slot that disagrees with the row, so the two questions
  // have to be about the same instant or the answer is about a different one.
  const revision = board.revisionOf(task.id)
  const changes = detailChanges(task)
  // `body` rides only when this dialog showed the prose and it is the record at
  // that revision: a description read for an earlier revision is not something
  // this dialog may write (its body is not what the row says is on disk, so
  // sending it erases whatever replaced it, and the newer revision going out
  // means no 409 stands in the way), an unread one is omitted rather than sent
  // empty, and an unedited one needs no write at all.
  const held = heldDescription(task)
  const body = held && held.revision === revision && held.body !== composedBody(held.body)
    ? composedBody(held.body)
    : undefined
  if (!Object.keys(changes).length && body === undefined) return true
  autosaving.value = true
  const saved = await board.update(
    workspace.value,
    task.id,
    revision,
    changes,
    body,
  )
  autosaving.value = false
  if (!saved) {
    load()
    return false
  }
  savedAt.value = Date.now()
  board.clearError()
  return true
}

async function deleteTask() {
  const task = detailTask.value
  if (!task || detailSaving.value) return
  cancelAutosave()
  const { confirmed, reason } = await askConfirmReason(
    `"${task.title}" will be deleted from this workspace. The record is a file in your vault and this unlinks it — there is no trash.`,
    { title: 'Delete task', confirmLabel: 'Delete', destructive: true },
  )
  if (!confirmed) return
  const revision = board.revisionOf(task.id)
  if (!revision) return
  board.clearError()
  detailSaving.value = true
  const removed = await board.remove(workspace.value, task.id, revision, reason)
  detailSaving.value = false
  if (removed) void closeDetail({ discard: true })
  // A refused delete is a stale revision or a file already gone; re-read so the
  // row on screen matches the disk.
  else load()
}

/** Re-read the description after a failed one, without closing the dialog. */
function retryDescription() {
  if (detailId.value) void loadDescription(detailId.value)
}

/**
 * The description as it reads, shown until the user presses Edit.
 *
 * `renderUserMarkdown`, not `renderMarkdown`: a task body is prose the user
 * edits in this very dialog, so raw HTML in it is escaped and shown as typed
 * rather than parsed. It still goes through DOMPurify.
 */
const bodyPreview = computed(() => renderUserMarkdown(detailForm.body))

const today = localDateKey()
</script>

<template>
  <div ref="paneEl" class="task-view">
    <PaneHeader page-tag="Tasks" @open-sidebar="emit('open-sidebar')">
      <template #actions>
        <button type="button" class="btn-primary btn-small task-new" @click="openCreate">New task</button>
      </template>
    </PaneHeader>

    <div class="page-grid page-grid--single">
      <div class="page-main">
        <!-- The filters sit outside the load branches: they describe what the
             board holds, not whether it has answered yet, so they are never the
             thing that disappears on a slow GET. -->
        <div class="task-filters">
          <div class="task-chips" role="group" aria-label="Filter by status">
            <button
              type="button"
              class="btn-chip task-chip"
              :class="{ active: statusFilter === 'all' }"
              :aria-pressed="statusFilter === 'all' ? 'true' : 'false'"
              @click="statusFilter = 'all'"
            >All <span class="task-chip-count">{{ tasks.length }}</span></button>
            <button
              v-for="option in TASK_STATUS_OPTIONS"
              :key="option.status"
              type="button"
              class="btn-chip task-chip"
              :class="{ active: statusFilter === option.status }"
              :aria-pressed="statusFilter === option.status ? 'true' : 'false'"
              @click="statusFilter = option.status"
            >{{ option.label }} <span class="task-chip-count">{{ counts[option.status] }}</span></button>
          </div>

          <div class="task-selects">
            <label class="sr-only" for="task-filter-project">Filter by project</label>
            <select id="task-filter-project" v-model="projectFilter" class="task-filter">
              <option value="">All projects</option>
              <option v-for="option in filterProjectOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
            <label class="sr-only" for="task-filter-due">Filter by due date</label>
            <select id="task-filter-due" v-model="dueFilter" class="task-filter">
              <option v-for="option in TASK_DUE_FILTERS" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
            <!-- One way back, never two: while the board is showing nothing
                 because of these filters, the empty state offers it more
                 prominently and this duplicate steps aside. -->
            <button
              v-if="filtersActive && !filteredEmpty"
              type="button"
              class="btn-chip task-chip"
              @click="clearFilters"
            >Clear filters</button>
          </div>
        </div>

        <!-- 1 · first load in flight -->
        <SkeletonLoader v-if="firstLoad" label="Loading tasks" :variant="isNarrow ? 'cards' : 'board'" :count="3" />

        <!-- 2 · first load failed: the server's sentence and a Retry. Never an
             "empty board" claim, which is what a failed GET would be read as. -->
        <div v-else-if="loadFailed" class="task-failed" role="alert">
          <p class="task-failed-text">{{ board.loadError }}</p>
          <button type="button" class="btn-small" @click="reloadBoard">Retry</button>
        </div>

        <template v-else>
          <!-- 3 · a refresh failed over rows that are still on screen: they stay,
               marked stale, with the same Retry. -->
          <p v-if="loadStale" class="task-stale" role="status">
            <span>{{ board.loadError }} These are the tasks as they were last read.</span>
            <button type="button" class="btn-chip task-chip" @click="reloadBoard">Retry</button>
          </p>

          <p v-if="missingTaskId" class="task-stale" role="status">
            <span>The linked task is not on the {{ workspace }} board. It may have been deleted, or it belongs to another workspace.</span>
            <button type="button" class="btn-chip task-chip" @click="missingTaskId = ''">Dismiss</button>
          </p>

          <p class="task-lede">
            {{ openCount }} open of {{ tasks.length }} in {{ workspace }}.
          </p>

          <!-- A file the server could not read as a task is shown, never dropped:
               an unreadable board is not an empty one. -->
          <section
            v-if="unreadable.length"
            class="task-unreadable"
            aria-labelledby="task-unreadable-title"
          >
            <h3 id="task-unreadable-title" class="section-title">
              {{ unreadable.length }} task file{{ unreadable.length === 1 ? '' : 's' }} could not be read
            </h3>
            <ul class="task-unreadable-list">
              <li v-for="row in unreadable" :key="row.path || row.id">
                <code class="task-unreadable-path">{{ row.path || row.id }}</code>
                <span class="badge badge--error">{{ row.code }}</span>
                <span class="task-unreadable-msg">{{ row.message }}</span>
              </li>
            </ul>
          </section>

          <!-- 4 · a filter hides a non-empty set. -->
          <div v-if="filteredEmpty" class="task-filtered-empty">
            <p class="hint">
              No task here matches these filters. {{ tasks.length }} task{{ tasks.length === 1 ? '' : 's' }} in
              {{ workspace }}.
            </p>
            <button type="button" class="btn-small" @click="clearFilters">Clear filters</button>
          </div>

          <!-- 5 · genuinely empty. -->
          <p v-else-if="boardEmpty" class="task-empty hint">
            No tasks in {{ workspace }} yet. Add one with New task, or file them
            from the command line with <code>ciao task</code>.
          </p>

          <!-- One card, one render path. The unfiltered board is its lanes side
               by side at every width, scrolling sideways inside the board, with
               Done behind a tab at the end; a status filter draws its one group
               at full width. -->
          <div
            v-else
            class="task-lanes"
            :class="lanes.length > 1 ? 'task-lanes--columns' : 'task-lanes--list'"
          >
            <section
              v-for="lane in boardLanes"
              :key="lane.status || 'all'"
              class="task-lane"
              :class="{ 'task-lane--drop': lane.status && dropStatus === lane.status }"
              :aria-label="lane.label"
              :data-lane="lane.status || undefined"
              @dragover="onDragOver(lane.status, $event)"
              @dragleave="onDragLeave(lane.status, $event)"
              @drop.prevent="onDrop(lane.status)"
            >
              <h3 class="task-lane-head">
                <span class="task-lane-label">{{ lane.label }}</span>
                <span class="task-lane-count">
                  <span class="badge badge--muted">{{ lane.tasks.length }}</span>
                  <button
                    v-if="columnsShown && lane.status === 'done'"
                    type="button"
                    class="task-lane-hide"
                    aria-label="Hide done tasks"
                    @click="doneShown = false"
                  >Hide</button>
                </span>
              </h3>
              <p v-if="!lane.tasks.length" class="task-lane-empty">
                {{ dragTask && lane.status && dragTask.status !== lane.status ? 'Drop here.' : lane.status === 'done' && lane.earlierDone ? 'Nothing done today.' : 'Nothing here.' }}
              </p>
              <ul v-else class="task-cards">
                <li
                  v-for="task in lane.tasks"
                  :key="task.id"
                  class="task-card"
                  :class="{ 'task-card--dragging': dragTaskId === task.id, 'task-card--done': task.status === 'done' }"
                  :data-task-id="task.id"
                  :draggable="columnsShown && busyTaskId !== task.id ? 'true' : 'false'"
                  @click="openDetail(task)"
                  @dragstart="onDragStart(task, $event)"
                  @dragend="onDragEnd"
                >
                  <div class="task-card-head">
                    <!-- Done is the one gesture every card offers, so it is the
                         card's own checkbox rather than a button in a row of them.
                         On a review-ready card it is the approval: a dashed ring. -->
                    <button
                      v-if="task.status !== 'done'"
                      type="button"
                      class="task-check"
                      :class="{ 'task-check--approve': isReviewReady(task) }"
                      :disabled="busyTaskId === task.id"
                      :aria-label="isReviewReady(task) ? 'Approve and mark done' : `Mark ${task.title} done`"
                      :title="isReviewReady(task) ? 'Approve and mark done' : 'Mark done'"
                      @click.stop="markDone(task)"
                    ><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 8.5l2.5 2.5L12 5.5" /></svg></button>
                    <span v-else-if="task.status === 'done'" class="task-check task-check--done" aria-hidden="true">
                      <svg viewBox="0 0 16 16"><path d="M4 8.5l2.5 2.5L12 5.5" /></svg>
                    </span>
                    <!-- The title as it reads, with any URL in it a real link. The
                         button that opens the card is the keyboard's way in (and
                         the drag's), kept out of sight: a link cannot live inside
                         a button, and the card's own click opens it for a pointer. -->
                    <p class="task-title" :aria-hidden="titleHasLinks(task) ? undefined : 'true'">
                      <template v-for="(segment, i) in titleSegments(task.title)" :key="i">
                        <a
                          v-if="segment.href"
                          :href="segment.href"
                          target="_blank"
                          rel="noopener noreferrer"
                          :title="segment.href"
                          draggable="false"
                          @click.stop
                        >{{ segment.text }}</a>
                        <template v-else>{{ segment.text }}</template>
                      </template>
                    </p>
                    <button
                      type="button"
                      class="task-open"
                      :aria-label="`Edit ${task.title}`"
                      :aria-keyshortcuts="columnsShown ? 'Shift+ArrowLeft Shift+ArrowRight' : undefined"
                      @click.stop="openDetail(task)"
                      @keydown="onCardKeydown(task, $event)"
                    >{{ task.title }}</button>
                    <!-- The run-managing gestures live in a menu, so the card never
                         sets Stop or Detach beside the way to open its chat. -->
                    <DropdownMenuRoot v-if="task.attempt_id || task.chat_id" :modal="false">
                      <DropdownMenuTrigger as-child>
                        <button
                          type="button"
                          class="task-more"
                          :aria-label="`Actions for ${task.title}`"
                          @click.stop
                        >&#8943;</button>
                      </DropdownMenuTrigger>
                      <DropdownMenuPortal>
                        <DropdownMenuContent
                          class="context-menu"
                          side="bottom"
                          align="end"
                          :side-offset="4"
                          :collision-padding="8"
                          position-strategy="fixed"
                        >
                          <DropdownMenuItem v-if="task.chat_id" as-child>
                            <button type="button" :disabled="busyTaskId === task.id" @click.stop="openAttemptChat(task)">Open chat</button>
                          </DropdownMenuItem>
                          <template v-if="!isReviewReady(task) && isSettledLinked(task)">
                            <DropdownMenuItem v-if="!chatArchived(task)" as-child>
                              <button type="button" :disabled="busyTaskId === task.id" @click.stop="actOnAttempt(task, 'resume')">Resume</button>
                            </DropdownMenuItem>
                            <DropdownMenuItem as-child>
                              <button type="button" :disabled="busyTaskId === task.id" @click.stop="actOnAttempt(task, 'retry')">{{ chatArchived(task) ? 'Continue in new chat' : 'Retry' }}</button>
                            </DropdownMenuItem>
                          </template>
                          <template v-else-if="!isReviewReady(task) && task.live_attempt_id">
                            <DropdownMenuItem as-child>
                              <button type="button" :disabled="busyTaskId === task.id" @click.stop="actOnAttempt(task, 'stop')">Stop</button>
                            </DropdownMenuItem>
                            <DropdownMenuItem as-child>
                              <button type="button" :disabled="busyTaskId === task.id" @click.stop="actOnAttempt(task, 'detach')">Detach</button>
                            </DropdownMenuItem>
                          </template>
                        </DropdownMenuContent>
                      </DropdownMenuPortal>
                    </DropdownMenuRoot>
                  </div>
                  <!-- Only what differs from the default: General and For me are what
                       most cards are, and saying so on each one buries the rest. The
                       lane heading already states the card's status. An overdue date
                       is red and says so in words for a screen reader. -->
                  <p
                    v-if="ownProject(task.project_id) || cardDate(task)"
                    class="task-meta"
                  >
                    <span v-if="ownProject(task.project_id)" class="task-project" :title="projectName(task.project_id)">{{ projectName(task.project_id) }}</span>
                    <span v-if="task.status === 'done' && task.attempt_outcome === 'done'" class="task-done-by">Done by agent</span>
                    <span
                      v-if="cardDate(task)"
                      class="task-due"
                      :class="{ 'task-due--over': isOverdue(task, today) }"
                      :title="isOverdue(task, today) ? 'Overdue' : undefined"
                    ><span v-if="isOverdue(task, today)" class="visually-hidden">Overdue: </span>{{ cardDate(task) }}</span>
                  </p>
                  <!-- Where the answer is, and when it ended. Never a summary of it:
                       the board holds no copy of the agent's reply, so it names the
                       chat to read and the approval that closes the card. -->
                  <!-- The agent's own words on where it got to, or the engine's
                       note when it said nothing. Clamped: the editor has the rest. -->
                  <p v-if="attemptNote(task) && task.status !== 'done'" class="task-agent-note">{{ attemptNote(task) }}</p>
                  <p v-if="chatArchived(task) && !isReviewReady(task)" class="task-changed">
                    The chat was archived. Continue in a new chat — it is handed what
                    this one did.
                  </p>
                  <!-- Inconsistency, said rather than repaired: the board may report
                       that a row disagrees with itself, never quietly rewrite it. -->
                  <div v-if="reconcile(task).length" class="task-reconcile">
                    <p
                      v-for="note in reconcile(task)"
                      :key="note.code"
                      class="task-reconcile-note"
                      role="status"
                    >
                      <span class="badge" :class="taskReconcileBadgeClass(note.code)" :data-code="note.code">{{ taskReconcileLabel(note.code) }}</span>
                      {{ note.text }}
                      <span class="task-reconcile-actions">{{ note.actions }}</span>
                    </p>
                  </div>
                  <!-- The agent's state in words, with the one action that state offers.
                       The rest of the run's gestures are in the card's menu. -->
                  <div
                    v-if="cardAgentStatus(task)"
                    class="task-agent-band"
                    :class="`task-agent-band--${cardAgentStatus(task)!.tone}`"
                  >
                    <span class="task-agent-status"><span class="task-agent-dot" aria-hidden="true" />{{ cardAgentStatus(task)!.word }}</span>
                    <button
                      v-if="bandAction(task)"
                      type="button"
                      class="task-agent-action"
                      :disabled="busyTaskId === task.id"
                      :aria-label="bandAction(task)!.aria"
                      @click.stop="bandAction(task)!.run()"
                    >{{ bandAction(task)!.label }} &rarr;</button>
                  </div>
                </li>
              </ul>
              <button
                v-if="lane.earlierDone"
                type="button"
                class="task-lane-more"
                @click="statusFilter = 'done'"
              >{{ lane.earlierDone }} done earlier · Show all</button>
            </section>
            <button
              v-if="columnsShown && !doneShown && doneLane"
              type="button"
              class="task-done-toggle"
              :class="{ 'task-done-toggle--drop': dropStatus === 'done' }"
              :aria-expanded="false"
              :aria-label="doneTabLabel"
              @click="showDone"
              @dragover="onDragOver('done', $event)"
              @dragleave="onDragLeave('done', $event)"
              @drop.prevent="onDrop('done')"
            >
              <span class="task-done-toggle-label">Done</span>
              <span class="badge badge--muted">{{ doneLane.tasks.length }}</span>
            </button>
          </div>

          <!-- A refused write keeps the rows it had and says the server's own
               sentence; the action slot is separate from the load slot so an
               unread 409 cannot be cleared by a later list read. The Reload is
               the way out: a conflict is the record moving on, and re-reading is
               how this board catches up. -->
          <p v-if="board.error" class="task-action-error" role="alert">
            {{ board.error }}
            <button type="button" class="btn-chip task-chip" @click="reloadBoard">Reload</button>
          </p>
        </template>
      </div>
    </div>

    <!-- Create -->
    <div v-if="createOpen" class="modal-backdrop" @click.self="closeCreate">
      <div
        ref="createEl"
        class="modal-sheet task-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-create-title"
      >
        <header class="task-sheet-head">
          <h3 id="task-create-title">New task</h3>
          <button type="button" class="btn-icon" aria-label="Close" @click="closeCreate">×</button>
        </header>
        <form class="task-form" novalidate @submit.prevent="submitCreate">
          <div class="form-group">
            <label for="task-create-name">Title</label>
            <input
              id="task-create-name"
              ref="createTitleField"
              v-model="createForm.title"
              type="text"
              autocomplete="off"
            />
          </div>
          <div class="form-grid">
            <div class="form-group">
              <label for="task-create-project">Project</label>
              <select id="task-create-project" v-model="createForm.project_id">
                <option value="">General</option>
                <option v-for="option in projectOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </option>
              </select>
            </div>
            <div class="form-group">
              <label for="task-create-due">Due</label>
              <input id="task-create-due" v-model="createForm.due" type="date" />
            </div>
          </div>
          <div class="form-group">
            <label for="task-create-body">Description</label>
            <textarea id="task-create-body" v-model="createForm.body" rows="4"></textarea>
            <p class="hint">Markdown. Optional.</p>
          </div>
          <div class="form-actions">
            <button
              type="submit"
              class="btn-primary"
              :disabled="!createValid || createSaving"
            >{{ createSaving ? 'Filing…' : 'File task' }}</button>
            <button type="button" class="btn-small" :disabled="createSaving" @click="closeCreate">Cancel</button>
            <p v-if="board.error" class="task-action-error" role="alert">{{ board.error }}</p>
          </div>
        </form>
      </div>
    </div>

    <!-- Delegate preview -->
    <div v-if="delegateOpen && delegateTask" class="modal-backdrop" @click.self="closeDelegate">
      <div
        ref="delegateEl"
        class="modal-sheet task-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-delegate-title"
      >
        <header class="task-sheet-head">
          <h3 id="task-delegate-title">Hand this task to the agent</h3>
          <button type="button" class="btn-icon" aria-label="Close" @click="closeDelegate">×</button>
        </header>

        <div class="task-form">
          <!-- The decision, stated before it is made: where the chat runs, what it
               runs with, and — the part a board cannot imply — that the turn is
               attended. An approval card it raises is an ordinary Needs-you card in
               that chat, answered in the ordinary way. -->
          <dl class="task-preview-facts">
            <div>
              <dt>Chat runs in</dt>
              <dd>{{ delegatePreview?.project }}</dd>
            </div>
            <div>
              <dt>Provider &amp; model</dt>
              <dd>{{ delegatePreview?.provider }} · {{ delegatePreview?.model }}</dd>
            </div>
            <div>
              <dt>Attendance</dt>
              <dd>{{ delegatePreview?.attendance }}</dd>
            </div>
          </dl>

          <!-- The description that goes into the chat, read by `openDelegate`
               because a list row carries none. While it is loading the sheet says
               so rather than showing an empty box: a user confirming a delegation
               against a description they were not shown is the one thing this
               preview exists to prevent. -->
          <p v-if="delegateBodyState === 'loading'" class="hint" role="status">
            Loading description…
          </p>
          <template v-else-if="delegateBodyState === 'failed'">
            <p class="hint">
              This board could not read this task's description, so it is not shown
              here. The chat still receives the record as it stands on disk.
            </p>
            <button type="button" class="btn-chip task-chip" @click="retryDelegateBody">
              Retry
            </button>
          </template>
          <template v-else>
            <p class="hint">
              {{
                delegateBody.trim()
                  ? 'The description below is quoted into the chat as the task.'
                  : 'This task has no description; only its title and metadata are handed over.'
              }}
            </p>
            <details v-if="delegateBody.trim()" class="task-preview">
              <summary>Show the description</summary>
              <div class="task-preview-body markdown" v-html="delegateBodyPreview"></div>
            </details>
          </template>

          <!-- The user's note for this hand-over. Its own field rather than an
               edit to the description: it shapes this attempt only, and the task
               stays as filed. -->
          <div v-if="delegateTakesInstructions" class="form-group">
            <label for="task-delegate-instructions">Instructions for the agent</label>
            <textarea
              id="task-delegate-instructions"
              v-model="delegateInstructions"
              rows="3"
              :maxlength="MAX_DELEGATE_INSTRUCTIONS"
              :disabled="delegateBusy"
              placeholder="How to go about it, what to avoid, what you expect when it's done"
            ></textarea>
            <p class="hint">Optional. Added after the description, for this attempt only.</p>
          </div>

          <p v-if="delegateMode === 'open_chat'" class="hint">
            This task's attempt is still live. Confirming opens the chat it is
            running in rather than starting a second turn.
          </p>
          <p v-else-if="delegateMode === 'resume'" class="hint">
            This task's last attempt ended. Resuming continues that attempt in its
            own chat; starting a new attempt leaves it as history.
          </p>
          <p v-else-if="delegateMode === 'retry'" class="hint">
            This starts a new attempt in a new chat. The previous attempt stays as
            history and is not re-run.
          </p>
          <p v-if="board.error" class="task-action-error" role="alert">
            {{ board.error }}
            <button type="button" class="btn-chip task-chip" @click="reloadBoard">Reload</button>
          </p>

          <div class="form-actions">
            <button
              type="button"
              class="btn-primary"
              :disabled="delegateBusy"
              @click="submitDelegate"
            >{{ delegateButtonLabel }}</button>
            <!-- Both ways out of a settled attempt, named separately: one
                 continues the chat that failed, the other starts a different one.
                 A single control here would have to be one of the two. -->
            <button
              v-if="delegateMode === 'resume'"
              type="button"
              class="btn-small"
              :disabled="delegateBusy"
              @click="openDelegate(delegateTask!, 'retry')"
            >Start a new attempt</button>
            <button type="button" class="btn-small" :disabled="delegateBusy" @click="closeDelegate">
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Detail. Nearly the whole window: the title and description are what the
         user came to read, so they get the width, and the fields that place the
         task (status, project, due) sit in a side column beside them. -->
    <div v-if="detailOpen" class="modal-backdrop task-detail-backdrop" @click.self="closeDetail()">
      <div
        ref="detailEl"
        class="modal-sheet task-sheet task-detail-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-detail-title"
      >
        <header class="task-sheet-head">
          <h3 id="task-detail-title">Edit task</h3>
          <!-- No Save: every field writes itself (see the Autosave section), and
               this line says where that stands. -->
          <p class="task-saved" role="status" aria-live="polite">{{
            !detailValid
              ? 'Add a title to save.'
              : autosaving || autosavePending
                ? 'Saving…'
                : savedAt
                  ? 'Saved'
                  : 'Changes save as you go.'
          }}</p>
          <button
            type="button"
            class="btn-small task-delete"
            :disabled="detailSaving"
            @click="deleteTask"
          >Delete</button>
          <button type="button" class="btn-icon" aria-label="Close" @click="closeDetail()">×</button>
        </header>

        <form class="task-form task-detail-form" novalidate @submit.prevent="flushDetail">
          <div class="task-detail-title">
            <label for="task-detail-name" class="sr-only">Title</label>
            <textarea
              id="task-detail-name"
              ref="detailTitleField"
              v-model="detailForm.title"
              class="task-title-field"
              rows="1"
              autocomplete="off"
              placeholder="Task title"
              @input="fitTitle"
              @keydown.enter.prevent="detailTitleField?.blur()"
              @blur="flushDetail"
            ></textarea>
          </div>

          <aside class="task-detail-side" aria-label="Task details">
            <!-- Status is a move, not a field to save: pressing one writes at once,
                 the same gesture as dragging the card. Done is the completion
                 gesture, which the store keeps as the user's own act. -->
            <div class="form-group">
              <span id="task-detail-status-label" class="task-field-label">Status</span>
              <div
                class="task-status-seg"
                role="radiogroup"
                aria-labelledby="task-detail-status-label"
                @keydown="onStatusKeydown"
              >
                <button
                  v-for="option in TASK_STATUS_OPTIONS"
                  :key="option.status"
                  type="button"
                  role="radio"
                  class="task-status-opt"
                  :data-status="option.status"
                  :aria-checked="detailForm.status === option.status ? 'true' : 'false'"
                  :tabindex="detailForm.status === option.status ? 0 : -1"
                  :disabled="detailSaving || board.saving"
                  @click="setDetailStatus(option.status)"
                >{{ option.label }}</button>
              </div>
              <p v-if="statusError" class="task-action-error" role="alert">{{ statusError }}</p>
            </div>
            <div class="form-group">
              <label for="task-detail-project">Project</label>
              <select id="task-detail-project" v-model="detailForm.project_id">
                <option value="">General</option>
                <option v-for="option in projectOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </option>
              </select>
            </div>
            <div class="form-group">
              <label for="task-detail-due">Due</label>
              <input
                id="task-detail-due"
                v-model="detailForm.due"
                type="date"
              />
            </div>
          </aside>

          <div class="task-detail-main">
          <!-- The description. A list row carries none, so it is read by id: the editor
               shows the prose it will write back, and while the read is in flight
               the field is absent rather than an empty box whose Save would clear
               what nobody was shown. -->
          <div class="form-group">
            <label v-if="bodyEditorShown" for="task-detail-body" class="task-field-label">Description</label>
            <span v-else id="task-detail-body-label" class="task-field-label">Description</span>
            <p v-if="descriptionState === 'loading'" class="hint" role="status">Loading description…</p>
            <template v-else-if="detailDescribed">
              <textarea
                v-if="bodyEditorShown"
                id="task-detail-body"
                ref="bodyField"
                v-model="detailForm.body"
                rows="10"
                @blur="onBodyBlur"
                placeholder="Markdown. What done looks like, links, notes."
              ></textarea>
              <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
              <div
                v-else
                class="task-body markdown"
                tabindex="0"
                aria-labelledby="task-detail-body-label"
                aria-describedby="task-detail-body-how"
                @click="onBodyClick"
                @keydown.enter.self.prevent="editBody"
                v-html="bodyPreview"
              ></div>
              <span v-if="!bodyEditorShown" id="task-detail-body-how" class="sr-only">Click or press Enter to edit.</span>
            </template>
            <p v-else class="hint">
              This board could not read this task's description, so it will not
              overwrite it.
              <button type="button" class="btn-chip task-chip" @click="retryDescription">Retry</button>
            </p>
          </div>

          <section class="task-progress" aria-labelledby="task-progress-title">
            <h4 id="task-progress-title" class="task-delegate-title">Activity</h4>
            <template v-if="canAddProgress">
              <div class="form-group task-note-composer">
                <label for="task-progress-text">Add a progress note</label>
                <textarea
                  id="task-progress-text"
                  v-model="progressDraft"
                  rows="3"
                  aria-describedby="task-progress-help"
                  :disabled="progressBusy"
                  placeholder="What moved, what's blocked, what you checked."
                ></textarea>
                <p id="task-progress-help" class="hint">Milestones and blockers. These notes are what the next resume and the completion memory pass read. They are not a copy of the chat.</p>
              </div>
              <div class="task-resolution-actions task-note-actions">
                <button type="button" class="btn-chip task-chip" :disabled="progressBusy || !progressDraft.trim()" @click="saveProgress(false)">Save note</button>
                <button
                  v-if="detailTask?.live_attempt_id"
                  type="button"
                  class="btn-chip task-chip"
                  :disabled="progressBusy || !progressDraft.trim()"
                  @click="saveProgress(true)"
                >Save and send to agent</button>
              </div>
            </template>
            <!-- A feed, newest first, under the box that adds to it: each note is a
                 bubble that names who wrote it and when, with its links live. -->
            <TaskNoteFeed v-if="detailHeld?.updates.length" :updates="detailHeld.updates" :editing-id="editingUpdateId">
              <template #actions="{ note }">
                <button
                  v-if="editingUpdateId !== note.id"
                  type="button"
                  class="btn-chip task-chip task-note-edit"
                  :disabled="progressBusy || detailSaving"
                  :aria-label="`Edit progress note from ${formatCompletedAt(note.recorded_at) || note.actor}`"
                  @click="beginEditProgress(note)"
                >Edit</button>
              </template>
              <template #editor="{ note }">
                <textarea v-model="editingUpdateDraft" rows="4" :disabled="progressBusy" :aria-label="`Edit progress note from ${note.actor}`"></textarea>
                <div class="task-resolution-actions task-note-actions">
                  <button type="button" class="btn-chip task-chip" :disabled="progressBusy" @click="editingUpdateId = ''">Cancel</button>
                  <button type="button" class="btn-chip task-chip" :disabled="progressBusy || !editingUpdateDraft.trim()" @click="saveEditedProgress">Save</button>
                </div>
              </template>
            </TaskNoteFeed>
            <p v-else-if="detailHeld" class="hint">No progress notes yet.</p>
            <p v-if="progressError" class="task-action-error" role="alert">{{ progressError }}</p>
          </section>

          <section v-if="detailTask?.status === 'done' || detailTask?.attempt_summary || detailHeld?.delegation_log" class="task-resolution" aria-labelledby="task-resolution-title">
            <!-- Before Done the block holds only what the agent left, so it is named for that. -->
            <h4 id="task-resolution-title" class="task-delegate-title">{{ detailTask?.status === 'done' ? 'Resolution' : 'Agent report' }}</h4>
            <template v-if="detailTask?.status === 'done'">
              <p class="hint">
                Completed{{ formatCompletedAt(detailTask.completed_at) ? ` ${formatCompletedAt(detailTask.completed_at)}` : ' (no time recorded)' }}.
              </p>
              <p v-if="detailHeld?.resolution" class="task-resolution-text">{{ detailHeld.resolution }}</p>
              <p v-else-if="detailHeld" class="hint">No resolution recorded.</p>
              <p v-else-if="descriptionState === 'loading'" class="hint" role="status">Loading resolution…</p>
              <p v-else class="hint">Open the task again to read its resolution.</p>
              <!-- A legacy done task has no completion record for an edit to reword
                   (#1189): `completed_at` is null exactly when there is none, so the
                   action row is left out rather than offering a refused write. -->
              <div v-if="detailTask.completed_at" class="task-resolution-actions">
                <button
                  type="button"
                  class="btn-chip task-chip"
                  :disabled="detailSaving || board.saving"
                  @click="editResolution(detailTask!)"
                >{{ resolutionEditLabel }}</button>
                <button
                  v-if="detailHeld?.resolution"
                  type="button"
                  class="btn-chip task-chip"
                  :disabled="reviewBusy || board.saving"
                  @click="reviewTaskResolution(detailTask!)"
                >Review resolution for learnings</button>
              </div>
              <p v-if="reviewNote" class="hint" role="status">{{ reviewNote }}</p>
              <div v-if="detailHeld && detailHeld.completions.length > 1" class="task-completions">
                <p class="task-field-label">Earlier completions</p>
                <ol aria-label="Earlier completions, newest first">
                  <li v-for="item in detailHeld.completions.slice(1)" :key="item.id" class="task-completion">
                    <span class="hint">{{ formatCompletedAt(item.completed_at) || 'No time recorded' }}</span>
                    <p class="task-resolution-text">{{ item.resolution || 'No resolution recorded.' }}</p>
                  </li>
                </ol>
              </div>
            </template>
            <!-- The agent's own report, read only. It is never copied into the
                 resolution field: the note is the user's words, the report is the
                 agent's. -->
            <div v-if="detailTask?.attempt_summary" class="task-agent-report">
              <p v-if="detailTask?.status === 'done'" class="task-field-label">Agent report</p>
              <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
              <div class="task-history-summary markdown" aria-label="Agent report" v-html="renderUserMarkdown(detailTask.attempt_summary)"></div>
            </div>
            <details v-if="detailHeld?.delegation_log" class="task-log-details">
              <summary>Delegation log</summary>
              <pre class="task-log">{{ detailHeld.delegation_log }}</pre>
            </details>
          </section>

          <section class="task-delegate" aria-labelledby="task-agent-title">
            <h4 id="task-agent-title" class="task-delegate-title">Agent</h4>
            <p v-if="!detailAttemptState" class="hint task-delegate-explain">
              Delegating starts a new chat in
              <strong>{{ projectName(detailForm.project_id) }}</strong> with this task as
              its prompt. You see what it will send first, the agent asks in that chat
              when it needs you, and the task only moves to Done when you approve it.
            </p>
            <p v-if="detailAttemptState" class="task-delegate-state">
              <span class="badge" :class="detailAttemptBadgeClass">{{
                taskAttemptLabel(detailAttemptState, detailTask?.attempt_outcome, detailTask?.attempt_detail)
              }}</span>
              <span v-if="detailTask && chatArchived(detailTask)" class="hint">
                The chat is archived; it opens read-only from the list below.
              </span>
            </p>
            <p v-if="detailTask && isReviewReady(detailTask)" class="task-review">
              <span class="task-review-lead">The agent says this is done.</span>
              Check its summary below and the chat, then approve Done to close the
              card.
            </p>
            <div
              v-if="detailTask && reconcile(detailTask).length"
              class="task-reconcile"
            >
              <p
                v-for="note in detailTask ? reconcile(detailTask) : []"
                :key="note.code"
                class="task-reconcile-note"
                role="status"
              >
                <span class="badge" :class="taskReconcileBadgeClass(note.code)" :data-code="note.code">{{ taskReconcileLabel(note.code) }}</span>
                {{ note.text }}
                <span class="task-reconcile-actions">{{ note.actions }}</span>
              </p>
            </div>
            <div class="task-delegate-actions">
              <button
                type="button"
                class="btn-chip task-chip task-delegate-go"
                :disabled="detailSaving || board.saving || !detailTask"
                @click="delegateFromDetail(detailTask!)"
              >{{ detailDelegateLabel }}</button>
              <!-- The one gesture that completes a reviewed result. The same call the
                   card's approve checkbox makes: the service releases the linkage and
                   closes the card together, so the attempt stays as review-ready
                   history instead of settling as stopped. -->
              <button
                v-if="detailTask && isReviewReady(detailTask) && detailTask.status !== 'done'"
                type="button"
                class="btn-chip task-chip"
                :disabled="detailSaving || board.saving"
                @click="markDone(detailTask!)"
              >Approve Done</button>
              <button
                v-if="detailTask && isReviewReady(detailTask)"
                type="button"
                class="btn-chip task-chip"
                :disabled="detailSaving || board.saving"
                @click.stop="rejectResult(detailTask!)"
              >Reject…</button>
              <!-- Deliberately not a second Resume/Retry pair here: this control
                   already opens the preview in the mode the row calls for, and the
                   preview names both ways out. A duplicate that opened a sheet and a
                   duplicate that acted immediately would be the same label on two
                   different gestures. The card's own foot carries the direct pair. -->
              <button
                v-if="detailTask?.live_attempt_id && !isReviewReady(detailTask!)"
                type="button"
                class="btn-chip task-chip"
                :disabled="detailSaving || board.saving"
                @click="actOnAttempt(detailTask!, 'stop')"
              >Stop</button>
              <button
                v-if="detailTask?.live_attempt_id && !isReviewReady(detailTask!)"
                type="button"
                class="btn-chip task-chip"
                :disabled="detailSaving || board.saving"
                @click="actOnAttempt(detailTask!, 'detach')"
              >Detach</button>
            </div>
            <!-- Every attempt at this task, newest first (#1064): which chat it
                 ran in, how it ended, and what the agent said it did. Replaces
                 the History sheet, so the record is where the decision is made. -->
            <template v-if="detailTask?.attempt_state">
              <p v-if="board.attemptsLoading && !detailAttempts.length" class="hint" role="status">Loading attempts…</p>
              <p v-else-if="board.attemptsError" class="task-action-error" role="alert">
                {{ board.attemptsError }}
                <button type="button" class="btn-chip task-chip" @click="retryHistory">Retry</button>
              </p>
              <ol v-else-if="detailAttempts.length" class="task-history" aria-label="Attempts, newest first">
                <li
                  v-for="attempt in detailAttempts"
                  :key="attempt.attempt_id"
                  class="task-history-row"
                >
                  <div class="task-history-head">
                    <span class="badge" :class="attemptStateBadgeClass(attempt.state)">
                      {{ taskAttemptLabel(attempt.state, attempt.outcome, attempt.detail) }}
                    </span>
                    <span class="task-history-when">{{ taskAttemptWhen(attempt) }}</span>
                    <button
                      v-if="attempt.chat_id"
                      type="button"
                      class="btn-chip task-chip"
                      :aria-label="`Open the chat attempt ${attempt.attempt_id} ran in`"
                      @click="openAttemptChat({ ...detailTask!, chat_id: attempt.chat_id })"
                    >{{ chatArchived(attempt) ? 'Archived chat' : 'Chat' }}</button>
                  </div>
                  <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
                  <div
                    v-if="attempt.summary"
                    class="task-history-summary markdown"
                    v-html="renderUserMarkdown(attempt.summary)"
                  ></div>
                  <p v-else-if="attempt.detail" class="task-history-summary">{{ attempt.detail }}</p>
                </li>
              </ol>
            </template>
          </section>

          <section v-if="detailTask?.linked_chats.length" class="task-linked-chats" aria-labelledby="task-linked-chats-title">
            <h4 id="task-linked-chats-title" class="task-delegate-title">Linked chats</h4>
            <ul class="task-linked-chat-list">
              <li v-for="chatId in detailTask.linked_chats" :key="chatId" class="task-linked-chat">
                <button type="button" class="btn-chip task-chip" :aria-label="`Open chat ${linkedChatTitle(chatId)}`" @click="openLinkedChat(chatId)">{{ linkedChatTitle(chatId) }}</button>
                <button type="button" class="btn-chip task-chip" :disabled="detailSaving" :aria-label="`Unlink chat ${linkedChatTitle(chatId)}`" @click="unlinkChat(chatId)">×</button>
              </li>
            </ul>
            <p v-if="linkedChatsError" class="hint" role="alert">{{ linkedChatsError }}</p>
          </section>

          <p v-if="board.error" class="task-action-error" role="alert">
            {{ board.error }}
            <button type="button" class="btn-chip task-chip" @click="reloadBoard">Reload</button>
          </p>
          </div>
        </form>
      </div>
    </div>

    <!-- Resolution sheet: one completion action, with an optional note. Cancel posts nothing. -->
    <div v-if="resolutionOpen && resolutionTask" class="modal-backdrop" @click.self="closeResolution">
      <div
        ref="resolutionEl"
        class="modal-sheet task-sheet task-resolution-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-resolution-sheet-title"
      >
        <header class="task-sheet-head">
          <h3 id="task-resolution-sheet-title">{{ resolutionTitle }}</h3>
          <button type="button" class="btn-icon" aria-label="Close" :disabled="resolutionBusy" @click="closeResolution">×</button>
        </header>
        <form class="task-form" novalidate @submit.prevent="submitResolution">
          <div class="form-group">
            <label for="task-resolution-text">Resolution (optional)</label>
            <p id="task-resolution-help" class="hint">How was this completed? What worked, and how did you check it?</p>
            <textarea
              id="task-resolution-text"
              ref="resolutionField"
              v-model="resolutionDraft"
              rows="6"
              aria-describedby="task-resolution-help"
              :disabled="resolutionBusy"
            ></textarea>
          </div>
          <DecisionReason
            v-if="resolutionMode === 'complete' && resolutionTask && isReviewReady(resolutionTask)"
            id-prefix="task-approve"
            v-model="resolutionReason"
            :disabled="resolutionBusy"
          />
          <div v-if="resolutionError" class="task-action-error" role="alert">
            <span>{{ resolutionError }}</span>
            <button type="button" class="btn-chip task-chip" :disabled="resolutionBusy" @click="refreshResolutionTask">Refresh task</button>
          </div>
          <div class="form-actions">
            <button
              type="submit"
              class="btn-primary"
              :disabled="resolutionBusy"
            >{{ resolutionBusy ? 'Saving…' : resolutionSubmitLabel }}</button>
            <button type="button" class="btn-small" :disabled="resolutionBusy" @click="closeResolution">Cancel</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* The pane root. It is also the element `NARROW_PANE_PX` observes: a block
   filling the `chat-pane` container, so its inline size IS the container's, and
   the one number both the JS lanes and the CSS breakpoint agree on. */
.task-view {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  overflow: hidden;
}
.task-view > .page-grid {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
}

/* ── Filters ───────────────────────────────────────────────────────────── */
.task-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-4) 0 var(--space-3);
}
.task-chips,
.task-selects {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-1);
}
.task-chip { gap: var(--space-1); }
.task-chip-count {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg3);
}
.task-chip.active .task-chip-count { color: inherit; }
.task-selects .task-filter { min-width: 0; max-width: 100%; }

/* A phone: the status chips are one row that scrolls sideways rather than two
   that wrap, and the two filters share one row instead of stacking. A native
   select is as wide as its longest option, so each one gets half the row and
   ellipsizes rather than pushing the page sideways. */
@container chat-pane (max-width: 940px) {
  .task-filters {
    flex-direction: column;
    align-items: stretch;
    flex-wrap: nowrap;
  }
  .task-chips {
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
    margin-inline: calc(-1 * var(--space-1));
    padding: 2px var(--space-1);
  }
  .task-chips::-webkit-scrollbar { display: none; }
  /* Grow to fill the row when they fit; never shrink, so a narrow phone
     scrolls the row instead of squeezing the labels. */
  .task-chips .task-chip { flex: 1 0 auto; justify-content: center; }
  .task-selects { flex-wrap: nowrap; }
  .task-selects .task-filter { flex: 1 1 0; }
  .task-selects .task-chip { flex: 0 0 auto; }
}
.task-filter {
  box-sizing: border-box;
  min-height: 34px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg);
  font-family: var(--font);
  font-size: var(--text-sm);
  cursor: pointer;
}

/* ── Load states ───────────────────────────────────────────────────────── */
.task-failed,
.task-filtered-empty {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-5) 0;
}
.task-failed-text,
.task-stale,
.task-action-error {
  margin: 0;
  color: var(--error);
  font-size: var(--text-sm);
  line-height: 1.5;
}
/* The action error carries a Reload beside it, so it lays out as a line rather
   than as prose with a button jammed into the sentence. */
.task-action-error {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0;
}
/* Muted small text with a Reload beside it: it is a caption on the board, not
   one of its voices. */
.task-lede {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin: 0 0 var(--space-3);
  color: var(--fg3);
  font-size: var(--text-sm);
}
.task-saved {
  margin: 0;
  min-width: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
}
.task-stale {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0;
}

/* ── Unreadable files ──────────────────────────────────────────────────── */
.task-unreadable {
  margin-bottom: var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-left: 3px solid var(--warning);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warning) 6%, var(--bg2));
}
.task-unreadable-list { margin: var(--space-2) 0 0; padding: 0; list-style: none; }
.task-unreadable-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
  padding: var(--space-1) 0;
  font-size: var(--text-sm);
}
.task-unreadable-path { color: var(--fg); }
.task-unreadable-msg { color: var(--fg2); min-width: 0; }

/* ── Board ─────────────────────────────────────────────────────────────── */
.task-lanes {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  /* The board owns the sideways scroll: the four lanes sit in one row at every
     width, and a narrow pane scrolls this element, not the page. */
  overflow-x: auto;
  overscroll-behavior-x: contain;
  -webkit-overflow-scrolling: touch;
  padding: var(--space-1) var(--space-1) var(--space-6);
}
/* One lane width at every pane width (no stretching, no 85% switch): a narrow
   pane scrolls the board sideways, a wide one leaves room after the last lane.
   310px keeps a card's state and its action on one line. */
.task-lanes { --task-lane-width: 310px; }
.task-lanes--columns > .task-lane { flex: 0 0 var(--task-lane-width); width: var(--task-lane-width); min-width: 0; }
.task-lanes--list > .task-lane { flex: 1 1 100%; min-width: 0; }

.task-lane {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  min-height: 8rem;
  padding: var(--space-1);
  border-radius: var(--radius);
  transition: background-color 120ms ease-out, box-shadow 120ms ease-out;
}
/* The lane a dragged card would land in. */
.task-lane--drop {
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 45%, transparent);
}
.task-lane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin: 0;
  padding-bottom: var(--space-1);
  border-bottom: 1px solid var(--border);
  font-size: calc(15px * var(--font-scale));
  font-weight: 650;
  letter-spacing: -0.01em;
}
.task-lane-count { display: inline-flex; align-items: center; gap: var(--space-2); }
.task-lane-hide {
  padding: 4px 0;
  border: 0;
  background: transparent;
  color: var(--fg3);
  font: inherit;
  font-size: var(--text-sm);
  font-weight: 400;
  letter-spacing: normal;
  cursor: pointer;
}
.task-lane-hide:hover { color: var(--fg); }
.task-lane-hide:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
@media (pointer: coarse) { .task-lane-hide { min-height: var(--touch); min-width: var(--touch); } }
/* Done, collapsed: a vertical tab that is also where a card can be dropped. */
.task-done-toggle {
  flex: 0 0 44px;
  align-self: stretch;
  min-height: 8rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border: 0;
  border-top: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg2);
  font: inherit;
  font-size: calc(15px * var(--font-scale));
  font-weight: 650;
  cursor: pointer;
  transition: background-color 120ms ease-out, box-shadow 120ms ease-out;
}
.task-done-toggle-label { writing-mode: vertical-rl; }
.task-done-toggle:hover { background: color-mix(in srgb, var(--fg) 5%, transparent); color: var(--fg); }
.task-done-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.task-done-toggle--drop {
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 45%, transparent);
}
.task-lane-more {
  align-self: flex-start;
  margin-top: var(--space-2);
  padding: 4px 0;
  border: 0;
  background: transparent;
  color: var(--fg3);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
.task-lane-more:hover { color: var(--fg); text-decoration-color: currentColor; }
.task-lane-more:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
@media (pointer: coarse) { .task-lane-more { min-height: var(--touch); } }
.task-lane-empty {
  margin: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
}
.task-cards {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.task-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  cursor: pointer;
  min-width: 0;
  /* The checkbox's net width in the head row (28px box with -6/-2 margins on
     a fine pointer, 44px with -14/-10 on touch: 20px either way) plus the head
     gap. Every text block under the title starts here; the ring's centre sits
     at --task-check-center. Keep in sync with .task-check's size and margins. */
  --task-text-inset: calc(20px + var(--space-2));
  --task-check-center: 8px;
}
.task-card:hover { border-color: var(--border-strong); }
.task-lanes--columns .task-card[draggable='true'] { cursor: grab; }
.task-card--dragging { opacity: 0.4; }
.task-card--done .task-title { color: var(--fg2); }
/* The card's Done. A round box rather than a square one: it reads as "tick
   this off", not as a form field. The hit area is the full touch target; the
   drawn ring sits inside it. */
/* Its margins feed --task-text-inset on .task-card; the touch block below
   restates them for 44px. */
.task-check {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  margin: 8px -2px 0 -6px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: none;
  color: transparent;
  cursor: pointer;
}
.task-check svg {
  width: 18px;
  height: 18px;
  border: 1.5px solid var(--fg3);
  border-radius: 50%;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: border-color 120ms ease-out, background-color 120ms ease-out, color 120ms ease-out;
}
button.task-check:hover svg,
button.task-check:focus-visible svg {
  border-color: var(--success);
  color: var(--success);
}
button.task-check:focus-visible { outline: 2px solid var(--accent); outline-offset: 0; }
button.task-check:disabled { cursor: progress; opacity: 0.5; }
.task-check--done { cursor: default; color: var(--bg2); }
.task-check--done svg { border-color: var(--success); background: var(--success); }
.task-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
  min-width: 0;
}
/* The title as it reads. Links in it open in a new tab; the rest of the card
   opens the editor. */
.task-title {
  flex: 1 1 auto;
  min-width: 0;
  margin: 0;
  padding: 10px 0;
  color: var(--fg);
  font-weight: 600;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.task-title a {
  color: var(--accent);
  text-decoration: underline;
  text-decoration-thickness: 1px;
  text-underline-offset: 2px;
  font-weight: 500;
}
.task-title a:hover { text-decoration-thickness: 2px; }
.task-title a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }
/* The card's keyboard control: out of sight, never out of reach. Its focus is
   drawn on the card, which is what it opens. */
.task-open {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
.task-card:has(.task-open:focus-visible) {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* The agent's own words above the band, clamped: the editor has the rest. */
.task-agent-note {
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
  white-space: pre-line;
  overflow-wrap: anywhere;
}
/* A caption about the card's chat (archived), not one of the card's voices. */
.task-changed {
  margin: 0;
  padding: var(--space-1) var(--space-2);
  border-left: 3px solid var(--warning);
  background: color-mix(in srgb, var(--warning) 8%, transparent);
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.5;
}
/* The review block is the card's own state, not a warning about it: it is where
   the answer lives and what closes the card. Kept beside the archived-chat
   caption rather than inside it. */
.task-review {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1) var(--space-2);
  margin: 0;
  padding: var(--space-1) var(--space-2);
  border-left: 3px solid var(--success);
  background: color-mix(in srgb, var(--success) 8%, transparent);
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.5;
}
.task-review-lead { font-weight: 600; color: var(--fg); }
.task-review-when { color: var(--fg3); }
/* An inconsistency the board reports rather than repairs. Distinct from the
   warning above: this one is about the row's own fields contradicting each
   other, and each line ends with the controls that resolve it. */
.task-reconcile { display: grid; gap: var(--space-1); }
.task-reconcile-note {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1) var(--space-2);
  margin: 0;
  padding: var(--space-1) var(--space-2);
  border-left: 3px solid var(--error);
  background: color-mix(in srgb, var(--error) 8%, transparent);
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.5;
}
.task-reconcile-actions { color: var(--fg3); }
.task-meta {
  display: flex;
  flex-wrap: nowrap;
  min-width: 0;
  align-items: center;
  gap: 0 var(--space-2);
  margin: 0;
  font-size: var(--text-sm);
  color: var(--fg3);
}
.task-meta > * { flex: none; white-space: nowrap; }
.task-meta > * + *::before {
  content: '\00b7';
  margin-right: var(--space-2);
  color: var(--border-strong);
}
.task-meta > .task-project {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* Hanging indent: the checkbox owns the gutter, so every text block under the
   title starts at the title's text column. Scoped to the card because the
   editor reuses .task-reconcile and .task-review at full width; the compound
   selector also outranks each block's own `margin: 0`. */
.task-card :is(.task-meta, .task-agent-note, .task-changed, .task-reconcile) {
  margin-inline-start: var(--task-text-inset);
}
.task-due--over { color: color-mix(in srgb, var(--error), var(--fg) 25%); }
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
/* The agent band: a footer strip that bleeds to the card's edges. It carries the
   agent's state on the left and the one action that state offers on the right. */
.task-agent-band {
  --band-tone: var(--fg2);
  display: flex;
  align-items: center;
  justify-content: space-between;
  /* The state word now starts at the text column, so a narrow lane can no
     longer hold word and action on one line: the action drops below, still
     right-aligned, rather than overlapping. */
  flex-wrap: wrap;
  gap: 0 var(--space-3);
  margin: 0 calc(-1 * var(--space-3)) calc(-1 * var(--space-3));
  padding: var(--space-1) var(--space-3);
  border-top: 1px solid var(--border);
  border-radius: 0 0 var(--radius) var(--radius);
  font-size: var(--text-sm);
}
.task-agent-band--running { --band-tone: color-mix(in srgb, var(--accent2) 60%, var(--fg)); }
.task-agent-band--needs {
  --band-tone: var(--warning);
  background: color-mix(in srgb, var(--warning) 6%, transparent);
}
.task-agent-band--review {
  --band-tone: var(--success);
  background: color-mix(in srgb, var(--success) 6%, transparent);
}
.task-agent-band--settled { --band-tone: var(--error); }
.task-agent-status {
  display: inline-flex;
  align-items: center;
  gap: 0;
  min-width: 0;
  white-space: nowrap;
  color: color-mix(in srgb, var(--band-tone), var(--fg) 25%);
}
.task-agent-band--running .task-agent-status { color: var(--fg2); }
.task-agent-dot {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--band-tone);
  /* The dot sits in the checkbox's gutter, under the ring; the word starts at
     the title's text column. The band's padding is the card's content edge. */
  margin-inline-start: calc(var(--task-check-center) - 3.5px);
  margin-inline-end: calc(var(--task-text-inset) - var(--task-check-center) - 3.5px);
}
.task-agent-band--running .task-agent-dot { animation: task-agent-pulse 1.6s ease-in-out infinite; }
@keyframes task-agent-pulse { 50% { opacity: 0.35; } }
@media (prefers-reduced-motion: reduce) {
  .task-agent-band--running .task-agent-dot { animation: none; }
}
.task-agent-action {
  flex: none;
  margin-inline-start: auto;
  min-height: 32px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
.task-agent-action:hover { text-decoration: underline; text-underline-offset: 3px; }
.task-agent-action:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }
.task-agent-action:disabled { cursor: progress; opacity: 0.5; }
/* The card's menu. Out of the way until the card is hovered, focused or the menu is open. */
.task-more {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  margin-top: 6px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-xs);
  background: none;
  color: var(--fg3);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
}
.task-card:hover .task-more,
.task-card:focus-within .task-more,
.task-more[data-state='open'] { opacity: 1; }
.task-more:hover { color: var(--fg); }
.task-more:focus-visible { outline: 2px solid var(--accent); outline-offset: 0; }
@media (hover: none) {
  .task-more { opacity: 1; }
}
.task-check--approve svg { border-color: var(--success); border-style: dashed; }

/* 44px on touch: the shared rule raises .btn-chip, and the native selects and
   buttons here carry it directly so a coarse pointer never gets a 34px target. */
@media (pointer: coarse) {
  .task-filter {
    min-height: var(--touch);
  }
  .task-more,
  .task-agent-action {
    min-height: var(--touch);
  }
  .task-more { width: var(--touch); }
  /* Net 20px again (44 - 14 - 10), so --task-text-inset holds. */
  .task-check {
    width: var(--touch);
    height: var(--touch);
    margin: 0 -10px 0 -14px;
  }
  /* The shared input rule is ~40px on touch, which every form in the app shares;
     the board's own fields hold the 44px line the rest of this pane does. */
  .task-form input,
  .task-form select,
  .task-form textarea {
    min-height: var(--touch);
  }
}

/* ── Dialogs ───────────────────────────────────────────────────────────── */
.task-sheet { padding: 0; }
.task-form textarea {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  max-height: 50dvh;
  resize: vertical;
}
/* The shared `.btn-icon` is already 44px square, but a scoped rule for the
   board's own headers wins on specificity over a change to the shared one, and
   the close button is the one target in the dialog a coarse pointer cannot miss
   if it is small. */
.task-sheet-head .btn-icon { min-width: var(--touch); min-height: var(--touch); }
.task-sheet-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border);
}
.task-sheet-head h3 {
  margin: 0;
  font-size: calc(15px * var(--font-scale));
  font-weight: 650;
}
/* The editor's agent block: its own band at the foot of the form, because it
   acts on a different thing — a turn in another chat — than the fields above
   edit. */
.task-delegate {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border);
}
.task-delegate-title {
  margin: 0;
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.task-linked-chat-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  list-style: none;
  padding: 0;
  margin: 0;
}
.task-linked-chat { display: inline-flex; gap: var(--space-1); }
.task-delegate-explain { max-width: 62ch; line-height: 1.5; }
.task-delegate-explain strong { color: var(--fg); font-weight: 600; }
.task-delegate-state {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
}
.task-delegate-state .hint { margin: 0; }
.task-delegate-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.task-delegate-go {
  border-color: color-mix(in srgb, var(--accent2) 55%, var(--border));
  color: var(--fg);
}
.task-delegate-go:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent2) 18%, var(--bg2));
}
/* Field captions that are not <label>s (a radio group, a rendered body) wear
   the shared label's look. */
.task-field-label {
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
/* Status: four segments, one pressed. */
.task-status-seg {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}
.task-status-opt {
  min-width: 0;
  min-height: 32px;
  padding: 4px 6px;
  border: 0;
  border-radius: 4px;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}
.task-status-opt:hover:not(:disabled) { background: var(--bg3); color: var(--fg); }
.task-status-opt[aria-checked='true'] {
  background: var(--bg3);
  color: var(--fg);
  font-weight: 600;
  box-shadow: inset 0 0 0 1px var(--border-strong);
}
.task-status-opt[data-status='done'][aria-checked='true'] {
  color: var(--success);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--success) 50%, transparent);
}
.task-status-opt:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.task-status-opt:disabled { cursor: progress; }
/* Must stay below the base .task-status-opt rule it overrides. */
@media (pointer: coarse) { .task-status-opt { min-height: var(--touch); } }
@media (max-width: 420px) {
  .task-status-opt { padding-inline: 2px; font-size: var(--text-xs); }
}
/* The description as it reads, and the surface a click edits. */
.task-body {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  font-size: var(--text-sm);
  line-height: 1.6;
  overflow-wrap: anywhere;
  cursor: text;
  transition: border-color 120ms var(--ease);
}
.task-body:hover { border-color: var(--border-strong); }
.task-body:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.task-body :deep(p),
.task-body :deep(ul),
.task-body :deep(ol) { margin: 0 0 0.6em; }
.task-body :deep(ul),
.task-body :deep(ol) { padding-left: 1.4em; }
.task-body :deep(p:last-child),
.task-body :deep(ul:last-child),
.task-body :deep(ol:last-child) { margin-bottom: 0; }
.task-body :deep(pre) { overflow-x: auto; }
/* The delegation preview's facts. A definition list rather than a table because
   each row is a label and one value, read in order down the sheet — a two-column
   grid of a label and a value would invite reading across rows as columns. */
.task-preview-facts {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
}
.task-preview-facts > div {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
}
.task-preview-facts dt {
  min-width: 8.5rem;
  color: var(--fg3);
  font-size: var(--text-sm);
}
.task-preview-facts dd {
  flex: 1 1 12rem;
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  font-size: var(--text-sm);
}
.task-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  overflow-y: auto;
}
/* The shared `.form-grid` is `1fr 1fr`, and a `1fr` track's min is its content:
   a native select sized by its longest option pushed the second column past the
   sheet's own edge. `minmax(0, 1fr)` is the same two columns without that — the
   unbreakable-child trap, in a grid. */
.task-form .form-grid { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
.task-form .hint { margin: 0; }
.task-preview { margin-top: var(--space-2); }
.task-preview-facts + .task-preview,
.task-preview-facts + .form-actions { margin-top: var(--space-2); }
.task-preview summary {
  color: var(--fg2);
  font-size: var(--text-sm);
  cursor: pointer;
}
/* The same reading rules as the detail dialog's `.task-body`: without an indent
   an ordered list's numbers hang outside the box. */
.task-preview-body {
  max-height: 18rem;
  overflow-y: auto;
  margin-top: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  font-size: var(--text-sm);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.task-preview-body :deep(p:first-child) { margin-top: 0; }
.task-preview-body :deep(p),
.task-preview-body :deep(ul),
.task-preview-body :deep(ol) { margin: 0 0 0.6em; }
.task-preview-body :deep(ul),
.task-preview-body :deep(ol) { padding-left: 1.4em; }
.task-preview-body :deep(li > ul),
.task-preview-body :deep(li > ol) { margin: 0.2em 0 0; }
.task-preview-body :deep(p:last-child),
.task-preview-body :deep(ul:last-child),
.task-preview-body :deep(ol:last-child) { margin-bottom: 0; }
.task-preview-body :deep(pre) { overflow-x: auto; }
.task-preview-body :deep(code) {
  padding: 1px 4px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--fg) 7%, transparent);
  font-family: var(--font-mono);
  font-size: 0.88em;
}
.task-preview-body :deep(pre code) { padding: 0; background: none; }
/* Attempt history. A list of records rather than a table: the columns a table
   would give are the badge, one sentence and a chat link, and at a phone width a
   table is where that stops being readable. */
.task-history {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}
.task-history-row {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.task-history-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.task-history-head .task-chip { margin-left: auto; }
.task-history-when { color: var(--fg3); font-size: var(--text-xs); }
.task-history-summary :deep(ul),
.task-history-summary :deep(ol) { margin: 0.3em 0; padding-left: 1.3em; }
.task-history-summary :deep(p) { margin: 0 0 0.4em; }
.task-history-summary :deep(p:last-child) { margin-bottom: 0; }
.task-history-summary {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

/* ── Resolution ────────────────────────────────────────────────────────── */
.task-resolution {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border);
}
.task-resolution-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.task-resolution-text {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.task-completions { display: flex; flex-direction: column; gap: var(--space-2); }
.task-completions ol { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: var(--space-2); }
.task-completion { display: flex; flex-direction: column; gap: var(--space-1); }
.task-agent-report { display: flex; flex-direction: column; gap: var(--space-1); }
.task-log-details summary { cursor: pointer; color: var(--fg3); font-size: var(--text-sm); }
.task-log {
  margin: var(--space-2) 0 0;
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (pointer: coarse) {
  .task-resolution .task-chip,
  .task-resolution-sheet .btn-primary,
  .task-resolution-sheet .btn-small,
  .task-resolution-sheet .task-chip { min-height: var(--touch); }
}
/* ── Task editor ─────────────────────────────────────────────────────────
   Nearly the whole window. The main column carries the title, description,
   activity and agent; a narrow side column holds what places the task. */
.task-detail-backdrop { padding: var(--space-5); }
.task-detail-sheet {
  max-width: 1120px;
  height: 100%;
  max-height: none;
  overflow: hidden;
}
.task-detail-sheet .task-sheet-head {
  gap: var(--space-3);
  padding: var(--space-2) var(--space-2) var(--space-2) var(--space-4);
}
.task-detail-sheet .task-sheet-head h3 {
  color: var(--fg2);
  font-size: var(--text-sm);
  font-weight: 600;
}
.task-detail-sheet .task-sheet-head h3 { flex: none; white-space: nowrap; }
.task-detail-sheet .task-saved {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--text-xs);
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* Delete is rare and confirmed, so it stays quiet until pointed at. */
.task-detail-sheet .task-delete {
  border-color: transparent;
  background: none;
  color: var(--fg3);
}
.task-detail-sheet .task-delete:hover:not(:disabled),
.task-detail-sheet .task-delete:focus-visible {
  border-color: color-mix(in srgb, var(--error) 45%, transparent);
  color: var(--error);
}
.task-detail-form {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 272px;
  grid-template-rows: auto 1fr;
  grid-template-areas:
    'title side'
    'main  side';
  align-content: start;
  column-gap: var(--space-6);
  row-gap: var(--space-4);
  padding: var(--space-5) var(--space-6) var(--space-6);
}
.task-detail-title { grid-area: title; min-width: 0; }
.task-detail-main {
  grid-area: main;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}
.task-detail-side {
  grid-area: side;
  align-self: start;
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--bg) 55%, var(--bg2));
}
/* The title as a heading that is also its field: it wraps instead of scrolling. */
.task-detail-form textarea.task-title-field {
  display: block;
  width: calc(100% + 2 * var(--space-2));
  max-height: none;
  margin: 0 calc(-1 * var(--space-2));
  padding: var(--space-1) var(--space-2);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: calc(22px * var(--font-scale, 1));
  font-weight: 650;
  line-height: 1.3;
  letter-spacing: -0.015em;
  text-wrap: balance;
  resize: none;
  overflow: hidden;
}
.task-title-field:hover { border-color: var(--border); }
.task-title-field:focus { border-color: var(--accent); background: var(--bg); outline: none; }
.task-title-field::placeholder { color: var(--fg3); }
/* Status reads down the side column, one row per lane. */
.task-detail-side .task-status-seg { grid-template-columns: 1fr; }
.task-detail-side .task-status-opt {
  min-height: 34px;
  padding: 6px 10px;
  text-align: left;
}
/* The side column's rule above outranks the generic one, so it needs its own. */
@media (pointer: coarse) { .task-detail-side .task-status-opt { min-height: var(--touch); } }
.task-detail-main textarea#task-detail-body { min-height: 14rem; }
.task-detail-main .task-resolution,
.task-detail-main .task-delegate,
.task-detail-main .task-linked-chats {
  padding-top: var(--space-4);
}
.task-progress {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding-top: var(--space-4);
  border-top: 1px solid var(--border);
}
.task-progress .form-group { margin: 0; }
.task-note-composer { margin: 0; }
.task-note-actions { justify-content: flex-end; }
.task-delegate-go { align-self: flex-start; }
@media (max-width: 860px) {
  .task-detail-form {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: none;
    grid-template-areas: 'title' 'side' 'main';
    padding: var(--space-4) var(--space-4) var(--space-5);
  }
  .task-detail-side {
    position: static;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    padding: 0;
    border: 0;
    background: none;
  }
  .task-detail-side > .form-group:first-child { grid-column: 1 / -1; }
  .task-detail-side .task-status-seg { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .task-detail-side .task-status-opt { text-align: center; padding: 4px 6px; }
}
@media (max-width: 600px) {
  .task-detail-backdrop { padding: 0; }
  .task-detail-form textarea.task-title-field { font-size: calc(19px * var(--font-scale, 1)); }
}
@media (pointer: coarse) {
  .task-detail-sheet .task-delete { min-height: var(--touch); }
}
</style>
