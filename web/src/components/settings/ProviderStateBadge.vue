<template>
  <span class="provider-state" :class="`provider-state--${view.tone}`">{{ view.label }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { providerStateView } from '../../lib/providerState'
import type { ProviderState } from '../../lib/types'

const props = defineProps<{ connKey: string; state: ProviderState }>()

const view = computed(() => providerStateView(props.connKey, props.state))
</script>

<style scoped>
/* State as text with a leading mark: the words carry it, the mark only supports it. */
.provider-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
  color: var(--fg2);
  font-size: var(--text-sm);
  white-space: nowrap;
}
.provider-state::before {
  content: '';
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.provider-state--ok::before { background: var(--success); }
.provider-state--error { color: var(--error); }
/* Not installed is a choice, not a fault: a hollow mark and muted text, never the error colour. */
.provider-state--neutral::before {
  background: transparent;
  box-sizing: border-box;
  border: 1px solid var(--fg3);
}
</style>
