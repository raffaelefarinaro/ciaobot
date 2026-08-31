<template>
  <div v-if="!store.bootstrapped && store.projects.length === 0" class="home-recent home-recent--loading" aria-hidden="true">
    <div class="home-lanes">
      <section class="home-lane-skeleton">
        <div class="mm-shimmer-line" style="width: 36%; height: 14px; margin-bottom: 14px;"></div>
        <div class="mm-shimmer-line" style="width: 100%; height: 56px; margin-bottom: 10px;"></div>
        <div class="mm-shimmer-line" style="width: 92%; height: 56px; margin-bottom: 10px;"></div>
        <div class="mm-shimmer-line" style="width: 88%; height: 56px;"></div>
      </section>
    </div>
  </div>
  <div v-else-if="hasHomeActivity" class="home-recent">
    <h2 class="home-recent-label">jump back in</h2>
    <div ref="lanesEl" class="home-lanes">
      <section
        v-for="lane in lanes"
        :key="lane.key"
        :ref="(element) => setLaneRef(lane.key, element as HTMLElement | null)"
        class="home-lane"
        :data-lane-key="lane.key"
      >
        <header class="home-lane-header" :data-workspace-color="lane.color">
          <div class="home-lane-topline">
            <div class="home-lane-heading">
              <!-- With a single workspace the key badge and the name are pure
                   noise: there is nothing to switch to and the workspace is the
                   only one. Keep the status summary and "+ new". -->
              <template v-if="hasMultipleWorkspaces">
                <span class="home-lane-shortcut">{{ lane.shortcut }}</span>
                <span class="home-lane-name">{{ lane.label || 'unassigned' }}</span>
              </template>
              <span class="home-lane-status-text" aria-live="polite">{{ laneStatusText(lane) }}</span>
            </div>
            <div v-if="lane.newAction" class="home-lane-new-split">
              <button
                type="button"
                class="home-lane-new"
                :class="{ 'home-lane-new--split': lane.projects.length > 1, 'home-lane-new--creating': lane.newAction.isCreating }"
                :data-workspace-color="lane.color"
                :disabled="lane.newAction.isCreating"
                :aria-label="`New chat in ${lane.label || 'workspace'}`"
                @click="emit('new-workspace-chat', lane.newAction)"
              ><span v-if="lane.newAction.isCreating" class="home-lane-new-spinner" aria-hidden="true" /><span>{{ lane.newAction.isCreating ? 'Creating…' : '+ new' }}</span></button>
              <!-- Dimmed at rest rather than hover-revealed: the PWA is used on
                   phones, where there is no hover, so a hover-only affordance is
                   simply missing. Hover and focus bring it up to full strength. -->
              <button
                v-if="lane.projects.length > 1"
                type="button"
                class="home-lane-new-caret"
                :data-workspace-color="lane.color"
                :disabled="lane.newAction.isCreating"
                :aria-label="`Choose a project for a new chat in ${lane.label || 'workspace'}`"
                aria-haspopup="menu"
                :aria-expanded="openProjectLane === lane.key"
                @click="toggleProjectMenu(lane)"
                @keydown.down.prevent="openProjectMenu(lane)"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="3" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
                  <polyline points="6 9 12 15 18 9" />
                </svg>
              </button>
              <!-- prevent, not stop: ChatLayout's window-level handler defers to
                   any key a nested popup already consumed, which is the same
                   contract ModelSelector relies on for Esc. Stopping propagation
                   here would make this menu the one popup playing by its own
                   rules. -->
              <div
                v-if="openProjectLane === lane.key"
                class="home-lane-project-menu"
                role="menu"
                @keydown.esc.prevent="closeProjectMenu({ restoreFocus: true })"
                @keydown.down.prevent="moveProjectMenuFocus(1)"
                @keydown.up.prevent="moveProjectMenuFocus(-1)"
              >
                <button
                  v-for="project in lane.projects"
                  :key="project.project_id"
                  type="button"
                  role="menuitem"
                  class="home-lane-project-option"
                  :disabled="Boolean(store.creatingChatProjectIds[project.project_id])"
                  @click="createChatInProject(lane, project.project_id)"
                >{{ project.name }}</button>
              </div>
            </div>
          </div>
        </header>

        <div class="home-lane-body">
          <div v-if="!laneHasChats(lane)" class="home-lane-empty">// no active chats</div>

          <template v-for="entry in tierEntries(lane)" :key="entry.key">
            <div
              v-if="entry.chats.length"
              class="home-tier"
              :class="`home-tier--${entry.key}`"
            >
              <div class="home-tier-label"><span>{{ entry.label }}</span></div>
              <button
                v-for="chat in entry.chats"
                :key="chat.chat_id"
                type="button"
                class="home-chat-item"
                :class="chatItemClasses(entry.key, chat)"
                :data-workspace-color="colorOf(chat)"
                :disabled="chat.local === false"
                :title="chat.local === false ? 'This chat lives on another instance' : chat.title"
                @click="chat.local !== false && store.switchChat(chat.chat_id)"
              >
                <span class="home-chat-heading">
                  <span
                    v-if="chat.title_status === 'pending'"
                    class="title-shimmer"
                    aria-label="Generating title"
                  />
                  <span
                    v-else
                    class="home-chat-title"
                    :class="{ 'home-chat-title--unread': store.chatUnread(chat.chat_id) > 0 }"
                  >{{ chat.title }}</span>
                  <ChatSignals
                    :chat-id="chat.chat_id"
                    :density="entry.key === 'needsYou' || entry.key === 'unread' ? 'card' : 'row'"
                    :hue="colorOf(chat)"
                  />
                  <!-- Project and time ride the title row in every density. On
                       the compact rows they already did (the row is one line);
                       on the cards this reclaims the third line, which is what
                       lets the preview below run to its full two-line clamp. -->
                  <span class="home-chat-meta">
                    <span v-if="store.projectFor(chat.chat_id)?.name" class="home-chat-project">
                      {{ store.projectFor(chat.chat_id)?.name }}
                    </span>
                    <span v-if="chat.local === false" class="remote-chip">remote</span>
                    <span class="home-chat-time">{{ relativeActivity(chat) }}</span>
                  </span>
                </span>
                <span v-if="entry.key === 'needsYou' && store.chatPendingQuestion(chat.chat_id)" class="home-chat-question">
                  {{ store.chatPendingQuestion(chat.chat_id) }}
                </span>
                <!-- Only populated for turns that completed while this tab was
                     open (see `lastResultSnippet` in the store) - a chat that
                     finished earlier shows no preview until its next turn. -->
                <span v-if="entry.key === 'unread' && store.chatLastSnippet(chat.chat_id)" class="home-chat-snippet">
                  {{ store.chatLastSnippet(chat.chat_id) }}
                </span>
              </button>
            </div>

          </template>

          <!-- Chats whose archive POST is in flight. The panel already closed
               optimistically; this queue is where the chat lives while the engine
               finishes the transcript write. -->
          <div v-if="lane.archivingChats.length" class="home-tier home-tier--archiving">
            <div class="home-tier-label"><span>archiving</span></div>
            <div
              v-for="chat in lane.archivingChats"
              :key="`archiving-${chat.chat_id}`"
              class="home-chat-item home-chat-item--archiving"
              :data-workspace-color="colorOf(chat)"
              title="Archiving…"
            >
              <span class="home-chat-heading">
                <span class="home-chat-title">{{ chat.title }}</span>
              </span>
              <span class="home-chat-meta">
                <span class="home-chat-tidy-note">
                  <span class="home-chat-archiving-dot" aria-hidden="true" />
                  archiving…
                </span>
                <span class="home-chat-time">{{ relativeActivity(chat) }}</span>
              </span>
            </div>
          </div>

          <!-- Archived chats the workspace is still tidying up. They are not
               part of the priority tiers (jump back in means active chats), but
               the lane header's "N tidying up" count should have rows behind
               it: opening a row shows the archived transcript, where the same
               pipeline keeps reporting its live step. -->
          <div v-if="lane.tidyChats.length" class="home-tier home-tier--tidying">
            <div class="home-tier-label"><span>tidying up</span></div>
            <button
              v-for="chat in lane.tidyChats"
              :key="`tidy-${chat.chat_id}`"
              type="button"
              class="home-chat-item home-chat-item--tidying"
              :data-workspace-color="colorOf(chat)"
              :disabled="!chat.archive_path"
              :title="chat.archive_path ? 'Open the archived transcript' : chat.title"
              @click="chat.archive_path && fileViewer.open(chat.archive_path)"
            >
              <span class="home-chat-heading">
                <span class="home-chat-title">{{ chat.title }}</span>
              </span>
              <span class="home-chat-meta">
                <span class="home-chat-tidy-note">
                  <span class="home-chat-tidy-dot" aria-hidden="true" />
                  {{ postprocessLabel(store.chatPostprocess(chat.chat_id)) }}…
                </span>
                <span class="home-chat-time">{{ relativeActivity(chat) }}</span>
              </span>
            </button>
          </div>

          <!-- Archived chats whose insights extraction failed. Unlike tidy-up
               (which is background work that never needs the user), a failed
               extraction is a recovery case: the row carries a retry button
               that re-runs it. -->
          <div v-if="lane.failedChats.length" class="home-tier home-tier--failed">
            <div class="home-tier-label"><span>insights failed</span></div>
            <div
              v-for="chat in lane.failedChats"
              :key="`failed-${chat.chat_id}`"
              class="home-chat-item home-chat-item--failed"
              :data-workspace-color="colorOf(chat)"
            >
              <span class="home-chat-heading">
                <span class="home-chat-title">{{ chat.title }}</span>
              </span>
              <span class="home-chat-meta">
                <span class="home-chat-tidy-note home-chat-tidy-note--failed">insights failed</span>
                <button
                  type="button"
                  class="home-chat-retry"
                  :disabled="retryingChats[chat.chat_id]"
                  :aria-label="`Retry extracting insights for ${chat.title}`"
                  @click.stop="retryInsightsFor(chat.chat_id)"
                >{{ retryingChats[chat.chat_id] ? '…' : 'retry' }}</button>
                <span class="home-chat-time">{{ relativeActivity(chat) }}</span>
              </span>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useProjectStore } from '../stores/projects'
import type { ChatInfo, ProjectInfo } from '../lib/types'
import { ageBucket, chatActivityTimestamp, groupHomeTiers, type HomeTierKey, type HomeTiers } from '../lib/homeLanes'
import { postprocessLabel, tidyingSummary } from '../lib/postprocessView'
import { errorMessage } from '../lib/errorMessage'
import { formatRelative } from '../lib/relativeTime'
import { colorForWorkspace, type WorkspaceColorId } from '../lib/workspaceColors'
import { useFileViewerStore } from '../stores/fileViewer'
import ChatSignals from './ChatSignals.vue'

type NewWorkspaceChatAction = { workspace: string; projectId: string; isCreating: boolean }

const emit = defineEmits<{
  'new-workspace-chat': [action: NewWorkspaceChatAction]
}>()

const store = useProjectStore()
const fileViewer = useFileViewerStore()
const hasMultipleWorkspaces = computed(() => store.workspaceOptions.length > 1)
const hasHomeActivity = computed(() => (
  store.activeChatsAll.length > 0
  || store.archivingChatsList().length > 0
  || store.postprocessingChats().length > 0
  || store.insightsFailedChats().length > 0
))
const lanesEl = ref<HTMLElement | null>(null)
const laneElements = ref<Record<string, HTMLElement>>({})
const openProjectLane = ref<string | null>(null)
// Chats whose insights retry is in flight, so the button shows a busy state.
const retryingChats = ref<Record<string, boolean>>({})

async function retryInsightsFor(chatId: string): Promise<void> {
  if (retryingChats.value[chatId]) return
  retryingChats.value[chatId] = true
  try {
    await store.retryInsights(chatId)
  } catch (e) {
    store.pushErrorToast('Could not retry insights', errorMessage(e))
  } finally {
    retryingChats.value[chatId] = false
  }
}

function closeProjectMenuOnOutsideClick(event: MouseEvent): void {
  if (!openProjectLane.value) return
  const target = event.target as Node | null
  const lane = laneElements.value[openProjectLane.value]
  if (target && lane?.contains(target)) return
  closeProjectMenu()
}

onMounted(() => document.addEventListener('click', closeProjectMenuOnOutsideClick))
onBeforeUnmount(() => document.removeEventListener('click', closeProjectMenuOnOutsideClick))

interface HomeLane {
  key: string
  workspace: string | null
  label: string
  shortcut: number | string
  color: WorkspaceColorId
  chats: ChatInfo[]
  newAction: NewWorkspaceChatAction | null
  projects: ProjectInfo[]
  tiers: HomeTiers
  archivingChats: ChatInfo[]
  tidyChats: ChatInfo[]
  failedChats: ChatInfo[]
}

// Grouped once per recompute rather than re-scanning the full archived-chat
// list inside makeLane for every lane — this component re-renders on every
// streaming tick, so that was an O(workspaces × chats) rescan. Partitioning a
// pre-sorted list preserves order, so no re-sort is needed per workspace.
function groupChatsByWorkspace(sortedChats: ChatInfo[]): Map<string, ChatInfo[]> {
  const map = new Map<string, ChatInfo[]>()
  for (const chat of sortedChats) {
    const workspace = store.projectFor(chat.chat_id)?.workspace
    if (!workspace) continue
    const bucket = map.get(workspace)
    if (bucket) bucket.push(chat)
    else map.set(workspace, [chat])
  }
  return map
}

const tidyChatsByWorkspace = computed(() => groupChatsByWorkspace(store.postprocessingChats()))
const failedChatsByWorkspace = computed(() => groupChatsByWorkspace(store.insightsFailedChats()))
const archivingChatsByWorkspace = computed(() => groupChatsByWorkspace(store.archivingChatsList()))

// Home is scoped to the selected workspace: switching workspaces swaps this
// lane's content instead of revealing another column. Chats belonging to other
// registered workspaces stay off the screen entirely — they are one toggle
// press away — while the rescue lanes below keep nothing vanishing until a
// reload heals the registry.
const lanes = computed<HomeLane[]>(() => {
  const grouped = new Map<string, ChatInfo[]>()
  const unknown: ChatInfo[] = []
  for (const chat of store.activeChatsAll) {
    const workspace = store.projectFor(chat.chat_id)?.workspace
    if (!workspace) {
      unknown.push(chat)
      continue
    }
    grouped.set(workspace, [...(grouped.get(workspace) || []), chat])
  }

  const knownNames = new Set(store.workspaceOptions.map(workspace => workspace.name))
  const active = store.workspaceOptions.find(workspace => workspace.name === store.activeWorkspace)
  const result: HomeLane[] = []

  if (active) {
    const chats = grouped.get(active.name) || []
    grouped.delete(active.name)
    knownNames.delete(active.name)
    // The key badge keeps the workspace's position among every registered
    // workspace so it still matches the sidebar toggle's shortcuts.
    const shortcutIndex = store.workspaceOptions.findIndex(w => w.name === active.name) + 1
    result.push(makeLane(
      active.name,
      workspaceLabel(active.name),
      shortcutIndex >= 1 && shortcutIndex <= 9 ? shortcutIndex : '—',
      colorForWorkspace(active),
      chats,
    ))
  }

  // Anything left in `grouped` belongs to a project whose workspace is no
  // longer in workspaceOptions — renaming or deleting a workspace refreshes
  // workspaces.value but leaves projects.value[].workspace on the old name.
  // Registered-but-inactive names are dropped here on purpose: their lanes
  // come back when the user switches to them. Only unregistered names get a
  // rescue lane, because no toggle position can ever reach those chats and,
  // without this sweep, they would vanish from home until a reload.
  for (const [workspace, chats] of grouped) {
    if (!chats.length || knownNames.has(workspace)) continue
    result.push(makeLane(workspace, workspaceLabel(workspace), '—', 'pink', chats))
  }

  if (unknown.length) {
    result.push(makeLane('unknown', '', '—', 'pink', unknown, null))
  }
  return result
})

function makeLane(
  key: string,
  label: string,
  shortcut: number | string,
  color: WorkspaceColorId,
  chats: ChatInfo[],
  workspace: string | null = key,
): HomeLane {
  return {
    key,
    workspace,
    label,
    shortcut,
    color,
    chats,
    // Derived once per lanes recompute rather than per template read. The
    // header asks for both several times (the button, its disabled state, the
    // caret's existence, the menu's v-for), and this component re-renders on
    // every streaming tick, so leaving them as template calls meant a find and
    // a sort per lane per tick for data that only moves when projects do.
    newAction: newActionFor(workspace),
    projects: projectsFor(workspace),
    tiers: groupHomeTiers(
      chats,
      chatId => store.chatNeedsInput(chatId),
      chatId => store.isChatStreaming(chatId) || store.chatHasBackgroundAgents(chatId) || store.chatHasRunningSubagents(chatId),
      chatId => store.chatUnread(chatId) > 0,
    ),
    archivingChats: (workspace && workspace !== 'unknown' && archivingChatsByWorkspace.value.get(workspace)) || [],
    tidyChats: (workspace && workspace !== 'unknown' && tidyChatsByWorkspace.value.get(workspace)) || [],
    failedChats: (workspace && workspace !== 'unknown' && failedChatsByWorkspace.value.get(workspace)) || [],
  }
}

function workspaceLabel(name: string): string {
  return name
    .split(/[-_\s]+/)
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
}

function colorOf(chat: ChatInfo): WorkspaceColorId {
  const workspace = store.projectFor(chat.chat_id)?.workspace
  return colorForWorkspace(store.workspaceOptions.find(item => item.name === workspace))
}

function laneHasChats(lane: HomeLane): boolean {
  return lane.chats.length > 0
}

// Derived from the same chat set as the rows beneath it, so the header can
// never claim a chat needs you with no row to answer.
function laneNeedsCount(lane: HomeLane): number {
  return lane.tiers.needsYou.length
}

function laneWorkingCount(lane: HomeLane): number {
  return lane.tiers.working.length
}

function laneUnreadCount(lane: HomeLane): number {
  return lane.tiers.unread.length
}

function laneQuietCount(lane: HomeLane): number {
  return lane.tiers.quiet.length + lane.tiers.older.length
}

// The header renders the needs count itself so it can carry the hue emphasis;
// this is everything after it. Returning 'all quiet' only when the lane is
// wholly empty keeps the header from printing "3 quiet all quiet".
function laneSummaryRest(lane: HomeLane): string {
  const parts: string[] = []
  if (laneWorkingCount(lane)) parts.push(`${laneWorkingCount(lane)} working`)
  if (laneUnreadCount(lane)) parts.push(`${laneUnreadCount(lane)} unread`)
  if (laneQuietCount(lane)) parts.push(`${laneQuietCount(lane)} quiet`)
  if (!parts.length && !laneNeedsCount(lane)) return 'all quiet'
  return parts.join(' · ')
}

// Chats this workspace is still tidying up after archiving them. The chats
// themselves stay out of the priority tiers — archiving must keep meaning —
// but they are listed in the lane's own "tidying up" tier below quiet, so the
// count always has rows behind it. Counted from the store rather than the
// lane's tiers for exactly that reason: the lane holds active chats only.
function laneTidyCount(lane: HomeLane): number {
  return lane.tidyChats.length
}

function laneTidyLabel(lane: HomeLane): string {
  return tidyingSummary(laneTidyCount(lane))
}

function laneArchivingCount(lane: HomeLane): number {
  return lane.archivingChats.length
}

function laneArchivingLabel(lane: HomeLane): string {
  const n = laneArchivingCount(lane)
  return n === 1 ? '1 archiving' : `${n} archiving`
}

/** Insights-failed count for a lane's workspace, for the header fragment. */
function laneInsightsFailedCount(lane: HomeLane): number {
  return lane.failedChats.length
}

// The glanceable sentence on the workspace line, in plain status grammar.
// Counts come from the same tiers as the rows below, so it can never disagree
// with what is rendered. Tidying/archiving ride along in their muted fragments
// even though they are not part of the lane's active tiers.
function laneStatusText(lane: HomeLane): string {
  // Both actionable tiers, as the glanceable sentence has always counted
  // them: an unread reply wants the user even though it is not blocking the
  // agent on a direct answer, and counting only `needsYou` told a reader with
  // unread replies waiting that "nothing needs your attention". The tiers are
  // mutually exclusive, so adding them cannot double-count a chat.
  const needs = laneNeedsCount(lane) + laneUnreadCount(lane)
  const sentences: string[] = []
  sentences.push(needs
    ? `${needs} chat${needs === 1 ? '' : 's'} need${needs === 1 ? 's' : ''} your attention`
    : 'nothing needs your attention')
  const working = laneWorkingCount(lane)
  sentences.push(working
    ? `${working} agent${working === 1 ? '' : 's'} still working`
    : 'no agents working')
  const tidying = laneTidyCount(lane) + laneArchivingCount(lane) + laneInsightsFailedCount(lane)
  if (tidying) sentences.push(`${tidying} chat${tidying === 1 ? '' : 's'} tidying up`)
  return sentences.join('. ') + '.'
}

function newActionFor(workspace: string | null): NewWorkspaceChatAction | null {
  if (!workspace || workspace === 'unknown') return null
  const projectId = store.projects.find(
    project => project.name === 'General' && project.workspace === workspace,
  )?.project_id || ''
  if (!projectId) return null
  return { workspace, projectId, isCreating: Boolean(store.creatingChatProjectIds[projectId]) }
}

// Projects a new chat can be started in, in the sidebar's own order but with
// General hoisted: it is what plain "+ new" creates in, so it belongs first.
function projectsFor(workspace: string | null): ProjectInfo[] {
  if (!workspace || workspace === 'unknown') return []
  return store.projects
    .filter(project => project.workspace === workspace)
    .sort((a, b) => {
      if (a.name === 'General' && b.name !== 'General') return -1
      if (b.name === 'General' && a.name !== 'General') return 1
      return a.order - b.order || a.name.localeCompare(b.name)
    })
}

function openProjectMenu(lane: HomeLane): void {
  openProjectLane.value = lane.key
  void nextTick(() => {
    projectMenuItems()[0]?.focus()
  })
}

function toggleProjectMenu(lane: HomeLane): void {
  if (openProjectLane.value === lane.key) {
    closeProjectMenu({ restoreFocus: true })
    return
  }
  openProjectMenu(lane)
}

function closeProjectMenu(options: { restoreFocus?: boolean } = {}): void {
  const laneKey = openProjectLane.value
  openProjectLane.value = null
  if (!laneKey || !options.restoreFocus) return
  // Esc and re-clicking the caret must not drop focus to the document body,
  // which would strand a keyboard user at the top of the page.
  void nextTick(() => {
    const lane = laneElements.value[laneKey]
    lane?.querySelector<HTMLElement>('.home-lane-new-caret')?.focus()
  })
}

// Only one menu is ever open, and it lives inside its lane — which laneElements
// already tracks for arrow-key roaming and the outside-click test. A second ref
// registry just for the menu would be a parallel lifecycle to keep in sync.
function projectMenuItems(): HTMLElement[] {
  const laneKey = openProjectLane.value
  const lane = laneKey ? laneElements.value[laneKey] : null
  if (!lane) return []
  return Array.from(
    lane.querySelectorAll<HTMLElement>('.home-lane-project-option:not([disabled])'),
  )
}

function moveProjectMenuFocus(step: number): void {
  const items = projectMenuItems()
  if (!items.length) return
  const current = items.indexOf(document.activeElement as HTMLElement)
  const next = (current + step + items.length) % items.length
  items[next]?.focus()
}

function createChatInProject(lane: HomeLane, projectId: string): void {
  if (!lane.workspace) return
  // restoreFocus for the same reason Esc does it: the menu item being clicked
  // is the focused element, so unmounting it drops focus on <body>. Creating a
  // chat usually navigates away, but it can fail or be slow, and a keyboard
  // user must not be left at the top of the document either way.
  closeProjectMenu({ restoreFocus: true })
  emit('new-workspace-chat', {
    workspace: lane.workspace,
    projectId,
    isCreating: Boolean(store.creatingChatProjectIds[projectId]),
  })
}

function tierEntries(lane: HomeLane): Array<{ key: HomeTierKey; label: string; chats: ChatInfo[] }> {
  return [
    { key: 'needsYou', label: 'needs you', chats: lane.tiers.needsYou },
    // A finished-but-unread chat has something to read, which needs as much
    // attention right now as a still-running chat needs none - so it outranks
    // working. It sits ahead of quiet for the same reason it was pulled out
    // of quiet in the first place: calling it quiet contradicted the unread
    // badge the sidebar showed for the very same chat.
    { key: 'unread', label: 'unread', chats: lane.tiers.unread },
    { key: 'working', label: 'working', chats: lane.tiers.working },
    // Older chats are listed inline with quiet rather than split behind a
    // disclosure. The age opacity ramp still dims them, so "old" stays legible
    // without a separate section and a count the user has to expand to read.
    { key: 'quiet', label: 'quiet', chats: [...lane.tiers.quiet, ...lane.tiers.older] },
  ]
}

function relativeActivity(chat: ChatInfo): string {
  return formatRelative(chatActivityTimestamp(chat))
}

function chatItemClasses(tier: HomeTierKey, chat: ChatInfo): string[] {
  // Age only dims the tiers where age is the whole story. An unread chat is
  // surfaced precisely because it wants reading, and one that has been waiting a
  // week is the most likely to be missed - fading it to 55% made the row look
  // disabled and undid the reason it was pulled out of quiet. Same for anything
  // needing you or working, which are about now rather than when.
  const dimByAge = tier === 'quiet' || tier === 'older'
  const age = dimByAge ? ageBucket(chatActivityTimestamp(chat)) : 'fresh'
  return [
    `home-chat-item--${tier}`,
    age === 'week' ? 'home-chat-item--week-old' : '',
    age === 'older' ? 'home-chat-item--old' : '',
    chat.local === false ? 'remote' : '',
  ].filter(Boolean)
}

function setLaneRef(key: string, element: HTMLElement | null) {
  if (element) laneElements.value[key] = element
  else delete laneElements.value[key]
}

function focusableLanes(): HTMLElement[][] {
  const container = lanesEl.value
  if (!container) return []
  // Every lane renders in full at every width now, so there is one selector.
  return Array.from(container.querySelectorAll<HTMLElement>('.home-lane')).map(lane =>
    Array.from(lane.querySelectorAll<HTMLElement>('.home-chat-item:not([disabled])')),
  )
}

function focusElement(element: HTMLElement) {
  element.focus()
  element.scrollIntoView({ block: 'nearest' })
}

// ChatLayout owns the global keydown. The model is intentionally 2-D, but its
// axes follow the rendered lane layout: the selected workspace's lane first,
// then any rescue lanes (stale or unknown workspaces) stacked beneath it. When
// lanes are stacked vertically moves between them and horizontally moves within
// one; side by side it is the reverse. With a single lane every arrow stays in
// that lane or is consumed at its edge.
function onArrow(key: string): boolean {
  const model = focusableLanes()
  const available = model.some(lane => lane.length)
  if (!available) return false

  let laneIndex = model.findIndex(lane => lane.includes(document.activeElement as HTMLElement))
  let itemIndex = laneIndex >= 0 ? model[laneIndex].indexOf(document.activeElement as HTMLElement) : -1
  if (laneIndex < 0) {
    // Focus is somewhere the grid does not model: a lane header control ("+ new"
    // or the caret), the sidebar, or the body after a click on empty space or a
    // return from a chat. Anchor to the lane the user is actually looking at —
    // the lane holding that focus, or the active workspace's lane — instead of
    // jumping to whichever lane comes first in DOM order, which read as the
    // arrow landing on a card in a random workspace.
    laneIndex = anchorLaneIndex(model)
    itemIndex = -1
  }

  const lanesAreStacked = laneAxis() === 'vertical'
  const laneKey = lanesAreStacked
    ? key === 'ArrowUp' || key === 'ArrowDown'
    : key === 'ArrowLeft' || key === 'ArrowRight'
  const itemKey = lanesAreStacked
    ? key === 'ArrowLeft' || key === 'ArrowRight'
    : key === 'ArrowUp' || key === 'ArrowDown'

  if (itemKey) {
    const delta = key === 'ArrowRight' || key === 'ArrowDown' ? 1 : -1
    const nextIndex = itemIndex < 0 ? 0 : clamp(itemIndex + delta, 0, model[laneIndex].length - 1)
    if (nextIndex === itemIndex && itemIndex >= 0) return true
    focusElement(model[laneIndex][nextIndex])
    return true
  }

  if (!laneKey) return false
  const delta = key === 'ArrowRight' || key === 'ArrowDown' ? 1 : -1
  if (itemIndex < 0) {
    focusElement(model[laneIndex][0])
    return true
  }
  let nextLane = laneIndex + delta
  while (nextLane >= 0 && nextLane < model.length && !model[nextLane].length) nextLane += delta
  if (nextLane < 0 || nextLane >= model.length) return true
  const nextIndex = clamp(itemIndex, 0, model[nextLane].length - 1)
  focusElement(model[nextLane][nextIndex])
  return true
}

// The grid is a single column now, but rescue lanes (stale or unknown
// workspaces) still stack beneath the selected workspace's lane, so the axis
// question survives: read the first rendered lane transition instead of
// assuming. In jsdom the boxes have no geometry, so the historical
// side-by-side mapping remains the safe fallback for unit tests and
// non-layout environments.
function laneAxis(): 'horizontal' | 'vertical' {
  const lanes = Array.from(lanesEl.value?.querySelectorAll<HTMLElement>('.home-lane') ?? [])
  for (let index = 1; index < lanes.length; index += 1) {
    const previous = lanes[index - 1].getBoundingClientRect()
    const current = lanes[index].getBoundingClientRect()
    const horizontalDistance = Math.abs(current.left - previous.left)
    const verticalDistance = Math.abs(current.top - previous.top)
    if (horizontalDistance <= 1 && verticalDistance <= 1) continue
    return verticalDistance > horizontalDistance ? 'vertical' : 'horizontal'
  }
  return 'horizontal'
}

function clamp(value: number, lower: number, upper: number): number {
  return Math.min(upper, Math.max(lower, value))
}

// Where arrows start when nothing in the chat grid has focus. Prefer the lane
// the focused element already lives in (a "+ new" / caret control), then the
// active workspace's lane (the first one on screen), then any lane with cards.
// The model's lane order matches the DOM `.home-lane`
// order because focusableLanes() builds from the same query.
function anchorLaneIndex(model: HTMLElement[][]): number {
  const lanes = () => Array.from(lanesEl.value?.querySelectorAll<HTMLElement>('.home-lane') ?? [])
  const host = (document.activeElement as HTMLElement | null)?.closest?.('.home-lane') as HTMLElement | null
  if (host) {
    const index = lanes().indexOf(host)
    if (index >= 0 && model[index]?.length) return index
  }
  const activeIndex = lanes().findIndex(lane => lane.dataset.laneKey === store.activeWorkspace)
  if (activeIndex >= 0 && model[activeIndex]?.length) return activeIndex
  return model.findIndex(lane => lane.length > 0)
}

defineExpose({ onArrow })

</script>

<style scoped>
.home-recent {
  width: 100%;
  max-width: var(--home-max);
  margin: 0 auto;
  text-align: left;
  /* Stacking is decided by the width the lanes actually get, not the window's.
     The sidebar is resizable and takes a large share, so a viewport media query
     stacked far too late: at a 1750px window the lanes still had only ~450px
     each and ellipsed almost every title. */
  container-type: inline-size;
}

/* Off the screen, still in the document. The lane headings and tier headings
   below already say what this list is, so the label was a caption for something
   self-evident - but it is the only heading naming this region, so assistive
   tech keeps it. Same rule as .sr-only in App.vue. */
.home-recent-label {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}

.home-lanes {
  display: grid;
  /* One lane at a time: the grid holds the selected workspace only, so a
     single full-width column beats two half columns. Deliberately no own
     max-width: .home-recent is the same --home-max shell the status line
     above centers in, and a second cap here re-centered the lane inside it —
     the lane floated mid-screen, unaligned with the greeting and leaving the
     shell's width unused. */
  grid-template-columns: minmax(0, 1fr);
  align-items: start;
  gap: 20px;
}

.home-lane {
  min-width: 0;
  /* Vertical rhythm only: the lane background is transparent, and horizontal
     padding here was the last few pixels offsetting the lane's left edge from
     the status row above it. The shell padding already keeps focus rings
     clear of the scroll edges. */
   padding: 0 0 var(--space-2);
  border-radius: var(--radius-sm);
  background: transparent;
}

/* The header is a single row now: the workspace line with its summary counts
   and the "+ new" split. The accent border stays on the whole header, so the
   divider lands below the line (above the tier labels). */
.home-lane-header {
  display: flex;
  flex-direction: row;
  align-items: center;
  min-width: 0;
  min-height: 44px;
  padding: 0 0 8px;
  border-bottom: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
}

/* Row one of the header: the workspace line and the "+ new" split share it,
   pushed to the two ends. The split must stay a compact control here — it
   was previously caught by this row's own justify-content, which flung
   "+ new" and the caret to opposite edges of the lane and centered the
   workspace name in the leftover space. */
.home-lane-topline {
  display: flex;
  flex: 1 1 auto;
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
  width: 100%;
}

.home-lane-heading {
  display: flex;
  flex: 1 1 auto;
  flex-direction: row;
  align-items: baseline;
  justify-content: flex-start;
  gap: 7px;
  min-width: 0;
  overflow: hidden;
}

/* A bordered badge rather than bracketed text: it is a key you can press, and
   the box is what makes that legible. Squared corners, per the design. */
.home-lane-shortcut {
  flex: 0 0 auto;
  min-width: 18px;
  padding: 0 var(--space-1);
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border-strong));
  border-radius: var(--radius-xs);
  color: color-mix(in srgb, var(--accent) 65%, var(--fg3));
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 700;
  text-align: center;
}

.home-lane-name {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  font-weight: 700;
  letter-spacing: 0.04em;
  text-overflow: ellipsis;
  text-transform: uppercase;
  white-space: nowrap;
}

.home-lane-status-text {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--fg2);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Muted, never accent: the count is information, not a call to act. */
.home-lane-tidy-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 4px;
  border-radius: 50%;
  background: var(--fg3);
  vertical-align: middle;
  animation: home-lane-tidy-breathe 2.6s ease-in-out infinite;
}

/* Archiving: same muted register as tidy-up, but with a spinner rather than
   a breathing dot — it is a short in-flight request, not background work. */
.home-lane-archiving-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 4px;
  border-radius: 50%;
  border: 1.5px solid color-mix(in srgb, var(--fg3) 45%, transparent);
  border-top-color: var(--fg3);
  vertical-align: middle;
  animation: home-lane-archiving-spin 0.9s linear infinite;
}

@keyframes home-lane-archiving-spin {
  to { transform: rotate(360deg); }
}

@keyframes home-lane-tidy-breathe {
  0%, 100% { opacity: 0.35; }
  50%      { opacity: 0.9; }
}

@media (prefers-reduced-motion: reduce) {
  .home-lane-tidy-dot,
  .home-chat-tidy-dot,
  .home-lane-archiving-dot,
  .home-chat-archiving-dot { animation: none; opacity: 0.75; }
}

.home-lane-new {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: var(--touch, 44px);
  flex: 0 0 auto;
  padding: 5px 9px;
  border: 1px dashed var(--accent);
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-xs);
  white-space: nowrap;
}

.home-lane-new:hover,
.home-lane-new:focus-visible {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}

.home-lane-new:disabled {
  cursor: wait;
  opacity: 0.65;
}

/* Solid, not dashed — mid-creation this is a status readout, not an
   affordance to invite another click. Ellipsis text alone read as inert
   during the multi-second round trip to create the chat, so the spinner
   carries the "still working" signal. */
.home-lane-new--creating {
  border-style: solid;
}

.home-lane-new-spinner {
  width: 9px;
  height: 9px;
  flex: 0 0 auto;
  border: 2px solid color-mix(in srgb, var(--accent) 28%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: home-lane-new-spin 0.8s linear infinite;
}

@keyframes home-lane-new-spin {
  to { transform: rotate(360deg); }
}

/* Split control: "+ new" keeps its one-click meaning (a chat in General) and
   the caret opens the project picker beside it. They read as one control, so
   the shared edge is flattened and only the outer corners stay rounded. */
.home-lane-new-split {
  position: relative;
  display: flex;
  flex: 0 0 auto;
  align-items: stretch;
  /* Explicitly not the topline's space-between: both buttons hug one end. */
  justify-content: flex-start;
}

.home-lane-new--split {
  border-top-right-radius: 0;
  border-bottom-right-radius: 0;
  border-right-width: 0;
}

.home-lane-new-caret {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: var(--touch, 44px);
  padding-inline: 7px;
  border: 1px dashed var(--accent);
  border-top-left-radius: 0;
  border-bottom-left-radius: 0;
  border-top-right-radius: var(--radius-sm, 6px);
  border-bottom-right-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  /* Quiet at rest so the lane header stays calm, but never hidden: hover does
     not exist on a phone, and an affordance that only appears on hover is one
     that half the users never get. */
  opacity: 0.55;
  transition: opacity 120ms var(--ease), background 120ms var(--ease);
}

.home-lane-new-caret:hover,
.home-lane-new-caret:focus-visible {
  opacity: 1;
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}

.home-lane-new-caret[aria-expanded='true'] {
  opacity: 1;
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}

.home-lane-new-caret:disabled {
  cursor: wait;
  opacity: 0.4;
}

.home-lane-project-menu {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  z-index: 20;
  display: flex;
  min-width: 160px;
  max-width: min(260px, 70vw);
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg2);
  box-shadow: 0 8px 24px rgb(0 0 0 / 35%);
}

.home-lane-project-option {
  min-height: var(--touch, 44px);
  padding: 8px 12px;
  border: none;
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.home-lane-project-option:hover,
.home-lane-project-option:focus-visible {
  background: var(--bg3);
}

.home-lane-project-option:disabled {
  cursor: wait;
  opacity: 0.6;
}

@media (prefers-reduced-motion: reduce) {
  .home-lane-new-caret { transition: none; }
}

.home-lane-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  padding-top: 10px;
}

.home-lane-empty {
  padding: 10px 2px;
  color: var(--fg3);
  font-size: var(--text-sm);
}

.home-tier {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

/* Lowercase deliberately: these are quiet structural markers, not headings.
   They used to inherit `text-transform: uppercase` with a `span:last-child`
   override cancelling it — which only fired when a tier had a second span, so
   "needs you" shouted in caps while "working" and "quiet" did not. */
.home-tier-label {
  display: flex;
  gap: var(--space-2);
  padding-bottom: var(--space-1);
  border-bottom: 1px solid var(--border);
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}

.home-chat-item {
  display: flex;
  width: 100%;
  min-width: 0;
  min-height: var(--touch, 44px);
  flex-direction: column;
  gap: 7px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-left: 2px solid var(--accent);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  text-align: left;
  transition: border-color 120ms var(--ease), background 120ms var(--ease), transform 60ms var(--ease);
}

.home-chat-item:hover {
  border-color: var(--accent);
  background: var(--bg3);
}

.home-chat-item:active {
  transform: translateY(1px);
}

/* Positive offset, so the ring sits clear of the card edge. At -1px it was drawn
   inside the border and merged with the accent rail on the left, which is the
   one place a keyboard user most needs to see where focus is. Kept to a single
   ring - the old two-ring treatment was what made the rows read as boxes inside
   boxes - with a --bg gap so it separates on any surface. */
.home-chat-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  box-shadow: 0 0 0 2px var(--bg);
}

.home-chat-item--needsYou {
  background: color-mix(in srgb, var(--accent) 8%, var(--bg2));
}

/* Working needs zero user action right now - the lane header's "N still
   working" summary already covers in-flight visibility - so it gets the
   compact row treatment: title + a flashing ChatSignals dot + timestamp,
   nothing to preview yet. Unread took over the bigger bordered-card
   treatment below, since it has a finished answer worth the extra room. */
.home-chat-item--working,
.home-chat-item--quiet,
.home-chat-item--older,
.home-chat-item--archiving,
.home-chat-item--tidying,
.home-chat-item--failed {
  flex-direction: row;
  align-items: center;
  gap: 8px;
  min-height: var(--touch, 44px);
  padding: 7px 10px;
  border: 0;
  border-left: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  /* Square against its hue rail, rounded away from it. */
  border-radius: 0 var(--radius-xs) var(--radius-xs) 0;
  background: transparent;
}

.home-chat-item--working:hover,
.home-chat-item--quiet:hover,
.home-chat-item--older:hover,
.home-chat-item--archiving:hover,
.home-chat-item--tidying:hover,
.home-chat-item--failed:hover {
  background: color-mix(in srgb, var(--accent) 7%, transparent);
}

.home-chat-item--week-old { opacity: 0.72; }
.home-chat-item--old { opacity: 0.55; }

.home-chat-heading {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  min-width: 0;
}

.home-chat-item--working .home-chat-heading,
.home-chat-item--quiet .home-chat-heading,
.home-chat-item--older .home-chat-heading,
.home-chat-item--archiving .home-chat-heading,
.home-chat-item--tidying .home-chat-heading,
.home-chat-item--failed .home-chat-heading {
  flex: 1;
  align-items: center;
}

.home-chat-title {
  display: -webkit-box;
  min-width: 0;
  flex: 1;
  overflow: hidden;
  color: var(--fg2);
  font-size: var(--text-sm);
  font-weight: 400;
  line-height: 1.35;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.home-chat-title--unread {
  color: var(--fg);
  font-weight: 600;
}

.home-chat-item--working .home-chat-title,
.home-chat-item--quiet .home-chat-title,
.home-chat-item--older .home-chat-title,
.home-chat-item--archiving .home-chat-title,
.home-chat-item--tidying .home-chat-title,
.home-chat-item--failed .home-chat-title {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-chat-item--needsYou .home-chat-title {
  color: var(--fg);
  font-weight: 600;
}

.home-chat-item .home-chat-title--unread {
  color: var(--fg);
  font-weight: 600;
}

.home-chat-question {
  display: -webkit-box;
  overflow: hidden;
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

/* Same clamp treatment as the pending-question preview, but muted: a finished
   answer to read is lower urgency than a question blocking the agent. */
.home-chat-snippet {
  display: -webkit-box;
  overflow: hidden;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

/* Keep project labels and timestamps in stable columns across every row. The
   metadata used to shrink to each row's contents, making both columns jump
   horizontally as project names and relative times changed. */
.home-chat-meta {
  display: flex;
  align-items: baseline;
  flex: 0 0 34%;
  gap: 7px;
  min-width: 0;
  max-width: 34%;
  margin-left: auto;
  color: var(--fg2);
  font-size: var(--text-xs);
  /* The card heading top-aligns its children, so this smaller text needs the
     title's first-line height (1.35 x text-sm) to share its centerline — and
     the unread dot's (see .chat-signals--card in ChatSignals.vue). */
  line-height: calc(1.35 * var(--text-sm));
}

.home-chat-project {
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  color: var(--fg2);
  letter-spacing: 0.03em;
  text-overflow: ellipsis;
  text-transform: uppercase;
  white-space: nowrap;
}

.home-chat-time {
  flex: 0 0 7ch;
  width: 7ch;
  margin-left: auto;
  color: var(--fg3);
  font-family: var(--font-mono);
  text-align: right;
  white-space: nowrap;
}

/* Live step of a post-archive pipeline ("extracting insights…"). Muted like
   the lane header's tidy fragment: this is background work, never a demand. */
.home-chat-tidy-note {
  min-width: 0;
  overflow: hidden;
  color: var(--fg3);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-chat-tidy-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 4px;
  border-radius: 50%;
  background: var(--fg3);
  vertical-align: middle;
  animation: home-lane-tidy-breathe 2.6s ease-in-out infinite;
}

.home-chat-archiving-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 4px;
  border-radius: 50%;
  border: 1.5px solid color-mix(in srgb, var(--fg3) 45%, transparent);
  border-top-color: var(--fg3);
  vertical-align: middle;
  animation: home-lane-archiving-spin 0.9s linear infinite;
}

/* A failed extraction is a recovery case, so its note carries the warn colour
   rather than the muted tidy grey. */
.home-chat-tidy-note--failed {
  color: var(--warning);
}

/* The retry button on a failed-insights row. Small bordered control in the
   warn register: it is an action, but a secondary one for a routine recovery,
   not the single most important thing on screen. */
.home-chat-retry {
  flex: 0 0 auto;
  min-height: 24px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--warning);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-xs);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.home-chat-retry:hover,
.home-chat-retry:focus-visible {
  background: color-mix(in srgb, var(--warning) 10%, transparent);
  border-color: var(--warning);
}

.home-chat-retry:disabled {
  cursor: wait;
  opacity: 0.6;
}

.remote-chip {
  flex: 0 0 auto;
  padding: 1px 4px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  color: var(--fg2);
  font-size: var(--text-xs);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.title-shimmer {
  width: 45%;
  height: 14px;
  flex: 1;
  border-radius: 4px;
  background: linear-gradient(90deg, var(--border) 25%, var(--bg3) 50%, var(--border) 75%);
  background-size: 200% 100%;
  animation: home-shimmer 1.2s ease-in-out infinite;
}

.mm-shimmer-line {
  display: block;
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--bg2) 0%, var(--bg3) 50%, var(--bg2) 100%);
  background-size: 200% 100%;
  animation: home-shimmer 1.2s ease-in-out infinite;
}

@keyframes home-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

@container (max-width: 760px) {
  /* The lane is already one column; this only widens the gap between the
     selected workspace's lane and any rescue lane beneath it on a phone. */
  .home-lanes {
    gap: var(--space-5);
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-chat-item { transition: none; }
  .title-shimmer { animation: none; }
  .home-lane-new-spinner { animation: none; }
}
</style>
