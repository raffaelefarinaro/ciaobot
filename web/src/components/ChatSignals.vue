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
    >
      <span class="chat-signal-core chat-signal-core--pulse" aria-hidden="true" />
      <span v-if="density === 'card'" class="chat-signal-label">working</span>
    </span>
    <span
      v-else-if="primarySignal === 'agents'"
      class="chat-signal chat-signal--agents"
      :title="`${backgroundCount} background agents running`"
      :aria-label="`${backgroundCount} background agents running`"
    >
      <span class="chat-signal-core chat-signal-core--pulse" aria-hidden="true" />
      <span v-if="density === 'card'" class="chat-signal-label">{{ backgroundCount }} agents</span>
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

.chat-signals--card .chat-signal--needs {
  width: auto;
  min-height: 20px;
  padding: 0 7px;
  border-radius: 10px;
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

.chat-signal--working,
.chat-signal--agents {
  width: 10px;
  height: 10px;
  border: 1.5px solid var(--accent);
  border-radius: 50%;
}

.chat-signals--card .chat-signal--working,
.chat-signals--card .chat-signal--agents {
  width: auto;
  height: 20px;
  gap: 5px;
  padding: 0 7px 0 4px;
  border-radius: 10px;
}

.chat-signal-core {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--accent);
}

.chat-signal-core--pulse {
  animation: chat-signal-pulse 1.1s ease-in-out infinite;
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
  0%, 100% { opacity: 0.65; transform: scale(0.75); }
  50% { opacity: 1; transform: scale(1); }
}

@media (prefers-reduced-motion: reduce) {
  .chat-signal-core--pulse {
    animation: none;
    opacity: 0.65;
  }
}
</style>
