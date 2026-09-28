<template>
  <div class="card">
    <div class="settings-card-header">
      <p class="section-title">Notifications</p>
      <p class="hint">
        Ciaobot notifies you when a chat replies and the browser tab is in the background.
      </p>
    </div>
    <!-- Status as one key/value row: a dot plus words (never colour alone),
         the fix on the line below, and the device action at the row's end. -->
    <div class="notif-rows">
      <div class="notif-row">
        <span class="notif-key">Status</span>
        <span class="notif-value">
          <span class="notif-state">
            <span class="notif-dot" :class="`notif-dot--${status.tone}`" aria-hidden="true" />
            {{ status.label }}
          </span>
          <span v-if="status.detail" class="notif-detail" v-html="status.detail" />
        </span>
        <span v-if="showToggle" class="notif-end">
          <button
            :class="(!pushEnabledFlag) ? 'btn-primary btn-small' : 'btn-secondary btn-small'"
            @click="togglePush"
            :disabled="pushPending"
          >
            {{ pushPending ? 'Working...' : (pushEnabledFlag ? 'Disable on this device' : 'Enable on this device') }}
          </button>
        </span>
      </div>
      <div class="notif-row">
        <span class="notif-key">Delivery</span>
        <span class="notif-value">
          <span class="notif-detail">{{ pushAllDevices ? 'Every device, including this computer' : 'Other devices only; this computer is not notified' }}</span>
        </span>
        <span class="notif-end">
          <button class="btn-secondary btn-small" @click="toggleDelivery" :disabled="deliveryPending">
            {{ pushAllDevices ? 'Other devices only' : 'Push to every device' }}
          </button>
        </span>
      </div>
    </div>
    <!-- Mac without web push: web push in an installed app is the only way a Mac
         gets a Ciaobot notification banner, so offer it as an optional upgrade. -->
    <p v-if="showToggle && isMacDesktop() && !pushEnabledFlag" class="hint notif-note">
      Optional: for notifications branded as <strong>Ciaobot</strong> that open the exact
      chat, install Ciaobot as an app
      (Chrome/Edge &ldquo;Install Ciaobot&rdquo;, or Safari &rarr; &ldquo;Add to Dock&rdquo;),
      then enable it here.
    </p>
    <p v-if="pushError" class="action-result" role="alert">{{ pushError }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../../lib/api'
import { errorMessage } from '../../lib/errorMessage'
import { currentSubscription, disablePush, enablePush, isPushEnabled, pushSupported } from '../../lib/push'
import { isIos, isMacDesktop, isStandalone } from '../../lib/pwaPlatform'
import type { RoutineSettings } from '../../lib/types'

const pushSupportedFlag = ref(false)
const pushEnabledFlag = ref(false)
const pushPending = ref(false)
const pushError = ref('')
const permissionDenied = ref(false)
const needsIosInstall = ref(false)
const pushAllDevices = ref(false)
const deliveryPending = ref(false)

const showToggle = computed(
  () => !needsIosInstall.value && !permissionDenied.value && pushSupportedFlag.value,
)

type Tone = 'ok' | 'warn' | 'off'
// Static strings only (no user data), so v-html is safe for the arrows/strong.
const status = computed<{ label: string; tone: Tone; detail: string }>(() => {
  if (needsIosInstall.value) {
    return {
      label: 'Needs the Home Screen app',
      tone: 'warn',
      detail: 'On iOS, notifications only work after you &ldquo;Add to Home Screen&rdquo; and open Ciaobot from there.',
    }
  }
  if (permissionDenied.value) {
    return {
      label: 'Blocked by the system',
      tone: 'warn',
      detail: 'Turn them on in System Settings &rarr; Notifications &rarr; Ciaobot (or in your browser&rsquo;s site settings).',
    }
  }
  if (!pushSupportedFlag.value) {
    return {
      label: 'Not available in this browser',
      tone: 'off',
      detail: 'On macOS, install Ciaobot as an app (Chrome/Edge &ldquo;Install Ciaobot&rdquo;, or Safari &rarr; &ldquo;Add to Dock&rdquo;) and enable them there.',
    }
  }
  if (pushEnabledFlag.value) return { label: 'On for this device', tone: 'ok', detail: '' }
  return { label: 'Off on this device', tone: 'off', detail: '' }
})

onMounted(async () => {
  pushSupportedFlag.value = pushSupported()
  if (isIos() && !isStandalone()) {
    needsIosInstall.value = true
  }
  if (typeof Notification !== 'undefined' && Notification.permission === 'denied') {
    permissionDenied.value = true
  }
  if (pushSupportedFlag.value) {
    pushEnabledFlag.value = await isPushEnabled()
    // Self-heal: if the browser thinks it has a subscription but the server
    // forgot it (state file moved, fresh deploy), silently re-register so
    // pushes actually arrive without making the user click anything.
    if (pushEnabledFlag.value && Notification.permission === 'granted') {
      try {
        const sub = await currentSubscription()
        if (sub) {
          const r = await api.get<{ registered: boolean }>(
            `/api/push/subscription?endpoint=${encodeURIComponent(sub.endpoint)}`
          )
          if (!r.registered) {
            await api.post('/api/push/subscribe', { subscription: sub.toJSON() })
          }
        }
      } catch { /* best-effort */ }
    }
  }
  // Delivery is an install-wide setting, not this browser's: read it once and
  // never let an unavailable settings endpoint break the card.
  try {
    pushAllDevices.value = Boolean((await api.get<RoutineSettings>('/api/settings/routines')).push_all_devices)
  } catch { /* settings unavailable: keep default */ }
})

async function togglePush() {
  pushPending.value = true
  pushError.value = ''
  try {
    if (pushEnabledFlag.value) {
      await disablePush()
      pushEnabledFlag.value = false
    } else {
      await enablePush()
      pushEnabledFlag.value = true
    }
  } catch (e) {
    pushError.value = errorMessage(e)
  } finally {
    pushPending.value = false
  }
}

async function toggleDelivery() {
  deliveryPending.value = true
  pushError.value = ''
  try {
    const res = await api.patch<RoutineSettings>('/api/settings/routines', { push_all_devices: !pushAllDevices.value })
    pushAllDevices.value = Boolean(res.push_all_devices)
  } catch (e) {
    pushError.value = errorMessage(e)
  } finally {
    deliveryPending.value = false
  }
}

</script>

<style scoped>
/* Shared settings-card scaffolding (mirrors SettingsView.vue so the card keeps
   its layout when rendered from a child component). */
.card {
  width: 100%;
  margin: 0;
  padding: 0;
  gap: var(--space-3);
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
  scroll-margin-top: var(--space-4);
}
.card:focus {
  outline: none;
}
.section-title {
  color: var(--fg);
  font-family: var(--font-sans);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.01em;
  line-height: 1.3;
  text-transform: none;
}
.settings-card-header {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding-bottom: var(--space-1);
  border-bottom: 0;
}
.settings-card-header .hint {
  color: var(--fg3);
}
.settings-card-header:last-child {
  padding-bottom: 0;
  border-bottom: none;
}
.settings-card-header--split {
  flex-direction: row;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
}
.settings-card-header--split > div {
  min-width: 0;
}
.settings-card-header .hint {
  margin: var(--space-1) 0 0;
  max-width: 76ch;
}
.notif-rows { border-top: 1px solid var(--border); }
.notif-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-4);
  min-height: var(--touch);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.notif-key { width: 116px; flex: none; color: var(--fg3); line-height: 1.5; }
.notif-value { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.notif-state {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg);
  font-weight: 600;
  line-height: 1.5;
}
.notif-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--fg3); }
.notif-dot--ok { background: var(--success); }
.notif-dot--warn { background: var(--warning); }
.notif-detail { color: var(--fg3); line-height: 1.5; max-width: 72ch; }
.notif-end { flex: none; margin-left: auto; }
.notif-note { margin: 0; color: var(--fg3); font-size: var(--text-sm); max-width: 72ch; }
@media (max-width: 600px) {
  .notif-row { flex-wrap: wrap; gap: var(--space-2) var(--space-4); }
  .notif-key { width: auto; }
  .notif-value { flex-basis: 100%; order: 3; }
}
.action-result {
  font-size: var(--text-sm);
  color: var(--fg2);
  padding: 4px 0;
}
</style>
