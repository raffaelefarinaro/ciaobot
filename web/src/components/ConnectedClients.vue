<template>
  <div class="connected-clients">
    <p class="hint">
      Remote devices that currently have an open Ciaobot connection to this host.
    </p>
    <div v-if="loading" class="loading">Checking connections&hellip;</div>
    <div v-else-if="error" class="action-result">{{ error }}</div>
    <ul v-else-if="clients.length" class="client-list">
      <li v-for="client in clients" :key="client.id" class="client-row">
        <div class="client-main">
          <code class="client-host">{{ client.client_host }}</code>
          <span class="client-meta">
            <span class="badge">{{ kindLabel(client.kind) }}</span>
            <span class="client-since">since {{ formatTime(client.connected_at) }}</span>
          </span>
        </div>
      </li>
    </ul>
    <p v-else class="hint hint--section-empty">No remote clients connected.</p>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../lib/api'

interface ConnectedClient {
  id: string
  kind: 'chat' | 'events'
  client_host: string
  client_port: number
  user_agent: string
  connected_at: string
  is_local: boolean
  chat_id?: string
}

const clients = ref<ConnectedClient[]>([])
const loading = ref(true)
const error = ref('')
let interval: number | undefined

const POLL_SECONDS = 5

async function load() {
  try {
    const res = await api.get<{ clients: ConnectedClient[] }>('/api/node/connected-clients')
    clients.value = res.clients || []
    error.value = ''
  } catch (reason) {
    error.value = `Could not read connected clients: ${String(reason)}`
  } finally {
    loading.value = false
  }
}

function kindLabel(kind: string) {
  if (kind === 'chat') return 'chat'
  if (kind === 'events') return 'awareness'
  return kind
}

function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return iso
  }
}

onMounted(() => {
  void load()
  interval = window.setInterval(() => void load(), POLL_SECONDS * 1000)
})

onUnmounted(() => {
  if (interval) {
    clearInterval(interval)
  }
})
</script>

<style scoped>
.connected-clients {
  margin-top: var(--space-3);
}
.client-list {
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
}
.client-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
}
.client-main {
  display: flex;
  flex: 1 1 auto;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.client-host {
  overflow-wrap: anywhere;
}
.client-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.client-since {
  color: var(--fg3);
  font-size: 0.75rem;
}
</style>
