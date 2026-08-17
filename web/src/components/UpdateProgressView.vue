<template>
  <div class="update-progress-overlay">
    <div class="update-progress-content">
      <div class="update-progress-head">
        <span class="wordmark wordmark--lg">ciaobot</span>
        <span class="update-progress-version">update · v{{ version || '…' }}</span>
      </div>

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

      <!-- Footer: blinking cursor while updating, ready line when done -->
      <div class="update-progress-foot">
        <template v-if="finishing">
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

const props = defineProps<{
  version?: string
  finishing?: boolean
}>()

const STAGES = [
  'checking the current Ciaobot version',
  'preparing the local engine',
  'checking the signed release',
  'downloading the next hello',
  'installing the updated runtime',
  'getting ready to restart',
] as const

const PROGRESS_WIDTH = 28
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

const progressPercent = computed(() => {
  const finished = Math.min(activeIndex.value, STAGES.length)
  return Math.round((finished / STAGES.length) * 100)
})

const progressTrack = computed(() => {
  const filled = Math.round((progressPercent.value / 100) * PROGRESS_WIDTH)
  return '█'.repeat(filled) + '░'.repeat(PROGRESS_WIDTH - filled)
})

function pad(n: number): string {
  return n.toString().padStart(2, '0')
}

// Prefer the activation clock; fall back to a synthesized t+offset so
// pending stages still show something, like the boot screen.
function timestampFor(name: string): string {
  const iso = startedAt.value[name]
  if (iso) {
    const d = new Date(iso)
    if (!Number.isNaN(d.getTime())) {
      return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    }
  }
  const index = STAGES.indexOf(name as (typeof STAGES)[number])
  return `t+${index.toString().padStart(2, '0')}`
}

const rows = computed<Row[]>(() =>
  STAGES.map((name, i) => {
    const status = i < activeIndex.value ? 'done' : i === activeIndex.value ? 'in_progress' : 'pending'
    const statusLabel = status === 'done' ? 'ok' : status === 'in_progress' ? '…' : 'wait'
    const dots = ' ' + '.'.repeat(Math.max(3, DOTS_TARGET - name.length))
    return { name, ts: timestampFor(name), dots, status, statusLabel }
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
  advance()
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

@keyframes update-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 600px) {
  .update-progress-log { font-size: var(--text-xs); }
  .update-progress { font-size: 12px; }
}
</style>