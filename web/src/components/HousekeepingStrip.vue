<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useHousekeepingStore } from '../stores/housekeeping'
import { useProjectStore } from '../stores/projects'
import { askConfirm } from '../lib/confirm'
import type { OperatorAction, UpdateTaskRow } from '../lib/types'
import { renderMarkdown } from '../lib/safeMarkdown'

const housekeeping = useHousekeepingStore()
const projectStore = useProjectStore()

const chatBusy = ref(false)

/** Actions grouped by the workspace they concern, shared ones first.
 *
 * An action's workspace decides where acting on it writes, so a flat strip made
 * the reader check each tile's prose to work out which one it was about.
 * Anything with no workspace is install-wide, and goes at the top because it
 * applies regardless of which workspace you are looking at.
 */
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

const groups = computed(() => {
  const byWorkspace = new Map<string, OperatorAction[]>()
  for (const action of scopedActions.value) {
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

// The strip has no permanent furniture: with zero actions it renders nothing at
// all (the template guards on a non-empty list before creating any element).
function hasActions(): boolean {
  return housekeeping.actions.length > 0
}

async function runAction(action: OperatorAction): Promise<void> {
  const { ok } = await housekeeping.run(action.id)
  // The star nudge's run records the star; the tile then disappears, so the
  // only feedback is this toast. Only the strip's explicit run button lands
  // here — a link click never records anything (see onLinkClick).
  if (ok && action.id === 'github-star') {
    projectStore.pushToast({
      chat_id: '',
      title: '★ Starred — thank you!',
      body: 'It genuinely helps other developers discover Ciaobot.',
    })
  }
}

// A tile's external link opens in a new tab. The GitHub-star nudge's link
// deliberately does NOT run the action: opening the repository page confirms
// nothing — the visitor may not be signed in, and inspecting or closing the
// tab is not a star. Recording `starred` here would thank users for an
// action they never took and silence the nudge permanently. The tile clears
// through the operator's explicit dismiss ("Later") instead, and other
// links — e.g. release notes on the update tile — never ran the action in
// the first place (clicking "Release notes" must not start an update).
async function onLinkClick(action: OperatorAction): Promise<void> {
  void action
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

function renderedDetail(detail: string): string {
  // Missed-schedule detail is markdown (bullets with [name](/schedules/id)).
  // Other tiles are plain sentences — markdown rendering keeps them as <p>.
  //
  // Details are prose, not authored HTML: a sentence naming a placeholder path
  // ("copy it into skills/<name>/") would otherwise be parsed as an inline tag
  // and dropped by the sanitizer, deleting the one word the sentence is about.
  // Escaping the angle brackets first keeps the text and leaves markdown link
  // and list syntax untouched.
  return renderMarkdown(detail.replace(/</g, '&lt;').replace(/>/g, '&gt;'))
}

async function onDetailClick(event: MouseEvent): Promise<void> {
  const anchor = (event.target as HTMLElement)?.closest('a')
  if (!anchor) return
  const href = anchor.getAttribute('href') || ''
  // Internal schedule links should route without a full reload.
  if (href.startsWith('/schedules')) {
    event.preventDefault()
    const { router } = await import('../router')
    await router.push(href)
  }
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

async function onChatButtonClick(action: OperatorAction): Promise<void> {
  await openChat(action)
}

async function openChat(action: OperatorAction): Promise<void> {
  if (chatBusy.value || !action.chat_prompt) return
  chatBusy.value = true
  try {
    const workspace = action.workspace || projectStore.activeWorkspace
    if (projectStore.activeWorkspace !== workspace) {
      await projectStore.switchWorkspace(workspace)
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

// ── "After this update" ────────────────────────────────────────────────────
//
// The work Ciaobot's own update leaves behind, as cards rather than as prose.
// They live in their own section instead of joining `groups` because their
// policy is different in a way the operator can feel: an operator action is a
// view of the machine and disappears when re-detection says the condition is
// gone, while an update task is a *decision* that persists until somebody
// reverses it, can be reopened from Settings, and must never be drawn as done
// before a completion check has actually said so.

/** The lifecycles where an attempt exists: a chat was opened, is waiting on the
 *  operator, or did not get going. Every state question below turns on this one
 *  list, because it is the difference between "nobody has started this" and
 *  "somebody already did". */
const LIVE_ATTEMPT_STATES: string[] = ['in_progress', 'waiting_review', 'failed']

/** The rows Home puts in front of the operator.
 *
 * Three kinds of thing survive, and everything else is somebody else's history:
 *
 *  - a task the operator already decided about is not news. `dismissed` is
 *    hidden in this scope (and reopenable from Settings → Update task history),
 *    and `completed` is a verdict a completion check reached rather than
 *    something to re-offer.
 *  - an offer that *applies* here is new work, and so is one nobody could rule
 *    on yet (`unknown`): a detector that has not run has not established that
 *    there is nothing to do.
 *  - a live attempt (`in_progress`, `waiting_review`, `failed`) always stays.
 *    Its chat is open or needs a decision, which is not a thing to hide.
 *
 * What is deliberately gone is an offer the detector says does *not* apply.
 * Every install ships the whole catalog, so drawing those would put a permanent
 * "nothing to do" card on the Home of everyone who does not need the task — and
 * an install with no work would no longer have an empty Home. */
const visibleUpdateTasks = computed(() =>
  housekeeping.updateTasks.filter((task) => {
    if (task.status === 'completed' || task.status === 'dismissed') return false
    if (LIVE_ATTEMPT_STATES.includes(task.status)) return true
    return task.applicability !== 'not_applicable'
  }),
)

/** Whether the group draws anything at all.
 *
 * Real rows, plus one exception: a group that still has an announcement to
 * deliver. Hiding the last card removes the button that was pressed, so without
 * this the group would vanish in the same tick — taking with it the only place
 * the outcome could be said and the only place focus could land. An operator who
 * pressed "Hide it" and saw the page rearrange itself in silence has been told
 * nothing about whether it worked.
 *
 * The exception is a *loan*, not a state: `clearGroupStatus` ends it on a timer
 * and on a workspace switch (see `GROUP_STATUS_TTL_MS`), because a heading, a
 * lede and a stale "Hidden …" left standing on Home would outlive the thing they
 * describe — and, across a workspace switch, be attributed to a workspace the
 * operator never pressed anything in.
 *
 * Zero tasks and a *failed* list are otherwise drawn identically — as nothing —
 * because that is the strip's own best-effort contract (`refresh` swallows a
 * failure and leaves the strip empty rather than showing an error, and a Home
 * that raised a red line for a background poll would nag about a condition the
 * operator cannot act on from this page). Silence is not a false "done": the
 * group has no all-clear state at all, so its absence never reads as a verdict.
 *
 * The one place a failed check *is* reported is Settings → Update task history,
 * which fetches for itself and owns the error, its retry, and the empty state. */
const showUpdateGroup = computed(
  () => visibleUpdateTasks.value.length > 0 || groupStatus.value !== '',
)

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
    return 'Its chat is open and waiting for you to decide what to do with it.'
  }
  if (task.status === 'failed') {
    return 'The last attempt did not get going. Starting again reuses the same chat.'
  }
  if (task.status === 'in_progress') {
    // Not "on the left": the chat is a route, and on a phone the list is above
    // the conversation. Where it opens is the layout's business.
    return 'Started. Its chat is open.'
  }
  return 'New since this update.'
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
  if (LIVE_ATTEMPT_STATES.includes(task.status)) return true
  return task.applicability === 'applicable'
}

const groupEl = ref<HTMLElement | null>(null)
/** The outcome of the last press. Declared here rather than beside the click
 *  handlers because `showUpdateGroup` reads it: a group that still owes the
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
  const hasChat = task.status === 'in_progress' || task.status === 'waiting_review' || !!task.chat_id
  const message = hasChat
    ? 'Hide this task in this workspace? Its chat stays open and is not marked ' +
      'done — you can carry on in it, and reopen the task from Settings → ' +
      'Update task history.'
    : 'Hide this task in this workspace? It stays hidden for this revision only, ' +
      'and you can reopen it from Settings → Update task history.'
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

// Applicability and state are per workspace, so a workspace switch re-asks the
// question rather than leaving the previous workspace's answers on screen — and
// takes the last press's announcement with them, since that press was made in
// the workspace being left, not in the one now on screen.
watch(
  () => projectStore.activeWorkspace,
  (next, previous) => {
    if (next && next !== previous) {
      clearGroupStatus()
      void housekeeping.refreshUpdateTasks(next)
    }
  },
)
</script>

<template>
  <!--
    Two sections, not one list. The operator actions above are preconditions and
    fixes, and a blocking one has to be seen first; the update tasks below are
    optional work the update left behind, with a different policy (a decision
    that persists, reopenable from Settings). Merging them would let an optional
    card sort above an unmissable one and would make "hide this" mean two
    different things in one list.
  -->
  <section v-if="hasActions()" class="housekeeping" aria-label="Housekeeping actions">
    <template v-for="group in groups" :key="group.workspace || '_shared'">
      <p v-if="showHeadings" class="housekeeping-group">
        {{ group.workspace || 'shared' }}
      </p>
      <article
        v-for="action in group.actions"
        :key="action.id"
        class="housekeeping-tile"
        :class="{ 'housekeeping-tile--blocking': action.blocking }"
      >
      <div class="housekeeping-body">
        <p class="housekeeping-title">{{ action.title }}</p>
        <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
        <div class="housekeeping-detail" v-html="renderedDetail(action.detail)" @click="onDetailClick"></div>
      </div>
      <div class="housekeeping-actions">
        <!-- A view-led tile (the update tile) puts its view button first and
             filled, in DOM order so focus and screen readers meet it first. -->
        <button
          v-if="viewLeads(action)"
          type="button"
          class="btn-small btn-primary"
          @click="openView(action)"
        >
          {{ action.view_label || 'Open' }}
        </button>
        <button
          v-if="action.run_label"
          type="button"
          class="btn-small btn-primary"
          :disabled="housekeeping.runningIds.has(action.id)"
          @click="runAction(action)"
        >
          {{ housekeeping.runningIds.has(action.id) ? 'Running…' : action.run_label }}
        </button>
        <a
          v-if="action.link_url"
          class="btn-small housekeeping-link"
          :class="viewLeads(action) ? 'btn-chip' : 'btn-primary'"
          :href="action.link_url"
          target="_blank"
          rel="noopener noreferrer"
          @click="onLinkClick(action)"
        >{{ action.link_label || 'Open' }}</a>
        <button
          v-if="action.view_route && !viewLeads(action)"
          type="button"
          class="btn-small btn-primary"
          @click="openView(action)"
        >
          {{ action.view_label || 'Open' }}
        </button>
        <button
          v-if="action.chat_prompt"
          type="button"
          class="btn-small btn-chip"
          :disabled="chatBusy"
          @click="onChatButtonClick(action)"
        >
          {{ chatButtonLabel(action) }}
        </button>
        <button
          v-if="action.dismiss_label"
          type="button"
          class="btn-small btn-chip"
          @click="dismissAction(action)"
        >{{ action.dismiss_label }}</button>
        </div>
      </article>
    </template>
  </section>

  <section
    v-if="showUpdateGroup"
    ref="groupEl"
    class="update-tasks"
    tabindex="-1"
    aria-labelledby="update-tasks-heading"
  >
    <h2 id="update-tasks-heading" class="update-tasks-heading">After this update</h2>
    <p class="update-tasks-lede">
      Work this version of Ciaobot left behind for
      {{ projectStore.activeWorkspace || 'this install' }}.
    </p>

    <!-- The outcome of the last press, in a live region. The button that was
         pressed is often the thing that disappeared, so this is where the
         result is announced once focus has landed back on the group. -->
    <p class="update-tasks-status" role="status">{{ groupStatus }}</p>

    <article
      v-for="task in visibleUpdateTasks"
      :key="taskKey(task)"
      class="housekeeping-tile update-task"
    >
      <div class="housekeeping-body">
        <p class="housekeeping-title">{{ task.title }}</p>
        <div class="housekeeping-detail">{{ task.why }}</div>
        <p class="update-task-meta">
          <span>{{ taskStateLine(task) }}</span>
          <span v-if="task.since_version">Since Ciaobot {{ task.since_version }}</span>
        </p>
        <!-- A refusal leaves the card exactly as it was and says why here. A
             cleared card would read as "handled", which is the one thing a
             failed start, a refused dismiss and a declined reopen have in
             common and none of them are. -->
        <p v-if="taskError(task)" class="update-task-error" role="alert">
          {{ taskError(task) }}
        </p>
      </div>
      <div class="housekeeping-actions">
        <!-- waiting_review: the decisions are on the review queue, which has
             per-row accept/dismiss and a destination picker. Sending the
             operator to the chat to re-decide them in prose would be worse than
             naming the surface that already exists. -->
        <button
          v-if="task.status === 'waiting_review'"
          type="button"
          class="btn-small btn-primary"
          @click="reviewTask()"
        >Review proposals</button>

        <!-- One start button for every state that can start. Idempotent on the
             server per (task, revision), so the label is the only thing that
             differs between "begin" and "carry on". Gate is the lifecycle, not
             the detector (see canStart): a live attempt gets Resume even when
             the detector has already decided the work is not needed, because
             the chat is open and stranding it is the worse outcome. -->
        <button
          v-if="canStart(task)"
          type="button"
          class="btn-small"
          :class="task.status === 'offered' ? 'btn-primary' : 'btn-chip'"
          :disabled="taskBusy(task)"
          @click="startTask(task)"
        >
          <template v-if="taskBusy(task)">Working…</template>
          <template v-else-if="task.status === 'in_progress'">Resume chat</template>
          <template v-else-if="task.status === 'waiting_review'">Resume chat</template>
          <template v-else-if="task.status === 'failed'">Try again</template>
          <template v-else>Start in chat</template>
        </button>

        <!-- The one row that reaches here is an offer nobody can say applies:
             `visibleUpdateTasks` drops an offer the detector has ruled out, and
             `canStart` gives every live attempt its Resume. So a quiet re-check
             is the only honest action left — and it is never drawn as "done". -->
        <button
          v-else
          type="button"
          class="btn-small btn-chip"
          :disabled="taskBusy(task)"
          @click="recheckTasks()"
        >Check again</button>

        <button
          v-if="task.chat_id"
          type="button"
          class="btn-small btn-chip"
          @click="openTaskChat(task.chat_id)"
        >Open its chat</button>

        <button
          type="button"
          class="btn-small btn-chip"
          :disabled="taskBusy(task)"
          @click="dismissTask(task)"
        >Hide it</button>
      </div>
    </article>
  </section>
</template>

<style scoped>
/* A blocking action is a precondition the install cannot get past on its own,
   so it reads as a warning rather than as one tile among several. Not a modal
   and not an app-wide lock: the one realistic cause is an uncommitted vault, and
   locking the app would take away the assistant needed to fix it. */
.housekeeping-tile--blocking {
  border-top-color: var(--warning);
  background: rgba(210, 153, 34, 0.08);
  padding-inline: var(--space-3);
  border-radius: var(--radius);
}

.housekeeping-group {
  margin: var(--space-2) 0 0;
  font-size: 0.72rem;
  text-transform: lowercase;
  letter-spacing: 0.04em;
  color: var(--fg2);
}

.housekeeping-group:first-child {
  margin-top: 0;
}

.housekeeping {
  width: 100%;
  max-width: var(--home-max);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  margin-block: var(--space-3);
}

.housekeeping-tile {
  display: grid;
  grid-template-columns: 1fr;
  grid-template-areas:
    'body'
    'actions';
  column-gap: var(--space-2);
  row-gap: var(--space-2);
  align-items: start;
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
  min-width: 0;
}

@media (min-width: 640px) {
  .housekeeping-tile {
    grid-template-columns: 1fr auto;
    grid-template-areas: 'body actions';
    align-items: center;
  }

  .housekeeping-actions {
    min-width: 160px;
  }

  .housekeeping-actions .btn-small {
    width: auto;
    min-width: 140px;
  }
}

.housekeeping-body {
  grid-area: body;
  min-width: 0;
}

.housekeeping-title {
  margin: 0;
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--fg);
}

.housekeeping-detail {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}

.housekeeping-detail :deep(a) {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.housekeeping-detail :deep(ul) {
  margin: var(--space-1) 0 0;
  padding-left: 1.1em;
}

.housekeeping-detail :deep(li) {
  margin: 2px 0;
}

.housekeeping-detail :deep(p) {
  margin: 0;
}

.housekeeping-detail :deep(p + ul) {
  margin-top: var(--space-1);
}

.housekeeping-actions {
  grid-area: actions;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-1);
}

.housekeeping-actions .btn-small {
  width: 100%;
}

/* The link variant is an <a> styled like a button: same size, centered text,
   no underline. It inherits .btn-small plus .btn-primary (or .btn-chip on a
   chat-led tile) from the global button tokens; only the anchor-specific
   resets are needed here. */
.housekeeping-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  text-decoration: none;
  box-sizing: border-box;
}

/* ── "After this update" ────────────────────────────────────────────────────
   The same hairline row and the same buttons the strip above uses — an update
   task is a different *kind* of thing, not a different design language, and a
   reader who knows the strip already knows these. What changes is the heading:
   this is a group with a name, where the strip's tiles are a list without one. */

.update-tasks {
  width: 100%;
  max-width: var(--home-max);
  margin: 0 auto;
  margin-block: var(--space-3);
  display: flex;
  flex-direction: column;
}

/* The group takes focus when a card it owns disappears (see keepFocus), and a
   visible ring is the only way that landing spot is findable by keyboard. The
   heading carries the focus outline instead, which is where the eye goes next
   anyway and keeps the ring off a full-width empty row. */
.update-tasks:focus {
  outline: none;
}

.update-tasks:focus-visible .update-tasks-heading {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}

/* The one Home heading shared with `HomeRecentChats`'s "Continue where you left
   off", token for token — same size, weight and tracking — so a Home with both
   on screen has one heading scale rather than two. */
.update-tasks-heading {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
  color: var(--fg);
  width: fit-content;
}

.update-tasks-lede {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--fg3);
  max-width: 76ch;
}

.update-tasks-status {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}

/* An empty live region must not reserve a row: `role="status"` regions are
   always in the accessibility tree, so the height has to come from the text
   rather than from a min-height. */
.update-tasks-status:empty {
  display: none;
}

.update-tasks-note {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}

.update-task .housekeeping-detail {
  margin-top: 2px;
}

.update-task-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1) var(--space-3);
  margin: var(--space-1) 0 0;
  font-size: var(--text-xs);
  color: var(--fg3);
}

.update-task-error {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--error);
}
</style>
