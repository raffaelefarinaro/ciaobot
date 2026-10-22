<template>
  <div class="gws-account">
    <SkeletonLoader v-if="!gwsIntegrationLoaded" label="Loading Google account" :count="1" />
    <template v-else-if="gwsIntegrationError">
      <p class="hint hint--warn">{{ gwsIntegrationError }}</p>
      <button type="button" class="btn-small" @click="fetchGwsIntegration()">Retry</button>
    </template>
    <template v-else>
      <!-- Connected or sign-in expired: one row for the linked account. -->
      <div v-if="profile && state !== 'none'" class="set-row">
        <div class="set-row-head">
          <div class="set-row-main">
            <p class="set-row-title">{{ profile.label }}</p>
            <p v-if="profile.email" class="set-row-sub">{{ profile.email }}</p>
          </div>
          <div class="set-row-actions">
            <span v-if="state === 'connected'" class="badge badge--success">Connected</span>
            <span v-else class="badge badge--error">Sign-in expired</span>
            <button
              v-if="state === 'expired'"
              ref="connectBtn"
              type="button"
              class="btn-primary btn-small"
              @click="openDialog('reauth')"
            >Sign in again</button>
            <DropdownMenuRoot :open="menuOpen" :modal="false" @update:open="menuOpen = $event">
              <DropdownMenuTrigger as-child>
                <button
                  ref="menuBtn"
                  type="button"
                  class="set-menu-btn"
                  aria-label="Actions for Google account"
                  :disabled="busy"
                >&middot;&middot;&middot;</button>
              </DropdownMenuTrigger>
              <DropdownMenuContent as-child align="end" :side-offset="4" :collision-padding="8">
                <div class="settings-menu" @keydown.esc="closeMenuOnEscape">
                  <DropdownMenuItem v-if="state === 'connected'" as-child @select="openDialog('reauth')">
                    <button type="button" class="settings-menu-item">Re-authenticate</button>
                  </DropdownMenuItem>
                  <DropdownMenuItem as-child @select="advancedOpen = !advancedOpen">
                    <button type="button" class="settings-menu-item">Advanced / headless setup</button>
                  </DropdownMenuItem>
                  <DropdownMenuItem v-if="others.length" as-child @select="stopUsingHere">
                    <button type="button" class="settings-menu-item">Stop using in this workspace</button>
                  </DropdownMenuItem>
                  <DropdownMenuItem as-child @select="disconnectAndRemove">
                    <button type="button" class="settings-menu-item settings-menu-item--danger">Disconnect and remove</button>
                  </DropdownMenuItem>
                </div>
              </DropdownMenuContent>
            </DropdownMenuRoot>
          </div>
        </div>
        <p v-if="state === 'expired' && profile.token_error" class="hint hint--compact">{{ profile.token_error }}</p>
      </div>

      <!-- No account, or a link whose account has no credentials. -->
      <div v-else class="gws-account-none">
        <p class="hint">
          No Google account is connected to this workspace. Chats and schedules here run without Google access.
        </p>
        <button ref="connectBtn" type="button" class="btn-primary btn-small" @click="openDialog('connect')">Connect Google account</button>
      </div>

      <div v-if="advancedOpen && profile" class="gws-advanced-panel">
        <div v-if="!profile.client_secret_present">
          <span class="dev-label">Create client</span>
          <code class="gws-command">ciao gws {{ profile.name }} auth setup</code>
          <span class="hint hint--compact">Needs the <a :href="gwsReloginHelpUrl()" target="_blank" rel="noopener noreferrer">gcloud CLI</a>.</span>
        </div>
        <div v-if="profile.setup_command">
          <span class="dev-label">Login</span>
          <code class="gws-command">{{ profile.setup_command }}</code>
        </div>
        <div v-if="profile.headless_auth_command">
          <span class="dev-label">Headless</span>
          <code class="gws-command">{{ profile.headless_auth_command }}</code>
        </div>
        <div>
          <span class="dev-label">Stored in</span>
          <code class="gws-command">{{ profile.config_dir }}</code>
        </div>
        <button type="button" class="btn-small" @click="advancedOpen = false">Hide</button>
      </div>
    </template>

    <GoogleConnectDialog :open="dialogOpen" :workspace="workspace" :mode="dialogMode" @close="closeDialog" />
  </div>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, ref } from 'vue'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRoot,
  DropdownMenuTrigger,
} from 'reka-ui'
import SkeletonLoader from '../SkeletonLoader.vue'
import GoogleConnectDialog from './GoogleConnectDialog.vue'
import { GWS_ACCOUNTS_KEY, type GwsAccounts } from '../../composables/useGwsAccounts'
import { WORKSPACE_ADMIN_KEY, type WorkspaceAdminContext } from '../../composables/useWorkspaceAdmin'
import { askConfirm } from '../../lib/confirm'
import { errorMessage } from '../../lib/errorMessage'
import { gwsAccountState } from '../../lib/gwsSetup'
import { useProjectStore } from '../../stores/projects'

// The Google account row on Workspace > Overview: the active workspace's one
// account in three states (connected, sign-in expired, none), its actions
// menu and the Connect dialog. All Google state is SettingsView's
// useGwsAccounts; the workspace link lives in useWorkspaceAdmin.
const injectedGws = inject(GWS_ACCOUNTS_KEY)
if (!injectedGws) throw new Error('WorkspaceGoogleAccount needs the Google accounts SettingsView provides')
const injectedAdmin = inject(WORKSPACE_ADMIN_KEY)
if (!injectedAdmin) throw new Error('WorkspaceGoogleAccount needs the workspace admin SettingsView provides')
const gws: GwsAccounts = injectedGws
const admin: WorkspaceAdminContext = injectedAdmin

const { gwsIntegration, gwsIntegrationLoaded, gwsIntegrationError, fetchGwsIntegration, gwsReloginHelpUrl } = gws
const { activeWorkspaceForm: form } = admin
const projectStore = useProjectStore()

const workspace = computed(() => projectStore.activeWorkspace ?? '')
// `profiles` can be absent from a partial payload; treat that as no accounts.
const profile = computed(
  () => gwsIntegration.value?.profiles?.find((p) => p.name === form.value?.gws_profile) ?? null,
)
const state = computed(() => gwsAccountState(profile.value))
const others = computed(() => (profile.value?.workspaces ?? []).filter((w) => w !== workspace.value))

const dialogOpen = ref(false)
const dialogMode = ref<'connect' | 'reauth'>('connect')
const advancedOpen = ref(false)
const menuOpen = ref(false)
const busy = ref(false)
const connectBtn = ref<HTMLButtonElement | null>(null)
const menuBtn = ref<HTMLButtonElement | null>(null)

// Esc inside the menu closes it and marks the press handled, so ChatLayout's
// window handler does not leave Settings (same as SettingsView's menus).
function closeMenuOnEscape(event: KeyboardEvent): void {
  event.preventDefault()
  menuOpen.value = false
}

function openDialog(mode: 'connect' | 'reauth'): void {
  dialogMode.value = mode
  dialogOpen.value = true
}

// The primary button can be gone after a successful connect; the menu button
// then takes focus.
async function closeDialog(): Promise<void> {
  dialogOpen.value = false
  await nextTick()
  ;(connectBtn.value ?? menuBtn.value)?.focus()
}

function notifySaved(body: string, title: string): void {
  projectStore.pushToast({ chat_id: '', title, body })
}

async function disconnectAndRemove(): Promise<void> {
  const p = profile.value
  if (!p) return
  const names = others.value.join(', ')
  const message = others.value.length
    ? `${names} also uses this Google account. Removing it deletes its stored credentials and OAuth client from this machine, and ${names} and ${workspace.value} all lose Google access until an account is connected again.`
    : `Remove ${p.label} and delete its stored credentials and OAuth client from this machine? ${workspace.value} will run without Google access until you connect an account.`
  if (!await askConfirm(message, {
    title: 'Remove Google account?',
    confirmLabel: 'Remove account',
    destructive: true,
  })) return
  busy.value = true
  try {
    if (await gws.removeGwsAccount(p.name)) {
      await admin.refreshGwsLinks()
      notifySaved('Google account removed.', 'Google')
    }
  } finally {
    busy.value = false
  }
}

async function stopUsingHere(): Promise<void> {
  const p = profile.value
  if (!p) return
  if (!await askConfirm(
    `Stop using ${p.label} in ${workspace.value}? It stays connected for ${others.value.join(', ')}.`,
    { title: 'Stop using this account?', confirmLabel: 'Stop using', destructive: false },
  )) return
  busy.value = true
  try {
    await admin.setWorkspaceGwsLink(workspace.value, '')
  } catch (e) {
    projectStore.pushErrorToast('Could not stop using the Google account', `${workspace.value}: ${errorMessage(e)}`)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.gws-account-none {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-3);
}
.gws-account-none .hint { margin: 0; }
.gws-advanced-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  align-items: flex-start;
  margin-top: var(--space-3);
  font-size: var(--text-xs);
}
.dev-label {
  display: inline-block;
  min-width: 84px;
  color: var(--fg2);
}
.gws-command {
  display: inline-block;
  min-width: 0;
  overflow-wrap: anywhere;
}
@media (pointer: coarse) {
  .gws-account .btn-small,
  .gws-account .btn-primary {
    min-height: var(--touch);
  }
}
</style>
