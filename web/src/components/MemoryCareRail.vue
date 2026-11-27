<template>
  <DockedWindow class="care-rail" title="Daily care" id="care-rail-title">
    <p v-if="!report" class="care-rail-text">{{ care.error ? 'Could not read the care report.' : 'Reading the report…' }}</p>
    <template v-else>
      <dl class="care-rail-facts">
        <div class="care-rail-fact">
          <dt>Last run</dt>
          <dd>
            <span class="care-rail-state">
              <CareGlyph :kind="glyph" :size="14" />
              <span>{{ stateWord }}</span>
            </span>
            <span v-if="lastWhen" class="care-rail-when">{{ lastWhen }}</span>
          </dd>
        </div>
        <div class="care-rail-fact">
          <dt>Next run</dt>
          <dd><span class="care-rail-when">{{ nextWhen }}</span></dd>
        </div>
        <div class="care-rail-fact">
          <dt>Weekly checks</dt>
          <dd><span class="care-rail-when">{{ weeklyWhen }}</span></dd>
        </div>
      </dl>
      <div class="docked-list--end care-rail-actions">
        <button
          v-if="report.schedule"
          type="button"
          class="docked-row"
          :disabled="care.starting || !!report.running"
          @click="runAgain"
        >
          <span class="docked-row-title">{{ runLabel }}</span>
        </button>
        <button
          type="button"
          class="docked-row"
          :disabled="report.status === 'missing'"
          :title="`Open ${report.report_path}`"
          @click="openReport"
        >
          <span class="docked-row-title">Open Care-Report.md</span>
        </button>
      </div>
      <p class="care-rail-status" role="status">{{ status }}</p>
    </template>
  </DockedWindow>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import CareGlyph from './CareGlyph.vue'
import DockedWindow from './DockedWindow.vue'
import { careGlyph, careState, dayWords, stampWords } from '../lib/careReport'
import { relativeOffset } from '../lib/scheduleList'
import { useCareReportStore } from '../stores/careReport'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'

/**
 * Memory · Workspace care's docked window: when the daily run last ran and will
 * next run, when the weekly checks were last done, and the two actions that
 * belong to the run (run it again, open its report). It reads the report the
 * page already holds; the next run comes from the care schedule's own row.
 */
const store = useProjectStore()
const care = useCareReportStore()
const tasks = useTaskStore()
const fileViewer = useFileViewerStore()

const report = computed(() => (care.report && care.report.workspace === store.activeWorkspace ? care.report : null))
const state = computed(() => (report.value ? careState(report.value) : 'never'))
const glyph = computed(() => careGlyph(state.value))

const stateWord = computed(() => {
  switch (state.value) {
    case 'running': return 'Running'
    case 'complete':
    case 'needs_you': return 'Done'
    case 'blocked':
    case 'interrupted': return 'Did not finish'
    case 'unreadable': return 'Report unreadable'
    default: return 'Never run'
  }
})

/** "Nov 27, 18:56 · 12m ago"; a run in progress is dated by its dispatch. */
function stamped(iso: string): string {
  const at = stampWords(iso)
  const ago = relativeOffset(iso)
  return at ? (ago ? `${at} · ${ago}` : at) : ''
}
const lastWhen = computed(() => {
  const r = report.value
  if (!r) return ''
  if (r.running) return stamped(r.running.since)
  return r.status === 'ok' ? stamped(r.modified_at) : ''
})

const scheduleRow = computed(() => {
  const id = report.value?.schedule?.id
  return id ? tasks.schedules.find(s => s.schedule_id === id) ?? null : null
})
const nextWhen = computed(() => {
  const schedule = report.value?.schedule
  if (!schedule || !schedule.enabled) return 'Not scheduled'
  return stamped(scheduleRow.value?.next_run ?? '') || '—'
})

const weeklyWhen = computed(() => (report.value ? dayWords(report.value.weekly_done) : '') || 'Not done yet')

const runLabel = computed(() => {
  if (care.starting) return 'Starting…'
  if (report.value?.running) return 'Running…'
  return report.value?.status === 'missing' ? 'Run care now' : 'Run care again'
})

const status = ref('')
async function runAgain(): Promise<void> {
  status.value = ''
  const { message } = await care.runAgain()
  status.value = message
  void tasks.fetchSchedules().catch(() => {})
}

function openReport(): void {
  if (report.value) void fileViewer.open(report.value.report_path)
}

onMounted(() => { void tasks.fetchSchedules().catch(() => {}) })
</script>

<style scoped>
.care-rail-text { margin: 10px 0 0; color: var(--fg2); font-size: var(--text-sm); line-height: 1.5; }
.care-rail-facts { margin: 0; }
.care-rail-fact {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 13px 0;
  border-bottom: 1px solid var(--border);
}
.care-rail-fact dt {
  color: var(--fg);
  font-size: calc(13px * var(--font-scale));
  font-weight: 650;
  line-height: 1.35;
}
.care-rail-fact dd { margin: 0; display: flex; flex-direction: column; gap: 2px; color: var(--fg2); line-height: 1.4; }
.care-rail-state { display: inline-flex; align-items: center; gap: var(--space-2); }
.care-rail-when { font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.care-rail-actions .docked-row-title { color: var(--accent); font-weight: 600; }
.care-rail-actions .docked-row:not(:disabled):hover .docked-row-title { text-decoration: underline; text-underline-offset: 3px; }
.care-rail-actions .docked-row:disabled .docked-row-title { color: var(--fg3); }
.care-rail-actions .docked-row:disabled { color: var(--fg3); cursor: default; }
.care-rail-status { margin: 10px 0 0; color: var(--fg2); font-size: var(--text-sm); line-height: 1.5; }
.care-rail-status:empty { display: none; }
</style>
