<script setup lang="ts">
/**
 * The workspace task board (`/tasks`).
 *
 * Four fixed columns — To do, In progress, In review, Done — on a wide pane, and
 * the same groups stacked on a narrow one. On the columns a card is dragged to
 * another lane, or moved one lane over with Shift+←/→ while its title has focus.
 * Everywhere, the editor's status control is the path a screen reader and a phone
 * use: a card carries no status control of its own, because its column already
 * says where it is.
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
 * - **"Changed since delegated" is the reviewer's problem.** The pane says the
 *   result was reached against an older description and decides nothing about it.
 *
 * The review loop is the rest of it: a **Ready for review** card says where the
 * result is and carries one **Approve Done**, which is the user's completion —
 * not a Detach and then a Done, and not a gesture that would settle the attempt
 * as `stopped` and lose the answer. **Send update** answers a mid-run edit with a
 * `chat_send`-shaped message into the attempt's own chat, never a second
 * delegation — through the board rather than the composer, because the server is
 * what rebinds the attempt to the revision the update was made at, so the flag
 * retires once the send lands and the answer settles the card. **History** reads
 * one task's attempts so a retried or stopped one is still readable, and a row
 * that disagrees with itself is **flagged with what disagrees and the controls
 * that resolve it** rather than silently rewritten.
 */
import SkeletonLoader from './SkeletonLoader.vue'
import { computed, inject, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import PaneHeader from './PaneHeader.vue'
import { useModalFocus } from '../composables/useModalFocus'
import { useProjectStore } from '../stores/projects'
import { useTaskBoardStore, type TaskChanges } from '../stores/taskBoard'
import { useTaskSignalsStore } from '../stores/taskSignals'
import { askConfirm, pendingConfirm } from '../lib/confirm'
import { renderUserMarkdown } from '../lib/safeMarkdown'
import {
  TASK_DUE_FILTERS,
  TASK_NO_PROJECT,
  TASK_STATUS_OPTIONS,
  buildTaskUpdateMessage,
  formatTaskDue,
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
  taskStatusLabel,
  titleSegments,
  splitTaskLog,
  joinTaskLog,
  type TaskDueFilter,
  type TaskReconcileNote,
} from '../lib/taskBoard'
import type { Task, TaskAttempt, TaskDetail, TaskStatus } from '../lib/types'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const projectStore = useProjectStore()
const board = useTaskBoardStore()
const workspace = computed(() => projectStore.activeWorkspace)

/**
 * Where the four columns become stacked status groups.
 *
 * One measurement, and it is the one the CSS below uses: the `chat-pane`
 * container's own inline size, against the same 940px the
 * `@container chat-pane (max-width: 940px)` rule uses. The window is not that
 * measurement — with the sidebar open or the split pane showing, a window
 * comfortably past any window threshold can still leave the pane under it, and
 * the measurement also disables horizontal drag and keyboard movement when
 * the status groups stack vertically.
 *
 * The pane root is a block that fills its container, so observing the root
 * measures the container. `contentRect` is the same inline-size the container
 * query resolves against; the window keeps a listener off this component
 * entirely, since the pane resizes when the sidebar drags, not only on resize.
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

onMounted(load)
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
  void closeDetail({ discard: true })
  // Not `closeDelegate`: a gesture already in flight makes that one refuse, which
  // would leave the sheet marked open with nothing drawn and the focus trap still
  // holding. `resetDelegate` closes it either way, and the late answer is dropped
  // by the store's own `drawingWorkspace` guard.
  resetDelegate()
  updateSentTaskId.value = ''
  updateSentWorkspace.value = ''
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
const columnsShown = computed(() => lanes.value.length > 1 && !isNarrow.value)

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
  busyTaskId.value = task.id
  if (next === 'done') await board.complete(workspace.value, task.id, revision)
  else await board.update(workspace.value, task.id, revision, { status: next })
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
  paneEl.value?.querySelector<HTMLElement>(`[data-task-id="${task.id}"] .task-open`)?.focus()
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

async function markDone(task: Task) {
  busyTaskId.value = task.id
  if (!(await releaseForDone(task))) {
    busyTaskId.value = ''
    if (board.error) {
      projectStore.pushErrorToast('Could not mark the task done', board.error)
      load()
    }
    return
  }
  const revision = board.revisionOf(task.id)
  if (!revision) {
    busyTaskId.value = ''
    return
  }
  board.clearError()
  await board.complete(workspace.value, task.id, revision)
  busyTaskId.value = ''
  if (board.error) {
    // The board's own message sits below the columns, out of sight from the
    // card that was ticked; say it where the user is looking.
    projectStore.pushErrorToast('Could not mark the task done', board.error)
    load()
  }
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

type DelegateMode = 'delegate' | 'resume' | 'retry' | 'open_chat' | 'update'

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
  if (isSettledLinked(task)) return chatArchived(task.chat_id) ? 'retry' : 'resume'
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
    case 'update':
      return 'Send update'
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
  // A sheet about to send something supersedes a note about something already
  // sent, and the note belongs to one card rather than to the board.
  if (updateSentTaskId.value === task.id) updateSentTaskId.value = ''
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
  updateTaskId.value = ''
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
 * Confirm the sheet, which is five gestures rather than one.
 *
 * `open_chat` starts nothing and opens the chat the attempt is running in — the
 * honest action for a live attempt, which cannot be resumed and must not be
 * delegated a second time. `update` sends one ordinary message into that same
 * chat carrying the task as it stands now, which is the only answer to an edit
 * made under a running turn. `resume` continues that attempt's own chat under its
 * own id. `retry` is a fresh delegation, and `delegate` is the first hand-over;
 * both present the revision the description on screen was read at.
 */
async function submitDelegate() {
  const task = delegateTask.value
  if (delegateBusy.value || !task) return
  if (delegateMode.value === 'update') {
    await sendTaskUpdate()
    return
  }
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
) {
  // A settled attempt still linked to its task is acted on through its own id,
  // which is the one `live_attempt_id` deliberately does not carry.
  const attemptId = task.live_attempt_id || task.attempt_id
  if (!attemptId || busyTaskId.value === task.id) return
  board.clearError()
  // Any other gesture on this card supersedes the note that an update landed: what
  // the card says now is about what was just done to it.
  if (updateSentTaskId.value === task.id) updateSentTaskId.value = ''
  busyTaskId.value = task.id
  await board.attemptAction(workspace.value, task.id, attemptId, action)
  busyTaskId.value = ''
  if (board.error) load()
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
 * card keeps reading `ready_for_review` forever, and offering "Approve Done" on a
 * result the user already approved would be a second completion gesture for the
 * same review.
 */
function isReviewReady(task: Task): boolean {
  return task.attempt_state === 'ready_for_review' && Boolean(task.live_attempt_id)
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
 * Whether this attempt's chat is archived.
 *
 * An archived chat still opens, read-only, from its vault transcript, but no
 * turn can run in it: Resume is not offered, and the way on is a new chat that
 * is handed what this one did.
 */
function chatArchived(chatId: string): boolean {
  return Boolean(chatId) && projectStore.chats.some((chat) => chat.chat_id === chatId && chat.archived)
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

// ── Send update ───────────────────────────────────────────────────────────

/**
 * The exact text a Send update hands to the attempt's chat.
 *
 * Shown before it is sent, not after: the user is approving a message into a
 * conversation they may not have open, and the whole reason the control exists
 * is that the description has changed underneath the agent. It is then sent as
 * approved rather than rebuilt server-side — see {@link sendTaskUpdate}.
 */
const updateMessage = computed(() =>
  delegateTask.value ? buildTaskUpdateMessage(delegateTask.value, delegateBody.value) : '',
)

/** The card whose update is in flight, and the one whose update has landed. */
const updateTaskId = ref('')
const updateSentTaskId = ref('')
const updateSentWorkspace = ref('')

/**
 * Hand the current task to the attempt's own chat, as one ordinary message.
 *
 * Through the board rather than the composer's `sendMessage`, because a composer
 * send cannot do the second half of this gesture: the server has to rebind the
 * attempt to the revision the update was made at, or `changed_since_delegated`
 * stays true and the control offers the very same update for ever. The server
 * sends the message into the attempt's own chat, queues it behind a turn already
 * running there, and attaches the settle watcher to whichever turn it caused — so
 * the answer comes back into the chat the attempt is already bound to and the
 * card follows it. No second attempt, no second chat, no new linkage.
 *
 * The revision is the one the *previewed description* was read at, exactly as
 * every other write here, so a body edited while the sheet was open is refused
 * rather than quoted from a stale read.
 *
 * Two things the card has to say, because an unchanged button after a send is a
 * button that cannot tell the user whether it worked: the chip reads *Sending…*
 * while the request is in flight, and the card carries a confirmation once it
 * landed. A refused send keeps the control and the flag, and leaves the server's
 * sentence in the sheet.
 */
async function sendTaskUpdate() {
  const task = delegateTask.value
  // A description this board could not read is not "empty". Sending the framed
  // message without it would tell the agent the description is gone, which is a
  // fact nobody checked; the confirm is disabled above, and this is the same rule
  // at the door.
  if (delegateBusy.value) return
  if (!task || !task.chat_id || delegateBodyState.value !== 'idle') {
    resetDelegate()
    return
  }
  const attemptId = task.live_attempt_id || task.attempt_id
  // Only an attempt that still holds the task can be updated, and that is the one
  // the card's own badge is drawn from. Without it there is nothing to rebind, so
  // there is nothing to confirm either.
  if (!attemptId) {
    resetDelegate()
    return
  }
  const revision = delegateRevision.value
  if (!revision) {
    resetDelegate()
    return
  }
  board.clearError()
  delegateBusy.value = true
  updateTaskId.value = task.id
  const originWorkspace = workspace.value
  const seq = delegateSeq
  const sent = await board.sendUpdate(
    originWorkspace, task.id, attemptId, revision, updateMessage.value,
  )
  if (seq !== delegateSeq || workspace.value !== originWorkspace) return
  delegateBusy.value = false
  updateTaskId.value = ''
  if (!sent) return
  board.clearError()
  // The row adopted the answer, so `changed_since_delegated` is already false and
  // the control has retired. What is left to say is that it was this card's update
  // and that it landed, which nothing on the row records.
  updateSentTaskId.value = task.id
  updateSentWorkspace.value = originWorkspace
  closeDelegate()
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

const detailOpen = ref(false)
/** A status write the server refused, said beside the control that made it. */
const statusError = ref('')
const detailEl = ref<HTMLElement | null>(null)
const detailTitleField = ref<HTMLInputElement | null>(null)
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
const detailForm = reactive({
  title: '',
  status: 'backlog' as TaskStatus,
  due: '',
  project_id: '',
  body: '',
})

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
const detailFocusActive = computed(() => detailOpen.value && !pendingConfirm.value)

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
  [routeTaskId, () => board.loadedWorkspace, tasks],
  async () => {
    const id = routeTaskId.value
    if (!id || board.loadedWorkspace !== workspace.value) return
    if (detailOpen.value && detailId.value === id) return
    const task = tasks.value.find(t => t.id === id)
    if (!task) {
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
    detailSaving.value = true
    const freed = await releaseForDone(task)
    detailSaving.value = false
    if (!freed) {
      if (board.error) {
        statusError.value = board.error
        load()
      }
      return
    }
  }
  const revision = board.revisionOf(task.id)
  if (!revision) return
  board.clearError()
  detailSaving.value = true
  const saved = next === 'done'
    ? await board.complete(workspace.value, task.id, revision)
    : await board.update(workspace.value, task.id, revision, { status: next })
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
  const ok = await askConfirm(
    `"${task.title}" will be deleted from this workspace. The record is a file in your vault and this unlinks it — there is no trash.`,
    { title: 'Delete task', confirmLabel: 'Delete', destructive: true },
  )
  if (!ok) return
  const revision = board.revisionOf(task.id)
  if (!revision) return
  board.clearError()
  detailSaving.value = true
  const removed = await board.remove(workspace.value, task.id, revision)
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
            <button type="button" class="btn-chip task-chip" @click="reloadBoard">Reload</button>
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

          <!-- One card, one render path. Unfiltered status groups are columns
               on wide panes and stacked sections on narrow panes. A status
               filter selects one group on either layout. -->
          <div
            v-else
            class="task-lanes"
            :class="lanes.length > 1 ? 'task-lanes--columns' : 'task-lanes--list'"
          >
            <section
              v-for="lane in lanes"
              :key="lane.status || 'all'"
              class="task-lane"
              :class="{ 'task-lane--drop': lane.status && dropStatus === lane.status }"
              :aria-label="lane.label"
              @dragover="onDragOver(lane.status, $event)"
              @dragleave="onDragLeave(lane.status, $event)"
              @drop.prevent="onDrop(lane.status)"
            >
              <h3 class="task-lane-head">
                <span class="task-lane-label">{{ lane.label }}</span>
                <span class="badge badge--muted">{{ lane.tasks.length }}</span>
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
                         A review-ready card is approved instead, below. -->
                    <button
                      v-if="task.status !== 'done' && !isReviewReady(task)"
                      type="button"
                      class="task-check"
                      :disabled="busyTaskId === task.id"
                      :aria-label="`Mark ${task.title} done`"
                      title="Mark done"
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
                  </div>
                  <!-- Only what differs from the default: General and For me are what
                       most cards are, and saying so on each one buries the rest. The
                       lane heading already states the card's status. -->
                  <p
                    v-if="task.attempt_state || ownProject(task.project_id) || formatTaskDue(task.due)"
                    class="task-meta"
                  >
                    <span
                      v-if="task.attempt_state"
                      class="badge"
                      :class="attemptBadgeClass(task)"
                    >{{ taskAttemptLabel(task.attempt_state, task.attempt_outcome, task.attempt_detail) }}</span>
                    <span v-if="ownProject(task.project_id)" class="task-project">{{ projectName(task.project_id) }}</span>
                    <span v-if="formatTaskDue(task.due)" class="badge" :class="isOverdue(task, today) ? 'badge--error' : 'badge--muted'">
                      {{ isOverdue(task, today) ? 'Overdue · ' : 'Due ' }}{{ formatTaskDue(task.due) }}
                    </span>
                  </p>
                  <p v-if="task.changed_since_delegated" class="task-changed">
                    Changed since delegated — the result was reached against an older
                    description.
                  </p>
                  <!-- What happened to the update, said once. The control retires on
                       its own (the rebind clears the flag it reads), so without this
                       the card would simply go back to looking untouched and the user
                       could not tell whether the send landed. -->
                  <p v-if="updateSentWorkspace === workspace && updateSentTaskId === task.id" class="task-update-sent" role="status">
                    Update sent — the agent has it in the linked chat, and its answer
                    will land here.
                  </p>
                  <!-- Where the answer is, and when it ended. Never a summary of it:
                       the board holds no copy of the agent's reply, so it names the
                       chat to read and the approval that closes the card. -->
                  <!-- The agent's own words on where it got to, or the engine's
                       note when it said nothing. Clamped: the editor has the rest. -->
                  <p v-if="attemptNote(task)" class="task-agent-note">{{ attemptNote(task) }}</p>
                  <p v-if="task.chat_id && chatArchived(task.chat_id) && !isReviewReady(task)" class="task-changed">
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
                  <!-- Only a delegated card has a foot: the gestures that act on its
                       attempt. Handing a task over starts in the editor, where the
                       preview is. -->
                  <div v-if="task.attempt_id || task.chat_id" class="task-card-foot">
                    <!-- The attempt's chat, when the board knows one. A button rather
                         than a link because it goes through the project store, not a
                         route — the same call the sidebar row makes. -->
                    <button
                      v-if="task.chat_id"
                      type="button"
                      class="btn-chip task-chip"
                      :disabled="busyTaskId === task.id"
                      :aria-label="`Open the chat working on ${task.title}`"
                      @click.stop="openAttemptChat(task)"
                    >Chat</button>
                    <!-- A mid-run edit. One message into the attempt's own chat,
                         carrying the task as it stands now — the sheet shows the exact
                         text first, and the server rebinds the attempt so the flag
                         retires once it has landed. Never a second delegation: that
                         would mint a new attempt and leave the one whose result is
                         under review looking abandoned. -->
                    <button
                      v-if="task.changed_since_delegated && task.chat_id"
                      type="button"
                      class="btn-chip task-chip"
                      :disabled="busyTaskId === task.id || updateTaskId === task.id"
                      :aria-label="`Send the current description of ${task.title} to the chat working on it`"
                      @click.stop="openDelegate(task, 'update')"
                    >{{ updateTaskId === task.id ? 'Sending…' : 'Send update' }}</button>
                    <!-- A review-ready card is read, not managed: the turn has ended,
                         so there is nothing to Stop, and Detach-then-Done would settle
                         the attempt as stopped and lose the result. Approve Done is
                         the whole of it. -->
                    <template v-if="isReviewReady(task)">
                      <button
                        v-if="task.status !== 'done'"
                        type="button"
                        class="btn-chip task-chip"
                        :disabled="busyTaskId === task.id"
                        :aria-label="`Approve the result of ${task.title} and mark it done`"
                        @click.stop="markDone(task)"
                      >Approve Done</button>
                    </template>
                    <!-- A settled attempt still holds the task, and it is the only
                         case the server will resume. Resume continues that chat
                         under the same attempt; Retry mints a new one. They are
                         two labelled gestures rather than one button, because
                         "Delegate" here would start a second attempt and quietly
                         abandon the one that failed. -->
                    <template v-else-if="isSettledLinked(task)">
                      <button
                        v-if="!chatArchived(task.chat_id)"
                        type="button"
                        class="btn-chip task-chip"
                        :disabled="busyTaskId === task.id"
                        :aria-label="`Resume the attempt on ${task.title} in its own chat`"
                        @click.stop="actOnAttempt(task, 'resume')"
                      >Resume</button>
                      <button
                        type="button"
                        class="btn-chip task-chip"
                        :disabled="busyTaskId === task.id"
                        :aria-label="`Start a new attempt at ${task.title}`"
                        @click.stop="actOnAttempt(task, 'retry')"
                      >{{ chatArchived(task.chat_id) ? 'Continue in new chat' : 'Retry' }}</button>
                    </template>
                    <template v-else-if="task.live_attempt_id">
                      <button
                        type="button"
                        class="btn-chip task-chip"
                        :disabled="busyTaskId === task.id"
                        :aria-label="`Stop the turn working on ${task.title}`"
                        @click.stop="actOnAttempt(task, 'stop')"
                      >Stop</button>
                      <button
                        type="button"
                        class="btn-chip task-chip"
                        :disabled="busyTaskId === task.id"
                        :aria-label="`Detach ${task.title} from its attempt`"
                        @click.stop="actOnAttempt(task, 'detach')"
                      >Detach</button>
                    </template>
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
          <h3 id="task-delegate-title">{{
            delegateMode === 'update' ? 'Send the update to the chat' : 'Hand this task to the agent'
          }}</h3>
          <button type="button" class="btn-icon" aria-label="Close" @click="closeDelegate">×</button>
        </header>

        <div class="task-form">
          <!-- The decision, stated before it is made: where the chat runs, what it
               runs with, and — the part a board cannot imply — that the turn is
               attended. An approval card it raises is an ordinary Needs-you card in
               that chat, answered in the ordinary way. -->
          <dl v-if="delegateMode !== 'update'" class="task-preview-facts">
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
              <template v-if="delegateMode === 'update'">
                {{
                  delegateBody.trim()
                    ? 'The description below is what the message carries.'
                    : 'This task has no description, so the message carries only its title and metadata.'
                }}
              </template>
              <template v-else>
                {{
                  delegateBody.trim()
                    ? 'The description below is quoted into the chat as the task.'
                    : 'This task has no description; only its title and metadata are handed over.'
                }}
              </template>
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
          <p v-else-if="delegateMode === 'update'" class="hint">
            This sends one message into the chat this attempt is working in,
            carrying the task as it stands now. It does not delegate again and
            starts no new attempt — the answer comes back into the same chat.
            Once it lands, this attempt counts as working from the description
            you just sent, and the card stops offering the update.
          </p>

          <!-- The exact message, before it is sent. The user is putting words into a
               conversation they may not have open, and the whole reason this
               control exists is that the description changed underneath the agent. -->
          <details v-if="delegateMode === 'update'" class="task-preview">
            <summary>Show the message</summary>
            <pre class="task-update-text">{{ updateMessage }}</pre>
          </details>

          <p v-if="board.error" class="task-action-error" role="alert">
            {{ board.error }}
            <button type="button" class="btn-chip task-chip" @click="reloadBoard">Reload</button>
          </p>

          <div class="form-actions">
            <button
              type="button"
              class="btn-primary"
              :disabled="delegateBusy || (delegateMode === 'update' && delegateBodyState !== 'idle')"
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

    <!-- Detail -->
    <div v-if="detailOpen" class="modal-backdrop" @click.self="closeDetail()">
      <div
        ref="detailEl"
        class="modal-sheet task-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-detail-title"
      >
        <header class="task-sheet-head">
          <h3 id="task-detail-title">Edit task</h3>
          <button type="button" class="btn-icon" aria-label="Close" @click="closeDetail()">×</button>
        </header>

        <form class="task-form" novalidate @submit.prevent="flushDetail">
          <div class="form-group">
            <label for="task-detail-name">Title</label>
            <input
              id="task-detail-name"
              ref="detailTitleField"
              v-model="detailForm.title"
              type="text"
              autocomplete="off"
              @blur="flushDetail"
            />
          </div>
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
          <div class="form-grid">
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
          </div>
          <!-- The description. A list row carries none, so it is read by id: the editor
               shows the prose it will write back, and while the read is in flight
               the field is absent rather than an empty box whose Save would clear
               what nobody was shown. -->
          <div class="form-group">
            <div class="task-field-head">
              <label v-if="bodyEditorShown" for="task-detail-body" class="task-field-label">Description</label>
              <span v-else id="task-detail-body-label" class="task-field-label">Description</span>
              <button
                v-if="detailDescribed && !bodyEditorShown && descriptionState !== 'loading'"
                type="button"
                class="btn-chip task-chip"
                @click="editBody"
              >Edit</button>
            </div>
            <p v-if="descriptionState === 'loading'" class="hint" role="status">Loading description…</p>
            <template v-else-if="detailDescribed">
              <textarea
                v-if="bodyEditorShown"
                id="task-detail-body"
                ref="bodyField"
                v-model="detailForm.body"
                rows="8"
                @blur="flushDetail"
                placeholder="Markdown. What done looks like, links, notes."
              ></textarea>
              <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
              <div
                v-else
                class="task-body markdown"
                aria-labelledby="task-detail-body-label"
                @dblclick="editBody"
                v-html="bodyPreview"
              ></div>
            </template>
            <p v-else class="hint">
              This board could not read this task's description, so it will not
              overwrite it.
              <button type="button" class="btn-chip task-chip" @click="retryDescription">Retry</button>
            </p>
          </div>

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
              <span v-if="detailTask?.chat_id && chatArchived(detailTask.chat_id)" class="hint">
                The chat is archived; it opens read-only from the list below.
              </span>
            </p>
            <p v-if="detailTask?.changed_since_delegated" class="task-changed">
              Changed since delegated — the result was reached against an older
              description.
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
                class="btn-chip task-chip"
                :disabled="detailSaving || board.saving || !detailTask"
                @click="delegateFromDetail(detailTask!)"
              >{{ detailDelegateLabel }}</button>
              <!-- The one gesture that completes a reviewed result. The same call the
                   card's Approve Done makes: the service releases the linkage and
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
                v-if="detailTask?.changed_since_delegated && detailTask?.chat_id"
                type="button"
                class="btn-chip task-chip"
                :disabled="detailSaving || board.saving"
                @click="openDelegate(detailTask!, 'update')"
              >Send update</button>
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
                    >{{ chatArchived(attempt.chat_id) ? 'Archived chat' : 'Chat' }}</button>
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

          <p v-if="board.error" class="task-action-error" role="alert">
            {{ board.error }}
            <button type="button" class="btn-chip task-chip" @click="reloadBoard">Reload</button>
          </p>

          <!-- No Save: every field writes itself (see the Autosave section), and
               this line says where that stands. -->
          <div class="form-actions">
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
              class="btn-small btn-danger task-delete"
              :disabled="detailSaving"
              @click="deleteTask"
            >Delete</button>
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
  display: grid;
  gap: var(--space-3);
  padding-bottom: var(--space-6);
  align-items: start;
}
.task-lanes--columns { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.task-lanes--list { grid-template-columns: minmax(0, 1fr); }

/* Narrow panes preserve the four headings and stack their groups. The same
   940px measurement disables column-only interactions in the script. */
@container chat-pane (max-width: 940px) {
  .task-lanes--columns { grid-template-columns: minmax(0, 1fr); }
  .task-lanes { row-gap: var(--space-5); }
  .task-lanes .task-lane { min-height: 0; margin: 0; padding: 0; }
}

.task-lane {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  min-height: 8rem;
  margin: calc(-1 * var(--space-1));
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
}
.task-card:hover { border-color: var(--border-strong); }
.task-lanes--columns .task-card[draggable='true'] { cursor: grab; }
.task-card--dragging { opacity: 0.4; }
.task-card--done .task-title { color: var(--fg2); }
/* The card's Done. A round box rather than a square one: it reads as "tick
   this off", not as a form field. The hit area is the full touch target; the
   drawn ring sits inside it. */
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
/* The agent's own words under the badge, clamped: the editor has the rest. */
.task-agent-note {
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
  white-space: pre-line;
  overflow-wrap: anywhere;
}
/* "Changed since delegated" is a warning about the *result*, not about the card, so
   it reads as a caption on it rather than as one of its voices. */
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
   the answer lives and what closes the card. Kept beside the changed-since
   caption rather than inside it, because the two can both be true. */
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
/* "The update landed" is neither of the two above: it is about a gesture the user
   just made, not about the result or about a disagreement. The accent rule is the
   same one the review block uses, because that is the outcome it reports — the
   agent now has the current description. */
.task-update-sent {
  margin: 0;
  padding: var(--space-1) var(--space-2);
  border-left: 3px solid var(--accent2);
  background: color-mix(in srgb, var(--accent2) 8%, transparent);
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.5;
}
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
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}

.task-project { overflow-wrap: anywhere; }
.task-card-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

/* 44px on touch: the shared rule raises .btn-chip, and the native selects and
   buttons here carry it directly so a coarse pointer never gets a 34px target. */
@media (pointer: coarse) {
  .task-filter,
  .task-card .btn-chip,
  .task-status-opt {
    min-height: var(--touch);
  }
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
.task-field-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  min-height: 24px;
}
.task-field-head .task-chip { min-height: 26px; padding-block: 0; }
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
@media (max-width: 420px) {
  .task-status-opt { padding-inline: 2px; font-size: var(--text-xs); }
}
/* The description as it reads. */
.task-body {
  max-height: 18rem;
  overflow-y: auto;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  font-size: var(--text-sm);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
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
.task-delete { margin-left: auto; }
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
/* The exact text a Send update would put into the delegated chat. A <pre>, not
   the Markdown renderer: it is a message to a chat, not prose the user wrote in
   their own vault, and wrapping it is the point. */
.task-update-text {
  margin: var(--space-2) 0 0;
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
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
</style>
