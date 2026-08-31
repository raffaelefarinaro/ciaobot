<template>
  <div class="chat-layout" :class="{ 'sidebar-open': !sidebarCollapsed }">
    <ProjectSidebar
      ref="projectSidebarRef"
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
          <SubagentChatView
            v-else-if="subagentRoute"
            :key="subagentRoute.agentId"
            :chat-id="subagentRoute.chatId"
            :agent-id="subagentRoute.agentId"
            @open-sidebar="sidebarCollapsed = false"
          />
          <ChatPanel v-else-if="store.activeChat" ref="chatPanelRef" :key="store.activeChat.chat_id" @close="closeChat" @open-sidebar="sidebarCollapsed = false" />
          <div v-else-if="!store.bootstrapped" class="empty-shell home-boot" aria-busy="true">
            <PaneHeader page-tag="home" @open-sidebar="sidebarCollapsed = false" />
            <div class="home-boot-body">
              <!-- Skeleton of the home screen this will become (lane header
                   with the face+status row inside it, housekeeping tile, chat
                   rows) — a spinner pill read as "nothing is coming", while
                   the shapes promise the layout that is about to land. -->
              <div class="home-boot-skeleton" role="status" aria-live="polite" aria-label="Loading your workspaces">
                <div class="boot-tile" aria-hidden="true">
                  <span class="boot-line boot-shimmer" style="width: 38%"></span>
                  <span class="boot-line boot-shimmer" style="width: 88%"></span>
                  <span class="boot-line boot-shimmer" style="width: 62%"></span>
                </div>
                <div class="boot-lane" aria-hidden="true">
                  <div class="boot-lane-header">
                    <span class="boot-chip boot-shimmer"></span>
                    <span class="boot-line boot-shimmer" style="width: 26%"></span>
                    <span class="boot-pill boot-shimmer"></span>
                  </div>
                  <div class="boot-status">
                    <span class="boot-face boot-shimmer" aria-hidden="true"></span>
                    <span class="boot-line boot-shimmer" style="width: 46%" aria-hidden="true"></span>
                  </div>
                  <div class="boot-row"><span class="boot-line boot-shimmer" style="width: 52%"></span><span class="boot-meta boot-shimmer"></span></div>
                  <div class="boot-row"><span class="boot-line boot-shimmer" style="width: 68%"></span><span class="boot-meta boot-shimmer"></span></div>
                  <div class="boot-row"><span class="boot-line boot-shimmer" style="width: 44%"></span><span class="boot-meta boot-shimmer"></span></div>
                </div>
              </div>
            </div>
          </div>
          <!-- On mobile, the sidebar already lists every active chat. Showing the
               homepage behind it after closing a chat would just duplicate the
               same list. Hide the empty-state whenever the mobile sidebar is open. -->
          <div v-else-if="!(isMobile && !sidebarCollapsed)" class="empty-shell">
            <PaneHeader page-tag="home" @open-sidebar="sidebarCollapsed = false" />
            <div class="empty-state" :class="{ 'empty-state--active': hasHomeActivity }">
              <!-- The glanceable status (face + summary) lives inside the active
                   workspace's lane header now (HomeRecentChats.vue), right under
                   the workspace name it summarizes. This header only remains for
                   the first-run greeting face, which is the sole greeting-bubble
                   owner: beside a status line that reads "nothing needs you" the
                   bubble used to collide with it. -->
              <div v-if="!hasHomeActivity" class="empty-home-header">
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
                    <img class="empty-face" :src="faceSrc" alt="" draggable="false" />
                  </button>
                </div>
              </div>
              <HousekeepingStrip />
              <HomeRecentChats ref="homeRecentRef" @new-workspace-chat="createWorkspaceChat" />
              <div v-if="showGlobalNewChatActions" class="empty-actions">
                <button
                  v-for="action in generalWorkspaceActions"
                  :key="action.workspace"
                  class="btn-primary"
                  :data-workspace-color="action.color"
                  :disabled="action.isCreating"
                  @click="createWorkspaceChat(action)"
                >
                  {{ action.isCreating ? 'Creating...' : `+ ${action.label} chat` }}
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
          <PinnedFilePanel :key="pinnedFilePath" :file-path="pinnedFilePath" @close="unpinCurrent" />
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
        <MemoryMapView
          v-else-if="viewMode === 'memory' || viewMode === 'proposals'"
          @open-sidebar="sidebarCollapsed = false"
        />
        <ProjectView
          v-else-if="projectIdParam"
          :project-id="projectIdParam"
          @close="closeProject"
          @open-sidebar="sidebarCollapsed = false"
        />
        <SubagentChatView
          v-else-if="subagentRoute"
          :key="subagentRoute.agentId"
          :chat-id="subagentRoute.chatId"
          :agent-id="subagentRoute.agentId"
          @open-sidebar="sidebarCollapsed = false"
        />
        <ChatPanel v-else-if="store.activeChat" ref="chatPanelRef" :key="store.activeChat.chat_id" @close="closeChat" @open-sidebar="sidebarCollapsed = false" />
        <div v-else-if="!store.bootstrapped" class="empty-shell home-boot" aria-busy="true">
          <PaneHeader page-tag="home" @open-sidebar="sidebarCollapsed = false" />
          <div class="home-boot-body">
            <!-- Same skeleton as the split-view copy above; only one is ever
                 mounted, so the two must stay identical. -->
            <div class="home-boot-skeleton" role="status" aria-live="polite" aria-label="Loading your workspaces">
              <div class="boot-tile" aria-hidden="true">
                <span class="boot-line boot-shimmer" style="width: 38%"></span>
                <span class="boot-line boot-shimmer" style="width: 88%"></span>
                <span class="boot-line boot-shimmer" style="width: 62%"></span>
              </div>
              <div class="boot-lane" aria-hidden="true">
                <div class="boot-lane-header">
                  <span class="boot-chip boot-shimmer"></span>
                  <span class="boot-line boot-shimmer" style="width: 26%"></span>
                  <span class="boot-pill boot-shimmer"></span>
                </div>
                <div class="boot-status">
                  <span class="boot-face boot-shimmer" aria-hidden="true"></span>
                  <span class="boot-line boot-shimmer" style="width: 46%" aria-hidden="true"></span>
                </div>
                <div class="boot-row"><span class="boot-line boot-shimmer" style="width: 52%"></span><span class="boot-meta boot-shimmer"></span></div>
                <div class="boot-row"><span class="boot-line boot-shimmer" style="width: 68%"></span><span class="boot-meta boot-shimmer"></span></div>
                <div class="boot-row"><span class="boot-line boot-shimmer" style="width: 44%"></span><span class="boot-meta boot-shimmer"></span></div>
              </div>
            </div>
          </div>
        </div>
        <!-- On mobile, the sidebar already lists every active chat. Showing the
             homepage behind it after closing a chat would just duplicate the
             same list. Hide the empty-state whenever the mobile sidebar is open. -->
        <div v-else-if="!(isMobile && !sidebarCollapsed)" class="empty-shell">
          <PaneHeader page-tag="home" @open-sidebar="sidebarCollapsed = false" />
          <div class="empty-state" :class="{ 'empty-state--active': hasHomeActivity }">
            <!-- The glanceable status (face + summary) lives inside the active
                 workspace's lane header now (HomeRecentChats.vue), right under
                 the workspace name it summarizes. This header only remains for
                 the first-run greeting face, which is the sole greeting-bubble
                 owner: beside a status line that reads "nothing needs you" the
                 bubble used to collide with it. -->
            <div v-if="!hasHomeActivity" class="empty-home-header">
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
                  <img class="empty-face" :src="faceSrc" alt="" draggable="false" />
                </button>
              </div>
            </div>
            <HousekeepingStrip />
            <HomeRecentChats ref="homeRecentRef" @new-workspace-chat="createWorkspaceChat" />
            <div v-if="showGlobalNewChatActions" class="empty-actions">
              <button
                v-for="action in generalWorkspaceActions"
                :key="action.workspace"
                class="btn-primary"
                :data-workspace-color="action.color"
                :disabled="action.isCreating"
                @click="createWorkspaceChat(action)"
              >
                {{ action.isCreating ? 'Creating...' : `+ ${action.label} chat` }}
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
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import { useTaskStore } from '../stores/tasks'
import { useMemoryMapStore } from '../stores/memoryMap'
import ProjectSidebar from './ProjectSidebar.vue'
import ChatPanel from './ChatPanel.vue'
// The destinations behind the rail are loaded on first visit, not on boot.
// Statically imported they landed in the same chunk as the chat itself — a
// third of a megabyte of Settings, Automations, the memory canvas and the
// project view that every cold start had to download and parse before the
// first chat could paint, whether or not the user ever opened them. They are
// each rendered behind a `viewMode` branch, so a component-level split needs
// no other change; the chunk is fetched from the local server (and is
// cache-first in the service worker after that), so the first switch is not
// perceptibly slower.
const MemoryMapView = defineAsyncComponent(() => import('./MemoryMapView.vue'))
const SubagentChatView = defineAsyncComponent(() => import('./SubagentChatView.vue'))
const ProjectView = defineAsyncComponent(() => import('./ProjectView.vue'))
const SchedulePanel = defineAsyncComponent(() => import('./SchedulePanel.vue'))
const SettingsView = defineAsyncComponent(() => import('./SettingsView.vue'))
import FileViewerModal from './FileViewerModal.vue'
import PinnedFilePanel from './PinnedFilePanel.vue'
import PaneHeader from './PaneHeader.vue'
import HomeRecentChats from './HomeRecentChats.vue'
import HousekeepingStrip from './HousekeepingStrip.vue'
import { formatDocumentTitle, settingsTabTitle } from '../lib/appTitle'
import { normalizeWorkspaceColor } from '../lib/workspaceColors'
import { pendingConfirm } from '../lib/confirm'
import { pendingPrompt } from '../lib/prompt'
import { isDesktopApp } from '../lib/desktop'
import { FONT_SCALE_STEP, useFontScale } from '../composables/useFontScale'

const store = useProjectStore()
const fileViewer = useFileViewerStore()

// Refs into the active ChatPanel, used by the global keyboard shortcuts to
// reach composer-owned actions (dictation, archive).
//
// The template declares ChatPanel and HomeRecentChats twice, once under
// `v-if="pinnedFilePath"` (split view) and once under the `v-else` (no pinned
// file). Both copies must carry the ref: only one is ever mounted, so the ref
// holds whichever that is, but tagging only the split-view copy left Cmd+D,
// Cmd+A and the home arrow keys silently dead in the far more common
// no-pinned-file layout.
const chatPanelRef = ref<InstanceType<typeof ChatPanel> | null>(null)
// Ref into HomeRecentChats for arrow-key navigation on the home screen.
const homeRecentRef = ref<InstanceType<typeof HomeRecentChats> | null>(null)
const projectSidebarRef = ref<InstanceType<typeof ProjectSidebar> | null>(null)
const schedulePanelRef = ref<any>(null)

// Reactive handle on the global font scale. Used by the Cmd/Ctrl+Shift+= and
// Cmd/Ctrl+Shift+- shortcuts (below); the same composable is consumed by
// Settings → Appearance so the +/- buttons and the shortcuts stay in sync.
const fontScale = useFontScale()

// Wide enough for the nav row to show the active item's label ("automations" is
// the longest) beside all four glyphs. At 280 the row could not fit it and the
// label was clipped mid-word; below ~334 the header now drops the label rather
// than cutting it, so this is the width at which the labelled row is intact.
const DEFAULT_SIDEBAR_WIDTH = 340
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
const memoryMapStore = useMemoryMapStore()
const route = useRoute()
const router = useRouter()
const projectIdParam = computed(() => (route.params.projectId as string) || '')
// /chat/:chatId/subagent/:agentId — a read-only view of one subagent's own
// conversation. It replaces the chat pane rather than opening beside it: the
// parent chat is one click away in the header, and the sidebar row stays
// selected so the relationship is still visible.
const subagentRoute = computed(() => {
  const chatId = (route.params.chatId as string) || ''
  const agentId = (route.params.agentId as string) || ''
  return chatId && agentId ? { chatId, agentId } : null
})
const viewMode = computed<'chat' | 'project' | 'schedules' | 'settings' | 'memory' | 'proposals'>(() => {
  const path = route.path
  if (path.startsWith('/settings')) return 'settings'
  if (path.startsWith('/schedules')) return 'schedules'
  if (path.startsWith('/memory')) return 'memory'
  if (path.startsWith('/proposals')) return 'proposals'
  if (projectIdParam.value) return 'project'
  return 'chat'
})
// Whether the chat-surface keyboard shortcuts apply. Only the full-screen
// views own the keyboard: `viewMode` is 'project' on /project/:projectId --
// the same ChatLayout, sidebar and open chat as /chat/:id -- so gating those
// shortcuts on `=== 'chat'` silently killed Esc and the arrow keys for anyone
// who reached a chat through a project. It read as "Esc only works after I
// click somewhere else", because clicking a chat in the sidebar navigates to
// /chat/:id and revived the handler. One predicate, so the next view mode
// added has a single place to declare itself.
// Split in two so the number-key workspace shortcut, which is useful on the
// schedules view, does not have to restate the rest of the gate and drift
// from it. Anything that owns the screen — a confirm dialog, the file viewer
// modal — belongs in the base predicate, so a new overlay is declared once.
const viewShortcutsActive = computed(() =>
  viewMode.value !== 'settings'
  && !pendingConfirm.value
  && !pendingPrompt.value
  && !fileViewer.isOpen,
)
const shortcutsActive = computed(() =>
  viewShortcutsActive.value && viewMode.value !== 'schedules' && viewMode.value !== 'memory' && viewMode.value !== 'proposals',
)
const sidebarCollapsed = ref(false)
const showNewSchedule = ref(false)
const isMobile = ref(window.innerWidth < 768)
let latestStatusSyncTimer: ReturnType<typeof setInterval> | null = null

// Current project id for pinned-file lookup.
const currentProjectId = computed(() => {
  if (projectIdParam.value) return projectIdParam.value
  const chat = store.activeChat
  if (chat?.project_id) return chat.project_id
  return ''
})
// Only for the true empty state. Every lane carries its own "+ new" and lanes
// no longer collapse at narrow widths, so on a phone with chats these were a
// second, louder copy of an action already on screen — and a saturated fill
// spent on something non-blocking, which the design system reserves for
// "needs the user".
const showGlobalNewChatActions = computed(() => !store.activeChatsAll.length)

// Keep Home visibly alive while an archived chat is still being processed.
// Post-archive work is not part of activeChatsAll, but it is still something
// the home surface should report before the user starts a new chat.
const hasHomeActivity = computed(() => (
  store.activeChatsAll.length > 0 || store.archivingChatsList().length > 0 || store.postprocessingChats().length > 0
))

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
        color: normalizeWorkspaceColor(workspace.color),
        isCreating: Boolean(projectId && store.creatingChatProjectIds[projectId]),
      }
    })
    .filter(action => action.projectId)
})

function workspaceLabel(name: string): string {
  if (!name) return 'workspace'
  return name
    .split(/[-_\s]+/)
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
}

const pageDocumentTitle = computed(() => {
  if (viewMode.value === 'settings') {
    return settingsTabTitle(route.params.tab as string | undefined)
  }
  if (viewMode.value === 'schedules') {
    if (showNewSchedule.value) return 'new automation'
    const scheduleId = route.params.scheduleId as string | undefined
    if (scheduleId) {
      const schedule = taskStore.schedules.find(s => s.schedule_id === scheduleId)
      if (schedule?.title) return schedule.title
    }
    return 'automations'
  }
  if (viewMode.value === 'memory') return 'memory'
  if (viewMode.value === 'proposals') return 'proposals'
  if (projectIdParam.value) {
    const project = store.projects.find(p => p.project_id === projectIdParam.value)
    return project?.name || 'project'
  }
  if (store.activeChat?.title) return store.activeChat.title
  return null
})

if (typeof document !== 'undefined') {
  watch(
    [pageDocumentTitle, () => store.totalUnread],
    ([pageTitle, unread]) => {
      document.title = formatDocumentTitle(pageTitle, unread)
    },
    { immediate: true },
  )
}

async function createWorkspaceChat(action: { workspace: string; projectId: string; isCreating: boolean }) {
  if (!action.projectId || action.isCreating) return
  await store.switchWorkspace(action.workspace)
  await store.createChat(action.projectId)
}

// Cmd+T (Desktop) / Option+N (Web/PWA): show the new-chat picker, which drills
// workspace → project and resolves to the chosen project's id.
async function handleNewChatShortcut() {
  const { openNewChatPicker } = await import('../lib/newChat')
  const projectId = await openNewChatPicker()
  if (!projectId) return
  await store.newChatInProject(projectId)
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
  if (viewMode.value === 'settings' || viewMode.value === 'schedules' || viewMode.value === 'memory' || viewMode.value === 'proposals') return ''
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

function startLatestStatusSync() {
  if (latestStatusSyncTimer) return
  latestStatusSyncTimer = setInterval(() => {
    void store.syncLatest()
  }, LATEST_STATUS_SYNC_MS)
}

function stopLatestStatusSync() {
  if (!latestStatusSyncTimer) return
  clearInterval(latestStatusSyncTimer)
  latestStatusSyncTimer = null
}

onMounted(async () => {
  await store.fetchAll()
  startLatestStatusSync()
  taskStore.fetchSchedules().catch(() => {})
  const chatId = route.params.chatId as string
  if (chatId && store.chats.find(c => c.chat_id === chatId)) {
    await store.openChatFromDeepLink(chatId)
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
    if (!id) {
      // The sidebar's "chats" nav tab (and any other plain link to `/` or
      // `/chat`) navigates here without going through closeChat(), so
      // activeChatId - and the ChatPanel/keyboard-shortcut logic keyed off
      // it - stayed on the chat the user left. Only bare chat routes mean
      // "go home": project/settings/schedules routes deliberately leave
      // activeChatId populated underneath them (see the Esc handler above),
      // so this only fires when chatId itself changed away from a real id -
      // not on a settings/schedules -> `/` transition, where chatId was
      // already undefined and the retained chat is meant to resurface.
      // Route through the local closeChat() wrapper, not store.closeChat()
      // directly, so a failed close (e.g. the DELETE request itself erroring
      // out) surfaces the same toast the close button and Esc already show,
      // instead of an unhandled rejection with no explanation.
      if (viewMode.value === 'chat' && store.activeChatId) closeChat()
      return
    }
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
  void store.closeChat().catch((error) => {
    store.pushErrorToast('Could not close chat', error instanceof Error ? error.message : 'Could not close chat')
  })
}

// ── Global keyboard shortcuts ───────────────────────────────────────
// Bound in both the PWA and the desktop app, but on different modifiers: the
// Tauri webview owns Cmd+T / Cmd+D / Cmd+A, while a browser tab has already
// spent them on new-tab / bookmark / select-all, so the PWA uses Option
// instead. See onShortcutKeydown for the pairs.
function isTypingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable
}

// Unmodified keys, which no browser reserves: number keys switch to the
// corresponding workspace, arrow keys roam the home recent-chat grid (Enter
// opens the focused card natively), and Esc closes the open chat. Anything
// carrying a modifier stays in onShortcutKeydown.
//
// These must live in exactly ONE listener. They were previously handled here
// AND again in onShortcutKeydown; in the desktop app both listeners are bound,
// so a single arrow press ran onArrow twice and focus jumped two cards at a
// time. The PWA, with only this listener, behaved correctly -- which is why the
// breakage looked desktop-specific.
function onUnreservedKeydown(e: KeyboardEvent) {
  // Switch top-level sections (chat → schedules → memory → settings). Desktop
  // uses Cmd+Arrow; the web PWA uses Option+Arrow, because the browser has
  // already spent Cmd+Left/Right on back/forward. Never Tab: that stays the
  // native focus traversal.
  const mod = e.metaKey || e.ctrlKey
  const alt = e.altKey
  const desktopSection = isDesktopApp() && mod && !alt
  const webSection = !isDesktopApp() && alt && !mod
  const isSectionArrow = (desktopSection || webSection) && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')
  if (isSectionArrow) {
    if (e.repeat || isTypingTarget(e.target) || pendingConfirm.value || pendingPrompt.value || fileViewer.isOpen) return
    const sections = ['/', '/schedules', '/memory', '/settings']
    const current = viewMode.value === 'chat' || viewMode.value === 'project'
      ? 0
      : viewMode.value === 'schedules'
        ? 1
        : viewMode.value === 'memory' || viewMode.value === 'proposals'
          ? 2
          : 3
    const next = (current + (e.key === 'ArrowLeft' ? -1 : 1) + sections.length) % sections.length
    e.preventDefault()
    void router.push(sections[next])
    return
  }

  const bare = !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey

  // Workspace navigation is also useful from the automations view, where the
  // chat-only shortcuts are disabled. Match the visible workspace order and
  // keep the shortcut out of text fields so numbers remain typeable.
  //
  // An open AskUserQuestion card gets first refusal on the same digits. Two
  // features want 1-9, and the card wins *while it is up*: the model is blocked
  // on that prompt, the numbers are printed on the options right there, and
  // switching workspace mid-question is not what anyone means by pressing them.
  // The claim is scoped to the card's lifetime instead of a global mode flag,
  // so workspace switching is untouched the rest of the time and this function
  // keeps no "is a card open" state. Same delegation shape as the home grid's
  // arrows below: the child says whether it used the key, and only then do we
  // preventDefault. Both branches stay inside the existing viewShortcutsActive
  // and typing-target gates, so a confirm dialog or the file viewer still
  // swallows the digit and the composer still types it.
  if (viewShortcutsActive.value && !isTypingTarget(e.target) && !e.defaultPrevented
    && bare && /^[1-9]$/.test(e.key)) {
    if (chatPanelRef.value?.handleQuestionShortcut?.(e)) {
      e.preventDefault()
      return
    }
    // A pending permission card takes the 1/2 keys over the workspace
    // switcher while it is up, mirroring the question card's first-refusal:
    // the model is blocked on the approval and the digits are printed on the
    // buttons. Only the first card is keyed (1 deny / 2 approve).
    if (chatPanelRef.value?.handlePermissionShortcut?.(e)) {
      e.preventDefault()
      return
    }
    const workspace = store.workspaceOptions[Number(e.key) - 1]
    if (workspace) {
      e.preventDefault()
      // The schedules and memory views have no chat to transition into.
      void store.switchWorkspace(workspace.name, {
        transition: viewMode.value !== 'schedules' && viewMode.value !== 'memory',
      })
      return
    }
  }

  // Enter submits a single-select question card that already has an answer
  // picked, so digit-then-Enter never has to reach for the mouse. ChatPanel
  // declines the key whenever a control is focused, which leaves Tab+Enter on
  // the card's own buttons alone.
  if (e.key === 'Enter' && viewShortcutsActive.value && !isTypingTarget(e.target)
    && !e.defaultPrevented && bare) {
    if (chatPanelRef.value?.handleQuestionShortcut?.(e)) {
      e.preventDefault()
      return
    }
  }

  // Esc runs before the chat-only gate: it is the universal way back, and
  // previously it did nothing at all on settings or automations because those
  // views are excluded from shortcutsActive.
  //
  // It closes the open chat even while typing in the composer: escaping a chat
  // is the more useful meaning of the key, and requiring a click-out first was
  // the common complaint. Widgets that genuinely own Esc claim it with
  // stopPropagation (the slash-command picker in ChatPanel, the notification
  // bell), so this never steals the key from them.
  if (e.key === 'Escape') {
    // The confirm dialog and the file viewer own Esc while they are up.
    if (pendingConfirm.value || pendingPrompt.value || fileViewer.isOpen) return
    // A nested control that handled the key already, without claiming it. Popups
    // like ModelSelector close on Esc but do not stopPropagation, and treating
    // that press as "go home" both discarded their dismissal and navigated away
    // from half-finished settings edits.
    if (e.defaultPrevented) return

    // Settings and Automations come first, ahead of any chat handling.
    // activeChatId deliberately stays populated when either is opened from a
    // chat, so checking the chat first meant Esc ran closeChat() on a chat that
    // was not even on screen - disconnecting it, and deleting it outright when it
    // was an unused draft with an unsent composer message. Leaving the view is
    // what the key means there.
    //
    // Project routes stay chat-first on purpose: Esc closing the chat you opened
    // through a project is long-standing, tested behaviour, and a project page is
    // one press further from home either way.
    // Memory Map's own detail panel is a mode within the page, not a
    // separate view — the first Esc closes it (matching the panel's own
    // close button) and only navigates home once nothing is selected.
    if (viewMode.value === 'memory' && memoryMapStore.selectedId) {
      e.preventDefault()
      memoryMapStore.selectNode(null)
      return
    }
    if (viewMode.value === 'settings' || viewMode.value === 'schedules' || viewMode.value === 'memory' || viewMode.value === 'proposals') {
      e.preventDefault()
      void router.push('/')
      return
    }
    // A subagent view is a mode within the chat, not a separate chat: the
    // first Esc goes back to the chat that spawned it, and only the next one
    // closes that chat. Closing outright would disconnect a chat the user
    // never left.
    if (subagentRoute.value) {
      e.preventDefault()
      void router.push(`/chat/${subagentRoute.value.chatId}`)
      return
    }
    if (store.activeChat) {
      e.preventDefault()
      closeChat()
      return
    }
    if (projectIdParam.value) {
      e.preventDefault()
      void router.push('/')
      return
    }
    return
  }

  // Automations: same arrow contract as home, but over the sidebar's
  // schedule lists and the overview pane. This lives outside the chat-only
  // gate because shortcutsActive deliberately excludes schedules — the
  // lists still want keyboard roaming.
  if (viewMode.value === 'schedules' && e.key.startsWith('Arrow')) {
    if (isTypingTarget(e.target) || e.defaultPrevented) return
    if (projectSidebarRef.value?.onArrow?.(e.key)) {
      e.preventDefault()
      return
    }
    if (schedulePanelRef.value?.onArrow?.(e.key)) {
      e.preventDefault()
      return
    }
    return
  }

  if (!shortcutsActive.value) return

  if (e.key.startsWith('Arrow')) {
    // Arrows keep deferring to text fields, or they would break caret movement.
    if (store.activeChat || isTypingTarget(e.target)) return
    // ...and to any open menu that already consumed the key. The Escape branch
    // above checks this; without the same check here, an arrow inside the home
    // lane's project menu moved the menu's focus *and* roamed the chat grid,
    // leaving the menu open with focus somewhere else entirely.
    if (e.defaultPrevented) return
    if (homeRecentRef.value?.onArrow(e.key)) e.preventDefault()
    return
  }

}

function onShortcutKeydown(e: KeyboardEvent) {
  if (!shortcutsActive.value) return
  // Holding the chord repeats the keydown at OS speed. None of these actions
  // are meant to fire more than once per press: New Chat created a fresh
  // "New Chat" on every repeat (each POST racing the server's empty-chat
  // sweep against the one before it, so the panel kept snapping to a newer
  // chat instead of opening directly), and dictation/sidebar/model-picker
  // would otherwise toggle back and forth for as long as the key was held.
  if (e.repeat) return

  // Arrow keys and Esc are handled by onUnreservedKeydown, which is bound in
  // both the PWA and the desktop app. Handling them here too made the desktop
  // app run them twice.
  const isDesktop = isDesktopApp()
  const mod = e.metaKey || e.ctrlKey
  const alt = e.altKey

  // New Chat: Cmd+T (Desktop) or Option+N (Web/PWA). Opens a small picker to
  // choose the workspace the new chat should live in; Enter creates it in the
  // active workspace's General project.
  if ((isDesktop && mod && (e.key === 't' || e.key === 'T')) || (!isDesktop && alt && (e.key === 'n' || e.key === 'N'))) {
    e.preventDefault()
    void handleNewChatShortcut()
    return
  }

  // Dictation: Cmd+D (Desktop) or Option+D (Web/PWA).
  if ((isDesktop && mod && (e.key === 'd' || e.key === 'D')) || (!isDesktop && alt && (e.key === 'd' || e.key === 'D'))) {
    if (!store.activeChat) return
    e.preventDefault()
    chatPanelRef.value?.toggleDictation()
    return
  }

  // Archive: Cmd+A (Desktop) or Option+A (Web/PWA). Skip while typing so Cmd+A/Alt+A keeps its
  // select-all meaning inside text fields.
  if ((isDesktop && mod && (e.key === 'a' || e.key === 'A')) || (!isDesktop && alt && (e.key === 'a' || e.key === 'A'))) {
    if (isTypingTarget(e.target) || !store.activeChat) return
    e.preventDefault()
    chatPanelRef.value?.archiveActiveChat()
    return
  }

  // Sidebar: Cmd+S (Desktop) or Option+S (Web/PWA), where Cmd+S is the
  // browser's Save Page. Skipped while typing for the same reason as archive:
  // in a text field Option+S is how you type ß, and stealing it would break
  // text entry for the sake of a view toggle.
  if ((isDesktop && mod && (e.key === 's' || e.key === 'S')) || (!isDesktop && alt && (e.key === 's' || e.key === 'S'))) {
    if (isTypingTarget(e.target)) return
    e.preventDefault()
    sidebarCollapsed.value = !sidebarCollapsed.value
    return
  }

  // Model picker: Cmd+Shift+M (Desktop) or Option+M (Web/PWA). Plain Cmd+M is
  // reserved by macOS for Minimize Window and cannot be intercepted reliably.
  // Not gated on the typing target, like dictation: opening the picker is the
  // useful reading of the key even mid-compose, and the picker is a popover,
  // not a text mutation.
  if ((isDesktop && mod && e.shiftKey && !alt && (e.key === 'm' || e.key === 'M')) || (!isDesktop && alt && (e.key === 'm' || e.key === 'M'))) {
    if (!store.activeChat) return
    e.preventDefault()
    chatPanelRef.value?.toggleModelPicker()
    return
  }

  // Font zoom: Cmd+Shift+= / Cmd+Shift+- in the desktop app, Option+= /
  // Option+- in the PWA — the same split as every other modifier shortcut
  // here, and for the same reason.
  //
  // Cmd+Shift+= cannot be used in a browser: on a US layout that chord *is*
  // Cmd++, the browser's own zoom-in, which is handled above the page and
  // ignores preventDefault. The page zoomed *and* the font grew, two steps at
  // once, while Cmd+Shift+- (not a browser chord) moved one — so the two
  // directions disagreed and browser zoom-in became unusable on its own.
  //
  // Skipped while typing because Option+= / Option+- type ≠ and – on macOS.
  // Step, bounds and persistence come from useFontScale, shared with the
  // Settings +/- buttons.
  const zoomModifier = isDesktop ? (mod && e.shiftKey && !alt) : (alt && !mod)
  if (zoomModifier && !isTypingTarget(e.target)) {
    if (e.key === '=' || e.key === '+') {
      e.preventDefault()
      fontScale.adjust(FONT_SCALE_STEP)
      return
    }
    if (e.key === '-' || e.key === '_') {
      e.preventDefault()
      fontScale.adjust(-FONT_SCALE_STEP)
      return
    }
  }
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
  // Arrow keys and Esc are not browser-reserved, so they bind in the PWA too.
  window.addEventListener('keydown', onUnreservedKeydown)
  window.addEventListener('keydown', onShortcutKeydown)
})

onBeforeUnmount(() => {
  stopLatestStatusSync()
  window.removeEventListener('resize', onResize)
  window.removeEventListener('touchstart', onTouchStart)
  window.removeEventListener('touchend', onTouchEnd)
  window.removeEventListener('touchcancel', onTouchEnd)
  window.removeEventListener('keydown', onUnreservedKeydown)
  window.removeEventListener('keydown', onShortcutKeydown)
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
  /* PaneHeader must respond to the space left after the resizable sidebar,
     not only to the browser viewport width. */
  container-type: inline-size;
  container-name: chat-pane;
}

.empty-shell {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

/* fetchAll() (workspaces/projects/chats) is still in flight here — a bare
   header over an empty flex area used to read as broken rather than loading. */
.home-boot-body {
  flex: 1;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: var(--space-4);
}

/* Skeleton of the home screen while workspaces load: the same vertical
   rhythm as the real thing (status line, tile, lane header, chat rows) so
   the handover from skeleton to content is a fill-in, not a jump. */
.home-boot-skeleton {
  width: 100%;
  max-width: var(--home-max);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  animation: home-boot-enter 220ms ease-out both;
}

.boot-shimmer {
  background: linear-gradient(90deg, var(--bg2) 0%, var(--bg3) 50%, var(--bg2) 100%);
  background-size: 200% 100%;
  animation: home-boot-shimmer 1.4s ease-in-out infinite;
}

.boot-line {
  display: block;
  height: 13px;
  border-radius: var(--radius-pill);
}

.boot-status {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  /* The promised status row sits inside the lane, under its header, in the
     muted register the real row uses (HomeRecentChats.vue). */
  padding: var(--space-2) 0;
}

.boot-status .boot-line {
  margin: 8px 0;
}

.boot-face {
  width: 30px;
  height: 30px;
  flex: 0 0 auto;
  border-radius: 50%;
}

.boot-tile {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius);
}

.boot-lane {
  display: flex;
  flex-direction: column;
}

.boot-lane-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
}

.boot-chip {
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  border-radius: var(--radius-sm);
}

.boot-pill {
  width: 64px;
  height: 28px;
  margin-left: auto;
  flex: 0 0 auto;
  border-radius: var(--radius-sm);
  border: 1px dashed var(--border-strong);
  background: none;
  animation: none;
}

.boot-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
}

.boot-row:last-child {
  border-bottom: none;
}

.boot-row .boot-line {
  flex: 0 1 auto;
}

.boot-meta {
  width: 72px;
  height: 11px;
  flex: 0 0 auto;
  border-radius: var(--radius-pill);
}

@keyframes home-boot-shimmer {
  from { background-position: 200% 0; }
  to { background-position: -200% 0; }
}

@keyframes home-boot-enter {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (prefers-reduced-motion: reduce) {
  .home-boot-skeleton {
    animation: none;
  }
  .boot-shimmer {
    animation: none;
  }
}

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  color: var(--fg2);
  /* PaneHeader already owns the top safe-area inset. Adding it here again
     creates a large empty band above the home status row on mobile. */
  padding: var(--space-4)
           calc(var(--space-4) + var(--safe-right))
           calc(var(--space-4) + var(--safe-bottom))
           calc(var(--space-4) + var(--safe-left));
  text-align: center;
  /* `safe center` centers short content but falls back to start-alignment
     (scrollable) when the full jump-back-in list overflows the viewport. */
  justify-content: safe center;
  overflow-y: auto;
}

.empty-state--active {
  align-items: stretch;
  justify-content: flex-start;
  text-align: left;
}

.empty-home-header {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  max-width: var(--home-max);
  min-height: 44px;
  gap: 10px;
  margin: 0 auto;
}

.empty-state--active .empty-home-header {
  justify-content: flex-start;
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
  /* Soft accent halo — follows the active workspace without a hard stroke. */
  filter:
    drop-shadow(0 0 1.5px color-mix(in srgb, var(--accent) 55%, transparent))
    drop-shadow(1px 0 0 color-mix(in srgb, var(--accent) 40%, transparent))
    drop-shadow(-1px 0 0 color-mix(in srgb, var(--accent) 40%, transparent))
    drop-shadow(0 1px 0 color-mix(in srgb, var(--accent) 40%, transparent))
    drop-shadow(0 -1px 0 color-mix(in srgb, var(--accent) 40%, transparent));
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
  /* Keep the genuine empty state aligned with the lane container. */
  width: 100%;
  max-width: 1040px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 8px;
}
.empty-actions .btn-primary {
  width: 100%;
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
