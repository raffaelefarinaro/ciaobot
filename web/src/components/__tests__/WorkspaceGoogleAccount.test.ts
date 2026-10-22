// @vitest-environment jsdom

// The Google account row on Settings > Workspace > Overview: the active
// workspace's one account in three states, its actions menu and the
// disconnect/unlink flows. The Connect dialog has its own file.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { GwsIntegrationProfile, GwsIntegrationSettings, WorkspacesResponse } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'

const state = vi.hoisted(() => ({
  workspaces: null as unknown,
  gws: null as unknown,
  confirm: true,
}))

vi.mock('../../lib/confirm', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/confirm')>()
  return { ...actual, askConfirm: vi.fn(() => Promise.resolve(state.confirm)) }
})

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') return Promise.resolve(state.workspaces)
    if (path === '/api/workspaces/archived') return Promise.resolve({ archived: [] })
    if (path === '/api/integrations/gws') return Promise.resolve(state.gws)
    if (path.startsWith('/api/proposals/handover')) {
      return Promise.resolve({
        workspace: 'work',
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
    name: 'work',
    label: 'Work Google account',
    purpose: '',
    examples: [],
    configured: true,
    credentials_present: true,
    client_secret_present: true,
    config_dir: '/tmp/secrets/gws',
    workspaces: ['work'],
    setup_command: 'ciao gws work auth login --full',
    headless_auth_command: 'ciao gws-auth-helper work',
    email: 'a@b.co',
    token_valid: true,
    token_error: '',
    needs_relogin: false,
    loopback_eligible: true,
    ...overrides,
  }
}

function integration(profiles: GwsIntegrationProfile[]): GwsIntegrationSettings {
  return { installed: true, binary_path: '/usr/local/bin/gws', cli_available: true, profiles }
}

function registry(links: Record<string, string>): WorkspacesResponse {
  return {
    workspaces: Object.entries(links).map(([name, gws_profile]) => ({
      name,
      vault_root: `${name}/memory-vault`,
      default_provider: 'claude',
      gws_profile,
      color: 'pink',
    })),
    active: 'work',
    primary: 'work',
    default_agent_fs_scope: 'workspace',
    provider_options: [{ value: 'claude', label: 'Claude' }],
  } as WorkspacesResponse
}

async function mountOverview() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push('/settings/workspace')
  await router.isReady()
  const store = useProjectStore()
  store.activeWorkspace = 'work'
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

function row(wrapper: Wrapper) {
  return wrapper.get('.gws-account')
}

async function openMenu(wrapper: Wrapper) {
  await row(wrapper).get('[aria-label="Actions for Google account"]').trigger('keydown', { key: 'Enter' })
  await flushPromises()
  await nextTick()
  return row(wrapper).findAll('.settings-menu-item')
}

function menuItem(items: ReturnType<typeof row>[], label: string) {
  const item = items.find((i) => i.text() === label)
  expect(item, `no menu item ${label}`).toBeTruthy()
  return item!
}

describe('Workspace Overview > Google account row', () => {
  beforeEach(async () => {
    localStorage.clear()
    document.body.innerHTML = ''
    setActivePinia(createPinia())
    state.confirm = true
    state.workspaces = registry({ work: 'work', home: '' })
    state.gws = integration([profile()])
    const { api } = await import('../../lib/api')
    vi.mocked(api.post).mockReset()
    vi.mocked(api.post).mockImplementation(() => Promise.resolve({}))
    vi.mocked(api.patch).mockReset()
    vi.mocked(api.patch).mockImplementation(() => Promise.resolve({}))
    const { askConfirm } = await import('../../lib/confirm')
    vi.mocked(askConfirm).mockClear()
  })

  it('connected shows name, email, Connected and the menu', async () => {
    const wrapper = await mountOverview()
    try {
      const text = row(wrapper).text()
      expect(text).toContain('Work Google account')
      expect(text).toContain('a@b.co')
      expect(text).toContain('Connected')
      const items = await openMenu(wrapper)
      expect(items.map((i) => i.text())).toEqual([
        'Re-authenticate',
        'Advanced / headless setup',
        'Disconnect and remove',
      ])
    } finally {
      wrapper.unmount()
    }
  })

  it('expired shows Sign in again', async () => {
    state.gws = integration([profile({ needs_relogin: true, token_error: 'invalid_grant' })])
    const wrapper = await mountOverview()
    try {
      expect(row(wrapper).text()).toContain('Sign-in expired')
      const button = row(wrapper).findAll('button.btn-primary').find((b) => b.text() === 'Sign in again')
      expect(button).toBeTruthy()
      await button!.trigger('click')
      await flushPromises()
      const dialog = wrapper.get('[role="dialog"]')
      expect(dialog.get('#gws-connect-title').text()).toBe('Sign in to Google again')
    } finally {
      wrapper.unmount()
    }
  })

  it('none shows Connect Google account', async () => {
    state.workspaces = registry({ work: '', home: '' })
    state.gws = integration([])
    const wrapper = await mountOverview()
    try {
      expect(row(wrapper).text()).toContain('No Google account is connected')
      expect(row(wrapper).findAll('button.btn-primary').map((b) => b.text())).toEqual(['Connect Google account'])
      const card = wrapper.findAll('.card').find((c) => c.find('.section-title').text() === 'Google account')!
      expect(card.find('select').exists()).toBe(false)
      expect(card.find('[id^="workspace-gws-hint-"]').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('linked profile with no credentials reads as none', async () => {
    state.gws = integration([profile({ configured: false, client_secret_present: false, credentials_present: false })])
    const wrapper = await mountOverview()
    try {
      expect(row(wrapper).text()).toContain('No Google account is connected')
      expect(row(wrapper).text()).not.toContain('Connected')
    } finally {
      wrapper.unmount()
    }
  })

  it('Disconnect and remove confirms and removes', async () => {
    const { api } = await import('../../lib/api')
    const { askConfirm } = await import('../../lib/confirm')
    vi.mocked(api.post).mockImplementation((path: string) => {
      if (path === '/api/integrations/gws/profiles/remove') {
        state.workspaces = registry({ work: '', home: '' })
        state.gws = integration([])
        return Promise.resolve(state.gws)
      }
      return Promise.resolve({})
    })
    const wrapper = await mountOverview()
    try {
      const items = await openMenu(wrapper)
      await menuItem(items, 'Disconnect and remove').trigger('click')
      await flushPromises()
      const message = vi.mocked(askConfirm).mock.calls[0][0]
      expect(message).toContain('Work Google account')
      expect(message).toContain('this machine')
      expect(vi.mocked(api.post).mock.calls).toEqual([
        ['/api/integrations/gws/profiles/remove', { profile: 'work' }],
      ])
      expect(row(wrapper).text()).toContain('No Google account is connected')
    } finally {
      wrapper.unmount()
    }
  })

  it('Disconnect and remove names the other workspace using the profile', async () => {
    const { api } = await import('../../lib/api')
    const { askConfirm } = await import('../../lib/confirm')
    state.gws = integration([profile({ workspaces: ['work', 'home'] })])
    state.confirm = false
    const wrapper = await mountOverview()
    try {
      const items = await openMenu(wrapper)
      await menuItem(items, 'Disconnect and remove').trigger('click')
      await flushPromises()
      expect(vi.mocked(askConfirm).mock.calls[0][0]).toContain('home')
      expect(vi.mocked(api.post)).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })

  it('Stop using in this workspace only unlinks', async () => {
    const { api } = await import('../../lib/api')
    state.gws = integration([profile({ workspaces: ['work', 'home'] })])
    vi.mocked(api.patch).mockImplementation(() => {
      state.workspaces = registry({ work: '', home: 'work' })
      return Promise.resolve(state.workspaces)
    })
    const wrapper = await mountOverview()
    try {
      const items = await openMenu(wrapper)
      await menuItem(items, 'Stop using in this workspace').trigger('click')
      await flushPromises()
      expect(vi.mocked(api.patch)).toHaveBeenCalledWith('/api/workspaces/work', { gws_profile: '' })
      expect(vi.mocked(api.post)).not.toHaveBeenCalled()
      expect(row(wrapper).text()).toContain('No Google account is connected')
    } finally {
      wrapper.unmount()
    }
  })
})
