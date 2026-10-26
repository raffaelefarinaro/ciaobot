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
    ><ActivityGlyph kind="working" /></span>
    <span
      v-else-if="primarySignal === 'agents'"
      class="chat-signal chat-signal--agents"
      :title="agentsTitle"
      :aria-label="agentsTitle"
    >
      <ActivityGlyph kind="agent" />
      <span v-if="density === 'card' && agentCount > 1" class="chat-signal-count">{{ agentCount }}</span>
    </span>
    <span
      v-else-if="primarySignal === 'retry'"
      class="chat-signal chat-signal--retry"
      title="Retry scheduled"
      aria-label="Retry scheduled"
    >↻ retry</span>
    <!-- Not on the ladder: a run can outlive everything else on the row (a dev
         server goes for hours), so it shows beside needs-you, working and agents
         instead of hiding behind them. -->
    <span
      v-if="runCount > 0"
      class="chat-signal chat-signal--runs"
      :title="runsTitle"
      :aria-label="runsTitle"
    >
      <ActivityGlyph kind="run" />
      <span v-if="density === 'card' && runCount > 1" class="chat-signal-count">{{ runCount }}</span>
    </span>
    <span
      v-if="intervalSummary"
      class="chat-signal chat-signal--interval"
      :class="{ stopped: !intervalSummary.running }"
      :title="intervalTitle"
      :aria-label="intervalTitle"
    >↻</span>

    <!-- Not beside needs-you: that mark already asks for the chat to be
         opened, and a second one next to it reads as a stutter. -->
    <span
      v-if="unread && primarySignal !== 'needs'"
      class="chat-signal chat-signal--unread"
      title="Unread chat"
      aria-label="Unread chat"
    >
      <svg width="14" height="9" viewBox="0 0 16 10" aria-hidden="true">
        <path d="M1 5C4 .8 12 .8 15 5C12 9.2 4 9.2 1 5Z" fill="none" stroke="currentColor" stroke-width="1.6" />
        <circle cx="8" cy="5" r="2.2" fill="currentColor" />
      </svg>
    </span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import type { WorkspaceAccent } from '../lib/workspaceColors'
import { backgroundRunLabel } from '../lib/backgroundRuns'
import { isShellRow } from '../lib/subagentIds'
import ActivityGlyph from './ActivityGlyph.vue'

const props = withDefaults(defineProps<{
  chatId: string
  density?: 'card' | 'row'
  hue?: WorkspaceAccent
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
  store.runningSubagentsFor(props.chatId).filter(sub => !isShellRow(sub)).length,
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

// Unread is a separate static notification eye. The per-chat value is binary,
// so the numeric counts remain reserved for project/workspace rollups.
const primarySignal = computed<'needs' | 'memory' | 'working' | 'agents' | 'retry' | null>(() => {
  if (needsInput.value) return 'needs'
  if (memoryAttention.value) return 'memory'
  if (working.value) return 'working'
  if (agentCount.value > 0) return 'agents'
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
  width: 9px;
  height: 9px;
  border-radius: 2px;
  background: var(--accent);
}

/* Unread is an eye in the row's accent, still: status takes the accent and
   shape carries the meaning, because a workspace accent can sit next to the
   semantic error red. */
.chat-signal--unread {
  color: var(--accent);
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

/* Working, agents and runs draw ActivityGlyph; the gap keeps a count clear of it. */
.chat-signal--working,
.chat-signal--agents,
.chat-signal--runs {
  gap: var(--space-1);
}

/* Only shown for more than one agent or run, where the number is the point. */
.chat-signal-count {
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 700;
}

/* A run's count takes the accent like its diamond; the diamond's shape is what
   says "run". */
.chat-signal--runs .chat-signal-count { color: var(--accent); }

/* A small mono tag, so it no longer shares the interval's ↻ glyph. */
.chat-signal--retry {
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 400;
  line-height: 16px;
  padding: 0 5px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
}

/* A pass that stopped and needs the owner. The accent like every status mark;
   the ✦ shape is what separates it from the working grid. */
.chat-signal--memory {
  color: var(--accent);
  font-size: var(--text-sm);
  line-height: 1;
}

/* An attribute of the chat, not a state, so it stays muted. */
.chat-signal--interval {
  color: var(--fg3);
  font-size: var(--text-lg);
  font-weight: 400;
}

/* Paused must still differ from running without colour. */
.chat-signal--interval.stopped {
  opacity: 0.55;
}
</style>
