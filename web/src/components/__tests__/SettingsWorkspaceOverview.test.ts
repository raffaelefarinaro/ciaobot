// @vitest-environment jsdom

// Settings > Workspace > Overview: the page for the active workspace. It draws
// the editor useWorkspaceAdmin owns, follows the sidebar's active workspace and
// drops a draft when the workspace changes.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { WorkspacesResponse } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { workspaceLabel } from '../../lib/workspaceLabel'

const state = vi.hoisted(() => ({ workspaces: null as unknown }))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') return Promise.resolve(state.workspaces)
    if (path === '/api/workspaces/archived') return Promise.resolve({ archived: [] })
    if (path === '/api/integrations/gws') {
      return Promise.resolve({ installed: false, binary_path: '', cli_available: false, profiles: [] })
    }
    if (path.startsWith('/api/proposals/handover')) {
      const workspace = decodeURIComponent(path.split('workspace=')[1] ?? '')
      return Promise.resolve({
        workspace,
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
    // The Overview's Chats and models card reads the engine defaults.
    if (path === '/api/settings/routines') return Promise.resolve({ model_options: { anthropic: [] } })
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

function workspaces(names: string[], primary = 'personal'): WorkspacesResponse {
  return {
    workspaces: names.map(name => ({
      name,
      vault_root: `${name}/memory-vault`,
      default_provider: 'claude',
      gws_profile: '',
      color: 'pink',
    })),
    active: names[0] ?? null,
    primary,
    default_agent_fs_scope: 'workspace',
    provider_options: [{ value: 'claude', label: 'Claude' }],
  }
}

async function mountOverview(active = 'work') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push('/settings/workspace')
  await router.isReady()
  const store = useProjectStore()
  store.activeWorkspace = active
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  await nextTick()
  return { wrapper, store }
}

function nameValue(wrapper: Awaited<ReturnType<typeof mountOverview>>['wrapper']): string {
  return (wrapper.get('#workspace-name-input').element as HTMLInputElement).value
}

describe('Settings > Workspace Overview', () => {
  beforeEach(async () => {
    localStorage.clear()
    document.body.innerHTML = ''
    setActivePinia(createPinia())
    state.workspaces = workspaces(['personal', 'work'])
    const { api } = await import('../../lib/api')
    vi.mocked(api.get).mockClear()
    vi.mocked(api.patch).mockClear()
  })

  it('shows the active workspace and its cards in order', async () => {
    const { wrapper } = await mountOverview('work')
    try {
      const titles = wrapper.findAll('.section-title').map(el => el.text())
      expect(titles).toEqual([
        'Workspace',
        'Chats and models',
        'Google account',
        'Memory handover',
        'Update task history',
        'Archive this workspace',
      ])
      expect(nameValue(wrapper)).toBe('work')
      expect(wrapper.find('.workspace-tile').exists()).toBe(true)
    } finally {
      wrapper.unmount()
    }
  })

  it('follows a workspace switch', async () => {
    const { api } = await import('../../lib/api')
    const { wrapper, store } = await mountOverview('work')
    try {
      store.activeWorkspace = 'personal'
      await flushPromises()
      await nextTick()

      expect(nameValue(wrapper)).toBe('personal')
      expect(vi.mocked(api.get)).toHaveBeenCalledWith('/api/proposals/handover?workspace=personal')
      // personal is the primary workspace: it cannot be archived.
      expect(wrapper.findAll('.section-title').map(el => el.text())).not.toContain('Archive this workspace')
    } finally {
      wrapper.unmount()
    }
  })

  it('hides Archive for the last workspace', async () => {
    state.workspaces = workspaces(['solo'], 'solo')
    const { wrapper } = await mountOverview('solo')
    try {
      expect(wrapper.findAll('.section-title').map(el => el.text())).not.toContain('Archive this workspace')
    } finally {
      wrapper.unmount()
    }
  })

  it("a switch discards the previous workspace's draft", async () => {
    const { wrapper, store } = await mountOverview('work')
    try {
      await wrapper.get('#workspace-name-input').setValue('work-renamed')
      expect(wrapper.find('.workspace-save').exists()).toBe(true)

      store.activeWorkspace = 'personal'
      await flushPromises()
      expect(nameValue(wrapper)).toBe('personal')

      store.activeWorkspace = 'work'
      await flushPromises()
      expect(nameValue(wrapper)).toBe('work')
      expect(wrapper.find('.workspace-save').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('a declined discard keeps the workspace and the draft, and tells the caller', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    const { wrapper, store } = await mountOverview('work')
    try {
      await wrapper.get('#workspace-name-input').setValue('work-renamed')
      const switched = store.switchWorkspace('personal', { transition: false })
      await flushPromises()
      expect(pendingConfirm.value!.message).toBe('Discard unsaved changes to work?')
      pendingConfirm.value!.resolve(false)

      expect(await switched).toBe(false)
      expect(store.activeWorkspace).toBe('work')
      expect(nameValue(wrapper)).toBe('work-renamed')
    } finally {
      wrapper.unmount()
    }
  })

  it('pane header reads "<Workspace> · Overview"', async () => {
    const { wrapper } = await mountOverview('work')
    try {
      expect(wrapper.get('.page-tag').text()).toBe(`${workspaceLabel('work')} · Overview`)
    } finally {
      wrapper.unmount()
    }
  })

  it('Save changes and Discard only while dirty, never Close', async () => {
    const { wrapper } = await mountOverview('work')
    try {
      expect(wrapper.find('.workspace-save').exists()).toBe(false)
      expect(wrapper.find('.workspace-discard').exists()).toBe(false)
      expect(wrapper.find('.workspace-close').exists()).toBe(false)

      const other = wrapper.findAll('.workspace-color-swatch').find(b => b.attributes('aria-checked') === 'false')
      await other!.trigger('click')
      await nextTick()
      expect(wrapper.findAll('.workspace-save')).toHaveLength(1)
      expect(wrapper.findAll('.workspace-discard')).toHaveLength(1)
      expect(wrapper.find('.workspace-close').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })
})
