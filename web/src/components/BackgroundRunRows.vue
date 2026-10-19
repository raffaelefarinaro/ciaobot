<template>
  <div class="rail-list bg-runs">
    <div v-for="run in runs" :key="run.run_id" class="bg-run">
      <div class="bg-run-head">
        <ActivityGlyph kind="run" />
        <span class="bg-run-label" :title="`${backgroundRunLabel(run)}\n${commandText(run)}`">{{ backgroundRunLabel(run) }}</span>
        <span class="bg-run-elapsed">{{ backgroundRunElapsed(run, now) }}</span>
        <button
          type="button"
          class="bg-run-action"
          :aria-expanded="openLog === run.run_id"
          :aria-controls="`bg-run-log-${run.run_id}`"
          @click="toggleLog(run.run_id)"
        >{{ openLog === run.run_id ? 'Hide log' : 'Log' }}</button>
        <button
          type="button"
          class="bg-run-action bg-run-action--stop"
          :disabled="stopping.has(run.run_id)"
          @click="stop(run.run_id)"
        >{{ stopping.has(run.run_id) ? 'Stopping…' : 'Stop' }}</button>
      </div>
      <!-- An unnamed run is already labelled by its command; only a named one
           needs the command spelled out. -->
      <p v-if="run.label.trim()" class="bg-run-cmd" :title="commandText(run)">{{ commandText(run) }}</p>
      <p v-if="errors[run.run_id]" class="bg-run-error" role="alert">{{ errors[run.run_id] }}</p>
      <div
        v-if="openLog === run.run_id"
        :id="`bg-run-log-${run.run_id}`"
        class="bg-run-log"
      >
        <p v-if="logLoading" class="rail-note">Reading the log…</p>
        <pre v-else-if="logLines.length">{{ logLines.join('\n') }}</pre>
        <p v-else class="rail-note">No output yet.</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import { backgroundRunElapsed, backgroundRunLabel } from '../lib/backgroundRuns'
import type { BackgroundRunSummary } from '../lib/types'
import ActivityGlyph from './ActivityGlyph.vue'

const props = defineProps<{
  chatId: string
  runs: BackgroundRunSummary[]
  now: number
}>()

const store = useProjectStore()

const openLog = ref<string | null>(null)
const logLines = ref<string[]>([])
const logLoading = ref(false)
const stopping = reactive(new Set<string>())
const errors = reactive<Record<string, string>>({})

function commandText(run: BackgroundRunSummary): string {
  return run.cmd.join(' ')
}

// Read once per opening: a log that rewrote itself under the reader's eyes
// would lose their place, and reopening is the refresh.
async function toggleLog(runId: string) {
  if (openLog.value === runId) {
    openLog.value = null
    return
  }
  openLog.value = runId
  logLines.value = []
  logLoading.value = true
  delete errors[runId]
  try {
    const log = await store.fetchBackgroundRunLog(props.chatId, runId)
    if (openLog.value === runId) logLines.value = log.last_lines || []
  } catch {
    if (openLog.value === runId) {
      openLog.value = null
      errors[runId] = 'Could not read the log. It may have just finished.'
    }
  } finally {
    // A slower read for a log the user already left must not clear the
    // loading state of the one they opened since.
    if (openLog.value === runId || openLog.value === null) logLoading.value = false
  }
}

async function stop(runId: string) {
  stopping.add(runId)
  delete errors[runId]
  try {
    await store.cancelBackgroundRun(props.chatId, runId)
  } catch {
    stopping.delete(runId)
    errors[runId] = 'Could not stop this run. Try again.'
  }
}

// A run that left the list (finished, or stopped from here) drops its state.
watch(() => props.runs.map(r => r.run_id), (ids) => {
  const live = new Set(ids)
  if (openLog.value && !live.has(openLog.value)) openLog.value = null
  for (const id of [...stopping]) if (!live.has(id)) stopping.delete(id)
  for (const id of Object.keys(errors)) if (!live.has(id)) delete errors[id]
})
</script>

<style scoped>
.bg-run {
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}

.bg-run-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}

.bg-run-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bg-run-elapsed {
  flex-shrink: 0;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}

/* Text actions on the run's own line: no border, a fill on hover and focus,
   and a full touch height under a coarse pointer. */
.bg-run-action {
  flex: 0 0 auto;
  padding: 2px 6px;
  border: 0;
  border-radius: var(--radius-xs);
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-xs);
  font-weight: 500;
  cursor: pointer;
}

.bg-run-action:hover,
.bg-run-action:focus-visible { background: var(--bg3); color: var(--fg); }
.bg-run-action:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.bg-run-action--stop,
.bg-run-action--stop:hover,
.bg-run-action--stop:focus-visible { color: var(--error); }
.bg-run-action:disabled { color: var(--fg3); cursor: default; background: none; }

@media (pointer: coarse) {
  .bg-run-action { min-height: var(--touch); padding: 0 10px; }
}

.bg-run-cmd {
  margin: 2px 0 0;
  padding-left: calc(12px + var(--space-2));
  overflow: hidden;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bg-run-error {
  margin: 0 0 var(--space-1);
  color: var(--error);
  font-size: var(--text-xs);
}

.bg-run-log pre {
  max-height: 220px;
  margin: var(--space-1) 0 0;
  padding: var(--space-2);
  overflow: auto;
  border-radius: var(--radius-sm);
  background: var(--bg3);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
