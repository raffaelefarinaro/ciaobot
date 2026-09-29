<template>
  <div class="card">
    <div class="settings-card-header">
      <p class="section-title">Session insights</p>
    </div>
    <div class="insights-control">
      <span class="hint">
        When you archive a chat, Ciaobot reads it for durable learnings and files them into memory.
        Off stops that pass.
      </span>
      <button
        class="insights-toggle"
        type="button"
        role="switch"
        :aria-checked="enabled"
        aria-label="Automatic session insights"
        :disabled="!routines || routinesSaving"
        @click="saveRoutines({ insights_enabled: !enabled })"
      >
        <span class="switch-word">{{ enabled ? 'On' : 'Off' }}</span>
        <span class="switch-track" aria-hidden="true"></span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { RoutineSettings } from '../../lib/types'

// The routines settings are owned by SettingsView, which the Models tab shares.
const props = defineProps<{
  routines: RoutineSettings | null
  routinesSaving: boolean
  saveRoutines: (patch: Record<string, unknown>) => Promise<void>
}>()

const enabled = computed(() => props.routines?.insights_enabled === true)
</script>

<style scoped>
/* Shared settings-card scaffolding (mirrors SettingsView.vue so the card keeps
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
}

/* One hairline row: the explanation, a switch on the right. */
.insights-control {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  min-height: 56px;
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
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
.insights-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
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
</style>
