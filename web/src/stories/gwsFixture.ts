/**
 * Google Workspace fixture: one provisioned `useGwsAccounts` +
 * `useWorkspaceAdmin` pair behind the injection keys SettingsView provides in
 * production, seeded with synthetic profiles only. The workspace owners inject
 * exactly these keys; nothing here starts a real fetch — every read or write a
 * story exercises is declared in that story's `parameters.fixture` routes, and
 * anything else fails closed.
 */
import { defineComponent, h, provide, ref, type Component } from 'vue'
import {
  GWS_ACCOUNTS_KEY,
  useGwsAccounts,
  type GwsAccounts,
} from '../composables/useGwsAccounts'
import {
  WORKSPACE_ADMIN_KEY,
  useWorkspaceAdmin,
  type WorkspaceAdminContext,
  type WorkspaceForm,
} from '../composables/useWorkspaceAdmin'
import type { GwsIntegrationProfile, GwsIntegrationSettings } from '../lib/types'
import { DEFAULT_WORKSPACE_COLOR } from '../lib/workspaceColors'
import { useProjectStore } from '../stores/projects'

export const GWS_WORKSPACE = 'storybook-alpha'
export const GWS_SECOND_WORKSPACE = 'storybook-beta'
/** `deriveGwsProfileId('storybook-alpha', [])`, the id the dialog derives. */
export const GWS_PROFILE_ID = 'storybook-alpha'

/** A synthetic profile. Synthetic values only: example.test, no real paths or tokens. */
export function gwsProfile(overrides: Partial<GwsIntegrationProfile> = {}): GwsIntegrationProfile {
  return {
    name: GWS_PROFILE_ID,
    label: 'Workspace Google account',
    purpose: 'personal',
    examples: [],
    configured: true,
    credentials_present: true,
    client_secret_present: true,
    config_dir: `secrets/gws-${GWS_PROFILE_ID}/`,
    workspaces: [GWS_WORKSPACE],
    setup_command: `ciao gws ${GWS_PROFILE_ID} auth login --setup`,
    headless_auth_command: `ciao gws ${GWS_PROFILE_ID} auth login --headless`,
    email: 'account@example.test',
    token_valid: true,
    token_error: '',
    needs_relogin: false,
    loopback_eligible: true,
    ...overrides,
  }
}

export function gwsIntegration(
  profiles: GwsIntegrationProfile[],
  installed = true,
  installProblem = '',
): GwsIntegrationSettings {
  return {
    installed,
    binary_path: installed ? '/usr/bin/gws' : '',
    cli_available: installed,
    install_problem: installProblem,
    profiles,
  }
}

export const GWS_AUTH_URL = 'https://oauth.example.test/authorize?state=storybook'

export type GwsSeed = {
  /** The payload seeded into `gwsIntegration`; null keeps the composable's initial null. */
  integration?: GwsIntegrationSettings | null
  /** Default true (a settled read); false renders the owner's initial skeleton. */
  loaded?: boolean
  loadError?: string
  /** The workspace form is linked to the profile, so a connected dialog's auto-link write never fires. */
  linked?: boolean
  /** `gwsAuthUrls[name]` seed. */
  authUrls?: Record<string, string>
  reloginPending?: boolean
  reloginError?: string
  redirectUrls?: Record<string, string>
  /** Adds a second workspace form, so the profile's `others` list is non-empty. */
  secondWorkspace?: boolean
}

const seedForm = (profileId: string, name = GWS_WORKSPACE, second = false): WorkspaceForm => ({
  name: second ? GWS_SECOND_WORKSPACE : name,
  vault_root: `vault/${second ? GWS_SECOND_WORKSPACE : GWS_WORKSPACE}`,
  default_provider: 'claude',
  gws_profile: second ? GWS_PROFILE_ID : profileId,
  disallowed_tools: '',
  color: DEFAULT_WORKSPACE_COLOR,
  provider_overrides: {},
  claude_trim: {},
})

/**
 * Creates the composable pair, seeds it and provides both injection keys.
 * Must run inside a mounted component: the composables register cleanup
 * callbacks on the host's scope.
 */
export function provideGws(seed: GwsSeed = {}): { gws: GwsAccounts; admin: WorkspaceAdminContext } {
  const projectStore = useProjectStore()
  const gws = useGwsAccounts({
    notifySaved: (body, title) => { projectStore.pushToast({ chat_id: '', title: title ?? 'Google', body }) },
    notifyFailed: (title, detail) => { projectStore.pushErrorToast(title, detail) },
  })
  if (seed.integration !== null) gws.gwsIntegration.value = seed.integration ?? gwsIntegration([])
  gws.gwsIntegrationLoaded.value = seed.loaded ?? true
  if (seed.loadError) gws.gwsIntegrationError.value = seed.loadError
  for (const [name, url] of Object.entries(seed.authUrls ?? {})) gws.gwsAuthUrls.value[name] = url
  for (const [name, url] of Object.entries(seed.redirectUrls ?? {})) gws.gwsRedirectUrls.value[name] = url
  if (seed.reloginPending) gws.gwsReloginPending.value[GWS_PROFILE_ID] = true
  if (seed.reloginError) gws.gwsReloginError.value[GWS_PROFILE_ID] = seed.reloginError
  const admin = useWorkspaceAdmin({
    notifySaved: (body, title) => { projectStore.pushToast({ chat_id: '', title: title ?? 'Workspaces', body }) },
    notifyFailed: (title, detail) => { projectStore.pushErrorToast(title, detail) },
    gwsProfileOptions: gws.gwsProfileOptions,
    fetchGwsIntegration: gws.fetchGwsIntegration,
  })
  admin.workspaceForms.value = [
    seedForm(seed.linked ? GWS_PROFILE_ID : ''),
    ...(seed.secondWorkspace ? [seedForm(GWS_PROFILE_ID, GWS_SECOND_WORKSPACE, true)] : []),
  ]
  admin.workspaceBaseline.value = Object.fromEntries(
    admin.workspaceForms.value.map((form) => [form.name, JSON.stringify(form)]),
  )
  provide(GWS_ACCOUNTS_KEY, gws)
  // Mirrors SettingsView's menu helpers; the workspace owners do not use them.
  const settingsMenu = ref<string | null>(null)
  function setSettingsMenu(key: string, open: boolean) {
    if (open) settingsMenu.value = key
    else if (settingsMenu.value === key) settingsMenu.value = null
  }
  function closeSettingsMenuOnEscape(event: KeyboardEvent) {
    event.preventDefault()
    settingsMenu.value = null
  }
  const adminContext: WorkspaceAdminContext = { ...admin, settingsMenu, setSettingsMenu, closeSettingsMenuOnEscape }
  provide(WORKSPACE_ADMIN_KEY, adminContext)
  return { gws, admin: adminContext }
}

/** Host for injection-owning components with no required props: provisions and renders the owner. */
export function gwsHost(owner: Component, seed: GwsSeed = {}, extras: Component[] = []): Component {
  return defineComponent({
    name: 'GwsFixtureHost',
    setup() {
      provideGws(seed)
      return () => h('div', [
        h(owner),
        ...extras.map(extra => h(extra)),
      ])
    },
  })
}

/**
 * Host for `GoogleConnectDialog`: provisions, then renders the dialog the
 * story passes with its production props (`open`, `workspace`, `mode`), so the
 * story keeps its own owner import; the open state lives in the host.
 */
export function googleConnectHost(
  owner: Component,
  mode: 'connect' | 'reauth',
  seed: GwsSeed = {},
  startOpen = true,
): Component {
  return defineComponent({
    name: 'GoogleConnectFixtureHost',
    setup() {
      provideGws(seed)
      const open = ref(startOpen)
      return () => h(owner, {
        open: open.value,
        workspace: GWS_WORKSPACE,
        mode,
        onClose: () => { open.value = false },
      })
    },
  })
}
