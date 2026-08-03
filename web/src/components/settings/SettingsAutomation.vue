<template>
  <div class="card">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <div class="settings-label-row">
          <p class="section-title">Background Automations</p>
          <button class="btn-small" :disabled="!automationLoaded" @click="fetchAutomation">Refresh</button>
        </div>
        <p class="hint">
          Status and logs of background tasks (git sync, memory curation, skills update, title generation).
          Tasks wrapped in telemetry are recorded here.
        </p>
        <div class="automation-legend" aria-label="Automation capability legend">
          <span class="automation-legend-item">
            <span class="automation-capability automation-capability--model">MODEL</span>
            invokes a language model
          </span>
          <span class="automation-legend-item">
            <span class="automation-capability automation-capability--outcome">OUTCOME</span>
            creates a durable or user-visible result
          </span>
          <span class="automation-legend-item">
            <span class="automation-capability automation-capability--muted">STATUS ONLY</span>
            records operational health
          </span>
        </div>
      </div>
    </div>

    <div v-if="!automationLoaded" class="card"><span class="loading">Loading&hellip;</span></div>
    <p v-else-if="automationError" class="hint hint--warn">{{ automationError }}</p>
    <template v-else-if="automationItems">
      <p v-if="automationItems.length === 0" class="hint hint--info">
        No automation runs recorded yet.
      </p>
      <div v-else class="automation-list">
        <div v-for="item in visibleAutomationItems" :key="item.job" class="automation-row-container">
          <!-- Header of job run -->
          <div class="automation-job-header" @click="expandedAutomations[item.job] = !expandedAutomations[item.job]">
            <div class="job-meta-left">
              <span class="expand-icon">{{ expandedAutomations[item.job] ? '▼' : '▶' }}</span>
              <span class="job-title">{{ item.label }}</span>
              <span
                class="automation-capability"
                :class="jobUsesModel(item) ? 'automation-capability--model' : 'automation-capability--muted'"
                :title="jobUsesModel(item) ? 'This automation invokes a language model.' : 'This automation does not invoke a language model.'"
              >
                {{ jobUsesModel(item) ? 'MODEL' : 'NO MODEL' }}
              </span>
              <span
                class="automation-capability"
                :class="jobProducesOutcome(item) ? 'automation-capability--outcome' : 'automation-capability--muted'"
                :title="jobProducesOutcome(item) ? 'This automation creates a durable or user-visible result.' : 'This automation records operational health only.'"
              >
                {{ jobProducesOutcome(item) ? 'OUTCOME' : 'STATUS ONLY' }}
              </span>
            </div>
            <div class="job-meta-right">
              <!-- Stats summary -->
              <span v-if="item.stats.total_runs" class="job-stat-summary">
                {{ Math.round((item.stats.success_rate || 0) * 100) }}% ok &middot; {{ item.stats.total_runs }} runs
              </span>
              <!-- Status badge -->
              <span class="badge" :class="getJobBadgeClass(item.job)">
                {{ getJobStatus(item.job) }}
              </span>
              <!-- Trigger button -->
              <button
                v-if="getJobSchedule(item.job)"
                class="btn-small btn-run"
                :disabled="triggeringJobs[item.job]"
                @click.stop="triggerJob(item.job)"
              >
                {{ triggeringJobs[item.job] ? 'Running...' : 'Run now' }}
              </button>
              <button
                v-else-if="item.job === 'backfill_insights' || item.job === 'insights_backfill'"
                class="btn-small btn-run"
                :disabled="backfillRunning"
                @click.stop="triggerBackfill"
              >
                {{ backfillRunning ? 'Backfilling...' : 'Backfill insights' }}
              </button>
            </div>
          </div>

          <!-- Expanded details (Runs log) -->
          <div v-if="expandedAutomations[item.job]" class="automation-job-detail">
            <div class="automation-job-description">
              <p class="automation-job-description-label">What it does</p>
              <p>{{ item.description || 'No description available.' }}</p>
            </div>

            <!-- Last run meta -->
            <div v-if="item.last_run" class="last-run-info">
              <div class="detail-row">
                <span class="detail-label">Last Run:</span>
                <span class="detail-val">{{ getJobLastRunLabel(item.job) }} ({{ getJobDuration(item.job) }})</span>
              </div>
              <div v-if="item.last_run.model" class="detail-row">
                <span class="detail-label">Model:</span>
                <span class="detail-val">{{ item.last_run.provider }}/{{ item.last_run.model }}</span>
              </div>
            </div>

            <!-- Last error banner -->
            <div v-if="item.stats.last_error" class="error-banner">
              <div class="error-banner-header">
                <span>Last Error ({{ formatTime(item.stats.last_error.ts) }})</span>
              </div>
              <pre class="error-text">{{ item.stats.last_error.error }}</pre>
            </div>

            <!-- Recent runs history -->
            <div class="runs-history">
              <p class="history-title">Recent Runs History</p>
              <div v-if="item.recent.length === 0" class="empty-history">No runs logged in this session.</div>
              <table v-else class="history-table">
                <thead>
                  <tr>
                    <th>Finished</th>
                    <th>Status</th>
                    <th>Duration</th>
                    <th>Details / Errors</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="run in item.recent" :key="run.started_at + run.ended_at">
                    <td class="td-time">{{ formatTime(run.ended_at || run.started_at) }}</td>
                    <td>
                      <span class="badge" :class="getTelemetryBadgeClass(run.status)">
                        {{ run.status }}
                      </span>
                    </td>
                    <td class="td-dur">{{ formatDuration(run.duration_ms) || 'unknown' }}</td>
                    <td class="td-details">
                      <span v-if="run.model" class="run-model-info">{{ run.provider }}/{{ run.model }}</span>
                      <span v-if="run.extra?.summary || run.extra?.message" class="run-summary-info">
                        {{ run.extra?.summary || run.extra?.message }}
                      </span>
                      <span v-if="run.status === 'skipped' && run.extra?.skip_reason" class="run-skip-info">
                        Skipped: {{ run.extra.skip_reason }}
                      </span>
                      <div v-if="run.error && run.error !== run.extra?.summary" class="run-error-details" :title="run.error">
                        {{ run.error }}
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../../lib/api'
import { errorMessage } from '../../lib/errorMessage'
import { formatDuration, formatTime } from '../../lib/time'
import { useTaskStore } from '../../stores/tasks'
import type { AutomationProcess } from '../../lib/types'

// The automation data and the job-telemetry read helpers live in SettingsView,
// which also renders the same telemetry on the Models tab. This component owns
// the tab's render-only state and the trigger actions, and receives the shared
// state as props.
const props = defineProps<{
  automationItems: AutomationProcess[]
  automationLoaded: boolean
  automationError: string
  fetchAutomation: () => Promise<void>
  notifySaved: (body: string, title?: string) => void
  getJobBadgeClass: (job: string) => string
  getJobStatus: (job: string) => string
  getJobLastRunLabel: (job: string) => string
  getJobDuration: (job: string) => string
  getTelemetryBadgeClass: (status: string | undefined) => string
}>()

const taskStore = useTaskStore()

const visibleAutomationItems = computed(() => {
  // Older runtimes used `insights_backfill`; current runtimes use
  // `backfill_insights`. If both are present in the retained run log, show
  // the canonical job once rather than presenting duplicate backfills.
  const hasCanonicalBackfill = props.automationItems.some(
    (item) => item.job === 'backfill_insights',
  )
  if (!hasCanonicalBackfill) return props.automationItems
  return props.automationItems.filter((item) => item.job !== 'insights_backfill')
})

// Keep the capability badges correct while a desktop engine is still serving
// an older API response. Newer servers provide these flags explicitly.
const modelBackedAutomationJobs = new Set([
  'title',
  'insights',
  'skill_evolution',
  'dependency_review',
  'schedule_dispatch',
  'schedule_attention_classifier',
  'backfill_insights',
])
const outcomeAutomationJobs = new Set([
  'title',
  'insights',
  'memory_proposals',
  'trajectory',
  'skill_evolution',
  'dependency_review',
  'schedule_dispatch',
  'backfill_insights',
])

function jobUsesModel(item: AutomationProcess): boolean {
  if (typeof item.uses_model === 'boolean') return item.uses_model
  return modelBackedAutomationJobs.has(item.job)
    || !!item.last_run?.model
    || item.recent.some((run) => !!run.model)
}

function jobProducesOutcome(item: AutomationProcess): boolean {
  if (typeof item.produces_outcome === 'boolean') return item.produces_outcome
  return outcomeAutomationJobs.has(item.job)
}

const expandedAutomations = ref<Record<string, boolean>>({})
const triggeringJobs = ref<Record<string, boolean>>({})
const jobScheduleMap: Record<string, string> = {
  memory_proposals: 'system-memory-curation',
  vault_index: 'system-workspace-hygiene',
  skill_evolution: 'system-skill-evolution'
}

async function triggerJob(jobId: string) {
  const scheduleId = jobScheduleMap[jobId]
  if (!scheduleId) return
  triggeringJobs.value[jobId] = true
  try {
    await taskStore.runScheduleNow(scheduleId)
    props.notifySaved(`Triggered automation job "${jobId}" via schedule "${scheduleId}".`, 'Automations')
    await props.fetchAutomation()
  } catch (e) {
    alert(`Failed to trigger job: ${errorMessage(e)}`)
  } finally {
    triggeringJobs.value[jobId] = false
  }
}

function getJobSchedule(jobId: string): string | undefined {
  return jobScheduleMap[jobId]
}

const backfillRunning = ref(false)
async function triggerBackfill() {
  backfillRunning.value = true
  try {
    await api.post('/api/automation/backfill-insights', {})
    props.notifySaved('Insights backfill started in the background.', 'Automations')
    setTimeout(props.fetchAutomation, 2000)
  } catch (e) {
    alert(`Failed to start backfill: ${errorMessage(e)}`)
  } finally {
    backfillRunning.value = false
  }
}
</script>

<style scoped>
/* Shared settings-card scaffolding (mirrors SettingsView.vue so the tab keeps
   its layout when rendered from a child component). */
.card {
  width: min(100%, 1040px);
  margin: 0 auto;
  gap: var(--space-4);
  border-color: var(--border);
  box-shadow: 0 1px 0 color-mix(in srgb, var(--fg) 4%, transparent);
}
.section-title {
  letter-spacing: 0.08em;
}
.settings-card-header {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-bottom: var(--space-3);
  border-bottom: 1px solid var(--border);
}
.settings-card-header:last-child {
  padding-bottom: 0;
  border-bottom: none;
}
.settings-card-header--split {
  flex-direction: row;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
}
.settings-card-header--split > div {
  min-width: 0;
}
.settings-card-header .hint {
  margin: var(--space-2) 0 0;
  max-width: 76ch;
}
.settings-label-row {
  min-height: 20px;
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.loading {
  color: var(--fg2);
  font-size: var(--text-base);
}

/* Background Automations Tab */
.automation-legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-4);
  margin-top: var(--space-3);
  color: var(--fg3);
  font-size: var(--text-xs);
}

.automation-legend-item {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.automation-capability {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  padding: 2px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  line-height: 1.3;
  white-space: nowrap;
}

.automation-capability--model {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}

.automation-capability--outcome {
  color: var(--success);
  border-color: color-mix(in srgb, var(--success) 55%, var(--border));
  background: color-mix(in srgb, var(--success) 10%, transparent);
}

.automation-capability--muted {
  color: var(--fg3);
  background: var(--bg3);
}

.automation-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin-top: var(--space-4);
}

.automation-row-container {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  transition: border-color 0.15s var(--ease);
}

.automation-row-container:hover {
  border-color: var(--border-strong);
}

.automation-job-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-3);
  cursor: pointer;
  user-select: none;
  background: var(--bg2);
}

.job-meta-left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  flex: 1 1 auto;
  min-width: 0;
}

.expand-icon {
  font-size: 10px;
  color: var(--fg3);
  width: 12px;
  text-align: center;
}

.job-title {
  font-weight: 600;
  color: var(--fg);
  white-space: nowrap;
}

.job-meta-right {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex: 0 0 auto;
}

@media (max-width: 768px) {
  .automation-job-header {
    align-items: flex-start;
    flex-direction: column;
    gap: var(--space-3);
  }

  .job-meta-left,
  .job-meta-right {
    width: 100%;
  }

  .job-meta-right {
    justify-content: flex-end;
  }

}

.job-stat-summary {
  font-size: var(--text-xs);
  color: var(--fg3);
  font-variant-numeric: tabular-nums;
  margin-right: var(--space-2);
}

.btn-run {
  background: var(--accent2);
  color: #fff;
  border: none;
}

.btn-run:hover:not(:disabled) {
  background: var(--accent-strong);
}

.automation-job-detail {
  padding: var(--space-4);
  background: var(--bg);
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.automation-job-description {
  max-width: 70ch;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.automation-job-description p {
  margin: 0;
}

.automation-job-description-label {
  margin-bottom: var(--space-1) !important;
  color: var(--fg3);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.last-run-info {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  font-size: var(--text-sm);
}

.detail-row {
  display: flex;
  gap: var(--space-2);
}

.detail-label {
  color: var(--fg2);
  font-weight: 500;
}

.detail-val {
  color: var(--fg);
  font-variant-numeric: tabular-nums;
}

.error-banner {
  background: rgba(244, 67, 54, 0.08);
  border: 1px solid var(--error);
  border-radius: var(--radius-sm);
  padding: var(--space-3);
}

.error-banner-header {
  font-weight: 600;
  color: var(--error);
  margin-bottom: var(--space-2);
  font-size: var(--text-xs);
}

.error-text {
  margin: 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: var(--text-xs);
  color: var(--fg);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
}

.runs-history {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.history-title {
  font-weight: 600;
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.empty-history {
  font-size: var(--text-xs);
  color: var(--fg3);
}

.history-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-xs);
}

.history-table th {
  text-align: left;
  padding: var(--space-2);
  color: var(--fg3);
  font-weight: 600;
  border-bottom: 1px solid var(--border);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.history-table td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--border);
  color: var(--fg);
  vertical-align: middle;
}

.history-table tbody tr:last-child td {
  border-bottom: none;
}

.td-time {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.td-dur {
  font-variant-numeric: tabular-nums;
  color: var(--fg2);
}

.run-model-info {
  background: var(--bg2);
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  color: var(--fg2);
  font-size: 10px;
  margin-right: var(--space-2);
}

.run-summary-info {
  color: var(--fg2);
}

.run-skip-info {
  color: var(--warning);
}

.run-error-details {
  color: var(--error);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 350px;
}
</style>
