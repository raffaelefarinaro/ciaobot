<template>
  <div v-if="store.activeChatsAll.length" class="home-recent">
    <div class="home-recent-label">jump back in</div>
    <div class="home-recent-grid" ref="gridEl">
      <button
        type="button"
        v-for="chat in store.activeChatsAll"
        :key="'home-recent-' + chat.chat_id"
        class="home-recent-card"
        :class="{
          remote: chat.local === false,
          'needs-input': store.chatNeedsInput(chat.chat_id),
        }"
        :disabled="chat.local === false"
        :title="chat.local === false ? 'This chat lives on another instance' : chat.title"
        @click="chat.local !== false && store.switchChat(chat.chat_id)"
      >
        <span
          class="home-recent-top"
          :data-workspace-color="colorOf(chat)"
        >
          <span
            v-if="chat.title_status === 'pending'"
            class="title-shimmer"
            aria-label="Generating title"
          />
          <span v-else class="home-recent-title">{{ chat.title }}</span>
          <span
            v-if="loopBadge(chat.chat_id)"
            class="loop-mark"
            :class="{ stopped: !loopBadge(chat.chat_id)!.running }"
            :title="loopBadge(chat.chat_id)!.title"
            :aria-label="loopBadge(chat.chat_id)!.title"
          >&#10227;</span>
          <span v-if="store.isChatStreaming(chat.chat_id)" class="spinner-dot" title="Working" />
          <span v-else-if="store.chatHasBackgroundAgents(chat.chat_id)" class="spinner-dot bg-agents" title="Background agents running" />
          <span v-else-if="store.chatNeedsInput(chat.chat_id)" class="needs-input-badge" title="Needs your answer">?</span>
          <span v-else-if="store.chatUnread(chat.chat_id) > 0" class="unread-dot" title="Unread" />
        </span>
        <span class="home-recent-meta">
          <span
            class="home-recent-ws"
            v-if="workspaceOf(chat)"
            :data-workspace-color="colorOf(chat)"
          >{{ workspaceOf(chat) }}</span>
          <span class="home-recent-project" v-if="store.projectFor(chat.chat_id)?.name">
            {{ store.projectFor(chat.chat_id)?.name }}
          </span>
          <span v-if="chat.local === false" class="remote-chip">remote</span>
        </span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import type { ChatInfo } from '../lib/types'
import { colorForWorkspace, type WorkspaceColorId } from '../lib/workspaceColors'

const store = useProjectStore()
const taskStore = useTaskStore()

// ── Keyboard navigation (desktop app) ────────────────────────────────
// ChatLayout owns the global keydown; it calls onArrow() here. We move DOM
// focus between the openable chat cards (grid-aware: Up/Down jump a row,
// Left/Right a column). Remote chats are disabled buttons and skipped.
// Enter is left to the browser: a focused card button activates on Enter,
// which triggers its @click → switchChat, so no openFocused() is needed.
const gridEl = ref<HTMLElement | null>(null)

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v))
}

// Resolved column count of the responsive grid (auto-fit minmax(240px,1fr)).
function columnCount(): number {
  const grid = gridEl.value
  if (!grid) return 1
  const cols = getComputedStyle(grid).gridTemplateColumns.trim().split(/\s+/).filter(Boolean)
  return Math.max(1, cols.length)
}

function openableCards(): HTMLButtonElement[] {
  const grid = gridEl.value
  if (!grid) return []
  return Array.from(grid.querySelectorAll<HTMLButtonElement>('.home-recent-card:not([disabled])'))
}

// Returns true if the key was consumed (i.e. there were chats to navigate).
function onArrow(key: string): boolean {
  const cards = openableCards()
  if (!cards.length) return false
  const cols = columnCount()
  const cur = cards.findIndex(c => c === document.activeElement)
  let next: number
  switch (key) {
    case 'ArrowRight': next = cur < 0 ? 0 : clamp(cur + 1, 0, cards.length - 1); break
    case 'ArrowLeft': next = cur < 0 ? 0 : clamp(cur - 1, 0, cards.length - 1); break
    case 'ArrowDown': next = cur < 0 ? 0 : clamp(cur + cols, 0, cards.length - 1); break
    case 'ArrowUp': next = cur < 0 ? 0 : clamp(cur - cols, 0, cards.length - 1); break
    default: return false
  }
  if (next === cur && cur >= 0) return true // at the edge: still consume the key
  cards[next].focus()
  cards[next].scrollIntoView({ block: 'nearest' })
  return true
}

defineExpose({ onArrow })

// Loop marker, same semantics as the sidebar: a loop-driven chat produces new
// turns on its own, which is worth knowing before you open it. ChatLayout
// fetches loops on mount and the `loops_changed` event keeps them fresh.
const loopsByChat = computed(() => {
  const byChat = new Map<string, { count: number; running: boolean }>()
  for (const l of taskStore.loops) {
    const prev = byChat.get(l.web_chat_id)
    byChat.set(l.web_chat_id, {
      count: (prev?.count || 0) + 1,
      running: (prev?.running || false) || !!l.running,
    })
  }
  return byChat
})
function loopBadge(chatId: string): { running: boolean; title: string } | null {
  const info = loopsByChat.value.get(chatId)
  if (!info) return null
  const plural = info.count > 1 ? `${info.count} loops` : 'A loop'
  return {
    running: info.running,
    title: info.running
      ? `${plural} running in this chat`
      : `${plural} attached to this chat (stopped)`,
  }
}

function workspaceNameOf(chat: ChatInfo): string {
  return store.projectFor(chat.chat_id)?.workspace || ''
}

// Workspace label for a chat's project, normalized like the sidebar
// workspace pills ("personal"/"work"). Empty when the project is unknown.
function workspaceOf(chat: ChatInfo): string {
  return workspaceNameOf(chat)
    .split(/[-_\s]+/)
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
}

function colorOf(chat: ChatInfo): WorkspaceColorId {
  const name = workspaceNameOf(chat)
  const ws = store.workspaceOptions.find((item) => item.name === name)
  return colorForWorkspace(ws)
}
</script>

<style scoped>
.home-recent {
  width: 100%;
  max-width: 560px;
  margin: 0 auto;
  text-align: left;
}
.home-recent-label {
  font-size: var(--text-xs, 0.72rem);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--fg2);
  margin: 0 0 8px 2px;
}
.home-recent-grid {
  display: grid;
  /* auto-fit (not auto-fill): empty tracks collapse so a lone card — or the
     last card on an odd-count row — stretches to the full column width. */
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 8px;
}
.home-recent-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--border, #2a2d34);
  border-radius: 8px;
  background: var(--bg2);
  color: var(--fg);
  cursor: pointer;
  text-align: left;
  font-family: inherit;
  transition: border-color 0.12s ease, background 0.12s ease, transform 0.06s ease;
}
.home-recent-card:hover {
  border-color: var(--accent, #f2555a);
  background: var(--bg3);
}
.home-recent-card:active {
  transform: translateY(1px);
}
.home-recent-card:focus-visible {
  outline: none;
  border-color: var(--accent, #f2555a);
  box-shadow: 0 0 0 2px var(--bg), 0 0 0 4px var(--accent, #f2555a);
}
.home-recent-card.remote {
  opacity: 0.6;
  cursor: default;
}
.home-recent-card.needs-input {
  border-color: var(--accent, #f2555a);
}
.home-recent-top {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.home-recent-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-sm, 0.86rem);
  font-weight: 500;
}
.home-recent-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}
.home-recent-ws {
  font-size: 0.62rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 600;
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 4px;
  padding: 1px 5px;
  flex: 0 0 auto;
  opacity: 0.85;
}
.home-recent-project {
  font-size: var(--text-xs, 0.72rem);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.loop-mark {
  flex: 0 0 auto;
  /* Matches the sidebar's loop marker: 11px was too small for a thin outline
     glyph, and a hardcoded px ignored the Appearance font scale. */
  font-size: var(--text-lg);
  font-weight: 700;
  line-height: 1;
  color: var(--accent);
}
.loop-mark.stopped {
  color: var(--fg2);
  opacity: 0.7;
}
.unread-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent, #f2555a);
  flex: 0 0 auto;
}
.spinner-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent, #f2555a);
  flex: 0 0 auto;
  animation: home-pulse 1s ease-in-out infinite;
}
.spinner-dot.bg-agents {
  background: var(--warning);
}
.needs-input-badge {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--accent, #f2555a);
  color: #fff;
  font-size: 0.7rem;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
}
.remote-chip {
  font-size: 0.62rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--fg2);
  border: 1px solid var(--border, #2a2d34);
  border-radius: 4px;
  padding: 1px 4px;
}
.title-shimmer {
  flex: 1;
  height: 12px;
  border-radius: 4px;
  background: linear-gradient(90deg, var(--border, #2a2d34) 25%, var(--bg3) 50%, var(--border, #2a2d34) 75%);
  background-size: 200% 100%;
  animation: home-shimmer 1.2s ease-in-out infinite;
}
@keyframes home-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
@keyframes home-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
</style>
