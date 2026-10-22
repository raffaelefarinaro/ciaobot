// @vitest-environment jsdom

// Settings > Manage workspaces (/settings/workspaces): every workspace as a
// boxed row, the short New workspace form, the Google Workspace card and the
// Archived list. Editing lives on a workspace's Overview.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { WorkspacesResponse } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'

const state = vi.hoisted(() => ({ workspaces: null as unknown }))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') return Promise.resolve(state.workspaces)
    if (path === '/api/workspaces/archived') return Promise.resolve({ archived: [] })
    if (path === '/api/integrations/gws') {
      return Promise.resolve({
        installed: true,
        binary_path: '/usr/bin/gws',
        cli_available: true,
        profiles: [{
          name: 'p', label: 'Personal', purpose: '', examples: [], configured: true,
          credentials_present: true, client_secret_present: true, config_dir: '/x',
          workspaces: ['personal'], setup_command: '', headless_auth_command: '',
          email: 'p@example.com', token_valid: true, token_error: '', needs_relogin: false,
          loopback_eligible: true,
        }],
      })
    }
    if (path.startsWith('/api/proposals/handover')) {
      return Promise.resolve({
        workspace: 'personal',
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

function registry(names: string[]): WorkspacesResponse {
  return {
    workspaces: names.map(name => ({
      name,
      vault_root: `${name}/memory-vault`,
      default_provider: 'claude',
      gws_profile: name === 'personal' ? 'p' : '',
      color: 'pink',
      agent_fs_scope: name === 'work' ? 'machine' : null,
    })),
    active: names[0] ?? null,
    primary: 'personal',
    default_agent_fs_scope: 'workspace',
    provider_options: [{ value: 'claude', label: 'Anthropic (via Claude Code)' }],
  } as WorkspacesResponse
}

async function mountManage(path = '/settings/workspaces') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push(path)
  await router.isReady()
  const store = useProjectStore()
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  await nextTick()
  return { wrapper, router, store }
}

type Mounted = Awaited<ReturnType<typeof mountManage>>

function rows(wrapper: Mounted['wrapper']) {
  return wrapper.findAll('.workspace-list .workspace-row')
}

function rowFor(wrapper: Mounted['wrapper'], name: string) {
  const row = rows(wrapper).find(r => r.find('.workspace-title').text() === name)
  expect(row, `no row for ${name}`).toBeTruthy()
  return row!
}

async function openMenu(row: ReturnType<typeof rowFor>) {
  await row.get('.set-menu-btn').trigger('keydown', { key: 'Enter' })
  await flushPromises()
  await nextTick()
  return row.findAll('.settings-menu-item')
}

describe('Settings > Manage workspaces', () => {
  beforeEach(async () => {
    localStorage.clear()
    document.body.innerHTML = ''
    setActivePinia(createPinia())
    state.workspaces = registry(['personal', 'work'])
    const { api } = await import('../../lib/api')
    vi.mocked(api.post).mockReset()
    vi.mocked(api.post).mockImplementation(() => Promise.resolve({}))
  })

  it('each workspace is a boxed row with tile, name and summary', async () => {
    const { wrapper } = await mountManage()
    try {
      expect(rows(wrapper)).toHaveLength(2)
      const [first, second] = rows(wrapper)
      expect(first.get('.workspace-tile').text()).toBe('1')
      expect(first.get('.workspace-title').text()).toBe('personal')
      expect(first.get('.set-tag').text()).toBe('Main workspace')
      expect(first.get('.set-row-sub').text()).toBe('Anthropic (via Claude Code) · Workspace only · p@example.com')
      expect(second.get('.workspace-tile').text()).toBe('2')
      expect(second.find('.set-tag').exists()).toBe(false)
      expect(second.get('.set-row-sub').text()).toBe('Anthropic (via Claude Code) · Whole machine · no Google')
    } finally {
      wrapper.unmount()
    }
  })

  it('Settings link switches to that workspace and opens its Overview', async () => {
    const { wrapper, router, store } = await mountManage()
    try {
      await wrapper.get('[aria-label="Settings for workspace work"]').trigger('click')
      await flushPromises()
      expect(store.activeWorkspace).toBe('work')
      expect(router.currentRoute.value.path).toBe('/settings/workspace')
    } finally {
      wrapper.unmount()
    }
  })

  it('stays on the page when the switch is declined', async () => {
    const { wrapper, router, store } = await mountManage()
    try {
      vi.spyOn(store, 'switchWorkspace').mockResolvedValue(false)
      await wrapper.get('[aria-label="Settings for workspace work"]').trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.path).toBe('/settings/workspaces')
    } finally {
      wrapper.unmount()
    }
  })

  it('the menu offers only Rename on the main workspace', async () => {
    const { wrapper } = await mountManage()
    try {
      expect((await openMenu(rowFor(wrapper, 'personal'))).map(b => b.text())).toEqual(['Rename'])
    } finally {
      wrapper.unmount()
    }
  })

  it('the menu offers Rename and Archive on an archivable workspace', async () => {
    const { wrapper } = await mountManage()
    try {
      expect((await openMenu(rowFor(wrapper, 'work'))).map(b => b.text())).toEqual(['Rename', 'Archive'])
    } finally {
      wrapper.unmount()
    }
  })

  it('Rename switches workspace and focuses the name field', async () => {
    const { wrapper, router, store } = await mountManage()
    try {
      const rename = (await openMenu(rowFor(wrapper, 'work'))).find(b => b.text() === 'Rename')
      await rename!.trigger('click')
      await flushPromises()
      expect(store.activeWorkspace).toBe('work')
      expect(router.currentRoute.value.path).toBe('/settings/workspace')
      expect(router.currentRoute.value.hash).toBe('#workspace-name')
    } finally {
      wrapper.unmount()
    }
  })

  it('New workspace form is short', async () => {
    const { wrapper } = await mountManage()
    try {
      await wrapper.findAll('button').find(b => b.text() === 'New workspace')!.trigger('click')
      await flushPromises()
      const panel = wrapper.get('#new-workspace')
      const labels = panel.findAll('.ws-label').map(l => l.text())
      expect(labels).toEqual(['Name', 'Accent color', 'Agent CLI/Runtime', 'File access'])
      expect(panel.text()).toContain("new workspace's Overview")
      expect(panel.findAll('button').map(b => b.text())).toContain('Create and open')
    } finally {
      wrapper.unmount()
    }
  })

  it('Create and open sends vault_root from the name, switches to it and opens Overview', async () => {
    const { api } = await import('../../lib/api')
    const { wrapper, router, store } = await mountManage()
    try {
      vi.mocked(api.post).mockImplementation((path: string) => {
        if (path === '/api/workspaces') {
          state.workspaces = registry(['personal', 'work', 'side'])
        }
        return Promise.resolve({})
      })
      await wrapper.findAll('button').find(b => b.text() === 'New workspace')!.trigger('click')
      await flushPromises()
      const panel = wrapper.get('#new-workspace')
      await panel.get('input').setValue('side')
      await panel.findAll('button').find(b => b.text() === 'Create and open')!.trigger('click')
      await flushPromises()

      const call = vi.mocked(api.post).mock.calls.find(c => c[0] === '/api/workspaces')
      const body = call?.[1] as Record<string, unknown>
      expect(body).toMatchObject({ name: 'side', vault_root: 'side', gws_profile: '' })
      expect(body).not.toHaveProperty('agent_fs_scope')
      expect(store.activeWorkspace).toBe('side')
      expect(router.currentRoute.value.path).toBe('/settings/workspace')
    } finally {
      wrapper.unmount()
    }
  })

  it('a declined switch after a create says the workspace was created and the draft kept', async () => {
    const { api } = await import('../../lib/api')
    const { wrapper, router, store } = await mountManage()
    try {
      vi.mocked(api.post).mockImplementation((path: string) => {
        if (path === '/api/workspaces') state.workspaces = registry(['personal', 'work', 'side'])
        return Promise.resolve({})
      })
      vi.spyOn(store, 'switchWorkspace').mockResolvedValue(false)
      await wrapper.findAll('button').find(b => b.text() === 'New workspace')!.trigger('click')
      await flushPromises()
      const panel = wrapper.get('#new-workspace')
      await panel.get('input').setValue('side')
      await panel.findAll('button').find(b => b.text() === 'Create and open')!.trigger('click')
      await flushPromises()

      expect(router.currentRoute.value.path).toBe('/settings/workspaces')
      expect(wrapper.get('.workspace-link-status').text())
        .toBe('Created "side"; your unsaved changes to "personal" were kept.')
      expect(wrapper.find('.action-result--error').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('a failed create stays on the page and shows the error', async () => {
    const { api } = await import('../../lib/api')
    const { wrapper, router, store } = await mountManage()
    try {
      const before = store.activeWorkspace
      vi.mocked(api.post).mockRejectedValue(new Error('name taken'))
      await wrapper.findAll('button').find(b => b.text() === 'New workspace')!.trigger('click')
      await flushPromises()
      const panel = wrapper.get('#new-workspace')
      await panel.get('input').setValue('side')
      await panel.findAll('button').find(b => b.text() === 'Create and open')!.trigger('click')
      await flushPromises()

      expect(router.currentRoute.value.path).toBe('/settings/workspaces')
      expect(wrapper.get('.action-result--error').text()).toContain('name taken')
      expect(store.activeWorkspace).toBe(before)
    } finally {
      wrapper.unmount()
    }
  })

  it('Archived workspaces and the Google Workspace card remain', async () => {
    const { wrapper } = await mountManage()
    try {
      expect(wrapper.text()).toContain('Archived workspaces')
      expect(wrapper.findAll('.section-title').map(t => t.text())).toContain('Google Workspace')
    } finally {
      wrapper.unmount()
    }
  })

  it('#new-workspace opens and focuses the form', async () => {
    const { wrapper } = await mountManage('/settings/workspaces#new-workspace')
    try {
      await flushPromises()
      await nextTick()
      const form = wrapper.get('.workspace-card--new')
      expect(document.activeElement).toBe(form.get('input').element)
    } finally {
      wrapper.unmount()
    }
  })
})
