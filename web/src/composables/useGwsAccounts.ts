import { computed, onUnmounted, ref, type InjectionKey } from 'vue'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'
import { errorMessage } from '../lib/errorMessage'
import { isLoopbackHostname } from '../lib/loopback'
import { useProjectStore } from '../stores/projects'
import type { GwsIntegrationSettings } from '../lib/types'

// Every Google account (gws profile) call and its transient state: the
// integration payload, the client-secret upload, the loopback and pasted-code
// sign-in flows, and the profile add/remove calls. SettingsView calls this
// once and provides it; the Overview row and the Connect dialog inject it, so
// there is one fetch path and one relogin timer per profile.

export function useGwsAccounts(deps: {
  notifySaved: (body: string, title?: string) => void
  notifyFailed: (title: string, detail: string) => void
}) {
  const { notifySaved, notifyFailed } = deps
  const projectStore = useProjectStore()

  const gwsIntegration = ref<GwsIntegrationSettings | null>(null)
  const gwsIntegrationLoaded = ref(false)
  const gwsIntegrationError = ref('')

  function gwsProfileStatus(profile: GwsProfile): string {
    if (profile.configured && profile.needs_relogin) return 'Login expired'
    if (profile.configured) return 'Authenticated'
    if (profile.client_secret_present) return 'Ready to auth'
    return 'Needs OAuth client'
  }

  function gwsProfileBadgeClass(profile: GwsProfile): string {
    if (profile.configured && profile.needs_relogin) return 'badge--error'
    if (profile.configured) return 'badge--success'
    if (profile.client_secret_present) return 'badge--warn'
    return 'badge--error'
  }

  // Only the accounts this install actually has. No built-in personal/work pair:
  // a fresh install has none until the user connects one from a workspace's Overview.
  const gwsProfileOptions = computed(() =>
    (gwsIntegration.value?.profiles || []).map((profile) => ({
      name: profile.name,
      label: profile.label,
      email: profile.email,
    })),
  )

  async function fetchGwsIntegration() {
    gwsIntegrationError.value = ''
    try {
      gwsIntegration.value = await api.get<GwsIntegrationSettings>('/api/integrations/gws')
    } catch (e) {
      gwsIntegrationError.value = `Failed to load Google Workspace integration: ${errorMessage(e)}`
    } finally {
      gwsIntegrationLoaded.value = true
    }
  }

  async function installGwsInChat() {
    try {
      await projectStore.fixError({
        errorText:
          'The @googleworkspace/cli (gws) is not installed on this machine, so Google Workspace tools are unavailable.',
        context:
          'Installing @googleworkspace/cli from Settings → Overview → Google account → Connect Google account. Run `npm install -g @googleworkspace/cli` (Node.js/npm required; npm exit code 243 / EACCES is common when /usr/local is root-owned after a .pkg Node install). Walk the operator through the install; Ciaobot never runs package installs on its own.',
        title: 'Install gws',
      })
    } catch (e) {
      notifyFailed('Could not open install chat', errorMessage(e))
    }
  }

  const gwsSavingProfile = ref<string | null>(null)
  const gwsAuthUrls = ref<Record<string, string>>({})
  // PKCE flow id issued alongside the auth URL (issue #354). The server holds the
  // code_verifier for this id and refuses an exchange that cannot name it, so the
  // id has to survive from `startGwsAuth` to `exchangeGwsCode` or manual connect
  // can never complete.
  const gwsFlowIds = ref<Record<string, string>>({})
  const gwsRedirectUrls = ref<Record<string, string>>({})

  // One-click "Sign in with Google" loopback flow (issue #145). Tracks, per
  // profile, whether a server-managed re-login is pending so the Settings card
  // can poll `relogin/status` until it completes instead of copy-pasting a
  // redirect URL. Mirrors the manual JSON-upload flow as the power-user path.
  const gwsReloginPending = ref<Record<string, boolean>>({})
  const gwsReloginError = ref<Record<string, string>>({})
  let gwsReloginTimer: ReturnType<typeof setInterval> | null = null
  // A tick can outlast its own interval: the completed branch awaits
  // fetchGwsIntegration(), which runs a health check that shells out per profile
  // with a 30s timeout. Without this, every 2s tick in that window starts another
  // round of status polls and another concurrent health scan.
  let gwsReloginPolling = false

  function gwsReloginHelpUrl(): string {
    return 'https://cloud.google.com/sdk/docs/install'
  }

  // The loopback re-login listener binds to this engine's own 127.0.0.1, so the
  // consent redirect only reaches it when the browser is on that machine
  // (localhost). From a phone or a LAN browser the popup's redirect would target
  // the browser's own loopback and never arrive — those users must use the
  // manual paste flow instead.
  function gwsOnEngineHost(): boolean {
    return isLoopbackHostname(window.location.hostname)
  }

  async function gwsReloginStart(profileName: string) {
    gwsReloginError.value[profileName] = ''
    if (!gwsOnEngineHost()) {
      // On a remote browser the loopback redirect can never arrive. Open the
      // manual paste flow directly so the button does something useful instead of
      // just showing an error the user then has to act on.
      await startGwsAuth(profileName)
      return
    }
    gwsSavingProfile.value = profileName
    // Open a placeholder tab synchronously so the browser does not treat it as a
    // popup (blocked outside the click's user-activation window); navigate it to
    // the consent URL once the backend hands one back. If the popup is blocked,
    // surface the link so the flow can still proceed manually.
    const tab = window.open('', '_blank')
    try {
      const res = await api.post<{ auth_url: string }>('/api/integrations/gws/relogin/start', {
        profile: profileName,
      })
      gwsReloginPending.value[profileName] = true
      if (tab && tab.location) {
        tab.location.href = res.auth_url
      } else {
        gwsReloginError.value[profileName] =
          'The sign-in tab was blocked by the browser. Open this link, then finish here:'
        gwsAuthUrls.value[profileName] = res.auth_url
      }
      gwsPollRelogin()
    } catch (e) {
      if (tab) tab.close()
      const msg = errorMessage(e, 'Could not start the sign-in flow.')
      // Web OAuth clients reject the loopback redirect entirely. That is not a
      // transient failure — retrying the same endpoint will never work. Fall
      // through to the manual paste flow so the sign-in still completes.
      if (/web app/i.test(msg)) {
        await startGwsAuth(profileName)
        return
      }
      gwsReloginError.value[profileName] = msg
    } finally {
      gwsSavingProfile.value = null
    }
  }

  function gwsClearRelogin(profileName: string) {
    delete gwsReloginPending.value[profileName]
    delete gwsReloginError.value[profileName]
    // The fallback auth URL belongs to a now-finished loopback session; clearing
    // it keeps the card out of a stale manual-flow state and un-hides the
    // authenticated controls on success.
    delete gwsAuthUrls.value[profileName]
    delete gwsRedirectUrls.value[profileName]
  }

  function gwsPollRelogin() {
    if (gwsReloginTimer) return
    gwsReloginTimer = setInterval(async () => {
      if (gwsReloginPolling) return
      const pending = Object.entries(gwsReloginPending.value)
        .filter(([, p]) => p)
        .map(([name]) => name)
      if (!pending.length) {
        if (gwsReloginTimer) {
          clearInterval(gwsReloginTimer)
          gwsReloginTimer = null
        }
        return
      }
      gwsReloginPolling = true
      try {
        for (const name of pending) {
          try {
            const status = await api.get<{ status: string; email?: string; error?: string }>(
              `/api/integrations/gws/relogin/status?profile=${encodeURIComponent(name)}`,
            )
            if (status.status === 'completed') {
              gwsClearRelogin(name)
              await fetchGwsIntegration()
              notifySaved('Google account connected.')
            } else if (status.status === 'error' || status.status === 'none') {
              const msg =
                status.error ||
                (status.status === 'none'
                  ? 'The sign-in timed out before you finished it. Try again.'
                  : 'The sign-in failed.')
              gwsClearRelogin(name)
              gwsReloginError.value[name] = msg
              await fetchGwsIntegration()
            }
          } catch (e) {
            const msg = errorMessage(e, 'Could not check the sign-in status.')
            gwsClearRelogin(name)
            gwsReloginError.value[name] = msg
          }
        }
      } finally {
        gwsReloginPolling = false
      }
    }, 2000)
  }

  function gwsReloginCancel(profileName: string) {
    gwsClearRelogin(profileName)
    api.post('/api/integrations/gws/relogin/cancel', { profile: profileName }).catch(() => {})
  }

  onUnmounted(() => {
    if (gwsReloginTimer) {
      clearInterval(gwsReloginTimer)
      gwsReloginTimer = null
    }
  })

  async function handleClientSecretUpload(event: Event, profileName: string) {
    const target = event.target as HTMLInputElement
    const file = target.files?.[0]
    if (!file) return

    gwsSavingProfile.value = profileName
    try {
      const content = await file.text()
      const updated = await api.post<GwsIntegrationSettings>('/api/integrations/gws/client-secret', {
        profile: profileName,
        client_secret: content,
      })
      gwsIntegration.value = updated
    } catch (e) {
      notifyFailed('Could not upload the client secret', errorMessage(e, 'The upload failed.'))
    } finally {
      gwsSavingProfile.value = null
      target.value = ''
    }
  }

  async function startGwsAuth(profileName: string) {
    gwsSavingProfile.value = profileName
    // A prior one-click failure (e.g. the remote-browser guard) leaves an error
    // that would otherwise win over the manual-flow box and hide it.
    delete gwsReloginError.value[profileName]
    delete gwsReloginPending.value[profileName]
    // Open a placeholder tab synchronously so the manual auth URL does not get
    // blocked as a popup — `window.open` after an `await` is outside the click's
    // user-activation window and most browsers block it, forcing the user to
    // click the fallback link. Same pattern as `gwsReloginStart`.
    const tab = window.open('', '_blank')
    try {
      const res = await api.post<{ auth_url: string; flow_id?: string }>('/api/integrations/gws/auth-url', {
        profile: profileName,
      })
      gwsAuthUrls.value[profileName] = res.auth_url
      // The auth URL carries a code_challenge; without the matching flow id the
      // exchange has no verifier to send and the server refuses it outright.
      gwsFlowIds.value[profileName] = res.flow_id || ''
      gwsRedirectUrls.value[profileName] = ''
      if (tab && tab.location) {
        tab.location.href = res.auth_url
      } else if (tab) {
        tab.close()
      }
      // The in-card "Open authorization page" link remains as the fallback when
      // the popup was blocked (tab is null). No extra error needed — the
      // gws-auth-flow-box already shows it.
    } catch (e) {
      if (tab) tab.close()
      notifyFailed('Could not generate the authorization URL', errorMessage(e, 'The request failed.'))
    } finally {
      gwsSavingProfile.value = null
    }
  }

  async function exchangeGwsCode(profileName: string) {
    const code = gwsRedirectUrls.value[profileName]?.trim()
    if (!code) return

    gwsSavingProfile.value = profileName
    try {
      const flowId = gwsFlowIds.value[profileName] || ''
      const updated = await api.post<GwsIntegrationSettings>('/api/integrations/gws/exchange', {
        profile: profileName,
        code: code,
        // Omitted only when the auth URL predates PKCE; the server tells those
        // apart from a flow it has forgotten, and needs the distinction.
        ...(flowId ? { flow_id: flowId } : {}),
      })
      gwsIntegration.value = updated
      delete gwsAuthUrls.value[profileName]
      delete gwsFlowIds.value[profileName]
      delete gwsRedirectUrls.value[profileName]
    } catch (e) {
      notifyFailed('Could not complete the connection', errorMessage(e, 'The request failed.'))
    } finally {
      gwsSavingProfile.value = null
    }
  }

  function cancelGwsAuth(profileName: string) {
    delete gwsAuthUrls.value[profileName]
    delete gwsFlowIds.value[profileName]
    delete gwsRedirectUrls.value[profileName]
  }

  async function disconnectGwsProfile(profileName: string, deleteClientSecret: boolean) {
    const message = deleteClientSecret
      ? `Are you sure you want to delete the OAuth Client Secret for the ${profileName} profile?`
      : `Are you sure you want to disconnect/sign out the ${profileName} Google account?`

    if (!await askConfirm(message, {
      title: deleteClientSecret ? 'Delete OAuth Client Secret' : 'Disconnect Google account',
      confirmLabel: deleteClientSecret ? 'Delete secret' : 'Disconnect',
      destructive: true,
    })) return

    gwsSavingProfile.value = profileName
    try {
      const updated = await api.post<GwsIntegrationSettings>('/api/integrations/gws/disconnect', {
        profile: profileName,
        delete_client_secret: deleteClientSecret,
      })
      gwsIntegration.value = updated
      cancelGwsAuth(profileName)
    } catch (e) {
      notifyFailed('Could not update the profile connection', errorMessage(e, 'The request failed.'))
    } finally {
      gwsSavingProfile.value = null
    }
  }


  /** POST /api/integrations/gws/profiles/add {name, label}. Sets gwsIntegration from the reply; false (and notifyFailed) on error. No confirm. */
  async function registerGwsProfile(name: string, label: string): Promise<boolean> {
    try {
      gwsIntegration.value = await api.post<GwsIntegrationSettings>(
        '/api/integrations/gws/profiles/add',
        { name, label },
      )
      return true
    } catch (e) {
      notifyFailed('Could not add the Google account', errorMessage(e, 'Failed to add the account'))
      return false
    }
  }

  /** POST /api/integrations/gws/profiles/remove {profile}. Sets gwsIntegration from the reply; false (and notifyFailed) on error. The caller confirms first. */
  async function removeGwsAccount(profile: string): Promise<boolean> {
    gwsSavingProfile.value = profile
    try {
      gwsIntegration.value = await api.post<GwsIntegrationSettings>(
        '/api/integrations/gws/profiles/remove',
        { profile },
      )
      return true
    } catch (e) {
      notifyFailed('Could not remove the Google account', errorMessage(e, 'Failed to remove the account'))
      return false
    } finally {
      gwsSavingProfile.value = null
    }
  }

  return {
    gwsIntegration,
    gwsIntegrationLoaded,
    gwsIntegrationError,
    gwsProfileStatus,
    gwsProfileBadgeClass,
    gwsProfileOptions,
    fetchGwsIntegration,
    installGwsInChat,
    gwsSavingProfile,
    gwsAuthUrls,
    gwsFlowIds,
    gwsRedirectUrls,
    gwsReloginPending,
    gwsReloginError,
    gwsReloginHelpUrl,
    gwsOnEngineHost,
    gwsReloginStart,
    gwsClearRelogin,
    gwsPollRelogin,
    gwsReloginCancel,
    handleClientSecretUpload,
    startGwsAuth,
    exchangeGwsCode,
    cancelGwsAuth,
    disconnectGwsProfile,
    registerGwsProfile,
    removeGwsAccount,
  }
}

export type GwsAccounts = ReturnType<typeof useGwsAccounts>
export type GwsProfile = GwsIntegrationSettings['profiles'][number]
export const GWS_ACCOUNTS_KEY: InjectionKey<GwsAccounts> = Symbol('gws-accounts')
