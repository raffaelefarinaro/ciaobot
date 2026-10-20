<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { COPY_FEEDBACK_MS, writeClipboard } from '../lib/codeCopy'
import { errorMessage } from '../lib/errorMessage'
import HomeNoticeWindow from './HomeNoticeWindow.vue'
import { useHomeNoticeWindows } from '../composables/useHomeNoticeWindows'
import { useWelcome } from '../composables/useWelcome'

const noticeWindows = useHomeNoticeWindows()
const NOTICE_KEY = 'welcome:install'

/** The one-time "Your Ciaobot" card: workspace folder, password, AI tool.
 *
 * The engine decides everything: whether the card is pending, whether this
 * browser is on the host (`on_host`), and dismissal (engine-wide). The
 * password is fetched only on Show or Copy and lives only in this component's
 * state, cleared on dismiss and unmount. It is never logged or persisted.
 */
const { status, ensureStatus, fetchPassword, openFolder, dismiss } = useWelcome()

const password = ref('')
const revealed = ref(false)
const busy = ref<'' | 'show' | 'copy' | 'folder'>('')
const copied = ref(false)
const error = ref('')
let copiedTimer: number | undefined

const visible = computed(() => status.value?.pending === true)
watch(visible, shown => noticeWindows.setAvailable('welcome', shown ? [NOTICE_KEY] : []), { immediate: true })

onMounted(ensureStatus)
onBeforeUnmount(() => {
  noticeWindows.clearAvailable('welcome')
  password.value = ''
  window.clearTimeout(copiedTimer)
})

const folderLabel = computed(() => {
  const platform = status.value?.platform
  if (platform === 'darwin') return 'Show in Finder'
  if (platform === 'win32') return 'Show in File Explorer'
  return 'Open folder'
})

async function ensurePassword(): Promise<void> {
  if (!password.value) password.value = await fetchPassword()
}

async function toggleShow() {
  error.value = ''
  if (revealed.value) {
    revealed.value = false
    return
  }
  busy.value = 'show'
  try {
    await ensurePassword()
    revealed.value = true
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = ''
  }
}

async function copy() {
  error.value = ''
  busy.value = 'copy'
  try {
    await ensurePassword()
    if (await writeClipboard(password.value)) {
      copied.value = true
      window.clearTimeout(copiedTimer)
      copiedTimer = window.setTimeout(() => { copied.value = false }, COPY_FEEDBACK_MS)
    } else {
      error.value = 'Could not copy. Select the password and copy it instead.'
    }
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = ''
  }
}

async function reveal() {
  error.value = ''
  busy.value = 'folder'
  try {
    await openFolder()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = ''
  }
}

async function close() {
  password.value = ''
  revealed.value = false
  await dismiss()
}
</script>

<template>
  <section v-if="visible && status && !noticeWindows.isClosed(NOTICE_KEY)" class="home-welcome" aria-label="Your Ciaobot">
    <HomeNoticeWindow title="Your Ciaobot" :notice-key="NOTICE_KEY" close-label="Dismiss this card on every device" @close="close">
      <dl class="home-welcome-rows">
        <div class="home-welcome-row">
          <dt>Folder</dt>
          <dd>
            <code class="home-welcome-path">{{ status.workspace_root }}</code>
            <span class="hint">Your notes, memory and settings live here.</span>
          </dd>
          <button v-if="status.on_host" class="btn-small" type="button" :disabled="busy === 'folder'" @click="reveal">{{ folderLabel }}</button>
        </div>
        <div class="home-welcome-row">
          <dt>Password</dt>
          <dd v-if="status.on_host">
            <code class="home-welcome-secret" :aria-label="revealed ? undefined : 'Password hidden'">{{ revealed ? password : '••••••••••••' }}</code>
            <span class="hint">Save it in your password manager — you need it to sign in from other devices.</span>
          </dd>
          <dd v-else><span class="hint">The password is shown on the computer running Ciaobot.</span></dd>
          <div v-if="status.on_host" class="home-welcome-actions">
            <button class="btn-small" type="button" :aria-pressed="revealed" :disabled="busy === 'show'" @click="toggleShow">{{ revealed ? 'Hide' : 'Show' }}</button>
            <button class="btn-small" type="button" :disabled="busy === 'copy'" @click="copy">{{ copied ? 'Copied' : 'Copy' }}</button>
          </div>
        </div>
        <div class="home-welcome-row">
          <dt>AI tool</dt>
          <dd>{{ status.provider_label }} <RouterLink class="home-welcome-link" to="/settings/models#chat-providers">Providers</RouterLink></dd>
        </div>
      </dl>
      <p class="sr-only" aria-live="polite">{{ copied ? 'Password copied' : '' }}</p>
      <p v-if="error" class="action-result" role="alert">{{ error }}</p>
    </HomeNoticeWindow>
  </section>
</template>

<style scoped>
.home-welcome { margin-block: var(--space-3); }
.home-welcome-rows { margin: 0; padding: 0; }
.home-welcome-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
}
.home-welcome-row:first-child { border-top: 0; padding-top: 0; }
.home-welcome-row dt {
  min-width: 6rem;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg3);
}
.home-welcome-row dd {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  min-width: 0;
  flex: 1 1 14rem;
}
.home-welcome-path, .home-welcome-secret { overflow-wrap: anywhere; }
.home-welcome-secret { user-select: all; }
.home-welcome-actions { display: flex; gap: var(--space-2); }
@media (pointer: coarse) {
  .home-welcome button, .home-welcome-link { min-height: var(--touch); }
  .home-welcome-link { display: inline-flex; align-items: center; }
}
@container (max-width: 560px) {
  .home-welcome-row > button, .home-welcome-actions { width: 100%; }
  .home-welcome-actions > button { flex: 1; }
}
</style>
