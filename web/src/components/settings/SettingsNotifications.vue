<template>
  <div v-if="!hidden" class="card">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <p class="section-title">notifications</p>
        <p class="hint">
          Get a notification when a chat replies and the app is not focused.
        </p>
      </div>
      <div v-if="!inDesktopApp && !needsIosInstall && !permissionDenied && pushSupportedFlag" class="settings-card-header-actions">
        <button
          :class="(!pushEnabledFlag && !isMacDesktop()) ? 'btn-primary btn-small' : 'btn-secondary btn-small'"
          @click="togglePush"
          :disabled="pushPending"
        >
          {{ pushPending ? 'Working...' : (pushEnabledFlag ? 'Disable on this device' : 'Enable on this device') }}
        </button>
      </div>
    </div>
    <!-- In the desktop app the menu-bar companion owns notifications, so
         the PWA web-push controls are not the surface here. Explain what
         controls them instead of rendering nothing. -->
    <template v-if="inDesktopApp">
      <p class="hint">
        In the Ciaobot desktop app, notifications are handled by the menu-bar
        companion: a chat reply while the app isn't focused posts a banner, and
        opening it takes you to the chat. Use <strong>Menu Bar &rarr; Advanced
        &rarr; Native Notifications</strong> to turn them on or off.
      </p>
      <p class="hint">
        If notifications are blocked at the OS level, re-enable them in
        System Settings &rarr; Notifications &rarr; Ciaobot.
      </p>
    </template>
    <template v-else>
      <div v-if="needsIosInstall" class="hint hint--warn">
        On iOS, push notifications only work after you "Add to Home Screen" and open the app from there.
      </div>
      <div v-else-if="permissionDenied" class="hint hint--warn">
        Notifications are blocked at the OS level. Re-enable them in System Settings &rarr; Notifications &rarr; Ciaobot (or your browser).
      </div>
      <div v-else-if="!pushSupportedFlag" class="loading">
        Push notifications are not supported here. On macOS, install Ciaobot as an app
        (Chrome/Edge &ldquo;Install Ciaobot&rdquo;, or Safari &rarr; &ldquo;Add to Dock&rdquo;) and enable them from there.
      </div>
      <template v-else>
        <!-- On macOS the menu-bar agent already posts chat-reply notifications
             out of the box (menubar_prefs defaults on, launchd RunAtLoad), so
             don't present web-push as a required action here — lead with the
             reassurance and offer the app-install path as an optional upgrade. -->
        <p v-if="isMacDesktop() && !pushEnabledFlag" class="hint">
          You're covered — the menu bar already shows a notification when a chat
          replies and the app isn't focused. Nothing to enable.
        </p>
        <p v-if="isMacDesktop() && !pushEnabledFlag" class="hint">
          Optional upgrade: for notifications branded as <strong>Ciaobot</strong> that
          open the exact chat (and keep working even if you quit the menu bar), install
          Ciaobot as an app (Chrome/Edge &ldquo;Install Ciaobot&rdquo;, or Safari &rarr;
          &ldquo;Add to Dock&rdquo;), then enable it here.
        </p>
      </template>
      <div v-if="pushError" class="action-result">{{ pushError }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../../lib/api'
import { errorMessage } from '../../lib/errorMessage'
import { isDesktopApp } from '../../lib/desktop'
import { currentSubscription, disablePush, enablePush, isPushEnabled, pushSupported } from '../../lib/push'

/** The Home tab renders this card too, so both surfaces stay one copy.
 *
 * They differ only in the desktop app: the dedicated Notifications tab
 * explains that the menu bar owns notifications there, while Home leaves that
 * to the tray and drops the card entirely.
 */
const props = defineProps<{ hideInDesktopApp?: boolean }>()

const inDesktopApp = isDesktopApp()
const hidden = computed(() => Boolean(props.hideInDesktopApp) && inDesktopApp)

const pushSupportedFlag = ref(false)
const pushEnabledFlag = ref(false)
const pushPending = ref(false)
const pushError = ref('')
const permissionDenied = ref(false)
const needsIosInstall = ref(false)

function isIos(): boolean {
  return /iphone|ipad|ipod/i.test(navigator.userAgent)
}
function isMacDesktop(): boolean {
  return /macintosh|mac os x/i.test(navigator.userAgent) && !isIos()
}
function isStandalone(): boolean {
  return (
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  )
}

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

<style scoped>
/* Shared settings-card scaffolding (mirrors SettingsView.vue so the card keeps
   its layout when rendered from a child component). */
.card {
  width: min(100%, 1040px);
  margin: 0 auto;
  gap: var(--space-4);
  border-color: var(--border);
  box-shadow: 0 1px 0 color-mix(in srgb, var(--fg) 4%, transparent);
}
.section-title {
  letter-spacing: 0.08em;
}
.settings-card-header {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-bottom: var(--space-3);
  border-bottom: 1px solid var(--border);
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
  margin: var(--space-2) 0 0;
  max-width: 76ch;
}
.settings-card-header-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  justify-content: flex-end;
  flex: 0 0 auto;
}
.loading {
  color: var(--fg2);
  font-size: var(--text-base);
}
.action-result {
  font-size: var(--text-sm);
  color: var(--fg2);
  padding: 4px 0;
}
</style>
