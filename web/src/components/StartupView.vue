<template>
  <div class="startup-overlay">
    <div class="startup-content" role="status" aria-live="polite">
      <span class="wordmark wordmark--md">ciaobot</span>
      <h1>{{ overallReady ? 'Connected to Ciaobot' : 'Connecting to Ciaobot' }}</h1>
      <p class="startup-description">
        {{ phases.length ? 'Getting your workspace ready.' : 'Checking the connection to your workspace.' }}
      </p>

      <ul v-if="phases.length" class="startup-phases" aria-label="Connection progress">
        <li v-for="phase in phases" :key="phase.name" class="startup-phase" :class="'is-' + phase.status">
          <span class="startup-phase-name">{{ phaseLabel(phase.name) }}</span>
          <span class="startup-phase-status">{{ statusLabel(phase.status) }}</span>
          <span v-if="phase.message" class="startup-phase-message">{{ phase.message }}</span>
        </li>
      </ul>
      <div v-else class="startup-wait" aria-hidden="true"><span></span></div>

      <button v-if="!overallReady" class="startup-continue" type="button" @click="$emit('skip')">
        Continue to app
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
interface Phase {
  name: string
  status: string
  message: string
  started_at: string | null
  finished_at: string | null
}

defineProps<{
  phases: Phase[]
  overallReady: boolean
}>()

defineEmits<{
  skip: []
}>()

function phaseLabel(name: string): string {
  const labels: Record<string, string> = {
    connect_claude_code: 'Connecting to Claude Code',
    refresh_vault_index: 'Preparing your notes',
    update_skills: 'Preparing skills',
    server_starting: 'Starting Ciaobot',
  }
  return labels[name] || name.replaceAll('_', ' ')
}

function statusLabel(status: string): string {
  if (status === 'done') return 'Ready'
  if (status === 'failed') return 'Needs attention'
  if (status === 'in_progress') return 'In progress'
  return 'Waiting'
}
</script>

<style scoped>
.startup-overlay {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: grid;
  place-items: center;
  padding: max(var(--page-gutter, 16px), env(safe-area-inset-top, 0px)) max(var(--page-gutter, 16px), env(safe-area-inset-right, 0px)) max(var(--page-gutter, 16px), env(safe-area-inset-bottom, 0px)) max(var(--page-gutter, 16px), env(safe-area-inset-left, 0px));
  background: var(--bg);
}
.startup-content {
  width: 100%;
  max-width: 34rem;
}
.startup-content h1 {
  margin: var(--space-5) 0 var(--space-2);
  color: var(--fg);
  font-size: calc(20px * var(--font-scale));
  font-weight: 700;
  letter-spacing: -0.01em;
}
.startup-description {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-base);
  line-height: 1.5;
}
.startup-wait {
  width: 100%;
  height: 2px;
  margin-top: var(--space-5);
  overflow: hidden;
  background: var(--border);
}
.startup-wait span {
  display: block;
  width: 30%;
  height: 100%;
  background: var(--accent);
  animation: connecting 1.8s var(--ease) infinite alternate;
}
.startup-phases {
  list-style: none;
  padding: 0;
  margin: var(--space-5) 0 0;
  border-top: 1px solid var(--border);
}
.startup-phase {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: var(--space-1) var(--space-3);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
  color: var(--fg2);
  font-size: var(--text-sm);
}
.startup-phase-name { color: var(--fg); }
.startup-phase-status { white-space: nowrap; }
.startup-phase.is-in_progress .startup-phase-status { color: var(--accent); }
.startup-phase.is-done .startup-phase-status { color: var(--success); }
.startup-phase.is-failed .startup-phase-status { color: var(--error); }
.startup-phase-message {
  flex-basis: 100%;
  color: var(--fg2);
}
.startup-continue {
  display: block;
  min-height: var(--touch);
  margin-top: var(--space-5);
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.startup-continue:hover { color: var(--fg); border-color: var(--fg2); }
.startup-continue:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@keyframes connecting {
  from { transform: translateX(0); }
  to { transform: translateX(230%); }
}
@media (prefers-reduced-motion: reduce) {
  .startup-wait span { animation: none; width: 100%; }
}
</style>
