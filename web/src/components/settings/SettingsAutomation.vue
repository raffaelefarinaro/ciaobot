<template>
  <div class="card">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <div class="settings-label-row">
          <p class="section-title">Background Automations</p>
          <button class="btn-small" :disabled="!automationLoaded" @click="fetchAutomation">Refresh</button>
        </div>
        <p class="hint">
          Work Ciaobot does on its own: naming chats, extracting insights when a chat is
          archived, keeping the vault and skills in order. Each row says when it runs and
          what happened last time. Rows that run several steps on one trigger list those
          steps in the order they execute.
        </p>
      </div>
    </div>

    <div v-if="!automationLoaded" class="card"><span class="loading">Loading&hellip;</span></div>
    <p v-else-if="automationError" class="hint hint--warn">{{ automationError }}</p>
    <template v-else-if="automationItems">
      <p v-if="automationItems.length === 0" class="hint hint--info">
        No automation runs recorded yet.
      </p>
      <template v-else>
        <p
          class="automation-headline"
          :class="{ 'automation-headline--warn': groups.attention.length > 0 }"
        >
          {{ headline }}
        </p>

        <!-- Failing first: the only rows that need a decision. -->
        <section v-if="groups.attention.length" class="automation-group">
          <p class="automation-group-title automation-group-title--warn">Needs attention</p>
          <div class="automation-list">
            <AutomationRow
              v-for="item in groups.attention"
              :key="item.job"
              :item="item"
              :expanded="!!expandedAutomations[item.job]"
              :busy="!!runningJobs[item.job]"
              :run-label="runLabel(item)"
              :retry-model-options="retryModelOptions"
              :configured-model="configuredInsightsModel"
              @toggle="toggle(item.job)"
              @run="runJob(item, $event)"
            />
          </div>
        </section>

        <section v-if="groups.healthy.length" class="automation-group">
          <p v-if="groups.attention.length" class="automation-group-title">Working</p>
          <div class="automation-list">
            <AutomationRow
              v-for="item in groups.healthy"
              :key="item.job"
              :item="item"
              :expanded="!!expandedAutomations[item.job]"
              :busy="!!runningJobs[item.job]"
              :run-label="runLabel(item)"
              :retry-model-options="retryModelOptions"
              :configured-model="configuredInsightsModel"
              @toggle="toggle(item.job)"
              @run="runJob(item, $event)"
            />
          </div>
        </section>

        <!-- One-shot migrations: kept for the record, not live automations. -->
        <details v-if="groups.settled.length" class="automation-settled">
          <summary>
            One-time migrations ({{ groups.settled.length }}) &mdash; already done
          </summary>
          <div class="automation-list">
            <AutomationRow
              v-for="item in groups.settled"
              :key="item.job"
              :item="item"
              :expanded="!!expandedAutomations[item.job]"
              :busy="!!runningJobs[item.job]"
              :run-label="runLabel(item)"
              :retry-model-options="retryModelOptions"
              :configured-model="configuredInsightsModel"
              @toggle="toggle(item.job)"
              @run="runJob(item, $event)"
            />
          </div>
        </details>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../../lib/api'
import { errorMessage } from '../../lib/errorMessage'
import {
  automationHeadline,
  groupAutomations,
  retryModelOptions as buildRetryModelOptions,
} from '../../lib/automationView'
import { useTaskStore } from '../../stores/tasks'
import type { AutomationProcess, RoutineSettings } from '../../lib/types'
import AutomationRow from './AutomationRow.vue'

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
  // The failure channel. `alert` cannot be used: it shows nothing at all in the
  // desktop webview, so a failed run looked like a button that did nothing.
  notifyFailed: (title: string, detail: string) => void
  // Model routing table, so a model-backed job that keeps failing can be
  // retried with a different model without leaving the page.
  routines: RoutineSettings | null
  // Per-provider effective tier models, from /api/models. Each provider
  // resolves these from its own account catalog, which is why they do not ride
  // on the routines payload.
  aliasTiers: Record<string, Record<string, string>> | undefined
  providerLabels: Record<string, string>
}>()

const taskStore = useTaskStore()

const groups = computed(() => groupAutomations(props.automationItems))
const headline = computed(() => automationHeadline(props.automationItems))

const retryModelOptions = computed(() =>
  buildRetryModelOptions(props.aliasTiers, props.providerLabels),
)
const configuredInsightsModel = computed(
  () => props.routines?.insights_model_effective || '',
)

const expandedAutomations = ref<Record<string, boolean>>({})
function toggle(job: string) {
  expandedAutomations.value[job] = !expandedAutomations.value[job]
}

const runningJobs = ref<Record<string, boolean>>({})

// Schedules for servers older than the API that reports `schedule_id`.
const legacyJobSchedules: Record<string, string> = {
  memory_proposals: 'system-memory-curation',
  vault_index: 'system-workspace-hygiene',
  skill_evolution: 'system-skill-evolution',
}

function scheduleFor(item: AutomationProcess): string {
  return item.schedule_id || legacyJobSchedules[item.job] || ''
}

/**
 * What the row's action button offers, or '' when the job has no manual
 * trigger. Session insights runs over every archive still missing them —
 * previously a separate "Insights backfill" row, which read as an unrelated
 * automation rather than as this one's catch-up pass.
 */
function runLabel(item: AutomationProcess): string {
  if (item.job === 'insights') return 'Run for all sessions'
  return scheduleFor(item) ? 'Run now' : ''
}

/**
 * One entry point for every row action. `model` is only meaningful for
 * Session insights, whose bulk run accepts a one-off model override.
 */
async function runJob(item: AutomationProcess, model: string) {
  runningJobs.value[item.job] = true
  try {
    if (item.job === 'insights') {
      await api.post('/api/automation/backfill-insights', model ? { model } : {})
      props.notifySaved(
        model
          ? `Running session insights over every archive missing them, using ${model}.`
          : 'Running session insights over every archive missing them.',
        'Automations',
      )
      setTimeout(props.fetchAutomation, 2000)
      return
    }
    const scheduleId = scheduleFor(item)
    if (!scheduleId) return
    await taskStore.runScheduleNow(scheduleId)
    props.notifySaved(`Started "${item.label}" via the ${scheduleId} schedule.`, 'Automations')
    await props.fetchAutomation()
  } catch (e) {
    props.notifyFailed(`Could not run "${item.label}"`, errorMessage(e))
  } finally {
    runningJobs.value[item.job] = false
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

.automation-headline {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 600;
}

.automation-headline--warn {
  color: var(--warning);
}

.automation-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.automation-group-title {
  margin: 0;
  color: var(--fg3);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.automation-group-title--warn {
  color: var(--warning);
}

.automation-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.automation-settled {
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}

.automation-settled > summary {
  color: var(--fg3);
  cursor: pointer;
  font-size: var(--text-sm);
  min-height: 32px;
  display: flex;
  align-items: center;
}

.automation-settled[open] > summary {
  margin-bottom: var(--space-3);
}
</style>
