<script setup lang="ts">
// The care run behind a schedule's last dispatch, shown under a Workspace care
// routine's "Last run" row. Read-only and bound to one exact dispatch id: the
// engine answers pending, superseded, ambiguous or unavailable instead of
// guessing, and each of those is said in words here. Nothing reads as success
// from the job status alone; the counts and the per-item outcomes speak.
import { computed, ref, watch } from 'vue'
import {
  careRunCountsLine,
  fetchCareRunReport,
  isNoOp,
  outstandingGroups,
  reviewLink,
  settledLine,
  weeklyLine,
  type CareRunReport,
} from '../lib/careRun'

const props = defineProps<{ scheduleId: string; chatId: string; dispatchId: string; workspace: string }>()

type Phase = 'loading' | 'ready' | 'error'
const phase = ref<Phase>('loading')
const report = ref<CareRunReport | null>(null)
// Each load takes a ticket; an answer whose ticket is no longer the newest is dropped.
let ticket = 0

async function load(): Promise<void> {
  const mine = ++ticket
  phase.value = 'loading'
  try {
    const answer = await fetchCareRunReport(props.scheduleId, props.dispatchId)
    if (mine !== ticket) return
    report.value = answer
    phase.value = 'ready'
  } catch {
    if (mine !== ticket) return
    report.value = null
    phase.value = 'error'
  }
}

watch(
  () => [props.scheduleId, props.chatId, props.dispatchId] as const,
  () => { void load() },
  { immediate: true },
)

const outcomes = computed(() => report.value?.outcomes ?? [])
const groups = computed(() => outstandingGroups(outcomes.value))
const settled = computed(() => settledLine(outcomes.value))
const noOp = computed(() => isNoOp(outcomes.value))
const reasons = computed(() => report.value?.care_run?.reasons ?? [])
const incomplete = computed(() => report.value?.care_run?.counts_complete === false)
</script>

<template>
  <div class="care-run">
    <p v-if="phase === 'loading'" class="care-run-state" role="status">Checking the outcome of this run…</p>

    <div v-else-if="phase === 'error'" class="care-run-state care-run-state--error" role="alert">
      <span>Could not load the outcome of this run.</span>
      <button type="button" class="care-run-retry" @click="load">Retry</button>
    </div>

    <p v-else-if="report?.state === 'pending'" class="care-run-state">
      This run is still running, or its outcome is not recorded yet.
    </p>

    <p v-else-if="report?.state === 'superseded'" class="care-run-state">
      A newer run replaced this one, so no outcome is shown for it.
    </p>

    <p v-else-if="report?.state === 'ambiguous'" class="care-run-state care-run-state--warn">
      The records for this run disagree, so no outcome is shown.
    </p>

    <div v-else-if="report?.state === 'unavailable'" class="care-run-state care-run-state--warn">
      <span>The record of this run cannot be read right now.</span>
      <button type="button" class="care-run-retry" @click="load">Retry</button>
    </div>

    <template v-else-if="report?.counts">
      <p v-if="report.job_status === 'error'" class="care-run-failed">This run failed.</p>
      <p v-else-if="report.job_status === 'skipped'" class="care-run-state">This run was skipped.</p>
      <p v-else-if="noOp" class="care-run-noop">No change needed.</p>

      <p class="care-run-counts">{{ careRunCountsLine(report.counts) }}</p>
      <p v-if="incomplete" class="care-run-incomplete">
        Incomplete: some work has no confirmed result yet.
      </p>
      <p v-if="settled" class="care-run-settled">{{ settled }}</p>

      <p v-if="!report.report_available" class="care-run-state">
        Per-item detail was not recorded for this run.
      </p>

      <section v-for="group in groups" :key="group.status" class="care-run-group" :data-status="group.status">
        <h4 class="care-run-group-title">{{ group.label }} ({{ group.items.length }})</h4>
        <ul class="care-run-items">
          <li v-for="(item, index) in group.items" :key="index">
            <span class="care-run-detail">{{ item.detail || 'No reason recorded.' }}</span>
            <span class="care-run-key">{{ item.item_key }}</span>
            <router-link
              v-if="reviewLink(workspace, item.item_key)"
              class="care-run-link"
              :to="reviewLink(workspace, item.item_key)!"
            >Open in Memory Review</router-link>
          </li>
        </ul>
      </section>

      <ul v-if="reasons.length" class="care-run-reasons" aria-label="Notes on this run">
        <li v-for="(reason, index) in reasons" :key="index">{{ reason }}</li>
      </ul>

      <p v-if="report.weekly" class="care-run-weekly">{{ weeklyLine(report.weekly.marker) }}</p>

      <router-link v-if="chatId" class="care-run-link" :to="`/chat/${chatId}`">Open the run chat</router-link>
    </template>
  </div>
</template>

<style scoped>
.care-run {
  display: grid;
  gap: var(--space-1);
  font-size: var(--text-sm);
  color: var(--fg);
  min-width: 0;
}

.care-run p { margin: 0; }

.care-run-state {
  color: var(--fg2);
}

.care-run-state--warn,
.care-run-state--error {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  color: var(--warning);
}

.care-run-retry {
  min-height: 30px;
  padding: 0 var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  font: inherit;
  cursor: pointer;
}

.care-run-retry:focus-visible,
.care-run-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.care-run-failed {
  color: var(--error-text);
  font-weight: 600;
}

.care-run-noop {
  color: var(--fg2);
}

.care-run-counts {
  font-family: var(--font-mono);
  overflow-wrap: anywhere;
}

.care-run-incomplete {
  color: var(--warning);
}

.care-run-settled,
.care-run-weekly {
  color: var(--fg2);
}

.care-run-group {
  display: grid;
  gap: var(--space-1);
  margin-top: var(--space-1);
}

.care-run-group-title {
  margin: 0;
  font-size: var(--text-sm);
  font-weight: 600;
}

.care-run-group[data-status='failed'] .care-run-group-title,
.care-run-group[data-status='conflict'] .care-run-group-title {
  color: var(--error-text);
}

.care-run-items,
.care-run-reasons {
  margin: 0;
  padding-left: var(--space-4);
  color: var(--fg2);
  overflow-wrap: anywhere;
}

.care-run-items li {
  display: grid;
  gap: 2px;
}

.care-run-key {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg2);
}

.care-run-link {
  display: inline-flex;
  align-items: center;
  width: fit-content;
  min-height: 30px;
  color: var(--accent2);
  text-decoration: underline;
}

@media (pointer: coarse) {
  .care-run-retry,
  .care-run-link { min-height: var(--touch); }
}
</style>
