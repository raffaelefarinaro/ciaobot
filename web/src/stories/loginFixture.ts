import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { expect, waitFor } from 'storybook/test'
import LoginView from '../components/LoginView.vue'
import type { SetupStatus, SetupProviderStatus } from '../lib/types'
import type { FixtureRoute } from './fixtureBoundary'
import { mountConnected } from './mountConnected'

/**
 * Synthetic connected fixtures for the startup/offline/login story slice
 * (#3310, B25): the `LoginView` route composition. It mounts as the `/` route
 * of a fresh Pinia app through #3267's `mountConnected`, probes
 * `GET /api/setup-status` on mount and — in the wizard — polls it and inspects
 * the chosen folder, so every story declares exactly the routes its own flow
 * performs and anything else fails closed. The login or setup writes a story
 * exercises are either held or answered so the owner records its own failure;
 * a successful credential write would navigate away and never happens here.
 * Synthetic names and text only.
 */

export const LOGIN_STATUS_PATH = '/api/setup-status'
export const LOGIN_SETUP_DONE_PATH = '/api/setup/finish'
/** The synthetic password the login plays type (never a real one). */
export const LOGIN_PASSWORD = 'storybook-password'

/** The workspace folder the wizard starts with (the component's own default). */
export const LOGIN_WORKSPACE = '~/ciaobot'
/** Encoded once for the exact route paths the boundary matches. */
const inspected = (path: string) => `/api/setup/inspect-folder?path=${encodeURIComponent(path)}`
const listed = (path: string) => `/api/setup/list-dirs?path=${encodeURIComponent(path)}`

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

export function route(method: FixtureRoute['method'], path: string, respond: () => Response | Promise<never>): FixtureRoute {
  return { method, path, respond }
}

/** A declared read or write that never answers: the owner stays in its busy state. */
export function held(): Promise<never> {
  return new Promise<never>(() => {})
}

/** A stubbed navigator.clipboard: real clipboard access is not a story fact. */
export function clipboardStub(): () => void {
  const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => {} } })
  return () => {
    if (original) Object.defineProperty(navigator, 'clipboard', original)
    else Reflect.deleteProperty(navigator, 'clipboard')
  }
}

/** The answer the mount probe and the wizard poll read for. */
export function setupStatus(overrides: Partial<SetupStatus> = {}): SetupStatus {
  return {
    configured: true,
    bootstrap: false,
    mode: 'vault',
    workspace_root: '~/storybook-workspace',
    vault_root: '~/storybook-workspace/memory-vault',
    checks: [],
    provider_ready: true,
    providers: providersReady(),
    ...overrides,
  }
}

/** The first-run machine: the wizard probes, polls and inspects the folder. */
export function bootstrapStatus(overrides: Partial<SetupStatus> = {}): SetupStatus {
  return setupStatus({ bootstrap: true, configured: false, ...overrides })
}

/** Both providers ready: the wizard's status card shows the ok badge. */
export function providersReady(): Record<string, SetupProviderStatus> {
  return {
    claude: { ok: true, auth: 'configured', detail: 'ready' },
    opencode: { ok: true, auth: 'configured', detail: 'ready' },
  }
}

/** The not-installed provider table: badge error, command and PATH steps. */
export function providersSetupNeeded(): Record<string, SetupProviderStatus> {
  return {
    claude: {
      ok: false,
      auth: 'not_installed',
      detail: 'not yet installed',
      command: 'provider-cli login',
      path_command: 'export PATH="$HOME/.provider/bin:$PATH"',
      install_url: 'https://install.example.test/provider',
      app_path: 'Storybook Desktop app',
    },
    opencode: { ok: false, auth: 'not_installed', detail: 'not installed', command: 'opencode login' },
  }
}

/** GET /api/setup-status answered by a canned `SetupStatus`. */
export function statusRoute(respond: () => Response | Promise<never>): FixtureRoute {
  return route('GET', LOGIN_STATUS_PATH, respond)
}

/** GET /api/setup/inspect-folder for an exactly encoded folder path. */
export function inspectRoute(path: string, respond: () => Response | Promise<never>): FixtureRoute {
  return route('GET', inspected(path), respond)
}

/** GET /api/setup/list-dirs for an exactly encoded folder path. */
export function listDirsRoute(path: string | undefined, respond: () => Response | Promise<never>): FixtureRoute {
  return route('GET', path === undefined ? '/api/setup/list-dirs' : listed(path), respond)
}

export type DirListing = {
  path: string
  display_path: string
  parent: string | null
  home: string
  dirs: Array<{ name: string; path: string }>
}

export function listing(path: string, dirs: DirListing['dirs'] = [], parent: string | null = null): DirListing {
  return { path, display_path: path, parent, home: '~', dirs }
}

/** The folder inspect answer: no existing workspaces, plus optional notes. */
export function inspectAnswer(extra: { existing_workspaces?: string[]; cloud_sync_warning?: string; macos_access_note?: string } = {}): Response {
  return json({
    mode: 'vault',
    existing_workspaces: extra.existing_workspaces ?? [],
    ...(extra.cloud_sync_warning ? { cloud_sync_warning: extra.cloud_sync_warning } : {}),
    ...(extra.macos_access_note ? { macos_access_note: extra.macos_access_note } : {}),
  })
}

/** The wizard's two fixed reads: the probe route and its poll, plus the folder inspection answer. */
export function wizardRoutes(workspace: string, respond: () => Response | Promise<never>): FixtureRoute[] {
  return [
    statusRoute(() => json(bootstrapStatus())),
    inspectRoute(workspace, respond),
  ]
}

export type LoginPanelConfig = {
  label: string
  /** Stubs navigator.clipboard for the owner's copy buttons. */
  clipboard?: boolean
}

/** The production LoginView mounted as the `/` route of the fixture app. */
export function renderLoginState(config: LoginPanelConfig) {
  return defineComponent({
    name: 'LoginStateStoryPanel',
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountConnected>>()
      const error = ref('')
      let restoreClipboard: (() => void) | undefined
      onMounted(() => {
        try {
          if (config.clipboard) restoreClipboard = clipboardStub()
          fixture.value = mountConnected(host.value!, { component: LoginView })
        } catch (cause) {
          error.value = String(cause)
        }
      })
      onBeforeUnmount(() => {
        fixture.value?.dispose()
        restoreClipboard?.()
      })
      return () => h('div', [
        h('p', `Synthetic ${config.label} fixture for the production LoginView.`),
        h('div', { ref: host }),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
      ])
    },
  })
}

/**
 * Waits for the owner's first declared read to be recorded, then one quiet
 * half second, and requires that the boundary still records no violation.
 * LoginView always probes on mount, so a zero count is a story bug.
 */
export async function settled(canvasElement: HTMLElement): Promise<void> {
  await waitFor(() => expect(canvasElement.textContent).toMatch(/Fixture requests: [1-9]/))
  await new Promise((resolve) => setTimeout(resolve, 500))
  expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  expect(canvasElement.textContent).not.toContain('Story network violation')
}
