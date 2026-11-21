<template>
  <div class="card">
    <div class="settings-card-header">
      <p class="section-title">Weekly care cycle</p>
      <p class="hint">
        Moves {{ workspace }}'s Workspace care state to the weekly cycle. You start the switch here, on
        the computer running Ciaobot. Workspace care never switches on its own.
      </p>
    </div>

    <!-- First load. A pending read is its own state, never an empty claim. -->
    <div v-if="!data && !loadError" class="hint" role="status">Checking the care state&hellip;</div>

    <!-- First-load failure: the error and a retry, never a guessed state. -->
    <template v-else-if="!data">
      <p class="action-result action-result--error" role="alert">
        Could not read the care state: {{ loadError }}
      </p>
      <div class="action-row settings-actions">
        <button type="button" class="btn-secondary btn-small" :disabled="loading" @click="load()">
          {{ loading ? 'Checking...' : 'Try again' }}
        </button>
      </div>
    </template>

    <template v-else-if="view">
      <!-- A failed refresh over data already on screen keeps it and says it is stale. -->
      <p v-if="loadError" class="hint hint--warn" role="alert">
        Could not refresh: {{ loadError }}. Showing the last known state.
        <button type="button" class="set-link" :disabled="loading" @click="load()">Try again</button>
      </p>

      <p v-if="applied" class="action-result" role="status">
        Switched. A copy of the old state is saved at {{ applied.backup_path }}. Ciaobot does not undo this automatically.
      </p>
      <div v-if="applyError" class="action-result action-result--error" role="alert">
        <p class="care-line">{{ applyError }}</p>
        <ul v-if="applyBlockers.length" class="care-list">
          <li v-for="reason in applyBlockers" :key="reason">{{ reason }}</li>
        </ul>
      </div>

      <!-- Not on the host: explain and offer the command to copy. No apply control. -->
      <template v-if="view.kind === 'not-on-host'">
        <p class="care-line" role="status">
          This switch runs on the computer running Ciaobot. In that computer's terminal, check the
          workspace with this command:
        </p>
        <div class="care-command">
          <code class="care-code">{{ CARE_CYCLE_CLI_COMMAND }}</code>
          <button type="button" class="btn-secondary btn-small" aria-label="Copy the command" @click="copyCommand">
            Copy
          </button>
        </div>
        <p v-if="copyStatus" class="hint hint--compact" role="status">{{ copyStatus }}</p>
      </template>

      <!-- Already switched: read-only. -->
      <template v-else-if="view.kind === 'switched'">
        <p class="care-line" role="status">This workspace already uses the weekly cycle.</p>
        <p class="hint hint--compact">
          The copy of the old state, if one was saved, is at <span class="care-path">{{ view.report.backup_path }}</span>.
        </p>
      </template>

      <!-- Blocked: the reasons, and the switch disabled with the reason beside it. -->
      <template v-else-if="view.kind === 'blocked'">
        <p class="care-line" role="status">
          {{ view.unreadable
            ? 'The care state cannot be read safely, so this workspace cannot be switched.'
            : 'This workspace cannot be switched yet.' }}
        </p>
        <ul class="care-list" aria-label="Why the switch is unavailable">
          <li v-for="reason in view.reasons" :key="reason">{{ reason }}</li>
        </ul>
        <div class="action-row settings-actions">
          <button
            type="button"
            class="btn-small btn-danger"
            aria-disabled="true"
            aria-describedby="care-cycle-reason"
            @click="applySwitch"
          >Switch this workspace</button>
        </div>
        <p id="care-cycle-reason" class="hint hint--compact">
          Switching is unavailable until the reasons above are resolved. Check again after they clear.
        </p>
      </template>

      <!-- Ready: what the switch keeps, the backup, the warning, then the checkbox and the button. -->
      <template v-else>
        <p class="care-line">Switching keeps your progress and moves this workspace to the weekly cycle.</p>
        <ul class="care-list">
          <li v-for="line in summary" :key="line">{{ line }}</li>
        </ul>
        <p class="care-line">
          Backup: <span class="care-path">{{ view.report.backup_path }}</span>
        </p>
        <p class="hint hint--compact">{{ undo }}</p>
        <p class="care-warning">{{ CARE_CYCLE_WARNING }}</p>

        <label class="care-confirm">
          <input
            v-model="confirmed"
            type="checkbox"
            :disabled="busy"
            aria-describedby="care-cycle-reason"
          />
          <span>{{ CARE_CYCLE_CONFIRM_LABEL }}</span>
        </label>
        <div class="action-row settings-actions">
          <button
            type="button"
            class="btn-small btn-danger"
            :aria-disabled="canApply ? undefined : 'true'"
            :aria-busy="applying ? 'true' : undefined"
            aria-describedby="care-cycle-reason"
            @click="applySwitch"
          >{{ applying ? 'Switching...' : 'Switch this workspace' }}</button>
        </div>
        <p id="care-cycle-reason" class="hint hint--compact">
          {{ canApply ? 'You will be asked to confirm before the switch runs.' : 'Tick the box above to confirm other copies are stopped.' }}
        </p>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { askConfirm } from '../../lib/confirm'
import { apiErrorMessage, errorPayloadList } from '../../lib/errorMessage'
import { useProjectStore } from '../../stores/projects'
import {
  CARE_CYCLE_CLI_COMMAND,
  CARE_CYCLE_CONFIRM_LABEL,
  CARE_CYCLE_WARNING,
  careCycleView,
  readySummary,
  undoSentence,
} from '../../lib/careCycle'
import type { CareCycleApplied, CareCycleResponse } from '../../lib/types'

// The one-way switch of the workspace's care state. The card owns its data: it
// reads the dry run on mount and posts the switch only with the digest of the
// preview it is showing, an explicit checkbox and a confirm dialog. Nothing
// here runs on a timer or on another card's say-so.

const undo = undoSentence()

const projectStore = useProjectStore()
const workspace = computed(() => projectStore.activeWorkspace)

const data = ref<CareCycleResponse | null>(null)
const loading = ref(false)
const loadError = ref('')
const confirmed = ref(false)
const confirming = ref(false)
const applying = ref(false)
const applied = ref<CareCycleApplied | null>(null)
const applyError = ref('')
const applyBlockers = ref<string[]>([])
const copyStatus = ref('')
let ticket = 0

const view = computed(() => (data.value ? careCycleView(data.value) : null))
const summary = computed(() => {
  const v = view.value
  return v && v.kind === 'ready' ? readySummary(v.report) : []
})
const busy = computed(() => applying.value || confirming.value)
const canApply = computed(() => view.value?.kind === 'ready' && confirmed.value && !busy.value)

async function load(): Promise<void> {
  const ws = workspace.value
  const mine = ++ticket
  loading.value = true
  try {
    const res = await api.get<CareCycleResponse>(`/api/care/cycle?workspace=${encodeURIComponent(ws)}`)
    if (mine !== ticket) return
    data.value = res
    loadError.value = ''
  } catch (e) {
    if (mine !== ticket) return
    loadError.value = apiErrorMessage(e, 'The request failed.')
  } finally {
    if (mine === ticket) loading.value = false
  }
}

async function applySwitch(): Promise<void> {
  const current = view.value
  // Checked before any await, so a second click while the dialog is open does nothing.
  if (busy.value || current?.kind !== 'ready' || !confirmed.value) return
  const name = workspace.value
  const digest = current.report.digest
  confirming.value = true
  let ok = false
  try {
    ok = await askConfirm(`${CARE_CYCLE_WARNING} ${undo}`, {
      title: 'Switch Workspace care to the new weekly cycle?',
      confirmLabel: 'Switch',
      destructive: true,
    })
  } finally {
    confirming.value = false
  }
  if (!ok || name !== workspace.value) return

  applying.value = true
  applyError.value = ''
  applyBlockers.value = []
  applied.value = null
  try {
    const res = await api.post<CareCycleApplied>('/api/care/cycle', {
      workspace: name,
      confirm_quiesced: true,
      digest,
    })
    if (name !== workspace.value) return
    applied.value = res
  } catch (e) {
    if (name !== workspace.value) return
    applyError.value = apiErrorMessage(e, 'The switch could not be completed.')
    applyBlockers.value = errorPayloadList(e, 'blockers') ?? []
  } finally {
    applying.value = false
    confirmed.value = false
  }
  // Re-read after either outcome: the switched state after success, and the
  // fresh preview (a new digest) after a refusal.
  if (name === workspace.value) await load()
}

async function copyCommand(): Promise<void> {
  try {
    await navigator.clipboard.writeText(CARE_CYCLE_CLI_COMMAND)
    copyStatus.value = 'Copied.'
  } catch {
    copyStatus.value = 'Could not copy. Select the command text and copy it.'
  }
}

onMounted(load)
watch(workspace, () => {
  // Another workspace's preview must never stand in for this one's.
  data.value = null
  loadError.value = ''
  confirmed.value = false
  applied.value = null
  applyError.value = ''
  applyBlockers.value = []
  copyStatus.value = ''
  void load()
})
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.care-line { margin: 0; color: var(--fg); font-size: var(--text-sm); line-height: 1.5; }
.care-list {
  margin: var(--space-2) 0 0;
  padding-left: var(--space-4);
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.5;
}
.care-list li { overflow-wrap: anywhere; }
.care-path { overflow-wrap: anywhere; font-family: var(--font-mono); }
.care-command {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
}
.care-code {
  min-width: 0;
  overflow-wrap: anywhere;
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}
.care-warning {
  margin: var(--space-3) 0 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--warning);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warning) 12%, transparent);
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.5;
}
.care-confirm {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-3);
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.5;
  cursor: pointer;
}
.care-confirm input { margin: 0; }
@media (pointer: coarse) {
  .care-confirm { min-height: var(--touch); }
  .care-confirm input { width: 1.5rem; height: 1.5rem; }
}
</style>
