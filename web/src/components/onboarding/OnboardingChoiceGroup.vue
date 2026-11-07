<script setup lang="ts">
import { provide, toRef, useId } from 'vue'
import { choiceGroupKey } from './choiceGroup'

// Replaces fieldset + legend + bare radios. `list` and `cards` stack the
// choices 12px apart; `segmented` lays them in one row that wraps.
const props = withDefaults(defineProps<{
  label: string
  name: string
  modelValue: string
  disabled?: boolean
  layout?: 'list' | 'cards' | 'segmented'
}>(), { disabled: false, layout: 'list' })
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const labelId = useId()
provide(choiceGroupKey, {
  name: props.name,
  modelValue: toRef(props, 'modelValue'),
  disabled: toRef(props, 'disabled'),
  select: value => emit('update:modelValue', value),
})
</script>

<template>
  <div class="ob-group">
    <div :id="labelId" class="ob-group-label">{{ label }}</div>
    <div class="ob-choices" role="radiogroup" :aria-labelledby="labelId" :data-layout="layout"><slot /></div>
    <slot name="after" />
  </div>
</template>

<style scoped>
.ob-group { display: grid; gap: var(--space-3, 12px); min-width: 0; }
.ob-group-label { font-weight: 600; }
.ob-choices { display: grid; gap: var(--space-3, 12px); }
.ob-choices[data-layout="segmented"] { display: flex; flex-wrap: wrap; }
.ob-choices[data-layout="cards"] { grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr)); }
/* Segmented rows read as one control: the native radio stays for keyboard and
   assistive tech but is not drawn. */
.ob-choices[data-layout="segmented"] :deep(.ob-choice) { align-items: center; justify-content: center; text-align: center; }
.ob-choices[data-layout="segmented"] :deep(.ob-choice input) { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
.ob-choices[data-layout="segmented"] :deep(.ob-choice[data-checked="true"]) { background: var(--accent); color: var(--on-accent); }
</style>
