// @vitest-environment jsdom

// Settings > Workspaces: each row shows its effective File access at a glance.
// An explicit choice shows as chosen; a workspace with no choice shows the
// engine's default_agent_fs_scope.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { WorkspacesResponse } from '../../lib/types'

const state = vi.hoisted(() => ({ workspaces: null as unknown }))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') return Promise.resolve(state.workspaces)
    if (path === '/api/workspaces/archived') return Promise.resolve({ archived: [] })
    if (path === '/api/integrations/gws') {
      return Promise.resolve({ installed: false, binary_path: '', default_profile: '', cli_available: false, profiles: [] })
    }
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

function response(defaultScope: 'workspace' | 'machine'): WorkspacesResponse {
  return {
    workspaces: [
      { name: 'explicit', vault_root: 'explicit/memory-vault', default_provider: 'claude', gws_profile: '', color: 'pink', agent_fs_scope: 'machine' },
      { name: 'defaulted', vault_root: 'defaulted/memory-vault', default_provider: 'claude', gws_profile: '', color: 'pink', agent_fs_scope: null },
    ],
    active: 'explicit',
    primary: 'explicit',
    default_agent_fs_scope: defaultScope,
    provider_options: [{ value: 'claude', label: 'Claude' }],
  } as WorkspacesResponse
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

function cardFor(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>, name: string) {
  const card = wrapper.findAll('.workspace-list .workspace-card')
    .find(c => c.find('.workspace-title').text() === name)
  expect(card, `no card for ${name}`).toBeTruthy()
  return card!
}

describe('Settings > Workspaces File access badge on the row', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows the explicit choice on a workspace that set one', async () => {
    state.workspaces = response('workspace')
    const wrapper = await mountWorkspacesTab()
    try {
      const badge = cardFor(wrapper, 'explicit').find('.workspace-fs-tag')
      expect(badge.exists()).toBe(true)
      expect(badge.text()).toBe('Whole machine')
    } finally {
      wrapper.unmount()
    }
  })

  it('shows the engine default on a workspace with no choice', async () => {
    state.workspaces = response('workspace')
    const wrapper = await mountWorkspacesTab()
    try {
      expect(cardFor(wrapper, 'defaulted').find('.workspace-fs-tag').text()).toBe('Workspace only')
    } finally {
      wrapper.unmount()
    }
  })

  it('follows the engine default when that default is whole machine', async () => {
    state.workspaces = response('machine')
    const wrapper = await mountWorkspacesTab()
    try {
      expect(cardFor(wrapper, 'defaulted').find('.workspace-fs-tag').text()).toBe('Whole machine')
      // An explicit choice is shown as chosen, whatever the default is.
      expect(cardFor(wrapper, 'explicit').find('.workspace-fs-tag').text()).toBe('Whole machine')
    } finally {
      wrapper.unmount()
    }
  })
})
