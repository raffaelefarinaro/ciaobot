<template>
  <div class="card">
    <div class="settings-card-header">
      <p class="section-title">Notifications</p>
      <p class="hint">
        {{ desktop
          ? (mac !== null
            ? 'Ciaobot for Mac shows a macOS notification when a chat replies. This is the same switch as the menu bar\'s Settings → Chat Notifications.'
            : 'Ciaobot for Mac shows a macOS notification when a chat replies.')
          : 'Ciaobot notifies you when a chat replies and the browser tab is in the background.' }}
        <a
          class="settings-help-link"
          href="https://www.raffaelefarinaro.com/ciaobot/remote.html#browser"
          target="_blank"
          rel="noopener noreferrer"
        >Notifications on other devices need HTTPS: how to set it up</a>
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
          <span v-if="mac && mac.detail" class="notif-detail">{{ mac.detail }}</span>
          <span v-else-if="status.detail" class="notif-detail" v-html="status.detail" />
        </span>
        <span v-if="mac && mac.action === 'toggle'" class="notif-end">
          <SettingsSwitch
            :checked="mac.state === 'on'"
            :busy="macBusy"
            aria-label="Chat notifications"
            @toggle="macAction"
          />
        </span>
        <span v-else-if="mac && mac.action === 'openSystemSettings'" class="notif-end">
          <button class="btn-secondary btn-small" :disabled="macBusy" @click="macAction">
            Open System Settings
          </button>
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
    <p v-if="macError" class="action-result" role="alert">{{ macError }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../../lib/api'
import { errorMessage } from '../../lib/errorMessage'
import { currentSubscription, disablePush, enablePush, isPushEnabled, pushSupported } from '../../lib/push'
import {
  MAC_APP_SETTINGS_CHANGED,
  hasMacAppSettings,
  readMacAppSettings,
  requestMacNotificationAction,
  type MacNotificationSettings,
} from '../../lib/macAppSettings'
import SettingsSwitch from './SettingsSwitch.vue'
import { isDesktopClient, isIos, isMacDesktop, isStandalone } from '../../lib/pwaPlatform'

/** Ciaobot for Mac sends its own native notifications, so no Web Push state
 *  or enable control applies inside it. */
const desktop = isDesktopClient()
const pushSupportedFlag = ref(false)
const pushEnabledFlag = ref(false)
const pushPending = ref(false)
const pushError = ref('')
const permissionDenied = ref(false)
const needsIosInstall = ref(false)

/** The Mac app's own notification state, mirrored from its menu bar Settings
 *  row. Null when the app has no bridge (an older Mac app): the static text
 *  below stays. */
const mac = ref<MacNotificationSettings | null>(null)
const macBusy = ref(false)
const macError = ref('')
const MAC_REFUSED = 'Ciaobot for Mac did not change notifications. Use the menu bar: Ciaobot \u2192 Settings.'

async function refreshMac() {
  const snapshot = await readMacAppSettings()
  if (snapshot?.notifications) mac.value = snapshot.notifications
}

async function macAction() {
  if (macBusy.value) return
  macBusy.value = true
  macError.value = ''
  try {
    const snapshot = await requestMacNotificationAction()
    if (snapshot?.notifications) mac.value = snapshot.notifications
    else macError.value = MAC_REFUSED
  } finally {
    macBusy.value = false
  }
}

function onVisible() {
  if (document.visibilityState === 'visible') void refreshMac()
}
onBeforeUnmount(() => {
  window.removeEventListener(MAC_APP_SETTINGS_CHANGED, refreshMac)
  document.removeEventListener('visibilitychange', onVisible)
})

const showToggle = computed(
  () => !desktop && !needsIosInstall.value && !permissionDenied.value && pushSupportedFlag.value,
)

type Tone = 'ok' | 'warn' | 'off'
function macTone(m: MacNotificationSettings): Tone {
  if (m.state === 'on' && m.ready) return 'ok'
  if (m.state === 'denied' || m.state === 'deliveryOff' || m.state === 'unavailable') return 'warn'
  return 'off'
}
// Static strings only (no user data), so v-html is safe for the arrows/strong.
const status = computed<{ label: string; tone: Tone; detail: string }>(() => {
  if (mac.value) {
    const m = mac.value
    return { label: m.title, tone: macTone(m), detail: '' }
  }
  if (desktop) {
    return {
      label: 'Handled by the Mac app',
      tone: 'off',
      detail: 'Ciaobot for Mac sends notifications itself. Turn them on or off in System Settings &rarr; Notifications &rarr; Ciaobot.',
    }
  }
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
  if (desktop) {
    if (hasMacAppSettings()) {
      window.addEventListener(MAC_APP_SETTINGS_CHANGED, refreshMac)
      document.addEventListener('visibilitychange', onVisible)
      await refreshMac()
    }
    return
  }
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

</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
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
