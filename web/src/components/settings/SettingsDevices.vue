<template>
  <div class="card">
    <div class="settings-card-header">
      <h2 class="section-title">Other devices</h2>
      <p class="hint">
        Open Ciaobot from a phone or another computer. Each device signs in with the Ciaobot
        password; these links never include it.
      </p>
    </div>
    <div v-if="loading" class="hint">Looking up addresses&hellip;</div>
    <p v-else-if="error" class="action-result" role="alert">{{ error }}</p>
    <ul v-else class="device-list">
      <li v-for="entry in addresses" :key="entry.url" class="device-row" :data-kind="entry.kind">
        <div class="device-main">
          <code class="device-url">{{ entry.url }}</code>
          <span class="device-tag">{{ labelFor(entry.kind) }}</span>
        </div>
        <div class="device-actions">
          <button class="btn-secondary btn-small" type="button" @click="copy(entry.url)">
            {{ copied === entry.url ? 'Copied' : 'Copy' }}
          </button>
          <button
            v-if="!entry.loopback"
            class="btn-secondary btn-small"
            type="button"
            :aria-expanded="shown === entry.url"
            @click="toggleQr(entry)"
          >
            {{ shown === entry.url ? 'Hide QR' : 'QR' }}
          </button>
        </div>
        <div v-if="shown === entry.url" class="device-qr">
          <!-- Generated locally; nothing is sent anywhere to render it. -->
          <div class="device-qr-code" v-html="qrSvg"></div>
        </div>
      </li>
    </ul>
    <form class="device-trusted" @submit.prevent="saveTrusted">
      <label for="trusted-url" class="notif-key">Trusted HTTPS address</label>
      <input
        id="trusted-url"
        v-model="trustedInput"
        type="url"
        inputmode="url"
        placeholder="https://your-mac.tailnet.ts.net"
        autocomplete="off"
      />
      <button class="btn-primary btn-small" type="submit" :disabled="saving">
        {{ saving ? 'Saving…' : 'Save' }}
      </button>
    </form>
    <p class="hint">
      For the full app on other devices, serve Ciaobot over HTTPS, e.g. with Tailscale:
      <code>tailscale serve --bg {{ port }}</code>, then paste the https://&hellip;.ts.net address
      here. Leave it empty to clear.
    </p>
    <p v-if="saveError" class="action-result" role="alert">{{ saveError }}</p>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import qrcode from 'qrcode-generator'
import { api } from '../../lib/api'
import { errorMessage } from '../../lib/errorMessage'
import { writeClipboard } from '../../lib/codeCopy'
import type { RoutineSettings } from '../../lib/types'

interface DeviceAddress {
  url: string
  kind: 'trusted' | 'lan' | 'loopback'
  secure: boolean
  loopback: boolean
}

const LABELS: Record<DeviceAddress['kind'], string> = {
  // The honest capability split: only an HTTPS origin is a secure context, so
  // only it can install the PWA or receive push notifications.
  trusted: 'Full app: install and notifications',
  lan: "Browser only: plain HTTP can't install the app or get notifications",
  loopback: 'This computer only',
}

function labelFor(kind: DeviceAddress['kind']): string {
  return LABELS[kind]
}

const addresses = ref<DeviceAddress[]>([])
const port = ref(8443)
const loading = ref(true)
const error = ref('')
const shown = ref<string | null>(null)
const qrSvg = ref('')
const copied = ref<string | null>(null)
const trustedInput = ref('')
const saving = ref(false)
const saveError = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.get<{
      port: number
      trusted_url: string | null
      addresses: DeviceAddress[]
    }>('/api/addresses')
    addresses.value = res.addresses || []
    if (typeof res.port === 'number') port.value = res.port
    trustedInput.value = res.trusted_url || ''
  } catch (e) {
    error.value = 'Could not read addresses: ' + errorMessage(e)
  } finally {
    loading.value = false
  }
}

function toggleQr(entry: DeviceAddress) {
  if (shown.value === entry.url) {
    shown.value = null
    qrSvg.value = ''
    return
  }
  // Type 0 lets the library pick the smallest version that fits, and error
  // correction "M" keeps a short URL scannable off a screen.
  const qr = qrcode(0, 'M')
  qr.addData(entry.url)
  qr.make()
  qrSvg.value = qr.createSvgTag({ scalable: true })
  shown.value = entry.url
}

async function copy(url: string) {
  const copiedOk = await writeClipboard(url)
  if (!copiedOk) return
  copied.value = url
  setTimeout(() => {
    if (copied.value === url) copied.value = null
  }, 1500)
}

async function saveTrusted() {
  saving.value = true
  saveError.value = ''
  try {
    await api.patch<RoutineSettings>('/api/settings/routines', {
      trusted_url: trustedInput.value.trim(),
    })
    await load()
  } catch (e) {
    saveError.value = errorMessage(e)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<!-- The card scaffolding (`.card`, `.section-title`, `.settings-card-header`,
     `.hint`, `.action-result`) used to live in SettingsView's own scoped block,
     which does not reach a child's subtree. Both sides load the same sheet. -->
<style scoped src="./settingsPanels.css"></style>

<style scoped>
.notif-key { width: auto; flex: none; }
.device-list {
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
}
.device-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
}
.device-main {
  display: flex;
  flex: 1 1 12rem;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.device-url {
  overflow-wrap: anywhere;
}
.device-tag {
  color: var(--fg3);
  font-size: 0.75rem;
}
.device-actions {
  display: flex;
  gap: var(--space-2);
}
.device-actions button {
  min-height: 2.75rem;
}
.device-qr {
  flex: 1 1 100%;
}
.device-qr-code {
  width: min(14rem, 100%);
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  /* The quiet zone has to stay white for reliable scanning, in either theme. */
  background: #fff;
}
.device-qr-code :deep(svg) {
  display: block;
  width: 100%;
  height: auto;
}
.device-trusted {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
  margin-top: var(--space-3);
}
.device-trusted input {
  flex: 1 1 14rem;
  min-width: 0;
  min-height: 2.75rem;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg);
  color: var(--fg);
  font-size: var(--text-sm);
}
.device-trusted input::placeholder {
  color: var(--fg3);
}
</style>
