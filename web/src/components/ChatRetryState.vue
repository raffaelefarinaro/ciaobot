<script setup lang="ts">
/**
 * The retry half of a failed turn's error card: when the next try is due, how
 * many have run, and the three things the reader can do about it. It draws
 * inside InjectedPromptCard's actions slot so the failure, its retry state and
 * its actions read as one card.
 */
import { computed } from 'vue'
import ProviderSignInHint from './ProviderSignInHint.vue'
import { isProviderAuthError } from '../lib/errorAttribution'
import { formatTime } from '../lib/time'
import type { ChatRetryInfo } from '../lib/types'

const props = defineProps<{
  retry: ChatRetryInfo
  provider: string
  streaming: boolean
}>()
defineEmits<{ (e: 'continue'): void; (e: 'try-now'): void; (e: 'stop'): void }>()

// Quota retries back off from an hour (1h, 2h, 4h, then 8h), weekly/monthly
// limits wait a day; connection, startup and auth retries 30s.
const title = computed(() => {
  if (isProviderAuthError(props.retry.last_error)) return 'Signed out — retrying this turn'
  const seconds = props.retry.interval_seconds || 0
  if (seconds && seconds < 3600) return 'Retrying this turn shortly'
  if (seconds >= 86400) return 'Retrying this turn once a day'
  if (seconds > 3600) return 'Retrying this turn every few hours'
  return 'Retrying this turn every hour'
})

const nextTry = computed(() => {
  if (!props.retry.next_at) return ''
  const d = new Date(props.retry.next_at)
  return Number.isNaN(d.getTime()) ? '' : formatTime(d.toISOString())
})
</script>

<template>
  <div class="retry-state">
    <div class="retry-state-text">
      <div class="retry-state-title">{{ title }}</div>
      <div class="retry-state-meta">
        <span v-if="nextTry">Next try {{ nextTry }}</span>
        <span v-if="retry.attempts"> · {{ retry.attempts }} attempt{{ retry.attempts === 1 ? '' : 's' }}</span>
      </div>
    </div>
    <div class="retry-state-actions">
      <button type="button" class="btn-small" :disabled="streaming" @click="$emit('continue')">Continue with...</button>
      <button type="button" class="btn-small" :disabled="streaming" @click="$emit('try-now')">Try now</button>
      <button type="button" class="btn-small" @click="$emit('stop')">Stop trying</button>
    </div>
    <ProviderSignInHint
      v-if="isProviderAuthError(retry.last_error)"
      :provider="provider"
      action-label="Try now"
      :action-disabled="streaming"
      @action="$emit('try-now')"
    />
  </div>
</template>

<style scoped>
.retry-state { display: flex; flex-direction: column; gap: var(--space-2); min-width: 0; }
.retry-state-title { color: var(--fg); font-size: var(--text-sm); font-weight: 600; }
.retry-state-meta { color: var(--fg2); font-size: var(--text-xs); margin-top: var(--space-1); }
.retry-state-actions { display: flex; flex-wrap: wrap; gap: var(--space-2); }
@media (pointer: coarse) {
  .retry-state-actions .btn-small { min-height: var(--touch); }
}
</style>
