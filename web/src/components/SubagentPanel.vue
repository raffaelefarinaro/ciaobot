<template>
  <ul v-if="subs.length" class="subagent-rows" role="list">
    <li v-for="sub in subs" :key="sub.agent_id" class="subagent-row" :class="sub.status">
      <ActivityGlyph v-if="sub.status === 'running'" kind="working" />
      <span class="subagent-row-name">{{ nameOf(sub) }}</span>
      <span v-if="sub.subagent_type" class="subagent-row-type">{{ sub.subagent_type }}</span>
      <span class="subagent-row-status">{{ statusWord(sub.status) }}<template v-if="duration(sub)"> &middot; {{ duration(sub) }}</template></span>
      <RouterLink
        v-if="chatId"
        class="subagent-row-open"
        :to="subagentPath(chatId, sub.agent_id)"
        :aria-label="`Open ${nameOf(sub)} transcript`"
      >Open &rarr;</RouterLink>
    </li>
  </ul>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { SubagentTranscript } from '../lib/types'
import { shortAgentId, subagentPath } from '../lib/subagentIds'
import { formatDuration } from '../lib/time'
import ActivityGlyph from './ActivityGlyph.vue'

// `chatId` is optional so the rows still render in isolation (tests, and any
// caller that has no route to offer); the link is simply omitted then.
const props = defineProps<{ subagents: SubagentTranscript[]; chatId?: string }>()
const chatId = computed(() => props.chatId || '')

const subs = computed<SubagentTranscript[]>(() => props.subagents || [])

function nameOf(sub: SubagentTranscript): string {
  return sub.description || shortAgentId(sub.agent_id)
}

function statusWord(status: SubagentTranscript['status']): string {
  switch (status) {
    case 'running': return 'running'
    case 'completed': return 'finished'
    case 'failed': return 'failed'
    case 'stopped': return 'stopped'
    default: return ''
  }
}

function duration(sub: SubagentTranscript): string {
  // The provider's own run length, when it can report one without the
  // transcript (an opencode row carries no messages).
  if (typeof sub.duration_ms === 'number') return formatDuration(sub.duration_ms)
  const times = sub.messages
    .map(m => Date.parse(m.timestamp))
    .filter(t => Number.isFinite(t))
  if (times.length < 2) return ''
  return formatDuration(times[times.length - 1] - times[0])
}
</script>

<style scoped>
.subagent-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.subagent-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--fg2);
}

.subagent-row-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.subagent-row-type {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg3);
  flex: 0 0 auto;
}

.subagent-row-status {
  color: var(--fg3);
  flex: 0 0 auto;
}

.running .subagent-row-status { color: var(--accent2); }
.failed .subagent-row-status { color: var(--error); }

.subagent-row-open {
  margin-left: auto;
  flex: 0 0 auto;
  color: var(--fg2);
  text-decoration: none;
}

.subagent-row-open:hover { text-decoration: underline; }
.subagent-row-open:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

@media (pointer: coarse) {
  .subagent-row-open {
    min-height: var(--touch);
    display: inline-flex;
    align-items: center;
  }
}
</style>
