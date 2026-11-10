<template>
  <div v-if="uploads.length" class="composer-uploads">
    <div v-for="u in uploads" :key="u.id" class="composer-upload">
      <div class="composer-upload-row">
        <span class="composer-upload-label" role="status">{{ phaseLabel(u) }}</span>
        <span class="composer-upload-pct" aria-hidden="true">{{ percent(u) }}%</span>
      </div>
      <progress class="composer-upload-bar" max="100" :value="percent(u)" :aria-label="phaseLabel(u)"></progress>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { ComposerUpload } from '../composables/useChatComposer'

defineProps<{ uploads: ComposerUpload[] }>()

function isProcessing(u: ComposerUpload): boolean {
  return u.total <= 0 || u.loaded >= u.total
}

function percent(u: ComposerUpload): number {
  return u.total > 0 ? Math.min(100, Math.round((u.loaded / u.total) * 100)) : 100
}

function phaseLabel(u: ComposerUpload): string {
  return isProcessing(u) ? `Processing ${u.label}…` : `Loading ${u.label}…`
}
</script>

<style scoped>
/* Same 12px inset as the chips and the textarea text below. */
.composer-uploads { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-2) 12px 0; }
.composer-upload-row { display: flex; justify-content: space-between; gap: 8px; font-size: var(--text-xs); color: var(--fg2); }
.composer-upload-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.composer-upload-pct { flex: none; font-variant-numeric: tabular-nums; }
.composer-upload-bar { appearance: none; -webkit-appearance: none; width: 100%; height: 4px; border: 0; border-radius: 2px; background: var(--bg3); overflow: hidden; }
.composer-upload-bar::-webkit-progress-bar { background: var(--bg3); border-radius: 2px; }
.composer-upload-bar::-webkit-progress-value { background: var(--accent); border-radius: 2px; transition: width 120ms var(--ease); }
.composer-upload-bar::-moz-progress-bar { background: var(--accent); border-radius: 2px; }
@media (prefers-reduced-motion: reduce) { .composer-upload-bar::-webkit-progress-value { transition: none; } }
</style>
