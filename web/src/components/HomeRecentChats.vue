<template>
  <div v-if="store.activeChatsAll.length" class="home-recent">
    <h2 class="home-recent-label">jump back in</h2>
    <div ref="lanesEl" class="home-lanes">
      <section
        v-for="lane in lanes"
        :key="lane.key"
        :ref="(element) => setLaneRef(lane.key, element as HTMLElement | null)"
        class="home-lane"
        :class="{
          'home-lane--active': isLaneActive(lane),
          'home-lane--unknown': !lane.workspace,
        }"
        :data-lane-key="lane.key"
      >
        <header class="home-lane-header" :data-workspace-color="lane.color">
          <div class="home-lane-heading">
            <span class="home-lane-shortcut">{{ lane.shortcut }}</span>
            <span class="home-lane-name">{{ lane.label || 'unassigned' }}</span>
            <span class="home-lane-summary" aria-live="polite">
              <template v-if="laneNeedsCount(lane)"><b>{{ laneNeedsCount(lane) }}</b> need{{ laneNeedsCount(lane) === 1 ? '' : 's' }} you</template>
              <template v-if="laneSummaryRest(lane)"><span v-if="laneNeedsCount(lane)"> · </span>{{ laneSummaryRest(lane) }}</template>
            </span>
          </div>
          <button
            v-if="laneNewAction(lane)"
            type="button"
            class="home-lane-new"
            :data-workspace-color="lane.color"
            :disabled="laneNewAction(lane)!.isCreating"
            :aria-label="`New chat in ${lane.label || 'workspace'}`"
            @click="emit('new-workspace-chat', laneNewAction(lane)!)"
          >{{ laneNewAction(lane)!.isCreating ? '...' : '+ new' }}</button>
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
                    :density="entry.key === 'needsYou' || entry.key === 'working' ? 'card' : 'row'"
                    :hue="colorOf(chat)"
                  />
                </span>
                <span v-if="entry.key === 'needsYou' && store.chatPendingQuestion(chat.chat_id)" class="home-chat-question">
                  {{ store.chatPendingQuestion(chat.chat_id) }}
                </span>
                <span class="home-chat-meta">
                  <span v-if="store.projectFor(chat.chat_id)?.name" class="home-chat-project">
                    {{ store.projectFor(chat.chat_id)?.name }}
                  </span>
                  <span v-if="chat.local === false" class="remote-chip">remote</span>
                  <span class="home-chat-time">{{ relativeActivity(chat) }}</span>
                </span>
              </button>
            </div>

          </template>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import type { ChatInfo } from '../lib/types'
import { ageBucket, chatActivityTimestamp, groupHomeTiers, type HomeTierKey, type HomeTiers } from '../lib/homeLanes'
import { formatRelative } from '../lib/relativeTime'
import { colorForWorkspace, type WorkspaceColorId } from '../lib/workspaceColors'
import ChatSignals from './ChatSignals.vue'

type NewWorkspaceChatAction = { workspace: string; projectId: string; isCreating: boolean }

const emit = defineEmits<{
  'new-workspace-chat': [action: NewWorkspaceChatAction]
}>()

const store = useProjectStore()
const lanesEl = ref<HTMLElement | null>(null)
const laneElements = ref<Record<string, HTMLElement>>({})

interface HomeLane {
  key: string
  workspace: string | null
  label: string
  shortcut: number | string
  color: WorkspaceColorId
  chats: ChatInfo[]
  tiers: HomeTiers
}

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

  const result = store.workspaceOptions.map((workspace, index) => {
    const chats = grouped.get(workspace.name) || []
    grouped.delete(workspace.name)
    return makeLane(
      workspace.name,
      workspaceLabel(workspace.name),
      index + 1,
      colorForWorkspace(workspace),
      chats,
    )
  })

  // Any workspace still in `grouped` belongs to a project whose workspace is no
  // longer in workspaceOptions — renaming or deleting a workspace refreshes
  // workspaces.value but leaves projects.value[].workspace on the old name. The
  // flat grid used to render these chats; without this sweep they would be
  // grouped and then never read back out, vanishing from home until a reload.
  // They keep a real workspace name, so the lane stays activatable.
  for (const [workspace, chats] of grouped) {
    if (!chats.length) continue
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
    tiers: groupHomeTiers(
      chats,
      chatId => store.chatNeedsInput(chatId),
      chatId => store.isChatStreaming(chatId) || store.chatHasBackgroundAgents(chatId),
      chatId => store.chatUnread(chatId) > 0,
    ),
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

// Derived from the same chat set as the rows beneath it. workspaceNeedsInput()
// counts nested delegates, which activeChatsAll deliberately excludes, so using
// it here made the header claim a chat needs you with no row to answer.
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

function laneNewAction(lane: HomeLane): NewWorkspaceChatAction | null {
  if (!lane.workspace || lane.workspace === 'unknown') return null
  const projectId = store.projects.find(
    project => project.name === 'General' && project.workspace === lane.workspace,
  )?.project_id || ''
  if (!projectId) return null
  return {
    workspace: lane.workspace,
    projectId,
    isCreating: Boolean(store.creatingChatProjectIds[projectId]),
  }
}

function isLaneActive(lane: HomeLane): boolean {
  return lane.workspace === store.activeWorkspace
}

function tierEntries(lane: HomeLane): Array<{ key: HomeTierKey; label: string; chats: ChatInfo[] }> {
  return [
    { key: 'needsYou', label: 'needs you', chats: lane.tiers.needsYou },
    { key: 'working', label: 'working', chats: lane.tiers.working },
    // A chat that finished while you were away is worth reading but is not
    // blocking, so it sits between working and quiet rather than being called
    // quiet - which contradicted the unread badge the sidebar showed for the
    // very same chat.
    { key: 'unread', label: 'unread', chats: lane.tiers.unread },
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
  return [
    `home-chat-item--${tier}`,
    ageBucket(chatActivityTimestamp(chat)) === 'week' ? 'home-chat-item--week-old' : '',
    ageBucket(chatActivityTimestamp(chat)) === 'older' ? 'home-chat-item--old' : '',
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

// ChatLayout owns the global keydown. The model is intentionally 2-D: vertical
// motion stays in a lane, while horizontal motion moves between lanes.
function onArrow(key: string): boolean {
  const model = focusableLanes()
  const available = model.some(lane => lane.length)
  if (!available) return false

  let laneIndex = model.findIndex(lane => lane.includes(document.activeElement as HTMLElement))
  let itemIndex = laneIndex >= 0 ? model[laneIndex].indexOf(document.activeElement as HTMLElement) : -1
  if (laneIndex < 0) {
    laneIndex = model.findIndex(lane => lane.length > 0)
    itemIndex = -1
  }

  if (key === 'ArrowUp' || key === 'ArrowDown') {
    const delta = key === 'ArrowDown' ? 1 : -1
    const nextIndex = itemIndex < 0 ? 0 : clamp(itemIndex + delta, 0, model[laneIndex].length - 1)
    if (nextIndex === itemIndex && itemIndex >= 0) return true
    focusElement(model[laneIndex][nextIndex])
    return true
  }

  if (key !== 'ArrowLeft' && key !== 'ArrowRight') return false

  const delta = key === 'ArrowRight' ? 1 : -1
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

function clamp(value: number, lower: number, upper: number): number {
  return Math.min(upper, Math.max(lower, value))
}

defineExpose({ onArrow })

watch(() => store.activeWorkspace, async () => {
  await nextTick()
  const lane = laneElements.value[store.activeWorkspace]
  lane?.scrollIntoView({ block: 'nearest' })
})

</script>

<style scoped>
.home-recent {
  width: 100%;
  max-width: 1320px;
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
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 20px;
}

.home-lane {
  min-width: 0;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: transparent;
}

/* The selected workspace reads as a raised surface, not another accent line.
   Every card and row in the lane already carries a hue rail, so a rail on the
   lane itself competed with them for the same meaning. A neutral lift says
   "this column" without adding a fourth coloured edge, and the accent stays
   where it identifies the workspace: the header rule, the name and the key
   badge. No border, so this is a surface rather than another outlined box. */
.home-lane--active {
  /* Between --bg and --bg2 on purpose: the cards inside are --bg2, so a full
     --bg2 lane would swallow them. This lifts the column while leaving the
     cards clearly above it. */
  background: color-mix(in srgb, var(--bg2) 55%, var(--bg));
}

.home-lane-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
  min-height: 44px;
  padding: 6px 0 8px;
  /* Inactive lanes keep the hue so the workspace is still identifiable, but at
     a fraction of the weight, so the active lane's rule reads as the emphatic
     one instead of every lane looking equally selected. */
  border-bottom: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
}

.home-lane--active .home-lane-header {
  border-bottom-width: 2px;
  border-bottom-color: var(--accent);
}

.home-lane-heading {
  display: flex;
  align-items: baseline;
  gap: 7px;
  min-width: 0;
  overflow: hidden;
}

.home-lane--active .home-lane-name {
  color: var(--fg);
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

.home-lane--active .home-lane-shortcut {
  border-color: var(--accent);
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

.home-lane-summary {
  min-width: 0;
  overflow: hidden;
  color: var(--fg2);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-lane-summary b {
  color: var(--accent);
  font-weight: 700;
}

.home-lane-new {
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

.home-chat-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -1px;
  box-shadow: none;
}

.home-chat-item--needsYou {
  background: color-mix(in srgb, var(--accent) 8%, var(--bg2));
}

.home-chat-item--working .home-chat-title {
  color: var(--fg2);
  font-weight: 400;
}

.home-chat-item--unread,
.home-chat-item--quiet,
.home-chat-item--older {
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

.home-chat-item--unread:hover,
.home-chat-item--quiet:hover,
.home-chat-item--older:hover {
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

.home-chat-item--unread .home-chat-heading,
.home-chat-item--quiet .home-chat-heading,
.home-chat-item--older .home-chat-heading {
  flex: 1;
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

.home-chat-item--unread .home-chat-title,
.home-chat-item--quiet .home-chat-title,
.home-chat-item--older .home-chat-title {
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

.home-chat-meta {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
}

.home-chat-project {
  min-width: 0;
  overflow: hidden;
  color: var(--fg2);
  letter-spacing: 0.03em;
  text-overflow: ellipsis;
  text-transform: uppercase;
  white-space: nowrap;
}

.home-chat-time {
  flex: 0 0 auto;
  margin-left: auto;
  color: var(--fg3);
  font-family: var(--font-mono);
  white-space: nowrap;
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

@keyframes home-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

@container (max-width: 760px) {
  /* Stack the lanes and keep every one of them open. Collapsing the inactive
     lane to a summary row read as a stray element rather than a control, and it
     hid that workspace's "+ new" button — leaving no way to start a chat there
     on a phone. Scrolling past a short lane is cheaper than that. */
  .home-lanes {
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-5);
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-chat-item { transition: none; }
  .title-shimmer { animation: none; }
}
</style>
