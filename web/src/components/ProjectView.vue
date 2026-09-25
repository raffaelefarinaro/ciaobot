<template>
  <div v-if="project" class="project-view">
    <PaneHeader page-tag="Project" @open-sidebar="emit('open-sidebar')">
      <template #title>
        <div class="header-left">
          <button class="btn-icon close-btn desktop-only" @click="$emit('close')" title="Close" aria-label="Close project">&times;</button>
          <input
            v-if="editingName"
            class="title-input"
            v-model="nameDraft"
            aria-label="Project name"
            @keyup.enter="saveName"
            @keyup.escape="editingName = false"
            @blur="saveName"
            autofocus
          />
          <!-- The project name is the title. Its workspace is named once, in
               the sidebar's workspace scope, so it is not repeated here. -->
          <h2 v-else class="project-title" @dblclick="startEditName">{{ project.name }}</h2>
        </div>
      </template>
      <template #actions>
        <button type="button" class="btn-small project-new-chat" aria-haspopup="dialog" @click="newChat">New chat</button>
        <button
          v-if="project.vault_folder && !project.is_auto"
          class="btn-small"
          @click="doComplete"
        >Complete</button>
        <div
          v-if="!project.vault_folder && !project.is_auto"
          ref="projectActionsEl"
          class="project-actions"
        >
          <button
            ref="projectActionsTrigger"
            type="button"
            class="btn-icon"
            aria-label="Project actions"
            aria-haspopup="menu"
            aria-controls="project-actions-menu"
            :aria-expanded="actionsOpen"
            @click="toggleProjectActions"
          >•••</button>
          <div
            v-if="actionsOpen"
            id="project-actions-menu"
            ref="projectActionsMenu"
            class="project-actions-menu"
            role="menu"
            aria-label="Project actions"
            @keydown="onProjectActionsKeydown"
          >
            <button type="button" role="menuitem" class="danger" @click="doDelete">Delete project</button>
          </div>
        </div>
      </template>
    </PaneHeader>

    <div class="page-grid project-grid">
      <div class="page-main">
        <section class="project-section project-context" aria-labelledby="project-context-title">
          <div class="section-head">
            <h3 id="project-context-title">Context</h3>
            <div class="section-actions">
              <span v-if="contextStatus" class="status" :class="contextStatus">{{ contextStatusLabel }}</span>
              <button
                v-if="!editingContext"
                type="button"
                class="text-action"
                @click="startEditContext"
              >Edit</button>
              <template v-else>
                <button type="button" class="btn-small" :disabled="contextSaving" @click="cancelContextEdit">Cancel</button>
                <button
                  type="button"
                  class="btn-small btn-primary"
                  :disabled="!contextDirty || contextSaving"
                  @click="saveContext"
                >{{ contextSaving ? 'Saving…' : 'Save' }}</button>
              </template>
            </div>
          </div>
          <label v-if="editingContext" class="sr-only" :for="`${uid}-project-context`">Project context</label>
          <textarea
            v-if="editingContext"
            :id="`${uid}-project-context`"
            v-model="contextDraft"
            class="context-textarea"
            placeholder="Describe the outcome, constraints, and useful sources for this project."
            rows="6"
          ></textarea>
          <p v-else class="context-display" :class="{ 'context-display--empty': !project.context }">
            {{ project.context || 'No project context yet. Add the outcome, constraints, and sources Ciao should keep in mind.' }}
          </p>
          <!-- Saving writes this into the canonical doc's `description:`
               frontmatter, and a doc edit flows back here. Say so, or the write
               into a vault file is invisible from the button that causes it. -->
          <p class="context-hint">
            Sent with every message in this project.
            <template v-if="project.vault_doc_path">
              Saved to <button type="button" class="link-btn" @click="openContextDoc">{{ project.vault_doc_path }}</button>
            </template>
          </p>
        </section>

        <section class="project-section" aria-labelledby="project-chats-title">
          <div class="section-head">
            <h3 id="project-chats-title">Chats</h3>
          </div>
          <div v-if="activeChats.length" class="chat-list">
            <button
              v-for="chat in activeChats"
              :key="chat.chat_id"
              type="button"
              class="chat-row"
              :class="{ remote: chat.local === false }"
              :disabled="chat.local === false"
              @click="openChat(chat.chat_id)"
              :title="chat.local === false ? 'This chat lives on another instance' : ''"
            >
              <span class="chat-row-main">
                <span class="chat-row-heading">
                  <!-- Chat-level unread is title weight, not a digit: chatUnread() is
                       binary, so a badge could only ever read "1". -->
                  <span
                    class="chat-name"
                    :class="{ 'chat-name--unread': store.chatUnread(chat.chat_id) > 0 }"
                  >{{ chat.title }}</span>
                  <ChatSignals :chat-id="chat.chat_id" density="row" :hue="workspaceHue" />
                  <span v-if="chat.local === false" class="remote-chip">remote</span>
                </span>
                <!-- Same status grammar as Home's rows, read from the same
                     signals, so a chat never reads differently in two places. -->
                <span class="chat-row-sub" :class="{ 'chat-row-sub--needs': store.chatNeedsInput(chat.chat_id) }">{{ chatStatusPhrase(chat) }}</span>
              </span>
              <span class="chat-row-time">{{ formatRelative(chatActivity(chat)) }}</span>
            </button>
          </div>
          <p v-else class="empty-row">No active chats in this project. <button type="button" class="link-btn" @click="newChat">Start one</button></p>
        </section>

        <section
          v-if="showFilesSection"
          class="project-section project-files"
          :class="{ 'drag-over': dragOver }"
          aria-labelledby="project-files-title"
          @dragover.prevent="onDragOver"
          @dragleave="onDragLeave"
          @drop.prevent="onDrop"
        >
          <div class="section-head">
            <h3 id="project-files-title">Files<span v-if="files.length" class="section-count">{{ files.length }}</span></h3>
            <div class="section-actions">
              <span v-if="filesLoading" class="status">Loading…</span>
              <span v-else-if="uploading" class="status">Uploading…</span>
              <button type="button" class="text-action" @click="triggerUpload" :disabled="uploading">Upload</button>
              <input
                ref="uploadInput"
                type="file"
                multiple
                class="hidden-input"
                @change="onFilePicked"
              />
            </div>
          </div>

          <div v-if="filesError" class="upload-errors">
            <div class="upload-error">{{ filesError }}</div>
          </div>

          <div v-if="uploadErrors.length" class="upload-errors">
            <div v-for="(err, i) in uploadErrors" :key="i" class="upload-error">
              {{ err.filename }}: {{ err.error }}
            </div>
            <button class="btn-tiny" @click="uploadErrors = []">Dismiss</button>
          </div>

          <p v-if="!files.length && !filesLoading && !filesError" class="empty-row">
            No files yet. Drop one here or use Upload.
          </p>

          <div v-if="markdownFiles.length" class="file-group">
            <div class="file-group-label">Markdown</div>
            <button
              v-for="f in markdownFiles"
              type="button"
              :key="f.path"
              class="file-row"
              @click="openFile(f)"
            >
              <AppIcon class="file-icon" name="doc" :size="18" />
              <span class="file-name">{{ f.path }}</span>
              <span class="file-meta">{{ formatSize(f.size) }} · {{ formatFileTime(f.mtime) }}</span>
            </button>
          </div>

          <div v-if="imageFiles.length" class="file-group">
            <div class="file-group-label">Images</div>
            <button
              v-for="f in imageFiles"
              type="button"
              :key="f.path"
              class="file-row"
              @click="openFile(f)"
            >
              <img
                class="file-thumb"
                :src="`/api/workspace-image?path=${encodeURIComponent(f.vault_path)}`"
                :alt="f.path"
                loading="lazy"
              />
              <span class="file-name">{{ f.path }}</span>
              <span class="file-meta">{{ formatSize(f.size) }} · {{ formatFileTime(f.mtime) }}</span>
            </button>
          </div>

          <div v-if="otherFiles.length" class="file-group">
            <div class="file-group-label">Other</div>
            <button
              v-for="f in otherFiles"
              type="button"
              :key="f.path"
              class="file-row"
              @click="openFile(f)"
            >
              <AppIcon class="file-icon" name="file" :size="18" />
              <span class="file-name">{{ f.path }}</span>
              <span class="file-meta">{{ formatSize(f.size) }} · {{ formatFileTime(f.mtime) }}</span>
            </button>
          </div>
        </section>

        <section class="project-section project-automations" aria-labelledby="project-automations-title">
          <div class="section-head">
            <div>
              <h3 id="project-automations-title">Automations<span v-if="scheduleCount" class="section-count">{{ scheduleCount }}</span></h3>
              <p class="section-hint">Prompts dispatched into this project or one of its chats on a cadence.</p>
            </div>
          </div>
          <div v-if="projectSchedules.length" class="automation-list">
            <button
              v-for="schedule in projectSchedules"
              :key="schedule.schedule_id"
              type="button"
              class="automation-row"
              @click="openAutomation(schedule.schedule_id)"
            >
              <span class="automation-row-main">
                <span class="automation-title" :class="{ 'automation-title--paused': !schedule.enabled }">{{ schedule.title || promptTitle(schedule.prompt) }}</span>
                <span class="automation-row-meta">
                  <span>{{ scheduleFrequencyLabel(schedule) }}</span>
                  <span class="dot">·</span>
                  <span>{{ scheduleTargetLabel(schedule) }}</span>
                </span>
              </span>
              <span class="automation-next">{{ schedule.enabled ? `next ${automationTimestamp(schedule.next_run)}` : 'Paused' }}</span>
            </button>
            <div v-if="taskStore.scheduleLoadError" class="automation-stale" role="status">
              Could not refresh automations. Showing the last successful load.
              <button type="button" class="btn-small" @click="loadAutomations">Retry</button>
            </div>
          </div>
          <p v-else-if="schedulesState === 'loading'" class="empty-row">Loading automations…</p>
          <p v-else-if="schedulesState === 'error'" class="empty-row" role="alert">
            Could not load automations.
            <button type="button" class="btn-small" @click="loadAutomations">Retry</button>
          </p>
          <p v-else class="empty-row">No automations deliver prompts to this project.</p>
        </section>

        <section v-if="archivedChats.length" class="project-section" aria-labelledby="project-archived-title">
          <div class="section-head">
            <h3 id="project-archived-title">Archived<span class="section-count">{{ archivedChats.length }}</span></h3>
          </div>
          <div class="chat-list">
            <button
              v-for="chat in pagedArchivedChats"
              :key="chat.chat_id"
              type="button"
              class="chat-row archived"
              :class="{ clickable: chat.archive_path, tidying: store.chatIsPostprocessing(chat.chat_id) }"
              :disabled="!chat.archive_path"
              @click="openArchive(chat)"
            >
              <span class="chat-row-main">
                <span class="chat-name">{{ chat.title }}</span>
                <!-- What Ciaobot is taking from this chat, or took. Doubles as an
                     index: it separates archives that produced durable knowledge
                     from the ones that were dead ends. -->
                <span
                  v-if="store.chatIsPostprocessing(chat.chat_id)"
                  class="chat-archive-note"
                >
                  <span class="chat-archive-dot" aria-hidden="true" />
                  {{ postprocessLabel(store.chatPostprocess(chat.chat_id)) }}…
                </span>
                <span
                  v-else-if="archiveSummary(chat.chat_id)"
                  class="chat-archive-note"
                  :class="{ failed: postprocessFailed(store.chatPostprocess(chat.chat_id)) }"
                >{{ archiveSummary(chat.chat_id) }}</span>
              </span>
              <span class="chat-row-time">{{ formatDate(chat.created_at) }}</span>
            </button>
          </div>
          <div v-if="archivedChats.length > ARCHIVED_PER_PAGE" class="pagination">
            <button
              class="btn-small"
              :disabled="archivedPage === 0"
              @click="archivedPage--"
            >Previous</button>
            <span class="page-info">{{ archivedPage + 1 }} / {{ totalArchivedPages }}</span>
            <button
              class="btn-small"
              :disabled="archivedPage >= totalArchivedPages - 1"
              @click="archivedPage++"
            >Next</button>
          </div>
        </section>
      </div>

      <aside class="page-rail project-rail" aria-label="Project summary">
        <section class="rail-section">
          <h3 class="rail-title">Now</h3>
          <div class="rail-kvs">
            <div class="rail-kv"><span>Need you</span><strong :class="{ 'rail-attention': needsInputCount > 0 }">{{ needsInputCount }}</strong></div>
            <div class="rail-kv"><span>Working</span><strong>{{ workingCount }}</strong></div>
            <div class="rail-kv"><span>Unread</span><strong>{{ totalUnread }}</strong></div>
            <div class="rail-kv"><span>Active chats</span><strong>{{ activeChats.length }}</strong></div>
            <div v-if="archivedChats.length" class="rail-kv"><span>Archived</span><strong>{{ archivedChats.length }}</strong></div>
            <div v-if="project.created_at" class="rail-kv"><span>Created</span><strong>{{ formatDate(project.created_at) }}</strong></div>
          </div>
        </section>
        <section class="rail-section">
          <h3 class="rail-title">Lives in</h3>
          <p v-if="project.vault_folder" class="rail-path"><code>{{ project.vault_folder }}</code></p>
          <p v-else class="rail-note">No vault folder: this project's chats and context live in Ciaobot.</p>
          <p v-if="project.vault_doc_path" class="rail-note">
            Project doc <button type="button" class="link-btn" @click="openContextDoc">{{ project.vault_doc_path }}</button>
          </p>
        </section>
      </aside>
    </div>
  </div>
  <div v-else class="empty-state">Project not found.</div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount, onMounted, nextTick, useId } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import { useFileViewerStore } from '../stores/fileViewer'
import { askConfirm } from '../lib/confirm'
import { formatRelative } from '../lib/relativeTime'
import { chatActivityTimestamp } from '../lib/homeLanes'
import { postprocessFailed, postprocessLabel, postprocessSummary } from '../lib/postprocessView'
import { colorForWorkspace } from '../lib/workspaceColors'
import { openNewChatPicker } from '../lib/newChat'
import PaneHeader from './PaneHeader.vue'
import ChatSignals from './ChatSignals.vue'
import AppIcon from './AppIcon.vue'
import type { ChatInfo, Schedule } from '../lib/types'

interface ProjectFile {
  path: string
  vault_path: string
  kind: 'markdown' | 'image' | 'text' | 'binary'
  size: number
  mtime: string
}

const props = defineProps<{ projectId: string }>()
const emit = defineEmits<{ close: [], 'open-sidebar': [] }>()

const store = useProjectStore()
const taskStore = useTaskStore()
const router = useRouter()

const project = computed(() => store.projects.find(p => p.project_id === props.projectId) || null)

// Unique per mounted instance, so the context editor's label/textarea pair
// stays valid even if two project views are ever alive at once.
const uid = useId()

watch(project, (p) => {
  if (p && p.workspace && p.workspace !== store.activeWorkspace) {
    store.activeWorkspace = p.workspace
  }
}, { immediate: true })

const allChats = computed(() => store.chats.filter(c => c.project_id === props.projectId))
const projectChatIds = computed(() => new Set(allChats.value.map(chat => chat.chat_id)))
const projectSchedules = computed(() =>
  taskStore.schedules.filter(schedule =>
    schedule.web_project_id === props.projectId
      || (schedule.web_chat_id !== null && projectChatIds.value.has(schedule.web_chat_id)),
  ),
)

function automationState(count: number): 'list' | 'loading' | 'error' | 'none' {
  if (count > 0) return 'list'
  if (taskStore.scheduleLoadError) return 'error'
  if (taskStore.scheduleLoading || !taskStore.schedulesLoaded) return 'loading'
  return 'none'
}
const schedulesState = computed(() => automationState(projectSchedules.value.length))

// undefined means "unknown", which renders as no count at all rather than a 0
// that would be indistinguishable from a real zero.
const scheduleCount = computed(() =>
  schedulesState.value === 'loading' || schedulesState.value === 'error'
    ? undefined
    : projectSchedules.value.length,
)

async function loadAutomations(): Promise<void> {
  try {
    await taskStore.fetchSchedules()
  } catch {
    // The shared task store retains the error and the last successful snapshot.
  }
}
const activeChats = computed(() =>
  allChats.value
    // Hide remote chats (session lives on another device, not openable here).
    .filter(c => !c.archived && c.local !== false)
    // Sorted by the same timestamp the row displays. Sorting by created_at
    // while showing last activity made the visible times non-monotonic, so the
    // list read as unsorted.
    .sort((a, b) => chatActivityTimestamp(b).localeCompare(chatActivityTimestamp(a)))
)
const archivedChats = computed(() =>
  allChats.value
    .filter(c => c.archived)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
)
const totalUnread = computed(() => store.projectUnread(props.projectId))
const needsInputCount = computed(() => store.projectNeedsInput(props.projectId))
const workingCount = computed(() =>
  activeChats.value.filter(c => store.chatIsWorking(c.chat_id)).length,
)

// Hue follows the project's workspace so the marks here read the same as the
// sidebar and the home lanes rather than inheriting the active accent.
const workspaceHue = computed(() =>
  colorForWorkspace(store.workspaceOptions.find(w => w.name === project.value?.workspace)),
)

function chatActivity(chat: ChatInfo): string {
  return chatActivityTimestamp(chat)
}

// Home's tier order: a question or permission beats work in flight, which
// beats an unread reply.
function chatStatusPhrase(chat: ChatInfo): string {
  if (store.chatNeedsInput(chat.chat_id)) return 'waiting for you'
  if (store.chatIsWorking(chat.chat_id)) return 'agent is working'
  if (store.chatUnread(chat.chat_id) > 0) return 'new reply'
  return 'no new activity'
}

function promptTitle(prompt: string): string {
  const first = prompt.split('\n')[0].trim()
  return first.length > 60 ? first.slice(0, 57) + '...' : first
}

function scheduleTargetLabel(schedule: Schedule): string {
  if (schedule.web_project_id === props.projectId) return 'new chat per run'
  if (schedule.web_chat_id) {
    return allChats.value.find(chat => chat.chat_id === schedule.web_chat_id)?.title
      || schedule.context_label
      || 'Unavailable chat'
  }
  return schedule.context_label || 'General'
}

function scheduleFrequencyLabel(schedule: Schedule): string {
  if (schedule.frequency === 'interval') return `every ${schedule.interval_minutes} min`
  if (schedule.frequency === 'once') return `once · ${schedule.run_at_date || 'date pending'}`
  if (schedule.frequency === 'manual') return 'manual'
  if (schedule.frequency === 'monthly') return `monthly · day ${schedule.day_of_month || '—'}`
  if (schedule.frequency === 'weekly') {
    return schedule.days_of_week?.length ? `weekly · ${schedule.days_of_week.join(', ')}` : 'weekly'
  }
  return 'daily'
}

// Past timestamps go through the shared helper — this file already had a
// private formatRelative removed once for forking the dialect (DESIGN_SYSTEM
// §6), and the same options as formatFileTime keep the two readings identical.
// formatRelative is past-only (it clamps negative elapsed time to zero), so a
// future next_run would read as "just now" and gets an absolute date instead.
function automationTimestamp(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const timestamp = Date.parse(iso)
  if (!Number.isFinite(timestamp)) return iso
  if (timestamp > Date.now()) {
    return new Date(timestamp).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    })
  }
  return formatRelative(iso, { suffix: true, absoluteAfterDays: 7 })
}

function openAutomation(id: string): void {
  router.push(`/schedules/${id}`)
}

const ARCHIVED_PER_PAGE = 10
const archivedPage = ref(0)
const pagedArchivedChats = computed(() => {
  const start = archivedPage.value * ARCHIVED_PER_PAGE
  return archivedChats.value.slice(start, start + ARCHIVED_PER_PAGE)
})
const totalArchivedPages = computed(() => Math.ceil(archivedChats.value.length / ARCHIVED_PER_PAGE) || 1)

// What the post-archive pipeline produced for an archived chat, once it has
// settled. Empty for chats archived before this existed, so old rows stay clean
// rather than claiming "nothing durable to save".
function archiveSummary(chatId: string): string {
  return postprocessSummary(store.chatPostprocess(chatId))
}

// ── Name edit ──────────────────────────────────────────────────────────
const editingName = ref(false)
const nameDraft = ref('')
function startEditName() {
  if (!project.value) return
  nameDraft.value = project.value.name
  editingName.value = true
}
async function saveName() {
  if (!project.value) return
  const name = nameDraft.value.trim()
  if (name && name !== project.value.name) {
    await store.updateProject(project.value.project_id, { name })
  }
  editingName.value = false
}

// ── Context edit ───────────────────────────────────────────────────────
const editingContext = ref(false)
const actionsOpen = ref(false)
const projectActionsEl = ref<HTMLElement | null>(null)
const projectActionsTrigger = ref<HTMLButtonElement | null>(null)
const projectActionsMenu = ref<HTMLElement | null>(null)

function closeProjectActions(restoreFocus = false) {
  if (!actionsOpen.value) return
  actionsOpen.value = false
  if (restoreFocus) void nextTick(() => projectActionsTrigger.value?.focus())
}

function toggleProjectActions() {
  if (actionsOpen.value) {
    closeProjectActions(true)
    return
  }
  actionsOpen.value = true
  void nextTick(() => {
    projectActionsMenu.value?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()
  })
}

function onProjectActionsKeydown(event: KeyboardEvent) {
  const items = Array.from(projectActionsMenu.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
  if (!items.length) return
  const current = items.indexOf(document.activeElement as HTMLButtonElement)
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closeProjectActions(true)
    return
  }
  if (event.key === 'Tab') {
    closeProjectActions(false)
    return
  }
  let next = current
  if (event.key === 'ArrowDown') next = (current + 1) % items.length
  else if (event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = items.length - 1
  else return
  event.preventDefault()
  items[next]?.focus()
}

function closeProjectActionsOnOutside(event: MouseEvent) {
  if (!actionsOpen.value) return
  if (event.target instanceof Node && projectActionsEl.value?.contains(event.target)) return
  closeProjectActions(false)
}
const contextDraft = ref('')
const contextSaving = ref(false)
const contextStatus = ref<'' | 'saved' | 'error'>('')

watch(
  () => [props.projectId, project.value?.context] as const,
  ([, ctx]) => {
    contextDraft.value = ctx || ''
    editingContext.value = false
    editingName.value = false
    closeProjectActions(false)
  },
  { immediate: true }
)

const contextDirty = computed(() => (project.value?.context || '') !== contextDraft.value)
const contextStatusLabel = computed(() => {
  if (contextStatus.value === 'saved') return 'Saved'
  if (contextStatus.value === 'error') return 'Error'
  return ''
})

function startEditContext() {
  if (!project.value) return
  contextDraft.value = project.value.context || ''
  contextStatus.value = ''
  editingContext.value = true
}

function cancelContextEdit() {
  if (!project.value || contextSaving.value) return
  contextDraft.value = project.value.context || ''
  contextStatus.value = ''
  editingContext.value = false
}

async function saveContext() {
  if (!project.value || !contextDirty.value) return
  const projectId = project.value.project_id
  const context = contextDraft.value
  contextSaving.value = true
  contextStatus.value = ''
  try {
    await store.updateProject(projectId, { context })
    if (project.value?.project_id !== projectId) return
    contextStatus.value = 'saved'
    editingContext.value = false
    setTimeout(() => { if (contextStatus.value === 'saved') contextStatus.value = '' }, 2000)
  } catch {
    if (project.value?.project_id === projectId) contextStatus.value = 'error'
  } finally {
    contextSaving.value = false
  }
}

// ── Actions ────────────────────────────────────────────────────────────
async function newChat() {
  if (!project.value) return
  const projectId = await openNewChatPicker({
    workspace: project.value.workspace,
    projectId: project.value.project_id,
  })
  if (!projectId) return
  const chat = await store.newChatInProject(projectId)
  if (chat) router.push(`/chat/${chat.chat_id}`)
}

function openChat(chatId: string) {
  router.push(`/chat/${chatId}`)
}

function openArchive(chat: { archive_path?: string }) {
  if (!chat.archive_path) return
  fileViewer.open(chat.archive_path)
}

async function doComplete() {
  if (!project.value) return
  closeProjectActions(false)
  if (!await askConfirm(`Complete "${project.value.name}"? This will move the vault entry to completed/ and remove the project from the PWA.`, {
    title: 'Complete project',
    confirmLabel: 'Complete project',
  })) return
  await store.completeProject(project.value.project_id)
  emit('close')
}

async function doDelete() {
  if (!project.value) return
  closeProjectActions(false)
  await nextTick()
  projectActionsTrigger.value?.focus()
  if (!await askConfirm('Delete this project and archive all its chats?', {
    title: 'Delete project',
    confirmLabel: 'Delete project',
    destructive: true,
  })) return
  await store.deleteProject(project.value.project_id)
  emit('close')
}

function formatDate(iso: string): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
  } catch {
    return iso.slice(0, 10)
  }
}

// ── Files ──────────────────────────────────────────────────────────────
const fileViewer = useFileViewerStore()
const files = ref<ProjectFile[]>([])
const filesLoading = ref(false)
const uploading = ref(false)
const uploadErrors = ref<{ filename: string; error: string }[]>([])
const uploadInput = ref<HTMLInputElement | null>(null)
const dragOver = ref(false)

// Folder-backed projects only. Single-file personal projects (the file
// itself acts as the readme) and manual projects without a vault entry both
// return an empty list from the API; we hide the section in those cases to
// avoid a permanent "No files yet" with no way to add any.
const showFilesSection = computed(() => Boolean(project.value?.vault_folder))

const markdownFiles = computed(() => files.value.filter(f => f.kind === 'markdown'))
const imageFiles = computed(() => files.value.filter(f => f.kind === 'image'))
const otherFiles = computed(() =>
  files.value.filter(f => f.kind !== 'markdown' && f.kind !== 'image')
)

// Surface the load status so a 404/500 (e.g. server hasn't been redeployed
// after a backend change) shows in the UI instead of silently looking like
// "no files". Empty list with no error == genuinely empty folder.
const filesError = ref('')

async function loadFiles(): Promise<void> {
  if (!project.value || !project.value.vault_folder) {
    files.value = []
    filesError.value = ''
    return
  }
  filesLoading.value = true
  filesError.value = ''
  try {
    const resp = await fetch(`/api/projects/${project.value.project_id}/files`, {
      credentials: 'same-origin',
    })
    if (resp.ok) {
      files.value = await resp.json()
    } else {
      files.value = []
      filesError.value = `Couldn't load files (HTTP ${resp.status}). Try redeploying the server.`
    }
  } catch (e) {
    files.value = []
    filesError.value = e instanceof Error ? e.message : String(e)
  } finally {
    filesLoading.value = false
  }
}

function openContextDoc(): void {
  const path = project.value?.vault_doc_path
  if (path) fileViewer.open(path)
}

function openFile(f: ProjectFile): void {
  const isDoc = f.kind === 'markdown' || f.kind === 'text' || /\.(pdf|pptx)$/i.test(f.vault_path)
  if (f.kind === 'image') {
    fileViewer.openImage(f.vault_path)
  } else if (isDoc) {
    fileViewer.open(f.vault_path)
  } else {
    // Binary: hand off to the workspace-binary endpoint. PDFs render
    // inline in a new tab; everything else downloads with the original
    // filename via Content-Disposition.
    const url = `/api/workspace-binary?path=${encodeURIComponent(f.vault_path)}`
    window.open(url, '_blank')
  }
}

function triggerUpload(): void {
  uploadInput.value?.click()
}

async function onFilePicked(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  if (!input.files || !input.files.length) return
  await uploadFiles(Array.from(input.files))
  input.value = '' // reset so picking the same file again still fires change
}

async function uploadFiles(picked: File[]): Promise<void> {
  if (!project.value || !picked.length) return
  uploading.value = true
  const form = new FormData()
  picked.forEach((f, i) => form.append(`file${i}`, f, f.name))
  try {
    const resp = await fetch(`/api/projects/${project.value.project_id}/files`, {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    })
    if (!resp.ok) {
      uploadErrors.value = [
        ...uploadErrors.value,
        { filename: '(upload)', error: `HTTP ${resp.status}` },
      ]
    } else {
      const body = await resp.json()
      if (body.errors?.length) {
        uploadErrors.value = [...uploadErrors.value, ...body.errors]
      }
    }
  } catch (e) {
    uploadErrors.value = [
      ...uploadErrors.value,
      { filename: '(upload)', error: e instanceof Error ? e.message : String(e) },
    ]
  } finally {
    uploading.value = false
    await loadFiles()
  }
}

function onDragOver(): void {
  dragOver.value = true
}

function onDragLeave(): void {
  dragOver.value = false
}

async function onDrop(e: DragEvent): Promise<void> {
  dragOver.value = false
  const dt = e.dataTransfer
  if (!dt || !dt.files || !dt.files.length) return
  await uploadFiles(Array.from(dt.files))
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// File mtimes read as prose and want an exact date once they are old enough,
// so they pass suffix + absoluteAfterDays to the shared helper. There is
// deliberately only one relative-time implementation in the app.
function formatFileTime(iso: string): string {
  return formatRelative(iso, { suffix: true, absoluteAfterDays: 7 })
}

async function reloadAll() {
  archivedPage.value = 0
  await Promise.all([loadFiles(), loadAutomations()])
}
onMounted(() => {
  document.addEventListener('click', closeProjectActionsOnOutside)
  void reloadAll()
})
onBeforeUnmount(() => {
  document.removeEventListener('click', closeProjectActionsOnOutside)
})
// Re-fetch when the user navigates between projects without unmounting
// the component (Vue keeps it alive across :projectId changes).
watch(() => props.projectId, async () => {
  await reloadAll()
})
</script>

<style scoped>
.project-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  min-width: 0;
}

.project-grid {
  padding-top: var(--space-6);
  padding-bottom: var(--space-6);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  text-align: left;
}

.desktop-only { display: inline-flex; }
@media (max-width: 768px) { .desktop-only { display: none; } }

.close-btn {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: 20px;
  line-height: 1;
  font-family: var(--font);
  min-width: 30px;
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.close-btn:hover { color: var(--fg); }

.project-title {
  min-width: 0;
  margin: 0;
  overflow: hidden;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.01em;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: text;
}

.title-input {
  font-size: var(--text-lg);
  font-weight: 650;
  background: var(--bg);
  border: 1px solid var(--accent);
  border-radius: var(--radius-xs);
  color: var(--fg);
  padding: 4px 8px;
  font-family: var(--font);
  width: min(320px, 60vw);
}

.project-new-chat {
  white-space: nowrap;
}

.project-actions {
  position: relative;
}

.project-actions-menu {
  position: absolute;
  z-index: 40;
  top: calc(100% + var(--space-1));
  right: 0;
  min-width: 10rem;
  padding: var(--space-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
  box-shadow: 0 0.75rem 2rem rgb(0 0 0 / 28%);
}

.project-actions-menu button {
  width: 100%;
  min-height: var(--touch);
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.project-actions-menu button:hover {
  background: var(--bg3);
}

.project-actions-menu .danger {
  color: var(--error);
}

/* Sections, not cards: a 16px heading with its one action on the right, then
   hairline rows. Same vocabulary as Home and the other aligned pages. */
.project-section + .project-section {
  margin-top: 36px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
}

.section-head h3 {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
}

.section-count {
  margin-left: var(--space-2);
  color: var(--fg3);
  font-size: var(--text-sm);
  font-weight: 500;
  letter-spacing: 0;
}

.section-hint {
  margin: 2px 0 0;
  color: var(--fg3);
  font-size: var(--text-sm);
}

.section-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: none;
}

.text-action,
.link-btn {
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font: inherit;
  cursor: pointer;
}

.text-action {
  min-height: 32px;
  font-size: var(--text-sm);
}

.text-action:hover:not(:disabled),
.link-btn:hover {
  text-decoration: underline;
  text-underline-offset: 3px;
}

.text-action:disabled {
  cursor: default;
  opacity: 0.55;
}

.text-action:focus-visible,
.link-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}

.status { font-size: var(--text-xs); color: var(--fg2); }
.status.saved { color: var(--success); }
.status.error { color: var(--error); }

.context-display {
  margin: 0;
  max-width: 75ch;
  color: var(--fg);
  font-size: var(--text-base);
  line-height: 1.6;
  white-space: pre-wrap;
}

.context-display--empty {
  color: var(--fg2);
}

.context-textarea {
  width: 100%;
  resize: vertical;
  min-height: 120px;
  font-size: var(--text-base);
  line-height: 1.5;
  padding: 10px 12px;
}

.context-hint {
  margin: var(--space-2) 0 0;
  font-size: var(--text-sm);
  color: var(--fg3);
  overflow-wrap: anywhere;
}

.context-hint .link-btn {
  color: var(--fg2);
  text-decoration: underline;
}

.chat-list,
.automation-list {
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--border);
}

/* Home's row: title over a status sub-line, time on the right. */
.chat-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  width: 100%;
  min-height: 60px;
  padding: 9px 2px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.chat-row:hover .chat-name { color: var(--accent); }
.chat-row:focus-visible,
.automation-row:focus-visible,
.file-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}
.chat-row.archived { min-height: var(--touch); cursor: default; }
.chat-row.archived .chat-name { color: var(--fg2); font-weight: 500; }
.chat-row.archived:hover .chat-name { color: var(--fg2); }
.chat-row.archived.clickable { cursor: pointer; }
.chat-row.archived.clickable:hover .chat-name { color: var(--accent); }
/* A row with work actually happening in it is not "past" yet. */
.chat-row.archived.tidying .chat-name { color: var(--fg); }
.chat-row.remote { opacity: 0.5; cursor: default; }
.chat-row.remote:hover .chat-name { color: var(--fg); }
.chat-row:disabled { opacity: 0.55; }

.chat-row-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.chat-row-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.chat-name {
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: color 120ms var(--ease);
}

/* Unread is weight, not a digit (chatUnread() is binary). Read rows step back. */
.chat-row:not(.archived) .chat-name:not(.chat-name--unread) {
  color: var(--fg2);
  font-weight: 500;
}

.chat-name--unread {
  color: var(--fg);
  font-weight: 650;
}

.chat-row-sub {
  color: var(--fg3);
  font-size: var(--text-sm);
}

.chat-row-sub--needs {
  color: var(--accent);
}

.chat-row-time {
  flex: none;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  white-space: nowrap;
}

.chat-archive-note {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-sm);
  color: var(--fg3);
  min-width: 0;
}

/* The one case that is not ambient: a failed step is the only place a user
   would ever see this, so it is allowed to carry the warning colour. */
.chat-archive-note.failed { color: var(--warning); }

.chat-archive-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fg3);
  flex: 0 0 auto;
  animation: chat-archive-breathe 2.6s ease-in-out infinite;
}

@keyframes chat-archive-breathe {
  0%, 100% { opacity: 0.35; }
  50%      { opacity: 0.9; }
}

.pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 10px;
}
.page-info {
  font-size: var(--text-sm);
  color: var(--fg2);
}

.dot { opacity: 0.5; }

.remote-chip {
  display: inline-flex;
  align-items: center;
  height: 16px;
  padding: 0 6px;
  border-radius: var(--radius-xs);
  background: var(--bg3);
  color: var(--fg2);
  font-size: var(--text-xs);
  font-weight: 600;
}

.empty-row {
  margin: 0;
  padding: 12px 2px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  color: var(--fg3);
  font-size: var(--text-sm);
}

.automation-stale {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-top: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warning) 8%, var(--bg2));
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}
.automation-stale .btn-small { flex: none; }

.automation-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  width: 100%;
  min-height: 56px;
  padding: 9px 2px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  text-align: left;
}
.automation-row:hover .automation-title { color: var(--accent); }
.automation-row-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.automation-title {
  overflow: hidden;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.automation-title--paused { color: var(--fg3); }
.automation-row-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-1);
  color: var(--fg3);
  font-size: var(--text-sm);
}
.automation-next {
  flex: none;
  color: var(--fg2);
  font-size: var(--text-sm);
  white-space: nowrap;
}

.empty-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--fg2);
}

/* Files section ------------------------------------------------------- */
.project-files {
  border-radius: var(--radius-sm);
  transition: background 120ms var(--ease), box-shadow 120ms var(--ease);
}

.project-files.drag-over {
  background: color-mix(in srgb, var(--accent) 7%, transparent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 45%, transparent);
}

.hidden-input {
  display: none;
}

.btn-tiny {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--fg2);
  border-radius: var(--radius-sm);
  padding: 2px 8px;
  font-size: var(--text-xs);
  cursor: pointer;
  font-family: var(--font);
  align-self: flex-start;
}
.btn-tiny:hover { color: var(--fg); border-color: var(--fg2); background: var(--bg3); }

.upload-errors {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: var(--space-2);
  background: color-mix(in srgb, var(--error) 8%, transparent);
  border: 1px solid var(--error);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}
.upload-error {
  font-size: var(--text-sm);
  color: var(--error);
}

.file-group {
  display: flex;
  flex-direction: column;
  margin-top: var(--space-2);
}

.file-group-label {
  padding: 6px 2px 4px;
  border-bottom: 1px solid var(--border);
  color: var(--fg3);
  font-size: var(--text-xs);
}

.file-row {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: var(--touch);
  gap: 10px;
  padding: 6px 2px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
  min-width: 0;
}
.file-row:hover .file-name { color: var(--accent); }

.file-icon {
  width: 24px;
  text-align: center;
  flex-shrink: 0;
  color: var(--fg2);
}

.file-thumb {
  width: 32px;
  height: 32px;
  object-fit: cover;
  border-radius: var(--radius-xs);
  background: var(--bg3);
  flex-shrink: 0;
}

.file-name {
  flex: 1;
  font-size: var(--text-base);
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.file-meta {
  font-size: var(--text-xs);
  color: var(--fg3);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

/* Rail ----------------------------------------------------------------- */
.project-rail .rail-title {
  margin-top: 0;
}

.rail-path {
  margin: 0;
  overflow-wrap: anywhere;
}

.rail-path code {
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}

.project-rail .link-btn {
  overflow-wrap: anywhere;
  text-align: left;
}

@media (pointer: coarse) {
  .text-action { min-height: var(--touch); }
}

@media (max-width: 768px) {
  .file-meta { display: none; }
  .automation-row { flex-wrap: wrap; row-gap: 2px; }
  .automation-next { width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .chat-archive-dot { animation: none; opacity: 0.75; }
  .chat-name,
  .project-files { transition: none; }
}
</style>
