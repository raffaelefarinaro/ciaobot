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
    <div class="home-recent-heading">
      <h2 class="home-recent-label">Continue where you left off</h2>
    </div>
    <div ref="lanesEl" class="home-lanes">
      <section
        v-for="lane in lanes"
        :key="lane.key"
        :ref="(element) => setLaneRef(lane.key, element as HTMLElement | null)"
        class="home-lane"
        :data-lane-key="lane.key"
      >
        <!-- The selected workspace's lane needs no header: the sidebar's
             workspace scope already names it, the composer above starts new
             work, and the tier labels carry the counts. Its status sentence
             stays for screen readers. Rescue lanes keep a visible header,
             because their workspace name is the only thing that says why
             they are here. -->
        <header
          class="home-lane-header"
          :class="{ 'home-lane-header--quiet': isActiveLane(lane) }"
          :data-workspace-color="lane.color"
        >
          <div class="home-lane-topline">
            <div class="home-lane-heading">
              <template v-if="!isActiveLane(lane)">
                <span class="home-lane-shortcut">{{ lane.shortcut }}</span>
                <span class="home-lane-name">{{ lane.label || 'unassigned' }}</span>
              </template>
              <span class="home-lane-status-text" aria-live="polite">{{ laneStatusText(lane) }}</span>
            </div>
            <button
              v-if="!isActiveLane(lane) && lane.newAction && lane.workspace"
              type="button"
              class="home-lane-new"
              :class="{ 'home-lane-new--creating': lane.newAction.isCreating }"
              :data-workspace-color="lane.color"
              :disabled="lane.newAction.isCreating"
              :aria-label="`Choose a project for a new chat in ${lane.label || 'workspace'}`"
              aria-haspopup="dialog"
              @click="emit('choose-new-chat', lane.workspace)"
            ><span v-if="lane.newAction.isCreating" class="home-lane-new-spinner" aria-hidden="true" /><span>{{ lane.newAction.isCreating ? 'Creating…' : '+ new' }}</span></button>
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
                <span class="home-chat-main">
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
                  </span>
                  <!-- Project plus a status phrase read from the same signals
                       that sorted the row into its tier, so the sub-line can
                       never claim more than the tier heading above it. -->
                  <span class="home-chat-meta">
                    <span v-if="store.projectFor(chat.chat_id)?.name" class="home-chat-project">{{ store.projectFor(chat.chat_id)?.name }}</span>
                    <span class="home-chat-status">{{ tierPhrase(entry.key) }}</span>
                    <span v-if="chat.local === false" class="remote-chip">remote</span>
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
                </span>
                <span class="home-chat-time">{{ relativeActivity(chat) }}</span>
              </button>
            </div>

          </template>

          <!-- The memory work each archived conversation handed off: the memory
               pass it queued, as ONE entry. The pass is
               a real chat, and it used to be listed twice — once in the tiers
               above under its own internal title, once here. The row now opens
               the pass (where the live turn, the question and the reply are),
               falling back to the archived transcript before one exists. -->
          <div v-if="lane.insights.length" class="home-insights">
            <div class="home-tier-label"><span>memory insights</span></div>
            <div
              v-for="insight in lane.insights"
              :key="`insight-${insight.sourceChatId}`"
              class="home-insight-row"
              :data-workspace-color="insightColor(insight)"
            >
              <button
                type="button"
                class="home-chat-item home-chat-item--insight"
                :class="`home-chat-item--insight-${insight.phase}`"
                :disabled="!insight.passChatId && !insight.archivePath"
                :title="insight.passChatId
                  ? `Open the memory insight for ${insight.title}`
                  : insight.archivePath
                    ? 'Open the archived transcript'
                    : insight.title"
                @click="openInsight(insight)"
              >
                <span class="home-chat-heading">
                  <span class="home-chat-title">{{ insight.title }}</span>
                </span>
                <span class="home-chat-meta">
                  <span class="home-chat-tidy-note">
                    <span
                      v-if="insight.active"
                      class="home-chat-tidy-dot"
                      aria-hidden="true"
                    />
                    {{ insight.label }}
                  </span>
                  <span class="home-chat-time">{{ formatRelative(insight.timestamp) }}</span>
                </span>
                <!-- The one question the pass is blocked on, when it is blocked
                     on one. The row's label says it needs you; this says what
                     you are being asked. -->
                <span v-if="insight.question" class="home-chat-question">
                  {{ insight.question }}
                </span>
              </button>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useProjectStore } from '../stores/projects'
import type { ChatInfo, ProjectInfo } from '../lib/types'
import { ageBucket, chatActivityTimestamp, groupHomeTiers, type HomeTierKey, type HomeTiers } from '../lib/homeLanes'
import { activeInsightSummary, type MemoryInsight } from '../lib/memoryInsights'
import { formatRelative } from '../lib/relativeTime'
import { colorForWorkspace, type WorkspaceColorId } from '../lib/workspaceColors'
import { useFileViewerStore } from '../stores/fileViewer'
import ChatSignals from './ChatSignals.vue'

type NewWorkspaceChatAction = { workspace: string; projectId: string; isCreating: boolean }

const emit = defineEmits<{
  'choose-new-chat': [workspace: string]
}>()

const store = useProjectStore()
const fileViewer = useFileViewerStore()
// A pass is no longer a chat row, so the memory-insight rail is the only thing
// left that says memory work is happening. Without it a workspace whose only
// activity is a running pass would read as empty.
const hasHomeActivity = computed(() => (
  store.activeChatsAll.length > 0 || store.memoryInsightRows.length > 0
))
const lanesEl = ref<HTMLElement | null>(null)
const laneElements = ref<Record<string, HTMLElement>>({})

// The pass is the chat a person opens: it is where the live turn, the question
// and the reply are. The transcript is the fallback for the window before a
// pass exists, and the only target when there never will be one.
function openInsight(insight: MemoryInsight): void {
  if (insight.passChatId) {
    void store.switchChat(insight.passChatId)
    return
  }
  if (insight.archivePath) void fileViewer.open(insight.archivePath)
}

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
  insights: MemoryInsight[]
}

// Grouped once per recompute rather than re-scanning the full list inside
// makeLane for every lane — this component re-renders on every streaming tick,
// so that was an O(workspaces × rows) rescan. Partitioning a pre-sorted list
// preserves order, so no re-sort is needed per workspace.
//
// A row is filed under the workspace of its pass when it has one, falling back
// to the source conversation: a pass can outlive the chat it was spawned for
// (the source is archived and may be deleted), and a pass always runs in the
// Memory project of the workspace that conversation belonged to.
function groupInsightsByWorkspace(rows: MemoryInsight[]): Map<string, MemoryInsight[]> {
  const map = new Map<string, MemoryInsight[]>()
  for (const row of rows) {
    const chatId = row.passChatId || row.sourceChatId
    const workspace = store.projectFor(chatId)?.workspace
    if (!workspace) continue
    const bucket = map.get(workspace)
    if (bucket) bucket.push(row)
    else map.set(workspace, [row])
  }
  return map
}

const insightsByWorkspace = computed(() => groupInsightsByWorkspace(store.memoryInsightRows))

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
      chatId => store.chatIsWorking(chatId),
      chatId => store.chatUnread(chatId) > 0,
    ),
    insights: (workspace && workspace !== 'unknown' && insightsByWorkspace.value.get(workspace)) || [],
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

function insightWorkspace(insight: MemoryInsight): string | undefined {
  return store.projectFor(insight.passChatId || insight.sourceChatId)?.workspace
}

function insightColor(insight: MemoryInsight): WorkspaceColorId {
  return colorForWorkspace(
    store.workspaceOptions.find(item => item.name === insightWorkspace(insight)),
  )
}

function isActiveLane(lane: HomeLane): boolean {
  return lane.workspace === store.activeWorkspace
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

/**
 * Memory-insight rows still in flight, plus the ones waiting on the owner. Both
 * are reported here because a pass blocked on a question is a live turn in a
 * chat the owner is expected to open — folding it into the in-flight fragment
 * would understate it, and dropping it would report "nothing needs your
 * attention" with a row on screen saying the opposite.
 */
function laneInsightCounts(lane: HomeLane): { active: number; blocking: number } {
  return {
    active: lane.insights.filter(row => row.active).length,
    blocking: lane.insights.filter(row => row.blocking).length,
  }
}

// The glanceable sentence on the workspace line, in plain status grammar.
// Counts come from the same tiers and rows as the content below, so it can
// never disagree with what is rendered.
function laneStatusText(lane: HomeLane): string {
  // Both actionable tiers, as the glanceable sentence has always counted
  // them: an unread reply wants the user even though it is not blocking the
  // agent on a direct answer, and counting only `needsYou` told a reader with
  // unread replies waiting that "nothing needs your attention". The tiers are
  // mutually exclusive, so adding them cannot double-count a chat.
  const insights = laneInsightCounts(lane)
  const needs = laneNeedsCount(lane) + laneUnreadCount(lane) + insights.blocking
  const sentences: string[] = []
  sentences.push(needs
    ? `${needs} chat${needs === 1 ? '' : 's'} need${needs === 1 ? 's' : ''} your attention`
    : 'nothing needs your attention')
  const working = laneWorkingCount(lane)
  sentences.push(working
    ? `${working} agent${working === 1 ? '' : 's'} still working`
    : 'no agents working')
  // A pass already waiting on the owner is not "still updating": it is counted
  // in `needs` above instead.
  const active = activeInsightSummary(insights.active)
  if (active) sentences.push(active)
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

function tierEntries(lane: HomeLane): Array<{ key: HomeTierKey; label: string; chats: ChatInfo[] }> {
  return [
    { key: 'needsYou', label: 'needs you', chats: lane.tiers.needsYou },
    { key: 'working', label: 'working', chats: lane.tiers.working },
    { key: 'unread', label: 'unread', chats: lane.tiers.unread },
    { key: 'quiet', label: 'earlier', chats: [...lane.tiers.quiet, ...lane.tiers.older] },
  ]
}

// The sub-line's status phrase. The tier was derived from the store's
// needs-input / working / unread signals, so it restates that fact in words.
function tierPhrase(tier: HomeTierKey): string {
  if (tier === 'needsYou') return 'waiting for you'
  if (tier === 'working') return 'agent is working'
  if (tier === 'unread') return 'new reply'
  return 'no new activity'
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
  margin: 42px auto 0;
  text-align: left;
  /* Stacking is decided by the width the lanes actually get, not the window's.
     The sidebar is resizable and takes a large share, so a viewport media query
     stacked far too late: at a 1750px window the lanes still had only ~450px
     each and ellipsed almost every title. */
  container-type: inline-size;
}

/* Prototype A's section heading. Visible again: the lane and tier labels
   below say how the list is sorted, this says what the list is for. */
.home-recent-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
}

.home-recent-label {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
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
  padding: 0;
}

.home-lane-header--quiet {
  position: absolute;
  width: 1px;
  height: 1px;
  min-height: 0;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
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
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg3);
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
  font-size: var(--text-sm);
  font-weight: 650;
  text-overflow: ellipsis;
  text-transform: capitalize;
  white-space: nowrap;
}

.home-lane-status-text {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--fg3);
  font-size: var(--text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* The workspace line's own in-flight dot. Muted, never accent: the count is
   information, not a call to act. */
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

@keyframes home-lane-archiving-spin {
  to { transform: rotate(360deg); }
}

@keyframes home-lane-tidy-breathe {
  0%, 100% { opacity: 0.35; }
  50%      { opacity: 0.9; }
}

@media (prefers-reduced-motion: reduce) {
  .home-chat-tidy-dot { animation: none; opacity: 0.75; }
}

.home-lane-new {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: var(--touch, 44px);
  flex: 0 0 auto;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--fg3);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
  white-space: nowrap;
}

/* A quiet secondary: the composer above is the page's primary way in. */
.home-lane-new:hover,
.home-lane-new:focus-visible {
  border-color: var(--border);
  background: var(--bg-elev);
  color: var(--fg);
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
  border-color: var(--border);
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
  min-width: var(--touch, 44px);
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
  gap: var(--space-4);
  min-width: 0;
  padding-top: 4px;
}

.home-lane-empty {
  padding: 10px 2px;
  border-top: 1px solid var(--border);
  color: var(--fg3);
  font-size: var(--text-sm);
}

.home-tier {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

/* Lowercase deliberately: these are quiet structural markers, not headings.
   The label's hairline doubles as the top rule of the tier's row list. */
.home-tier-label {
  display: flex;
  gap: var(--space-2);
  padding-bottom: var(--space-1);
  border-bottom: 1px solid var(--border);
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}

/* Prototype A's chat row: a flat hairline list, title over a project/status
   sub-line, time in its own right column. Every tier shares the row; urgency
   rides the title weight, the ChatSignals dot and the tier label rather than
   a card treatment. */
.home-chat-item {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  width: 100%;
  min-width: 0;
  min-height: 62px;
  padding: 10px 2px;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  text-align: left;
  transition: background 120ms var(--ease);
}

.home-chat-item:hover .home-chat-title {
  color: var(--accent);
}

.home-chat-item:active {
  background: color-mix(in srgb, var(--accent) 5%, transparent);
}

.home-chat-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
  box-shadow: 0 0 0 2px var(--bg);
}

/* Needs-you rows keep one quiet accent: a short rail on the left edge, so
   the tier stays findable when it is scrolled past its label. */
.home-chat-item--needsYou {
  padding-left: 12px;
  box-shadow: inset 2px 0 0 var(--accent);
}

.home-chat-item--needsYou:focus-visible {
  box-shadow: inset 2px 0 0 var(--accent), 0 0 0 2px var(--bg);
}

.home-chat-item--insight {
  min-height: var(--touch, 44px);
  flex: 1;
  /* A pending question needs its own line. The tier rows get that from the
     `.home-chat-main` column they sit in; this row is flat (title left, status
     and time right), so it wraps instead and the question takes the full width
     rather than landing between the status and the time. */
  flex-wrap: wrap;
  row-gap: 0;
  /* The hairline lives on the wrapper. */
  border-bottom: 0;
}

.home-chat-item--insight .home-chat-question {
  flex: 1 0 100%;
}

/* The row wrapper carries the workspace colour; it has no background and no
   radius of its own — only the rule every other row in the section draws. */
.home-insight-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  border-bottom: 1px solid var(--border);
}

.home-chat-item--week-old { opacity: 0.72; }
.home-chat-item--old { opacity: 0.55; }

.home-chat-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.home-chat-heading {
  display: flex;
  flex: 1;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.home-chat-title {
  display: block;
  min-width: 0;
  flex: 1;
  overflow: hidden;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 600;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: color 120ms var(--ease);
}

/* Quiet rows step the title back a register; anything that wants the user
   (needs you, unread) keeps full weight. A memory-insight row is a quiet row
   until it asks for something, and the two states that do take the accent
   colour below. */
.home-chat-item--quiet .home-chat-title,
.home-chat-item--older .home-chat-title,
.home-chat-item--insight .home-chat-title {
  color: var(--fg2);
  font-weight: 500;
}

.home-chat-item--insight-needsYou .home-chat-title,
.home-chat-item--insight-attention .home-chat-title {
  color: var(--fg);
  font-weight: 600;
}

.home-chat-item .home-chat-title--unread {
  color: var(--fg);
  font-weight: 650;
}

.home-chat-question {
  display: -webkit-box;
  margin-top: 4px;
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
  margin-top: 4px;
  overflow: hidden;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.home-chat-meta {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.4;
}

/* A memory-insight row puts its status and time in the meta; there it is the
   row's right column rather than a sub-line. */
.home-chat-item--insight .home-chat-meta {
  flex: 0 1 auto;
  align-items: center;
  margin-left: auto;
}

.home-chat-project {
  min-width: 0;
  flex: 0 1 auto;
  overflow: hidden;
  color: var(--fg2);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-chat-status {
  flex: 0 0 auto;
  white-space: nowrap;
}

.home-chat-project + .home-chat-status::before {
  content: '·';
  margin-right: 6px;
  color: var(--fg3);
}

.home-chat-item--needsYou .home-chat-status {
  color: var(--accent);
}

.home-chat-time {
  flex: 0 0 auto;
  min-width: 5ch;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  text-align: right;
  white-space: nowrap;
}

/* Live step of a memory insight ("updating memory…"). Muted like the lane
   header's in-flight fragment: this is background work, never a demand. */
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

/* A pass blocked on the owner takes the accent, and a pass that stopped with
   work left takes the warn colour. Both are rows you are meant to
   act on, so neither reads as the muted background chatter an in-flight row is.
   One rule per phase, so nothing has to win a specificity fight to colour a
   row correctly. */
.home-chat-item--insight-needsYou .home-chat-tidy-note {
  color: var(--accent);
}

.home-chat-item--insight-attention .home-chat-tidy-note {
  color: var(--warning);
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

@container (max-width: 560px) {
  /* On a phone the lane header (badge + workspace name + status sentence +
     "+ new") has ~320px to share. Keeping name and status on one flex row
     made both ellipsis to ~2–3 chars ("WO…", "nothing needs…") — neither
     was readable. Hiding the status when a workspace name is present gives
     the name its full width; the single-workspace case has no name, so its
     status stays visible as the only heading text. Two-line stacking is the
     alternative if the status must remain glanceable on mobile. */
  .home-lane-name ~ .home-lane-status-text {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-chat-item,
  .home-chat-title { transition: none; }
  .title-shimmer { animation: none; }
  .home-lane-new-spinner { animation: none; }
}
</style>
