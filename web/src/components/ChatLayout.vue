<template>
  <div class="chat-layout" :class="{ 'sidebar-open': !sidebarCollapsed }">
    <ProjectSidebar
      :collapsed="sidebarCollapsed"
      :mode="viewMode"
      :style="sidebarStyle"
      @toggle="sidebarCollapsed = !sidebarCollapsed"
      @chat-selected="onChatSelected"
      @new-schedule="showNewSchedule = true"
    />
    <div
      v-if="!sidebarCollapsed && !isMobile"
      class="sidebar-resizer"
      :class="{ 'is-dragging': isDraggingSidebar }"
      @mousedown="startSidebarDrag"
    />
    <div
      v-if="isMobile && !sidebarCollapsed"
      class="sidebar-backdrop"
      aria-hidden="true"
      @click="sidebarCollapsed = true"
    />
    <div class="chat-main" :class="{ 'chat-split': !!pinnedFilePath }">
      <!-- Split view when a file is pinned -->
      <template v-if="pinnedFilePath">
        <div
          class="chat-split-main"
          :style="{
            width: isMobile ? '100%' : (chatSplitRatio * 100) + '%',
            flex: isMobile ? undefined : '0 0 auto',
            transition: isDraggingSplit ? 'none' : undefined
          }"
        >
          <ProjectView
            v-if="projectIdParam"
            :project-id="projectIdParam"
            @close="closeProject"
            @open-sidebar="sidebarCollapsed = false"
          />
          <ChatPanel v-else-if="store.activeChat" @close="closeChat" @open-sidebar="sidebarCollapsed = false" />
          <div v-else class="empty-shell">
            <PaneHeader title="Ciao" @open-sidebar="sidebarCollapsed = false" />
            <div class="empty-state">
              <div class="empty-mark">
                <img
                  class="empty-face"
                  :src="faceSrc"
                  alt="ciao"
                  draggable="false"
                  @click="faceToggled = !faceToggled"
                  @mouseenter="faceHover = true"
                  @mouseleave="faceHover = false"
                />
              </div>
              <p class="empty-hint">// select a chat from the sidebar, or start a new one.</p>
              <div class="empty-actions">
                <button
                  v-for="action in generalWorkspaceActions"
                  :key="action.workspace"
                  class="btn-primary"
                  :disabled="action.isCreating"
                  @click="createWorkspaceChat(action)"
                >
                  {{ action.isCreating ? 'Creating...' : `+ ${action.label} Chat` }}
                </button>
              </div>
            </div>
          </div>
        </div>
        <div
          v-if="!isMobile"
          class="chat-split-resizer"
          :class="{ 'is-dragging': isDraggingSplit }"
          @mousedown="startSplitDrag"
        />
        <div
          class="chat-split-side"
          :style="{
            width: isMobile ? '100%' : ((1 - chatSplitRatio) * 100) + '%',
            flex: isMobile ? undefined : '0 0 auto',
            transition: isDraggingSplit ? 'none' : undefined
          }"
        >
          <PinnedFilePanel :file-path="pinnedFilePath" @close="unpinCurrent" />
        </div>
      </template>
      <template v-else>
        <SettingsView v-if="viewMode === 'settings'" @open-sidebar="sidebarCollapsed = false" />
        <SchedulePanel
          v-else-if="viewMode === 'schedules'"
          :show-new="showNewSchedule"
          @created="showNewSchedule = false"
          @close="showNewSchedule = false"
          @open-sidebar="sidebarCollapsed = false"
        />
        <ProjectView
          v-else-if="projectIdParam"
          :project-id="projectIdParam"
          @close="closeProject"
          @open-sidebar="sidebarCollapsed = false"
        />
        <ChatPanel v-else-if="store.activeChat" @close="closeChat" @open-sidebar="sidebarCollapsed = false" />
        <div v-else class="empty-shell">
          <PaneHeader title="Ciao" @open-sidebar="sidebarCollapsed = false" />
          <div class="empty-state">
            <div class="empty-mark">
              <img
                class="empty-face"
                :src="faceSrc"
                alt="ciao"
                draggable="false"
                @click="faceToggled = !faceToggled"
                @mouseenter="faceHover = true"
                @mouseleave="faceHover = false"
              />
            </div>
            <p class="empty-hint">// select a chat from the sidebar, or start a new one.</p>
            <div class="empty-actions">
              <button
                v-for="action in generalWorkspaceActions"
                :key="action.workspace"
                class="btn-primary"
                :disabled="action.isCreating"
                @click="createWorkspaceChat(action)"
              >
                {{ action.isCreating ? 'Creating...' : `+ ${action.label} Chat` }}
              </button>
            </div>
          </div>
        </div>
      </template>
    </div>
    <FileViewerModal />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import ProjectSidebar from './ProjectSidebar.vue'
import ChatPanel from './ChatPanel.vue'
import ProjectView from './ProjectView.vue'
import SchedulePanel from './SchedulePanel.vue'
import SettingsView from './SettingsView.vue'
import FileViewerModal from './FileViewerModal.vue'
import PinnedFilePanel from './PinnedFilePanel.vue'
import PaneHeader from './PaneHeader.vue'

const store = useProjectStore()

const DEFAULT_SIDEBAR_WIDTH = 260
const MIN_SIDEBAR_WIDTH = 180
const MAX_SIDEBAR_WIDTH = 500
const SIDEBAR_SNAP_THRESHOLD = 15 // px

const DEFAULT_SPLIT_RATIO = 0.5
const MIN_PANE_WIDTH = 360
const SPLIT_SNAP_THRESHOLD = 15 // px

function safeGetItem(key: string): string | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null
  } catch {
    return null
  }
}

function safeSetItem(key: string, value: string) {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(key, value)
    }
  } catch {}
}

const sidebarWidth = ref(Number(safeGetItem('ciao:sidebar-width')) || DEFAULT_SIDEBAR_WIDTH)
const chatSplitRatio = ref(Number(safeGetItem('ciao:chat-split-ratio')) || DEFAULT_SPLIT_RATIO)

const isDraggingSidebar = ref(false)
const isDraggingSplit = ref(false)

const sidebarStyle = computed(() => {
  if (isMobile.value) return {}
  return {
    width: sidebarCollapsed.value ? '40px' : `${sidebarWidth.value}px`,
    minWidth: sidebarCollapsed.value ? '40px' : `${sidebarWidth.value}px`,
    transition: isDraggingSidebar.value ? 'none' : undefined
  }
})

let dragStartWidth = 0
let dragStartX = 0
let dragStartRatio = 0
let dragContainerWidth = 0
let dragContainerLeft = 0

function startSidebarDrag(e: MouseEvent) {
  e.preventDefault()
  isDraggingSidebar.value = true
  dragStartWidth = sidebarWidth.value
  dragStartX = e.clientX
  
  window.addEventListener('mousemove', handleSidebarDrag)
  window.addEventListener('mouseup', stopSidebarDrag)
  document.body.classList.add('is-dragging-layout')
}

function handleSidebarDrag(e: MouseEvent) {
  if (!isDraggingSidebar.value) return
  const deltaX = e.clientX - dragStartX
  let newWidth = dragStartWidth + deltaX
  
  if (Math.abs(newWidth - DEFAULT_SIDEBAR_WIDTH) < SIDEBAR_SNAP_THRESHOLD) {
    newWidth = DEFAULT_SIDEBAR_WIDTH
  }
  
  newWidth = Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, newWidth))
  sidebarWidth.value = newWidth
}

function stopSidebarDrag() {
  if (isDraggingSidebar.value) {
    isDraggingSidebar.value = false
    safeSetItem('ciao:sidebar-width', String(sidebarWidth.value))
    window.removeEventListener('mousemove', handleSidebarDrag)
    window.removeEventListener('mouseup', stopSidebarDrag)
    document.body.classList.remove('is-dragging-layout')
  }
}

function startSplitDrag(e: MouseEvent) {
  e.preventDefault()
  isDraggingSplit.value = true
  dragStartRatio = chatSplitRatio.value
  dragStartX = e.clientX
  
  const splitContainer = document.querySelector('.chat-main')
  if (splitContainer) {
    const rect = splitContainer.getBoundingClientRect()
    dragContainerWidth = rect.width
    dragContainerLeft = rect.left
  } else {
    dragContainerWidth = window.innerWidth - (sidebarCollapsed.value ? 40 : sidebarWidth.value)
    dragContainerLeft = sidebarCollapsed.value ? 40 : sidebarWidth.value
  }
  
  window.addEventListener('mousemove', handleSplitDrag)
  window.addEventListener('mouseup', stopSplitDrag)
  document.body.classList.add('is-dragging-layout')
}

function handleSplitDrag(e: MouseEvent) {
  if (!isDraggingSplit.value) return
  
  const clientX = e.clientX
  let newLeftWidth = clientX - dragContainerLeft
  
  const minLeft = MIN_PANE_WIDTH
  const maxLeft = dragContainerWidth - MIN_PANE_WIDTH
  
  if (maxLeft < minLeft) {
    chatSplitRatio.value = 0.5
    return
  }
  
  newLeftWidth = Math.max(minLeft, Math.min(maxLeft, newLeftWidth))
  let ratio = newLeftWidth / dragContainerWidth
  
  const defaultSplitWidth = dragContainerWidth * DEFAULT_SPLIT_RATIO
  if (Math.abs(newLeftWidth - defaultSplitWidth) < SPLIT_SNAP_THRESHOLD) {
    ratio = DEFAULT_SPLIT_RATIO
  }
  
  chatSplitRatio.value = ratio
}

function stopSplitDrag() {
  if (isDraggingSplit.value) {
    isDraggingSplit.value = false
    safeSetItem('ciao:chat-split-ratio', String(chatSplitRatio.value))
    window.removeEventListener('mousemove', handleSplitDrag)
    window.removeEventListener('mouseup', stopSplitDrag)
    document.body.classList.remove('is-dragging-layout')
  }
}

// Welcome-screen mascot. Hover or click swaps between the two faces:
// hover XOR the persistent click-toggle picks the scared face.
const faceHover = ref(false)
const faceToggled = ref(false)
const faceSrc = computed(() => (faceHover.value !== faceToggled.value ? '/face_scared.png' : '/face.png'))
const taskStore = useTaskStore()
const route = useRoute()
const router = useRouter()
const projectIdParam = computed(() => (route.params.projectId as string) || '')
const viewMode = computed<'chat' | 'project' | 'schedules' | 'settings'>(() => {
  const path = route.path
  if (path.startsWith('/settings')) return 'settings'
  if (path.startsWith('/schedules')) return 'schedules'
  if (projectIdParam.value) return 'project'
  return 'chat'
})
const sidebarCollapsed = ref(false)
const showNewSchedule = ref(false)
const isMobile = ref(window.innerWidth < 768)

// Current project id for pinned-file lookup.
const currentProjectId = computed(() => {
  if (projectIdParam.value) return projectIdParam.value
  const chat = store.activeChat
  if (chat?.project_id) return chat.project_id
  return ''
})
const generalWorkspaceActions = computed(() => {
  return store.workspaceOptions
    .map(workspace => {
      const projectId = store.projects.find(
        p => p.name === 'General' && p.workspace === workspace.name,
      )?.project_id || ''
      return {
        workspace: workspace.name,
        label: workspaceLabel(workspace.name),
        projectId,
        isCreating: Boolean(projectId && store.creatingChatProjectIds[projectId]),
      }
    })
    .filter(action => action.projectId)
})

function workspaceLabel(name: string): string {
  if (!name) return 'Workspace'
  return name
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

async function createWorkspaceChat(action: { workspace: string; projectId: string; isCreating: boolean }) {
  if (!action.projectId || action.isCreating) return
  await store.switchWorkspace(action.workspace)
  await store.createChat(action.projectId)
}
const activePinKey = computed(() => {
  return store.activeChatId || currentProjectId.value
})
const pinnedFilePath = computed(() => {
  if (isMobile.value) return ''
  // Pinned files are scoped. When the user navigates to a global
  // surface (settings, schedules), the split layout would otherwise mask
  // those views entirely because the v-if="pinnedFilePath" branch only
  // renders ProjectView/ChatPanel. Hide the pin in those modes; the store
  // entry stays intact, so coming back restores it.
  if (viewMode.value === 'settings' || viewMode.value === 'schedules') return ''
  return activePinKey.value ? store.pinnedFileFor(activePinKey.value) || '' : ''
})
function unpinCurrent(): void {
  if (activePinKey.value) store.unpinFile(activePinKey.value)
}

function onResize() {
  const wasMobile = isMobile.value
  isMobile.value = window.innerWidth < 768
  if (wasMobile && !isMobile.value) {
    // Switched to desktop: expand sidebar
    sidebarCollapsed.value = false
  } else if (!wasMobile && isMobile.value) {
    // Switched to mobile: collapse sidebar
    sidebarCollapsed.value = true
  }
}
window.addEventListener('resize', onResize)

onMounted(async () => {
  await store.fetchAll()
  taskStore.fetchSchedules().catch(() => {})
  const chatId = route.params.chatId as string
  if (chatId && store.chats.find(c => c.chat_id === chatId)) {
    store.switchChat(chatId)
  }
  // Auto-collapse sidebar on mobile when a chat is active
  if (isMobile.value && store.activeChat) {
    sidebarCollapsed.value = true
  }
})

watch(() => route.path, (p) => {
  if (!p.startsWith('/schedules')) showNewSchedule.value = false
})

// React to route changes (e.g. clicking a chat link from ProjectView).
watch(
  () => route.params.chatId,
  (chatId) => {
    const id = chatId as string
    if (!id) return
    if (!store.chats.find(c => c.chat_id === id)) return
    if (store.activeChatId !== id) store.switchChat(id)
    else void store.markRead(id)
    if (isMobile.value) sidebarCollapsed.value = true
  }
)

// Auto-collapse sidebar on mobile
if (window.innerWidth < 768) {
  sidebarCollapsed.value = true
}

function onChatSelected() {
  // On mobile, collapse sidebar when a chat is selected
  if (isMobile.value) {
    sidebarCollapsed.value = true
  }
}

function closeChat() {
  if (isMobile.value) {
    // On mobile, show sidebar
    sidebarCollapsed.value = false
  }
  store.activeChatId = null
}

function closeProject() {
  router.push('/')
  if (isMobile.value) sidebarCollapsed.value = false
}

// Edge-swipe to open the sidebar on mobile. Swipe-left on an open sidebar
// closes it. Touch state is captured only when the gesture starts from the
// left edge (or from inside the open sidebar), so normal horizontal scrolling
// inside messages / code blocks stays untouched.
const EDGE_WIDTH = 24 // px from left where a swipe-to-open can begin
const OPEN_THRESHOLD = 60 // px of rightward travel to count as "open"
const CLOSE_THRESHOLD = 60 // px of leftward travel to count as "close"
const VERTICAL_TOLERANCE = 0.8 // |dy| must be < this * |dx| to count as horizontal

let touchStartX = 0
let touchStartY = 0
let touchTracking: 'open' | 'close' | null = null

function onTouchStart(e: TouchEvent) {
  if (!isMobile.value) return
  if (e.touches.length !== 1) { touchTracking = null; return }
  const t = e.touches[0]
  touchStartX = t.clientX
  touchStartY = t.clientY
  if (sidebarCollapsed.value && touchStartX <= EDGE_WIDTH) {
    touchTracking = 'open'
  } else if (!sidebarCollapsed.value) {
    // Only start a close-tracker if the touch began inside the sidebar pane,
    // not on the backdrop (backdrop has its own @click to close).
    const target = e.target as HTMLElement | null
    if (target && target.closest('.sidebar')) {
      touchTracking = 'close'
    } else {
      touchTracking = null
    }
  } else {
    touchTracking = null
  }
}

function onTouchEnd(e: TouchEvent) {
  if (!touchTracking) return
  const t = e.changedTouches[0]
  const dx = t.clientX - touchStartX
  const dy = t.clientY - touchStartY
  const horizontal = Math.abs(dy) < Math.abs(dx) * VERTICAL_TOLERANCE
  if (touchTracking === 'open' && horizontal && dx > OPEN_THRESHOLD) {
    sidebarCollapsed.value = false
  } else if (touchTracking === 'close' && horizontal && dx < -CLOSE_THRESHOLD) {
    sidebarCollapsed.value = true
  }
  touchTracking = null
}

onMounted(() => {
  window.addEventListener('touchstart', onTouchStart, { passive: true })
  window.addEventListener('touchend', onTouchEnd, { passive: true })
  window.addEventListener('touchcancel', onTouchEnd, { passive: true })
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  window.removeEventListener('touchstart', onTouchStart)
  window.removeEventListener('touchend', onTouchEnd)
  window.removeEventListener('touchcancel', onTouchEnd)
  window.removeEventListener('mousemove', handleSidebarDrag)
  window.removeEventListener('mouseup', stopSidebarDrag)
  window.removeEventListener('mousemove', handleSplitDrag)
  window.removeEventListener('mouseup', stopSplitDrag)
  document.body.classList.remove('is-dragging-layout')
})
</script>

<style scoped>
.chat-layout {
  display: flex;
  height: var(--app-h, 100dvh);
  overflow: hidden;
}

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.empty-shell {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  color: var(--fg2);
  padding: calc(var(--space-4) + var(--safe-top))
           calc(var(--space-4) + var(--safe-right))
           calc(var(--space-4) + var(--safe-bottom))
           calc(var(--space-4) + var(--safe-left));
  text-align: center;
  overflow: hidden;
}

.empty-state .empty-mark {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  opacity: 0.85;
}
.empty-state .empty-face {
  width: 120px;
  height: 120px;
  image-rendering: pixelated;
  cursor: pointer;
  user-select: none;
  -webkit-user-drag: none;
  transition: transform 120ms var(--ease);
}
.empty-state .empty-face:hover { transform: scale(1.08); }
.empty-state .empty-face:active { transform: scale(0.94); }
.empty-state .empty-hint {
  color: var(--fg3);
  font-size: var(--text-sm);
  max-width: 100%;
}

.empty-actions {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
  justify-content: center;
}

.sidebar-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 40;
  animation: fade-in 160ms var(--ease);
}

@keyframes fade-in { from { opacity: 0 } to { opacity: 1 } }

/* Split-screen layout for pinned file viewer.
   Both panes share the available width 50/50 (clamped to a sensible
   minimum), so the file viewer doesn't get squeezed below ~45% on
   wide screens the way it did when capped at 720px. */
.chat-split {
  flex-direction: row;
}
.chat-split-main {
  width: 50%;
  flex: 1 1 0;
  min-width: 360px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.chat-split-side {
  width: 50%;
  flex: 1 1 0;
  min-width: 360px;
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg);
}

.sidebar-resizer,
.chat-split-resizer {
  position: relative;
  width: 6px;
  margin-left: -3px;
  margin-right: -3px;
  cursor: col-resize;
  z-index: 10;
  user-select: none;
}
.sidebar-resizer::after,
.chat-split-resizer::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 2px;
  width: 2px;
  background-color: transparent;
  transition: background-color 0.15s ease;
}
.sidebar-resizer:hover::after,
.sidebar-resizer.is-dragging::after,
.chat-split-resizer:hover::after,
.chat-split-resizer.is-dragging::after {
  background-color: var(--accent);
}

:global(body.is-dragging-layout) {
  user-select: none !important;
  -webkit-user-select: none !important;
  cursor: col-resize !important;
}
:global(body.is-dragging-layout iframe),
:global(body.is-dragging-layout object),
:global(body.is-dragging-layout embed) {
  pointer-events: none !important;
}

@media (max-width: 768px) {
  .chat-layout { position: relative; }
  .chat-split-side { display: none; }
}
</style>
