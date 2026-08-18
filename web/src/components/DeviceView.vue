<template>
  <div class="page device-page">
    <header class="page-header device-header">
      <div>
        <h2>this device</h2>
        <p class="hint">
          Everything on this page is about
          <code>{{ deviceName }}</code>, the computer in front of you.
          <template v-if="isClient">
            Chats, automations and Settings belong to the host and are shown as they are there.
          </template>
        </p>
      </div>
      <div class="device-header-actions">
        <span class="badge" :class="isClient ? 'badge--warn' : 'badge--success'">{{ roleLabel }}</span>
        <router-link class="btn-small" to="/">back to app</router-link>
      </div>
    </header>

    <DevicePanel ref="panel" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import DevicePanel from './DevicePanel.vue'
import { api } from '../lib/api'
import type { NodeStatus } from '../lib/types'

const nodeStatus = ref<NodeStatus | null>(null)

const isClient = computed(() => {
  const role = nodeStatus.value?.role
  return role === 'client' || role === 'standby'
})
const roleLabel = computed(() => (isClient.value ? 'client' : 'host'))
const deviceName = computed(() => nodeStatus.value?.node_id || 'this machine')

async function fetchNodeStatus() {
  try {
    nodeStatus.value = await api.get<NodeStatus>('/api/node/status')
  } catch {
    /* leave null; the page still renders its explanation */
  }
}

onMounted(() => {
  fetchNodeStatus()
})
</script>

<style scoped>
/* Standalone route (not inside ChatLayout) so it stays usable with the host
   down: no sidebar, no chat stores, only never-proxied calls. */
.device-page {
  max-width: 1040px;
  gap: var(--space-4);
  padding: calc(var(--space-5) + var(--safe-top)) calc(var(--space-5) + var(--safe-right))
           calc(var(--space-5) + var(--safe-bottom)) calc(var(--space-5) + var(--safe-left));
}
.device-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  flex-wrap: wrap;
}
.device-header-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
</style>
