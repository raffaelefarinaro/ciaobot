// @vitest-environment jsdom

// Settings > Workspaces rows are a list: name, badge, summary, a Settings link
// to the Overview page and the Archive menu. There is no inline editor.

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
    if (path === '/api/integrations/gws') {
      return Promise.resolve({ installed: false, binary_path: '', cli_available: false, profiles: [] })
    }
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

function workspaces(names: string[]): WorkspacesResponse {
  return {
    workspaces: names.map(name => ({
      name,
      vault_root: `${name}/memory-vault`,
      default_provider: 'claude',
      gws_profile: '',
      color: 'pink',
    })),
    active: names[0] ?? null,
    primary: names[0] ?? null,
    default_agent_fs_scope: 'workspace',
    provider_options: [{ value: 'claude', label: 'Claude' }],
  }
}

describe('Settings > Workspaces rows', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    state.workspaces = workspaces(['personal', 'work'])
  })

  it('Settings link switches workspace and opens Overview', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
    })
    await router.push('/settings/workspace')
    await router.isReady()
    const { default: SettingsView } = await import('../SettingsView.vue')
    const wrapper = mount(SettingsView, {
      global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
    })
    await flushPromises()
    await nextTick()
    try {
      const store = useProjectStore()
      expect(store.activeWorkspace).toBe('personal')
      // Rows carry no inline editor and no Edit button.
      expect(wrapper.findAll('button').some(b => b.text() === 'Edit')).toBe(false)
      expect(wrapper.find('[aria-label^="Edit workspace"]').exists()).toBe(false)
      expect(wrapper.find('.set-row-body').exists()).toBe(false)

      await wrapper.get('[aria-label="Settings for workspace work"]').trigger('click')
      await flushPromises()

      expect(store.activeWorkspace).toBe('work')
      expect(router.currentRoute.value.path).toBe('/settings/workspace')
      expect((wrapper.get('#workspace-name-input').element as HTMLInputElement).value).toBe('work')
    } finally {
      wrapper.unmount()
    }
  })
})
