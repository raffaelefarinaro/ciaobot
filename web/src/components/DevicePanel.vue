<template>
  <div class="device-panel">
    <!-- Role and host connection -->
    <section class="device-section" aria-labelledby="device-role-title">
      <div class="device-section-head">
        <h3 id="device-role-title">Role</h3>
      </div>

      <div v-if="!nodeStatus" class="loading">Loading node status&hellip;</div>
      <template v-else>
        <div class="device-kvs">
          <div class="device-kv device-kv--top">
            <span class="device-k">Current role</span>
            <span class="device-v">
              <span class="device-state">
                <span class="device-dot" :class="roleTone" aria-hidden="true"></span>
                {{ roleLabel }}
              </span>
              <span class="device-sub">
                <template v-if="isClient">
                  Tray and PWA tunnel to the host below, and automations run there, not here.
                </template>
                <template v-else-if="nodeStatusUnknown">
                  The saved role could not be verified. Repair it before using host or client actions.
                </template>
                <template v-else>
                  Schedules, loops and vault writes run here.
                </template>
              </span>
            </span>
          </div>
          <template v-if="isClient">
            <div class="device-kv">
              <span class="device-k">This device</span>
              <code class="device-v device-code" :title="deviceName">{{ deviceName }}</code>
            </div>
            <div class="device-kv">
              <span class="device-k">Host</span>
              <code class="device-v device-code" :title="hostUrl || undefined">{{ hostLabel }}</code>
            </div>
            <div v-if="nodeStatus.host_reachable != null" class="device-kv">
              <span class="device-k">Connection</span>
              <span class="device-v device-state">
                <span class="device-dot" :class="nodeStatus.host_reachable ? 'device-dot--ok' : 'device-dot--warn'" aria-hidden="true"></span>
                {{ nodeStatus.host_reachable ? 'Reachable' : 'Unreachable' }}
              </span>
            </div>
          </template>
        </div>

        <template v-if="nodeStatusUnknown">
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
            <button
              class="btn-danger btn-small"
              @click="forceHostFromUnknown"
              :disabled="nodePending !== null"
              title="Discard the unverifiable role and make this device the host"
            >
              Make this the host…
            </button>
          </div>
          <template v-if="showConnectForm">
            <p class="hint">
              This replaces the unverifiable role with a confirmed client connection.
              The host must have a PWA password.
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
        <!-- Client: this device → host -->
        <template v-else-if="isClient">
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
              class="btn-small"
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
          <p v-if="nativeSessionNote" class="hint native-session-warning">
            {{ nativeSessionNote }}
          </p>
        </template>

        <!-- Host: reachable addresses, connected clients, opt into client mode -->
        <template v-else>
          <div class="device-subsection">
            <h4 class="device-subtitle">Addresses</h4>
            <NodeAddresses />
          </div>
          <div class="device-subsection">
            <h4 class="device-subtitle">Connected clients</h4>
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
    </section>

    <!-- The local install, which no other screen can update in client mode -->
    <section class="device-section" aria-labelledby="device-app-title">
      <div class="device-section-head">
        <h3 id="device-app-title">Local app</h3>
        <div v-if="packageStatus" class="device-section-end">
          <button
            v-if="packageStatus.update_available"
            class="btn-primary btn-small"
            @click="openUpdatePanel"
            :disabled="updating || showUpdatePanel"
          >
            Update to {{ packageStatus.latest_version }}
          </button>
          <span v-else class="device-uptodate">Up to date</span>
        </div>
      </div>
      <p class="hint">
        The Ciaobot install on {{ deviceName }}.
        <template v-if="isClient">
          Settings &rarr; package update reports the host instead, so this is the only place
          that upgrades this computer.
        </template>
      </p>

      <div class="device-kvs">
        <div class="device-kv">
          <span class="device-k">Installed here</span>
          <code class="device-v device-code">{{ localVersion || '—' }}</code>
        </div>
        <div v-if="isClient" class="device-kv">
          <span class="device-k">Running on host</span>
          <code class="device-v device-code">{{ nodeStatus?.host_version || '—' }}</code>
        </div>
        <div class="device-kv">
          <span class="device-k">Local engine</span>
          <span class="device-v device-state">
            <span class="device-dot" :class="localReady ? 'device-dot--ok' : 'device-dot--warn'" aria-hidden="true"></span>
            {{ localReady ? 'Ready' : 'Starting…' }}
          </span>
        </div>
      </div>

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
        <h4 class="device-subtitle">What&rsquo;s new in {{ packageStatus?.latest_version }}</h4>
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
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import ConnectedClients from './ConnectedClients.vue'
import { errorMessage, apiErrorMessage, errorPayload } from '../lib/errorMessage'
import NodeAddresses from './NodeAddresses.vue'
import { api } from '../lib/api'
import { navigateToContent, replaceToContent } from '../lib/originNavigation'
import { askConfirm } from '../lib/confirm'
import type { NodeStatus, PackageStatus, PackageChangelog, PackageUpdateResult, ActionResult } from '../lib/types'

// Every request in this panel targets a never-proxied route, so it keeps
// working (and can disconnect) while the host is unreachable.
const nodeStatus = ref<NodeStatus | null>(null)
const nodeStatusError = ref(false)
const localVersion = ref('')
const localReady = ref(true)
const nodePending = ref<string | null>(null)
const nodeActionResult = ref('')
const nodeActionError = ref(false)
const hostUrlInput = ref('')
const hostPasswordInput = ref('')
const showConnectForm = ref(false)

function clientBridgeTarget(result: ActionResult): string | null {
  if (typeof result.bridge_url === 'string') return result.bridge_url
  return window.location.hostname === '127.0.0.1' ? null : '/'
}

function navigateAfterAuth(result: ActionResult): boolean {
  const target = clientBridgeTarget(result)
  if (!target) return false
  if (target.includes('/api/auth/bridge')) replaceToContent(target)
  else navigateToContent(target)
  return true
}

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
const nodeStatusUnknown = computed(
  () => nodeStatusError.value || nodeStatus.value?.state_valid === false || nodeStatus.value?.role === 'invalid',
)
const roleLabel = computed(() => (nodeStatusUnknown.value ? 'Unknown' : isClient.value ? 'Client' : 'Host'))
const roleTone = computed(() => (nodeStatusUnknown.value || isClient.value ? 'device-dot--warn' : 'device-dot--ok'))
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
    nodeStatusError.value = false
    if (isClient.value) showConnectForm.value = false
  } catch {
    nodeStatus.value = {
      node_id: '',
      role: 'invalid',
      mode: 'invalid',
      state_valid: false,
      active_since: null,
      last_handover: null,
      host_url: null,
      active_peer_url: null,
      has_host_session: false,
      peers: [],
    }
    nodeStatusError.value = true
  }
}

async function fetchLocalEngine() {
  try {
    const res = await fetch('/api/startup-status', { credentials: 'same-origin', redirect: 'manual' })
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
      if (!navigateAfterAuth(r)) {
        nodeActionError.value = true
        nodeActionResult.value = 'The client session bridge was not issued.'
      }
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
    const result = await api.post<ActionResult>('/api/auth', { token: password })
    hostPasswordInput.value = ''
    if (!navigateAfterAuth(result)) throw new Error('The client session bridge was not issued.')
    return
  } catch (e) {
    nodeActionError.value = true
    nodeActionResult.value = `Error: ${apiErrorMessage(e, 'Reconnect failed')}`
  }
  nodePending.value = null
}

// Recovery from an unverifiable role discards the saved state, so it asks
// first even though the forced handover itself never does.
async function forceHostFromUnknown() {
  if (!await askConfirm(
    'Discard the saved role and make this device the host? Automations and vault writes will run here.',
    { title: 'Make this the host?', confirmLabel: 'Make this the host', destructive: true },
  )) return
  return becomeHost(true)
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

// Externally-started CLI sessions (a terminal `claude` on this machine) are
// invisible to Ciaobot but write to the same workspace. Surface them near the
// handover controls so the operator knows before switching roles.
const nativeSessionNote = ref('')

async function fetchNativeSessions() {
  try {
    const r = await api.get<{ sessions: { session_id: string; pid: number; cwd: string }[] }>(
      '/api/native/sessions'
    )
    const count = r?.sessions?.length || 0
    nativeSessionNote.value = count
      ? `Warning: ${count} Claude Code CLI session${count > 1 ? 's' : ''} started outside Ciaobot ${count > 1 ? 'are' : 'is'} running on this workspace (pid ${r!.sessions.map(s => s.pid).join(', ')}). Terminal activity may conflict with handover.`
      : ''
  } catch {
    nativeSessionNote.value = ''
  }
}

onMounted(() => {
  fetchNodeStatus()
  fetchLocalEngine()
  fetchPackageStatus()
  void fetchNativeSessions()
})
</script>

<style scoped>
.device-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  width: 100%;
}
.native-session-warning {
  color: var(--warning);
}
.device-section + .device-section {
  margin-top: var(--space-6);
}
.device-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.device-section-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-height: 34px;
}
.device-section-head h3 {
  margin: 0;
  font-size: calc(16px * var(--font-scale));
  font-weight: 650;
  letter-spacing: -0.02em;
}
.device-section-end {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.device-uptodate {
  color: var(--fg3);
  font-size: var(--text-sm);
}
.device-subsection {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.device-subtitle {
  margin: var(--space-2) 0 0;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
}
/* Hairline key/value rows, the same vocabulary as the page rails. */
.device-kvs {
  border-top: 1px solid var(--border);
}
.device-kv {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  min-height: 44px;
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.device-kv--top { align-items: flex-start; }
.device-k {
  flex: 0 0 132px;
  color: var(--fg3);
}
.device-v {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--fg);
}
.device-code {
  overflow-wrap: anywhere;
  word-break: break-word;
}
.device-state {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg);
}
.device-sub {
  display: block;
  margin-top: 2px;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.45;
}
.device-dot {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: 50%;
  background: var(--fg3);
}
.device-dot--ok { background: var(--success); }
.device-dot--warn { background: var(--warning); }
@media (max-width: 480px) {
  .device-kv { flex-direction: column; align-items: flex-start; gap: 2px; }
  .device-k { flex-basis: auto; }
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
