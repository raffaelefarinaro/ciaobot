<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { enablePush, isPushEnabled, pushSupported, sendTestNotification } from '../lib/push'
import { installInstructions, isIos, isStandalone, SETUP_CARD_DISMISSED_KEY } from '../lib/pwaPlatform'
import { canPromptInstall, installed, promptInstall } from '../lib/installPrompt'
import { isDesktopApp } from '../lib/desktop'
import { errorMessage } from '../lib/errorMessage'

/** The device-local setup nudge: install, notifications, and a proof they work.
 *
 * A browser tab that was never installed cannot receive push on iOS at all and
 * is easy to forget about on desktop, and Settings' scattered hints only appear
 * once the user goes looking. This is one place, in the order the steps have to
 * happen, and it disappears by itself once both real steps are done.
 */
const dismissed = ref(false)
try {
  dismissed.value = localStorage.getItem(SETUP_CARD_DISMISSED_KEY) === '1'
} catch { /* localStorage blocked: keep showing the card */ }

const standalone = ref(isStandalone())
const pushOn = ref(false)
const pushAvailable = ref(false)
const denied = ref(false)
/** The step currently running, so its own button can show as busy. */
const busy = ref('')
const testResult = ref('')
const error = ref('')
/** True once the user acts here: the card must not vanish mid-visit, or
 *  "Send test" could never be used in the very flow that enables it. It still
 *  auto-hides on the next mount, when both steps were already done. */
const touched = ref(false)
/** True once the push probe settled, one way or the other. Deciding `visible`
 *  before that would flash the card on every Home visit for the users who
 *  already finished setup, and would keep it up forever where push cannot
 *  exist, because `pushOn` could never become true there. */
const probed = ref(false)

const installDone = computed(() => standalone.value || installed.value)
const needsInstallFirst = computed(() => isIos() && !standalone.value)

const visible = computed(() => {
  if (dismissed.value) return false
  // Ciaobot.app has its own install (it is the app) and its own tray.
  if (isDesktopApp()) return false
  // Neither install prompts nor push subscriptions exist without a secure
  // origin, so the steps would be instructions for something impossible.
  if (window.isSecureContext !== true) return false
  if (!probed.value) return false
  // A browser without push has no second step to complete, so an installed app
  // there is done, not stuck.
  return touched.value || !(installDone.value && (pushOn.value || !pushAvailable.value))
})

onMounted(async () => {
  pushAvailable.value = pushSupported()
  denied.value = typeof Notification !== 'undefined' && Notification.permission === 'denied'
  try {
    pushOn.value = await isPushEnabled()
  } catch { /* keep the step visible: the user can still try to enable it */ }
  probed.value = true
})

async function install() {
  touched.value = true
  busy.value = 'install'
  try {
    await promptInstall()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = ''
  }
}

async function enable() {
  touched.value = true
  busy.value = 'notify'
  error.value = ''
  try {
    await enablePush()
    pushOn.value = true
  } catch (e) {
    // The user may have just chosen Block, which only moves `permission` and
    // leaves the Enable button guaranteed to fail again.
    denied.value = typeof Notification !== 'undefined' && Notification.permission === 'denied'
    error.value = errorMessage(e)
  } finally {
    busy.value = ''
  }
}

async function test() {
  busy.value = 'test'
  error.value = ''
  try {
    // Accepted by the push service is not proof of display; say so.
    testResult.value = await sendTestNotification()
      ? 'Sent. If nothing appears within a few seconds, check this browser\'s notification permission in your system settings.'
      : 'Enable notifications on this device first.'
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = ''
  }
}

/** Per browser and origin by construction: there is no server to tell. */
function hide() {
  try {
    localStorage.setItem(SETUP_CARD_DISMISSED_KEY, '1')
  } catch { /* nothing to persist to; still hide it for this session */ }
  dismissed.value = true
}
</script>

<template>
  <section v-if="visible" class="home-setup" aria-labelledby="home-setup-title">
    <div class="home-setup-head">
      <h2 id="home-setup-title" class="home-setup-title">Set up this device</h2>
      <button class="btn-small btn-chip" type="button" @click="hide">Hide</button>
    </div>
    <ol class="home-setup-steps">
      <li class="home-setup-step" :data-done="installDone">
        <div class="home-setup-text">
          <strong>Install the app</strong>
          <span v-if="installDone" class="hint">Installed</span>
          <span v-else-if="!canPromptInstall" class="hint">{{ installInstructions() }}</span>
        </div>
        <button
          v-if="!installDone && canPromptInstall"
          class="btn-small btn-primary"
          type="button"
          :disabled="busy === 'install'"
          @click="install"
        >Install</button>
      </li>
      <li v-if="pushAvailable || needsInstallFirst" class="home-setup-step" :data-done="pushOn">
        <div class="home-setup-text">
          <strong>Turn on notifications</strong>
          <span v-if="pushOn" class="hint">On for this device</span>
          <span v-else-if="needsInstallFirst" class="hint">Install the app first; iPhone and iPad only notify from the Home Screen app.</span>
          <span v-else-if="denied" class="hint">Blocked. Allow notifications for this site in your system settings.</span>
        </div>
        <button
          v-if="!pushOn && !needsInstallFirst && !denied"
          class="btn-small btn-primary"
          type="button"
          :disabled="busy === 'notify'"
          @click="enable"
        >Enable</button>
      </li>
      <li v-if="pushAvailable" class="home-setup-step">
        <div class="home-setup-text">
          <strong>Send a test notification</strong>
          <span v-if="testResult" class="hint" role="status">{{ testResult }}</span>
        </div>
        <button
          class="btn-small btn-chip"
          type="button"
          :disabled="!pushOn || busy === 'test'"
          @click="test"
        >Send test</button>
      </li>
    </ol>
    <p v-if="error" class="action-result" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
/* A plain section, not a card: Home already has one raised surface in view
   (the housekeeping strip) and a second box here would compete with it. */
.home-setup { margin-block: var(--space-3); }
.home-setup-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.home-setup-title {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
}
.home-setup-steps {
  list-style: none;
  margin: var(--space-2) 0 0;
  padding: 0;
}
.home-setup-step {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
}
.home-setup-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1 1 14rem;
}
.home-setup-step[data-done="true"] strong { color: var(--fg3); }
</style>
