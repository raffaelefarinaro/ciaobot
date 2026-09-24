<template>
  <span v-if="status" class="host-status-pill" :class="`host-status-pill--${status.tone}`">
    <span class="host-status-dot" aria-hidden="true" />
    <span class="host-status-text">{{ status.label }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed, inject } from 'vue'
import { CONNECTION_ROLE_KEY } from '../lib/connectionRole'

// Restates the connection role in the words the client-mode banner already
// uses. It says which node is serving this page, nothing about where data
// lives: that depends on the host's own providers, not on this badge.
const role = inject(CONNECTION_ROLE_KEY, null)

const status = computed(() => {
  const current = role?.value
  if (!current) return null
  if (current.kind === 'host') return { label: 'Host', tone: 'ok' }
  if (current.kind === 'client') {
    return current.reachable
      ? { label: `Client · ${current.hostLabel}`, tone: 'client' }
      : { label: 'Reconnecting…', tone: 'warn' }
  }
  return { label: 'Role unavailable', tone: 'warn' }
})
</script>

<style scoped>
.host-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  max-width: 28ch;
  color: var(--fg2);
  font-size: var(--text-sm);
  white-space: nowrap;
}

.host-status-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.host-status-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--success);
}

.host-status-pill--client .host-status-dot {
  background: var(--accent2);
}

.host-status-pill--warn .host-status-dot {
  background: var(--warning);
}
</style>
