<template>
  <div v-if="project" class="project-view">
    <PaneHeader page-tag="project" @open-sidebar="emit('open-sidebar')">
      <template #title>
        <div class="header-left">
          <button class="close-btn desktop-only" @click="$emit('close')" title="Close">&times;</button>
          <input
            v-if="editingName"
            class="title-input"
            v-model="nameDraft"
            @keyup.enter="saveName"
            @keyup.escape="editingName = false"
            @blur="saveName"
            autofocus
          />
          <h2 v-else class="project-title" @dblclick="startEditName">{{ project.name }}</h2>
          <span class="workspace-badge">{{ project.workspace }}</span>
        </div>
      </template>
      <template #actions>
        <button
          v-if="project.vault_folder && !project.is_auto"
          class="btn-small"
          @click="doComplete"
        >Complete</button>
        <button
          v-if="!project.vault_folder && !project.is_auto"
          class="btn-small danger"
          @click="doDelete"
        >Delete</button>
      </template>
    </PaneHeader>

    <!-- Counts only, ordered by urgency. "Created" used to sit here as a date
         styled like a metric, while the count that matters - how many chats want
         you - was missing entirely. Dates moved to the caption below. -->
    <div class="project-stats">
      <div class="stat" :class="{ 'stat--hot': needsInputCount > 0 }">
        <div class="stat-value">{{ needsInputCount }}</div>
        <div class="stat-label">Need you</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ workingCount }}</div>
        <div class="stat-label">Working</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ totalUnread }}</div>
        <div class="stat-label">Unread</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ activeChats.length }}</div>
        <div class="stat-label">Active</div>
      </div>
    </div>
    <p class="project-caption">
      created {{ formatDate(project.created_at) }}
      <template v-if="archivedChats.length"> · {{ archivedChats.length }} archived</template>
    </p>

    <!-- A tablist, not a nav landmark: these switch panels in place rather than
         navigating. role="tab" is only meaningful inside role="tablist", and
         each tab owns the panel it names via aria-controls/aria-labelledby. -->
    <div class="project-tabs" role="tablist" aria-label="Project sections">
      <button
        v-for="tab in projectTabs"
        :key="tab.key"
        :id="tabId(tab.key)"
        type="button"
        class="project-tab"
        :class="{ active: activeTab === tab.key }"
        role="tab"
        :aria-selected="activeTab === tab.key"
        :aria-controls="panelId(tab.key)"
        :tabindex="activeTab === tab.key ? 0 : -1"
        :data-tab="tab.key"
        @click="activeTab = tab.key"
        @keydown="onTabKeydown"
      >
        {{ tab.label }}
        <span v-if="tab.count !== undefined" class="tab-count">{{ tab.count }}</span>
      </button>
    </div>

    <!-- The overview panel needs a real element to carry role="tabpanel", and
         .project-view lays its children out with a flex gap — so this wrapper
         re-declares the column gap or the rhythm between the cards collapses. -->
    <div
      v-if="activeTab === 'overview'"
      :id="panelId('overview')"
      class="tab-panel"
      role="tabpanel"
      :aria-labelledby="tabId('overview')"
      tabindex="0"
    >
    <section class="card">
      <div class="card-header">
        <h3>project context</h3>
        <div class="card-actions">
          <span v-if="contextStatus" class="status" :class="contextStatus">{{ contextStatusLabel }}</span>
          <button
            class="btn-small"
            :disabled="!contextDirty || contextSaving"
            @click="saveContext"
          >{{ contextSaving ? 'Saving...' : 'Save' }}</button>
        </div>
      </div>
      <textarea
        v-model="contextDraft"
        class="context-textarea"
        placeholder="Describe what this project is about."
        rows="8"
      ></textarea>
    </section>

    <section class="card">
      <div class="card-header">
        <h3>active chats ({{ activeChats.length }})</h3>
        <button class="btn-small" @click="newChat">+ New chat</button>
      </div>
      <div v-if="activeChats.length" class="chat-list">
        <div
          v-for="chat in activeChats"
          :key="chat.chat_id"
          class="chat-row"
          :class="{ remote: chat.local === false }"
          @click="chat.local !== false && openChat(chat.chat_id)"
          :title="chat.local === false ? 'This chat lives on another instance' : ''"
        >
          <div class="chat-row-main">
            <!-- Chat-level unread is title weight, not a digit: chatUnread() is
                 binary, so a badge could only ever read "1". -->
            <span
              class="chat-name"
              :class="{ 'chat-name--unread': store.chatUnread(chat.chat_id) > 0 }"
            >{{ chat.title }}</span>
            <ChatSignals :chat-id="chat.chat_id" density="row" :hue="workspaceHue" />
            <span v-if="chat.local === false" class="remote-chip">remote</span>
          </div>
          <div class="chat-row-meta">
            <span>{{ chat.model }}</span>
            <span class="dot">·</span>
            <span>{{ formatRelative(chatActivity(chat), { suffix: true, absoluteAfterDays: 7 }) }}</span>
          </div>
        </div>
      </div>
      <div v-else class="empty-row">// no active chats in this project</div>
    </section>

    <section v-if="showFilesSection" class="card" @dragover.prevent="onDragOver" @dragleave="onDragLeave" @drop.prevent="onDrop" :class="{ 'drag-over': dragOver }">
      <div class="card-header">
        <h3>files ({{ files.length }})</h3>
        <div class="card-actions">
          <span v-if="filesLoading" class="status">Loading…</span>
          <span v-else-if="uploading" class="status">Uploading…</span>
          <button class="btn-small" @click="triggerUpload" :disabled="uploading">+ Upload</button>
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
        <button class="btn-tiny" @click="uploadErrors = []">dismiss</button>
      </div>

      <div v-if="!files.length && !filesLoading && !filesError" class="empty-row">
        // no files yet. drag one in or hit Upload.
      </div>

      <div v-if="markdownFiles.length" class="file-group">
        <div class="file-group-label">Markdown</div>
        <div
          v-for="f in markdownFiles"
          :key="f.path"
          class="file-row"
          @click="openFile(f)"
        >
          <AppIcon class="file-icon" name="doc" :size="18" />
          <span class="file-name">{{ f.path }}</span>
          <span class="file-meta">{{ formatSize(f.size) }} · {{ formatFileTime(f.mtime) }}</span>
        </div>
      </div>

      <div v-if="imageFiles.length" class="file-group">
        <div class="file-group-label">Images</div>
        <div
          v-for="f in imageFiles"
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
        </div>
      </div>

      <div v-if="otherFiles.length" class="file-group">
        <div class="file-group-label">Other</div>
        <div
          v-for="f in otherFiles"
          :key="f.path"
          class="file-row"
          @click="openFile(f)"
        >
          <AppIcon class="file-icon" name="file" :size="18" />
          <span class="file-name">{{ f.path }}</span>
          <span class="file-meta">{{ formatSize(f.size) }} · {{ formatFileTime(f.mtime) }}</span>
        </div>
      </div>
    </section>

    <section class="card" v-if="archivedChats.length">
      <div class="card-header">
        <h3>archived ({{ archivedChats.length }})</h3>
      </div>
      <div class="chat-list">
        <div
          v-for="chat in pagedArchivedChats"
          :key="chat.chat_id"
          class="chat-row archived"
          :class="{ clickable: chat.archive_path }"
          @click="chat.archive_path && openArchive(chat)"
        >
          <div class="chat-row-main">
            <span class="chat-name">{{ chat.title }}</span>
          </div>
          <div class="chat-row-meta">
            <span>{{ chat.model }}</span>
            <span class="dot">·</span>
            <span>{{ formatDate(chat.created_at) }}</span>
          </div>
        </div>
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

    <section
      v-else-if="activeTab === 'loops'"
      :id="panelId('loops')"
      class="card automation-card"
      role="tabpanel"
      :aria-labelledby="tabId('loops')"
      tabindex="0"
    >
      <div class="card-header">
        <div>
          <h3>loops <span v-if="loopCount !== undefined">({{ loopCount }})</span></h3>
          <p class="card-hint">Prompts that repeat inside a chat in this project.</p>
        </div>
      </div>
      <div v-if="projectLoops.length" class="automation-list">
        <button
          v-for="loop in projectLoops"
          :key="loop.loop_id"
          type="button"
          class="automation-row"
          @click="openAutomation(loop.loop_id)"
        >
          <span class="automation-row-main">
            <span class="automation-title">{{ loop.title || promptTitle(loop.prompt) }}</span>
            <span class="automation-status" :class="{ running: loop.running }">
              {{ loop.running ? 'running' : 'stopped' }}
            </span>
          </span>
          <span class="automation-row-meta">
            <span>every {{ loop.interval_minutes }} min</span>
            <span class="dot">·</span>
            <span>{{ loopTargetLabel(loop) }}</span>
            <span class="dot">·</span>
            <span>last {{ automationTimestamp(loop.last_run_at) }}</span>
          </span>
        </button>
      </div>
      <!-- Absence is only asserted once the fetch has actually resolved
           (Rule S6): a failed or in-flight load must not read as "none". -->
      <div v-else-if="loopsState === 'loading'" class="empty-row">// loading loops…</div>
      <div v-else-if="loopsState === 'error'" class="empty-row">// could not load loops</div>
      <div v-else class="empty-row">// no loops send prompts into this project</div>
    </section>

    <section
      v-else-if="activeTab === 'schedules'"
      :id="panelId('schedules')"
      class="card automation-card"
      role="tabpanel"
      :aria-labelledby="tabId('schedules')"
      tabindex="0"
    >
      <div class="card-header">
        <div>
          <h3>schedules <span v-if="scheduleCount !== undefined">({{ scheduleCount }})</span></h3>
          <p class="card-hint">Scheduled prompts delivered to this project or one of its chats.</p>
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
            <span class="automation-title">{{ schedule.title || promptTitle(schedule.prompt) }}</span>
            <span class="automation-status" :class="{ running: schedule.enabled }">
              {{ schedule.enabled ? 'enabled' : 'paused' }}
            </span>
          </span>
          <span class="automation-row-meta">
            <span>{{ scheduleFrequencyLabel(schedule) }}</span>
            <span class="dot">·</span>
            <span>{{ scheduleTargetLabel(schedule) }}</span>
            <span class="dot">·</span>
            <span>next {{ automationTimestamp(schedule.next_run) }}</span>
          </span>
        </button>
      </div>
      <div v-else-if="schedulesState === 'loading'" class="empty-row">// loading schedules…</div>
      <div v-else-if="schedulesState === 'error'" class="empty-row">// could not load schedules</div>
      <div v-else class="empty-row">// no schedules deliver prompts to this project</div>
    </section>
  </div>
  <div v-else class="empty-state">// project not found.</div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, useId } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import { useFileViewerStore } from '../stores/fileViewer'
import { askConfirm } from '../lib/confirm'
import { formatRelative } from '../lib/relativeTime'
import { chatActivityTimestamp } from '../lib/homeLanes'
import { colorForWorkspace } from '../lib/workspaceColors'
import PaneHeader from './PaneHeader.vue'
import ChatSignals from './ChatSignals.vue'
import AppIcon from './AppIcon.vue'
import type { ChatInfo, Loop, Schedule } from '../lib/types'

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

type ProjectTab = 'overview' | 'loops' | 'schedules'
const activeTab = ref<ProjectTab>('overview')

// Unique per mounted instance so the tab/panel id pairs stay valid even if two
// project views are ever alive at once.
const uid = useId()
const tabId = (key: ProjectTab) => `${uid}-tab-${key}`
const panelId = (key: ProjectTab) => `${uid}-panel-${key}`

const projectTabs = computed(() => [
  { key: 'overview' as const, label: 'Overview' },
  { key: 'loops' as const, label: 'Loops', count: loopCount.value },
  { key: 'schedules' as const, label: 'Schedules', count: scheduleCount.value },
])

const TAB_KEYS = ['ArrowLeft', 'ArrowRight', 'Home', 'End']

// Roving tabindex: the bar is a single Tab stop and Left/Right/Home/End move
// between tabs, per the ARIA tabs pattern. Selection follows focus because the
// panels are cheap to render. Mirrors the arrow roaming in HomeRecentChats.
function onTabKeydown(event: KeyboardEvent): void {
  if (!TAB_KEYS.includes(event.key)) return
  const current = event.currentTarget as HTMLElement | null
  const bar = current?.parentElement
  if (!current || !bar) return
  const tabs = Array.from(bar.querySelectorAll<HTMLElement>('[role="tab"]'))
  const index = tabs.indexOf(current)
  if (index < 0) return
  event.preventDefault()
  let next = index
  if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
  else if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
  else if (event.key === 'Home') next = 0
  else next = tabs.length - 1
  const target = tabs[next]
  const key = target?.dataset.tab as ProjectTab | undefined
  if (!key) return
  activeTab.value = key
  target.focus()
}

watch(project, (p) => {
  if (p && p.workspace && p.workspace !== store.activeWorkspace) {
    store.activeWorkspace = p.workspace
  }
}, { immediate: true })

const allChats = computed(() => store.chats.filter(c => c.project_id === props.projectId))
const projectChatIds = computed(() => new Set(allChats.value.map(chat => chat.chat_id)))
const projectLoops = computed(() =>
  taskStore.loops.filter(loop => projectChatIds.value.has(loop.web_chat_id)),
)
const projectSchedules = computed(() =>
  taskStore.schedules.filter(schedule =>
    schedule.web_project_id === props.projectId
      || (schedule.web_chat_id !== null && projectChatIds.value.has(schedule.web_chat_id)),
  ),
)

// The task store is filled by ChatLayout's fire-and-forget fetch, so an empty
// list here could mean "none", "still loading" or "the request failed" — three
// different answers that all rendered as "no loops". Track the load so the
// panel can say which, and so a tab omits a count it cannot vouch for.
const automationLoad = ref<'loading' | 'ready' | 'error'>('loading')

function automationState(count: number): 'list' | 'loading' | 'error' | 'none' {
  if (count > 0) return 'list'
  if (automationLoad.value === 'loading') return 'loading'
  if (automationLoad.value === 'error') return 'error'
  return 'none'
}
const loopsState = computed(() => automationState(projectLoops.value.length))
const schedulesState = computed(() => automationState(projectSchedules.value.length))

// undefined means "unknown", which renders as no count at all rather than a 0
// that would be indistinguishable from a real zero.
const loopCount = computed(() =>
  loopsState.value === 'loading' || loopsState.value === 'error'
    ? undefined
    : projectLoops.value.length,
)
const scheduleCount = computed(() =>
  schedulesState.value === 'loading' || schedulesState.value === 'error'
    ? undefined
    : projectSchedules.value.length,
)

async function loadAutomations(): Promise<void> {
  try {
    await Promise.all([taskStore.fetchLoops(), taskStore.fetchSchedules()])
    automationLoad.value = 'ready'
  } catch {
    automationLoad.value = 'error'
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
  activeChats.value.filter(c =>
    store.isChatStreaming(c.chat_id) || store.chatHasBackgroundAgents(c.chat_id),
  ).length,
)

// Hue follows the project's workspace so the marks here read the same as the
// sidebar and the home lanes rather than inheriting the active accent.
const workspaceHue = computed(() =>
  colorForWorkspace(store.workspaceOptions.find(w => w.name === project.value?.workspace)),
)

function chatActivity(chat: ChatInfo): string {
  return chatActivityTimestamp(chat)
}

function promptTitle(prompt: string): string {
  const first = prompt.split('\n')[0].trim()
  return first.length > 60 ? first.slice(0, 57) + '...' : first
}

function loopTargetLabel(loop: Loop): string {
  return loop.context_label || allChats.value.find(chat => chat.chat_id === loop.web_chat_id)?.title || 'Unavailable chat'
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
const contextDraft = ref('')
const contextSaving = ref(false)
const contextStatus = ref<'' | 'saved' | 'error'>('')

watch(
  () => project.value?.context,
  (ctx) => { contextDraft.value = ctx || '' },
  { immediate: true }
)

const contextDirty = computed(() => (project.value?.context || '') !== contextDraft.value)
const contextStatusLabel = computed(() => {
  if (contextStatus.value === 'saved') return 'Saved'
  if (contextStatus.value === 'error') return 'Error'
  return ''
})

async function saveContext() {
  if (!project.value || !contextDirty.value) return
  contextSaving.value = true
  contextStatus.value = ''
  try {
    await store.updateProject(project.value.project_id, { context: contextDraft.value })
    contextStatus.value = 'saved'
    setTimeout(() => { if (contextStatus.value === 'saved') contextStatus.value = '' }, 2000)
  } catch {
    contextStatus.value = 'error'
  } finally {
    contextSaving.value = false
  }
}

// ── Actions ────────────────────────────────────────────────────────────
async function newChat() {
  if (!project.value) return
  const c = await store.createChat(project.value.project_id)
  router.push(`/chat/${c.chat_id}`)
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
  if (!await askConfirm(`Complete "${project.value.name}"? This will move the vault entry to completed/ and remove the project from the PWA.`, {
    title: 'Complete project',
    confirmLabel: 'Complete project',
  })) return
  await store.completeProject(project.value.project_id)
  emit('close')
}

async function doDelete() {
  if (!project.value) return
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
onMounted(reloadAll)
// Re-fetch when the user navigates between projects without unmounting
// the component (Vue keeps it alive across :projectId changes).
watch(() => props.projectId, async () => {
  activeTab.value = 'overview'
  await reloadAll()
})
</script>

<style scoped>
.project-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  gap: 16px;
  padding: 0 16px;
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
  font-size: 16px;
  font-weight: 700;
  margin: 0;
  cursor: text;
}

.title-input {
  font-size: 16px;
  font-weight: 700;
  background: var(--bg);
  border: 1px solid var(--accent);
  border-radius: 4px;
  color: var(--fg);
  padding: 4px 8px;
  font-family: var(--font);
  width: 320px;
}

.workspace-badge {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--fg2);
  background: var(--bg3);
  padding: 2px 8px;
  border-radius: 4px;
}

.btn-small.danger {
  border-color: var(--error);
  color: var(--error);
}
.btn-small.danger:hover { background: var(--error); color: white; }

.project-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}

.stat {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

/* Only the urgent tile carries the accent, so a glance answers "does anything
   here want me?" before any number is read. */
.stat--hot {
  border-left: 2px solid var(--accent);
}

.stat--hot .stat-value {
  color: var(--accent);
}

.stat-value {
  font-size: var(--text-lg);
  font-weight: 700;
  color: var(--fg);
  font-variant-numeric: tabular-nums;
}

.stat-label {
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--fg2);
}

.project-caption {
  margin: 0 0 var(--space-4);
  padding: 0 var(--space-4);
  font-size: var(--text-xs);
  color: var(--fg3);
}

.project-tabs {
  display: flex;
  gap: var(--space-1);
  border-bottom: 1px solid var(--border);
  margin-bottom: 0;
  overflow-x: auto;
}

/* The overview panel's own children are the cards, and .project-view spaces its
   children with a flex gap — so the panel repeats that column layout. */
.tab-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.project-tab {
  min-height: var(--touch);
  padding: var(--space-2) var(--space-3);
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  font: 600 var(--text-sm) var(--font);
  white-space: nowrap;
}
.project-tab:hover { color: var(--fg); background: var(--bg3); }
.project-tab.active {
  border-bottom-color: var(--accent);
  color: var(--fg);
}
.tab-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  /* Sized from the type token rather than a fixed 18px box, so the pill grows
     with the Appearance font scale instead of clipping the digit. */
  min-width: calc(var(--text-xs) + var(--space-2));
  min-height: calc(var(--text-xs) + var(--space-1));
  margin-left: var(--space-1);
  padding: 0 var(--space-1);
  border-radius: var(--radius-pill);
  background: var(--bg3);
  color: var(--fg2);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
/* Active is carried by the underline and the text colour. The pill stays
   unfilled on purpose: a solid fill is reserved for "needs the user"
   (Rule S1), and --fg on --accent2 is ~1.9:1 in the light theme. */
.project-tab.active .tab-count { color: var(--fg); }

.card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.card-header h3 {
  font-size: 13px;
  font-weight: 700;
  margin: 0;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--fg2);
}

.card-hint {
  margin: var(--space-1) 0 0;
  color: var(--fg3);
  font-size: var(--text-xs);
}

.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status { font-size: 11px; color: var(--fg2); }
.status.saved { color: var(--success); }
.status.error { color: var(--error); }

.context-textarea {
  width: 100%;
  resize: vertical;
  min-height: 120px;
  font-size: 13px;
  line-height: 1.5;
  padding: 10px 12px;
}

.chat-list { display: flex; flex-direction: column; }

.chat-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 4px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  gap: 12px;
}
.chat-row:last-child { border-bottom: none; }
.chat-row:hover { background: var(--bg3); }
.chat-row.archived { opacity: 0.6; cursor: default; }
.chat-row.archived:hover { background: transparent; }
.chat-row.archived.clickable { cursor: pointer; }
.chat-row.archived.clickable:hover { background: var(--bg3); }
.chat-row.remote { opacity: 0.5; cursor: default; }
.chat-row.remote:hover { background: transparent; }

.pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 10px;
}
.page-info {
  font-size: 12px;
  color: var(--fg2);
}

.chat-row-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.chat-name {
  font-size: var(--text-base);
  color: var(--fg2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Chat-level unread reads as title weight; the digit belongs to the project
   and workspace rollups, which are real counts. */
.chat-name--unread {
  color: var(--fg);
  font-weight: 600;
}

.chat-row-meta {
  font-size: 11px;
  color: var(--fg2);
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.dot { opacity: 0.5; }

.remote-chip {
  display: inline-flex;
  align-items: center;
  height: 16px;
  padding: 0 6px;
  border-radius: 4px;
  background: var(--bg3);
  color: var(--fg2);
  font-size: 9px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

.empty-row {
  font-size: 12px;
  color: var(--fg2);
  font-style: italic;
  padding: 4px 0;
}

/* Raw px on purpose: keeps a mostly-empty automations card from collapsing to a
   two-line sliver. No token expresses "minimum card body height". */
.automation-card { min-height: 180px; }
.automation-list { display: flex; flex-direction: column; }
.automation-row {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  width: 100%;
  min-height: var(--touch);
  padding: var(--space-2) var(--space-1);
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  text-align: left;
}
.automation-row:last-child { border-bottom: none; }
.automation-row:hover { background: var(--bg3); }
/* Tab panels carry tabindex="0" per the ARIA tabs pattern, so they need a
   visible ring too — a focusable region with no ring is a lost keyboard user. */
.automation-row:focus-visible,
.tab-panel:focus-visible,
.automation-card:focus-visible,
.project-tab:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.automation-row-main,
.automation-row-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.automation-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-base);
}
.automation-status {
  flex-shrink: 0;
  color: var(--fg3);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.35px;
}
.automation-status.running { color: var(--success); }
.automation-row-meta {
  flex-wrap: wrap;
  color: var(--fg2);
  font-size: var(--text-xs);
}

.empty-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--fg2);
}

/* Files section ------------------------------------------------------- */
.card.drag-over {
  border-color: var(--accent);
  background: var(--bg3);
}

.hidden-input {
  display: none;
}

.btn-tiny {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--fg2);
  border-radius: 4px;
  padding: 2px 6px;
  font-size: 10px;
  cursor: pointer;
  font-family: var(--font);
}
.btn-tiny:hover { color: var(--fg); border-color: var(--fg2); }

.upload-errors {
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: rgba(248, 113, 113, 0.08);
  border: 1px solid var(--error);
  border-radius: 6px;
  padding: 8px 10px;
}
.upload-error {
  font-size: 12px;
  color: var(--error);
}

.file-group {
  display: flex;
  flex-direction: column;
  margin-top: 6px;
}

.file-group-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--fg2);
  padding: 6px 4px 2px;
  border-bottom: 1px solid var(--border);
}

.file-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 4px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  min-width: 0;
}
.file-row:last-child { border-bottom: none; }
.file-row:hover { background: var(--bg3); }

.file-icon {
  width: 24px;
  text-align: center;
  flex-shrink: 0;
  font-size: 14px;
  color: var(--fg2);
}

.file-thumb {
  width: 32px;
  height: 32px;
  object-fit: cover;
  border-radius: 4px;
  background: var(--bg3);
  flex-shrink: 0;
}

.file-name {
  flex: 1;
  font-size: 13px;
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.file-meta {
  font-size: 11px;
  color: var(--fg2);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

@media (max-width: 768px) {
  .project-stats { grid-template-columns: repeat(2, 1fr); }
  .file-meta { display: none; }
  .automation-row-meta { display: block; line-height: 1.5; }
  .automation-row-meta .dot { margin: 0 var(--space-1); }
}
</style>
