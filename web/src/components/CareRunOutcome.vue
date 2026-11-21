<script setup lang="ts">
// The last care run's derived outcome, shown under a Workspace care routine's
// "Last run" row. Read-only: the route answers for the chat this row names and
// nothing else, so a stale answer for a previous chat is dropped, not shown.
import { ref, watch } from 'vue'
import { careRunCountsLine, fetchCareRun, type CareRunOutcome } from '../lib/careRun'

const props = defineProps<{ scheduleId: string; chatId: string }>()

type Phase = 'loading' | 'ready' | 'error'
const phase = ref<Phase>('loading')
const run = ref<CareRunOutcome | null>(null)
// Each load takes a ticket; an answer whose ticket is no longer the newest is dropped.
let ticket = 0

async function load(): Promise<void> {
  const mine = ++ticket
  phase.value = 'loading'
  try {
    const outcome = await fetchCareRun(props.scheduleId)
    if (mine !== ticket) return
    run.value = outcome
    phase.value = 'ready'
  } catch {
    if (mine !== ticket) return
    run.value = null
    phase.value = 'error'
  }
}

watch(
  () => [props.scheduleId, props.chatId] as const,
  () => { void load() },
  { immediate: true },
)
</script>

<template>
  <div class="care-run">
    <p v-if="phase === 'loading'" class="care-run-state" role="status">Checking the outcome of this run…</p>

    <div v-else-if="phase === 'error'" class="care-run-state care-run-state--error" role="alert">
      <span>Could not load the outcome of this run.</span>
      <button type="button" class="care-run-retry" @click="load">Retry</button>
    </div>

    <p v-else-if="!run" class="care-run-state">No outcome recorded for this run yet.</p>

    <template v-else>
      <p class="care-run-counts">{{ careRunCountsLine(run) }}</p>
      <p v-if="!run.counts_complete" class="care-run-incomplete">
        Incomplete: some work has no confirmed result yet.
      </p>
      <ul v-if="run.reasons.length" class="care-run-reasons" aria-label="Notes on this run">
        <li v-for="(reason, index) in run.reasons" :key="index">{{ reason }}</li>
      </ul>
    </template>
  </div>
</template>

<style scoped>
.care-run {
  display: grid;
  gap: var(--space-1);
  font-size: var(--text-sm);
  color: var(--fg);
}

.care-run-state {
  margin: 0;
  color: var(--fg2);
}

.care-run-state--error {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  color: var(--warning);
}

.care-run-retry {
  min-height: var(--touch);
  padding: 0 var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  font: inherit;
  cursor: pointer;
}

.care-run-retry:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.care-run-counts {
  margin: 0;
  font-family: var(--font-mono);
}

.care-run-incomplete {
  margin: 0;
  color: var(--warning);
}

.care-run-reasons {
  margin: 0;
  padding-left: var(--space-4);
  color: var(--fg2);
  overflow-wrap: anywhere;
}
</style>
