// @vitest-environment jsdom

// Settings > Workspaces: the "File access" select sets the workspace's
// agent_fs_scope. Edit saves the chosen value. A new workspace starts on
// "Default for this computer" and omits agent_fs_scope on create unless the
// user picks one, so the server applies its platform default.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount, type DOMWrapper } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { WorkspacesResponse } from '../../lib/types'

const state = vi.hoisted(() => ({
  workspaces: null as unknown,
}))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') return Promise.resolve(state.workspaces)
    if (path === '/api/workspaces/archived') return Promise.resolve({ archived: [] })
    if (path === '/api/integrations/gws') {
      return Promise.resolve({
        installed: false,
        binary_path: '',
        default_profile: '',
        cli_available: false,
        profiles: [],
      })
    }
    return Promise.resolve({})
  })
  return {
    api: {
      get,
      post: vi.fn(() => Promise.resolve(state.workspaces)),
      patch: vi.fn(() => Promise.resolve(state.workspaces)),
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

function workspaces(): WorkspacesResponse {
  return {
    workspaces: [
      {
        name: 'personal',
        vault_root: 'personal/memory-vault',
        default_provider: 'claude',
        gws_profile: '',
        color: 'pink',
        agent_fs_scope: 'workspace',
      },
    ],
    active: 'personal',
    primary: 'personal',
    provider_options: [{ value: 'claude', label: 'Claude' }],
  }
}

async function mountWorkspacesTab() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push('/settings/workspaces')
  await router.isReady()
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

// The File access select is the only select inside a field labelled "File access".
function fileAccessSelect(root: { findAll: (selector: string) => DOMWrapper<Element>[] }) {
  const field = root.findAll('label.settings-field').find(l => l.text().includes('File access'))
  expect(field, 'no File access field').toBeTruthy()
  return field!.find('select')
}

describe('Settings > Workspaces file access', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    state.workspaces = workspaces()
    const { api } = await import('../../lib/api')
    vi.mocked(api.post).mockClear()
    vi.mocked(api.patch).mockClear()
  })

  it('saves workspace or machine from the edit panel', async () => {
    const { api } = await import('../../lib/api')
    const wrapper = await mountWorkspacesTab()
    try {
      await wrapper.get('[aria-label="Edit workspace personal"]').trigger('click')
      await flushPromises()

      const select = fileAccessSelect(wrapper)
      expect(select.element.value).toBe('workspace')
      expect(select.findAll('option').map(o => o.text())).toEqual(['Workspace only', 'Whole machine'])
      expect(wrapper.text()).toContain('If this computer cannot sandbox')

      await select.setValue('machine')
      await flushPromises()
      await wrapper.get('.workspace-save').trigger('click')
      await flushPromises()

      expect(api.patch).toHaveBeenCalledWith(
        '/api/workspaces/personal',
        expect.objectContaining({ agent_fs_scope: 'machine' }),
      )
    } finally {
      wrapper.unmount()
    }
  })

  it('creates a new workspace with the server default unless a scope is picked', async () => {
    const { api } = await import('../../lib/api')
    const wrapper = await mountWorkspacesTab()
    try {
      const addButton = wrapper.findAll('button').find(b => b.text() === 'New workspace')
      expect(addButton).toBeTruthy()
      await addButton!.trigger('click')
      await flushPromises()

      const newPanel = wrapper.get('#new-workspace')
      const select = fileAccessSelect(newPanel)
      expect(select.element.value).toBe('')
      expect(select.findAll('option').map(o => o.text())).toEqual([
        'Default for this computer',
        'Workspace only',
        'Whole machine',
      ])
      expect(newPanel.text()).toContain('whole machine on Windows')

      await newPanel.get('input[placeholder="letters, numbers, dashes, underscores"]').setValue('client-a')
      const create = wrapper.findAll('button').find(b => b.text() === 'Create workspace')
      await create!.trigger('click')
      await flushPromises()

      const payload = vi.mocked(api.post).mock.calls.find(c => c[0] === '/api/workspaces')?.[1] as Record<string, unknown>
      expect(payload).toMatchObject({ name: 'client-a' })
      expect(payload).not.toHaveProperty('agent_fs_scope')
    } finally {
      wrapper.unmount()
    }
  })

  it('sends an explicitly picked file access on create', async () => {
    const { api } = await import('../../lib/api')
    const wrapper = await mountWorkspacesTab()
    try {
      const addButton = wrapper.findAll('button').find(b => b.text() === 'New workspace')
      await addButton!.trigger('click')
      await flushPromises()

      const newPanel = wrapper.get('#new-workspace')
      await fileAccessSelect(newPanel).setValue('workspace')
      await newPanel.get('input[placeholder="letters, numbers, dashes, underscores"]').setValue('client-b')
      const create = wrapper.findAll('button').find(b => b.text() === 'Create workspace')
      await create!.trigger('click')
      await flushPromises()

      expect(api.post).toHaveBeenCalledWith(
        '/api/workspaces',
        expect.objectContaining({ name: 'client-b', agent_fs_scope: 'workspace' }),
      )
    } finally {
      wrapper.unmount()
    }
  })
})
