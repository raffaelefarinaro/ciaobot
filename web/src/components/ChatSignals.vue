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
      <ActivityGlyph v-else kind="alert" />
    </span>
    <span
      v-else-if="primarySignal === 'memory'"
      class="chat-signal chat-signal--memory"
      title="Memory pass needs you"
      aria-label="Memory pass needs you"
    ><ActivityGlyph kind="alert" /></span>
    <span
      v-else-if="primarySignal === 'working'"
      class="chat-signal chat-signal--working"
      :title="workingTitle"
      :aria-label="workingTitle"
    ><ActivityGlyph kind="working" /></span>
    <!-- A retry follows a failed run, or was scheduled by the agent from a
         turn that succeeded; the alert mark is the same for both, the label says which. -->
    <span
      v-else-if="primarySignal === 'retry'"
      class="chat-signal chat-signal--retry"
      :title="retryTitle"
      :aria-label="retryTitle"
    ><ActivityGlyph kind="alert" /></span>
    <span
      v-if="scheduleSummary"
      class="chat-signal chat-signal--schedule"
      :class="{ stopped: !scheduleSummary.running }"
      role="img"
      :title="scheduleTitle"
      :aria-label="scheduleTitle"
    ><ScheduleGlyph /></span>

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
import { isShellRow } from '../lib/subagentIds'
import ActivityGlyph from './ActivityGlyph.vue'
import ScheduleGlyph from './ScheduleGlyph.vue'

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
// Subagents are the chat's work too: they show as working, not a mark of
// their own, and only the label says who is doing it.
const workingTitle = computed(() => {
  if (working.value || agentCount.value === 0) return 'Working'
  return agentCount.value === 1 ? '1 agent running' : `${agentCount.value} agents running`
})
const retryPending = computed(() => store.chats.find(c => c.chat_id === props.chatId)?.retry?.status === 'pending')
// An agent can schedule a retry from a turn that succeeded, so only say
// "failed" when there is an error on the chat.
const retryTitle = computed(() =>
  store.chats.find(c => c.chat_id === props.chatId)?.last_error
    ? 'Run failed, retry scheduled'
    : 'Retry scheduled',
)
const unread = computed(() => store.chatUnread(props.chatId) > 0)

// A memory pass that ended unclean. Ranked below `needs` and above `working`:
// it is the one background job whose unfinished state is the owner's problem,
// so it must outrank the "still going" signals that are not asking anything.
const memoryAttention = computed(() => store.memoryPassNeedsAttention(props.chatId))

// Unread is a separate static notification eye. The per-chat value is binary,
// so the numeric counts remain reserved for project/workspace rollups.
const primarySignal = computed<'needs' | 'memory' | 'working' | 'retry' | null>(() => {
  if (needsInput.value) return 'needs'
  if (memoryAttention.value) return 'memory'
  if (working.value || agentCount.value > 0) return 'working'
  if (props.density === 'row' && retryPending.value) return 'retry'
  return null
})

// Automations bound to this chat, of any cadence -- the "this chat runs on a
// schedule" clock, an attribute like the task and code marks.
const scheduleSummary = computed(
  () => taskStore.schedulesByChat.get(props.chatId) || null,
)
const scheduleTitle = computed(() => {
  if (!scheduleSummary.value) return ''
  const label = scheduleSummary.value.count > 1
    ? `${scheduleSummary.value.count} automations`
    : 'An automation'
  return scheduleSummary.value.running
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
  min-height: 20px;
  background: var(--accent);
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

/* Working draws ActivityGlyph; the gap keeps a label clear of it. */
.chat-signal--working {
  gap: var(--space-1);
}

/* An attribute of the chat, not a state, so it stays muted like the task and
   code marks. */
.chat-signal--schedule {
  color: var(--fg3);
}

/* Paused must still differ from running without colour. */
.chat-signal--schedule.stopped {
  opacity: 0.55;
}
</style>
