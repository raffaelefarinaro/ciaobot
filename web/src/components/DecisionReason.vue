<template>
  <div class="form-group decision-reason">
    <label :for="`${idPrefix}-reason`">{{ label }}</label>
    <div class="decision-reason-chips" role="group" aria-label="Quick reasons">
      <button
        v-for="chip in chips"
        :key="chip"
        type="button"
        class="btn-chip task-chip"
        :aria-pressed="active.includes(chip)"
        :disabled="disabled"
        @click="emit('update:modelValue', toggleChip(modelValue, chip))"
      >{{ chip }}</button>
    </div>
    <textarea
      :id="`${idPrefix}-reason`"
      rows="2"
      :maxlength="REASON_MAX"
      :value="modelValue"
      :disabled="disabled"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    ></textarea>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { REASON_CHIPS, REASON_MAX, chipsIn, toggleChip } from '../lib/decisionReasons'

const props = withDefaults(defineProps<{
  modelValue: string
  disabled?: boolean
  idPrefix: string
  /** The quick reasons offered; the task board's by default. */
  chips?: readonly string[]
  label?: string
}>(), { chips: () => REASON_CHIPS, label: 'Why? (optional)' })
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const active = computed<string[]>(() => chipsIn(props.modelValue, props.chips))
</script>

<style scoped>
.decision-reason-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin: var(--space-2) 0;
}
.decision-reason-chips .task-chip[aria-pressed='true'] {
  border-color: var(--accent);
  color: var(--accent);
}
.decision-reason-chips .task-chip:focus-visible,
.decision-reason textarea:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
@media (pointer: coarse) {
  .decision-reason-chips .task-chip,
  .decision-reason textarea {
    min-height: 2.75rem;
  }
}
</style>
