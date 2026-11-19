<template>
  <!-- Ciaobot for Mac's own update state, the first row of the Updates card in
       the app. It renders nothing without the app's bridge (a browser, an older
       app), and the card keeps its static text. -->
  <div v-if="mac" class="mac-updates">
    <div class="mac-updates-row">
      <span class="mac-updates-key">Ciaobot for Mac</span>
      <span class="mac-updates-value">
        <span class="mac-updates-state">
          <span class="mac-updates-dot" :class="`mac-updates-dot--${tone}`" aria-hidden="true" />
          {{ statusLabel }}
        </span>
        <span v-if="statusDetail" :id="reasonId" class="mac-updates-detail">{{ statusDetail }}</span>
      </span>
    </div>
    <div class="mac-updates-row">
      <span class="mac-updates-key">Install Updates Automatically</span>
      <span class="mac-updates-value mac-updates-switch">
        <SettingsSwitch
          :checked="mac.automatic"
          :disabled="!mac.available || busy"
          :busy="busy"
          aria-label="Install Updates Automatically"
          :aria-describedby="mac.available ? undefined : reasonId"
          @toggle="toggleAutomatic"
        />
      </span>
    </div>
    <div class="mac-updates-actions">
      <!-- canCheck is false whenever available is false (see parseUpdates in lib/macAppSettings.ts). -->
      <button
        type="button"
        class="btn-secondary btn-small"
        :disabled="!mac.canCheck || busy"
        :aria-describedby="mac.available ? undefined : reasonId"
        @click="check"
      >
        Check for Updates&hellip;
      </button>
    </div>
    <p v-if="actionError" class="action-result" role="alert">{{ actionError }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  MAC_APP_SETTINGS_CHANGED,
  hasMacAppSettings,
  readMacAppSettings,
  requestMacAutomaticUpdates,
  requestMacCheckForUpdates,
  type MacAppSettings,
  type MacUpdateSettings,
} from '../../lib/macAppSettings'
import SettingsSwitch from './SettingsSwitch.vue'

const reasonId = 'mac-updates-reason'
const mac = ref<MacUpdateSettings | null>(null)
const busy = ref(false)
const actionError = ref('')
const CHECK_REFUSED = 'Ciaobot for Mac did not start a check. Use Ciaobot > Settings > Check for Updates… in the menu bar.'
const AUTOMATIC_REFUSED = 'Ciaobot for Mac did not change this setting. Use Ciaobot > Settings > Install Updates Automatically in the menu bar.'

// `seq` ranks every request that may write the state, reads and actions alike,
// so a reply that lands late cannot overwrite a newer snapshot. `actionSeq`
// ranks actions on their own: a refused action always reports its error, even
// when a read superseded it, because a refusal carries no state to lose.
let seq = 0
let actionSeq = 0

async function refresh() {
  const ticket = ++seq
  // The row exists only with the app's bridge. A missing bridge hides it.
  if (!hasMacAppSettings()) {
    mac.value = null
    return
  }
  const snapshot = await readMacAppSettings()
  // A refused or malformed read keeps the last good state, so a transient
  // refusal does not hide the row. Only an explicit null block hides it.
  if (ticket !== seq || snapshot === null) return
  mac.value = snapshot.updates
}

function onVisible() {
  if (document.visibilityState === 'visible') void refresh()
}

onMounted(async () => {
  if (!hasMacAppSettings()) return
  window.addEventListener(MAC_APP_SETTINGS_CHANGED, refresh)
  document.addEventListener('visibilitychange', onVisible)
  await refresh()
})

onBeforeUnmount(() => {
  window.removeEventListener(MAC_APP_SETTINGS_CHANGED, refresh)
  document.removeEventListener('visibilitychange', onVisible)
})

type Tone = 'ok' | 'warn' | 'off'
const tone = computed<Tone>(() => {
  const m = mac.value
  if (!m || !m.available) return 'warn'
  return m.automatic ? 'ok' : 'off'
})

const statusLabel = computed(() => {
  const m = mac.value
  if (!m) return ''
  if (!m.available) return 'Updates are unavailable'
  if (!m.canCheck) return 'Checking for updates'
  return m.automatic ? 'Installs updates automatically' : 'Asks before installing updates'
})

const statusDetail = computed(() => {
  const m = mac.value
  if (!m) return ''
  // Native text, shown as text only; it is the reason the controls are off.
  if (!m.available) return m.reason
  return ''
})

async function applyOutcome(request: Promise<MacAppSettings | null>, refused: string) {
  const action = ++actionSeq
  const ticket = ++seq
  busy.value = true
  actionError.value = ''
  try {
    const snapshot = await request
    if (action !== actionSeq) return
    const updates = snapshot?.updates
    if (!updates) {
      actionError.value = refused
      return
    }
    // A newer read has already written a fresher state; keep that one.
    if (ticket === seq) mac.value = updates
  } finally {
    busy.value = false
  }
}

function check() {
  if (!mac.value?.canCheck || busy.value) return
  void applyOutcome(requestMacCheckForUpdates(), CHECK_REFUSED)
}

function toggleAutomatic() {
  if (!mac.value?.available || busy.value) return
  void applyOutcome(requestMacAutomaticUpdates(!mac.value.automatic), AUTOMATIC_REFUSED)
}
</script>

<style scoped>
.mac-updates { display: flex; flex-direction: column; border-top: 1px solid var(--border); }
.mac-updates-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-4);
  min-height: var(--touch);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.mac-updates-key { width: 116px; flex: none; color: var(--fg3); line-height: 1.5; }
.mac-updates-value { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.mac-updates-switch { flex: none; margin-left: auto; align-items: flex-end; }
.mac-updates-state {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg);
  font-weight: 600;
  line-height: 1.5;
}
.mac-updates-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--fg3); }
.mac-updates-dot--ok { background: var(--success); }
.mac-updates-dot--warn { background: var(--warning); }
.mac-updates-detail { color: var(--fg3); line-height: 1.5; max-width: 72ch; }
.mac-updates-actions { padding: var(--space-3) 0 0; }
.mac-updates-actions .btn-small { min-height: var(--touch); }
@media (max-width: 600px) {
  .mac-updates-row { flex-wrap: wrap; gap: var(--space-2) var(--space-4); }
  .mac-updates-key { width: auto; }
  .mac-updates-value { flex-basis: 100%; order: 3; }
  .mac-updates-row > .mac-updates-switch { flex-basis: auto; order: 0; }
}
.action-result { font-size: var(--text-sm); color: var(--fg2); padding: 4px 0; }
</style>
