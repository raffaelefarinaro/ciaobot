// @vitest-environment jsdom

// The Connect Google account dialog, opened from the Overview row of a
// workspace that has no account yet (acme). The profile id is derived from
// the workspace name; nothing is registered until an action needs it.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { GwsIntegrationProfile, GwsIntegrationSettings, WorkspacesResponse } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { workspaceLabel } from '../../lib/workspaceLabel'

const state = vi.hoisted(() => ({
  workspaces: null as unknown,
  gws: null as unknown,
  // What /api/integrations/gws/exchange answers with.
  exchange: null as unknown,
}))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') return Promise.resolve(state.workspaces)
    if (path === '/api/settings/routines') return Promise.resolve({ model_options: { anthropic: [] } })
    if (path === '/api/workspaces/archived') return Promise.resolve({ archived: [] })
    if (path === '/api/integrations/gws') return Promise.resolve(state.gws)
    if (path.startsWith('/api/proposals/handover')) {
      return Promise.resolve({
        workspace: 'acme',
        handover: 'off',
        acting_available: true,
        agreement: {
          decided: 0, matched: 0, rate: null, window: 50, min_decisions: 20, threshold: 0.9,
          predicted_accept: 0, accept_matched: 0, predicted_dismiss: 0, dismiss_matched: 0,
          suggest_handover: false,
        },
      })
    }
    if (path.startsWith('/api/update-tasks')) return Promise.resolve({ tasks: [] })
    return Promise.resolve({})
  })
  return {
    api: {
      get,
      post: vi.fn(() => Promise.resolve({})),
      patch: vi.fn(() => Promise.resolve({})),
      del: vi.fn(() => Promise.resolve({})),
    },
  }
})

vi.mock('../../lib/push', () => ({
  pushSupported: () => false,
  pushEnabled: () => false,
  enablePush: vi.fn(),
  disablePush: vi.fn(),
}))

const Stub = defineComponent({ render: () => h('div') })

function profile(overrides: Partial<GwsIntegrationProfile> = {}): GwsIntegrationProfile {
  return {
    name: 'acme',
    label: 'Acme Google account',
    purpose: '',
    examples: [],
    configured: false,
    credentials_present: false,
    client_secret_present: false,
    config_dir: '/tmp/secrets/gws-acme',
    workspaces: [],
    setup_command: '',
    headless_auth_command: '',
    email: '',
    token_valid: null,
    token_error: '',
    needs_relogin: false,
    loopback_eligible: true,
    ...overrides,
  }
}

function integration(profiles: GwsIntegrationProfile[]): GwsIntegrationSettings {
  return { installed: true, binary_path: '/usr/local/bin/gws', cli_available: true, profiles }
}

function registry(acmeLink = ''): WorkspacesResponse {
  return {
    workspaces: ['acme', 'other'].map((name) => ({
      name,
      vault_root: `${name}/memory-vault`,
      default_provider: 'claude',
      gws_profile: name === 'acme' ? acmeLink : '',
      color: 'pink',
    })),
    active: 'acme',
    primary: 'acme',
    default_agent_fs_scope: 'workspace',
    provider_options: [{ value: 'claude', label: 'Claude' }],
  } as WorkspacesResponse
}

type Api = (typeof import('../../lib/api'))['api']

// profiles/add registers the profile the way the engine does and answers with
// the new integration payload.
async function wireApi(): Promise<Api> {
  const { api } = await import('../../lib/api')
  vi.mocked(api.post).mockReset()
  vi.mocked(api.post).mockImplementation((path: string, body?: unknown) => {
    const data = (body ?? {}) as Record<string, string>
    if (path === '/api/integrations/gws/profiles/add') {
      const current = state.gws as GwsIntegrationSettings
      state.gws = integration([...current.profiles, profile({ name: data.name, label: data.label || `${data.name} account` })])
      return Promise.resolve(state.gws)
    }
    if (path === '/api/integrations/gws/relogin/start') return Promise.resolve({ auth_url: 'https://accounts.example/consent' })
    if (path === '/api/integrations/gws/auth-url') return Promise.resolve({ auth_url: 'https://accounts.example/manual', flow_id: 'flow-1' })
    if (path === '/api/integrations/gws/exchange') {
      state.gws = state.exchange
      return Promise.resolve(state.gws)
    }
    if (path === '/api/integrations/gws/client-secret') {
      const current = state.gws as GwsIntegrationSettings
      state.gws = integration(current.profiles.map((p) => (p.name === data.profile ? { ...p, client_secret_present: true } : p)))
      return Promise.resolve(state.gws)
    }
    return Promise.resolve({})
  })
  vi.mocked(api.patch).mockReset()
  vi.mocked(api.patch).mockImplementation(() => Promise.resolve(state.workspaces))
  return api
}

async function mountOverview() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push('/settings/workspace')
  await router.isReady()
  const store = useProjectStore()
  store.activeWorkspace = 'acme'
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

type Wrapper = Awaited<ReturnType<typeof mountOverview>>

function connectButton(wrapper: Wrapper) {
  return wrapper.get('.gws-account button.btn-primary')
}

async function openDialog(wrapper: Wrapper) {
  await connectButton(wrapper).trigger('click')
  await flushPromises()
  await nextTick()
  return wrapper.get('[role="dialog"]')
}

function button(dialog: ReturnType<Wrapper['get']>, label: string) {
  const found = dialog.findAll('button').find((b) => b.text() === label)
  expect(found, `no button ${label}`).toBeTruthy()
  return found!
}

function posts(api: Api, path: string) {
  return vi.mocked(api.post).mock.calls.filter(([p]) => p === path)
}

describe('Connect Google account dialog', () => {
  let api: Api

  beforeEach(async () => {
    localStorage.clear()
    document.body.innerHTML = ''
    setActivePinia(createPinia())
    state.workspaces = registry()
    state.gws = integration([])
    state.exchange = null
    api = await wireApi()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('opens with a derived, never typed profile id', async () => {
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      expect(dialog.find('input[name*="profile" i]').exists()).toBe(false)
      expect(dialog.text()).not.toContain('Account name')
      expect(dialog.get('#gws-connect-storage').text()).toContain('secrets/gws-acme/')
      const name = dialog.get('input.routine-input').element as HTMLInputElement
      expect(name.value).toBe(`${workspaceLabel('acme')} Google account`)
    } finally {
      wrapper.unmount()
    }
  })

  it('nothing is registered until an action needs the profile', async () => {
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      await button(dialog, 'Cancel').trigger('click')
      await flushPromises()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(posts(api, '/api/integrations/gws/profiles/add')).toHaveLength(0)
    } finally {
      wrapper.unmount()
    }
  })

  it('first action registers the profile once with the derived id and label', async () => {
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      await dialog.get('input.routine-input').setValue('My work')
      await dialog.findAll('input[type="radio"]')[0].setValue()
      await button(dialog, 'Check again').trigger('click')
      await flushPromises()
      await button(dialog, 'Check again').trigger('click')
      await flushPromises()
      expect(posts(api, '/api/integrations/gws/profiles/add')).toEqual([
        ['/api/integrations/gws/profiles/add', { name: 'acme', label: 'My work' }],
      ])
    } finally {
      wrapper.unmount()
    }
  })

  it('an id used by another workspace gets a suffix', async () => {
    state.gws = integration([profile({ workspaces: ['other'] })])
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      await dialog.findAll('input[type="radio"]')[0].setValue()
      await button(dialog, 'Check again').trigger('click')
      await flushPromises()
      expect(posts(api, '/api/integrations/gws/profiles/add')[0][1]).toMatchObject({ name: 'acme-2' })
    } finally {
      wrapper.unmount()
    }
  })

  it('an unused profile with the derived id is adopted', async () => {
    state.gws = integration([profile({ label: 'Old', client_secret_present: true, workspaces: [] })])
    vi.stubGlobal('open', vi.fn(() => ({ location: { href: '' }, close: vi.fn() })))
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      const name = dialog.get('input.routine-input')
      expect((name.element as HTMLInputElement).value).toBe('Old')
      expect(name.attributes('readonly')).toBeDefined()
      await button(dialog, 'Paste a code instead').trigger('click')
      await flushPromises()
      expect(posts(api, '/api/integrations/gws/profiles/add')).toHaveLength(0)
      expect(posts(api, '/api/integrations/gws/auth-url')).toHaveLength(1)
    } finally {
      wrapper.unmount()
    }
  })

  it('step 2 offers gcloud and upload', async () => {
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      const group = dialog.get('[role="radiogroup"][aria-label="OAuth client"]')
      const radios = group.findAll('input[type="radio"]')
      expect(radios).toHaveLength(2)
      expect(dialog.text()).toContain('Upload client_secret.json')
      await radios[0].setValue()
      expect(dialog.get('code.gws-command').text()).toBe('ciao gws acme auth setup')
      expect(dialog.text()).not.toContain('Run in Terminal')
      await radios[1].setValue()
      expect(button(dialog, 'Upload client_secret.json').exists()).toBe(true)
    } finally {
      wrapper.unmount()
    }
  })

  it('upload posts the client secret for the derived profile', async () => {
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      const input = dialog.get('input[type="file"]')
      const file = new File(['{"installed":{}}'], 'client_secret.json', { type: 'application/json' })
      Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
      await input.trigger('change')
      await flushPromises()
      expect(vi.mocked(api.post).mock.calls.map(([path]) => path)).toEqual([
        '/api/integrations/gws/profiles/add',
        '/api/integrations/gws/client-secret',
      ])
      expect(posts(api, '/api/integrations/gws/client-secret')[0][1]).toEqual({
        profile: 'acme',
        client_secret: '{"installed":{}}',
      })
    } finally {
      wrapper.unmount()
    }
  })

  it('sign in is disabled until a client exists', async () => {
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      const signIn = button(dialog, 'Sign in with Google')
      expect(signIn.attributes('disabled')).toBeDefined()
      const reason = dialog.get(`#${signIn.attributes('aria-describedby')}`)
      expect(reason.text()).toBe('Upload an OAuth client first.')
    } finally {
      wrapper.unmount()
    }
  })

  it('Sign in with Google starts the loopback flow', async () => {
    state.gws = integration([profile({ client_secret_present: true })])
    const tab = { location: { href: '' }, close: vi.fn() }
    vi.stubGlobal('open', vi.fn(() => tab))
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      await button(dialog, 'Sign in with Google').trigger('click')
      await flushPromises()
      expect(posts(api, '/api/integrations/gws/relogin/start')).toEqual([
        ['/api/integrations/gws/relogin/start', { profile: 'acme' }],
      ])
      expect(tab.location.href).toBe('https://accounts.example/consent')
    } finally {
      wrapper.unmount()
    }
  })

  it('Paste a code instead runs the manual flow', async () => {
    state.gws = integration([profile({ client_secret_present: true })])
    state.exchange = integration([profile({ client_secret_present: true })])
    vi.stubGlobal('open', vi.fn(() => ({ location: { href: '' }, close: vi.fn() })))
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      await button(dialog, 'Paste a code instead').trigger('click')
      await flushPromises()
      expect(posts(api, '/api/integrations/gws/auth-url')).toEqual([
        ['/api/integrations/gws/auth-url', { profile: 'acme' }],
      ])
      await dialog.get('input.gws-auth-input').setValue('4/abc')
      await button(dialog, 'Complete Sign-In').trigger('click')
      await flushPromises()
      expect(posts(api, '/api/integrations/gws/exchange')).toEqual([
        ['/api/integrations/gws/exchange', { profile: 'acme', code: '4/abc', flow_id: 'flow-1' }],
      ])
    } finally {
      wrapper.unmount()
    }
  })

  it('links the workspace once connected and enables Done', async () => {
    state.gws = integration([profile({ client_secret_present: true })])
    state.exchange = integration([profile({ client_secret_present: true, configured: true, email: 'me@acme.co' })])
    vi.stubGlobal('open', vi.fn(() => ({ location: { href: '' }, close: vi.fn() })))
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      expect(button(dialog, 'Done').attributes('disabled')).toBeDefined()
      await button(dialog, 'Paste a code instead').trigger('click')
      await flushPromises()
      await dialog.get('input.gws-auth-input').setValue('4/abc')
      await button(dialog, 'Complete Sign-In').trigger('click')
      await flushPromises()
      expect(vi.mocked(api.patch).mock.calls).toEqual([['/api/workspaces/acme', { gws_profile: 'acme' }]])
      expect(button(dialog, 'Done').attributes('disabled')).toBeUndefined()
      expect(dialog.text()).toContain('Linked to acme')
    } finally {
      wrapper.unmount()
    }
  })

  it('Escape closes, cancels a pending sign-in and leaves focus on the opener', async () => {
    state.gws = integration([profile({ client_secret_present: true })])
    vi.stubGlobal('open', vi.fn(() => ({ location: { href: '' }, close: vi.fn() })))
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      await button(dialog, 'Sign in with Google').trigger('click')
      await flushPromises()
      expect(dialog.text()).toContain('Sign-in in progress')
      await dialog.trigger('keydown', { key: 'Escape' })
      await flushPromises()
      await nextTick()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(posts(api, '/api/integrations/gws/relogin/cancel')).toEqual([
        ['/api/integrations/gws/relogin/cancel', { profile: 'acme' }],
      ])
      expect(document.activeElement).toBe(connectButton(wrapper).element)
    } finally {
      wrapper.unmount()
    }
  })

  it('Tab stays inside the dialog', async () => {
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      const first = dialog.get('[aria-label="Close"]').element as HTMLElement
      const last = button(dialog, 'Cancel').element as HTMLElement
      last.focus()
      await dialog.trigger('keydown', { key: 'Tab' })
      expect(document.activeElement).toBe(first)
      await dialog.trigger('keydown', { key: 'Tab', shiftKey: true })
      expect(document.activeElement).toBe(last)
    } finally {
      wrapper.unmount()
    }
  })

  it('has the modal semantics', async () => {
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      expect(dialog.attributes('aria-modal')).toBe('true')
      const heading = document.getElementById(dialog.attributes('aria-labelledby')!)
      expect(heading?.textContent).toBe('Connect Google account')
      let inertSiblings = 0
      for (let node: HTMLElement | null = dialog.element as HTMLElement; node && node !== document.body; node = node.parentElement) {
        expect(Boolean(node.inert)).toBe(false)
        for (const sibling of Array.from(node.parentElement?.children ?? [])) {
          if (sibling !== node && (sibling as HTMLElement).inert) inertSiblings += 1
        }
      }
      expect(inertSiblings).toBeGreaterThan(0)
    } finally {
      wrapper.unmount()
    }
  })

  it('never shows or copies a stored link that is not a plain profile id', async () => {
    state.workspaces = registry('a;b\nc')
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      await dialog.findAll('input[type="radio"]')[0].setValue()
      const shown = dialog.get('code.gws-command').text()
      expect(shown).toBe('ciao gws acme auth setup')
      expect(shown).not.toMatch(/[\n;]/)
      expect(dialog.get('#gws-connect-storage').text()).toContain('secrets/gws-acme/')
      await button(dialog, 'Check again').trigger('click')
      await flushPromises()
      expect(posts(api, '/api/integrations/gws/profiles/add')[0][1]).toMatchObject({ name: 'acme' })
    } finally {
      wrapper.unmount()
    }
  })

  it('reauth on a connected account offers sign in', async () => {
    state.workspaces = registry('acme')
    state.gws = integration([profile({ configured: true, client_secret_present: true, email: 'me@acme.co', workspaces: ['acme'] })])
    vi.stubGlobal('open', vi.fn(() => ({ location: { href: '' }, close: vi.fn() })))
    const wrapper = await mountOverview()
    try {
      await wrapper.get('[aria-label="Actions for Google account"]').trigger('keydown', { key: 'Enter' })
      await flushPromises()
      await nextTick()
      const item = wrapper.findAll('.settings-menu-item').find((i) => i.text() === 'Re-authenticate')!
      await item.trigger('click')
      await flushPromises()
      const dialog = wrapper.get('[role="dialog"]')
      const signIn = button(dialog, 'Sign in with Google')
      expect(signIn.attributes('disabled')).toBeUndefined()
      await signIn.trigger('click')
      await flushPromises()
      expect(posts(api, '/api/integrations/gws/relogin/start')).toEqual([
        ['/api/integrations/gws/relogin/start', { profile: 'acme' }],
      ])
    } finally {
      wrapper.unmount()
    }
  })

  it('links the workspace the dialog was opened for, not the one active later', async () => {
    state.gws = integration([profile({ client_secret_present: true })])
    state.exchange = integration([profile({ client_secret_present: true, configured: true })])
    vi.stubGlobal('open', vi.fn(() => ({ location: { href: '' }, close: vi.fn() })))
    const wrapper = await mountOverview()
    try {
      const dialog = await openDialog(wrapper)
      await button(dialog, 'Paste a code instead').trigger('click')
      await flushPromises()
      await dialog.get('input.gws-auth-input').setValue('4/abc')
      useProjectStore().activeWorkspace = 'other'
      await flushPromises()
      await button(wrapper.get('[role="dialog"]'), 'Complete Sign-In').trigger('click')
      await flushPromises()
      expect(vi.mocked(api.patch).mock.calls).toEqual([['/api/workspaces/acme', { gws_profile: 'acme' }]])
    } finally {
      wrapper.unmount()
    }
  })

  it('a failed copy keeps the command visible and says so', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn(() => Promise.reject(new Error('denied'))) } })
    const wrapper = await mountOverview()
    try {
      const store = useProjectStore()
      const toast = vi.spyOn(store, 'pushErrorToast')
      const dialog = await openDialog(wrapper)
      await dialog.findAll('input[type="radio"]')[0].setValue()
      await button(dialog, 'Copy').trigger('click')
      await flushPromises()
      expect(toast).toHaveBeenCalledWith('Google account', 'Could not copy. Select the command and copy it by hand.')
      expect(dialog.get('code.gws-command').text()).toBe('ciao gws acme auth setup')
    } finally {
      wrapper.unmount()
    }
  })
})
