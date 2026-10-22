<template>
  <div class="workspace-color-swatches" role="radiogroup" :aria-labelledby="labelledby">
    <button
      v-for="preset in visiblePresets"
      :key="preset.id"
      type="button"
      class="workspace-color-swatch"
      role="radio"
      :aria-checked="modelValue === preset.id"
      :aria-label="preset.label"
      :title="preset.label"
      :disabled="disabled"
      :class="{ active: modelValue === preset.id }"
      :style="{ '--swatch': preset.swatch }"
      @click="emit('update:modelValue', preset.id)"
    />
    <button
      type="button"
      class="workspace-color-swatch workspace-color-swatch--more"
      :aria-expanded="expanded"
      :aria-label="expanded ? 'Show fewer colours' : 'More colours'"
      :title="expanded ? 'Show fewer colours' : 'More colours'"
      :disabled="disabled"
      @click="expanded = !expanded"
    >{{ expanded ? '\u2212' : '+' }}</button>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { WORKSPACE_COLOR_PRESETS, type WorkspaceColorId } from '../../lib/workspaceColors'

const props = defineProps<{
  modelValue: WorkspaceColorId
  labelledby: string
  disabled?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: WorkspaceColorId] }>()

const expanded = ref(WORKSPACE_COLOR_PRESETS.some((p) => p.extra && p.id === props.modelValue))
// The chosen preset stays visible even if the disclosure is closed afterwards.
const visiblePresets = computed(() =>
  WORKSPACE_COLOR_PRESETS.filter((p) => !p.extra || expanded.value || p.id === props.modelValue),
)
</script>

<style scoped>
.workspace-color-swatches {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
/* 26px swatch; the selected one carries a ring in the text colour so the
   choice reads without relying on the swatch hue. */
.workspace-color-swatch {
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: var(--swatch);
  cursor: pointer;
  transition: box-shadow 120ms var(--ease), transform 120ms var(--ease);
}
.workspace-color-swatch:hover:not(:disabled) {
  transform: scale(1.08);
}
.workspace-color-swatch.active {
  box-shadow: 0 0 0 2px var(--bg), 0 0 0 4px var(--fg);
}
@media (pointer: coarse) {
  .workspace-color-swatch {
    width: var(--touch);
    height: var(--touch);
    background: radial-gradient(circle at center, var(--swatch) 0 13px, transparent 14px);
  }
  .workspace-color-swatch.active {
    box-shadow: none;
    background: radial-gradient(circle at center, var(--swatch) 0 13px, var(--bg) 14px 15px, var(--fg) 16px 17px, transparent 18px);
  }
}
.workspace-color-swatch:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.workspace-color-swatch:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.workspace-color-swatch--more {
  background: transparent;
  border: 1px dashed var(--border-strong);
  color: var(--fg2);
  font-size: var(--text-base);
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
@media (pointer: coarse) {
  .workspace-color-swatch--more {
    background: none;
  }
}
</style>
