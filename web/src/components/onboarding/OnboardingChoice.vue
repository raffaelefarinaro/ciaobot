<script setup lang="ts">
import { computed, inject, nextTick } from 'vue'
import { choiceGroupKey } from './choiceGroup'

const props = defineProps<{ value: string; disabled?: boolean }>()
const group = inject(choiceGroupKey)
if (!group) throw new Error('OnboardingChoice must be used inside OnboardingChoiceGroup')
const checked = computed(() => group.modelValue.value === props.value)
// The group owns the value. When it ignores a tap (a save is in flight), the
// browser has already moved the native radio, so put it back on the model.
async function pick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  group!.select(props.value)
  await nextTick()
  // Re-sync the whole group: moving one native radio unchecked another.
  input.closest('[role="radiogroup"]')?.querySelectorAll<HTMLInputElement>('input[type="radio"]')
    .forEach(radio => { radio.checked = radio.value === group!.modelValue.value })
}
const off = computed(() => group.disabled.value || !!props.disabled)
</script>

<template>
  <label class="ob-choice" :data-checked="checked" :data-disabled="off">
    <input type="radio" :name="group.name" :value="value" :checked="checked" :disabled="off" @change="pick">
    <span class="ob-choice-text">
      <span class="ob-choice-title"><slot /></span>
      <span v-if="$slots.description" class="ob-choice-desc"><slot name="description" /></span>
    </span>
  </label>
</template>

<style scoped>
.ob-choice { position: relative; display: flex; align-items: flex-start; gap: var(--space-3, 12px); min-height: var(--touch, 44px); padding: var(--space-3, 12px) var(--space-4, 16px); border: 1px solid var(--border); border-radius: var(--radius-sm, 6px); background: var(--bg); cursor: pointer; min-width: 0; }
.ob-choice[data-checked="true"] { border-color: var(--accent); }
.ob-choice[data-disabled="true"] { opacity: .55; cursor: not-allowed; }
.ob-choice:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
input { flex: 0 0 auto; width: 20px; height: 20px; margin: 0; accent-color: var(--accent); }
.ob-choice-text { display: grid; gap: var(--space-1, 4px); min-width: 0; line-height: 1.45; }
.ob-choice-title { overflow-wrap: anywhere; }
.ob-choice-desc { color: var(--fg2); }
</style>
