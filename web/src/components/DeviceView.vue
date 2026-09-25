<template>
  <div class="device-page">
    <header class="device-header">
      <a class="device-back" :href="deviceHref('/device/return')">
        <span aria-hidden="true">&larr;</span> Back to Ciaobot
      </a>
      <h2>This device</h2>
    </header>

    <div class="page-grid device-grid">
      <div class="page-main">
        <p class="hint device-intro">
          Everything here is about
          <code>{{ deviceName }}</code>, the computer in front of you.
          <template v-if="isClient">
            Chats, automations and Settings belong to the host and are shown as they are there.
          </template>
        </p>
        <DevicePanel ref="panel" />
      </div>
      <aside class="page-rail device-rail" aria-label="About this page">
        <h3 class="rail-title">Why this page</h3>
        <div class="rail-kvs">
          <div class="rail-kv">
            <span>Role</span>
            <strong :class="{ 'rail-attention': nodeStatusUnknown }">{{ roleLabel }}</strong>
          </div>
        </div>
        <p class="rail-note">
          It stays on this machine's own address, so it works even when the host is unreachable.
        </p>
      </aside>
    </div>
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
const roleLabel = computed(() => (nodeStatusUnknown.value ? 'Unknown' : isClient.value ? 'Client' : 'Host'))
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
   down: no sidebar, no chat stores, only never-proxied calls. It still uses
   the shared page grid so it reads like every other pane. */
.device-page {
  height: 100%;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
  padding: var(--safe-top) var(--safe-right) calc(var(--space-6) + var(--safe-bottom)) var(--safe-left);
}
.device-header {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-height: 56px;
  max-width: var(--page-max);
  margin: 0 auto var(--space-5);
  padding-inline: var(--page-gutter);
  border-bottom: 1px solid var(--border);
}
.device-header h2 {
  margin: 0;
  font-size: calc(16px * var(--font-scale));
  font-weight: 700;
  letter-spacing: -0.02em;
}
.device-back {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 36px;
  color: var(--fg2);
  font-size: var(--text-sm);
  text-decoration: none;
  border-radius: var(--radius-sm);
}
.device-back:hover { color: var(--fg); }
.device-intro { margin-bottom: var(--space-5); }
/* The shared page grid collapses on the chat-pane container, which this
   standalone route does not have: collapse on the viewport instead. */
@media (max-width: 940px) {
  .device-grid { grid-template-columns: minmax(0, 1fr); gap: var(--space-6); }
  .device-rail { position: static; }
}
@media (pointer: coarse) {
  .device-back { min-height: var(--touch); }
}
</style>
