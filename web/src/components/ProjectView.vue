<template>
  <div v-if="project" class="project-view">
    <PaneHeader @open-sidebar="emit('open-sidebar')">
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

    <div class="project-stats">
      <div class="stat">
        <div class="stat-value">{{ activeChats.length }}</div>
        <div class="stat-label">Active chats</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ archivedChats.length }}</div>
        <div class="stat-label">Archived</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ totalUnread }}</div>
        <div class="stat-label">Unread</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ formatDate(project.created_at) }}</div>
        <div class="stat-label">Created</div>
      </div>
    </div>

    <section class="card">
      <div class="card-header">
        <h3>Project context</h3>
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
        <h3>Active chats ({{ activeChats.length }})</h3>
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
            <span class="chat-name">{{ chat.title }}</span>
            <span v-if="store.isChatStreaming(chat.chat_id)" class="spinner-dot" title="Working" />
            <span v-if="chat.local === false" class="remote-chip">remote</span>
            <span v-if="store.chatUnread(chat.chat_id) > 0" class="badge">{{ store.chatUnread(chat.chat_id) }}</span>
          </div>
          <div class="chat-row-meta">
            <span>{{ chat.model }}</span>
            <span class="dot">·</span>
            <span>{{ formatDate(chat.created_at) }}</span>
          </div>
        </div>
      </div>
      <div v-else class="empty-row">// no active chats in this project</div>
    </section>

    <section v-if="showFilesSection" class="card" @dragover.prevent="onDragOver" @dragleave="onDragLeave" @drop.prevent="onDrop" :class="{ 'drag-over': dragOver }">
      <div class="card-header">
        <h3>Files ({{ files.length }})</h3>
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
          <span class="file-icon">📄</span>
          <span class="file-name">{{ f.path }}</span>
          <span class="file-meta">{{ formatSize(f.size) }} · {{ formatRelative(f.mtime) }}</span>
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
          <span class="file-meta">{{ formatSize(f.size) }} · {{ formatRelative(f.mtime) }}</span>
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
          <span class="file-icon">📎</span>
          <span class="file-name">{{ f.path }}</span>
          <span class="file-meta">{{ formatSize(f.size) }} · {{ formatRelative(f.mtime) }}</span>
        </div>
      </div>
    </section>

    <section class="card" v-if="archivedChats.length">
      <div class="card-header">
        <h3>Archived ({{ archivedChats.length }})</h3>
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
  <div v-else class="empty-state">// project not found.</div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import PaneHeader from './PaneHeader.vue'

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
const router = useRouter()

const project = computed(() => store.projects.find(p => p.project_id === props.projectId) || null)

const allChats = computed(() => store.chats.filter(c => c.project_id === props.projectId))
const activeChats = computed(() =>
  allChats.value
    // Hide remote chats (session lives on another device, not openable here).
    .filter(c => !c.archived && c.local !== false)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
)
const archivedChats = computed(() =>
  allChats.value
    .filter(c => c.archived)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
)
const totalUnread = computed(() => store.projectUnread(props.projectId))

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
  if (!confirm(`Complete "${project.value.name}"? This will move the vault entry to completed/ and remove the project from the PWA.`)) return
  await store.completeProject(project.value.project_id)
  emit('close')
}

async function doDelete() {
  if (!project.value) return
  if (!confirm('Delete this project and archive all its chats?')) return
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

function formatRelative(iso: string): string {
  if (!iso) return ''
  try {
    const t = new Date(iso).getTime()
    const diffSec = (Date.now() - t) / 1000
    if (diffSec < 60) return 'just now'
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`
    if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}d ago`
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  } catch {
    return iso.slice(0, 10)
  }
}

async function reloadAll() {
  archivedPage.value = 0
  await loadFiles()
}
onMounted(reloadAll)
// Re-fetch when the user navigates between projects without unmounting
// the component (Vue keeps it alive across :projectId changes).
watch(() => props.projectId, reloadAll)
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

.stat-value {
  font-size: 18px;
  font-weight: 700;
  color: var(--fg);
}

.stat-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--fg2);
}

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
  font-size: 13px;
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

.spinner-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border: 2px solid var(--accent2);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: ciao-spin 0.8s linear infinite;
  vertical-align: middle;
  flex-shrink: 0;
}

@keyframes ciao-spin {
  to { transform: rotate(360deg); }
}

.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 6px;
  border-radius: 9px;
  background: var(--accent);
  color: var(--bg);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
}

.empty-row {
  font-size: 12px;
  color: var(--fg2);
  font-style: italic;
  padding: 4px 0;
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
}
</style>
