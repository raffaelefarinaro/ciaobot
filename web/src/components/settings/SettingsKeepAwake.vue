<template>
  <div v-if="supported" class="card">
    <div class="settings-card-header">
      <h2 class="section-title">Power</h2>
    </div>
    <div class="settings-switch-row">
      <div class="routine-info">
        <span class="routine-name">{{ label }}</span>
        <span id="keep-awake-description" class="routine-detail">{{ description }}</span>
      </div>
      <button
        class="settings-switch"
        type="button"
        role="switch"
        :aria-checked="enabled"
        :aria-label="label"
        aria-describedby="keep-awake-description"
        :disabled="!routines || routinesSaving"
        @click="saveRoutines({ keep_awake_while_agents_working: !enabled })"
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

const props = defineProps<{
  routines: RoutineSettings | null
  routinesSaving: boolean
  saveRoutines: (patch: Record<string, unknown>) => Promise<void>
}>()

const platform = computed(() => props.routines?.keep_awake_platform || '')
const supported = computed(() => platform.value === 'macos' || platform.value === 'windows')
const enabled = computed(() => props.routines?.keep_awake_while_agents_working === true)
const label = computed(() => platform.value === 'macos'
  ? 'Keep Mac awake while agents are working'
  : 'Keep Windows PC awake while agents are working')
const description = computed(() => platform.value === 'macos'
  ? 'Prevents idle sleep on the Mac running Ciaobot, not on the device viewing this page. Agent-launched background commands count too. The display may still sleep; closing the lid or other macOS power conditions may still put the Mac to sleep.'
  : 'Prevents idle sleep on the Windows PC running Ciaobot, not on the device viewing this page. Agent-launched background commands count too. The display may still turn off; Windows power settings or closing the lid may still put the PC to sleep.')
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.routine-detail {
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.45;
}

.settings-switch-row .routine-info {
  max-width: 72ch;
}
</style>
