<template>
  <div class="update-progress-overlay">
    <!-- The overlay's status region, and where focus lands when a caller treats
         this overlay as the modal it is (the Settings engine-update card does).
         `tabindex="-1"` keeps it out of the tab order: nothing here is
         actionable, so Tab belongs to the curtain that holds focus, not to a row
         in a log. Announced politely as the record's phase moves. -->
    <div
      ref="statusRegion"
      class="update-progress-content"
      role="status"
      aria-live="polite"
      tabindex="-1"
    >
      <div class="update-progress-head">
        <span class="wordmark wordmark--lg">ciaobot</span>
        <span class="update-progress-version">update · v{{ version || '…' }}</span>
      </div>

      <!-- The real job's phase, when there is a real job. Absent on the boot
           path, which is an animation and has no record behind it. -->
      <p v-if="phaseName" class="update-progress-phase">{{ phaseName }}</p>

      <!-- Mono progress bar: filled █, empty ░ -->
      <div class="update-progress" :aria-label="`Updating ${progressPercent} percent`">
        <span class="update-progress-track">{{ progressTrack }}</span>
        <span class="update-progress-pct">{{ progressPercent.toString().padStart(3, ' ') }}%</span>
      </div>

      <!-- Log lines, one per stage -->
      <ul class="update-progress-log" role="log">
        <li
          v-for="row in rows"
          :key="row.name"
          class="update-progress-log-row"
          :class="'is-' + row.status"
        >
          <span class="update-progress-log-ts">[{{ row.ts }}]</span>
          <span class="update-progress-log-name">{{ row.name }}</span>
          <span class="update-progress-log-dots">{{ row.dots }}</span>
          <span class="update-progress-log-status">{{ row.statusLabel }}</span>
        </li>
      </ul>

      <!-- Footer: blinking cursor while updating, ready line when done, and the
           record's own reason when the job failed. -->
      <div class="update-progress-foot">
        <template v-if="failureText">
          <span class="update-progress-failed">[failed] {{ failureText }}</span>
        </template>
        <template v-else-if="ready">
          <span class="update-progress-ready">[ok] ciaobot is up to date.</span>
        </template>
        <template v-else>
          <span class="update-progress-prompt">$</span>
          <span class="update-progress-prompt-text">updating</span>
          <span class="caret"></span>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  UPDATE_APPLIED_PHASE,
  isUpdateFailurePhase,
  updateFailureSentence,
  updatePhaseName,
} from '../lib/engineUpdate'

const props = defineProps<{
  version?: string
  finishing?: boolean
  /**
   * The persisted job's phase (`GET /api/update/status`). With it the rows are
   * the record's own phases, in the order the engine runs them, and the boot
   * timer never starts; without it this is the boot screen's staged animation,
   * which is what it has always been.
   */
  phase?: string
  /** The record's own reason, for a run that failed. */
  error?: string
}>()

/**
 * The boot animation's six stages. Untouched by the real-job path below: a run
 * nobody is performing is an animation, and it has always advanced on a timer.
 */
const STAGES = [
  'checking the current Ciaobot version',
  'preparing the local engine',
  'checking the signed release',
  'downloading the next hello',
  'installing the updated runtime',
  'getting ready to restart',
] as const

/**
 * The real run's rows, in the order `ciao/engine_update.py:PHASES` runs them.
 *
 * The record is the only clock this screen has, so a row is a phase and the
 * order is the coordinator's: `downloading` is row 2 and `verifying` row 3, as
 * they actually happen. A list that put a later phase on an earlier row moved
 * the bar backwards and un-completed a row on every ordinary update.
 */
const PHASE_ROWS: readonly string[] = [
  'resolving',
  'downloading',
  'verifying',
  'staging',
  'staged',
  'draining',
  'applying',
  'stopping',
  'swapping',
  'starting',
  'verifying_start',
  'applied',
]

/**
 * The rollback track, listed only once the record names a rollback.
 *
 * A run heading forward has no reason to show what it has not started, and
 * `rolling_back` has to read as the rollback it is rather than as whatever row
 * it would otherwise land on. `rolled_back` and `rollback_failed` are its two
 * terminals.
 */
const ROLLBACK_ROWS: readonly string[] = ['rolling_back', 'rolled_back', 'rollback_failed']

/** The phase of a run that stopped, naming no phase it stopped at. */
const FAILED_PHASE = 'failed'

const PROGRESS_WIDTH = 28
const DOTS_TARGET = 28

interface Row {
  name: string
  ts: string
  dots: string
  status: 'pending' | 'in_progress' | 'done' | 'error'
  statusLabel: string
}

const activeIndex = ref(0)
const startedAt = ref<Record<string, string>>({})
let stageTimer: number | null = null

/**
 * The overlay's status region, handed to a caller that treats this overlay as
 * the modal it is so focus can land inside it (SettingsView's engine update).
 */
const statusRegion = ref<HTMLElement | null>(null)

defineExpose({ statusRegion })

/** The record's phase in plain language; empty on the boot path. */
const phaseName = computed(() => (props.phase ? updatePhaseName(props.phase) : ''))

/**
 * Why the job failed, or ''. Never blank for a failed run, and never a success
 * line: an update that rolled back is not an update that is up to date.
 */
const failureText = computed(() => {
  if (!props.phase || !isUpdateFailurePhase(props.phase)) return ''
  const recorded = (props.error || '').trim()
  return recorded || updateFailureSentence(props.phase)
})

const ready = computed(() => !!props.finishing || props.phase === UPDATE_APPLIED_PHASE)

/** The rows this record's phase is listed against, or the boot screen's. */
const phaseRows = computed<readonly string[]>(() => {
  const phase = props.phase
  if (!phase) return STAGES
  // A phase with no row of its own is shown as a row of its own and nothing
  // else. `failed` names no phase it broke at, and a phase a newer engine
  // added is unknown here, so in both cases the record says the run stopped
  // here and says nothing about what came before: no earlier row may claim to
  // be done.
  if (phase === FAILED_PHASE) return [FAILED_PHASE]
  const track = ROLLBACK_ROWS.includes(phase)
    ? [...PHASE_ROWS, ...ROLLBACK_ROWS]
    : PHASE_ROWS
  return track.includes(phase) ? track : [phase]
})

/** The record's row on that list, or null on the boot path. */
const phaseIndex = computed<number | null>(() => {
  const phase = props.phase
  return phase ? phaseRows.value.indexOf(phase) : null
})

/** The stage the log is on: the record's, or the animation's. */
const currentIndex = computed(() => phaseIndex.value ?? activeIndex.value)

/**
 * How many rows are behind the record.
 *
 * A phase the run is in is live, so the boundary is that row. `applied` is the
 * one phase the run is not in any more — it landed — so every row is behind it.
 */
const rowsBehind = computed(() => {
  if (phaseIndex.value === null) return Math.min(currentIndex.value, STAGES.length)
  return props.phase === UPDATE_APPLIED_PHASE ? PHASE_ROWS.length : phaseIndex.value
})

/**
 * The bar's percentage.
 *
 * The forward track is the bar: `resolving` at 0%, `applied` at 100%. A rollback
 * holds the 100% mark because it is the response to a forward run that
 * finished, and the log and the phase line say which version and why; a record
 * that says only `failed` proves no phase completed, so it stays at the start
 * rather than claiming progress the record does not show.
 */
const progressPercent = computed(() => {
  if (phaseIndex.value === null) {
    const finished = Math.min(currentIndex.value, STAGES.length)
    return Math.round((finished / STAGES.length) * 100)
  }
  const total = PHASE_ROWS.length - 1
  const steps = Math.min(Math.max(rowsBehind.value, 0), total)
  return Math.round((steps / total) * 100)
})

const progressTrack = computed(() => {
  const filled = Math.round((progressPercent.value / 100) * PROGRESS_WIDTH)
  return '█'.repeat(filled) + '░'.repeat(PROGRESS_WIDTH - filled)
})

function pad(n: number): string {
  return n.toString().padStart(2, '0')
}

// Prefer the activation clock; fall back to a synthesized t+offset so pending
// stages still show something, like the boot screen. For a real job that offset
// is the record's own order and no clock: the record timestamps the run, not
// this screen.
function timestampFor(name: string, index: number): string {
  const iso = startedAt.value[name]
  if (iso) {
    const d = new Date(iso)
    if (!Number.isNaN(d.getTime())) {
      return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    }
  }
  return `t+${index.toString().padStart(2, '0')}`
}

const rows = computed<Row[]>(() =>
  phaseRows.value.map((phase, i) => {
    // The log's own voice is lower case, like the boot rows, and it is the same
    // vocabulary the phase line above uses. A phase this build has no name for
    // shows its identifier: better the record's word than an invented one.
    const name = phaseIndex.value === null
      ? phase
      : (updatePhaseName(phase) || phase).toLowerCase()
    const failed = !!failureText.value && i === currentIndex.value
    const status: Row['status'] = failed
      ? 'error'
      : i < rowsBehind.value
        ? 'done'
        : i === currentIndex.value
          ? 'in_progress'
          : 'pending'
    // The word first: the error color is a second signal, never the only one.
    const statusLabel = status === 'error'
      ? 'failed'
      : status === 'done'
        ? 'ok'
        : status === 'in_progress'
          ? '…'
          : 'wait'
    const dots = ' ' + '.'.repeat(Math.max(3, DOTS_TARGET - name.length))
    return { name, ts: timestampFor(phase, i), dots, status, statusLabel }
  }),
)

function advance() {
  if (props.finishing || activeIndex.value >= STAGES.length) return
  const name = STAGES[activeIndex.value]
  startedAt.value = { ...startedAt.value, [name]: new Date().toISOString() }
  activeIndex.value += 1
  stageTimer = window.setTimeout(advance, 900)
}

watch(() => props.finishing, (finishing) => {
  if (!finishing) return
  if (stageTimer) window.clearTimeout(stageTimer)
  activeIndex.value = STAGES.length
})

onMounted(() => {
  // The animation belongs to the boot path only. A real job is described by its
  // record, and a timer advancing rows nobody is performing would be a lie.
  if (props.phase === undefined) advance()
})

onUnmounted(() => {
  if (stageTimer) window.clearTimeout(stageTimer)
})
</script>

<style scoped>
.update-progress-overlay {
  position: fixed;
  inset: 0;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 300;
  padding: var(--space-4);
  /* Same faint scanline texture as the boot overlay. */
  background-image:
    linear-gradient(180deg, rgba(255, 77, 109, 0.04) 0%, transparent 60%),
    repeating-linear-gradient(0deg, rgba(255, 255, 255, 0.012) 0 1px, transparent 1px 3px);
}

.update-progress-content {
  width: 100%;
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  /* A real job lists a row per phase, which is taller than the boot screen's
     six. Scroll it rather than let a short window clip the reason a run failed
     off the bottom. */
  max-height: 100%;
  overflow-y: auto;
}

/* The app's focus ring (DESIGN: 2px accent) does not reach a region, so the
   overlay's focus target states itself the same way a control would. */
.update-progress-content:focus {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.update-progress-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  padding-bottom: var(--space-3);
  border-bottom: 1px dashed var(--border);
}
.update-progress-version {
  font-size: var(--text-xs);
  color: var(--fg3);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

/* The real job's phase, under the head. Muted: the log below is the progress. */
.update-progress-phase {
  margin: calc(-1 * var(--space-3)) 0 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}

/* Progress row: monospace bar + numeric percent on the right */
.update-progress {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: 14px;
  line-height: 1;
}
.update-progress-track {
  color: var(--accent);
  letter-spacing: -1px;
  flex: 1;
}
.update-progress-pct {
  color: var(--fg2);
  font-variant-numeric: tabular-nums;
  min-width: 4ch;
  text-align: right;
}

/* Log */
.update-progress-log {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--text-sm);
  line-height: 1.5;
}
.update-progress-log-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-1);
  white-space: nowrap;
  overflow: hidden;
  color: var(--fg3);
  opacity: 0.55;
  transition: opacity 200ms var(--ease), color 200ms var(--ease);
}
.update-progress-log-row.is-in_progress {
  color: var(--fg2);
  opacity: 1;
}
.update-progress-log-row.is-done {
  color: var(--fg2);
  opacity: 1;
}
.update-progress-log-ts {
  color: var(--fg3);
  flex-shrink: 0;
}
.update-progress-log-name {
  color: var(--fg);
  flex-shrink: 0;
}
.update-progress-log-row.is-pending .update-progress-log-name { color: var(--fg3); }
.update-progress-log-dots {
  color: var(--fg3);
  opacity: 0.5;
  flex: 1;
  overflow: hidden;
  text-overflow: clip;
  letter-spacing: 1px;
}
.update-progress-log-status {
  flex-shrink: 0;
  letter-spacing: 0.3px;
  text-transform: uppercase;
  font-size: var(--text-xs);
}
.update-progress-log-row.is-done .update-progress-log-status { color: var(--success); }
.update-progress-log-row.is-in_progress .update-progress-log-status { color: var(--accent); }
/* A failed row keeps the other rows' quiet grey: the run is over, not busy. */
.update-progress-log-row.is-error { color: var(--fg2); opacity: 1; }
.update-progress-log-row.is-error .update-progress-log-name { color: var(--error); }
.update-progress-log-row.is-error .update-progress-log-status { color: var(--error); }
.update-progress-log-row.is-in_progress .update-progress-log-status::after {
  content: "";
  display: inline-block;
  width: 0.4em;
  height: 0.9em;
  background: var(--accent);
  margin-left: 0.2em;
  vertical-align: text-bottom;
  animation: caret-blink 0.9s steps(2, end) infinite;
}

/* Footer */
.update-progress-foot {
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px dashed var(--border);
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-base);
  color: var(--fg2);
}
.update-progress-prompt {
  color: var(--accent);
  font-weight: 700;
}
.update-progress-prompt-text {
  color: var(--fg2);
}
.update-progress-ready {
  color: var(--success);
  font-weight: 600;
  animation: update-fade-in 500ms var(--ease);
}
/* The record's own reason. The leading [failed] is the text signal; the red is
   a second one, so a monochrome render still says what happened. */
.update-progress-failed {
  color: var(--error);
  font-weight: 600;
  overflow-wrap: anywhere;
  animation: update-fade-in 500ms var(--ease);
}

@keyframes update-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 600px) {
  .update-progress-log { font-size: var(--text-xs); }
  .update-progress { font-size: 12px; }
}
</style>