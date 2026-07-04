<template>
  <div class="startup-overlay">
    <div class="startup-content">
      <div class="startup-head">
        <span class="wordmark wordmark--lg">ciaobot</span>
        <span class="startup-version">boot · v0.1</span>
      </div>

      <!-- Mono progress bar: filled █, partial ▓, empty ░ -->
      <div class="startup-progress" :aria-label="`Booting ${progressPercent} percent`">
        <span class="startup-progress-track">{{ progressTrack }}</span>
        <span class="startup-progress-pct">{{ progressPercent.toString().padStart(3, ' ') }}%</span>
      </div>

      <!-- Log lines, one per phase -->
      <ul class="startup-log" role="log">
        <li
          v-for="(phase, i) in phases"
          :key="phase.name"
          class="startup-log-row"
          :class="'is-' + phase.status"
        >
          <span class="startup-log-ts">[{{ timestampFor(phase, i) }}]</span>
          <span class="startup-log-name">{{ phaseLabel(phase.name) }}</span>
          <span class="startup-log-dots">{{ dotsFor(phase.name) }}</span>
          <span class="startup-log-status">{{ statusLabel(phase.status) }}</span>
          <span v-if="phase.message" class="startup-log-msg">// {{ phase.message }}</span>
        </li>
      </ul>

      <!-- Footer: blinking cursor while booting, ready line when done -->
      <div class="startup-foot">
        <template v-if="overallReady">
          <span class="startup-ready">[ok] ciaobot is online.</span>
        </template>
        <template v-else>
          <span class="startup-prompt">$</span>
          <span class="startup-prompt-text">booting</span>
          <span class="caret"></span>
          <button class="startup-skip" @click="$emit('skip')">[skip]</button>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface Phase {
  name: string
  status: string
  message: string
  started_at: string | null
  finished_at: string | null
}

const props = defineProps<{
  phases: Phase[]
  overallReady: boolean
}>()

defineEmits<{
  skip: []
}>()

const progressPercent = computed(() => {
  if (props.phases.length === 0) return 0
  const finished = props.phases.filter(p => p.status === 'done' || p.status === 'failed').length
  return Math.round((finished / props.phases.length) * 100)
})

const PROGRESS_WIDTH = 28
const progressTrack = computed(() => {
  const filled = Math.round((progressPercent.value / 100) * PROGRESS_WIDTH)
  return '█'.repeat(filled) + '░'.repeat(PROGRESS_WIDTH - filled)
})

function phaseLabel(name: string): string {
  const labels: Record<string, string> = {
    connect_claude_code: 'connect_claude_code',
    connect_pi: 'connect_pi',
    sync_workspace: 'sync_workspace',
    refresh_vault_index: 'refresh_vault_index',
    rebuild_pwa: 'rebuild_pwa',
    update_skills: 'update_skills',
    server_starting: 'server_starting',
  }
  return labels[name] || name
}

// Right-pad each phase name with dots to a fixed width so the status column lines up.
const DOTS_TARGET = 28
function dotsFor(name: string): string {
  const label = phaseLabel(name)
  const remaining = DOTS_TARGET - label.length
  return ' ' + '.'.repeat(Math.max(3, remaining))
}

function statusLabel(s: string): string {
  if (s === 'done') return 'ok'
  if (s === 'failed') return 'fail'
  if (s === 'in_progress') return '…'
  return 'wait'
}

function timestampFor(phase: Phase, index: number): string {
  // Prefer started_at; fall back to a synthesized t+offset so something always shows.
  if (phase.started_at) {
    try {
      const d = new Date(phase.started_at)
      return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    } catch {
      // fall through
    }
  }
  return `t+${index.toString().padStart(2, '0')}`
}
function pad(n: number): string {
  return n.toString().padStart(2, '0')
}
</script>

<style scoped>
.startup-overlay {
  position: fixed;
  inset: 0;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
  padding: var(--space-4);
  /* Faint scanline texture only on the overlay surface. */
  background-image:
    linear-gradient(180deg, rgba(255, 77, 109, 0.04) 0%, transparent 60%),
    repeating-linear-gradient(0deg, rgba(255, 255, 255, 0.012) 0 1px, transparent 1px 3px);
}

.startup-content {
  width: 100%;
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.startup-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  padding-bottom: var(--space-3);
  border-bottom: 1px dashed var(--border);
}
.startup-version {
  font-size: var(--text-xs);
  color: var(--fg3);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

/* Progress row: monospace bar + numeric percent on the right */
.startup-progress {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: 14px;
  line-height: 1;
}
.startup-progress-track {
  color: var(--accent);
  letter-spacing: -1px;
  flex: 1;
  /* Render the .░ portion in fg3 by letting the accent paint over the whole
     string and dimming the empty cells via a subtle CSS trick: use color-stop.
     Simpler: keep one color, the eye reads "more vs less" naturally. */
}
.startup-progress-pct {
  color: var(--fg2);
  font-variant-numeric: tabular-nums;
  min-width: 4ch;
  text-align: right;
}

/* Log */
.startup-log {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--text-sm);
  line-height: 1.5;
}
.startup-log-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-1);
  white-space: nowrap;
  overflow: hidden;
  color: var(--fg3);
  opacity: 0.55;
  transition: opacity 200ms var(--ease), color 200ms var(--ease);
}
.startup-log-row.is-in_progress {
  color: var(--fg2);
  opacity: 1;
}
.startup-log-row.is-done {
  color: var(--fg2);
  opacity: 1;
}
.startup-log-row.is-failed {
  color: var(--error);
  opacity: 1;
}

.startup-log-ts {
  color: var(--fg3);
  flex-shrink: 0;
}
.startup-log-name {
  color: var(--fg);
  flex-shrink: 0;
}
.startup-log-row.is-pending .startup-log-name { color: var(--fg3); }
.startup-log-dots {
  color: var(--fg3);
  opacity: 0.5;
  flex: 1;
  overflow: hidden;
  text-overflow: clip;
  letter-spacing: 1px;
}
.startup-log-status {
  flex-shrink: 0;
  letter-spacing: 0.3px;
  text-transform: uppercase;
  font-size: var(--text-xs);
}
.startup-log-row.is-done .startup-log-status { color: var(--success); }
.startup-log-row.is-failed .startup-log-status { color: var(--error); }
.startup-log-row.is-in_progress .startup-log-status { color: var(--accent); }
.startup-log-row.is-in_progress .startup-log-status::after {
  content: "";
  display: inline-block;
  width: 0.4em;
  height: 0.9em;
  background: var(--accent);
  margin-left: 0.2em;
  vertical-align: text-bottom;
  animation: caret-blink 0.9s steps(2, end) infinite;
}
.startup-log-msg {
  color: var(--fg3);
  font-size: var(--text-xs);
  margin-left: var(--space-2);
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Footer */
.startup-foot {
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px dashed var(--border);
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-base);
  color: var(--fg2);
}
.startup-prompt {
  color: var(--accent);
  font-weight: 700;
}
.startup-prompt-text {
  color: var(--fg2);
}
.startup-ready {
  color: var(--success);
  font-weight: 600;
  animation: fadeIn 500ms var(--ease);
}
.startup-skip {
  margin-left: auto;
  background: none;
  border: 1px solid var(--border);
  color: var(--fg2);
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-family: inherit;
  font-size: var(--text-sm);
  letter-spacing: 0.3px;
  transition: color 120ms var(--ease), border-color 120ms var(--ease);
}
.startup-skip:hover { color: var(--fg); border-color: var(--fg2); }

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 600px) {
  .startup-log { font-size: var(--text-xs); }
  .startup-log-msg { display: none; }
  .startup-progress { font-size: 12px; }
}
</style>
