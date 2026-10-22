<template>
  <div v-if="open" class="modal-backdrop" @click.self="close">
    <div
      ref="sheetEl"
      class="modal-sheet gws-connect"
      role="dialog"
      aria-modal="true"
      aria-labelledby="gws-connect-title"
    >
      <header class="gws-connect-head">
        <h3 id="gws-connect-title">{{ mode === 'reauth' ? 'Sign in to Google again' : 'Connect Google account' }}</h3>
        <button type="button" class="gws-connect-close" aria-label="Close" @click="close">×</button>
      </header>

      <div class="gws-connect-body">
        <label class="settings-field">
          <span class="ws-label">Display name</span>
          <input
            ref="nameInput"
            v-model="displayName"
            class="routine-input"
            :readonly="mode === 'reauth' || !!profile"
            aria-describedby="gws-connect-storage"
          />
        </label>
        <p id="gws-connect-storage" class="hint hint--compact">
          Stored on this machine in <code>{{ profile?.config_dir || gwsStorageHint(profileId) }}</code>.
        </p>

        <!-- Step 1: the gws CLI -->
        <p v-if="stepState('install') === 'done'" class="gws-step-done">gws installed</p>
        <section v-else class="gws-step" aria-labelledby="gws-step-install">
          <h4 id="gws-step-install" class="gws-step-title">Install the gws CLI</h4>
          <p class="hint hint--warn">
            Install <code>@googleworkspace/cli</code> before enabling Google Workspace tools for chats and schedules.
          </p>
          <div class="action-row">
            <HostTerminalButton action="gws-install" variant="small" label="Install in Terminal" />
            <button type="button" class="btn-primary btn-small" @click="onInstallInChat">Install in Chat</button>
            <button type="button" class="btn-small" @click="fetchGwsIntegration()">Check again</button>
          </div>
          <p class="hint hint--compact">Opens a chat that walks through <code>npm install -g @googleworkspace/cli</code> with you — Ciaobot does not run package installs on its own.</p>
        </section>

        <!-- Step 2: the OAuth client -->
        <div v-if="stepState('client') === 'done'" class="gws-step-done-row">
          <p class="gws-step-done">OAuth client present</p>
          <template v-if="profile?.client_secret_present">
            <button type="button" class="btn-small" @click="fileInput?.click()">Replace client_secret.json</button>
          </template>
        </div>
        <section v-else class="gws-step" aria-labelledby="gws-step-client">
          <h4 id="gws-step-client" class="gws-step-title">OAuth client</h4>
          <div class="gws-radio-group" role="radiogroup" aria-label="OAuth client">
            <label class="gws-radio">
              <input v-model="clientMode" type="radio" name="gws-client-mode" value="gcloud" />
              <span>Create one with gcloud</span>
            </label>
            <label class="gws-radio">
              <input v-model="clientMode" type="radio" name="gws-client-mode" value="upload" />
              <span>Upload my own client_secret.json</span>
            </label>
          </div>
          <div v-if="clientMode === 'gcloud'" class="gws-step-panel">
            <div class="gws-command-row">
              <code ref="gcloudCommand" class="gws-command">ciao gws {{ profileId }} auth setup</code>
              <button type="button" class="btn-small" @click="copyCommand">Copy</button>
            </div>
            <p class="hint hint--compact">
              Needs the <a :href="gwsReloginHelpUrl()" target="_blank" rel="noopener noreferrer">gcloud CLI</a>.
              Run it in a terminal on this machine, then press Check again.
            </p>
            <div class="action-row">
              <button type="button" class="btn-small" @click="onCheckAgain">Check again</button>
            </div>
          </div>
          <div v-else class="gws-step-panel">
            <p class="hint hint--compact">
              In <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Google Cloud Console &rarr; Credentials</a>
              create an OAuth client ID of type Desktop app and download the JSON. Enable the APIs you need in your project:
              Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, People (Contacts), Forms.
            </p>
            <div class="action-row">
              <button type="button" class="btn-small" :disabled="gwsSavingProfile === profileId" @click="fileInput?.click()">Upload client_secret.json</button>
            </div>
          </div>
        </section>
        <input ref="fileInput" type="file" accept=".json" hidden @change="onUpload" />

        <!-- Step 3: sign in -->
        <section class="gws-step" aria-labelledby="gws-step-signin">
          <h4 id="gws-step-signin" class="gws-step-title">Sign in with Google</h4>
          <template v-if="stepState('client') !== 'done'">
            <button
              type="button"
              class="btn-primary btn-small"
              disabled
              aria-describedby="gws-signin-reason"
            >Sign in with Google</button>
            <span id="gws-signin-reason" class="gws-action-hint">Upload an OAuth client first.</span>
          </template>
          <template v-else-if="profile">
            <p v-if="accountState === 'expired'" class="gws-action-hint hint--warn">
              This login has expired or been revoked<span v-if="profile.token_error"> ({{ profile.token_error }})</span>.
              Sign back in to restore Gmail, Calendar, Drive, and scheduled Google tasks.
            </p>
            <p v-if="connected" class="gws-action-hint gws-connected-line">
              Connected as {{ profile.email || profile.label }}<template v-if="linked">. Linked to {{ workspace }}</template>
            </p>
            <template v-else>
              <div v-if="!gwsAuthUrls[profileId] && !gwsReloginPending[profileId]" class="gws-btn-group">
                <button
                  v-if="profile.loopback_eligible"
                  type="button"
                  class="btn-primary btn-small"
                  :disabled="gwsSavingProfile === profileId"
                  @click="onSignIn"
                >Sign in with Google</button>
                <button
                  type="button"
                  class="btn-small"
                  :disabled="gwsSavingProfile === profileId"
                  @click="onPasteCode"
                >Paste a code instead</button>
              </div>
              <p v-if="gwsReloginPending[profileId]" class="gws-action-hint">
                <template v-if="gwsAuthUrls[profileId]">
                  The sign-in tab was blocked. Open this link, then finish in the browser — it connects automatically:
                  <a :href="gwsAuthUrls[profileId]" target="_blank" class="gws-auth-link">Open authorization page</a>
                </template>
                <template v-else>
                  Sign-in in progress: complete it in the browser tab that opened. It will connect automatically.
                </template>
                <button type="button" class="btn-small" @click="gwsReloginCancel(profileId)">Cancel</button>
              </p>
              <p v-else-if="gwsReloginError[profileId]" class="gws-action-hint hint--warn">{{ gwsReloginError[profileId] }}</p>
              <div v-else-if="gwsAuthUrls[profileId]" class="gws-auth-flow-box">
                <p class="gws-flow-step">
                  1. Follow the Google auth flow. If the browser did not open, click here:
                  <a :href="gwsAuthUrls[profileId]" target="_blank" class="gws-auth-link">Open authorization page</a>
                </p>
                <p class="gws-flow-step">
                  2. After signing in, copy the full redirect URL (even if it fails to load) or authorization code, and paste it below:
                </p>
                <input
                  v-model="gwsRedirectUrls[profileId]"
                  type="text"
                  class="routine-input gws-auth-input"
                  aria-label="Redirect URL or authorization code"
                  placeholder="Paste redirect URL (http://localhost/?code=...) or code"
                  :disabled="gwsSavingProfile === profileId"
                  @keyup.enter="exchangeGwsCode(profileId)"
                />
                <div class="gws-flow-buttons">
                  <button
                    type="button"
                    class="btn-primary btn-small"
                    :disabled="!gwsRedirectUrls[profileId]?.trim() || gwsSavingProfile === profileId"
                    @click="exchangeGwsCode(profileId)"
                  >{{ gwsSavingProfile === profileId ? 'Connecting...' : 'Complete Sign-In' }}</button>
                  <button
                    type="button"
                    class="btn-small"
                    :disabled="gwsSavingProfile === profileId"
                    @click="cancelGwsAuth(profileId)"
                  >Cancel</button>
                </div>
              </div>
            </template>
          </template>
        </section>
      </div>

      <footer class="gws-connect-foot">
        <button type="button" class="btn-small" @click="close">Cancel</button>
        <button
          type="button"
          class="btn-primary btn-small"
          :disabled="!(connected && linked) || linking"
          @click="emit('close')"
        >Done</button>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, ref, toRef, watch } from 'vue'
import HostTerminalButton from '../HostTerminalButton.vue'
import { useModalFocus } from '../../composables/useModalFocus'
import { GWS_ACCOUNTS_KEY, type GwsAccounts } from '../../composables/useGwsAccounts'
import { WORKSPACE_ADMIN_KEY, type WorkspaceAdminContext } from '../../composables/useWorkspaceAdmin'
import { useProjectStore } from '../../stores/projects'
import { errorMessage } from '../../lib/errorMessage'
import { deriveGwsProfileId, gwsStorageHint } from '../../lib/gwsProfileId'
import { gwsAccountState, gwsDialogSteps, type GwsDialogStepId } from '../../lib/gwsSetup'
import { workspaceLabel } from '../../lib/workspaceLabel'
import { writeClipboard } from '../../lib/codeCopy'

// The Connect Google account dialog: install gws, get an OAuth client, sign
// in. One modal sheet for the active workspace's account. The account id is
// derived from the workspace name and frozen while the dialog is open; nothing
// is registered until an action needs the profile, and the workspace is linked
// only once the account is connected.
const props = defineProps<{
  open: boolean
  workspace: string
  mode: 'connect' | 'reauth'
}>()
const emit = defineEmits<{ (e: 'close'): void }>()

const injectedGws = inject(GWS_ACCOUNTS_KEY)
if (!injectedGws) throw new Error('GoogleConnectDialog needs the Google accounts SettingsView provides')
const injectedAdmin = inject(WORKSPACE_ADMIN_KEY)
if (!injectedAdmin) throw new Error('GoogleConnectDialog needs the workspace admin SettingsView provides')
const gws: GwsAccounts = injectedGws
const admin: WorkspaceAdminContext = injectedAdmin

const {
  gwsIntegration,
  gwsSavingProfile,
  gwsAuthUrls,
  gwsRedirectUrls,
  gwsReloginPending,
  gwsReloginError,
  fetchGwsIntegration,
  gwsReloginHelpUrl,
  gwsReloginStart,
  gwsReloginCancel,
  startGwsAuth,
  exchangeGwsCode,
  cancelGwsAuth,
  handleClientSecretUpload,
} = gws

const projectStore = useProjectStore()

const sheetEl = ref<HTMLElement | null>(null)
const nameInput = ref<HTMLInputElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const gcloudCommand = ref<HTMLElement | null>(null)

const profileId = ref('')
const displayName = ref('')
const clientMode = ref<'gcloud' | 'upload'>('upload')
const linking = ref(false)

const profile = computed(
  () => gwsIntegration.value?.profiles?.find((p) => p.name === profileId.value) ?? null,
)
const steps = computed(() => gwsDialogSteps({ installed: !!gwsIntegration.value?.installed }, profile.value))
function stepState(id: GwsDialogStepId): string {
  return steps.value.find((step) => step.id === id)?.state ?? 'pending'
}
const accountState = computed(() => gwsAccountState(profile.value))
const connected = computed(() => accountState.value === 'connected')
const linked = computed(() => {
  const form = admin.workspaceForms.value.find((f) => f.name === props.workspace)
  return !!form && form.gws_profile === profileId.value
})

// The id is chosen once when the dialog opens. A workspace that already has a
// link keeps it; otherwise an unused profile with the derived name is adopted
// and one used by another workspace gets a numeric suffix.
watch(
  () => props.open,
  (open) => {
    if (!open) return
    const existing = admin.workspaceForms.value.find((f) => f.name === props.workspace)?.gws_profile
    const taken = new Set(
      (gwsIntegration.value?.profiles ?? [])
        .filter((p) => p.workspaces.some((w) => w !== props.workspace))
        .map((p) => p.name),
    )
    profileId.value = existing || deriveGwsProfileId(props.workspace, taken)
    displayName.value = profile.value?.label || `${workspaceLabel(props.workspace)} Google account`
    clientMode.value = 'upload'
  },
  { immediate: true },
)

// Once the account exists, its stored label is the display name.
watch(profile, (p) => {
  if (p && props.open) displayName.value = p.label
})

useModalFocus(sheetEl, toRef(props, 'open'), {
  initialFocus: nameInput,
  onEscape: close,
  // The opener is a menu item that unmounts, so the row restores focus.
  restoreFocus: false,
})

async function ensureProfile(): Promise<boolean> {
  if (profile.value) return true
  return gws.registerGwsProfile(profileId.value, displayName.value.trim())
}

async function onCheckAgain(): Promise<void> {
  await ensureProfile()
  await fetchGwsIntegration()
}

async function onUpload(event: Event): Promise<void> {
  if (!await ensureProfile()) return
  await handleClientSecretUpload(event, profileId.value)
}

// Both sign-in buttons open a tab. A browser only allows that inside the click's
// user activation, so an existing profile goes straight to the flow with no await.
async function onSignIn(): Promise<void> {
  if (!profile.value && !await ensureProfile()) return
  await gwsReloginStart(profileId.value)
}

async function onPasteCode(): Promise<void> {
  if (!profile.value && !await ensureProfile()) return
  await startGwsAuth(profileId.value)
}

async function onInstallInChat(): Promise<void> {
  await gws.installGwsInChat()
  close()
}

async function copyCommand(): Promise<void> {
  const text = gcloudCommand.value?.textContent ?? ''
  if (await writeClipboard(text)) return
  const el = gcloudCommand.value
  if (el) window.getSelection()?.selectAllChildren(el)
}

watch([connected, () => props.open], async ([isConnected, isOpen]) => {
  if (!isConnected || !isOpen || linked.value || linking.value) return
  linking.value = true
  try {
    await admin.setWorkspaceGwsLink(props.workspace, profileId.value)
  } catch (e) {
    projectStore.pushErrorToast('Could not link the Google account', `${props.workspace}: ${errorMessage(e)}`)
  } finally {
    linking.value = false
  }
}, { immediate: true })

// Cancelling is what stops a loopback listener from finishing a sign-in nobody
// is looking at.
function close(): void {
  if (gwsReloginPending.value[profileId.value]) gwsReloginCancel(profileId.value)
  cancelGwsAuth(profileId.value)
  emit('close')
}
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.gws-connect-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border);
}
.gws-connect-head h3 { margin: 0; font-size: var(--text-lg); font-weight: 700; }
.gws-connect-close {
  width: 36px;
  height: 36px;
  border: 0;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--fg2);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}
.gws-connect-close:hover { color: var(--fg); background: var(--bg3); }
.gws-connect-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  overflow: auto;
}
.gws-connect-foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--border);
}
.gws-step {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.gws-step-title {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 600;
}
.gws-step-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.gws-step-done {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
}
.gws-step-done::before {
  content: "\2713";
  margin-right: var(--space-2);
  color: var(--success);
}
.gws-step-done-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.gws-radio-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.gws-radio {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 32px;
  font-size: var(--text-sm);
  cursor: pointer;
}
.gws-command-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.gws-command {
  display: inline-block;
  min-width: 0;
  overflow-wrap: anywhere;
}
.gws-action-hint {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
}
.gws-btn-group {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-2);
}
.gws-btn-group > .btn-small,
.gws-btn-group > .btn-primary {
  width: 100%;
}
.gws-auth-flow-box {
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.gws-flow-step {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.4;
}
.gws-auth-link {
  color: var(--accent);
  text-decoration: underline;
  font-weight: 500;
}
.gws-auth-link:hover {
  color: var(--accent2);
}
.gws-auth-input {
  font-size: var(--text-xs) !important;
  padding: 4px 8px !important;
}
.gws-flow-buttons {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-1);
}
.gws-flow-buttons > button {
  flex: 1 1 0;
  min-width: 0;
}
@media (pointer: coarse) {
  .gws-connect .btn-small,
  .gws-connect .btn-primary,
  .gws-connect .routine-input,
  .gws-connect-close {
    min-height: var(--touch);
  }
  .gws-connect-close { min-width: var(--touch); }
  .gws-radio { min-height: var(--touch); }
}
</style>
