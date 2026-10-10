// @vitest-environment jsdom

// Settings > Workspaces: the "claude.ai connectors" switch on the edit panel
// sets the workspace's claude_ai_connectors. It reads On for a workspace that
// never set it, and an edit that leaves it alone does not change it.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
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

function workspaces(connectors?: boolean): WorkspacesResponse {
  const entry: Record<string, unknown> = {
    name: 'personal',
    vault_root: 'personal/memory-vault',
    default_provider: 'claude',
    gws_profile: '',
    color: 'pink',
    agent_fs_scope: null,
  }
  if (connectors !== undefined) entry.claude_ai_connectors = connectors
  return {
    workspaces: [entry],
    active: 'personal',
    primary: 'personal',
    provider_options: [{ value: 'claude', label: 'Claude' }],
    default_agent_fs_scope: 'workspace',
  } as unknown as WorkspacesResponse
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

function connectorsSwitch(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>) {
  const row = wrapper.findAll('.settings-switch-row').find(r => r.text().includes('claude.ai connectors'))
  expect(row, 'no claude.ai connectors row').toBeTruthy()
  return row!.get('button[role="switch"]')
}

describe('Settings > Workspaces claude.ai connectors switch', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    const { api } = await import('../../lib/api')
    vi.mocked(api.patch).mockClear()
  })

  it('reads On for a workspace that never set it, and says what it does', async () => {
    state.workspaces = workspaces()
    const wrapper = await mountWorkspacesTab()
    try {
      await wrapper.get('[aria-label="Edit workspace personal"]').trigger('click')
      await flushPromises()

      const toggle = connectorsSwitch(wrapper)
      expect(toggle.attributes('aria-checked')).toBe('true')
      expect(toggle.text()).toBe('On')
      expect(wrapper.text()).toContain("Load your Claude account's connectors (Slack, Jira, …) in this workspace's Claude chats.")
    } finally {
      wrapper.unmount()
    }
  })

  it('reads Off for a workspace that turned it off', async () => {
    state.workspaces = workspaces(false)
    const wrapper = await mountWorkspacesTab()
    try {
      await wrapper.get('[aria-label="Edit workspace personal"]').trigger('click')
      await flushPromises()
      expect(connectorsSwitch(wrapper).attributes('aria-checked')).toBe('false')
    } finally {
      wrapper.unmount()
    }
  })

  it('saves the switch from the edit panel', async () => {
    const { api } = await import('../../lib/api')
    state.workspaces = workspaces(true)
    const wrapper = await mountWorkspacesTab()
    try {
      await wrapper.get('[aria-label="Edit workspace personal"]').trigger('click')
      await flushPromises()

      await connectorsSwitch(wrapper).trigger('click')
      await flushPromises()
      expect(connectorsSwitch(wrapper).attributes('aria-checked')).toBe('false')

      await wrapper.get('.workspace-save').trigger('click')
      await flushPromises()

      expect(api.patch).toHaveBeenCalledWith(
        '/api/workspaces/personal',
        expect.objectContaining({ claude_ai_connectors: false }),
      )
    } finally {
      wrapper.unmount()
    }
  })
})
