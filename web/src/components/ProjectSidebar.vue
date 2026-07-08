<template>
  <aside class="sidebar" :class="{ collapsed }" v-bind="$attrs">
    <div class="sidebar-header">
      <button
        class="toggle-btn"
        :class="{ 'toggle-btn--collapsed': collapsed }"
        @click="$emit('toggle')"
        :title="collapsed ? 'Open sidebar' : 'Collapse sidebar'"
        :aria-label="collapsed ? 'Open sidebar' : 'Collapse sidebar'"
      >
        <!-- Panel icon: a rectangle with a vertical bar showing the sidebar's
             position. Mirrors based on `collapsed` so it always points at the
             panel it would reveal/hide. -->
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2"
             stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="1" />
          <line x1="9" y1="4" x2="9" y2="20" />
        </svg>
      </button>
      <template v-if="!collapsed">
        <span
          class="brand wordmark wordmark--sm"
          :class="{ 'brand--refreshing': refreshing }"
          @click="onBrandClick"
          :title="refreshing ? 'Refreshing...' : 'Click to reload the latest app build'"
          role="button"
        >{{ refreshing ? 'sync...' : 'ciaobot' }}</span>
        <div class="nav-links">
          <router-link
            to="/"
            class="nav-item"
            :class="{ 'nav-item--active': mode === 'chat' || mode === 'project' }"
            title="Chats"
            aria-label="Chats"
          >
            <!-- Stacked message lines: sharper, more "log-window" than a speech bubble -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
              <rect x="3" y="4" width="18" height="14" />
              <line x1="6" y1="9" x2="14" y2="9" />
              <line x1="6" y1="13" x2="18" y2="13" />
              <polyline points="8 18 8 21 11 18" />
            </svg>
          </router-link>
          <router-link to="/schedules" class="nav-item" active-class="nav-item--active" title="Schedules" aria-label="Schedules" data-tour="nav-schedules">
            <!-- Clock face with hour markers: more diagrammatic than calendar grid -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
              <rect x="3" y="3" width="18" height="18" />
              <line x1="12" y1="3" x2="12" y2="5" />
              <line x1="12" y1="19" x2="12" y2="21" />
              <line x1="3" y1="12" x2="5" y2="12" />
              <line x1="19" y1="12" x2="21" y2="12" />
              <polyline points="12 8 12 12 15 14" />
            </svg>
          </router-link>
          <router-link to="/settings" class="nav-item" active-class="nav-item--active" title="Settings" aria-label="Settings">
            <!-- Sliders / equalizer: more direct than a gear, mono-grid friendly -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
              <line x1="4" y1="7" x2="20" y2="7" />
              <line x1="4" y1="12" x2="20" y2="12" />
              <line x1="4" y1="17" x2="20" y2="17" />
              <rect x="14" y="5" width="4" height="4" fill="currentColor" />
              <rect x="7" y="10" width="4" height="4" fill="currentColor" />
              <rect x="15" y="15" width="4" height="4" fill="currentColor" />
            </svg>
          </router-link>
          <NotificationBell class="sidebar-bell" />
        </div>
      </template>
    </div>

    <template v-if="!collapsed && (mode === 'schedules')">
      <div class="sidebar-section-header">
        <span class="sidebar-section-title">Schedules</span>
        <button class="add-chip" @click="emit('new-schedule')" title="New schedule">+ New</button>
      </div>
      <div class="schedules-list">
        <div v-if="taskStore.schedules.length === 0" class="empty-hint">// no schedules yet</div>

        <template v-if="oneOffSchedules.length">
          <div class="schedule-group schedule-group--once">
            <div class="schedule-group-header">
              <span>One-offs <span class="schedule-group-hint">delete after run</span></span>
              <span class="schedule-group-count">{{ oneOffSchedules.length }}</span>
            </div>
            <div class="schedule-group-items">
              <router-link
                v-for="s in oneOffSchedules"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="schedule-item schedule-item--once"
                :class="{ 'schedule-item--missed': s.missed }"
                active-class="active"
              >
                <span class="schedule-time">{{ s.run_at_date?.slice(5) }} {{ s.daily_time_utc }}</span>
                <span class="schedule-label">{{ s.title || promptTitle(s.prompt) }}</span>
                <span v-if="s.missed" class="missed-dot" title="Expected to run but didn't"></span>
              </router-link>
            </div>
          </div>
        </template>

        <template v-if="userRoutines.length">
          <div class="schedule-group">
            <div class="schedule-group-header">
              <span>Routines <span class="schedule-group-hint">recurring</span></span>
              <span class="schedule-group-count">{{ userRoutines.length }}</span>
            </div>
            <div class="schedule-group-items">
              <router-link
                v-for="s in userRoutines"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="schedule-item"
                :class="{ 'schedule-item--missed': s.missed }"
                active-class="active"
              >
                <span class="schedule-time">{{ s.frequency === 'manual' ? '·' : s.daily_time_utc }}</span>
                <span class="schedule-label">{{ s.title || promptTitle(s.prompt) }}</span>
                <span v-if="s.missed" class="missed-dot" title="Expected to run but didn't"></span>
              </router-link>
            </div>
          </div>
        </template>

        <template v-if="systemAutomations.length">
          <div class="schedule-group schedule-group--system">
            <div class="schedule-group-header">
              <span>System <span class="schedule-group-hint">built-in</span></span>
              <span class="schedule-group-count">{{ systemAutomations.length }}</span>
            </div>
            <div class="schedule-group-items">
              <router-link
                v-for="s in systemAutomations"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="schedule-item"
                :class="{ 'schedule-item--missed': s.missed }"
                active-class="active"
              >
                <span class="schedule-time">{{ s.frequency === 'manual' ? '·' : s.daily_time_utc }}</span>
                <span class="schedule-label">{{ s.title || promptTitle(s.prompt) }}</span>
                <span v-if="s.missed" class="missed-dot" title="Expected to run but didn't"></span>
              </router-link>
            </div>
          </div>
        </template>
      </div>
    </template>

    <template v-if="!collapsed && mode === 'settings'">
      <div class="sidebar-section-header">
        <span class="sidebar-section-title">Settings</span>
      </div>
      <div class="settings-nav-list">
        <router-link
          to="/settings"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings' }"
        >
          Home
        </router-link>
        <router-link
          to="/settings/providers"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/providers' }"
        >
          Providers
        </router-link>
        <router-link
          to="/settings/models"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/models' }"
        >
          Models
        </router-link>
        <router-link
          to="/settings/instructions"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/instructions' }"
        >
          Instructions
        </router-link>
        <router-link
          to="/settings/workspaces"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/workspaces' }"
        >
          Workspaces
        </router-link>
        <router-link
          to="/settings/skills"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/skills' }"
        >
          Agent assets
        </router-link>
      </div>
    </template>

    <template v-if="!collapsed && (!mode || mode === 'chat' || mode === 'project')">
      <!-- Workspace toggle -->
      <div class="workspace-toggle" data-tour="sidebar-workspaces">
        <button
          v-for="workspace in store.workspaceOptions"
          :key="workspace.name"
          :class="{ active: store.activeWorkspace === workspace.name }"
          @click="store.switchWorkspace(workspace.name)"
        >
          {{ workspaceLabel(workspace.name) }}
          <span v-if="store.workspaceIsStreaming(workspace.name)" class="spinner-dot" title="A chat is working" />
          <span v-else-if="store.workspaceUnread(workspace.name) > 0" class="badge">{{ store.workspaceUnread(workspace.name) }}</span>
        </button>
      </div>

      <!-- Scrollable area for chats/projects -->
      <div class="chats-scroll-area">
        <!-- Recent chats (max 5) -->
        <div v-if="store.recentChats.length" class="recent-section">
          <div class="recent-label">Recent</div>
          <div class="recent-items">
            <div
              v-for="chat in store.recentChats"
              :key="'recent-' + chat.chat_id"
              class="recent-item"
              :class="{ active: chat.chat_id === store.activeChatId, remote: chat.local === false }"
              @click="chat.local !== false && selectChat(chat.chat_id)"
              :title="chat.local === false ? 'This chat lives on another instance' : ''"
            >
              <span v-if="store.isChatStreaming(chat.chat_id)" class="spinner-dot" />
              <span v-else-if="store.chatHasBackgroundAgents(chat.chat_id)" class="spinner-dot bg-agents" title="Background agents running" />
              <span
                v-if="chat.title_status === 'pending'"
                class="title-shimmer"
                aria-label="Generating title"
                title="Generating title..."
              />
              <span v-else class="recent-title">{{ chat.title }}</span>
              <span v-if="chat.local === false" class="remote-chip">remote</span>
              <span class="recent-project" v-if="store.projectFor(chat.chat_id)?.name">
                {{ store.projectFor(chat.chat_id)?.name }}
              </span>
              <span v-if="store.chatUnread(chat.chat_id) > 0" class="badge">{{ store.chatUnread(chat.chat_id) }}</span>
            </div>
          </div>
        </div>

        <!-- Project list -->
        <div class="project-list" data-tour="sidebar-projects">
          <div
            v-for="project in store.workspaceProjects"
            :key="project.project_id"
            class="project-group"
          >
            <div
              class="project-header"
              :class="{ 'is-system': project.is_auto }"
              @contextmenu.prevent="toggleProjectMenu($event, project)"
            >
              <span
                class="project-icon"
                @click="toggleProject(project.project_id)"
                :title="expandedProjects.has(project.project_id) ? 'Collapse' : 'Expand'"
              >{{ expandedProjects.has(project.project_id) ? '▾' : '▸' }}</span>
              <span
                class="project-name"
                v-if="editingProject !== project.project_id"
                @click="openProject(project.project_id)"
                title="Open project page"
              >
                {{ project.name }}
                <span v-if="project.is_auto" class="system-chip" title="Auto-managed project">auto</span>
                <span v-if="store.projectIsStreaming(project.project_id)" class="spinner-dot" title="A chat in this project is working" />
                <span v-if="store.projectUnread(project.project_id) > 0" class="badge">{{ store.projectUnread(project.project_id) }}</span>
              </span>
              <input
                v-else
                class="edit-input"
                :value="project.name"
                @keyup.enter="finishEditProject($event, project.project_id)"
                @keyup.escape="editingProject = null"
                @blur="finishEditProject($event, project.project_id)"
                ref="editInput"
                autofocus
              />
              <button
                class="add-chat-btn"
                :class="{ 'add-chat-btn--creating': store.creatingChatProjectIds[project.project_id] }"
                :disabled="store.creatingChatProjectIds[project.project_id]"
                @click.stop="addChat(project.project_id)"
                title="New chat"
              >{{ store.creatingChatProjectIds[project.project_id] ? '...' : '+' }}</button>
            </div>

            <!-- Context menu (suppressed for system projects) - teleported to body -->
            <Teleport to="body">
              <div
                v-if="projectMenu === project.project_id && !project.is_auto"
                class="context-menu-overlay"
                @click.self="projectMenu = null"
              >
                <div
                  class="context-menu"
                  :style="{ top: projectMenuPos.top + 'px', left: projectMenuPos.left + 'px' }"
                  @mouseleave="projectMenu = null"
                >
                  <button @click="startEditProject(project.project_id)">Rename</button>
                  <button
                    v-if="!project.vault_folder"
                    @click="confirmDeleteProject(project.project_id)"
                  >Delete</button>
                </div>
              </div>
            </Teleport>

            <!-- Chats in project -->
            <div v-if="expandedProjects.has(project.project_id)" class="chat-list">
              <div
                v-for="chat in store.projectChats(project.project_id)"
                :key="chat.chat_id"
                class="chat-item"
                :class="{ active: chat.chat_id === store.activeChatId, remote: chat.local === false }"
                @click="chat.local !== false && selectChat(chat.chat_id)"
                @contextmenu.prevent="toggleChatMenu($event, chat.chat_id)"
                :title="chat.local === false ? 'This chat lives on another instance' : ''"
              >
                <span
                  v-if="chat.title_status === 'pending'"
                  class="title-shimmer"
                  aria-label="Generating title"
                  title="Generating title..."
                />
                <span v-else class="chat-title">{{ chat.title }}</span>
                <span v-if="store.isChatStreaming(chat.chat_id)" class="spinner-dot" title="Working" />
                <span v-else-if="store.chatHasBackgroundAgents(chat.chat_id)" class="spinner-dot bg-agents" title="Background agents running" />
                <span v-else-if="chat.retry?.status === 'pending'" class="retry-dot" title="Retry scheduled" />
                <span v-if="chat.local === false" class="remote-chip">remote</span>
                <span v-if="store.chatUnread(chat.chat_id) > 0" class="badge">{{ store.chatUnread(chat.chat_id) }}</span>
                <button
                  class="chat-actions-btn"
                  aria-label="Chat actions"
                  title="Rename, move, archive, delete"
                  @click.stop="toggleChatMenu($event, chat.chat_id)"
                >&middot;&middot;&middot;</button>
              </div>

              <!-- Chat context menu - teleported to body -->
              <Teleport to="body">
                <div
                  v-if="chatMenu && store.projectChats(project.project_id).some(c => c.chat_id === chatMenu)"
                  class="context-menu-overlay"
                  @click.self="closeChatMenus()"
                >
                  <div
                    class="context-menu"
                    :style="{ top: chatMenuPos.top + 'px', left: chatMenuPos.left + 'px' }"
                  >
                    <template v-if="!moveSubmenu">
                      <button @click="startRenameChat(chatMenu!)">Rename</button>
                      <button v-if="moveTargets.length" @click="openMoveSubmenu()">Move to...</button>
                      <button v-if="chatMenuChat?.retry?.status === 'pending'" @click="stopRetry(chatMenu!)">Stop trying</button>
                      <button v-else @click="setRetry(chatMenu!)">Set to retry</button>
                      <button @click="doArchiveChat(chatMenu!)">Archive</button>
                      <button @click="confirmDeleteChat(chatMenu!)">Delete</button>
                    </template>
                    <template v-else>
                      <div class="context-menu-label">Move to project</div>
                      <button
                        v-for="target in moveTargets"
                        :key="target.project_id"
                        @click="doMoveChat(target.project_id)"
                      >{{ target.name }}</button>
                      <button class="context-menu-back" @click="moveSubmenu = false">← Back</button>
                    </template>
                  </div>
                </div>
              </Teleport>
            </div>
          </div>
        </div>
      </div>

      <!-- Add project button + archived-projects entry point -->
      <div class="sidebar-footer">
        <button class="add-project-btn" @click="addProject">+ New Project</button>
        <button
          class="archive-btn"
          @click="openArchive"
          title="Completed projects"
          aria-label="Completed projects"
        >
          <!-- Archive box: lid over a bin, the conventional "archived" glyph -->
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
            <rect x="3" y="4" width="18" height="4" />
            <path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" />
            <line x1="10" y1="12" x2="14" y2="12" />
          </svg>
        </button>
      </div>
    </template>
  </aside>

  <!-- Completed (archived) projects dialog -->
  <div v-if="archiveOpen" class="modal-overlay" @click.self="archiveOpen = false">
    <div class="modal modal--archive">
      <h3>Completed projects</h3>
      <p class="archive-hint">
        Restoring moves the project back to active. Its old chats stay archived.
      </p>
      <div v-if="loadingCompleted" class="archive-empty">Loading...</div>
      <div v-else-if="!completedProjects.length" class="archive-empty">
        // no completed projects in {{ store.activeWorkspace }}
      </div>
      <div v-else class="archive-list">
        <div v-for="cp in completedProjects" :key="cp.stem" class="archive-item">
          <span class="archive-name" :title="cp.context || cp.name">{{ cp.name }}</span>
          <button
            class="btn-small archive-restore"
            :disabled="restoringStem === cp.stem"
            @click="doRestore(cp)"
          >{{ restoringStem === cp.stem ? '...' : 'Restore' }}</button>
        </div>
      </div>
      <div class="modal-actions">
        <button @click="archiveOpen = false">Close</button>
      </div>
    </div>
  </div>

  <!-- Rename chat dialog -->
  <div v-if="renamingChat" class="modal-overlay" @click.self="renamingChat = null">
    <div class="modal">
      <h3>Rename Chat</h3>
      <input v-model="renameValue" @keyup.enter="doRenameChat" autofocus />
      <div class="modal-actions">
        <button @click="renamingChat = null">Cancel</button>
        <button class="btn-primary" @click="doRenameChat">Save</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import NotificationBell from './NotificationBell.vue'

defineProps<{ collapsed: boolean; mode?: 'chat' | 'project' | 'schedules' | 'settings' }>()
const emit = defineEmits<{ toggle: []; 'chat-selected': []; 'new-schedule': [] }>()

const store = useProjectStore()
const taskStore = useTaskStore()
const route = useRoute()
const router = useRouter()

function promptTitle(prompt: string): string {
  const first = prompt.split('\n')[0].trim()
  return first.length > 36 ? first.slice(0, 33) + '...' : first
}

// Schedule list split: one-offs first (sorted by datetime), then recurring.
const oneOffSchedules = computed(() => {
  return taskStore.schedules
    .filter(s => s.frequency === 'once')
    .slice()
    .sort((a, b) => {
      const ka = `${a.run_at_date || ''} ${a.daily_time_utc || ''}`
      const kb = `${b.run_at_date || ''} ${b.daily_time_utc || ''}`
      return ka.localeCompare(kb)
    })
})
const userRoutines = computed(() =>
  taskStore.schedules.filter(s => s.frequency !== 'once' && s.scope !== 'system'),
)
const systemAutomations = computed(() =>
  taskStore.schedules.filter(s => s.frequency !== 'once' && s.scope === 'system'),
)

import type { ChatInfo, ProjectInfo } from '../lib/types'
function openProject(projectId: string) {
  router.push(`/project/${projectId}`)
  emit('chat-selected') // collapse sidebar on mobile
}

const expandedProjects = reactive(new Set<string>())
const projectMenu = ref<string | null>(null)
const chatMenu = ref<string | null>(null)
const chatMenuPos = ref<{ top: number; left: number }>({ top: 0, left: 0 })
const projectMenuPos = ref<{ top: number; left: number }>({ top: 0, left: 0 })
const moveSubmenu = ref(false)
const editingProject = ref<string | null>(null)
const renamingChat = ref<string | null>(null)
const renameValue = ref('')
const refreshing = ref(false)

// Destination projects for "Move to..." — same workspace as the chat,
// excluding the chat's current project. Backend rejects cross-workspace moves.
const chatMenuChat = computed<ChatInfo | null>(() => {
  const cid = chatMenu.value
  if (!cid) return null
  return store.chats.find(c => c.chat_id === cid) || null
})

const moveTargets = computed<ProjectInfo[]>(() => {
  const cid = chatMenu.value
  if (!cid) return []
  const chat = store.chats.find(c => c.chat_id === cid)
  if (!chat) return []
  return store.workspaceProjects
    .filter(p => p.project_id !== chat.project_id)
    .slice()
    .sort((a, b) => {
      // Pin "General" first, then alphabetical.
      if (a.name === 'General') return -1
      if (b.name === 'General') return 1
      return a.name.localeCompare(b.name)
    })
})

function menuPosition(rect: DOMRect, menuHeight = 160): { top: number; left: number } {
  const top = rect.bottom + 4
  const left = Math.max(8, rect.right - 160)
  // If the menu would overflow the viewport bottom, flip it above the trigger
  if (top + menuHeight > window.innerHeight) {
    return { top: Math.max(8, rect.top - menuHeight - 4), left }
  }
  return { top, left }
}

function toggleChatMenu(event: MouseEvent, chatId: string) {
  if (chatMenu.value === chatId) {
    chatMenu.value = null
    return
  }
  const btn = event.currentTarget as HTMLElement
  const rect = btn.getBoundingClientRect()
  chatMenuPos.value = menuPosition(rect)
  chatMenu.value = chatId
}

function toggleProjectMenu(event: MouseEvent, project: ProjectInfo) {
  if (project.is_auto) { projectMenu.value = null; return }
  if (projectMenu.value === project.project_id) {
    projectMenu.value = null
    return
  }
  const el = event.currentTarget as HTMLElement
  const rect = el.getBoundingClientRect()
  projectMenuPos.value = menuPosition(rect, 80)
  projectMenu.value = project.project_id
}

function openMoveSubmenu() {
  moveSubmenu.value = true
}

function closeChatMenus() {
  chatMenu.value = null
  moveSubmenu.value = false
}

// Reset the submenu whenever the active chat menu changes (open, close,
// switch chats), so re-opening always starts at the top-level menu.
watch(chatMenu, () => { moveSubmenu.value = false })

async function doMoveChat(targetProjectId: string) {
  const cid = chatMenu.value
  closeChatMenus()
  if (!cid) return
  try {
    await store.moveChat(cid, targetProjectId)
    expandedProjects.add(targetProjectId)
  } catch (e: any) {
    store.pushErrorToast('Could not move chat', `${e?.message || e}`)
  }
}

async function onBrandClick() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    // Force the service worker to update without unregistering it,
    // so push subscriptions survive across builds.
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations()
      await Promise.all(regs.map(r => r.update()))
    }
    if (typeof caches !== 'undefined') {
      const keys = await caches.keys()
      await Promise.all(keys.map(k => caches.delete(k)))
    }
  } catch (e) {
    console.warn('Hard refresh cleanup failed', e)
  }
  // Bust HTTP cache too via a query string; replace so no back-button stale entry.
  const url = new URL(window.location.href)
  url.searchParams.set('_r', String(Date.now()))
  window.location.replace(url.toString())
}

// Auto-expand all projects when they load (and keep new ones expanded)
watch(() => store.workspaceProjects, (projects) => {
  for (const p of projects) {
    expandedProjects.add(p.project_id)
  }
}, { immediate: true })

function selectChat(chatId: string) {
  store.switchChat(chatId)
  emit('chat-selected')
}

function toggleProject(id: string) {
  if (expandedProjects.has(id)) {
    expandedProjects.delete(id)
  } else {
    expandedProjects.add(id)
  }
}

async function addProject() {
  const name = prompt('Project name:')
  if (!name) return
  const p = await store.createProject(name)
  expandedProjects.add(p.project_id)
}

function workspaceLabel(name: string): string {
  if (!name) return 'Workspace'
  return name
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

// ── Completed (archived) projects ──────────────────────────────────────
type CompletedProject = { stem: string; name: string; context: string; workspace: string }
const archiveOpen = ref(false)
const loadingCompleted = ref(false)
const completedProjects = ref<CompletedProject[]>([])
const restoringStem = ref<string | null>(null)

async function openArchive() {
  archiveOpen.value = true
  loadingCompleted.value = true
  try {
    completedProjects.value = await store.fetchCompletedProjects()
  } catch (e: any) {
    store.pushErrorToast('Could not load completed projects', `${e?.message || e}`)
    archiveOpen.value = false
  } finally {
    loadingCompleted.value = false
  }
}

async function doRestore(cp: CompletedProject) {
  if (restoringStem.value) return
  restoringStem.value = cp.stem
  try {
    const restored = await store.restoreProject(cp.workspace, cp.stem)
    completedProjects.value = completedProjects.value.filter(p => p.stem !== cp.stem)
    if (restored) expandedProjects.add(restored.project_id)
    if (!completedProjects.value.length) archiveOpen.value = false
  } catch (e: any) {
    store.pushErrorToast('Could not restore project', `${e?.message || e}`)
  } finally {
    restoringStem.value = null
  }
}

function startEditProject(id: string) {
  editingProject.value = id
  projectMenu.value = null
}

async function finishEditProject(event: Event, id: string) {
  const input = event.target as HTMLInputElement
  const name = input.value.trim()
  if (name) {
    await store.updateProject(id, { name })
  }
  editingProject.value = null
}

async function confirmDeleteProject(id: string) {
  projectMenu.value = null
  if (confirm('Delete this project and archive all its chats?')) {
    await store.deleteProject(id)
  }
}

async function addChat(projectId: string) {
  expandedProjects.add(projectId)
  await store.createChat(projectId)
}

function startRenameChat(chatId: string) {
  chatMenu.value = null
  const chat = store.chats.find(c => c.chat_id === chatId)
  renameValue.value = chat?.title || ''
  renamingChat.value = chatId
}

async function doRenameChat() {
  if (renamingChat.value && renameValue.value.trim()) {
    await store.renameChat(renamingChat.value, renameValue.value.trim())
  }
  renamingChat.value = null
}

async function doArchiveChat(chatId: string) {
  chatMenu.value = null
  await store.archiveChat(chatId)
}

async function setRetry(chatId: string) {
  chatMenu.value = null
  await store.loadMessages(chatId)
  const msgs = store.messages[chatId] || []
  const lastUser = [...msgs].reverse().find(m => m.role === 'user')
  const text = lastUser?.content?.trim()
  if (!text) {
    alert('Open the chat or type a message first. No user turn found to retry.')
    return
  }
  await store.setChatRetry(chatId, text, lastUser?.images)
}

async function stopRetry(chatId: string) {
  chatMenu.value = null
  await store.stopChatRetry(chatId)
}

async function confirmDeleteChat(chatId: string) {
  chatMenu.value = null
  if (confirm('Delete this chat permanently?')) {
    await store.deleteChat(chatId)
  }
}
</script>

<style scoped>
.sidebar {
  width: 280px;
  min-width: 280px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  /* Project list scrolls internally; keeping overflow off the outer
     sidebar prevents double-scroll and keeps the footer fixed. */
  overflow: hidden;
  transition: width 0.2s var(--ease), min-width 0.2s var(--ease), transform 0.22s var(--ease);
  padding-top: var(--safe-top);
  padding-left: var(--safe-left);
  padding-bottom: var(--safe-bottom);
}

.sidebar.collapsed {
  width: 40px;
  min-width: 40px;
}

.sidebar-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px;
  border-bottom: 1px solid var(--border);
}

.toggle-btn {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  padding: 4px;
  width: 28px;
  min-height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  transition: color 120ms var(--ease), background 120ms var(--ease);
}
.toggle-btn:hover { color: var(--fg); background: var(--bg3); }
.toggle-btn:active { transform: scale(0.94); }
.toggle-btn--collapsed svg { transform: scaleX(-1); }

@media (max-width: 768px) {
  .toggle-btn {
    min-width: var(--touch);
    min-height: var(--touch);
  }
}

.brand {
  /* Inherits .wordmark base from App.vue; override size and add interaction. */
  font-size: calc(16px * var(--font-scale));
  cursor: pointer;
  transition: opacity 120ms var(--ease);
}
.brand::before {
  content: none;
}
.brand:hover { opacity: 0.85; }
.brand:active { opacity: 0.7; }
.brand--refreshing { opacity: 0.6; }

/* Pulsing dot used inline next to project / chat names to signal activity.
   A breathing scale+opacity pulse reads as "alive" at a glance, unlike a
   thin two-tone ring spin which is too subtle at this size to notice. */
.spinner-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-left: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: ciao-pulse 1.1s ease-in-out infinite;
  vertical-align: middle;
  flex-shrink: 0;
}

@keyframes ciao-pulse {
  0%, 100% { transform: scale(0.55); opacity: 0.35; }
  50% { transform: scale(1); opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .spinner-dot { animation-duration: 2.2s; }
}

/* Slower, dimmer variant: background subagents still working after the
   parent turn ended (no turn is streaming, but the chat isn't idle). */
.spinner-dot.bg-agents {
  background: var(--accent2);
  animation-duration: 1.8s;
}

.retry-dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  margin-left: 6px;
  border-radius: 50%;
  border: 1px solid rgba(255, 193, 7, 0.8);
  background: rgba(255, 193, 7, 0.22);
  flex-shrink: 0;
}

/* Scrollable area for chats and projects */
.chats-scroll-area {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  padding: 8px 8px 12px;
}

/* Recent chats section above the project list. */
.recent-section {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  margin-bottom: 10px;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.recent-label {
  display: flex;
  align-items: center;
  padding: 6px 10px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
  color: var(--fg2);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.recent-items {
  display: flex;
  flex-direction: column;
}

.recent-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  cursor: pointer;
  font-size: var(--text-base);
  color: var(--fg2);
  overflow: hidden;
  white-space: nowrap;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
}

.recent-item:last-child {
  border-bottom: none;
}

.recent-item:hover {
  background: var(--bg3);
  color: var(--fg);
}

.recent-item.active {
  background: var(--bg3);
  color: var(--fg);
  border-left: 2px solid var(--accent);
  padding-left: 8px;
}

.recent-item.remote,
.chat-item.remote {
  opacity: 0.5;
  cursor: default;
}

.remote-chip {
  display: inline-flex;
  align-items: center;
  height: 14px;
  padding: 0 5px;
  border-radius: 4px;
  background: var(--bg3);
  color: var(--fg2);
  font-size: calc(9px * var(--font-scale));
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  flex-shrink: 0;
}

.recent-title {
  flex-shrink: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-project {
  font-size: calc(10px * var(--font-scale));
  color: var(--fg3);
  background: var(--bg);
  padding: 1px 5px;
  border-radius: 4px;
  flex-shrink: 0;
  max-width: 80px;
  overflow: hidden;
  text-overflow: ellipsis;
}

@media (max-width: 768px) {
  .recent-item {
    min-height: var(--touch);
    font-size: calc(14px * var(--font-scale));
  }
}

.nav-links {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
}

.nav-item {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  color: var(--fg2);
  text-decoration: none;
  border-radius: 6px;
  transition: background 120ms var(--ease), color 120ms var(--ease);
}

.nav-item:hover,
.nav-item--active {
  color: var(--fg);
  background: var(--bg3);
}

.sidebar-bell :deep(.bell-btn) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  color: var(--fg2);
  background: none;
  border: none;
  border-radius: 6px;
  padding: 0;
  cursor: pointer;
  transition: background 120ms var(--ease), color 120ms var(--ease);
}
.sidebar-bell :deep(.bell-btn) svg {
  width: 18px;
  height: 18px;
}
.sidebar-bell :deep(.bell-btn:hover) {
  color: var(--fg);
  background: var(--bg3);
}
.sidebar-bell :deep(.bell-btn.has-unread) {
  color: var(--accent);
}
.sidebar-bell :deep(.bell-badge) {
  top: -2px;
  right: -2px;
  font-size: calc(10px * var(--font-scale));
  min-width: 14px;
  height: 14px;
}

@media (max-width: 768px) {
  .nav-item {
    width: var(--touch);
    height: var(--touch);
  }
  .sidebar-bell { display: none; }
}

.workspace-toggle {
  display: flex;
  flex-wrap: wrap;
  padding: 8px;
  gap: 4px;
}

.workspace-toggle button {
  flex: 1 1 0;
  min-width: 0;
  min-height: 44px;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg3);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  overflow-wrap: anywhere;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
}

.workspace-toggle button:hover {
  background: var(--bg);
  border-color: var(--accent);
  color: var(--fg);
}

.workspace-toggle button.active {
  border-color: var(--accent);
}

.project-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex-shrink: 0;
}

.project-group {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.project-header {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  font-size: var(--text-sm);
  color: var(--fg2);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  background: var(--bg2);
}

.project-header:hover {
  background: var(--bg3);
}

.project-group:has(.chat-list) .project-header {
  border-bottom: 1px solid var(--border);
}

.project-header.is-system {
  opacity: 0.85;
}
.project-header.is-system .project-name {
  font-weight: 500;
  text-transform: none;
  letter-spacing: 0;
  color: var(--fg2);
}
.project-header.is-system:hover .project-name { color: var(--fg); }

.system-chip {
  display: inline-flex;
  align-items: center;
  height: 14px;
  padding: 0 5px;
  margin-left: 6px;
  border-radius: 4px;
  background: var(--bg3);
  color: var(--fg2);
  font-size: calc(9px * var(--font-scale));
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  vertical-align: middle;
}

.project-icon {
  font-size: calc(10px * var(--font-scale));
  width: 14px;
  cursor: pointer;
  text-align: center;
  user-select: none;
}
.project-icon:hover { color: var(--fg); }

.project-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}
.project-name:hover { color: var(--fg); }

.edit-input {
  flex: 1;
  font-size: var(--text-sm);
  padding: 2px 4px;
  background: var(--bg);
  border: 1px solid var(--accent);
  border-radius: 3px;
  color: var(--fg);
  font-family: var(--font);
}

.add-chat-btn {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: calc(14px * var(--font-scale));
  padding: 0 4px;
  opacity: 0;
  transition: opacity 0.15s;
  min-width: 18px;
  text-align: center;
}

.project-header:hover .add-chat-btn {
  opacity: 1;
}

.add-chat-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.chat-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px 6px 20px;
  cursor: pointer;
  font-size: var(--text-base);
  color: var(--fg2);
  overflow: hidden;
  white-space: nowrap;
  border-bottom: 1px solid var(--border);
}

.chat-item:last-child {
  border-bottom: none;
}

.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 6px;
  margin-left: 6px;
  border-radius: 9px;
  background: var(--accent);
  color: var(--bg);
  font-size: var(--text-xs);
  font-weight: 700;
  line-height: 1;
  text-transform: none;
  letter-spacing: 0;
  vertical-align: middle;
}

.chat-item:hover {
  background: var(--bg3);
  color: var(--fg);
}

.chat-item.active {
  background: var(--bg3);
  color: var(--fg);
  border-left: 2px solid var(--accent);
  padding-left: 18px;
}

.chat-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Shimmer placeholder shown in the sidebar while the server auto-titles
   a brand new chat. The linear-gradient "sweep" is what the eye reads as
   "something is happening", similar to skeleton loaders elsewhere. */
.title-shimmer {
  flex: 1;
  min-width: 0;
  height: 12px;
  border-radius: 4px;
  background: linear-gradient(
    90deg,
    var(--bg2) 0%,
    var(--bg3) 50%,
    var(--bg2) 100%
  );
  background-size: 200% 100%;
  animation: title-shimmer-sweep 1.4s ease-in-out infinite;
}

@keyframes title-shimmer-sweep {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}

@media (prefers-reduced-motion: reduce) {
  .title-shimmer {
    animation: title-shimmer-pulse 1.8s ease-in-out infinite;
  }
  @keyframes title-shimmer-pulse {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
  }
}

/* Three-dot chat actions. Hidden by default on desktop, fade in on row
   hover or when the menu is open. Always visible on touch devices so
   the entry point is discoverable without hover. */
.chat-actions-btn {
  flex-shrink: 0;
  margin-left: 2px;
  padding: 0 6px;
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: calc(14px * var(--font-scale));
  line-height: 1;
  border-radius: 4px;
  opacity: 0;
  transition: opacity 100ms var(--ease), background 100ms var(--ease);
}
.chat-item:hover .chat-actions-btn,
.chat-item.active .chat-actions-btn { opacity: 1; }
.chat-actions-btn:hover {
  color: var(--fg);
  background: var(--bg2);
}
@media (hover: none) {
  .chat-actions-btn { opacity: 0.6; }
}


.sidebar-footer {
  padding: 8px;
  border-top: 1px solid var(--border);
  display: flex;
  gap: 6px;
  align-items: stretch;
}

.add-project-btn {
  flex: 1;
  min-height: 44px;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg3);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
}

.add-project-btn:hover {
  background: var(--bg);
  border-color: var(--accent);
  color: var(--fg);
}

.archive-btn {
  flex-shrink: 0;
  width: 44px;
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg3);
  color: var(--fg2);
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
}
.archive-btn:hover {
  background: var(--bg);
  border-color: var(--accent);
  color: var(--fg);
}

/* Completed-projects dialog */
.modal--archive {
  width: 360px;
  max-width: calc(100vw - 32px);
}
.archive-hint {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--fg2);
  line-height: 1.4;
}
.archive-empty {
  padding: 16px 4px;
  color: var(--fg2);
  font-size: var(--text-sm);
  text-align: center;
}
.archive-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 50vh;
  overflow-y: auto;
}
.archive-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
}
.archive-item:hover { background: var(--bg3); }
.archive-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-sm);
  color: var(--fg);
}
.archive-restore { flex-shrink: 0; }

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.modal {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  width: 320px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.modal h3 {
  font-size: calc(14px * var(--font-scale));
  margin: 0;
}

.modal input {
  width: 100%;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.modal-actions button {
  padding: 6px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-base);
}

@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    top: 0; left: 0; bottom: 0;
    z-index: 50;
    width: 84vw;
    min-width: 0;
    max-width: 320px;
    box-shadow: 4px 0 24px rgba(0, 0, 0, 0.5);
    transform: translateX(0);
  }
  .sidebar.collapsed {
    transform: translateX(-100%);
    box-shadow: none;
    pointer-events: none;
  }
  .sidebar.collapsed .sidebar-header,
  .sidebar.collapsed .project-list,
  .sidebar.collapsed .sidebar-footer {
    visibility: hidden;
  }
  .chat-item, .project-header {
    min-height: var(--touch);
    font-size: calc(14px * var(--font-scale));
  }
  .chat-item { padding: 10px 16px 10px 32px; }
  .add-chat-btn { opacity: 1; min-width: 32px; min-height: 32px; }
}

/* Schedules list in sidebar (schedules mode) */
.sidebar-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px 4px;
}
.sidebar-section-title {
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--fg2);
  font-weight: 600;
}
.add-chip {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--fg);
  font-size: var(--text-xs);
  padding: 3px 8px;
  border-radius: 999px;
  cursor: pointer;
}
.add-chip:hover { background: var(--bg3); }

.schedules-list {
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  padding: 8px 8px 12px;
}

/* Grouped schedule sections */
.schedule-group {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  margin-bottom: 10px;
  flex-shrink: 0;
}
.schedule-group--once {
  border-left: 2px solid var(--accent);
}
.schedule-group--system {
  border-left: 2px solid var(--accent2);
}
.schedule-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--fg2);
  font-weight: 600;
}
.schedule-group-hint {
  font-weight: 400;
  text-transform: none;
  letter-spacing: 0;
  color: var(--fg2);
  opacity: 0.7;
  font-size: calc(10px * var(--font-scale));
  margin-left: 4px;
}
.schedule-group-count {
  font-size: calc(10px * var(--font-scale));
  background: var(--bg3);
  padding: 1px 5px;
  border-radius: 999px;
  color: var(--fg2);
  min-width: 16px;
  text-align: center;
}
.schedule-group-items {
  display: flex;
  flex-direction: column;
}
.schedule-group-items .schedule-item {
  border-radius: 0;
}
.schedule-item--once .schedule-time {
  color: var(--accent, #ff5566);
  font-weight: 600;
}
.schedule-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  text-decoration: none;
  color: var(--fg2);
  font-size: var(--text-base);
  cursor: pointer;
}
.schedule-item:hover { background: var(--bg3); color: var(--fg); }
.schedule-item.active {
  background: var(--bg3);
  color: var(--fg);
  font-weight: 500;
  border-left: 2px solid var(--accent);
  padding-left: 8px;
}
.schedule-item .schedule-time {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: var(--fg2);
  flex-shrink: 0;
  font-size: var(--text-base);
}
.schedule-item .schedule-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.schedule-item--missed .schedule-time { color: var(--warning); }
.schedule-item .missed-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--warning);
  flex-shrink: 0;
}
.empty-hint {
  padding: 12px 16px;
  color: var(--fg2);
  font-size: var(--text-sm);
  text-align: center;
}

/* Settings sub-page navigation */
.settings-nav-list {
  display: flex;
  flex-direction: column;
  padding: 8px;
  gap: 2px;
}
.settings-nav-item {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  text-decoration: none;
  color: var(--fg2);
  font-size: var(--text-base);
  cursor: pointer;
  transition: background 120ms var(--ease), color 120ms var(--ease);
}
.settings-nav-item:hover {
  background: var(--bg);
  color: var(--fg);
}
.settings-nav-item.active {
  background: var(--bg3);
  color: var(--fg);
  border-right: 2px solid var(--accent);
}
</style>

<!-- Non-scoped: teleported context menus live outside this component's DOM -->
<style>
.context-menu-overlay {
  position: fixed;
  inset: 0;
  z-index: 200;
}

.context-menu {
  position: fixed;
  min-width: 150px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  z-index: 201;
  padding: 4px 0;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}

.context-menu button {
  display: block;
  width: 100%;
  text-align: left;
  padding: 6px 16px;
  border: none;
  background: none;
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-base);
}

.context-menu button:hover {
  background: var(--bg3);
}

.context-menu-label {
  padding: 6px 16px 4px;
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

.context-menu-back {
  border-top: 1px solid var(--border) !important;
  margin-top: 4px;
  color: var(--fg2) !important;
  font-size: var(--text-sm) !important;
}
</style>
