<template>
  <div class="addresses">
    <p class="hint">
      Open Ciaobot from another device on this network — or scan the code with a phone.
    </p>
    <div v-if="loading" class="loading">Looking up addresses&hellip;</div>
    <div v-else-if="error" class="action-result">{{ error }}</div>
    <ul v-else class="address-list">
      <li v-for="entry in addresses" :key="entry.url" class="address-row">
        <div class="address-main">
          <code class="address-url">{{ entry.url }}</code>
          <span v-if="entry.loopback" class="address-tag">this machine only</span>
        </div>
        <div class="address-actions">
          <button class="btn-secondary btn-small" type="button" @click="copy(entry.url)">
            {{ copied === entry.url ? 'Copied' : 'Copy' }}
          </button>
          <button
            class="btn-secondary btn-small"
            type="button"
            :aria-expanded="shown === entry.url"
            @click="toggle(entry)"
          >
            {{ shown === entry.url ? 'Hide QR' : 'QR' }}
          </button>
        </div>
        <div v-if="shown === entry.url" class="address-qr">
          <p v-if="entry.loopback" class="hint hint--warn">
            A phone scanning this lands on its own device. Use one of the other
            addresses instead.
          </p>
          <!-- Generated locally; nothing is sent anywhere to render it. -->
          <div class="address-qr-code" v-html="qrSvg"></div>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import qrcode from 'qrcode-generator'
import { api } from '../lib/api'

interface AddressEntry {
  url: string
  loopback: boolean
}

const addresses = ref<AddressEntry[]>([])
const loading = ref(true)
const error = ref('')
const shown = ref<string | null>(null)
const qrSvg = ref('')
const copied = ref<string | null>(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.get<{ addresses: AddressEntry[] }>('/api/node/addresses')
    addresses.value = res.addresses || []
  } catch (reason) {
    error.value = `Could not read addresses: ${String(reason)}`
  } finally {
    loading.value = false
  }
}

function toggle(entry: AddressEntry) {
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
  try {
    await navigator.clipboard.writeText(url)
    copied.value = url
    setTimeout(() => {
      if (copied.value === url) copied.value = null
    }, 1500)
  } catch {
    // Clipboard access can be refused; the URL is on screen to read regardless.
  }
}

void load()
</script>

<style scoped>
.address-list {
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
}
.address-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
}
.address-main {
  display: flex;
  flex: 1 1 12rem;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.address-url {
  overflow-wrap: anywhere;
}
.address-tag {
  color: var(--fg3);
  font-size: 0.75rem;
}
.address-actions {
  display: flex;
  gap: var(--space-2);
}
.address-actions button {
  min-height: 2.75rem;
}
.address-qr {
  flex: 1 1 100%;
}
.address-qr-code {
  width: min(14rem, 100%);
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  /* The quiet zone has to stay white for reliable scanning, in either theme. */
  background: #fff;
}
.address-qr-code :deep(svg) {
  display: block;
  width: 100%;
  height: auto;
}
</style>
