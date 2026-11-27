<template>
  <DockedWindow class="unsure-rail" title="How this works" title-id="unsure-rail-title">
    <div class="unsure-rail-counts" role="group" aria-label="Unsure list">
      <div class="unsure-rail-kv">
        <span>Waiting for you</span>
        <span class="unsure-rail-value">{{ counts ? counts.waiting.toLocaleString() : '—' }}</span>
      </div>
      <div class="unsure-rail-kv">
        <span>Approved, not applied yet</span>
        <span class="unsure-rail-value">{{ counts ? counts.ticked.toLocaleString() : '—' }}</span>
      </div>
    </div>
    <p class="unsure-rail-text">
      Approve an item and the next Workspace care run writes it where it belongs. Reject removes it.
      Discuss opens a chat to settle it with Ciaobot.
    </p>
    <p class="unsure-rail-text">
      A new category, a change to a code repository's instructions and a rule change are settled in a chat, so Approve opens one.
    </p>
  </DockedWindow>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useUnsureStore } from '../stores/unsure'
import DockedWindow from './DockedWindow.vue'

/** Memory · Unsure's rail window: the two counts and what a click does. */
const store = useProjectStore()
const unsure = useUnsureStore()
const counts = computed(() => (unsure.list?.workspace === store.activeWorkspace ? unsure.list.counts : null))
</script>

<style scoped>
.unsure-rail-counts { display: flex; flex-direction: column; }
.unsure-rail-kv {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-height: 40px;
  border-bottom: 1px solid var(--border);
  color: var(--fg);
}
.unsure-rail-value {
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  font-variant-numeric: tabular-nums;
}
.unsure-rail-text {
  margin: 10px 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}
</style>
