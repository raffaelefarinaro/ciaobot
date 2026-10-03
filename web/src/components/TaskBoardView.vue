<script setup lang="ts">
/**
 * The workspace task board (`/tasks`).
 *
 * Four fixed columns — Backlog, In progress, On hold, Done — on a wide pane, and
 * one status-filtered list on a narrow one. There is no drag: a card's status is
 * a native `<select>`, which is a control a keyboard, a screen reader and a
 * phone can all use.
 *
 * Every write goes through `stores/taskBoard.ts` at the `revision` this pane read,
 * so a board drawn from an older read gets the server's 409 with its rows intact
 * instead of overwriting what is on disk now.
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import PaneHeader from './PaneHeader.vue'
import { useModalFocus } from '../composables/useModalFocus'
import { useProjectStore } from '../stores/projects'
import { useTaskBoardStore, type TaskChanges } from '../stores/taskBoard'
import { askConfirm } from '../lib/confirm'
import { renderUserMarkdown } from '../lib/safeMarkdown'
import {
  TASK_DUE_FILTERS,
  TASK_NO_PROJECT,
  TASK_STATUS_OPTIONS,
  formatTaskDue,
  invalidTaskRows,
  isOverdue,
  localDateKey,
  matchesDueFilter,
  matchesProjectFilter,
  openTaskCount,
  readableTasks,
  statusCounts,
  taskAssigneeLabel,
  taskLanes,
  taskStatusLabel,
  type TaskDueFilter,
} from '../lib/taskBoard'
import type { Task, TaskStatus } from '../lib/types'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const projectStore = useProjectStore()
const board = useTaskBoardStore()
const workspace = computed(() => projectStore.activeWorkspace)

/**
 * Where the four columns become one list.
 *
 * The same 900px Memory Map draws its tile at, and it is a threshold on the
 * window rather than the pane because that is the measurement this app already
 * reacts to (`isMobile` in ChatLayout is the same idea at 768). Four columns of
 * cards need more room than that; below it the board reads as a scrolling list,
 * which is what a phone wants anyway.
 */
const NARROW_PANE_PX = 900
const isNarrow = ref(typeof window !== 'undefined' && window.innerWidth <= NARROW_PANE_PX)
function onResize() {
  isNarrow.value = typeof window !== 'undefined' && window.innerWidth <= NARROW_PANE_PX
}
if (typeof window !== 'undefined') window.addEventListener('resize', onResize)
onBeforeUnmount(() => {
  if (typeof window !== 'undefined') window.removeEventListener('resize', onResize)
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

function load() {
  void board.ensureLoaded(workspace.value)
}

onMounted(load)
// The board is per workspace, so a switch is the one thing that re-reads it, and
// only when the held rows are another workspace's.
watch(workspace, load)

function retry() {
  board.clearError()
  void board.reload(workspace.value)
}

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
      matchesProjectFilter(task, projectFilter.value) && matchesDueFilter(task, dueFilter.value),
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
  taskLanes(filtered.value, { status: statusFilter.value, narrow: isNarrow.value }),
)

/**
 * The projects a task can name: the unfiled bucket when any task uses it, then
 * this workspace's own projects, then any project id the board has seen that the
 * registry no longer lists.
 *
 * That last group is load-bearing rather than tidy: a `<select>` with no matching
 * `<option>` renders blank, and a Save would then send `project_id: null` and
 * quietly detach the task from a project that exists. Naming the unknown id is
 * the difference between "I cannot see that project" and "I cleared it".
 */
const projectOptions = computed(() => {
  const options: Array<{ value: string; label: string }> = []
  if (tasks.value.some((task) => !task.project_id)) {
    options.push({ value: TASK_NO_PROJECT, label: 'No project' })
  }
  const listed = new Set<string>()
  for (const project of projectStore.workspaceProjects) {
    if (project.project_id === TASK_NO_PROJECT) continue
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

/** The create dialog's list: no "unfiled" entry, because its blank already
 *  means that and the card reads it as General. */
const createProjectOptions = computed(() =>
  projectOptions.value.filter((option) => option.value !== TASK_NO_PROJECT),
)

const projectName = (projectId: string): string => {
  if (!projectId) return 'General'
  return projectStore.workspaceProjects.find((p) => p.project_id === projectId)?.name || projectId
}

/**
 * Whether this board holds the description for a task.
 *
 * A list row carries no `body` and `/api/tasks*` has no read-by-id route, so the
 * only tasks whose Markdown description the board can honestly show are the ones
 * it created or last wrote. Everywhere else the field is absent and a Save omits
 * `body`, because sending an empty one would erase prose nobody was shown. The
 * read-by-id route that would close this gap is an API change, and that is not
 * this surface's to make.
 */
function describesTask(taskId: string): boolean {
  return board.described?.id === taskId
}

// ── Writes ────────────────────────────────────────────────────────────────

const busyTaskId = ref('')

/**
 * Move one card to another column.
 *
 * The `<select>` is put back to the column the row is actually in before the
 * request goes out, so a refused move never leaves a card claiming a column it
 * was never admitted to. Moving *to* Done is the completion gesture, which is
 * why it goes to `complete` rather than through the status field: the store
 * enforces completion as the signed-in user's own act.
 */
async function moveTask(task: Task, event: Event) {
  const select = event.target as HTMLSelectElement
  const next = select.value as TaskStatus
  select.value = task.status
  if (next === task.status) return
  const revision = board.revisionOf(task.id)
  if (!revision) {
    board.clearError()
    return
  }
  board.clearError()
  busyTaskId.value = task.id
  if (next === 'done') await board.complete(workspace.value, task.id, revision)
  else await board.update(workspace.value, task.id, revision, { status: next })
  busyTaskId.value = ''
}

async function markDone(task: Task) {
  const revision = board.revisionOf(task.id)
  if (!revision) return
  board.clearError()
  busyTaskId.value = task.id
  await board.complete(workspace.value, task.id, revision)
  busyTaskId.value = ''
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
const detailEl = ref<HTMLElement | null>(null)
const detailTitleField = ref<HTMLInputElement | null>(null)
const detailSaving = ref(false)
const detailId = ref('')
const detailForm = reactive({
  title: '',
  status: 'backlog' as TaskStatus,
  due: '',
  assignee: 'user' as 'user' | 'agent',
  project_id: '',
  body: '',
})

useModalFocus(detailEl, detailOpen, { initialFocus: detailTitleField, onEscape: () => { if (!detailSaving.value) closeDetail() } })

/** The row a dialog is editing, or undefined once it has been deleted. */
const detailTask = computed(() => tasks.value.find((task) => task.id === detailId.value) ?? null)

/** Whether this dialog is offering the description, and may therefore write it. */
const detailDescribed = computed(() => describesTask(detailId.value))

const detailValid = computed(() => detailForm.title.trim() !== '')

function openDetail(task: Task) {
  board.clearError()
  detailId.value = task.id
  const held = describesTask(task.id) ? board.described : null
  detailForm.title = task.title
  detailForm.status = task.status
  detailForm.due = task.due
  detailForm.assignee = task.assignee
  detailForm.project_id = task.project_id
  detailForm.body = held?.body ?? ''
  detailOpen.value = true
}

function closeDetail() {
  if (detailSaving.value) return
  detailOpen.value = false
  detailId.value = ''
  board.clearError()
}

/** The status field of the open dialog is a move, written the moment it changes. */
async function onDetailStatusChange(event: Event) {
  const select = event.target as HTMLSelectElement
  const next = select.value as TaskStatus
  const task = detailTask.value
  select.value = detailForm.status
  if (!task || next === detailForm.status) return
  const revision = board.revisionOf(task.id)
  if (!revision) return
  board.clearError()
  detailSaving.value = true
  await board.complete(workspace.value, task.id, revision)
  detailSaving.value = false
}

async function saveDetail() {
  const task = detailTask.value
  if (detailSaving.value || !task || !detailValid.value) return
  const changes: TaskChanges = {
    title: detailForm.title.trim(),
    due: detailForm.due || null,
    assignee: detailForm.assignee,
    project_id: detailForm.project_id || null,
  }
  detailSaving.value = true
  // `body` rides only when this dialog is the one that showed it, so a Save on
  // the fields the board does know can never clear a description it never read.
  const saved = await board.update(
    workspace.value,
    task.id,
    board.revisionOf(task.id),
    changes,
    detailDescribed.value ? detailForm.body : undefined,
  )
  detailSaving.value = false
  if (!saved) return
  detailForm.title = saved.title
  detailForm.due = saved.due
  detailForm.assignee = saved.assignee
  detailForm.project_id = saved.project_id
  board.clearError()
}

async function deleteTask() {
  const task = detailTask.value
  if (!task || detailSaving.value) return
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
  if (removed) closeDetail()
}

/**
 * The description as it will read.
 *
 * `renderUserMarkdown`, not `renderMarkdown`: a task body is prose the user
 * edits in this very dialog, so raw HTML in it is escaped and shown as typed
 * rather than parsed. It still goes through DOMPurify.
 */
const bodyPreview = computed(() => renderUserMarkdown(detailForm.body))
const createPreview = computed(() => renderUserMarkdown(createForm.body))

const today = localDateKey()
</script>

<template>
  <div class="task-view">
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
              <option v-for="option in projectOptions" :key="option.value" :value="option.value">
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
        <div v-if="firstLoad" class="task-loading" role="status" aria-live="polite">
          <span class="task-spinner" aria-hidden="true"></span> Loading tasks…
        </div>

        <!-- 2 · first load failed: the server's sentence and a Retry. Never an
             "empty board" claim, which is what a failed GET would be read as. -->
        <div v-else-if="loadFailed" class="task-failed" role="alert">
          <p class="task-failed-text">{{ board.loadError }}</p>
          <button type="button" class="btn-small" @click="retry">Retry</button>
        </div>

        <template v-else>
          <!-- 3 · a refresh failed over rows that are still on screen: they stay,
               marked stale, with the same Retry. -->
          <p v-if="loadStale" class="task-stale" role="status">
            <span>{{ board.loadError }} These are the tasks as they were last read.</span>
            <button type="button" class="btn-chip task-chip" @click="retry">Retry</button>
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
            No tasks in {{ workspace }} yet. New task files one, and Ciaobot's
            <code>ciao task</code> command files the rest.
          </p>

          <!-- One card, one status select, one render path. Wide with no status
               picked the lanes are the four columns; narrow (or filtered) it is
               the one lane, and CSS lays it out as a list. -->
          <div
            v-else
            class="task-lanes"
            :class="lanes.length > 1 ? 'task-lanes--columns' : 'task-lanes--list'"
          >
            <section
              v-for="lane in lanes"
              :key="lane.status || 'all'"
              class="task-lane"
              :aria-label="lane.label"
            >
              <h3 class="task-lane-head">
                <span class="task-lane-label">{{ lane.label }}</span>
                <span class="badge badge--muted">{{ lane.tasks.length }}</span>
              </h3>
              <p v-if="!lane.tasks.length" class="task-lane-empty">Nothing here.</p>
              <ul v-else class="task-cards">
                <li
                  v-for="task in lane.tasks"
                  :key="task.id"
                  class="task-card"
                  @click="openDetail(task)"
                >
                  <div class="task-card-head">
                    <button
                      type="button"
                      class="task-open"
                      :aria-label="`Edit ${task.title}`"
                      @click.stop="openDetail(task)"
                    >{{ task.title }}</button>
                    <span v-if="task.review_state === 'ready'" class="badge badge--accent2">Review</span>
                  </div>
                  <p class="task-meta">
                    <span class="task-project">{{ projectName(task.project_id) }}</span>
                    <span v-if="formatTaskDue(task.due)" class="badge" :class="isOverdue(task, today) ? 'badge--error' : 'badge--muted'">
                      {{ isOverdue(task, today) ? 'Overdue · ' : 'Due ' }}{{ formatTaskDue(task.due) }}
                    </span>
                    <span class="task-assignee">{{ taskAssigneeLabel(task.assignee) }}</span>
                  </p>
                  <div class="task-card-foot">
                    <label class="sr-only" :for="`task-status-${task.id}`">
                      Status for {{ task.title }}
                    </label>
                    <select
                      :id="`task-status-${task.id}`"
                      class="task-filter task-status"
                      :value="task.status"
                      :disabled="busyTaskId === task.id"
                      @click.stop
                      @change="moveTask(task, $event)"
                    >
                      <option v-for="option in TASK_STATUS_OPTIONS" :key="option.status" :value="option.status">
                        {{ option.label }}
                      </option>
                    </select>
                    <button
                      v-if="task.status !== 'done'"
                      type="button"
                      class="btn-chip task-chip"
                      :disabled="busyTaskId === task.id"
                      :aria-label="`Mark ${task.title} done`"
                      @click.stop="markDone(task)"
                    >Done</button>
                  </div>
                </li>
              </ul>
            </section>
          </div>

          <!-- A refused write keeps the rows it had and says the server's own
               sentence; the action slot is separate from the load slot so an
               unread 409 cannot be cleared by a later list read. -->
          <p v-if="board.error" class="task-action-error" role="alert">{{ board.error }}</p>
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
                <option v-for="option in createProjectOptions" :key="option.value" :value="option.value">
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
            <details v-if="createForm.body" class="task-preview">
              <summary>Preview</summary>
              <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
              <div class="task-preview-body markdown" v-html="createPreview"></div>
            </details>
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

    <!-- Detail -->
    <div v-if="detailOpen" class="modal-backdrop" @click.self="closeDetail">
      <div
        ref="detailEl"
        class="modal-sheet task-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-detail-title"
      >
        <header class="task-sheet-head">
          <h3 id="task-detail-title">Edit task</h3>
          <button type="button" class="btn-icon" aria-label="Close" @click="closeDetail">×</button>
        </header>

        <!-- The status field is a move, not a field to save: changing it writes
             at once, through the same gesture the card offers. Moving to Done is
             the completion gesture, which the store keeps as the user's act. -->
        <div class="task-move">
          <label class="sr-only" for="task-detail-status">Move this task to</label>
          <select
            id="task-detail-status"
            class="task-filter task-status"
            :value="detailForm.status"
            :disabled="detailSaving || board.saving"
            @change="onDetailStatusChange"
          >
            <option v-for="option in TASK_STATUS_OPTIONS" :key="option.status" :value="option.status">
              {{ option.label }}
            </option>
          </select>
          <p class="hint">Moving a task to {{ taskStatusLabel('done') }} marks it done.</p>
        </div>

        <form class="task-form" novalidate @submit.prevent="saveDetail">
          <div class="form-group">
            <label for="task-detail-name">Title</label>
            <input
              id="task-detail-name"
              ref="detailTitleField"
              v-model="detailForm.title"
              type="text"
              autocomplete="off"
            />
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
            <div class="form-group">
              <label for="task-detail-assignee">Assignee</label>
              <select id="task-detail-assignee" v-model="detailForm.assignee">
                <option value="user">For me</option>
                <option value="agent">For the agent</option>
              </select>
            </div>
          </div>
          <!-- The description, where this board actually holds it: a list row
               carries none and the API has no read-by-id, so for every other task
               the field is absent rather than an empty box whose Save would clear
               prose nobody was shown. -->
          <div v-if="detailDescribed" class="form-group">
            <label for="task-detail-body">Description</label>
            <textarea
              id="task-detail-body"
              v-model="detailForm.body"
              rows="6"
            ></textarea>
            <details v-if="detailForm.body" class="task-preview">
              <summary>Preview</summary>
              <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
              <div class="task-preview-body markdown" v-html="bodyPreview"></div>
            </details>
          </div>
          <p v-else class="hint">
            This board has not read this task's description, so it will not
            overwrite it. File one with the task, or from the command line.
          </p>

          <p v-if="board.error" class="task-action-error" role="alert">{{ board.error }}</p>

          <div class="form-actions">
            <button
              type="submit"
              class="btn-primary"
              :disabled="!detailValid || detailSaving"
            >{{ detailSaving ? 'Saving…' : 'Save' }}</button>
            <button type="button" class="btn-small" :disabled="detailSaving" @click="closeDetail">Cancel</button>
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
.task-loading,
.task-failed,
.task-filtered-empty {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-5) 0;
}
.task-loading { flex-direction: row; align-items: center; color: var(--fg2); }
.task-failed-text,
.task-stale,
.task-action-error {
  margin: 0;
  color: var(--error);
  font-size: var(--text-sm);
  line-height: 1.5;
}
.task-stale {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0;
}
.task-lede {
  margin: 0 0 var(--space-3);
  color: var(--fg3);
  font-size: var(--text-sm);
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

/* A narrow pane gets the one lane the filters chose, read as a list. The grid
   above already collapsed to a single track; the lane only has to stop dressing
   itself like a column. */
@container chat-pane (max-width: 940px) {
  .task-lanes--columns { grid-template-columns: minmax(0, 1fr); }
}

.task-lane {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
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
.task-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
  min-width: 0;
}
/* The whole card is clickable, and this is the reachable control inside it: a
   pointer-only card is not acceptable, so the title is a real button that carries
   the card's one job. */
.task-open {
  flex: 1 1 auto;
  min-width: 0;
  /* A one-line title is ~20px of text. The reachable control gets a real box so
     it is not a 20px-tall hit area the card's own click handler was quietly
     carrying; the card is still clickable, this is what makes it operable. */
  min-height: var(--touch);
  padding: 0;
  border: 0;
  background: none;
  color: var(--fg);
  font: inherit;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
  overflow-wrap: anywhere;
}
.task-open:hover { color: var(--accent); }
.task-open:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
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
.task-assignee { color: var(--fg3); }
.task-card-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.task-status { flex: 1 1 auto; min-width: 0; }

/* 44px on touch: the shared rule raises .btn-chip, and the native selects and
   buttons here carry it directly so a coarse pointer never gets a 34px target. */
@media (pointer: coarse) {
  .task-filter,
  .task-card .btn-chip {
    min-height: var(--touch);
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
.task-move {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4) 0;
}
.task-move .hint { margin: 0; }
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
.task-preview summary {
  color: var(--fg2);
  font-size: var(--text-sm);
  cursor: pointer;
}
.task-preview-body {
  margin-top: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  overflow-wrap: anywhere;
}
.task-preview-body :deep(p:first-child) { margin-top: 0; }
.task-preview-body :deep(p:last-child) { margin-bottom: 0; }
.task-preview-body :deep(pre) { overflow-x: auto; }
</style>