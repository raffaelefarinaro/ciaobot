<template>
  <div class="device-panel">
    <!-- Role and host connection -->
    <div class="card">
      <div class="device-card-header">
        <div>
          <p class="section-title">role</p>
          <p class="hint">
            <template v-if="!nodeStatus">Loading&hellip;</template>
            <template v-else-if="isClient">
              This device is a <strong>client</strong>: tray and PWA tunnel to the host below,
              and automations run there, not here.
            </template>
            <template v-else>
              This device is the <strong>host</strong>: schedules, loops and vault writes run here.
            </template>
          </p>
        </div>
        <div v-if="nodeStatus" class="device-card-actions">
          <span class="badge" :class="isClient ? 'badge--warn' : 'badge--success'">{{ roleLabel }}</span>
        </div>
      </div>

      <div v-if="!nodeStatus" class="loading">Loading node status&hellip;</div>
      <template v-else>
        <!-- Client: this device → host -->
        <template v-if="isClient">
          <div class="node-path" aria-label="Client connection">
            <div class="node-path-endpoint">
              <span class="node-path-label">this device</span>
              <code class="node-path-value" :title="deviceName">{{ deviceName }}</code>
            </div>
            <div class="node-path-link">
              <span class="node-path-arrow" aria-hidden="true">&rarr;</span>
              <span
                v-if="nodeStatus.host_reachable != null"
                class="badge"
                :class="nodeStatus.host_reachable ? 'badge--success' : 'badge--warn'"
              >
                {{ nodeStatus.host_reachable ? 'reachable' : 'unreachable' }}
              </span>
            </div>
            <div class="node-path-endpoint node-path-endpoint--host">
              <span class="node-path-label">host</span>
              <code class="node-path-value" :title="hostUrl || undefined">{{ hostLabel }}</code>
            </div>
          </div>

          <div v-if="nodeActionResult" class="action-result" :class="{ 'action-result--error': nodeActionError }">
            {{ nodeActionResult }}
          </div>

          <p class="hint">
            Disconnect asks the host to push its changes, then this machine pulls them and becomes host again.
          </p>
          <template v-if="!nodeStatus.has_host_session">
            <div class="action-result action-result--error">
              Host password session missing. Enter the host password to finish connecting.
            </div>
            <div class="device-form">
              <label class="device-field">
                <span class="device-field-label">Host password</span>
                <input
                  v-model="hostPasswordInput"
                  type="password"
                  class="device-input"
                  placeholder="Password set on the host"
                  autocomplete="off"
                  @keyup.enter="reconnectHostSession"
                />
              </label>
              <div class="action-row">
                <button
                  class="btn-primary btn-small"
                  @click="reconnectHostSession"
                  :disabled="!hostPasswordInput || nodePending !== null"
                >
                  {{ nodePending === 'reconnect' ? 'Reconnecting…' : 'Reconnect' }}
                </button>
              </div>
            </div>
          </template>
          <div class="action-row">
            <button
              class="btn-primary btn-small"
              @click="() => becomeHost(false)"
              :disabled="nodePending !== null"
            >
              {{ nodePending === 'handover' ? 'Disconnecting…' : 'Disconnect' }}
            </button>
            <button
              class="btn-danger btn-small"
              @click="() => becomeHost(true)"
              :disabled="nodePending !== null"
              title="Become host even if the host is offline (skip remote push)"
            >
              Force disconnect
            </button>
          </div>
        </template>

        <!-- Host: reachable addresses, connected clients, opt into client mode -->
        <template v-else>
          <NodeAddresses />
          <div class="device-subsection">
            <p class="section-title">connected clients</p>
            <ConnectedClients />
          </div>
          <div v-if="nodeActionResult" class="action-result" :class="{ 'action-result--error': nodeActionError }">
            {{ nodeActionResult }}
          </div>
          <div class="action-row">
            <button
              class="btn-small"
              @click="showConnectForm = !showConnectForm"
              :disabled="nodePending !== null"
            >
              {{ showConnectForm ? 'Cancel' : 'Connect as client…' }}
            </button>
          </div>
          <template v-if="showConnectForm">
            <p class="hint">
              This pauses local automations and points tray + PWA at another Ciaobot.
              That host must have a PWA password. Tailscale URLs work
              (e.g. http://100.x.x.x:8443).
            </p>
            <div class="device-form">
              <label class="device-field">
                <span class="device-field-label">Host URL</span>
                <input
                  v-model="hostUrlInput"
                  type="text"
                  class="device-input"
                  placeholder="http://100.x.x.x:8443"
                  @keyup.enter="connectAsClient"
                />
              </label>
              <label class="device-field">
                <span class="device-field-label">Host password</span>
                <input
                  v-model="hostPasswordInput"
                  type="password"
                  class="device-input"
                  placeholder="Password set on the host"
                  autocomplete="off"
                  @keyup.enter="connectAsClient"
                />
              </label>
              <div class="action-row">
                <button
                  class="btn-primary btn-small"
                  @click="connectAsClient"
                  :disabled="!hostUrlInput.trim() || !hostPasswordInput || nodePending !== null"
                >
                  {{ nodePending === 'connect' ? 'Connecting…' : 'Connect' }}
                </button>
              </div>
            </div>
          </template>
        </template>
      </template>
    </div>

    <!-- The local install, which no other screen can update in client mode -->
    <div class="card">
      <div class="device-card-header">
        <div>
          <p class="section-title">local app</p>
          <p class="hint">
            The Ciaobot install on {{ deviceName }}.
            <template v-if="isClient">
              Settings &rarr; package update reports the host instead, so this is the only place
              that upgrades this computer.
            </template>
          </p>
        </div>
        <div v-if="packageStatus" class="device-card-actions">
          <button
            :class="packageStatus.update_available ? 'btn-primary btn-small' : 'btn-small'"
            @click="openUpdatePanel"
            :disabled="!packageStatus.update_available || updating || showUpdatePanel"
          >
            {{ packageStatus.update_available ? `Update to ${packageStatus.latest_version}` : 'Up to date' }}
          </button>
        </div>
      </div>

      <dl class="device-facts">
        <div>
          <dt>installed here</dt>
          <dd><code>{{ localVersion || '—' }}</code></dd>
        </div>
        <div v-if="isClient">
          <dt>running on host</dt>
          <dd><code>{{ nodeStatus?.host_version || '—' }}</code></dd>
        </div>
        <div>
          <dt>local engine</dt>
          <dd>{{ localReady ? 'ready' : 'starting…' }}</dd>
        </div>
      </dl>

      <p v-if="versionSkew" class="hint hint--warn">
        This device runs {{ localVersion }} while the host runs {{ nodeStatus?.host_version }}.
        The UI you see is the host's, so the mismatch only affects this device's own tray and update
        path. Updating here brings them back in line.
      </p>

      <div v-if="packageLoading && !packageStatus" class="loading">Checking package status&hellip;</div>
      <p v-else-if="packageStatus?.error" class="hint hint--warn">
        Update check failed: {{ packageStatus.error }}
      </p>

      <div v-if="showUpdatePanel" class="device-form">
        <p class="section-title">What&rsquo;s new in {{ packageStatus?.latest_version }}</p>
        <div v-if="changelogLoading" class="loading">Loading changelog&hellip;</div>
        <template v-else>
          <ul v-if="changelog.commits && changelog.commits.length" class="device-changelog">
            <li v-for="c in changelog.commits" :key="c.sha || c.subject">
              <code v-if="c.sha">{{ c.sha }}</code>
              <span>{{ c.subject }}</span>
            </li>
          </ul>
          <p v-else class="hint">
            {{ changelog.error ? `Could not load changelog: ${changelog.error}` : 'No changelog details available.' }}
          </p>
        </template>
        <div class="action-row">
          <button class="btn-primary btn-small" @click="doUpdate" :disabled="updating">
            {{ updating ? 'Updating…' : 'Update & Restart' }}
          </button>
          <button class="btn-small" @click="showUpdatePanel = false" :disabled="updating">Cancel</button>
        </div>
      </div>
      <div v-if="updateResult" class="action-result">{{ updateResult }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import ConnectedClients from './ConnectedClients.vue'
import { errorMessage, apiErrorMessage, errorPayload } from '../lib/errorMessage'
import NodeAddresses from './NodeAddresses.vue'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'
import type { NodeStatus, PackageStatus, PackageChangelog, PackageUpdateResult, ActionResult } from '../lib/types'

// Every request in this panel targets a never-proxied route, so it keeps
// working (and can disconnect) while the host is unreachable.
const nodeStatus = ref<NodeStatus | null>(null)
const localVersion = ref('')
const localReady = ref(true)
const nodePending = ref<string | null>(null)
const nodeActionResult = ref('')
const nodeActionError = ref(false)
const hostUrlInput = ref('')
const hostPasswordInput = ref('')
const showConnectForm = ref(false)

const packageStatus = ref<PackageStatus | null>(null)
const packageLoading = ref(false)
const updating = ref(false)
const updateResult = ref('')
const showUpdatePanel = ref(false)
const changelogLoading = ref(false)
const changelog = ref<PackageChangelog>({ commits: [], compare_url: '', error: '' })

const isClient = computed(() => {
  const role = nodeStatus.value?.role
  return role === 'client' || role === 'standby'
})
const roleLabel = computed(() => (isClient.value ? 'client' : 'host'))
const deviceName = computed(() => nodeStatus.value?.node_id || 'this machine')
const hostUrl = computed(() => nodeStatus.value?.host_url || nodeStatus.value?.active_peer_url || '')
const hostLabel = computed(() => {
  const named = nodeStatus.value?.host_node_id
  if (named && hostUrl.value) return `${named} (${hostUrl.value})`
  return named || hostUrl.value || '—'
})
const versionSkew = computed(() => {
  const host = nodeStatus.value?.host_version
  return Boolean(isClient.value && host && localVersion.value && host !== localVersion.value)
})

async function fetchNodeStatus() {
  try {
    nodeStatus.value = await api.get<NodeStatus>('/api/node/status')
    if (isClient.value) showConnectForm.value = false
  } catch {
    /* leave null; the panel still renders its explanation */
  }
}

async function fetchLocalEngine() {
  try {
    const res = await fetch('/api/startup-status', { credentials: 'same-origin' })
    if (!res.ok) return
    const data = await res.json()
    localVersion.value = String(data.version || '')
    localReady.value = Boolean(data.overall_ready)
  } catch {
    /* best effort */
  }
}

async function fetchPackageStatus() {
  packageLoading.value = true
  try {
    packageStatus.value = await api.get<PackageStatus>('/api/device/package-status')
  } catch {
    /* best effort: the version facts above still render */
  } finally {
    packageLoading.value = false
  }
}

async function openUpdatePanel() {
  showUpdatePanel.value = true
  changelogLoading.value = true
  changelog.value = { commits: [], compare_url: '', error: '' }
  try {
    changelog.value = await api.get<PackageChangelog>('/api/device/changelog')
  } catch (e) {
    changelog.value = { commits: [], compare_url: '', error: errorMessage(e, 'unknown error') }
  } finally {
    changelogLoading.value = false
  }
}

async function doUpdate() {
  updating.value = true
  updateResult.value = `Updating ${deviceName.value} and restarting…`
  try {
    const res = await api.post<PackageUpdateResult>('/api/device/update')
    updateResult.value = res?.ok
      ? 'Updated. This device is restarting; reload in a few seconds.'
      : res?.error || 'Update failed'
  } catch (e) {
    updateResult.value = `Error: ${apiErrorMessage(e, 'update failed')}`
  }
  updating.value = false
}

async function connectAsClient() {
  const hostUrlValue = hostUrlInput.value.trim()
  const password = hostPasswordInput.value
  if (!hostUrlValue) return
  if (!password) {
    nodeActionError.value = true
    nodeActionResult.value =
      'Enter the host password. If the host has none yet, set one in Settings → PWA password on that machine first.'
    return
  }
  if (!await askConfirm(
    `Connect as client to ${hostUrlValue}? This machine stops being host and mirrors that Ciaobot.`,
    { title: 'Connect as client', confirmLabel: 'Connect as client' },
  )) return

  nodePending.value = 'connect'
  nodeActionResult.value = ''
  nodeActionError.value = false
  try {
    const r = await api.post<ActionResult>('/api/node/connect', { host_url: hostUrlValue, password })
    if (r?.ok) {
      hostPasswordInput.value = ''
      showConnectForm.value = false
      // Full reload: the UI bundle itself now comes from the host.
      window.location.assign('/')
      return
    }
    nodeActionError.value = true
    nodeActionResult.value = r?.error || 'Connect failed'
  } catch (e) {
    nodeActionError.value = true
    const detail = apiErrorMessage(e, 'Connect failed')
    nodeActionResult.value = errorPayload(e)?.password_required_on_host ? detail : `Error: ${detail}`
  }
  nodePending.value = null
}

async function reconnectHostSession() {
  const password = hostPasswordInput.value
  if (!password) return
  nodePending.value = 'reconnect'
  nodeActionResult.value = ''
  nodeActionError.value = false
  try {
    await api.post('/api/auth', { token: password })
    hostPasswordInput.value = ''
    window.location.assign('/')
    return
  } catch (e) {
    nodeActionError.value = true
    nodeActionResult.value = `Error: ${apiErrorMessage(e, 'Reconnect failed')}`
  }
  nodePending.value = null
}

async function becomeHost(force = false) {
  if (
    !force &&
    !await askConfirm(
      'Disconnect and become host here? The host will push its changes, then this machine pulls and resumes automations.',
      { title: 'Become host on this device?', confirmLabel: 'Disconnect and become host' },
    )
  ) {
    return
  }

  nodePending.value = 'handover'
  nodeActionResult.value = ''
  nodeActionError.value = false
  try {
    const r = await api.post<ActionResult>('/api/node/handover', {
      target_node_url: hostUrl.value,
      force,
    })
    if (r?.ok) {
      nodeActionResult.value = force
        ? 'Force disconnect complete. This device is now the host.'
        : 'Disconnected. This device is now the host.'
      await fetchNodeStatus()
    } else {
      nodeActionError.value = true
      nodeActionResult.value = r?.error || 'Disconnect failed'
    }
  } catch (e) {
    nodeActionError.value = true
    if (errorPayload(e)?.peer_unreachable) {
      if (await askConfirm('Host is unreachable. Force disconnect anyway (skip remote push)?', {
        title: 'Force disconnect',
        confirmLabel: 'Force disconnect',
      })) {
        nodePending.value = null
        return becomeHost(true)
      }
    }
    nodeActionResult.value = `Error: ${apiErrorMessage(e, 'Disconnect failed')}`
  }
  nodePending.value = null
}

onMounted(() => {
  fetchNodeStatus()
  fetchLocalEngine()
  fetchPackageStatus()
})
</script>

<style scoped>
.device-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  width: 100%;
}
.device-card-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.device-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  padding-bottom: var(--space-3);
  border-bottom: 1px solid var(--border);
}
.device-subsection {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.device-form {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--bg) 72%, transparent);
}
.device-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.device-field-label {
  font-size: var(--text-sm);
  color: var(--fg2);
}
.device-input {
  width: 100%;
  min-width: 0;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font-size: var(--text-sm);
  min-height: 38px;
  box-sizing: border-box;
}
.device-facts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-5);
  margin: 0;
}
.device-facts dt {
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--fg3, var(--fg2));
}
.device-facts dd {
  margin: 4px 0 0;
  font-size: var(--text-sm);
  color: var(--fg);
}
.device-changelog {
  margin: 0;
  padding-left: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--text-sm);
}
.device-changelog code {
  margin-right: var(--space-2);
  color: var(--fg2);
}
.node-path {
  display: flex;
  align-items: stretch;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.node-path-endpoint {
  flex: 1 1 140px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}
.node-path-endpoint--host {
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
  background: color-mix(in srgb, var(--accent) 5%, var(--bg));
}
.node-path-label {
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--fg3, var(--fg2));
}
.node-path-value {
  color: var(--fg);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
  word-break: break-word;
}
.node-path-link {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 72px;
}
.node-path-arrow {
  color: var(--accent);
  font-size: calc(18px * var(--font-scale));
  font-weight: 700;
  line-height: 1;
}
.action-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.action-result {
  font-size: var(--text-sm);
  color: var(--fg2);
  padding: 4px 0;
  overflow-wrap: anywhere;
}
.action-result--error {
  color: var(--error);
}
.loading {
  color: var(--fg2);
  font-size: var(--text-base);
}
</style>
