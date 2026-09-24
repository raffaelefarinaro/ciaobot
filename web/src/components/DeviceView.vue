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
          <template v-else-if="nodeStatusUnknown">
            The saved node role could not be verified; device recovery is available below.
          </template>
        </p>
      </div>
      <div class="device-header-actions">
        <span class="badge" :class="nodeStatusUnknown ? 'badge--warn' : isClient ? 'badge--warn' : 'badge--success'">{{ roleLabel }}</span>
        <a class="btn-small" :href="deviceHref('/device/return')">back to app</a>
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
import { deviceHref } from '../lib/originNavigation'

const nodeStatus = ref<NodeStatus | null>(null)

const isClient = computed(() => {
  const role = nodeStatus.value?.role
  return role === 'client' || role === 'standby'
})
const nodeStatusUnknown = computed(
  () => !nodeStatus.value || nodeStatus.value.state_valid === false || nodeStatus.value.role === 'invalid',
)
const roleLabel = computed(() => (nodeStatusUnknown.value ? 'unknown' : isClient.value ? 'client' : 'host'))
const deviceName = computed(() => nodeStatus.value?.node_id || 'this machine')

function parseNodeStatus(value: unknown): NodeStatus {
  if (!value || typeof value !== 'object') throw new Error('invalid node status')
  const data = value as Record<string, unknown>
  const role = data.role
  if (
    typeof data.node_id !== 'string'
    || !['host', 'client', 'active', 'standby', 'invalid'].includes(String(role))
    || data.state_valid !== true
  ) {
    throw new Error('invalid node status')
  }
  return value as NodeStatus
}

async function fetchNodeStatus() {
  try {
    nodeStatus.value = parseNodeStatus(await api.get<unknown>('/api/node/status'))
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
