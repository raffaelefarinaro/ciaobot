<template>
  <div v-if="store.activeChatsAll.length" class="home-recent">
    <div class="home-recent-label">jump back in</div>
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
            <span class="home-lane-shortcut">[{{ lane.shortcut }}]</span>
            <span class="home-lane-name">{{ lane.label || 'unassigned' }}</span>
            <span class="home-lane-summary" aria-live="polite">
              <template v-if="laneNeedsCount(lane)">
                <b>{{ laneNeedsCount(lane) }}</b> need{{ laneNeedsCount(lane) === 1 ? '' : 's' }} you
              </template>
              <template v-if="laneWorkingCount(lane)">
                <span v-if="laneNeedsCount(lane)"> · </span>{{ laneWorkingCount(lane) }} working
              </template>
              <template v-if="laneQuietCount(lane)">
                <span v-if="laneNeedsCount(lane) || laneWorkingCount(lane)"> · </span>{{ laneQuietCount(lane) }} quiet
              </template>
              <span v-if="!laneNeedsCount(lane) && !laneWorkingCount(lane)">all quiet</span>
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

        <button
          v-if="!isLaneActive(lane)"
          type="button"
          class="home-lane-peek"
          :data-workspace-color="lane.color"
          :aria-label="`Show ${lane.label || 'unassigned'} workspace`"
          @click="activateLane(lane)"
        >
          <span>[{{ lane.shortcut }}] {{ lane.label || 'unassigned' }}</span>
          <span class="home-lane-peek-summary">· {{ laneSummary(lane) }}</span>
        </button>

        <div class="home-lane-body">
          <div v-if="!laneHasChats(lane)" class="home-lane-empty">// no active chats</div>

          <template v-for="entry in tierEntries(lane)" :key="entry.key">
            <div
              v-if="entry.key !== 'older' && (entry.chats.length || entry.key === 'needsYou')"
              class="home-tier"
              :class="`home-tier--${entry.key}`"
            >
              <div class="home-tier-label">
                <span>{{ entry.label }}</span>
                <span v-if="entry.key === 'needsYou' && !entry.chats.length">// nothing needs you here</span>
              </div>
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

            <div v-else-if="entry.chats.length" class="home-tier home-tier--older">
              <div class="home-tier-label"><span>older</span></div>
              <button
                type="button"
                class="home-lane-older-toggle"
                :aria-expanded="Boolean(olderExpanded[lane.key])"
                @click="olderExpanded[lane.key] = !olderExpanded[lane.key]"
              >
                {{ olderExpanded[lane.key] ? 'Hide older chats' : `${entry.chats.length} more, older than a week` }}
              </button>
              <template v-if="olderExpanded[lane.key]">
                <button
                  v-for="chat in entry.chats"
                  :key="chat.chat_id"
                  type="button"
                  class="home-chat-item home-chat-item--quiet home-chat-item--older"
                  :class="chatItemClasses('older', chat)"
                  :data-workspace-color="colorOf(chat)"
                  :disabled="chat.local === false"
                  :title="chat.local === false ? 'This chat lives on another instance' : chat.title"
                  @click="chat.local !== false && store.switchChat(chat.chat_id)"
                >
                  <span
                    class="home-chat-title"
                    :class="{ 'home-chat-title--unread': store.chatUnread(chat.chat_id) > 0 }"
                  >{{ chat.title }}</span>
                  <ChatSignals :chat-id="chat.chat_id" density="row" :hue="colorOf(chat)" />
                  <span class="home-chat-time">{{ relativeActivity(chat) }}</span>
                </button>
              </template>
            </div>
          </template>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
const olderExpanded = ref<Record<string, boolean>>({})
const isNarrow = ref(typeof window !== 'undefined' && window.innerWidth < 820)

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
    return makeLane(
      workspace.name,
      workspaceLabel(workspace.name),
      index + 1,
      colorForWorkspace(workspace),
      chats,
    )
  })
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

function laneNeedsCount(lane: HomeLane): number {
  return lane.workspace && lane.workspace !== 'unknown'
    ? store.workspaceNeedsInput(lane.workspace)
    : lane.tiers.needsYou.length
}

function laneWorkingCount(lane: HomeLane): number {
  return lane.tiers.working.length
}

function laneQuietCount(lane: HomeLane): number {
  return lane.tiers.quiet.length + lane.tiers.older.length
}

function laneSummary(lane: HomeLane): string {
  const needs = laneNeedsCount(lane)
  const working = laneWorkingCount(lane)
  const quiet = laneQuietCount(lane)
  const parts: string[] = []
  if (needs) parts.push(`${needs} need you`)
  if (working) parts.push(`${working} working`)
  if (quiet) parts.push(`${quiet} quiet`)
  return parts.join(' · ') || 'all quiet'
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

function activateLane(lane: HomeLane) {
  if (lane.workspace) void store.switchWorkspace(lane.workspace)
}

function tierEntries(lane: HomeLane): Array<{ key: HomeTierKey; label: string; chats: ChatInfo[] }> {
  return [
    { key: 'needsYou', label: 'needs you', chats: lane.tiers.needsYou },
    { key: 'working', label: 'working', chats: lane.tiers.working },
    { key: 'quiet', label: 'quiet', chats: lane.tiers.quiet },
    { key: 'older', label: 'older', chats: lane.tiers.older },
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
  return Array.from(container.querySelectorAll<HTMLElement>('.home-lane')).map(lane => {
    const key = lane.dataset.laneKey || ''
    const selector = isNarrow.value && key !== store.activeWorkspace
      ? '.home-lane-peek:not([disabled])'
      : '.home-chat-item:not([disabled]), .home-lane-older-toggle:not([disabled])'
    return Array.from(lane.querySelectorAll<HTMLElement>(selector))
  })
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
  if (isNarrow.value) return false

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

function onResize() {
  isNarrow.value = window.innerWidth < 820
}

watch(() => store.activeWorkspace, async () => {
  await nextTick()
  const lane = laneElements.value[store.activeWorkspace]
  lane?.scrollIntoView({ block: 'nearest' })
})

onMounted(() => window.addEventListener('resize', onResize))
onBeforeUnmount(() => window.removeEventListener('resize', onResize))
</script>

<style scoped>
.home-recent {
  width: 100%;
  max-width: 1040px;
  margin: 0 auto;
  text-align: left;
}

.home-recent-label {
  margin: 0 0 8px 2px;
  color: var(--fg2);
  font-size: var(--text-xs, 0.72rem);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.home-lanes {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 20px;
}

.home-lane {
  min-width: 0;
  border-radius: var(--radius, 10px);
}

.home-lane--active {
  background: color-mix(in srgb, var(--bg2) 28%, transparent);
}

.home-lane-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
  min-height: 44px;
  padding: 6px 0 8px;
  border-bottom: 2px solid var(--accent);
}

.home-lane-heading {
  display: flex;
  align-items: baseline;
  gap: 7px;
  min-width: 0;
  overflow: hidden;
}

.home-lane-shortcut {
  flex: 0 0 auto;
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  font-weight: 700;
}

.home-lane-name {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
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

.home-tier-label {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--border);
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  letter-spacing: 0.07em;
  text-transform: uppercase;
}

.home-tier-label span:last-child {
  letter-spacing: 0;
  text-transform: none;
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
  border-radius: var(--radius, 10px);
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

.home-chat-item:focus-visible,
.home-lane-peek:focus-visible,
.home-lane-older-toggle:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px var(--bg), 0 0 0 4px var(--accent);
}

.home-chat-item--needsYou {
  background: color-mix(in srgb, var(--accent) 8%, var(--bg2));
}

.home-chat-item--working .home-chat-title {
  color: var(--fg2);
  font-weight: 400;
}

.home-chat-item--quiet,
.home-chat-item--older {
  flex-direction: row;
  align-items: center;
  gap: 8px;
  min-height: var(--touch, 44px);
  padding: 7px 10px;
  border: 0;
  border-left: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  border-radius: 0;
  background: transparent;
}

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

.home-lane-older-toggle {
  align-self: flex-start;
  min-height: var(--touch, 44px);
  padding: 5px 2px;
  border: 0;
  background: none;
  color: var(--fg2);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-xs);
  text-align: left;
}

.home-lane-older-toggle:hover { color: var(--fg); }

.home-lane-peek {
  display: none;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: var(--touch, 44px);
  padding: 7px 10px;
  border: 1px solid color-mix(in srgb, var(--accent) 55%, var(--border));
  border-radius: var(--radius, 10px);
  background: color-mix(in srgb, var(--accent) 5%, var(--bg2));
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
}

.home-lane-peek-summary {
  min-width: 0;
  overflow: hidden;
  color: var(--fg2);
  text-overflow: ellipsis;
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

@media (max-width: 819px) {
  .home-lanes {
    grid-template-columns: minmax(0, 1fr);
    gap: 8px;
  }

  .home-lane:not(.home-lane--active) .home-lane-header,
  .home-lane:not(.home-lane--active) .home-lane-body {
    display: none;
  }

  .home-lane:not(.home-lane--active) .home-lane-peek {
    display: flex;
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-chat-item { transition: none; }
  .title-shimmer { animation: none; }
}
</style>
