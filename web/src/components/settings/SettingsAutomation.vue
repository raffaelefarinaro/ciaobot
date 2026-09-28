<template>
  <div class="card">
    <div class="settings-card-header">
      <div class="automation-section-head">
        <p class="section-title">Background automations</p>
        <button class="text-link" type="button" :disabled="!automationLoaded" @click="fetchAutomation">Refresh</button>
      </div>
      <p class="hint">
        Work Ciaobot does on its own: naming chats, capturing a session trajectory when a
        chat is archived, keeping the vault and skills in order.
      </p>
    </div>

    <div v-if="routines" class="insights-control">
      <div class="insights-control-copy">
        <span class="insights-control-title">Session insights</span>
        <span class="hint">
          Reads the chat you just finished for durable learnings. Off stops the model pass.
        </span>
      </div>
      <button
        class="insights-toggle"
        type="button"
        role="switch"
        :aria-checked="insightsEnabled"
        aria-label="Automatic session insights"
        :disabled="routinesSaving"
        @click="toggleInsights"
      >
        <span class="switch-word">{{ insightsEnabled ? 'On' : 'Off' }}</span>
        <span class="switch-track" aria-hidden="true"></span>
      </button>
    </div>

    <div v-if="routines" class="insights-control">
      <div class="insights-control-copy">
        <span class="insights-control-title">Trajectory capture</span>
        <span class="hint">
          Writes a structured trajectory record for each archived chat.
        </span>
      </div>
      <button
        class="insights-toggle"
        type="button"
        role="switch"
        :aria-checked="trajectoriesEnabled"
        aria-label="Automatic trajectory capture"
        :disabled="routinesSaving"
        @click="toggleTrajectories"
      >
        <span class="switch-word">{{ trajectoriesEnabled ? 'On' : 'Off' }}</span>
        <span class="switch-track" aria-hidden="true"></span>
      </button>
    </div>

    <p v-if="!automationLoaded" class="loading">Loading&hellip;</p>
    <p v-else-if="automationError" class="hint hint--warn">{{ automationError }}</p>
    <template v-else-if="automationItems">
      <p class="automation-runs-title">Recent runs</p>
      <p v-if="automationItems.length === 0" class="hint automation-empty">
        No runs recorded yet. They appear here after the first nightly pass.
      </p>
      <template v-else>
        <p
          class="automation-headline"
          :class="{ 'automation-headline--warn': groups.attention.length > 0 }"
        >
          {{ headline }}
        </p>

        <!-- Whether memory extraction is useful, not only whether it ran:
              proposals promoted vs dismissed, over time. -->
        <div v-if="proposalsLine" class="automation-proposals">
          <p class="automation-proposals-total">{{ proposalsLine }}</p>
          <!-- Visible, not hover-only: touch and keyboard users get the
               per-workspace split too. -->
          <p
            v-for="entry in proposalsByWorkspace"
            :key="entry.workspace"
            class="automation-proposals-workspace"
          >
            {{ entry.workspace }}: {{ entry.counts.promoted }} promoted ·
            {{ entry.counts.dismissed }} dismissed
          </p>
        </div>

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
              @toggle="toggle(item.job)"
              @run="runJob(item)"
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
              @toggle="toggle(item.job)"
              @run="runJob(item)"
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
              @toggle="toggle(item.job)"
              @run="runJob(item)"
            />
          </div>
        </details>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { errorMessage } from '../../lib/errorMessage'
import { automationHeadline, groupAutomations } from '../../lib/automationView'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import type { AutomationProcess, ProposalOutcomes, RoutineSettings } from '../../lib/types'
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
  // Promoted-vs-dismissed memory-proposal tally, or null when the server does
  // not serve it yet (or nothing has been resolved at all).
  proposalOutcomes?: ProposalOutcomes | null
  notifySaved: (body: string, title?: string) => void
  // The failure channel. `alert` cannot be used: it shows nothing at all in the
  // desktop webview, so a failed run looked like a button that did nothing.
  notifyFailed: (title: string, detail: string) => void
  // Model routing table, so a model-backed job that keeps failing can be
  // retried with a different model without leaving the page.
  routines: RoutineSettings | null
  routinesSaving: boolean
  saveRoutines: (patch: Record<string, unknown>) => Promise<void>
}>()

const taskStore = useTaskStore()
const projectStore = useProjectStore()

const groups = computed(() => groupAutomations(props.automationItems))
const headline = computed(() => automationHeadline(props.automationItems))

// One compact pipeline-health line beside the job stats. Hidden until at least
// one decision exists: "0 promoted · 0 dismissed" on a fresh install reads as
// breakage, not as an empty ledger.
const proposalsLine = computed(() => {
  const outcomes = props.proposalOutcomes
  if (!outcomes || (!outcomes.promoted && !outcomes.dismissed)) return ''
  const recent = (outcomes.recent_30d?.promoted ?? 0) + (outcomes.recent_30d?.dismissed ?? 0)
  return (
    `Memory proposals: ${outcomes.promoted} promoted · ` +
    `${outcomes.dismissed} dismissed (${recent} in last 30 days)`
  )
})

// The per-workspace split, rendered as visible lines — a title tooltip is
// unreachable on touch and not keyboard-focusable.
const proposalsByWorkspace = computed(() =>
  Object.entries(props.proposalOutcomes?.by_workspace ?? {}).map(([workspace, counts]) => ({
    workspace: workspace || 'shared',
    counts,
  })),
)

const insightsEnabled = computed(() => props.routines?.insights_enabled === true)
const trajectoriesEnabled = computed(
  () => props.routines?.trajectories_enabled === true,
)

async function toggleInsights() {
  await props.saveRoutines({ insights_enabled: !insightsEnabled.value })
}

async function toggleTrajectories() {
  await props.saveRoutines({ trajectories_enabled: !trajectoriesEnabled.value })
}

const expandedAutomations = ref<Record<string, boolean>>({})
function toggle(job: string) {
  expandedAutomations.value[job] = !expandedAutomations.value[job]
}

const runningJobs = ref<Record<string, boolean>>({})

// Schedules for servers older than the API that reports `schedule_id`.
const legacyJobSchedules: Record<string, string> = {
  memory_proposals: 'system-memory-curation',
  vault_index: 'system-memory-curation',
  skill_evolution: 'system-skill-evolution',
}

/**
 * The schedule id to actually POST for this row's "Run now".
 *
 * A per-workspace system routine is installed as `<base>@<workspace>`, so the
 * packaged id the API reports names no real schedule and running it 404s. Match
 * on the base id and prefer the row for the workspace the user is looking at.
 */
function scheduleFor(item: AutomationProcess): string {
  const base = item.schedule_id || legacyJobSchedules[item.job] || ''
  if (!base) return ''
  const matches = taskStore.schedules.filter(
    s => s.schedule_id === base || s.schedule_id.startsWith(`${base}@`),
  )
  if (!matches.length) return base
  const active = matches.find(s => s.workspace === projectStore.activeWorkspace)
  return (active ?? matches[0]).schedule_id
}

/**
 * What the row's action button offers, or '' when the job has no manual
 * trigger.
 */
function runLabel(item: AutomationProcess): string {
  return scheduleFor(item) ? 'Run now' : ''
}

/** One entry point for every row action: run the row's schedule now. */
async function runJob(item: AutomationProcess) {
  const scheduleId = scheduleFor(item)
  if (!scheduleId) return
  runningJobs.value[item.job] = true
  try {
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
  width: 100%;
  margin: 0;
  padding: 0;
  gap: var(--space-3);
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
  scroll-margin-top: var(--space-4);
}
.card:focus {
  outline: none;
}
.section-title {
  color: var(--fg);
  font-family: var(--font-sans);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.01em;
  line-height: 1.3;
  text-transform: none;
}
.settings-card-header {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding-bottom: var(--space-1);
  border-bottom: 0;
}
.settings-card-header .hint {
  color: var(--fg3);
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
  margin: var(--space-1) 0 0;
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

.automation-section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  min-width: 0;
}
/* Quiet text action at the section's right edge (matches SchedulePanel). */
.text-link {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  flex: none;
}
.text-link:hover:not(:disabled) { text-decoration: underline; text-underline-offset: 3px; }
.text-link:disabled { color: var(--fg3); cursor: default; }

/* Hairline rows: title and one muted line, a switch on the right. */
.insights-control {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  min-height: 56px;
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--border);
}
.card > .settings-card-header + .insights-control { border-top: 1px solid var(--border); }
.insights-control-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.insights-control-title {
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 600;
}
.insights-control .hint {
  margin: 0;
  max-width: 72ch;
  color: var(--fg3);
  font-size: var(--text-sm);
}
/* A switch reads as state, keeping pink for the accent rather than an action.
   The visible On/Off word means it does not rely on colour. */
.insights-toggle {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  padding: 0 2px;
  border: 0;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.insights-toggle:disabled { opacity: 0.6; cursor: default; }
.switch-word { min-width: 2.2em; text-align: right; }
.switch-track {
  position: relative;
  width: 36px;
  height: 20px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-full, 9999px);
  background: var(--bg3);
  transition: background 120ms ease, border-color 120ms ease;
}
.switch-track::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--fg3);
  transition: transform 120ms ease, background 120ms ease;
}
.insights-toggle[aria-checked='true'] .switch-word { color: var(--fg); }
.insights-toggle[aria-checked='true'] .switch-track {
  border-color: transparent;
  background: var(--accent);
}
.insights-toggle[aria-checked='true'] .switch-track::after {
  transform: translateX(16px);
  background: var(--on-accent);
}
@media (prefers-reduced-motion: reduce) {
  .switch-track, .switch-track::after { transition: none; }
}

.automation-runs-title {
  margin: var(--space-4) 0 0;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.01em;
}
.automation-empty {
  margin: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
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

.automation-proposals {
  margin: 0;
  font-size: var(--text-sm);
}

.automation-proposals-total,
.automation-proposals-workspace {
  margin: 0;
}

.automation-proposals-workspace {
  color: var(--fg3);
}

.automation-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.automation-group-title {
  margin: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  font-weight: 600;
}

.automation-group-title--warn {
  color: var(--warning);
}

.automation-list {
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--border);
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
