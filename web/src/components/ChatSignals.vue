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
      v-else-if="primarySignal === 'delegates'"
      class="chat-signal chat-signal--delegates"
      title="Sub-chats are still working"
      aria-label="Sub-chats are still working"
    >
      <span class="activity-spinner activity-spinner--delegates" aria-hidden="true" />
      <span v-if="density === 'card'" class="chat-signal-label">sub-chats working</span>
    </span>
    <span
      v-else-if="primarySignal === 'retry'"
      class="chat-signal chat-signal--retry"
      title="Retry scheduled"
      aria-label="Retry scheduled"
    >↻</span>
    <!-- Ranked last on purpose: post-archive work never needs the user, so it
         must never outrank a signal that does. In practice it cannot collide —
         an archived chat has no turn, no agents and no retry. -->
    <span
      v-else-if="primarySignal === 'tidying'"
      class="chat-signal chat-signal--tidying"
      :title="tidyingTitle"
      :aria-label="tidyingTitle"
    >
      <span class="tidy-spinner" aria-hidden="true" />
      <span v-if="density === 'card'" class="chat-signal-tidy-label">{{ tidyingLabel }}</span>
    </span>

    <span
      v-if="loopSummary"
      class="chat-signal chat-signal--loop"
      :class="{ stopped: !loopSummary.running }"
      :title="loopTitle"
      :aria-label="loopTitle"
    >↻</span>

    <span
      v-if="unread"
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
import { postprocessLabel } from '../lib/postprocessView'
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
const hasActiveDelegates = computed(() => store.chatHasActiveDelegates(props.chatId))
const retryPending = computed(() => store.chats.find(c => c.chat_id === props.chatId)?.retry?.status === 'pending')
const unread = computed(() => store.chatUnread(props.chatId) > 0)

// Post-archive tidy-up: insights extraction and friends, running after the
// chat was archived. Deliberately the quietest thing this component can draw.
const tidying = computed(() => store.chatIsPostprocessing(props.chatId))
const tidyingLabel = computed(() => postprocessLabel(store.chatPostprocess(props.chatId)))
const tidyingTitle = computed(() => `Ciaobot is ${tidyingLabel.value || 'tidying up'}`)

// Unread is a separate static notification dot. The per-chat value is binary,
// so the numeric counts remain reserved for project/workspace rollups.
const primarySignal = computed<'needs' | 'working' | 'agents' | 'delegates' | 'retry' | 'tidying' | null>(() => {
  if (needsInput.value) return 'needs'
  if (working.value) return 'working'
  if (hasBackgroundAgents.value) return 'agents'
  if (hasActiveDelegates.value) return 'delegates'
  if (props.density === 'row' && retryPending.value) return 'retry'
  if (tidying.value) return 'tidying'
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

/* Unread is a notification state rather than workspace activity, so it keeps
   the semantic error color instead of inheriting the workspace accent. */
.chat-signal--unread {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--error, #f44336);
  box-shadow: 0 0 4px var(--error, #f44336);
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
.chat-signal--delegates {
  gap: var(--space-1);
}

/* Sub-chats working, not this chat directly: a hollow ring instead of the
   solid dot used for .activity-spinner, so a supervisor whose delegate is
   busy reads as related-but-distinct from the chat's own direct activity. */
.activity-spinner--delegates {
  box-sizing: border-box;
  background: transparent;
  border: 2px solid var(--accent);
  box-shadow: none;
}

.activity-spinner--delegates::before {
  background: transparent;
  border: 1px solid var(--accent);
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

/* Post-archive tidy-up. The accent pulse above means "your turn is live" or
   "agents are working" — things that may want you. This never wants you, so it
   gets the muted foreground instead of the accent, and breathes slower and
   shallower than .activity-spinner (2.6s vs 1.1s). Keeping it in the accent
   colour would have taught the eye to discount the accent. */
.chat-signal--tidying { gap: var(--space-1); }

.tidy-spinner {
  position: relative;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--fg3);
  animation: chat-signal-breathe 2.6s ease-in-out infinite;
  flex-shrink: 0;
}

/* A hollow expanding ring, not a filled halo: less visual weight than the
   accent version even at the same size. */
.tidy-spinner::before {
  content: "";
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  border: 1px solid var(--fg3);
  opacity: 0;
  animation: chat-signal-ring-slow 2.6s ease-out infinite;
  pointer-events: none;
}

.chat-signal-tidy-label {
  font-size: var(--text-xs);
  color: var(--fg3);
  white-space: nowrap;
}

@keyframes chat-signal-breathe {
  0%, 100% { opacity: 0.35; transform: scale(0.8); }
  50%      { opacity: 0.9;  transform: scale(1); }
}

@keyframes chat-signal-ring-slow {
  0%   { transform: scale(0.7); opacity: 0.5; }
  100% { transform: scale(1.9); opacity: 0; }
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
  .activity-spinner::before,
  .tidy-spinner,
  .tidy-spinner::before {
    animation: none;
  }

  .activity-spinner { opacity: 1; }
  .activity-spinner::before { opacity: 0.3; }
  /* Still dimmer than the accent dot without motion to separate them. */
  .tidy-spinner { opacity: 0.75; }
  .tidy-spinner::before { opacity: 0; }
}
</style>
