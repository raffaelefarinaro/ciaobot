<template>
  <div class="automation-row" :class="`automation-row--${health}`">
    <div class="automation-row-head">
      <!-- A real button, so the row is reachable and toggleable by keyboard. -->
      <button
        class="automation-row-toggle"
        type="button"
        :aria-expanded="expanded"
        @click="emit('toggle')"
      >
        <span class="expand-icon" aria-hidden="true">{{ expanded ? '▼' : '▶' }}</span>
        <span class="automation-row-text">
          <span class="job-title">
            {{ title }}
            <span class="job-status" :class="`job-status--${health}`">{{ statusLine }}</span>
            <!-- Running right now, in the same muted register the rest of the
                 app uses for background work: informative, not a demand. -->
            <span v-if="running" class="job-running">
              <span class="job-running-dot" aria-hidden="true" />running
            </span>
          </span>
          <span class="job-description">{{ item.description || 'No description available.' }}</span>
          <span v-if="item.trigger && !steps.length" class="job-trigger">{{ item.trigger }}</span>
          <span v-if="health === 'error' && lastErrorText" class="job-error">{{ lastErrorText }}</span>

          <!-- A pipeline's steps, always visible rather than hidden behind the
               disclosure: the shape of the sequence *is* the explanation of the
               row, and these used to be four peer rows each claiming a trigger
               of its own ("After session insights", which is a position, not a
               trigger). -->
          <span v-if="steps.length" class="job-steps">
            <span
              v-for="step in steps"
              :key="step.job"
              class="job-step"
              :class="{ 'job-step--error': step.health === 'error' }"
            >
              <span class="job-step-rail" aria-hidden="true" />
              <span class="job-step-name">{{ step.label }}</span>
              <span v-if="step.running" class="job-running">
                <span class="job-running-dot" aria-hidden="true" />running
              </span>
              <span v-else-if="step.health === 'error'" class="job-step-note job-step-note--error">
                failed last time
              </span>
              <span v-else-if="step.condition" class="job-step-note">{{ step.condition }}</span>
            </span>
          </span>
        </span>
      </button>

      <div v-if="runLabel" class="automation-row-actions">
        <label v-if="showModelPicker" class="automation-model-picker">
          <span class="automation-model-label">Model for this run</span>
          <select v-model="selectedModel" class="automation-select" :disabled="busy">
            <option value="">
              {{ configuredModel ? `Configured (${configuredModel})` : 'Configured model' }}
            </option>
            <option v-for="option in retryModelOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <button
          class="btn-small btn-run"
          type="button"
          :disabled="busy"
          @click="emit('run', selectedModel)"
        >
          {{ busy ? 'Running…' : runLabel }}
        </button>
      </div>
    </div>

    <div v-if="expanded" class="automation-row-detail">
      <div class="detail-facts">
        <span v-if="item.last_run">
          Last run {{ formatTime(item.last_run.ended_at || item.last_run.started_at) }}
          · {{ formatDuration(item.last_run.duration_ms) || 'unknown duration' }}
        </span>
        <span v-if="item.last_run?.model">
          Model {{ item.last_run.provider }}/{{ item.last_run.model }}
        </span>
        <span v-else-if="item.uses_model === false">Runs without a model</span>
        <span v-if="item.stats.total_runs">
          {{ item.stats.total_runs }} recorded runs
          · {{ Math.round((item.stats.success_rate || 0) * 100) }}% succeeded
        </span>
      </div>

      <div v-if="errorDetail" class="error-banner">
        <div class="error-banner-header">
          Last error ({{ formatTime(errorDetail.ts) }})
        </div>
        <pre class="error-text">{{ errorDetail.error }}</pre>
      </div>

      <RunHistory :runs="item.recent" :title="steps.length ? `${item.label} — recent runs` : 'Recent runs'" />
      <!-- Each pipeline step keeps its own history: they run on one trigger but
           succeed and fail independently. -->
      <RunHistory
        v-for="step in item.steps || []"
        :key="step.job"
        :runs="step.recent"
        :title="`${step.label} — recent runs`"
      />
      <!-- Bulk/manual variants (Session insights carries its catch-up pass). -->
      <RunHistory
        v-for="sub in item.sub_jobs || []"
        :key="sub.job"
        :runs="sub.recent"
        :title="`${sub.label} — recent runs`"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { formatDuration, formatRelative, formatTime } from '../../lib/time'
import {
  attentionSource,
  isRunningNow,
  lastRunSentence,
  overallHealth,
  pipelineSteps,
} from '../../lib/automationView'
import type { RetryModelOption } from '../../lib/automationView'
import type { AutomationProcess } from '../../lib/types'
import RunHistory from './AutomationRunHistory.vue'

const props = defineProps<{
  item: AutomationProcess
  expanded: boolean
  busy: boolean
  /** Label for the manual-run button; '' hides it (nothing to trigger). */
  runLabel: string
  retryModelOptions: RetryModelOption[]
  configuredModel: string
}>()

const emit = defineEmits<{ toggle: []; run: [model: string] }>()

const selectedModel = ref('')

// Health folds in bulk variants: a failed insights backfill is a failure of
// Session insights from the user's side of the screen.
const health = computed(() => overallHealth(props.item))
const statusLine = computed(() => lastRunSentence(props.item, formatRelative))

// A pipeline is named by the thing the user recognises ("When you archive a
// chat"); the owning job's own label becomes its first step.
const steps = computed(() => pipelineSteps(props.item))
const title = computed(() => props.item.pipeline_label || props.item.label)
const running = computed(() => (steps.value.length ? false : isRunningNow(props.item)))
const lastErrorText = computed(() => {
  const source = attentionSource(props.item)
  return source.last_run?.error || source.stats.last_error?.error || ''
})
const errorDetail = computed(() => attentionSource(props.item).stats.last_error)

// Only offered where it changes the outcome: a failed model-backed run whose
// next attempt can use a different model (Session insights' bulk pass).
const showModelPicker = computed(
  () =>
    health.value === 'error'
    && props.item.job === 'insights'
    && props.retryModelOptions.length > 0,
)
</script>

<style scoped>
.automation-row {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  transition: border-color 0.15s var(--ease);
}

.automation-row:hover {
  border-color: var(--border-strong);
}

.automation-row--error {
  border-color: color-mix(in srgb, var(--error) 45%, var(--border));
}

.automation-row-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3);
}

.automation-row-toggle {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  flex: 1 1 auto;
  min-width: 0;
  padding: 0;
  border: none;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.automation-row-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}

.expand-icon {
  font-size: 10px;
  color: var(--fg3);
  width: 12px;
  flex: 0 0 12px;
  line-height: 1.7;
  text-align: center;
}

.automation-row-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.job-title {
  color: var(--fg);
  font-weight: 600;
}

.job-status {
  margin-left: var(--space-2);
  color: var(--fg3);
  font-size: var(--text-xs);
  font-weight: 500;
  white-space: nowrap;
}

.job-status--error {
  color: var(--error);
}

.job-status--never,
.job-status--idle {
  color: var(--fg3);
}

.job-description {
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}

.job-trigger {
  color: var(--fg3);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.job-error {
  margin-top: 2px;
  color: var(--error);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}

/* ── Pipeline steps ─────────────────────────────────────────────────────── */

.job-steps {
  display: flex;
  flex-direction: column;
  margin-top: var(--space-2);
  min-width: 0;
}

/* A real tree, because the data is a real tree: one task, four steps in
   execution order. */
.job-step {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  padding: 3px 0;
  min-width: 0;
  position: relative;
}

.job-step-rail {
  position: relative;
  flex: 0 0 14px;
  align-self: stretch;
}

.job-step-rail::before {
  content: "";
  position: absolute;
  left: 4px;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--border-strong);
}

/* The last step's rail stops at its own branch, so the tree closes instead of
   trailing a line into nothing. */
.job-step:last-child .job-step-rail::before {
  bottom: 50%;
}

.job-step-rail::after {
  content: "";
  position: absolute;
  left: 4px;
  top: 50%;
  width: 7px;
  height: 1px;
  background: var(--border-strong);
}

.job-step-name {
  color: var(--fg2);
  font-size: var(--text-sm);
  min-width: 0;
}

.job-step--error .job-step-name {
  color: var(--error);
}

.job-step-note {
  color: var(--fg3);
  font-size: var(--text-xs);
  font-family: var(--font-mono, ui-monospace, monospace);
}

.job-step-note--error {
  color: var(--error);
  font-family: inherit;
}

/* Matches the quiet register used for post-archive work everywhere else: grey
   and slow, never the accent, because nothing here needs the user. */
.job-running {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-left: var(--space-2);
  color: var(--fg3);
  font-size: var(--text-xs);
  font-weight: 500;
  white-space: nowrap;
}

.job-running-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fg3);
  flex: 0 0 auto;
  animation: job-running-breathe 2.6s ease-in-out infinite;
}

@keyframes job-running-breathe {
  0%, 100% { opacity: 0.35; }
  50%      { opacity: 0.9; }
}

@media (prefers-reduced-motion: reduce) {
  .job-running-dot { animation: none; opacity: 0.75; }
}

.automation-row-actions {
  display: flex;
  align-items: flex-end;
  gap: var(--space-2);
  flex: 0 0 auto;
}

.automation-model-picker {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.automation-model-label {
  color: var(--fg3);
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

/* Mirrors `.routine-select` in SettingsView, which is scoped there and so does
   not reach this child component. */
.automation-select {
  max-width: 220px;
  min-width: 0;
  width: 100%;
  min-height: 38px;
  padding: 6px 30px 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg);
  color: var(--fg);
  font-size: var(--text-sm);
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2.5 4.5L6 8l3.5-3.5' fill='none' stroke='%23888' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/></svg>");
  background-repeat: no-repeat;
  background-position: right 10px center;
  background-size: 12px 12px;
}

.automation-select::-ms-expand {
  display: none;
}

.btn-run {
  background: var(--accent2);
  color: #fff;
  border: none;
  white-space: nowrap;
}

.btn-run:hover:not(:disabled) {
  background: var(--accent-strong);
}

@media (max-width: 768px) {
  .automation-row-head {
    flex-direction: column;
  }

  .automation-row-actions {
    width: 100%;
    align-items: flex-end;
    justify-content: flex-end;
  }

  .automation-model-picker {
    flex: 1 1 auto;
    min-width: 0;
  }

  .automation-select {
    max-width: none;
  }
}

.automation-row-detail {
  padding: var(--space-4);
  background: var(--bg);
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.detail-facts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-4);
  color: var(--fg2);
  font-size: var(--text-sm);
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
</style>
