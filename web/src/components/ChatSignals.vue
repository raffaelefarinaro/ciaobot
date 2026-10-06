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
      v-else-if="primarySignal === 'memory'"
      class="chat-signal chat-signal--memory"
      title="Memory pass needs you"
      aria-label="Memory pass needs you"
    >✦</span>
    <span
      v-else-if="primarySignal === 'working'"
      class="chat-signal chat-signal--working"
      title="Working"
      aria-label="Working"
    ><span class="activity-spinner" aria-hidden="true" /></span>
    <span
      v-else-if="primarySignal === 'agents'"
      class="chat-signal chat-signal--agents"
      :title="agentsTitle"
      :aria-label="agentsTitle"
    >
      <span class="activity-spinner" aria-hidden="true" />
      <span v-if="density === 'card' && agentCount > 1" class="chat-signal-count">{{ agentCount }}</span>
    </span>
    <!-- Ranked below agents: both mean "still going", but an agent has a
         transcript to open and a run has only a count and a log, so when a
         chat has both the agent is the more useful thing to point at. Its own
         state rather than folded into `agents`, because that label counts
         agents and a background run is not one. -->
    <span
      v-else-if="primarySignal === 'runs'"
      class="chat-signal chat-signal--runs"
      :title="runsTitle"
      :aria-label="runsTitle"
    >
      <span class="activity-spinner" aria-hidden="true" />
      <span v-if="density === 'card' && runCount > 1" class="chat-signal-count">{{ runCount }}</span>
    </span>
    <span
      v-else-if="primarySignal === 'retry'"
      class="chat-signal chat-signal--retry"
      title="Retry scheduled"
      aria-label="Retry scheduled"
    >↻</span>
    <span
      v-if="intervalSummary"
      class="chat-signal chat-signal--interval"
      :class="{ stopped: !intervalSummary.running }"
      :title="intervalTitle"
      :aria-label="intervalTitle"
    >↻</span>

    <!-- Not beside needs-you: that dot already asks for the chat to be
         opened, and a second one next to it reads as a stutter. -->
    <span
      v-if="unread && primarySignal !== 'needs'"
      class="chat-signal chat-signal--unread"
      title="Unread chat"
      aria-label="Unread chat"
    />
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import type { WorkspaceColorId } from '../lib/workspaceColors'
import { backgroundRunLabel } from '../lib/backgroundRuns'

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
// Subagents working for this chat. Two sources for the same fact, on
// different clocks: the watcher's own count (pushed over /ws/events) and the
// sidebar's running-subagent poll. Take the larger rather than picking a side,
// so the row never reads idle just because one of them has not ticked yet.
const agentCount = computed(() => Math.max(
  Number(store.backgroundAgents[props.chatId] || 0),
  store.runningSubagentsFor(props.chatId).length,
))
const agentsTitle = computed(() =>
  agentCount.value === 1 ? '1 agent running' : `${agentCount.value} agents running`,
)
// Tracked background command runs. One run is named, the way the composer's
// run line names it, so hovering the row says what is going.
const runs = computed(() => store.backgroundRuns[props.chatId] || [])
const runCount = computed(() => runs.value.length)
const runsTitle = computed(() =>
  runCount.value === 1
    ? `Background run: ${backgroundRunLabel(runs.value[0])}`
    : `${runCount.value} background runs`,
)
const retryPending = computed(() => store.chats.find(c => c.chat_id === props.chatId)?.retry?.status === 'pending')
const unread = computed(() => store.chatUnread(props.chatId) > 0)

// A memory pass that ended unclean. Ranked below `needs` and above `working`:
// it is the one background job whose unfinished state is the owner's problem,
// so it must outrank the "still going" signals that are not asking anything.
const memoryAttention = computed(() => store.memoryPassNeedsAttention(props.chatId))

// Unread is a separate static notification dot. The per-chat value is binary,
// so the numeric counts remain reserved for project/workspace rollups.
const primarySignal = computed<'needs' | 'memory' | 'working' | 'agents' | 'runs' | 'retry' | null>(() => {
  if (needsInput.value) return 'needs'
  if (memoryAttention.value) return 'memory'
  if (working.value) return 'working'
  if (agentCount.value > 0) return 'agents'
  if (runCount.value > 0) return 'runs'
  if (props.density === 'row' && retryPending.value) return 'retry'
  return null
})

// Interval automations bound to this chat -- the "this chat re-runs itself"
// marker. Distinct from a time-of-day schedule, which is not a property of the
// chat in the same way.
const intervalSummary = computed(
  () => taskStore.intervalsByChat.get(props.chatId) || null,
)
const intervalTitle = computed(() => {
  if (!intervalSummary.value) return ''
  const label = intervalSummary.value.count > 1
    ? `${intervalSummary.value.count} interval automations`
    : 'An interval automation'
  return intervalSummary.value.running
    ? `${label} running in this chat`
    : `${label} attached to this chat (paused)`
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

/* Unread is a notification state rather than workspace activity, so it keeps
   the semantic error color instead of inheriting the workspace accent. */
.chat-signal--unread {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--error, #f44336);
  box-shadow: 0 0 4px var(--error, #f44336);
}

/* Card headings top-align their row so a two-line title can wrap, which
   leaves this box only as tall as its dot — the dot then rides high next to
   the meta text. Match the title's first line (1.35 x text-sm, see
   .home-chat-title in HomeRecentChats.vue) so the dot centers on the row. */
.chat-signals--card {
  min-height: calc(1.35 * var(--text-sm));
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
.chat-signal--agents,
.chat-signal--runs {
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

/* Only shown for more than one agent or run, where the number is the point. */
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

/* A pass that stopped and needs the owner. The warning colour, not the accent:
   the accent pulse below already means "your turn is live", and a glyph that
   borrows it would say the opposite of what this says. */
.chat-signal--memory {
  color: var(--warning);
  font-size: var(--text-sm);
  line-height: 1;
}

.chat-signal--interval {
  color: var(--accent);
  font-size: var(--text-lg);
  font-weight: 700;
}

.chat-signal--interval.stopped {
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
