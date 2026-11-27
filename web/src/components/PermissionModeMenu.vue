<template>
  <!-- The chat's permission mode: the button names the current mode and what
       it allows; the menu lists the four modes with their meanings. -->
  <MenuSelect
    :model-value="modelValue"
    :options="options"
    :disabled="disabled"
    :labelled-by="labelledBy"
    :described-by="describedBy"
    empty-label="Unknown"
    empty-description="choose a mode"
    trigger-class="permission-mode-trigger"
    @select="onPick"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MenuSelect from './MenuSelect.vue'
import { isBridgeMode, PERMISSION_MODE_COPY, type BridgeMode } from '../lib/permissionModes'

const props = withDefaults(defineProps<{
  /** The chat's saved mode; anything that is not a known mode reads as Unknown. */
  modelValue?: string | null
  modes: BridgeMode[]
  disabled?: boolean
  /** Id of the visible label that names the control ("Permission mode"). */
  labelledBy?: string
  describedBy?: string
}>(), { modelValue: null, disabled: false, labelledBy: '', describedBy: '' })

const emit = defineEmits<{ select: [mode: BridgeMode] }>()

const options = computed(() => props.modes.map(mode => ({
  value: mode,
  label: PERMISSION_MODE_COPY[mode].name,
  description: PERMISSION_MODE_COPY[mode].allows,
})))

function onPick(value: string): void {
  if (isBridgeMode(value)) emit('select', value)
}
</script>
