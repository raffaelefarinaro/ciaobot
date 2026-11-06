<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import { useHousekeepingStore } from '../stores/housekeeping'
import { useProjectStore } from '../stores/projects'
import { askConfirm } from '../lib/confirm'
import type { OperatorAction, UpdateTaskRow } from '../lib/types'
import NeedsYouRow, { type NeedsYouMenuItem } from './NeedsYouRow.vue'
import HostTerminalButton from './HostTerminalButton.vue'
import { useCommandCopy } from '../composables/useCommandCopy'
import { useHostTerminal } from '../composables/useHostTerminal'
import { focusAfterNoticeClose, useHomeNoticeWindows } from '../composables/useHomeNoticeWindows'
import { hasLiveAttempt, visibleUpdateTasks as filterVisibleUpdateTasks } from '../lib/updateTasks'
import { actionNoticeKey, parseHomeNoticeKey, updateNoticeKey } from '../lib/notificationInbox'

const housekeeping = useHousekeepingStore()
const projectStore = useProjectStore()
const noticeWindows = useHomeNoticeWindows()

const chatBusy = ref(false)
const commandCopy = useCommandCopy()

/** Actions relevant to the workspace on screen.
 *
 * A shared action (no workspace) always applies. A workspace-scoped action
 * names where acting on it writes, and another workspace's pile is not this
 * tab's business: the review queue behind /proposals scopes its rows the same
 * way, so a summed strip made the tile claim more than the opened page showed.
 * With no active workspace yet, show everything rather than an empty strip.
 */
const scopedActions = computed(() => {
  const active = projectStore.activeWorkspace
  if (!active) return housekeeping.actions
  return housekeeping.actions.filter(
    (action) => !action.workspace || action.workspace === active,
  )
})
const visibleActions = computed(() => scopedActions.value.filter(action => !noticeWindows.isClosed(actionKey(action))))

function actionKey(action: OperatorAction): string {
  return actionNoticeKey(action.workspace, action.id)
}

const groups = computed(() => {
  const byWorkspace = new Map<string, OperatorAction[]>()
  for (const action of visibleActions.value) {
    const key = action.workspace || ''
    const bucket = byWorkspace.get(key)
    if (bucket) bucket.push(action)
    else byWorkspace.set(key, [action])
  }
  return [...byWorkspace.entries()]
    .sort(([a], [b]) => (a === '' ? -1 : b === '' ? 1 : a.localeCompare(b)))
    .map(([workspace, actions]) => ({ workspace, actions }))
})

// A single group with no workspace is the common case (one install, nothing
// workspace-specific); labelling it "shared" there is noise, so headings only
// appear once there is something to distinguish. After scoping, a named group
// is by definition the workspace you are standing in, so only a mixed group
// list — possible while no workspace is active — needs labels.
const showHeadings = computed(
  () => groups.value.some(
    (g) => g.workspace && g.workspace !== projectStore.activeWorkspace,
  ),
)

onMounted(() => {
  housekeeping.init()
})

async function runAction(action: OperatorAction): Promise<void> {
  const { ok } = await housekeeping.run(action.id)
  // The star nudge's run records the star; the tile then disappears, so the
  // only feedback is this toast. Only the strip's explicit run button lands
  // here — opening the link never records anything: the visitor may not be
  // signed in, and inspecting or closing the tab is not a star. The nudge clears
  // through the explicit dismiss ("Later") instead.
  if (ok && action.id === 'github-star') {
    projectStore.pushToast({
      chat_id: '',
      title: '★ Starred — thank you!',
      body: 'It genuinely helps other developers discover Ciaobot.',
    })
  }
}

async function dismissAction(action: OperatorAction): Promise<void> {
  await housekeeping.dismiss(action.id)
}

async function openView(action: OperatorAction): Promise<void> {
  // The queue tiles name a surface that already has per-row accept/dismiss, a
  // destination picker and batch operations. Sending the operator there beats
  // asking them to work through a hundred items in prose.
  const { router } = await import('../router')
  await router.push(action.view_route)
}

// The backend names the tile's lead button. "view" (the update tile) makes the
// view button the filled primary and demotes the link to a chip; anything else
// keeps the default order, where run/link/view buttons lead.
function viewLeads(action: OperatorAction): boolean {
  return action.primary === 'view' && !!action.view_route
}

function chatButtonLabel(action: OperatorAction): string {
  return action.chat_label || 'Discuss in chat'
}

async function openChat(action: OperatorAction): Promise<void> {
  if (chatBusy.value || !action.chat_prompt) return
  chatBusy.value = true
  try {
    const workspace = action.workspace || projectStore.activeWorkspace
    if (projectStore.activeWorkspace !== workspace) {
      if ((await projectStore.switchWorkspace(workspace)) === false) return
    }
    let project = projectStore.projects.find(
      p => p.workspace === workspace && p.is_auto,
    )
    if (!project) {
      project = await projectStore.createProject('General')
    }
    const chat = await projectStore.createChat(project.project_id, 'Housekeeping')
    if (chat) {
      projectStore.sendMessage(chat.chat_id, action.chat_prompt)
      const { router } = await import('../router')
      router.push(`/chat/${chat.chat_id}`)
    }
  } finally {
    chatBusy.value = false
  }
}

// ── One list ────────────────────────────────────────────────────────────────
//
// Workspace-health errors, operator actions and update tasks share one list
// above the composer. A row is a title, one line and one lead control; anything
// else the source can do sits in the row's ⋯ menu. The sources keep their own
// policies (an operator action is a view of the machine and goes away when the
// condition does; an update task is a decision that persists until somebody
// reverses it), so each keeps its own row builder below.

const hostTerminal = useHostTerminal()
onMounted(() => { void hostTerminal.ensureCapabilities() })

/** Blocking preconditions first, then the rest in the workspace order above. */
const orderedActions = computed(() => {
  const flat = groups.value.flatMap(group => group.actions)
  return [...flat.filter(a => a.blocking), ...flat.filter(a => !a.blocking)]
})

/** The first line of a detail as plain text. Details are prose or short
 *  markdown (the missed-reminder list); a row has room for one line, and the
 *  rest is a click away in the chat the row can open. */
function oneLine(detail: string): string {
  const first = detail.split('\n').map(line => line.trim()).find(line => line) ?? ''
  return first
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^[-+*]\s+/, '')
    .replace(/\*\*|`/g, '')
    .trim()
}

type ActionLead = 'view' | 'terminal' | 'copy' | 'run' | 'link' | 'chat' | 'dismiss' | ''

function terminalReady(action: OperatorAction): boolean {
  return !!action.terminal_action && hostTerminal.canRun(action.terminal_action)
}

/** Which of an action's controls leads its row.
 *
 * The backend can name the lead ("view", the update row). Otherwise the order is
 * the one the tiles always had: a terminal that can run the command, the run
 * button, the link, the surface that already exists, the chat. A provider update
 * whose terminal is not on this machine leads with copying the command instead,
 * because "Update in Terminal" would be a button that is not there. */
function leadOf(action: OperatorAction): ActionLead {
  if (viewLeads(action)) return 'view'
  if (terminalReady(action)) return 'terminal'
  if (action.terminal_action && action.command) return 'copy'
  if (action.run_label) return 'run'
  if (action.link_url) return 'link'
  if (action.view_route) return 'view'
  if (action.chat_prompt) return 'chat'
  if (action.dismiss_label) return 'dismiss'
  return ''
}

function copyLabel(action: OperatorAction): string {
  return commandCopy.stateOf(action.id) === 'copied' ? 'Copied' : 'Copy command'
}

function actionTitle(action: OperatorAction): string {
  return showHeadings.value && action.workspace ? `${action.title} · ${action.workspace}` : action.title
}

/** One line. When copying was refused the command itself takes the line, as
 *  selectable text: the button that failed was the only other way to it. */
function actionLine(action: OperatorAction): string {
  if (action.command && commandCopy.stateOf(action.id) === 'failed') {
    return `Copy failed. Run: ${action.command}`
  }
  return oneLine(action.detail)
}

function actionItems(action: OperatorAction): NeedsYouMenuItem[] {
  const lead = leadOf(action)
  const running = housekeeping.runningIds.has(action.id)
  const items: NeedsYouMenuItem[] = []
  if (action.view_route && lead !== 'view') {
    items.push({ id: 'view', label: action.view_label || 'Open' })
  }
  if (action.run_label && lead !== 'run') {
    items.push({ id: 'run', label: running ? 'Running…' : action.run_label, disabled: running })
  }
  if (action.link_url && lead !== 'link') {
    items.push({ id: 'link', label: action.link_label || 'Open', href: action.link_url })
  }
  if (action.chat_prompt && lead !== 'chat') {
    items.push({ id: 'chat', label: chatButtonLabel(action), disabled: chatBusy.value })
  }
  if (action.command && lead !== 'copy') items.push({ id: 'copy', label: copyLabel(action) })
  if (action.dismiss_label && lead !== 'dismiss') items.push({ id: 'dismiss', label: action.dismiss_label })
  items.push({ id: 'hide', label: 'Hide for now' })
  return items
}

async function hideNotice(key: string): Promise<void> {
  noticeWindows.close(key)
  await focusAfterNoticeClose()
}

async function onActionSelect(action: OperatorAction, id: string): Promise<void> {
  switch (id) {
    case 'view': return openView(action)
    case 'run': return runAction(action)
    case 'chat': return openChat(action)
    case 'copy': return commandCopy.copy(action.id, action.command || '')
    case 'dismiss': return dismissAction(action)
    case 'hide': return hideNotice(actionKey(action))
    // 'link' is an anchor: the browser opens it.
  }
}

// Workspace health: the install-wide report Settings → Advanced shows. Only
// errors raise a row; each names its own problem, and the one remedy the engine
// has (`POST /api/workspace-health/fix`) is the lead of every one of them.
const HEALTH_SETTINGS_ROUTE = '/settings/advanced'
const healthItems: NeedsYouMenuItem[] = [{ id: 'settings', label: 'Open in Settings' }]

async function fixHealth(): Promise<void> {
  const at = projectStore.activeWorkspace || ''
  const ok = await housekeeping.fixHealth()
  const left = housekeeping.healthErrors.length
  if (!ok) {
    await report(housekeeping.healthFixError, at)
  } else if (left) {
    await report(
      `Applied the automatic fix. ${left} ${left === 1 ? 'problem still needs' : 'problems still need'} a look in Settings.`,
      at,
    )
  } else {
    await report('Fixed. Workspace health has no errors.', at)
  }
}

async function onHealthSelect(id: string): Promise<void> {
  if (id !== 'settings') return
  const { router } = await import('../router')
  await router.push(HEALTH_SETTINGS_ROUTE)
}

// ── "After this update" ────────────────────────────────────────────────────
//
// The work Ciaobot's own update leaves behind, as cards rather than as prose.
// They live in their own section instead of joining `groups` because their
// policy is different in a way the operator can feel: an operator action is a
// view of the machine and disappears when re-detection says the condition is
// gone, while an update task is a *decision* that persists until somebody
// reverses it, can be reopened from Settings, and must never be drawn as done
// before a completion check has actually said so.

// Which update tasks Home shows, and what counts as a live attempt, live in
// lib/updateTasks.ts so the inbox applies the identical rule.
const visibleUpdateTasks = computed(() => filterVisibleUpdateTasks(housekeeping.updateTasks))
const openUpdateTasks = computed(() => visibleUpdateTasks.value.filter(task => !noticeWindows.isClosed(updateKey(task))))

function updateKey(task: UpdateTaskRow): string {
  return updateNoticeKey(projectStore.activeWorkspace, task.id, task.revision)
}

watch(
  () => [
    ...scopedActions.value.map(actionKey),
    ...visibleUpdateTasks.value.map(updateKey),
  ],
  keys => noticeWindows.setAvailable('housekeeping', keys),
  { immediate: true },
)
onBeforeUnmount(() => noticeWindows.clearAvailable('housekeeping'))

/** Whether the list draws anything at all.
 *
 * Real rows, plus one exception: a list that still has an announcement to
 * deliver. Resolving the last row removes the button that was pressed, so
 * without this the list would vanish in the same tick — taking with it the only
 * place the outcome could be said and the only place focus could land. An
 * operator who pressed "Hide it" and saw the page rearrange itself in silence
 * has been told nothing about whether it worked.
 *
 * The exception is a *loan*, not a state: `clearGroupStatus` ends it on a timer
 * and on a workspace switch (see `GROUP_STATUS_TTL_MS`), because a heading and a
 * stale "Hidden …" left standing on Home would outlive the thing they describe
 * — and, across a workspace switch, be attributed to a workspace the operator
 * never pressed anything in.
 *
 * Zero rows and a *failed* source are drawn identically — as nothing — because
 * that is the strip's own best-effort contract (a Home that raised a red line
 * for a background poll would nag about a condition the operator cannot act on
 * from this page). Silence is not a false "done": the list has no all-clear
 * state at all, so its absence never reads as a verdict. The one place a failed
 * update-task check *is* reported is Settings → Overview → Update task history. */
const rowCount = computed(
  () => housekeeping.healthErrors.length + orderedActions.value.length + openUpdateTasks.value.length,
)
const showNeedsYou = computed(() => rowCount.value > 0 || groupStatus.value !== '')

/** One task's key. A revision is a different record and a different card, so
 *  the two are never collapsed into one row. */
function taskKey(task: UpdateTaskRow): string {
  return `${task.id}@${task.revision}`
}

function taskBusy(task: UpdateTaskRow): boolean {
  return housekeeping.pendingUpdateTaskIds.has(taskKey(task))
}

function taskError(task: UpdateTaskRow): string {
  return housekeeping.taskErrors[taskKey(task)] || ''
}

/** The one line that says where this task stands, in words rather than a colour.
 *
 * `unknown` is worded as a question nobody could answer, never as an absence of
 * work: a detector that has not run, or could not, has not established that
 * there is nothing to do. It is checked first, so a row whose detector is still
 * silent says so even while an attempt is under way — the silence is the more
 * recent fact, and the one an operator is waiting on.
 *
 * There is no "does not apply" line, because a row that does not apply and has
 * no attempt is not drawn at all (see `visibleUpdateTasks`): a permanent card
 * reading "nothing to do" on the Home of an install that never needed the task
 * is the opposite of what a zero-task Home should look like. */
function taskStateLine(task: UpdateTaskRow): string {
  if (task.applicability === 'unknown') {
    return 'Checking whether this applies here…'
  }
  if (task.status === 'waiting_review') {
    // An archived chat cannot be "open and waiting": say what is true instead,
    // because the operator is being asked to decide about a chat that is gone.
    return task.chat_live
      ? 'Its chat is open and waiting for you to decide what to do with it.'
      : 'The chat this opened has been archived or deleted. Starting opens a new one.'
  }
  if (task.status === 'failed') {
    return task.chat_live
      ? 'The last attempt did not get going. Starting again reuses the same chat.'
      : 'The last attempt did not get going, and its chat is gone. Starting opens a new one.'
  }
  if (task.status === 'in_progress') {
    // Not "on the left": the chat is a route, and on a phone the list is above
    // the conversation. Where it opens is the layout's business. And when the
    // chat no longer exists, "its chat is open" would be a flat untruth — the
    // start will mint a fresh one, which is what this line says.
    return task.chat_live
      ? 'Started. Its chat is open.'
      : 'Started, but its chat has been archived or deleted. Starting opens a new one.'
  }
  return 'New since this update.'
}

/** What the lead button says for a task already under way.
 *
 * "Resume chat" is right only while there is a chat to resume. Against an
 * archived or deleted one a start mints a fresh chat and dispatches the prompt
 * into that, so calling it "Resume" would promise continuity the server does not
 * deliver. Same action, honest label. */
function startLabel(task: UpdateTaskRow): string {
  return task.chat_live ? 'Resume chat' : 'Start a new chat'
}

/** Whether this row's lead button starts — or resumes — its chat.
 *
 * The gate is the row's *state*, not the detector's answer, and the difference
 * is not cosmetic. An offer is the only row that needs `applicable` first:
 * nobody may be handed instructions for a condition this install has not
 * established exists. A live attempt is a different question — the chat is
 * already open and the work is already under way, and `not_applicable` is the
 * *normal* state once the chat has done the job but the completion check has not
 * said so yet. Gating Resume on it would strand a live chat behind a small "Open
 * its chat" link, which is precisely the wrong thing to do to somebody whose
 * work is running. */
function canStart(task: UpdateTaskRow): boolean {
  if (hasLiveAttempt(task.status)) return true
  return task.applicability === 'applicable'
}

const groupEl = ref<HTMLElement | null>(null)
/** The outcome of the last press. Declared here rather than beside the click
 *  handlers because `showNeedsYou` reads it: a list that still owes the
 *  operator an announcement has to stay on screen to deliver it. */
const groupStatus = ref('')

/** How long the outcome of the last press keeps the group on screen.
 *
 * Long enough to be read aloud by a screen reader, and long enough for the eye
 * to find the group focus just landed on; short enough that a Home left open
 * does not sit there with yesterday's "Hidden …" under a heading that is no
 * longer about anything. */
const GROUP_STATUS_TTL_MS = 10_000
let groupStatusTimer: ReturnType<typeof setTimeout> | null = null

/** Drop the announcement, and with it the group when it has no cards left.
 *
 *  Called on a timer and on a workspace switch. A workspace change is not
 *  optional: the status describes a press in the workspace the operator just
 *  left, and carrying it over would attribute it to the one they are now in. */
function clearGroupStatus(): void {
  if (groupStatusTimer !== null) {
    clearTimeout(groupStatusTimer)
    groupStatusTimer = null
  }
  groupStatus.value = ''
}

onBeforeUnmount(clearGroupStatus)

/** Keep the keyboard where the operator left it when a card goes away.
 *
 * A dismissal removes the very button that was pressed, so the browser drops
 * focus to the body and the next Tab starts again from the top of the page —
 * which reads, to somebody who just acted, as the app having decided they were
 * finished. Landing on the group itself (rather than on the next card's first
 * button) keeps the reading order intact and gives the announcement a home. */
async function keepFocus(): Promise<void> {
  await nextTick()
  const active = document.activeElement
  if (groupEl.value && (!active || active === document.body)) {
    groupEl.value.focus()
  }
}

async function report(text: string, at: string, after?: () => Promise<void>): Promise<void> {
  // A press belongs to the workspace it was made in, and this sentence is about
  // that press. If the operator has moved on since, the group on screen is
  // someone else's workspace: saying it there would attribute the decision to
  // the wrong workspace, and clearing the status first would throw away whatever
  // the new workspace has to say for itself. So the outcome goes unsaid and the
  // effect — the chat it opened — still happens.
  if (projectStore.activeWorkspace !== at) {
    if (after) await after()
    return
  }
  clearGroupStatus()
  groupStatus.value = text
  groupStatusTimer = setTimeout(clearGroupStatus, GROUP_STATUS_TTL_MS)
  if (after) await after()
  await keepFocus()
}

async function openTaskChat(chatId: string): Promise<void> {
  if (!chatId) return
  const { router } = await import('../router')
  await router.push(`/chat/${chatId}`)
}

/** Start (or resume) a task's chat.
 *
 * The button is the same on an offered task and on one already under way, and
 * that is the server's doing rather than a shortcut here: `start` is idempotent
 * per `(task, revision)`, so a double press, a second tab or a retry after a
 * dropped response all land in one chat with the prompt dispatched once. The
 * label says which of the two is meant so nobody is surprised. */
async function startTask(task: UpdateTaskRow): Promise<void> {
  // Captured before the first await: the outcome is about the workspace the
  // button was pressed in, wherever the reader has wandered to since.
  const at = projectStore.activeWorkspace || ''
  const outcome = await housekeeping.startUpdateTask(task)
  if (!outcome.ok) {
    if (outcome.chatId) {
      // The chat exists and the prompt never reached it. Opening it rather than
      // throwing it away is the whole point of the server sending the id back.
      await report('The chat was opened but the task could not be sent into it.', at, () =>
        openTaskChat(outcome.chatId),
      )
      return
    }
    await report(outcome.error, at)
    return
  }
  const title = task.title
  await report(
    outcome.resumed
      ? `Reopened the existing chat for “${title}”.`
      : `Started “${title}” in a new chat.`,
    at,
    () => openTaskChat(outcome.chatId),
  )
}

/** Decline this task at this revision, after saying exactly what that means.
 *
 * The confirmation is not ceremony. Two things are true at once and an operator
 * pressing a small button is unlikely to have thought about either: the card
 * disappears for this workspace only, and — when a chat is open — nothing about
 * that chat changes. It is not cancelled and it is not marked done; dismissing
 * records "not this one" and leaves the work exactly where it was. */
async function dismissTask(task: UpdateTaskRow): Promise<void> {
  // Same reasoning as `startTask`: the confirmation can be open for as long as it
  // takes to read, and the press after it belongs to the workspace it was
  // confirmed in.
  const at = projectStore.activeWorkspace || ''
  // "Its chat stays open" is only true while the chat is live. Against an
  // archived or deleted one the promise would be about a chat that is gone, so
  // the copy falls back to the plain dismissal wording.
  const hasChat =
    task.chat_live
    && (task.status === 'in_progress' || task.status === 'waiting_review' || !!task.chat_id)
  const message = hasChat
    ? 'Hide this task in this workspace? Its chat stays open and is not marked ' +
      'done — you can carry on in it, and reopen the task from Settings → ' +
      'Overview → Update task history.'
    : 'Hide this task in this workspace? It stays hidden for this revision only, ' +
      'and you can reopen it from Settings → Overview → Update task history.'
  if (!await askConfirm(message, {
    title: 'Hide this update task?',
    confirmLabel: 'Hide it',
    cancelLabel: 'Keep it',
  })) return

  const outcome = await housekeeping.dismissUpdateTask(task)
  if (!outcome.ok) {
    await report(outcome.error, at)
    return
  }
  await report(
    hasChat
      ? `Hidden “${task.title}”. Its chat is untouched and still open.`
      : `Hidden “${task.title}” in this workspace. Reopen it from Settings.`,
    at,
  )
}

async function reviewTask(): Promise<void> {
  const { router } = await import('../router')
  await router.push('/memory/review?show=suggested')
}

async function recheckTasks(): Promise<void> {
  await housekeeping.recheckUpdateTask()
  await keepFocus()
}

type TaskLead = 'review' | 'start' | 'check'

function taskLead(task: UpdateTaskRow): TaskLead {
  if (task.status === 'waiting_review') return 'review'
  return canStart(task) ? 'start' : 'check'
}

function startText(task: UpdateTaskRow): string {
  if (taskBusy(task)) return 'Working…'
  if (task.status === 'in_progress' || task.status === 'waiting_review') return startLabel(task)
  if (task.status === 'failed') return 'Try again'
  return 'Start in chat'
}

/** One line: a refusal first (it is the thing the operator must read), then —
 *  for an offer the detector has confirmed — why it exists, and otherwise where
 *  the attempt stands. */
function taskLine(task: UpdateTaskRow): string {
  const error = taskError(task)
  if (error) return error
  if (task.status === 'offered' && task.applicability !== 'unknown') return task.why
  return taskStateLine(task)
}

function taskItems(task: UpdateTaskRow): NeedsYouMenuItem[] {
  const items: NeedsYouMenuItem[] = []
  // A review row leads with the queue; its chat is still one tap away.
  if (taskLead(task) === 'review') {
    items.push({ id: 'start', label: startText(task), disabled: taskBusy(task) })
  }
  if (task.chat_live) items.push({ id: 'chat', label: 'Open its chat' })
  items.push({ id: 'dismiss', label: 'Hide it', disabled: taskBusy(task) })
  items.push({ id: 'hide', label: 'Hide for now' })
  return items
}

async function onTaskSelect(task: UpdateTaskRow, id: string): Promise<void> {
  switch (id) {
    case 'start': return startTask(task)
    case 'chat': return openTaskChat(task.chat_id)
    case 'dismiss': return dismissTask(task)
    case 'hide': return hideNotice(updateKey(task))
  }
}

// Applicability and state are per workspace, so a workspace switch re-asks the
// question rather than leaving the previous workspace's answers on screen — and
// takes the last press's announcement with them, since that press was made in
// the workspace being left, not in the one now on screen.
watch(
  () => projectStore.activeWorkspace,
  (next, previous) => {
    if (next && next !== previous) {
      clearGroupStatus()
      clearNoticeUnavailable()
      void housekeeping.refreshUpdateTasks(next)
    }
  },
)

// ── Reveal a notice from /?notice=<key> ─────────────────────────────────────
//
// The notification inbox links an operator or update notice here. The route
// only ever reopens, scrolls to and focuses the notice: it never runs, starts,
// dismisses or confirms anything. A key that does not name a notice drawn for
// the workspace on screen says so and reveals nothing else.

const NOTICE_KEY_MAX = 256
const NOTICE_UNAVAILABLE_TTL_MS = 10_000
const noticeUnavailable = ref(false)
let noticeUnavailableTimer: ReturnType<typeof setTimeout> | null = null

function clearNoticeUnavailable(): void {
  if (noticeUnavailableTimer !== null) {
    clearTimeout(noticeUnavailableTimer)
    noticeUnavailableTimer = null
  }
  noticeUnavailable.value = false
}

function showNoticeUnavailable(): void {
  clearNoticeUnavailable()
  noticeUnavailable.value = true
  noticeUnavailableTimer = setTimeout(clearNoticeUnavailable, NOTICE_UNAVAILABLE_TTL_MS)
}

onBeforeUnmount(clearNoticeUnavailable)

const noticeRoute = inject(routeLocationKey, null)
const noticeRouter = inject(routerKey, null)

function firstNoticeKey(raw: unknown): string {
  const first = Array.isArray(raw) ? raw[0] : raw
  return typeof first === 'string' && first.length <= NOTICE_KEY_MAX ? first : ''
}

watch(
  () => noticeRoute?.query.notice,
  async (raw) => {
    if (!noticeRoute || raw === undefined) return
    const key = firstNoticeKey(raw)
    if (key) noticeWindows.requestReveal(key)
    const { notice: _consumed, ...rest } = noticeRoute.query
    void _consumed
    const target = noticeRouter ?? (await import('../router')).router
    await target.replace({ path: noticeRoute.path, query: rest, hash: noticeRoute.hash })
  },
  { immediate: true },
)

const revealDecision = computed<'none' | 'wait' | 'found' | 'missing'>(() => {
  const key = noticeWindows.pendingReveal.value
  if (!key) return 'none'
  const parsed = parseHomeNoticeKey(key)
  if (!parsed) return 'missing'
  if (parsed.kind === 'action') {
    if (housekeeping.actionsHealth.status === 'checking') return 'wait'
    return scopedActions.value.some(a => actionKey(a) === key) ? 'found' : 'missing'
  }
  const active = projectStore.activeWorkspace
  if (!active) return 'wait'
  // A previous workspace's key can never target this workspace's row.
  if (parsed.workspace !== active) return 'missing'
  if (housekeeping.updateTasksWorkspace !== active || housekeeping.updateTasksHealth.status === 'checking') {
    return 'wait'
  }
  return visibleUpdateTasks.value.some(t => updateKey(t) === key) ? 'found' : 'missing'
})

let updateRefreshRequestedFor = ''

watch(
  revealDecision,
  async (decision) => {
    const key = noticeWindows.pendingReveal.value
    if (decision === 'wait') {
      const active = projectStore.activeWorkspace
      if (
        active
        && parseHomeNoticeKey(key)?.kind === 'update'
        && housekeeping.updateTasksWorkspace !== active
        && updateRefreshRequestedFor !== key
      ) {
        updateRefreshRequestedFor = key
        void housekeeping.refreshUpdateTasks(active)
      }
      return
    }
    if (decision === 'missing') {
      showNoticeUnavailable()
      noticeWindows.clearReveal()
      void housekeeping.refresh()
      void housekeeping.refreshUpdateTasks()
      return
    }
    if (decision !== 'found') return
    clearNoticeUnavailable()
    noticeWindows.reopen(key)
    await nextTick()
    if (noticeWindows.pendingReveal.value !== key) return
    const el = [...document.querySelectorAll<HTMLElement>('[data-home-notice-window]')]
      .find(candidate => candidate.dataset.noticeKey === key)
    if (!el) {
      showNoticeUnavailable()
      noticeWindows.clearReveal()
      return
    }
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    el.scrollIntoView?.({ block: 'center', behavior: reducedMotion ? 'auto' : 'smooth' })
    el.focus({ preventScroll: true })
    noticeWindows.clearReveal()
  },
  { immediate: true, flush: 'post' },
)
</script>

<template>
  <p v-if="noticeUnavailable" class="home-notice-unavailable" role="status">This notice is no longer available</p>

  <!--
    One list, above the composer. Health errors lead (the install cannot do its
    work), then blocking preconditions, then everything else an operator action
    offers, then the work the last update left behind. Each row is a title, one
    line and one lead control; the rest is in the row's menu. With nothing to
    show the section is not rendered at all: there is no "all clear" row, so the
    page below does not move when a row appears or goes.
  -->
  <section
    v-if="showNeedsYou"
    ref="groupEl"
    class="needs-you"
    tabindex="-1"
    aria-labelledby="needs-you-heading"
  >
    <h2 id="needs-you-heading" class="needs-you-heading">Needs you</h2>

    <!-- The outcome of the last press, in a live region. The button that was
         pressed is often the thing that disappeared, so this is where the
         result is announced once focus has landed back on the list. -->
    <p class="needs-you-status" role="status">{{ groupStatus }}</p>

    <ul v-if="rowCount" class="needs-you-list">
      <NeedsYouRow
        v-for="check in housekeeping.healthErrors"
        :key="`health:${check.id}`"
        tone="error"
        :title="check.title"
        :line="housekeeping.healthFixError || check.detail"
        :alert="!!housekeeping.healthFixError"
        :items="healthItems"
        @select="onHealthSelect"
      >
        <template #primary>
          <button
            type="button"
            class="btn-small btn-primary"
            :disabled="housekeeping.healthFixing"
            @click="fixHealth"
          >{{ housekeeping.healthFixing ? 'Fixing…' : 'Fix' }}</button>
        </template>
      </NeedsYouRow>

      <NeedsYouRow
        v-for="action in orderedActions"
        :key="action.id"
        :tone="action.blocking ? 'blocking' : ''"
        :title="actionTitle(action)"
        :line="actionLine(action)"
        :alert="!!action.command && commandCopy.stateOf(action.id) === 'failed'"
        :notice-key="actionKey(action)"
        :items="actionItems(action)"
        @select="onActionSelect(action, $event)"
      >
        <template #primary>
          <button
            v-if="leadOf(action) === 'view'"
            type="button"
            class="btn-small"
            :class="{ 'btn-primary': action.blocking }"
            @click="openView(action)"
          >{{ action.view_label || 'Open' }}</button>
          <HostTerminalButton
            v-else-if="leadOf(action) === 'terminal'"
            :action="action.terminal_action"
            variant="small"
            label="Update in Terminal"
            :aria-label="`${action.title} in Terminal`"
            @not-applicable="housekeeping.refresh()"
          />
          <button
            v-else-if="leadOf(action) === 'copy'"
            type="button"
            class="btn-small"
            @click="onActionSelect(action, 'copy')"
          >{{ copyLabel(action) }}</button>
          <button
            v-else-if="leadOf(action) === 'run'"
            type="button"
            class="btn-small"
            :class="{ 'btn-primary': action.blocking }"
            :disabled="housekeeping.runningIds.has(action.id)"
            @click="runAction(action)"
          >{{ housekeeping.runningIds.has(action.id) ? 'Running…' : action.run_label }}</button>
          <a
            v-else-if="leadOf(action) === 'link'"
            class="btn-small needs-you-link"
            :class="{ 'btn-primary': action.blocking }"
            :href="action.link_url"
            target="_blank"
            rel="noopener noreferrer"
          >{{ action.link_label || 'Open' }}</a>
          <button
            v-else-if="leadOf(action) === 'chat'"
            type="button"
            class="btn-small"
            :class="{ 'btn-primary': action.blocking }"
            :disabled="chatBusy"
            @click="openChat(action)"
          >{{ chatButtonLabel(action) }}</button>
          <button
            v-else-if="leadOf(action) === 'dismiss'"
            type="button"
            class="btn-small"
            @click="dismissAction(action)"
          >{{ action.dismiss_label }}</button>
        </template>
      </NeedsYouRow>

      <NeedsYouRow
        v-for="task in openUpdateTasks"
        :key="taskKey(task)"
        :title="task.title"
        :line="taskLine(task)"
        :alert="!!taskError(task)"
        :notice-key="updateKey(task)"
        :items="taskItems(task)"
        @select="onTaskSelect(task, $event)"
      >
        <template #primary>
          <!-- waiting_review: the decisions are on the review queue, which has
               per-row accept/dismiss and a destination picker. Sending the
               operator to the chat to re-decide them in prose would be worse
               than naming the surface that already exists. -->
          <button
            v-if="taskLead(task) === 'review'"
            type="button"
            class="btn-small"
            @click="reviewTask()"
          >Review proposals</button>
          <!-- One start button for every state that can start. Idempotent on the
               server per (task, revision), so the label is the only thing that
               differs between "begin" and "carry on". Gate is the lifecycle, not
               the detector (see canStart). -->
          <button
            v-else-if="taskLead(task) === 'start'"
            type="button"
            class="btn-small"
            :disabled="taskBusy(task)"
            @click="startTask(task)"
          >{{ startText(task) }}</button>
          <!-- The one row that reaches here is an offer nobody can say applies,
               so a quiet re-check is the only honest action — never drawn as
               "done". -->
          <button
            v-else
            type="button"
            class="btn-small"
            :disabled="taskBusy(task)"
            @click="recheckTasks()"
          >Check again</button>
        </template>
      </NeedsYouRow>
    </ul>
  </section>
</template>

<style scoped>
.home-notice-unavailable {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}

.needs-you {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  /* The row lays itself out against the list's own width, not the window's: the
     main column is narrower than the pane once a panel is docked. */
  container-type: inline-size;
  container-name: needs-you;
}

/* The list takes focus when a row it owns disappears (see keepFocus), and a
   visible ring is the only way that landing spot is findable by keyboard. The
   heading carries the ring, which is where the eye goes next anyway. */
.needs-you:focus { outline: none; }
.needs-you:focus-visible .needs-you-heading {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}

/* The same Home heading as "Continue where you left off" token for token. */
.needs-you-heading {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
  color: var(--fg);
  width: fit-content;
}

.needs-you-status {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}
/* An empty live region must not reserve a row: the height comes from the text. */
.needs-you-status:empty { display: none; }

.needs-you-list {
  margin: 0;
  padding: 0;
  list-style: none;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}

.needs-you-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  text-decoration: none;
  box-sizing: border-box;
}
</style>
