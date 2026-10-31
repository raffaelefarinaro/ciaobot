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
    <div ref="chatMainEl" class="chat-main" :class="{ 'chat-split': sideSlotOccupied }">
      <!-- The chat column: everything the route shows. The slot beside it is
           one slot with two possible occupants — a pinned file, and the
           terminal — and whichever wins it, the divider and the remembered
           ratio are the same. -->
      <div
        ref="chatBodyEl"
        class="chat-body"
        :class="{ 'chat-body--split': sideSlotOccupied, 'chat-body--terminal': terminalInTile }"
        :style="chatColumnStyle"
      >
        <!-- Split view when a file is pinned -->
        <template v-if="panelPath">
          <SubagentChatView
            v-if="subagentRoute"
            :key="subagentRoute.agentId"
            :chat-id="subagentRoute.chatId"
            :agent-id="subagentRoute.agentId"
            @open-sidebar="sidebarCollapsed = false"
          />
          <ChatPanel v-else-if="store.activeChat" ref="chatPanelRef" :key="store.activeChat.chat_id" file-pinned @close="closeChat" @open-sidebar="sidebarCollapsed = false" />
          <!-- The route names a chat the catalog has not delivered yet: the
               pane is on its way, so it shows the skeleton rather than
               nothing (#1651). -->
          <ChatPaneSkeleton v-else-if="chatRoutePending" />
          <div v-else-if="!store.bootstrapped" class="empty-shell home-boot" aria-busy="true">
            <PaneHeader page-tag="Home" @open-sidebar="sidebarCollapsed = false" />
            <div class="home-boot-body">
              <!-- Skeleton of the home screen this will become (lane header
                   with the status row inside it, housekeeping tile, chat
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
            <PaneHeader page-tag="Home" @open-sidebar="sidebarCollapsed = false" />
            <div class="empty-state" :class="{ 'empty-state--active': hasHomeActivity }">
              <div class="home-workbench">
                <div class="home-main">
                  <HomeIntake />
                  <template v-if="!homeAlertsInRail">
                    <HomeNoticeReopen />
                    <HousekeepingStrip />
                  </template>
                  <HomeOnboardingResume />
                  <HomeWelcomeCard />
                  <HomeSetupCard />
                  <HomeRecentChats ref="homeRecentRef" @choose-new-chat="chooseNewChat" />
                  <div v-if="showGlobalNewChatActions" class="empty-actions">
                    <button
                      v-for="action in generalWorkspaceActions"
                      :key="action.workspace"
                      class="btn-primary"
                      :data-workspace-color="action.color"
                      :disabled="action.isCreating"
                      @click="chooseNewChat(action.workspace)"
                    >
                      {{ action.isCreating ? 'Creating...' : `+ ${action.label} chat` }}
                    </button>
                  </div>
                </div>
                <div class="home-rail">
                  <template v-if="homeAlertsInRail">
                    <HomeNoticeReopen />
                    <HousekeepingStrip />
                  </template>
                  <HomeReviewSummary />
                </div>
              </div>
            </div>
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
          <TaskBoardView
            v-else-if="viewMode === 'tasks'"
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
          <template v-else-if="store.activeChat">
            <!-- Narrow shared pin: a compact native opener for the chat's
                 server-owned pin, not a forced split pane and never an
                 auto-opened modal. -->
            <div v-if="narrowSharedPin" class="narrow-pin-opener">
              <button
                type="button"
                class="narrow-pin-btn"
                :aria-label="`Open pinned file ${narrowSharedPinBase}`"
                :title="narrowSharedPin"
                @click="openNarrowSharedPin"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a3 3 0 0 0-6 0z"/></svg>
                <span class="narrow-pin-name">{{ narrowSharedPinBase }}</span>
              </button>
              <button
                type="button"
                class="narrow-pin-close btn-icon"
                aria-label="Unpin file"
                :disabled="pinWritePending"
                @click="unpinCurrent"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
            <ChatPanel ref="chatPanelRef" :key="store.activeChat.chat_id" @close="closeChat" @open-sidebar="sidebarCollapsed = false" />
          </template>
          <!-- The route names a chat the catalog has not delivered yet: the pane
               is on its way, so it shows the skeleton rather than nothing
               (#1651). -->
          <ChatPaneSkeleton v-else-if="chatRoutePending" />
          <div v-else-if="!store.bootstrapped" class="empty-shell home-boot" aria-busy="true">
            <PaneHeader page-tag="Home" @open-sidebar="sidebarCollapsed = false" />
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
            <PaneHeader page-tag="Home" @open-sidebar="sidebarCollapsed = false" />
            <div class="empty-state" :class="{ 'empty-state--active': hasHomeActivity }">
              <div class="home-workbench">
                <div class="home-main">
                  <HomeIntake />
                  <template v-if="!homeAlertsInRail">
                    <HomeNoticeReopen />
                    <HousekeepingStrip />
                  </template>
                  <HomeOnboardingResume />
                  <HomeWelcomeCard />
                  <HomeSetupCard />
                  <HomeRecentChats ref="homeRecentRef" @choose-new-chat="chooseNewChat" />
                  <div v-if="showGlobalNewChatActions" class="empty-actions">
                    <button
                      v-for="action in generalWorkspaceActions"
                      :key="action.workspace"
                      class="btn-primary"
                      :data-workspace-color="action.color"
                      :disabled="action.isCreating"
                      @click="chooseNewChat(action.workspace)"
                    >
                      {{ action.isCreating ? 'Creating...' : `+ ${action.label} chat` }}
                    </button>
                  </div>
                </div>
                <div class="home-rail">
                  <template v-if="homeAlertsInRail">
                    <HomeNoticeReopen />
                    <HousekeepingStrip />
                  </template>
                  <HomeReviewSummary />
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>
      <!-- The slot's divider: the same grip whether a pinned file or the
           terminal is in the tile beside it. -->
      <div
        v-if="sideSlotOccupied && !isMobile"
        class="chat-split-resizer"
        role="separator"
        aria-orientation="vertical"
        :class="{ 'is-dragging': isDraggingSplit }"
        @mousedown="startSplitDrag"
      />
      <!-- The slot: the tile the file panel and the terminal share. It takes
           whatever the chat pane and gutter leave, so the tile's inset
           margin never pushes the pair past 100%. -->
      <div
        v-if="sideTileExists"
        class="chat-split-side"
        :class="{
          'chat-split-side--terminal': terminalInTile,
          'chat-split-side--closed': !sideSlotOccupied,
        }"
      >
        <PinnedFilePanel v-if="panelPath" ref="pinnedFilePanelRef" :key="activePinKey" :file-path="panelPath" :close-disabled="pinWritePending" @close="unpinCurrent" />
        <TerminalPane v-if="terminalMounted && terminalInTile" in-tile :chat-id="activePinKey" @open="terminalOpen = $event" />
      </div>
      <!-- The terminal leaves the tile when it cannot have it: a pinned file
           keeps the slot, and the terminal falls back to the drawer over the
           pane. -->
      <TerminalPane v-if="terminalMounted && !terminalInTile" :chat-id="activePinKey" @open="terminalOpen = $event" />
    </div>
    <FileViewerModal v-if="fileViewerMounted" :opener="fileViewerOpener" />
    <ItemPeekHost v-if="itemPeekMounted" />
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import HomeNoticeReopen from './HomeNoticeReopen.vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { terminalPanelWanted } from '../lib/terminalPanel'
import { homeNewChatProjectId, openNewChatPicker, pendingNewChat } from '../lib/newChat'
import { useFileViewerStore } from '../stores/fileViewer'
import { useItemPeekStore } from '../stores/itemPeek'
import { useTaskStore } from '../stores/tasks'
import { useNotificationInboxStore } from '../stores/notificationInbox'
import { useMemoryMapStore } from '../stores/memoryMap'
import ProjectSidebar from './ProjectSidebar.vue'
import type ChatPanelComponent from './ChatPanel.vue'
import type PinnedFilePanelComponent from './PinnedFilePanel.vue'
import { loadChatPanel, loadFileViewerModal, loadPinnedFilePanel, prefetchChatPanelForRoute } from '../lib/chatComponents'
import ChatPaneSkeleton from './ChatPaneSkeleton.vue'
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
const TaskBoardView = defineAsyncComponent(() => import('./TaskBoardView.vue'))
const SettingsView = defineAsyncComponent(() => import('./SettingsView.vue'))
// The chat chunk is the biggest one, and the first chat opened after an
// update is exactly when it has to be fetched: the hashed assets change with
// every build, so nothing is warm. `defineAsyncComponent` renders nothing
// while the loader is in flight, which is how the pane could come up blank
// (#1651). The skeleton is the loading component, and `delay: 0` because
// Vue's default 200 ms wait is what makes the pane look dead on a cold
// chunk — the placeholder has to be on screen from the first frame.
const ChatPanel = defineAsyncComponent({
  loader: loadChatPanel,
  loadingComponent: ChatPaneSkeleton,
  delay: 0,
  timeout: 30000,
})
const PinnedFilePanel = defineAsyncComponent(loadPinnedFilePanel)
// The browser terminal's panel, loaded the first time a terminal is asked
// for. It stays mounted after that: the screen it holds is the session's,
// and a hide must not throw it away. The signal that asks for it is its own
// tiny module, so the app shell does not carry the terminal store.
// The terminal follows the chat key (activePinKey), as the pin does.
const TerminalPane = defineAsyncComponent(() => import('./TerminalPane.vue'))
// The signal is asked for once and stays true for the session, so a shell
// mounted after that has to read it: a watcher alone only sees the change, and
// a terminal opened on another page would leave this one without its pane.
const terminalMounted = ref(terminalPanelWanted.value)
watch(terminalPanelWanted, (wanted) => {
  if (wanted) terminalMounted.value = true
})
const FileViewerModal = defineAsyncComponent(loadFileViewerModal)
// Lazy like the file viewer, and not in lib/chatComponents: that module's loaders
// are the chat chunk's named entry points.
const ItemPeekHost = defineAsyncComponent(() => import('./ItemPeekHost.vue'))
import PaneHeader from './PaneHeader.vue'
import HomeRecentChats from './HomeRecentChats.vue'
import HomeIntake from './HomeIntake.vue'
import HomeReviewSummary from './HomeReviewSummary.vue'
import HousekeepingStrip from './HousekeepingStrip.vue'
import HomeSetupCard from './HomeSetupCard.vue'
import HomeOnboardingResume from './HomeOnboardingResume.vue'
import HomeWelcomeCard from './HomeWelcomeCard.vue'
import { formatDocumentTitle, settingsTabTitle } from '../lib/appTitle'
import { normalizeWorkspaceColor } from '../lib/workspaceColors'
import { pendingConfirm } from '../lib/confirm'
import { pendingPrompt } from '../lib/prompt'
import { FONT_SCALE_STEP, useFontScale } from '../composables/useFontScale'
import { useKeyboardSettings } from '../composables/useKeyboardSettings'
import { useAppearanceSettings } from '../composables/useAppearanceSettings'
import { effectiveBinding, keyboardMatches, type ShortcutId } from '../lib/keyboardShortcuts'

const store = useProjectStore()
const fileViewer = useFileViewerStore()
const fileViewerMounted = ref(fileViewer.isOpen)
const fileViewerOpener = ref<HTMLElement | null>(null)
watch(() => fileViewer.isOpen, (open, wasOpen) => {
  if (open) fileViewerMounted.value = true
  if (open && !wasOpen && document.activeElement instanceof HTMLElement) {
    fileViewerOpener.value = document.activeElement
  }
}, { flush: 'sync' })
// The item peek (a task opened from a link) is mounted once first opened, like
// the file viewer, and its open state gates the chat's own keys the same way.
const itemPeek = useItemPeekStore()
const itemPeekMounted = ref(itemPeek.isOpen)
watch(() => itemPeek.isOpen, (open) => {
  if (open) itemPeekMounted.value = true
})

// Refs into the active ChatPanel, used by the global keyboard shortcuts to
// reach composer-owned actions (archive).
//
// The template declares ChatPanel and HomeRecentChats twice, once under
// `v-if="pinnedFilePath"` (split view) and once under the `v-else` (no pinned
// file). Both copies must carry the ref: only one is ever mounted, so the ref
// holds whichever that is, but tagging only the split-view copy left
// Cmd+Backspace and the home arrow keys silently dead in the far more common
// no-pinned-file layout.
const chatPanelRef = ref<InstanceType<typeof ChatPanelComponent> | null>(null)
const pinnedFilePanelRef = ref<InstanceType<typeof PinnedFilePanelComponent> | null>(null)
// Ref into HomeRecentChats for arrow-key navigation on the home screen.
const homeRecentRef = ref<InstanceType<typeof HomeRecentChats> | null>(null)
const projectSidebarRef = ref<InstanceType<typeof ProjectSidebar> | null>(null)
const schedulePanelRef = ref<any>(null)

// Reactive handle on the global font scale. Used by the Cmd/Ctrl+Shift+= and
// Cmd/Ctrl+Shift+- shortcuts (below); the same composable is consumed by
// Settings → Appearance so the +/- buttons and the shortcuts stay in sync.
const fontScale = useFontScale()
const keyboard = useKeyboardSettings()
void keyboard.load().catch(() => {})
void useAppearanceSettings().load().catch(() => {})

function matchesShortcut(event: KeyboardEvent, id: ShortcutId): boolean {
  return keyboardMatches(event, effectiveBinding(keyboard.settings.value, id))
}

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
// Gutter (--space-2) + the tile's right inset (--space-4): width the docked
// file tile occupies beyond its own box.
const SIDE_TILE_CHROME = 24
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
  
  // The ratio is a share of the pane: the chat column takes that share and
  // the slot beside it takes the rest, whether a pinned file or the terminal
  // is in it.
  const splitContainer = chatMainEl.value
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
  
  // The floors belong to whoever is in the slot: a file keeps the widths it
  // has always had, and the terminal keeps its own — a chat narrower than
  // that is a sliver, and a shell narrower than that is not a shell.
  const minLeft = terminalInTile.value ? TERMINAL_CHAT_MIN_PX : MIN_CHAT_PANE_WIDTH
  const minSide = terminalInTile.value ? TERMINAL_DOCK_MIN_PX : MIN_SIDE_PANE_WIDTH
  const maxLeft = dragContainerWidth - minSide - SIDE_TILE_CHROME
  
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

const taskStore = useTaskStore()
const notificationInbox = useNotificationInboxStore()
const memoryMapStore = useMemoryMapStore()
const route = useRoute()
const router = useRouter()
prefetchChatPanelForRoute(route.path, route.params.chatId as string | undefined)
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
// The route names a chat, but the catalog has not caught up with it yet:
// `activeChat` is still null while the selection and the URL already point at
// the conversation (a reload, a desktop deep link, a chat the poll has not
// delivered yet). Every render branch used to fall through in that window and
// the pane came up completely empty (#1651) — no header, no skeleton, no
// composer, and no `aria-busy` to say it was on its way. The pane skeleton is
// what covers the gap; the real transcript takes over as soon as the entry
// arrives.
//
// "The catalog has not caught up yet" is a statement about a lookup that is
// still running, so the pending state is bound to that lookup and not to the
// absence of a chat. A route that names a chat which does not exist — a dead
// deep link, a chat deleted in another tab, a chat the poll dropped from the
// catalog — has nothing to wait for: the lookup settles, finds nothing, and
// the skeleton must not be the terminal state. Otherwise the pane would say
// "Loading conversation" forever for a conversation that will never arrive,
// and the user is left with a placeholder they cannot dismiss. Once the
// lookup is done, the pane falls back to the home empty state, which is what
// it showed before the skeleton existed.
//
// The ref holds the id of the chat the pane is waiting for, not a flag: two
// lookups can overlap (the boot's deep-link open and a route change while
// that one is still running), and a flag cleared by the older one would drop
// the skeleton while the newer lookup is still in flight.
const chatLookupPendingId = ref<string | null>(null)

// The route names a chat, so the pane waits for it — until the chat is on
// screen (the arrival watcher below ends the wait) or until a lookup for it
// comes back empty (it does not exist, and no amount of waiting changes that).
watch(
  () => (route.params.chatId as string) || '',
  (id) => {
    if (!id) {
      chatLookupPendingId.value = null
      return
    }
    // A chat that is already on screen is not on its way: the wait ends the
    // moment it is there, and a later disappearance is a fact, not a lag.
    if (store.activeChat?.chat_id === id) {
      chatLookupPendingId.value = null
      return
    }
    chatLookupPendingId.value = id
  },
  { immediate: true },
)

// The chat arrived: the wait is over. If it leaves again later (deleted in
// another tab, dropped from the catalog by the poll), the pane falls back to
// the home empty state instead of keeping a placeholder for a conversation
// that is gone.
watch(
  () => store.activeChat?.chat_id ?? null,
  (id) => {
    if (id && id === chatLookupPendingId.value) chatLookupPendingId.value = null
  },
)

// The lookup the pane is waiting for. It either produces the chat — the
// arrival watcher ends the wait and the transcript takes over — or it does
// not, and then the chat does not exist: the wait ends and the pane falls
// back to the home empty state, as it did before the skeleton existed.
async function openChatFromRoute(chatId: string) {
  try {
    await store.openChatFromDeepLink(chatId)
  } finally {
    // The chat arrived: the arrival watcher has already ended the wait, and
    // the lookup is not the one the pane is waiting on any more.
    if (chatLookupPendingId.value === chatId && store.activeChat?.chat_id !== chatId) {
      chatLookupPendingId.value = null
    }
  }
}

const chatRoutePending = computed(() => {
  const id = (route.params.chatId as string) || ''
  if (!id || store.activeChat) return false
  return chatLookupPendingId.value === id
})
const viewMode = computed<'chat' | 'project' | 'schedules' | 'tasks' | 'settings' | 'memory' | 'proposals'>(() => {
  const path = route.path
  if (path.startsWith('/settings')) return 'settings'
  if (path.startsWith('/schedules')) return 'schedules'
  if (path.startsWith('/tasks')) return 'tasks'
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
// schedules and task-board views, does not have to restate the rest of the gate
// and drift from it. Anything that owns the screen — a confirm dialog, the file
// viewer modal — belongs in the base predicate, so a new overlay is declared once.
const viewShortcutsActive = computed(() =>
  viewMode.value !== 'settings'
  && !pendingConfirm.value
  && !pendingPrompt.value
  && !fileViewer.isOpen
  && !itemPeek.isOpen,
)
// The chat-only chords are inert on the full-screen views that own the keyboard
// themselves. `1`–`9` keep switching workspaces from all of them: they are gated
// on `viewShortcutsActive`, which the board does not exclude.
const shortcutsActive = computed(() =>
  viewShortcutsActive.value && viewMode.value !== 'schedules' && viewMode.value !== 'tasks' && viewMode.value !== 'memory' && viewMode.value !== 'proposals',
)
const sidebarCollapsed = ref(false)
const showNewSchedule = ref(false)
const isMobile = ref(window.innerWidth < 768)

/** Mirrors `@container chat-pane (max-width: 940px)` in the styles below: the
 *  same element (.chat-body), the same box (content inline size), the same
 *  threshold. Home's alerts sit in the rail only while the rail is beside the
 *  column; stacked, they stay under the composer instead of below every chat. */
const HOME_RAIL_MIN_PANE_PX = 940
/** The chat column: the route's content, and the box the panes answer to. */
const chatBodyEl = ref<HTMLElement | null>(null)
/** The pane: the chat column and the terminal's dock, side by side. */
const chatMainEl = ref<HTMLElement | null>(null)
const chatPaneWidth = ref<number | null>(null)
const homeAlertsInRail = computed(() => chatPaneWidth.value === null
  ? !isMobile.value
  : chatPaneWidth.value > HOME_RAIL_MIN_PANE_PX)

// The terminal's shape is decided by the room the pane actually has, not by
// the window: a dock takes its own width, and a chat squeezed to a sliver
// beside it is not a chat anyone can read.
//
// 360px is the chat column's floor when the terminal docks. It is the width
// the app's own narrow chat uses (the details sit in a drawer under 940px),
// and it is the same floor the dock has, so a pane that holds one holds the
// other. Below that the terminal takes the drawer, which is always usable.
const TERMINAL_CHAT_MIN_PX = 360
/** The dock's own floor: the tile is never narrower than this. */
const TERMINAL_DOCK_MIN_PX = 360
/** The pane has to hold both columns, and the tile's inset and grip. */
const TERMINAL_DOCK_MIN_PANE_PX =
  TERMINAL_CHAT_MIN_PX + TERMINAL_DOCK_MIN_PX + SIDE_TILE_CHROME
/** The pane's width, measured: the space the columns have to share. */
const chatMainWidth = ref<number | null>(null)
const terminalDockFits = computed(() => chatMainWidth.value !== null
  && chatMainWidth.value >= TERMINAL_DOCK_MIN_PANE_PX)
/** Whether the terminal is on screen. The pane owns that, and says so. */
const terminalOpen = ref(false)
/**
 * The terminal occupies the slot: no file has claimed it, and the pane is
 * wide enough to hold both columns.
 *
 * A hidden terminal keeps the slot — it is the same element either way, and
 * the shell it runs in is still there behind the hide.
 */
const terminalInTile = computed(() => terminalMounted.value
  && !isMobile.value
  && terminalDockFits.value
  && !panelPath.value)
/** The slot is on screen: a pinned file, or an open terminal in the tile. */
const sideSlotOccupied = computed(() => !!panelPath.value
  || (terminalInTile.value && terminalOpen.value))
/** The slot's tile exists whenever it has an occupant to keep, or the
 *  terminal is mounted: the pane stays in it across a hide. */
const sideTileExists = computed(() => !!panelPath.value || terminalMounted.value)
/** The chat column's width: the shared ratio while the slot is on screen. */
const chatColumnStyle = computed(() => {
  if (!sideSlotOccupied.value) return {}
  return {
    width: isMobile.value ? '100%' : (chatSplitRatio.value * 100) + '%',
    // Shrinkable, so the slot's floor always wins over the ratio: the chat
    // gives up the room the tile needs rather than the tile overflowing.
    flex: isMobile.value ? undefined : '0 1 auto',
    transition: isDraggingSplit.value ? 'none' : undefined,
  }
})
let chatPaneObserver: ResizeObserver | null = null
let chatMainObserver: ResizeObserver | null = null
let latestStatusSyncTimer: ReturnType<typeof setInterval> | null = null

// Only for the true empty state. Every lane carries its own "+ new" and lanes
// no longer collapse at narrow widths, so on a phone with chats these were a
// second, louder copy of an action already on screen — and a saturated fill
// spent on something non-blocking, which the design system reserves for
// "needs the user".
//
// A memory-insight row counts as activity: a workspace whose only thing
// happening is a memory pass is not empty — post-archive work is not part of
// `activeChatsAll`, but it is still something this surface reports before the
// user starts a new chat.
const hasHomeActivity = computed(
  () => store.activeChatsAll.length > 0 || store.memoryInsightRows.length > 0,
)
const showGlobalNewChatActions = computed(() => !hasHomeActivity.value)

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
  if (viewMode.value === 'tasks') return 'tasks'
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

async function chooseNewChat(workspace = store.activeWorkspace, projectId?: string) {
  const selectedProject = projectId
    || (workspace === store.activeWorkspace ? homeNewChatProjectId.value : '')
    || await openNewChatPicker({ workspace })
  if (!selectedProject) return
  await store.newChatInProject(selectedProject)
}

// Option+N and every visible New action use the same project picker.
async function handleNewChatShortcut() {
  await chooseNewChat()
}
const activePinKey = computed(() => {
  // The pinned pane belongs to the route on screen, never to whatever chat is
  // still selected underneath it. Home (`/` or a bare `/chat`) has no key at
  // all, so a retained chat can never surface its pinned page for even one
  // frame while the route watcher clears the view; Settings, Automations,
  // Tasks, Memory and the project routes have no key either. Coming back to
  // the chat restores that chat's pin, because the pin is looked up under the
  // chat the route names.
  //
  // A chat route shows that chat only while it is the one selected: archiving
  // or deleting it, or a refresh that no longer lists it, clears the selection
  // without a navigation, and the screen falls back to Home. The pin follows
  // what is on screen, so Home never carries a chat's page beside it.
  const chatId = (route.params.chatId as string) || ''
  return chatId && store.activeChat?.chat_id === chatId ? chatId : ''
})
// A file opened from a chat while the split view is available docks beside
// that chat instead of opening the viewer (stores/fileViewer.ts).
watch(
  [activePinKey, isMobile],
  ([key, mobile]) => fileViewer.setDockTarget(mobile ? '' : key),
  { immediate: true },
)
onBeforeUnmount(() => fileViewer.setDockTarget(''))
const pinnedFilePath = computed(() => {
  if (isMobile.value) return ''
  return activePinKey.value ? store.pinnedFileFor(activePinKey.value) || '' : ''
})
// The split panel shows the route's pinned file, but a remote pin change must
// never discard an in-progress edit. `panelPath` tracks `pinnedFilePath` while
// the panel is clean (a remote replace/unpin switches or unmounts it
// immediately), and holds the previous path while the panel is dirty; the
// deferred change is applied the moment the panel goes idle.
const panelPath = ref('')
const deferredPinPath = ref<string | null>(null)
watch(pinnedFilePath, (next) => {
  if (pinnedFilePanelRef.value?.isBusyAuthoring) {
    deferredPinPath.value = next
    return
  }
  panelPath.value = next
  deferredPinPath.value = null
}, { immediate: true })
watch(
  () => pinnedFilePanelRef.value?.isBusyAuthoring,
  (busy) => {
    if (busy || deferredPinPath.value === null) return
    panelPath.value = deferredPinPath.value
    deferredPinPath.value = null
  },
)
// On a narrow device there is no split pane: the chat's shared (server-owned)
// pin is a compact opener within the chat. Only a chat route carries a pin key.
const narrowSharedPin = computed(() => {
  if (!isMobile.value) return ''
  const key = activePinKey.value
  if (!key) return ''
  return store.pinnedFileFor(key) || ''
})
const pinWritePending = computed(() => {
  const key = activePinKey.value
  return !!key && store.isChatPinPending(key)
})
const narrowSharedPinBase = computed(() => {
  const p = narrowSharedPin.value
  const idx = p.lastIndexOf('/')
  return idx === -1 ? p : p.slice(idx + 1)
})
function openNarrowSharedPin(): void {
  const key = activePinKey.value
  const path = narrowSharedPin.value
  if (!key || !path) return
  void fileViewer.openSharedPin(path, key)
}
async function unpinCurrent(): Promise<void> {
  const key = activePinKey.value
  if (!key) return
  await store.unpinFile(key)
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

// Home is a view, not a request to destroy anything: reaching `/` (or a bare
// `/chat`) shows Home alone, with no conversation and no pinned file. This
// reconciles the store with the route that is actually displayed, so the
// sidebar's "chats" nav tab, a direct `router.push('/')`, browser back/forward
// and the history buttons all land in the same place.
//
// Deliberately NOT closeChat(): that is the explicit Close/Escape gesture and
// it owns the empty-draft delete policy. Clicking Home is a navigation, so an
// unused New Chat, its typed-but-unsent prompt and its staged screenshots all
// survive it and reappear when that chat is opened again.
//
// Secondary destinations (settings, automations, tasks, memory, a project) keep
// the selection underneath them, as they always have - so a chat the user left
// for Automations is still there when they come back to it. Only the bare Home
// route clears.
function isHomeRoute(path: string): boolean {
  return path === '/' || path === '/chat'
}

function clearRetainedChatView() {
  if (store.activeChatId) store.clearActiveChatView()
}

onMounted(async () => {
  try {
    await store.fetchAll()
  } catch {
    // The engine is unreachable. Without this, the rejection skipped the
    // whole tail of this hook — including the deep-link lookup — and
    // nothing could ever clear the pending state, so a chat route sat on
    // "Loading conversation" instead of the home-boot skeleton that says
    // the workspaces are still on their way. The store is not bootstrapped
    // in this state, so the home-boot branch is the honest answer.
    chatLookupPendingId.value = null
    // Init on this path too, so the inbox's Retry can re-bootstrap.
    notificationInbox.init()
    return
  }
  notificationInbox.init()
  startLatestStatusSync()
  taskStore.fetchSchedules().catch(() => {})
  // The route is re-read here, after the awaits, never captured before them.
  // A slow `fetchAll()` is long enough for the user to have navigated, and a
  // stale pre-fetch route would clear a chat they opened in the meantime —
  // so a chat route is resolved against where the router actually is now.
  const chatId = (route.params.chatId as string) || ''
  if (isHomeRoute(route.path)) {
    // Home alone: no conversation, no pinned page. The store restores
    // `ciao-active-chat` from localStorage when it is created, before this
    // layout mounts, so a reload of `/` arrives here with that chat selected.
    clearRetainedChatView()
  } else if (chatId && store.activeChatId !== chatId) {
    // The lookup is marked as running for as long as it takes: the pane shows
    // the skeleton while the catalog is still being asked, and falls back to
    // the home empty state once the answer is in and it is not this chat.
    await openChatFromRoute(chatId)
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
//
// The path is watched, not just `params.chatId`: a move from a secondary page
// to `/` leaves chatId undefined on *both* routes, so a chatId-keyed watcher
// never fired and the retained chat resurfaced on Home. Watching the path also
// keeps the explicit `/chat/:chatId` and subagent routes working exactly as
// before.
watch(
  () => route.path,
  (path) => {
    const id = (route.params.chatId as string) || ''
    if (!id) {
      if (isHomeRoute(path)) clearRetainedChatView()
      return
    }
    if (!store.bootstrapped) return
    if (store.activeChatId !== id) {
      // Same as the boot's deep-link open: the skeleton covers the lookup,
      // and a route that names a chat which does not exist ends on the home
      // empty state rather than on a permanent "Loading conversation".
      void openChatFromRoute(id)
    } else if (!store.activeChat) {
      // The selection names a chat the catalog does not have: the poll
      // dropped it, another device pruned it, the boot could not resolve it.
      // Nothing is on screen and no lookup is running, so the pane must not
      // sit on the placeholder for a conversation that does not exist.
      chatLookupPendingId.value = null
    } else void store.markRead(id)
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
// A browser tab has already spent the Cmd chords on itself -- Cmd+T opens a
// new tab, Cmd+S saves the page, Cmd+[ / Cmd+] are back/forward -- so every
// single-key shortcut here binds Option (Alt) instead. Archive deliberately
// moved off Cmd+A (which select-all owns inside text fields) to
// Option+Backspace, which fires everywhere including while typing.
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
// AND again in onShortcutKeydown, so a single arrow press ran onArrow twice
// and focus jumped two cards at a time.
function onUnreservedKeydown(e: KeyboardEvent) {
  // The new-chat picker is an aria-modal dialog: while it is open it owns the
  // keyboard, and nothing here may act on the page behind it. Without this,
  // the section chords below navigated underneath the still-open modal, so
  // cancelling left the user in a different top-level section and choosing a
  // project created the chat from an unexpectedly changed route. The picker
  // claims the keys it uses in the capture phase; this covers every chord it
  // does not.
  if (pendingNewChat.value) return
  // Switch top-level sections (chat → schedules → tasks → memory → settings)
  // with Option+Arrow, because the browser has already spent Cmd+Left/Right on
  // back/forward. Never Tab: that stays the native focus traversal.
  const mod = e.metaKey || e.ctrlKey
  const alt = e.altKey
  const isSectionArrow = alt && !mod && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')
  if (isSectionArrow) {
    if (e.repeat || isTypingTarget(e.target) || pendingConfirm.value || pendingPrompt.value || fileViewer.isOpen || itemPeek.isOpen) return
    // Order matches the sidebar's own destination order (Chats, Automations,
    // Tasks, Memory, Settings), so Option+Arrow walks the sections the way the
    // nav lists them. `/tasks` needs its own index: it has no `viewMode` fall
    // through to, so without one Alt+Right from the board jumped to Home.
    const sections = ['/', '/schedules', '/tasks', '/memory', '/settings']
    const current = viewMode.value === 'chat' || viewMode.value === 'project'
      ? 0
      : viewMode.value === 'schedules'
        ? 1
        : viewMode.value === 'tasks'
          ? 2
          : viewMode.value === 'memory' || viewMode.value === 'proposals'
            ? 3
            : 4
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
  const workspaceSlot = Array.from({ length: 9 }, (_, i) => i + 1)
    .find(i => matchesShortcut(e, `workspace${i}` as ShortcutId))
  if (viewShortcutsActive.value && !isTypingTarget(e.target) && !e.defaultPrevented
    && workspaceSlot) {
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
    const workspace = store.workspaceOptions[workspaceSlot - 1]
    if (workspace) {
      e.preventDefault()
      // The schedules, task-board and memory views have no chat to transition
      // into: switching workspace there re-reads the page in place, and
      // navigating to `/` would throw the user off the surface they pressed a
      // number on.
      void store.switchWorkspace(workspace.name, {
        transition: viewMode.value !== 'schedules' && viewMode.value !== 'tasks' && viewMode.value !== 'memory',
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
  if (matchesShortcut(e, 'closeChat')) {
    // The confirm dialog and the file viewer own Esc while they are up.
    if (pendingConfirm.value || pendingPrompt.value || fileViewer.isOpen || itemPeek.isOpen) return
    // Any open Reka layer (dialog, menu, popover) owns Esc: Reka marks each one
    // `data-dismissable-layer` and closes it from its own window listener,
    // which is registered after this one and so runs after it. Without this,
    // Esc in a dialog on Settings or Automations also left the page.
    if (document.querySelector('[data-dismissable-layer]')) return
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
    if (viewMode.value === 'settings' || viewMode.value === 'schedules' || viewMode.value === 'tasks' || viewMode.value === 'memory' || viewMode.value === 'proposals') {
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
    if (homeRecentRef.value?.onArrow?.(e.key)) e.preventDefault()
    return
  }

}

function onShortcutKeydown(e: KeyboardEvent) {
  if (!shortcutsActive.value) return
  // See onUnreservedKeydown: the new-chat dialog owns the keyboard while open.
  if (pendingNewChat.value) return
  // Holding the chord repeats the keydown at OS speed. None of these actions
  // are meant to fire more than once per press: New Chat created a fresh
  // "New Chat" on every repeat (each POST racing the server's empty-chat
  // sweep against the one before it, so the panel kept snapping to a newer
  // chat instead of opening directly), and sidebar/model-picker
  // would otherwise toggle back and forth for as long as the key was held.
  if (e.repeat) return

  // Arrow keys and Esc are handled by onUnreservedKeydown, which is always
  // bound. Handling them here too ran them twice.
  const mod = e.metaKey || e.ctrlKey
  const alt = e.altKey

  // Cmd/Ctrl+Enter sends the composer draft from anywhere in the chat view.
  // The composer's own keydown covers the focused case; this covers the press
  // straight after attaching an image or a comment, when focus sits on the
  // control that just closed rather than the textarea. Text fields keep the
  // chord -- the composer and the comment popovers each handle it themselves
  // while focus is in their textarea -- and shortcutsActive already excludes
  // the file viewer and the dialogs, so nothing sends from behind them.
  //
  // A comment popover's own Save/Cancel buttons are not text fields, so
  // isTypingTarget alone would miss focus sitting there: this would send the
  // unrelated chat draft and swallow the button's own Enter activation.
  // PinnedFilePanel's popover lives outside ChatPanel, so it needs its own
  // check; ChatPanel's own popovers are guarded inside handleSendShortcut.
  if (mod && !alt && e.key === 'Enter') {
    if (isTypingTarget(e.target) || e.defaultPrevented || !store.activeChat) return
    if (pinnedFilePanelRef.value?.isBusyAuthoring) return
    if (chatPanelRef.value?.handleSendShortcut?.()) e.preventDefault()
    return
  }

  // User shortcuts must never take printable chords away from an editor.
  if (isTypingTarget(e.target) || isTypingTarget(document.activeElement)) return

  // New Chat: Option+N. Opens a small picker to choose the workspace the new
  // chat should live in; Enter creates it in the active workspace's General
  // project. Cmd+T is left alone: it is the browser's new tab.
  if (matchesShortcut(e, 'newChat')) {
    if (isTypingTarget(e.target) || isTypingTarget(document.activeElement)) return
    e.preventDefault()
    void handleNewChatShortcut()
    return
  }

  // Archive is deliberately inert in editable controls so Option+Backspace
  // keeps its native “delete previous word” meaning on macOS.
  if (matchesShortcut(e, 'archiveChat')) {
    if (isTypingTarget(e.target) || isTypingTarget(document.activeElement)) return
    if (!store.activeChat) return
    e.preventDefault()
    // The ref is on `<ChatPanel>`, but while the chunk is in flight the
    // loading component is what the ref resolves to, and it has none of
    // ChatPanel's methods. Guarding the ref alone left `archiveActiveChat`
    // an "is not a function" throw in exactly the window the skeleton
    // covers (#1651).
    chatPanelRef.value?.archiveActiveChat?.()
    return
  }

  // Sidebar: Option+S, because Cmd+S is the browser's Save Page. Skipped while
  // typing for the same reason as archive: in a text field Option+S is how you
  // type ß, and stealing it would break text entry for the sake of a view
  // toggle.
  if (matchesShortcut(e, 'toggleSidebar')) {
    if (isTypingTarget(e.target)) return
    e.preventDefault()
    sidebarCollapsed.value = !sidebarCollapsed.value
    return
  }

  // Model picker: Option+M. Not gated on the typing target: opening the picker
  // is the useful reading of the key even mid-compose, and the picker is a
  // popover, not a text mutation.
  if (matchesShortcut(e, 'modelPicker')) {
    if (!store.activeChat) return
    e.preventDefault()
    // Same as the archive chord above: the ref can be the loading
    // component, which has no model picker to open.
    chatPanelRef.value?.toggleModelPicker?.()
    return
  }

  // Font zoom: Option+= / Option+-, the same modifier as every other shortcut
  // here. Cmd+Shift+= cannot be used instead: on a US layout that chord *is*
  // Cmd++, the browser's own zoom-in, which is handled above the page and
  // ignores preventDefault -- the page would zoom *and* the font would grow,
  // two steps at once.
  //
  // Skipped while typing because Option+= / Option+- type ≠ and – on macOS.
  // Step, bounds and persistence come from useFontScale, shared with the
  // Settings +/- buttons.
  if (!isTypingTarget(e.target) && !isTypingTarget(document.activeElement)) {
    const zoomIn = matchesShortcut(e, 'fontIncrease')
    const zoomOut = matchesShortcut(e, 'fontDecrease')
    if (zoomIn) {
      e.preventDefault()
      fontScale.adjust(FONT_SCALE_STEP)
      return
    }
    if (zoomOut) {
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
  if (typeof ResizeObserver !== 'undefined') {
    // Two observers, one per box: the chat column and the pane that holds
    // it. The column's width decides where Home's alerts sit; the pane's
    // decides whether the terminal can dock beside the chat at all.
    if (chatBodyEl.value) {
      chatPaneObserver = new ResizeObserver(entries => {
        const entry = entries[entries.length - 1]
        if (entry) chatPaneWidth.value = entry.contentRect.width
      })
      chatPaneObserver.observe(chatBodyEl.value)
    }
    if (chatMainEl.value) {
      chatMainObserver = new ResizeObserver(entries => {
        const entry = entries[entries.length - 1]
        if (entry) chatMainWidth.value = entry.contentRect.width
      })
      chatMainObserver.observe(chatMainEl.value)
    }
  }
  window.addEventListener('touchstart', onTouchStart, { passive: true })
  window.addEventListener('touchend', onTouchEnd, { passive: true })
  window.addEventListener('touchcancel', onTouchEnd, { passive: true })
  // Arrow keys and Esc are not browser-reserved, so they bind in the PWA too.
  window.addEventListener('keydown', onUnreservedKeydown)
  window.addEventListener('keydown', onShortcutKeydown)
})

onBeforeUnmount(() => {
  chatPaneObserver?.disconnect()
  chatMainObserver?.disconnect()
  stopLatestStatusSync()
  notificationInbox.dispose()
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
  /* Layout containment (it used to come with the container type below) keeps
     this box the containing block for the terminal drawer's absolute inset, so
     the phone drawer still covers the pane it is in. */
  contain: layout style;
}

/* The chat column: the route's content, and the box the panes answer to.
   PaneHeader must respond to the space left after the resizable sidebar and
   the slot beside it, not only to the browser viewport width, so this is the
   element the `chat-pane` container is declared on. */
.chat-body {
  flex: 1 1 0%;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  container-type: inline-size;
  container-name: chat-pane;
}
/* While the slot is on screen the chat column is the split's left half: it
   keeps the shared ratio, and the split's own container queries answer to
   it — the same box the file panel's split used to measure. */
.chat-body--split {
  min-width: 240px;
  container-name: chat-pane chat-split;
}
/* The terminal's slot keeps the chat column at the width the drag holds it
   to, so the two floors are the same number on both sides of the divider. */
.chat-body--terminal {
  min-width: 360px;
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

.boot-tile {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--border);
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
  padding-top: var(--space-2);
  /* The workbench carries the page gutter itself (--page-gutter), so the
     shell only keeps the device safe-area insets. */
  padding-left: var(--safe-left);
  padding-right: var(--safe-right);
  text-align: left;
}

.empty-state > .home-intake {
  order: -1;
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

/* Workbench composition (prototype A): the request column keeps the readable
   measure, and the memory pulse sits in a quiet side rail on wide panes
   instead of stacking another full-width band under the prompt. The rail holds
   Home's alerts (operator notices, update tasks) above At a glance. */
.home-workbench {
  box-sizing: border-box;
  width: 100%;
  max-width: var(--page-max);
  margin: 0 auto;
  padding: 48px var(--page-gutter) 18px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) var(--page-rail);
  align-items: start;
  gap: 48px;
}

/* One column, one gap. Every section root here used to carry its own top
   margin, and the result was three different distances between the composer
   and the next thing depending on which notices were open — 12px next to a
   notice, 42px down to the recent list, and the two summed wherever both
   applied. The gap lives here; the roots below hand theirs back. */
.home-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

/* Section roots give up their *block* margins so the gap is the only thing
   separating them — `margin-block`, not `margin`: these are flex items, and
   inline `auto` margins on a flex item absorb all the free space on the cross
   axis, which re-centres the chip and shrinks the setup card to fit-content.
   Their own inline margins (the three capped shells) stay as they were.
   `:deep()` because these are other components' root elements (and
   HousekeepingStrip's are a fragment, which inherits no scope id). Card,
   heading and row spacing inside each section is untouched. */
.home-main > :deep(.housekeeping),
.home-main > :deep(.update-tasks),
.home-main > :deep(.home-welcome),
.home-main > :deep(.home-setup),
.home-main > :deep(.home-notice-reopen),
.home-main > :deep(.home-recent),
.home-rail > :deep(.housekeeping),
.home-rail > :deep(.update-tasks),
.home-rail > :deep(.home-notice-reopen) {
  margin-block: 0;
}

.home-workbench .home-main > .home-intake,
.home-workbench .home-main > .empty-actions {
  width: 100%;
  max-width: none;
}

/* Not sticky itself: a rail taller than the viewport would hide its own
   bottom (At a glance) until the end of the page. It stretches to the row
   height instead, and only the glance stays pinned. */
.home-rail {
  width: 100%;
  min-width: 0;
  margin: 0;
  align-self: stretch;
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

/* Notices scroll away, the glance stays. */
.home-rail > :deep(.home-review-summary) {
  position: sticky;
  top: var(--space-2);
}

/* Keyed to the pane, not the viewport: with the resizable sidebar open, a
   1280px window leaves the pane well under 980px, and a viewport query kept
   the rail beside a request column too narrow for its own chips. The 940px
   here and HOME_RAIL_MIN_PANE_PX in the script decide together where Home's
   alerts live: keep them in sync. */
@container chat-pane (max-width: 940px) {
  .home-workbench {
    grid-template-columns: minmax(0, 1fr);
    gap: 25px;
    padding: var(--space-5) var(--page-gutter) var(--space-4);
  }

  .home-rail > :deep(.home-review-summary) {
    position: static;
  }
}

@container chat-pane (max-width: 700px) {
  .home-workbench {
    padding-top: var(--space-3);
  }
}

.sidebar-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 40;
  animation: fade-in 160ms var(--ease);
}

@keyframes fade-in { from { opacity: 0 } to { opacity: 1 } }

/* Narrow shared-pin opener: a compact in-chat bar naming the chat's pinned
   file, so a phone gets the same server-owned pin state without a split pane
   or an unsolicited modal. Every control keeps the 44px touch target. */
.narrow-pin-opener {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--page-gutter);
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
}
.narrow-pin-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elev);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  cursor: pointer;
  min-width: 0;
  flex: 1 1 auto;
}
.narrow-pin-btn:hover { background: var(--bg3); }
.narrow-pin-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.narrow-pin-close {
  flex: 0 0 auto;
}

/* Split-screen layout for the right-hand slot. The chat column and the tile
   share it: a pinned file is in the tile, and so is the terminal when no
   file has claimed it. Both panes share width 50/50 by default; min-width is
   a soft floor during drag so a compressed window can still show chat and
   the tile side by side. */
.chat-split {
  flex-direction: row;
}
/* The pinned file is a window docked inside the pane, not a second flat
   column: inset from the pane edges, in the sidebar's tone (--bg2), so the
   sidebar and the file read as one layer and the chat is the canvas between.
   The inset is Chat details' and the composer's 16px, so every docked window
   keeps the same distance from the pane's edge; with the 44px title bar
   (PinnedFilePanel) the bar's rule lands on the chat header's. The terminal
   is the same window with a shell in it. */
.chat-split-side {
  flex: 1 1 0;
  min-width: 240px;
  margin: var(--space-4) var(--space-4) var(--space-4) 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: 0 12px 32px -14px rgb(0 0 0 / 45%);
  animation: chat-tile-in 180ms var(--ease);
}
/* The terminal keeps its own floor in the slot: a shell narrower than that
   is not worth looking at. */
.chat-split-side--terminal {
  min-width: 360px;
}
/* The slot is closed — the terminal is hidden, or a file owns the tile and
   the terminal is the drawer over the pane. The tile keeps the terminal
   mounted behind it, so the shell it runs in survives a hide. */
.chat-split-side--closed {
  display: none;
}
:global(:root.theme-light) .chat-split-side {
  box-shadow: 0 10px 28px -14px rgb(26 26 46 / 28%);
}
@keyframes chat-tile-in {
  from { opacity: 0; transform: translateX(12px) scale(0.985); }
}
@media (prefers-reduced-motion: reduce) {
  .chat-split-side { animation: none; }
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
/* Between the chat and the tile the resizer is the visible gap itself, with a
   grip that shows on hover instead of a full-height accent line. */
.chat-split-resizer {
  flex: none;
  width: var(--space-2);
  margin: 0;
}
.chat-split-resizer::after {
  top: 50%;
  bottom: auto;
  left: 50%;
  width: 3px;
  height: 36px;
  border-radius: 2px;
  transform: translate(-50%, -50%);
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
