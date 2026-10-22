<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { enablePush, isPushEnabled, pushSupported } from '../lib/push'
import { installInstructions, isAndroid, isIos, isIpadDesktopMode, isStandalone, SETUP_CARD_DISMISSED_KEY } from '../lib/pwaPlatform'
import { canPromptInstall, installed, installedAppProbed, installedOnDevice, promptInstall } from '../lib/installPrompt'
import { isLoopbackPage } from '../lib/loopback'
import { errorMessage } from '../lib/errorMessage'
import HomeNoticeWindow from './HomeNoticeWindow.vue'
import { useHomeNoticeWindows } from '../composables/useHomeNoticeWindows'

const noticeWindows = useHomeNoticeWindows()
const SETUP_NOTICE_KEY = 'setup:device'

/** The device-local setup nudge: install, then notifications.
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
const error = ref('')
/** True once the user acts here: the card must not vanish under the click
 *  that completes it, so the step's "On for this device" confirmation stays
 *  readable. It still auto-hides on the next mount, when both steps were
 *  already done. */
const touched = ref(false)
/** True once the push and install probes settled, one way or the other. Deciding `visible`
 *  before that would flash the card on every Home visit for the users who
 *  already finished setup, and would keep it up forever where push cannot
 *  exist, because `pushOn` could never become true there. */
const probed = ref(false)

const installDone = computed(() => standalone.value || installed.value || installedOnDevice.value)
/** Installed, but this is still a browser tab: say where the app is instead. */
const installedHint = computed(() => standalone.value || installed.value
  ? 'Installed'
  : 'Installed on this device. Open it from its icon instead of this tab.')
const needsInstallFirst = computed(() => isIos() && !standalone.value)
/** Phone setup is something you do *from* the computer running Ciaobot: a
 *  phone or a remote browser is already the other device. Informational, so
 *  it never counts toward `complete`. */
const showPhoneStep = isLoopbackPage() && !isIos() && !isAndroid() && !isIpadDesktopMode()

/** Both real steps are finished (or cannot be finished here). */
const complete = computed(() => installDone.value && (pushOn.value || !pushAvailable.value || denied.value))
/** The card lingers a moment after the user completes it so the confirmation
 *  is readable, then goes away by itself. */
const finished = ref(false)
let finishTimer: ReturnType<typeof setTimeout> | undefined
watch(() => touched.value && complete.value, done => {
  if (done) finishTimer = setTimeout(() => { finished.value = true }, 2500)
})
onBeforeUnmount(() => clearTimeout(finishTimer))

const visible = computed(() => {
  if (dismissed.value || finished.value) return false
  // Neither install prompts nor push subscriptions exist without a secure
  // origin, so the steps would be instructions for something impossible.
  if (window.isSecureContext !== true) return false
  if (!probed.value) return false
  // A browser without push has no second step to complete, so an installed app
  // there is done, not stuck. Nor is an installed app whose notifications are
  // blocked: nothing in this card can unblock them, and the Settings
  // notifications card still explains how.
  return touched.value || !complete.value
})
watch(visible, shown => noticeWindows.setAvailable('device-setup', shown ? [SETUP_NOTICE_KEY] : []), { immediate: true })
onBeforeUnmount(() => noticeWindows.clearAvailable('device-setup'))

onMounted(async () => {
  pushAvailable.value = pushSupported()
  denied.value = typeof Notification !== 'undefined' && Notification.permission === 'denied'
  // The install answer arrives asynchronously too; deciding before it would
  // show the card and then hide it under the user's finger.
  const installProbe = installedAppProbed()
  try {
    pushOn.value = await isPushEnabled()
  } catch { /* keep the step visible: the user can still try to enable it */ }
  await installProbe
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

/** Per browser and origin by construction: there is no server to tell. */
function hide() {
  try {
    localStorage.setItem(SETUP_CARD_DISMISSED_KEY, '1')
  } catch { /* nothing to persist to; still hide it for this session */ }
  dismissed.value = true
}
</script>

<template>
  <section v-if="visible && !noticeWindows.isClosed(SETUP_NOTICE_KEY)" class="home-setup" aria-label="Set up this device">
    <HomeNoticeWindow title="Set up this device" :notice-key="SETUP_NOTICE_KEY" close-label="Dismiss setup reminder on this device" @close="hide">
    <ol class="home-setup-steps">
      <li class="home-setup-step" :data-done="installDone">
        <div class="home-setup-text">
          <span class="home-setup-label">Install the app</span>
          <span v-if="installDone" class="hint">{{ installedHint }}</span>
          <span v-else-if="!canPromptInstall" class="hint">{{ installInstructions() }}</span>
        </div>
        <button
          v-if="!installDone && canPromptInstall"
          class="btn-small btn-primary"
          type="button"
          :disabled="busy === 'install'"
          @click="install"
        >Install</button>
        <!-- The browser has no prompt: the row's action is the way to the
             per-platform steps, a real link to Settings and not a dead end. -->
        <RouterLink
          v-else-if="!installDone && !canPromptInstall"
          class="btn-small home-setup-action"
          to="/settings#install-app"
        >How to install</RouterLink>
      </li>
      <li v-if="pushAvailable || needsInstallFirst" class="home-setup-step" :data-done="pushOn">
        <div class="home-setup-text">
          <span class="home-setup-label">Turn on notifications</span>
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
      <li v-if="showPhoneStep" class="home-setup-step">
        <!-- Phone setup is a long tail read on demand: the row keeps its
             label, the steps wait behind a native disclosure. -->
        <details class="home-setup-phone">
          <summary class="home-setup-summary">
            <span class="home-setup-label">Use on your phone</span>
          </summary>
          <ol class="home-setup-substeps hint">
            <li>Install Tailscale on this computer and your phone, signed in to the same account.</li>
            <li>Make Ciaobot reachable from the phone: Settings shows the address and the one command.</li>
            <li>On the phone, open the address or scan its QR code, sign in with your Ciaobot password, then Add to Home Screen.</li>
          </ol>
          <RouterLink class="btn-small home-setup-action" to="/settings/access#other-devices">Phone setup</RouterLink>
        </details>
      </li>
    </ol>
    <p v-if="error" class="action-result" role="alert">{{ error }}</p>
    </HomeNoticeWindow>
  </section>
</template>

<style scoped>
.home-setup { margin-block: var(--space-3); }
.home-setup-steps {
  list-style: none;
  margin: 0;
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
.home-setup-step:first-child { border-top: 0; padding-top: 0; }
.home-setup-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1 1 14rem;
}
/* A task name, not a heading: one quiet label per row. */
.home-setup-label {
  font-size: var(--text-base);
  font-weight: 600;
  line-height: 1.35;
}
.home-setup-step[data-done="true"] .home-setup-label { color: var(--fg3); }
/* One action slot per row, so the rows' actions share a column. */
.home-setup-step > .btn-small,
.home-setup-action {
  flex: 0 0 auto;
  width: 9.5rem;
  justify-content: center;
}
.home-setup-action { white-space: nowrap; }
.home-setup-substeps { margin: var(--space-2) 0 0; padding-left: 1.25em; display: grid; gap: 2px; }
/* The phone row is a disclosure: the label stays, the steps wait. */
.home-setup-phone { flex: 1 1 14rem; min-width: 0; }
.home-setup-summary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 32px;
  color: var(--fg);
  cursor: pointer;
  list-style: none;
}
.home-setup-summary::-webkit-details-marker { display: none; }
.home-setup-summary::before {
  content: '';
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  margin: 0 2px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(-45deg);
  transition: transform 120ms var(--ease);
}
.home-setup-phone[open] > .home-setup-summary::before { transform: rotate(45deg); }
@media (prefers-reduced-motion: reduce) {
  .home-setup-summary::before { transition: none; }
}
/* `summary` is not in the global focus rule's :where() list, so the ring this
   control needs is stated here rather than assumed. */
.home-setup-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}
@media (pointer: coarse) {
  .home-setup-summary { min-height: var(--touch); }
}
@container (max-width: 560px) {
  .home-setup-step > .btn-small,
  .home-setup-step > .home-setup-action,
  .home-setup-phone .home-setup-action {
    width: 100%;
    min-height: var(--touch);
  }
}
</style>
