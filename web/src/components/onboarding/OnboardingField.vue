<script setup lang="ts">
import { computed } from 'vue'
// One labelled control: label, control, anything that belongs under the
// control (chips, suggestions), help, then the error, each 8px apart.
const props = defineProps<{ id: string; label: string; help?: string; error?: string }>()
const helpId = computed(() => `${props.id}-help`)
const errorId = computed(() => `${props.id}-error`)
const describedby = computed(() => [helpId.value, props.error ? errorId.value : ''].filter(Boolean).join(' '))
</script>

<template>
  <div class="ob-field">
    <label :for="id">{{ label }}</label>
    <slot :describedby="describedby" :invalid="!!error" />
    <div v-if="$slots.below" class="ob-field-below"><slot name="below" /></div>
    <div :id="helpId" class="ob-field-help"><slot name="help">{{ help }}</slot></div>
    <p v-if="error" :id="errorId" class="ob-field-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.ob-field { display: grid; gap: var(--space-2, 8px); min-width: 0; }
label { font-weight: 600; }
.ob-field-help { display: grid; gap: var(--space-2, 8px); color: var(--fg2); line-height: 1.5; }
.ob-field-help:empty { display: none; }
.ob-field-help :deep(p) { margin: 0; }
.ob-field-help :deep(a) { color: var(--accent); }
.ob-field-below { display: flex; flex-wrap: wrap; gap: var(--space-2, 8px); }
.ob-field-error { margin: 0; color: var(--error); }
.ob-field :slotted(input) { width: 100%; min-height: var(--touch, 44px); padding: var(--space-2, 8px) var(--space-3, 12px); border: 1px solid var(--border-strong); border-radius: var(--radius-sm, 6px); background: var(--bg); color: var(--fg); font: inherit; }
.ob-field :slotted(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
