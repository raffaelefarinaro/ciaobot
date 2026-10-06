<template>
  <div class="card">
    <div class="settings-card-header">
      <p class="section-title">Session insights</p>
    </div>
    <div class="settings-switch-row">
      <span class="hint">
        When you archive a chat, Ciaobot reads it for durable learnings and files them into memory.
        Off stops that pass.
      </span>
      <button
        class="settings-switch"
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

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.settings-switch-row .hint {
  max-width: 72ch;
  color: var(--fg3);
}
</style>
