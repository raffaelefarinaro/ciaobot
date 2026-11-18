<template>
  <div class="update-progress-overlay">
    <!-- The package update's full-window progress view. A `/api/package/update`
         run leaves no persisted record for the screen to read, so these staged
         rows are the screen's own timed animation while the install is in
         flight, exactly as the boot screen advances its rows; a failed run
         returns to the Updates card, and a settled one hands the window to the
         app-level restart notice. -->
    <div class="update-progress-content">
      <div class="update-progress-head">
        <Wordmark size="lg" />
        <span class="update-progress-version">update · v{{ version || '…' }}</span>
      </div>

      <!-- Progress bar: a filled fraction of the available width -->
      <div
        class="update-progress"
        role="progressbar"
        :aria-valuemin="0"
        :aria-valuemax="100"
        :aria-valuenow="progressPercent"
        :aria-label="progressLabel"
      >
        <span class="update-progress-track" aria-hidden="true"><span class="update-progress-fill" :style="{ width: `${progressPercent}%` }"></span></span>
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

      <div class="update-progress-foot">
        <span class="update-progress-prompt">$</span>
        <span class="update-progress-prompt-text">updating</span>
        <span class="caret"></span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import Wordmark from './Wordmark.vue'
import { computed, onMounted, onUnmounted, ref } from 'vue'

defineProps<{
  /** The version the run is moving to; `…` while the card has no answer. */
  version?: string
}>()

/**
 * The six stages this screen advances on a timer. Nothing behind it is being
 * read, so the animation is the progress view's own clock, and it stops only
 * when the run does (the run's caller unmounts it).
 */
const STAGES = [
  'checking the current Ciaobot version',
  'preparing the local engine',
  'checking the signed release',
  'downloading the next hello',
  'installing the updated runtime',
  'getting ready to restart',
] as const

const DOTS_TARGET = 28

interface Row {
  name: string
  ts: string
  dots: string
  status: 'pending' | 'in_progress' | 'done'
  statusLabel: string
}

const activeIndex = ref(0)
const startedAt = ref<Record<string, string>>({})
let stageTimer: number | null = null

/** The bar's percentage: the first stage at 0%, the last at 100%. */
const progressPercent = computed(() =>
  Math.round((Math.min(activeIndex.value, STAGES.length) / STAGES.length) * 100),
)

const progressLabel = computed(() => `Updating ${progressPercent.value} percent`)

function pad(n: number): string {
  return n.toString().padStart(2, '0')
}

// Prefer the activation clock; fall back to a synthesized t+offset so pending
// stages still show something.
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
  STAGES.map((name, i) => {
    const status: Row['status'] = i < activeIndex.value
      ? 'done'
      : i === activeIndex.value
        ? 'in_progress'
        : 'pending'
    const statusLabel = status === 'done' ? 'ok' : status === 'in_progress' ? '…' : 'wait'
    const dots = ' ' + '.'.repeat(Math.max(3, DOTS_TARGET - name.length))
    return { name, ts: timestampFor(name, i), dots, status, statusLabel }
  }),
)

function advance() {
  if (activeIndex.value >= STAGES.length) return
  const name = STAGES[activeIndex.value]
  startedAt.value = { ...startedAt.value, [name]: new Date().toISOString() }
  activeIndex.value += 1
  stageTimer = window.setTimeout(advance, 900)
}

onMounted(() => advance())

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
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
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

/* Progress row: monospace bar + numeric percent on the right */
.update-progress {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: 14px;
  line-height: 1;
}
/* A bar that is a fraction of its container, not a fixed run of characters:
   text has an intrinsic width, which is what pushed the panel past its
   column and under the page rail. min-width: 0 lets the flex item shrink. */
.update-progress-track {
  flex: 1 1 0;
  min-width: 0;
  height: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  background: var(--bg3);
  overflow: hidden;
}
.update-progress-fill {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 200ms var(--ease);
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

@media (max-width: 600px) {
  .update-progress-log { font-size: var(--text-xs); }
  .update-progress { font-size: 12px; }
}
</style>
