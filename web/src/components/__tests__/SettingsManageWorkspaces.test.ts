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
    if (path === '/api/settings/routines') return Promise.resolve({ model_options: { anthropic: [] } })
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

  it('the menu offers no Archive on the main workspace', async () => {
    const { wrapper } = await mountManage()
    try {
      expect((await openMenu(rowFor(wrapper, 'personal'))).map(b => b.text())).toEqual(['Rename', 'Move up', 'Move down'])
    } finally {
      wrapper.unmount()
    }
  })

  it('the menu offers Rename and Archive on an archivable workspace', async () => {
    const { wrapper } = await mountManage()
    try {
      expect((await openMenu(rowFor(wrapper, 'work'))).map(b => b.text())).toEqual(['Rename', 'Move up', 'Move down', 'Archive'])
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

describe('Settings > Manage workspaces > reorder', () => {
  beforeEach(async () => {
    localStorage.clear()
    document.body.innerHTML = ''
    setActivePinia(createPinia())
    state.workspaces = registry(['personal', 'work', 'side'])
    const { api } = await import('../../lib/api')
    vi.mocked(api.post).mockReset()
    vi.mocked(api.post).mockImplementation((path: string, body?: unknown) => {
      if (path === '/api/workspaces/reorder') {
        const order = (body as { order: string[] }).order
        state.workspaces = registry(order)
        return Promise.resolve(state.workspaces)
      }
      return Promise.resolve({})
    })
  })

  const order = (wrapper: Mounted['wrapper']) =>
    rows(wrapper).map(r => r.get('.workspace-title').text())
  const handle = (wrapper: Mounted['wrapper'], name: string) =>
    wrapper.get(`[data-workspace-handle="${name}"]`)
  const live = (wrapper: Mounted['wrapper']) => wrapper.get('[role="status"][aria-live="polite"]')

  it('Alt+ArrowDown on a handle moves the row down, announces it and keeps focus', async () => {
    const { wrapper, store } = await mountManage()
    try {
      const spy = vi.spyOn(store, 'reorderWorkspaces')
      const first = handle(wrapper, 'personal')
      ;(first.element as HTMLElement).focus()
      await first.trigger('keydown', { key: 'ArrowDown', altKey: true })
      await flushPromises()
      await nextTick()

      expect(spy).toHaveBeenCalledWith(['work', 'personal', 'side'])
      expect(order(wrapper)).toEqual(['work', 'personal', 'side'])
      expect(live(wrapper).text()).toBe('Moved Personal to position 2 of 3')
      expect(document.activeElement).toBe(handle(wrapper, 'personal').element)
    } finally {
      wrapper.unmount()
    }
  })

  it('Alt+ArrowUp on the first row and plain arrows do nothing', async () => {
    const { wrapper, store } = await mountManage()
    try {
      const spy = vi.spyOn(store, 'reorderWorkspaces')
      const first = handle(wrapper, 'personal')
      await first.trigger('keydown', { key: 'ArrowUp', altKey: true })
      await first.trigger('keydown', { key: 'ArrowDown' })
      await first.trigger('keydown', { key: 'ArrowUp' })
      await flushPromises()
      expect(spy).not.toHaveBeenCalled()
      expect(live(wrapper).text()).toBe('')
      const last = handle(wrapper, 'side')
      await last.trigger('keydown', { key: 'ArrowDown', altKey: true })
      expect(spy).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })

  it('the menu disables Move up on the first row and Move down on the last', async () => {
    const { wrapper } = await mountManage()
    try {
      const firstItems = await openMenu(rowFor(wrapper, 'personal'))
      const up = firstItems.find(b => b.text() === 'Move up')!
      const down = firstItems.find(b => b.text() === 'Move down')!
      expect(up.attributes('data-disabled')).toBeDefined()
      expect(down.attributes('data-disabled')).toBeUndefined()
      expect(up.element.closest('[role="menuitem"]')).toBeTruthy()
    } finally {
      wrapper.unmount()
    }
  })

  it('the menu disables Move down on the last row', async () => {
    const { wrapper } = await mountManage()
    try {
      const items = await openMenu(rowFor(wrapper, 'side'))
      expect(items.find(b => b.text() === 'Move down')!.attributes('data-disabled')).toBeDefined()
      expect(items.find(b => b.text() === 'Move up')!.attributes('data-disabled')).toBeUndefined()
    } finally {
      wrapper.unmount()
    }
  })

  it('menu Move down reorders, announces and returns focus to the handle', async () => {
    const { wrapper, store } = await mountManage()
    try {
      const spy = vi.spyOn(store, 'reorderWorkspaces')
      const items = await openMenu(rowFor(wrapper, 'work'))
      await items.find(b => b.text() === 'Move down')!.trigger('click')
      await flushPromises()
      await nextTick()

      expect(spy).toHaveBeenCalledWith(['personal', 'side', 'work'])
      expect(order(wrapper)).toEqual(['personal', 'side', 'work'])
      expect(live(wrapper).text()).toBe('Moved Work to position 3 of 3')
      expect(document.activeElement).toBe(handle(wrapper, 'work').element)
    } finally {
      wrapper.unmount()
    }
  })

  it('dropping the first row on the last posts the moved order', async () => {
    const { wrapper, store } = await mountManage()
    try {
      const spy = vi.spyOn(store, 'reorderWorkspaces')
      const dataTransfer = { setData: vi.fn(), setDragImage: vi.fn(), effectAllowed: '' }
      await handle(wrapper, 'personal').trigger('dragstart', { dataTransfer })
      await rowFor(wrapper, 'side').trigger('dragover', { dataTransfer })
      expect(rowFor(wrapper, 'side').classes()).toContain('workspace-row--drag-over')
      await rowFor(wrapper, 'side').trigger('drop', { dataTransfer })
      await flushPromises()

      expect(spy).toHaveBeenCalledWith(['work', 'side', 'personal'])
      expect(order(wrapper)).toEqual(['work', 'side', 'personal'])
      expect(live(wrapper).text()).toBe('Moved Personal to position 3 of 3')
      expect(wrapper.find('.workspace-row--drag-over').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('a rejected save shows the alert, restores the order and does not announce', async () => {
    const { api } = await import('../../lib/api')
    const { wrapper } = await mountManage()
    try {
      vi.mocked(api.post).mockRejectedValue(new Error('disk full'))
      await handle(wrapper, 'personal').trigger('keydown', { key: 'ArrowDown', altKey: true })
      await flushPromises()

      expect(wrapper.get('.action-result--error[role="alert"]').text())
        .toBe('Could not save the workspace order: disk full')
      expect(order(wrapper)).toEqual(['personal', 'work', 'side'])
      expect(live(wrapper).text()).toBe('')
    } finally {
      wrapper.unmount()
    }
  })

  it('each row shows the key its position maps to, and the shortcut follows a move', async () => {
    const { wrapper } = await mountManage()
    try {
      const keys = () => rows(wrapper).map(r => r.get('.workspace-row-key').text())
      expect(keys()).toEqual(['Key 1', 'Key 2', 'Key 3'])
      await handle(wrapper, 'personal').trigger('keydown', { key: 'ArrowDown', altKey: true })
      await flushPromises()
      expect(order(wrapper)).toEqual(['work', 'personal', 'side'])
      expect(keys()).toEqual(['Key 1', 'Key 2', 'Key 3'])
    } finally {
      wrapper.unmount()
    }
  })

  it('the main workspace tag follows the server after a move', async () => {
    const { api } = await import('../../lib/api')
    const { wrapper } = await mountManage()
    try {
      vi.mocked(api.post).mockImplementation((_path: string, body?: unknown) => {
        const names = (body as { order: string[] }).order
        return Promise.resolve({ ...registry(names), primary: names[0] })
      })
      await handle(wrapper, 'personal').trigger('keydown', { key: 'ArrowDown', altKey: true })
      await flushPromises()
      expect(rowFor(wrapper, 'work').find('.set-tag').exists()).toBe(true)
      expect(rowFor(wrapper, 'personal').find('.set-tag').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('archived rows have no handle', async () => {
    const { api } = await import('../../lib/api')
    const base = vi.mocked(api.get).getMockImplementation()!
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/api/workspaces/archived') {
        return Promise.resolve({
          archived: [{ id: 'old-1', name: 'old', archived_at: '2026-01-01T00:00:00Z', path: '/x', restorable: true }],
        })
      }
      return base(path)
    })
    const { wrapper } = await mountManage()
    try {
      expect(wrapper.findAll('.archived-item')).toHaveLength(1)
      expect(wrapper.findAll('[data-workspace-handle]')).toHaveLength(3)
      expect(wrapper.find('.archived-item [data-workspace-handle]').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('a failed move is rolled back before the queued next move is computed', async () => {
    const { api } = await import('../../lib/api')
    const { wrapper } = await mountManage()
    try {
      const bodies: string[][] = []
      vi.mocked(api.post).mockImplementation((_path: string, body?: unknown) => {
        const names = (body as { order: string[] }).order
        bodies.push(names)
        if (bodies.length === 1) return Promise.reject(new Error('nope'))
        state.workspaces = registry(names)
        return Promise.resolve(state.workspaces)
      })
      await handle(wrapper, 'personal').trigger('keydown', { key: 'ArrowDown', altKey: true })
      await handle(wrapper, 'side').trigger('keydown', { key: 'ArrowUp', altKey: true })
      await flushPromises()

      expect(bodies).toEqual([['work', 'personal', 'side'], ['personal', 'side', 'work']])
      expect(order(wrapper)).toEqual(['personal', 'side', 'work'])
    } finally {
      wrapper.unmount()
    }
  })

  it('a successful move keeps an unrelated error on the page', async () => {
    const { wrapper } = await mountManage()
    try {
      const admin = wrapper.findComponent({ name: 'SettingsManageWorkspaces' })
      expect(admin.exists()).toBe(true)
      ;(admin.vm as unknown as { workspacesResult: string }).workspacesResult = 'Error: something else'
      await nextTick()
      await handle(wrapper, 'personal').trigger('keydown', { key: 'ArrowDown', altKey: true })
      await flushPromises()
      expect(wrapper.get('.action-result--error').text()).toBe('Error: something else')
    } finally {
      wrapper.unmount()
    }
  })

  describe('with a main workspace that is not named personal', () => {
    beforeEach(() => {
      state.workspaces = { ...registry(['work', 'side', 'other']), primary: 'work' }
    })

    it('pins the main row first: spacer instead of a handle, no move items', async () => {
      const { wrapper } = await mountManage()
      try {
        expect(order(wrapper)).toEqual(['work', 'side', 'other'])
        expect(wrapper.find('[data-workspace-handle="work"]').exists()).toBe(false)
        expect(rowFor(wrapper, 'work').find('.workspace-row-handle--spacer').exists()).toBe(true)
        expect(wrapper.findAll('[data-workspace-handle]')).toHaveLength(2)
        const items = await openMenu(rowFor(wrapper, 'work'))
        expect(items.map(b => b.text())).toEqual(['Rename'])
      } finally {
        wrapper.unmount()
      }
    })

    it('cannot move the next row above it', async () => {
      const { wrapper, store } = await mountManage()
      try {
        const spy = vi.spyOn(store, 'reorderWorkspaces')
        await handle(wrapper, 'side').trigger('keydown', { key: 'ArrowUp', altKey: true })
        await flushPromises()
        expect(spy).not.toHaveBeenCalled()
        const items = await openMenu(rowFor(wrapper, 'side'))
        expect(items.find(b => b.text() === 'Move up')!.attributes('data-disabled')).toBeDefined()
        expect(items.find(b => b.text() === 'Move down')!.attributes('data-disabled')).toBeUndefined()
      } finally {
        wrapper.unmount()
      }
    })

    it('refuses a drop onto the pinned row and allows one below it', async () => {
      const { wrapper, store } = await mountManage()
      try {
        const spy = vi.spyOn(store, 'reorderWorkspaces')
        const dataTransfer = { setData: vi.fn(), setDragImage: vi.fn(), effectAllowed: '' }
        await handle(wrapper, 'other').trigger('dragstart', { dataTransfer })
        await rowFor(wrapper, 'work').trigger('dragover', { dataTransfer })
        expect(wrapper.find('.workspace-row--drag-over').exists()).toBe(false)
        await rowFor(wrapper, 'work').trigger('drop', { dataTransfer })
        await flushPromises()
        expect(spy).not.toHaveBeenCalled()

        await handle(wrapper, 'other').trigger('dragstart', { dataTransfer })
        await rowFor(wrapper, 'side').trigger('drop', { dataTransfer })
        await flushPromises()
        expect(spy).toHaveBeenCalledWith(['work', 'other', 'side'])
      } finally {
        wrapper.unmount()
      }
    })
  })

  it('pins nothing when a workspace is named personal', async () => {
    const { wrapper } = await mountManage()
    try {
      expect(wrapper.findAll('[data-workspace-handle]')).toHaveLength(3)
      expect(wrapper.find('.workspace-row-handle--spacer').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })
})
