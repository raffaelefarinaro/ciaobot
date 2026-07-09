<template>
  <div class="bell-root" @keydown.esc="open = false">
    <button
      ref="btnRef"
      class="bell-btn touch-hit"
      :class="{ 'has-unread': totalUnread > 0 }"
      :aria-label="totalUnread > 0 ? `${totalUnread} unread chats` : 'Notifications'"
      :aria-expanded="open"
      @click="toggle"
    >
      <svg
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
        <path d="M13.73 21a2 2 0 0 1-3.46 0" />
      </svg>
      <span v-if="totalUnread > 0" class="bell-badge">{{ totalUnread }}</span>
    </button>

    <teleport to="body">
      <div v-if="open" class="bell-backdrop" @click="open = false" />
      <div
        v-if="open"
        class="bell-panel"
        role="dialog"
        aria-label="Unread chats"
        :style="panelStyle"
      >
        <div class="bell-header">
          <span class="bell-title">Notifications</span>
          <button
            v-if="totalUnread > 0"
            class="bell-mark-all"
            @click="onMarkAll"
          >Mark all read</button>
        </div>
        <div v-if="unreadChats.length === 0" class="bell-empty">
          No unread chats.
        </div>
        <ul v-else class="bell-list">
          <li
            v-for="chat in unreadChats"
            :key="chat.chat_id"
            class="bell-item"
            @click="onOpen(chat.chat_id)"
          >
            <div class="bell-item-row">
              <span class="bell-item-title">{{ chat.title || 'New chat' }}</span>
              <span class="bell-item-time">{{ formatTime(chat.last_activity_at) }}</span>
            </div>
            <div class="bell-item-sub">
              <span class="bell-item-project">{{ projectName(chat.chat_id) }}</span>
            </div>
          </li>
        </ul>
      </div>
    </teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { useProjectStore } from '../stores/projects'
import type { ChatInfo } from '../lib/types'

const store = useProjectStore()
const open = ref(false)
const btnRef = ref<HTMLElement | null>(null)
const panelStyle = ref<Record<string, string>>({})

async function toggle() {
  open.value = !open.value
  if (!open.value) return
  // Position the panel under the bell button, flush to the right edge but
  // clamped inside the viewport (so it works even when the bell sits in the
  // narrow sidebar header).
  await nextTick()
  const btn = btnRef.value
  if (!btn) return
  const rect = btn.getBoundingClientRect()
  const panelWidth = 320
  const margin = 8
  const top = rect.bottom + margin
  let left = rect.right - panelWidth
  if (left < margin) left = margin
  const maxLeft = window.innerWidth - panelWidth - margin
  if (left > maxLeft) left = Math.max(margin, maxLeft)
  panelStyle.value = {
    top: `${top}px`,
    left: `${left}px`,
    width: `${panelWidth}px`,
  }
}

const totalUnread = computed(() =>
  store.chats.reduce(
    (sum, c) => sum + (c.archived ? 0 : store.chatUnread(c.chat_id)),
    0,
  ),
)

const unreadChats = computed<ChatInfo[]>(() =>
  store.chats
    .filter(c => !c.archived && store.chatUnread(c.chat_id) > 0)
    .slice()
    .sort((a, b) => (b.last_activity_at || '').localeCompare(a.last_activity_at || '')),
)

function projectName(chatId: string): string {
  const project = store.projectFor(chatId)
  return project?.name || ''
}

function onOpen(chatId: string) {
  open.value = false
  // switchChat internally calls markRead, so this clears the unread everywhere.
  void store.switchChat(chatId)
}

function onMarkAll() {
  void store.markAllRead()
  open.value = false
}

function formatTime(iso: string | undefined): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const diff = Date.now() - then
  const mins = Math.round(diff / 60_000)
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.round(hours / 24)
  if (days < 7) return `${days}d`
  return new Date(iso).toLocaleDateString()
}
</script>

<style scoped>
.bell-root {
  position: relative;
  display: inline-block;
}

.bell-btn {
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-elev);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 999px;
  cursor: pointer;
  position: relative;
  transition: transform 120ms var(--ease), background 120ms var(--ease);
}
.bell-btn svg {
  width: 18px;
  height: 18px;
}
.bell-btn:hover { background: var(--bg3); }
.bell-btn:active { transform: scale(0.94); }
.bell-btn.has-unread { color: var(--accent, #4c8bf5); }

.bell-badge {
  position: absolute;
  top: 4px;
  right: 4px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--danger, #e06c75);
  color: white;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
}

.bell-backdrop {
  position: fixed;
  inset: 0;
  z-index: 45;
}

.bell-panel {
  position: fixed;
  max-height: 70vh;
  overflow-y: auto;
  background: var(--bg-elev);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.45);
  z-index: 50;
  animation: bell-in 140ms var(--ease);
}

@keyframes bell-in {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

.bell-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
}

.bell-title {
  font-weight: 600;
  font-size: 13px;
}

.bell-mark-all {
  background: none;
  border: none;
  color: var(--fg2);
  font-size: 12px;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 4px;
}
.bell-mark-all:hover { background: var(--bg3); color: var(--fg); }

.bell-empty {
  padding: 18px 14px;
  text-align: center;
  color: var(--fg2);
  font-size: 13px;
}

.bell-list {
  list-style: none;
  margin: 0;
  padding: 4px 0;
}

.bell-item {
  padding: 10px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--border);
  transition: background 120ms var(--ease);
}
.bell-item:last-child { border-bottom: none; }
.bell-item:hover { background: var(--bg3); }

.bell-item-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.bell-item-title {
  font-weight: 500;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.bell-item-time {
  color: var(--fg2);
  font-size: 11px;
  flex-shrink: 0;
}

.bell-item-sub {
  margin-top: 2px;
  font-size: 11px;
  color: var(--fg2);
}

.bell-item-project {
  opacity: 0.8;
}
</style>
