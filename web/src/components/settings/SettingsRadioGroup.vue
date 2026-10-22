<template>
  <div class="instance-toggle" role="radiogroup" :aria-label="label" @keydown="onKeydown">
    <button
      v-for="(option, index) in options"
      :key="option.value"
      ref="buttons"
      class="toggle-btn"
      type="button"
      role="radio"
      :aria-checked="modelValue === option.value"
      :class="{ active: modelValue === option.value }"
      :style="option.fontFamily ? { fontFamily: option.fontFamily } : undefined"
      :tabindex="index === tabStop ? 0 : -1"
      @click="emit('update:modelValue', option.value)"
    >
      {{ option.label }}
    </button>
  </div>
</template>

<script setup lang="ts" generic="T extends string">
import { computed, ref } from 'vue'

/**
 * A segmented control with the WAI-ARIA radio pattern: one tab stop (the
 * checked option), Arrow keys move and select, Home/End jump to the ends.
 */
const props = defineProps<{
  label: string
  options: { value: T; label: string; fontFamily?: string }[]
  modelValue: T | null
}>()
const emit = defineEmits<{ 'update:modelValue': [value: T] }>()

const buttons = ref<HTMLButtonElement[]>([])
const tabStop = computed(() => {
  const checked = props.options.findIndex((option) => option.value === props.modelValue)
  return checked === -1 ? 0 : checked
})

function onKeydown(event: KeyboardEvent) {
  if (event.altKey || event.ctrlKey || event.metaKey) return
  const count = props.options.length
  const current = buttons.value.findIndex((button) => button === document.activeElement)
  const from = current === -1 ? tabStop.value : current
  let next: number
  switch (event.key) {
    case 'ArrowRight': case 'ArrowDown': next = (from + 1) % count; break
    case 'ArrowLeft': case 'ArrowUp': next = (from - 1 + count) % count; break
    case 'Home': next = 0; break
    case 'End': next = count - 1; break
    default: return
  }
  event.preventDefault()
  buttons.value[next]?.focus()
  emit('update:modelValue', props.options[next].value)
}
</script>

<style scoped src="./settingsPanels.css"></style>
