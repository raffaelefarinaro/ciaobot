<template>
  <!-- Vault answer check: engine-wide, stored as verify_vault_answers. -->
  <div class="card">
    <div class="settings-card-header">
      <h2 class="section-title">Check vault answers</h2>
      <p class="hint">
        When an answer draws on notes from your vault, Ciaobot checks each claim against those notes
        with a small model and marks what they do not support. Costs one extra small model call per
        such answer.
      </p>
    </div>
    <div class="settings-switch-row">
      <span class="hint">Check answers against your notes</span>
      <button
        class="settings-switch"
        type="button"
        role="switch"
        :aria-checked="enabled"
        aria-label="Check vault answers against your notes"
        :disabled="!routines || routinesSaving"
        @click="saveRoutines({ verify_vault_answers: !enabled })"
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

// The settings store is owned by SettingsView.
const props = defineProps<{
  routines: RoutineSettings | null
  routinesSaving: boolean
  saveRoutines: (patch: Record<string, unknown>) => Promise<void>
}>()

// On by default: only an explicit false from the engine turns it off.
const enabled = computed(() => props.routines?.verify_vault_answers !== false)
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.settings-switch-row .hint {
  color: var(--fg2);
}
</style>
