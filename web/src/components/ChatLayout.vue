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
          <div v-else-if="!store.bootstrapped" class="empty-shell home-boot" aria-busy="true">
            <PaneHeader title="ciaobot" @open-sidebar="sidebarCollapsed = false" />
          </div>
          <div v-else class="empty-shell">
            <PaneHeader title="ciaobot" @open-sidebar="sidebarCollapsed = false" />
            <div class="empty-state">
              <div class="empty-mark">
                <button
                  type="button"
                  class="empty-face-btn"
                  aria-label="Say hello"
                  @click="onFaceClick"
                  @mouseenter="onFaceEnter"
                  @mouseleave="onFaceLeave"
                >
                  <Transition name="face-bubble">
                    <div v-if="speechGreeting" :key="speechGreeting" class="face-speech-bubble">
                      {{ speechGreeting }}
                    </div>
                  </Transition>
                  <img
                    class="empty-face"
                    :src="faceSrc"
                    alt=""
                    draggable="false"
                  />
                </button>
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
        <div v-else-if="!store.bootstrapped" class="empty-shell home-boot" aria-busy="true">
          <PaneHeader title="ciaobot" @open-sidebar="sidebarCollapsed = false" />
        </div>
        <div v-else class="empty-shell">
          <PaneHeader title="ciaobot" @open-sidebar="sidebarCollapsed = false" />
          <div class="empty-state">
            <div class="empty-mark">
              <button
                type="button"
                class="empty-face-btn"
                aria-label="Say hello"
                @click="onFaceClick"
                @mouseenter="onFaceEnter"
                @mouseleave="onFaceLeave"
              >
                <Transition name="face-bubble">
                  <div v-if="speechGreeting" :key="speechGreeting" class="face-speech-bubble">
                    {{ speechGreeting }}
                  </div>
                </Transition>
                <img
                  class="empty-face"
                  :src="faceSrc"
                  alt=""
                  draggable="false"
                />
              </button>
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
            <OnboardingCard variant="home" @open-sidebar="sidebarCollapsed = false" />
          </div>
        </div>
      </template>
    </div>
    <FileViewerModal />
    <ProductTour />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import { useProductTourStore } from '../stores/productTour'
import ProjectSidebar from './ProjectSidebar.vue'
import ChatPanel from './ChatPanel.vue'
import ProjectView from './ProjectView.vue'
import SchedulePanel from './SchedulePanel.vue'
import SettingsView from './SettingsView.vue'
import FileViewerModal from './FileViewerModal.vue'
import PinnedFilePanel from './PinnedFilePanel.vue'
import PaneHeader from './PaneHeader.vue'
import ProductTour from './ProductTour.vue'
import OnboardingCard from './OnboardingCard.vue'

const store = useProjectStore()
const tourStore = useProductTourStore()

const DEFAULT_SIDEBAR_WIDTH = 280
const MIN_SIDEBAR_WIDTH = 180
const MAX_SIDEBAR_WIDTH = 500
const SIDEBAR_SNAP_THRESHOLD = 15 // px

const DEFAULT_SPLIT_RATIO = 0.5
const MIN_CHAT_PANE_WIDTH = 240
const MIN_SIDE_PANE_WIDTH = 240
const SPLIT_SNAP_THRESHOLD = 15 // px
const LATEST_STATUS_SYNC_MS = 15000

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
  
  const minLeft = MIN_CHAT_PANE_WIDTH
  const maxLeft = dragContainerWidth - MIN_SIDE_PANE_WIDTH
  
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

// Welcome-screen mascot. Hovering shows a comic bubble with "hello" in a
// different language until the pointer leaves; clicking pins the bubble
// on until the next click.
const FACE_GREETINGS = [
  'Ciao!', '¡Hola!', 'Salut!', 'Hallo!', 'Olá!', 'Hello!',
  'こんにちは!', '안녕!', '你好!', 'مرحبا!', 'Привет!', 'नमस्ते!',
  'Merhaba!', 'Γειά σου!', 'Hej!', 'Cześć!', 'สวัสดี!', 'Xin chào!',
  'שלום!', 'Halo!', 'Ahoj!', 'Szia!', 'Dia dhuit!', 'Sawubona!',
] as const

const speechGreeting = ref<string | null>(null)
const speechPinned = ref(false)
let greetingQueue: string[] = []

function shuffleGreetings(): string[] {
  const next = [...FACE_GREETINGS]
  for (let i = next.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[next[i], next[j]] = [next[j], next[i]]
  }
  return next
}

function nextGreeting(): string {
  if (greetingQueue.length === 0) greetingQueue = shuffleGreetings()
  return greetingQueue.pop()!
}

function onFaceClick() {
  speechPinned.value = !speechPinned.value
  speechGreeting.value = speechPinned.value ? nextGreeting() : null
}

function onFaceEnter() {
  if (!speechPinned.value) speechGreeting.value = nextGreeting()
}

function onFaceLeave() {
  if (!speechPinned.value) speechGreeting.value = null
}

const faceSrc = computed(() => (speechGreeting.value ? '/face_scared.png' : '/face.png'))
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
let latestStatusSyncTimer: ReturnType<typeof window.setInterval> | null = null

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

function openSidebarForTour() {
  sidebarCollapsed.value = false
}

async function navigateToChatForTour() {
  if (viewMode.value !== 'chat') {
    await router.push('/')
  }
}

async function ensureWelcomeChatForTour() {
  if (store.activeChat) return
  const welcome = store.chats.find(c => /welcome|connect existing vault/i.test(c.title))
  const target = welcome
    ?? (() => {
      const general = store.projects.find(p => p.name === 'General')
      if (!general) return store.chats[0]
      return store.chats.find(c => c.project_id === general.project_id)
    })()
  if (!target) return
  await store.switchChat(target.chat_id)
  await router.push(`/chat/${target.chat_id}`)
  if (isMobile.value) sidebarCollapsed.value = true
}

async function waitForStartupDismissed() {
  for (let i = 0; i < 120; i++) {
    if (!document.querySelector('.startup-overlay')) return
    await new Promise<void>(resolve => setTimeout(resolve, 250))
  }
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

function startLatestStatusSync() {
  if (latestStatusSyncTimer) return
  latestStatusSyncTimer = window.setInterval(() => {
    void store.syncLatest()
  }, LATEST_STATUS_SYNC_MS)
}

function stopLatestStatusSync() {
  if (!latestStatusSyncTimer) return
  window.clearInterval(latestStatusSyncTimer)
  latestStatusSyncTimer = null
}

onMounted(async () => {
  tourStore.registerHooks({
    openSidebar: openSidebarForTour,
    navigateToChat: navigateToChatForTour,
    ensureWelcomeChat: ensureWelcomeChatForTour,
  })
  await store.fetchAll()
  startLatestStatusSync()
  taskStore.fetchSchedules().catch(() => {})
  taskStore.fetchLoops().catch(() => {})
  const chatId = route.params.chatId as string
  if (chatId && store.chats.find(c => c.chat_id === chatId)) {
    await store.openChatFromDeepLink(chatId)
  }
  // Auto-collapse sidebar on mobile when a chat is active
  if (isMobile.value && store.activeChat) {
    sidebarCollapsed.value = true
  }
  await waitForStartupDismissed()
  void tourStore.maybeAutoStart()
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
    if (store.activeChatId !== id) void store.openChatFromDeepLink(id)
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
  stopLatestStatusSync()
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
.empty-state .empty-face-btn {
  position: relative;
  display: inline-flex;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
  user-select: none;
  transition: transform 120ms var(--ease);
}
.empty-state .empty-face-btn:active { transform: scale(0.94); }
.empty-state .empty-face {
  display: block;
  width: 120px;
  height: 120px;
  image-rendering: pixelated;
  -webkit-user-drag: none;
  pointer-events: none;
}
.face-speech-bubble {
  position: absolute;
  bottom: calc(100% + 10px);
  left: 50%;
  transform: translateX(-50%);
  padding: 8px 14px;
  background: #fff;
  color: #111;
  border: 3px solid #111;
  border-radius: 14px 14px 14px 4px;
  box-shadow: 4px 4px 0 #111;
  font-size: 15px;
  font-weight: 700;
  line-height: 1.2;
  white-space: nowrap;
  z-index: 1;
}
.face-speech-bubble::after {
  content: '';
  position: absolute;
  left: 18px;
  bottom: -12px;
  width: 0;
  height: 0;
  border: 6px solid transparent;
  border-top-color: #111;
}
.face-speech-bubble::before {
  content: '';
  position: absolute;
  left: 20px;
  bottom: -6px;
  width: 0;
  height: 0;
  border: 4px solid transparent;
  border-top-color: #fff;
  z-index: 1;
}
.face-bubble-enter-active {
  animation: face-bubble-pop 220ms var(--ease);
}
.face-bubble-leave-active {
  animation: face-bubble-pop 160ms var(--ease) reverse;
}
@keyframes face-bubble-pop {
  from {
    opacity: 0;
    transform: translateX(-50%) translateY(6px) scale(0.82);
  }
  to {
    opacity: 1;
    transform: translateX(-50%) translateY(0) scale(1);
  }
}
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

/* Split-screen layout for pinned file viewer. Both panes share width 50/50
   by default; min-width is a soft floor during drag so a compressed window
   can still show chat and the pinned document side by side. */
.chat-split {
  flex-direction: row;
}
.chat-split-main {
  width: 50%;
  flex: 1 1 0;
  min-width: 240px;
  container-type: inline-size;
  container-name: chat-split;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.chat-split-side {
  width: 50%;
  flex: 1 1 0;
  min-width: 240px;
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
