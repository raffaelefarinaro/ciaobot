<template>
  <span
    class="chat-signals"
    :class="`chat-signals--${density}`"
    :data-workspace-color="hue"
  >
    <span
      v-if="primarySignal === 'needs'"
      class="chat-signal chat-signal--needs"
      title="Needs your answer"
      aria-label="Needs your answer"
    >
      <span v-if="density === 'card'" class="chat-signal-label">needs you</span>
    </span>
    <span
      v-else-if="primarySignal === 'working'"
      class="chat-signal chat-signal--working"
      title="Working"
      aria-label="Working"
    ><span class="activity-spinner" aria-hidden="true" /></span>
    <span
      v-else-if="primarySignal === 'agents'"
      class="chat-signal chat-signal--agents"
      :title="`${backgroundCount} background agents running`"
      :aria-label="`${backgroundCount} background agents running`"
    >
      <span class="activity-spinner" aria-hidden="true" />
      <span v-if="density === 'card' && backgroundCount > 1" class="chat-signal-count">{{ backgroundCount }}</span>
    </span>
    <span
      v-else-if="primarySignal === 'retry'"
      class="chat-signal chat-signal--retry"
      title="Retry scheduled"
      aria-label="Retry scheduled"
    >↻</span>

    <span
      v-if="loopSummary"
      class="chat-signal chat-signal--loop"
      :class="{ stopped: !loopSummary.running }"
      :title="loopTitle"
      :aria-label="loopTitle"
    >↻</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import type { WorkspaceColorId } from '../lib/workspaceColors'

const props = withDefaults(defineProps<{
  chatId: string
  density?: 'card' | 'row'
  hue?: WorkspaceColorId
}>(), {
  density: 'row',
})

const store = useProjectStore()
const taskStore = useTaskStore()

const needsInput = computed(() => store.chatNeedsInput(props.chatId))
const working = computed(() => store.isChatStreaming(props.chatId))
const backgroundCount = computed(() => Number(store.backgroundAgents[props.chatId] || 0))
const hasBackgroundAgents = computed(() => store.chatHasBackgroundAgents(props.chatId))
const retryPending = computed(() => store.chats.find(c => c.chat_id === props.chatId)?.retry?.status === 'pending')

// Unread deliberately does not render here. Chat-level unread is title weight
// in the parent (chatUnread(id) > 0); only project/workspace rollups get digits.
const primarySignal = computed<'needs' | 'working' | 'agents' | 'retry' | null>(() => {
  if (needsInput.value) return 'needs'
  if (working.value) return 'working'
  if (hasBackgroundAgents.value) return 'agents'
  if (props.density === 'row' && retryPending.value) return 'retry'
  return null
})

const loopSummary = computed(() => taskStore.loopsByChat.get(props.chatId) || null)
const loopTitle = computed(() => {
  if (!loopSummary.value) return ''
  const label = loopSummary.value.count > 1 ? `${loopSummary.value.count} loops` : 'A loop'
  return loopSummary.value.running
    ? `${label} running in this chat`
    : `${label} attached to this chat (stopped)`
})
</script>

<style scoped>
.chat-signals {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
  min-width: 0;
}

.chat-signal {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  line-height: 1;
}

.chat-signal--needs {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}

/* A small squared tag, not a pill. The design this came from used a 4px radius
   deliberately: the pill shape reads as a count badge, and counts mean something
   else in this vocabulary. */
.chat-signals--card .chat-signal--needs {
  width: auto;
  min-height: 20px;
  padding: 0 var(--space-2);
  border-radius: var(--radius-xs);
  color: var(--bg);
}

:global(:root.theme-light) .chat-signals--card .chat-signal--needs {
  color: var(--fg);
}

.chat-signal-label {
  font-size: var(--text-xs);
  font-weight: 700;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

/* Working reuses the chat transcript's own activity pulse rather than a
   bordered pill: a solid accent dot with a halo and an expanding ring. The
   outlined ring it replaced was nearly invisible at sidebar size, and the
   card variant carried a redundant "working" label under a tier heading that
   already said working. Kept in sync with .activity-spinner in ChatPanel.vue —
   if that changes, change this. */
.chat-signal--working,
.chat-signal--agents {
  gap: var(--space-1);
}

.activity-spinner {
  position: relative;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 4px var(--accent);
  animation: chat-signal-pulse 1.1s ease-in-out infinite;
  flex-shrink: 0;
}

.activity-spinner::before {
  content: "";
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  background: var(--accent);
  opacity: 0.45;
  animation: chat-signal-ring 1.1s ease-out infinite;
  pointer-events: none;
}

/* Only shown for more than one agent, where the number is the whole point. */
.chat-signal-count {
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 700;
}

.chat-signal--retry {
  color: var(--warning);
  font-size: var(--text-lg);
  font-weight: 700;
}

.chat-signal--loop {
  color: var(--accent);
  font-size: var(--text-lg);
  font-weight: 700;
}

.chat-signal--loop.stopped {
  color: var(--fg3);
}

@keyframes chat-signal-pulse {
  0%, 100% { transform: scale(0.55); opacity: 0.35; }
  50% { transform: scale(1); opacity: 1; }
}

@keyframes chat-signal-ring {
  0% { transform: scale(0.6); opacity: 0.45; }
  100% { transform: scale(1.6); opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .activity-spinner,
  .activity-spinner::before {
    animation: none;
  }

  .activity-spinner { opacity: 1; }
  .activity-spinner::before { opacity: 0.3; }
}
</style>
