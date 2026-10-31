<template>
  <!-- Resume after restart (#2128): engine-wide, stored as resume_after_restart. -->
  <div class="card">
    <div class="settings-card-header">
      <h2 class="section-title">Resume after restart</h2>
      <p class="hint">
        When Ciaobot restarts or stops unexpectedly while a chat is answering, it asks that chat
        to continue once it is back. Chats waiting on a question or an approval, archived chats,
        automations and Ciaobot's own helper chats are left alone.
      </p>
    </div>
    <div class="settings-switch-row">
      <span class="hint">Continue interrupted chats</span>
      <button
        class="settings-switch"
        type="button"
        role="switch"
        :aria-checked="enabled"
        aria-label="Continue interrupted chats after a restart"
        :disabled="!routines || routinesSaving"
        @click="saveRoutines({ resume_after_restart: !enabled })"
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
const enabled = computed(() => props.routines?.resume_after_restart !== false)
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.settings-switch-row .hint {
  color: var(--fg2);
}
</style>
