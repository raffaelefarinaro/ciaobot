// @vitest-environment jsdom

// Settings > All workspaces: each row's summary line shows its effective
// File access. An explicit choice shows as chosen; a workspace with no choice
// shows the engine's default_agent_fs_scope.

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
      return Promise.resolve({ installed: false, binary_path: '', cli_available: false, profiles: [] })
    }
    // The merged page also renders the active workspace's Overview cards.
    if (path === '/api/settings/routines') return Promise.resolve({ model_options: { anthropic: [] } })
    if (path.startsWith('/api/proposals/handover')) {
      return Promise.resolve({
        workspace: 'explicit',
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
  await router.push('/settings/workspace')
  await router.isReady()
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

function summaryFor(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>, name: string): string {
  return cardFor(wrapper, name).find('.set-row-sub').text()
}

function cardFor(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>, name: string) {
  const card = wrapper.findAll('.workspace-list .workspace-card')
    .find(c => c.find('.workspace-title').text() === name)
  expect(card, `no card for ${name}`).toBeTruthy()
  return card!
}

describe('Settings > All workspaces summary shows file access', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows the explicit choice on a workspace that set one', async () => {
    state.workspaces = response('workspace')
    const wrapper = await mountWorkspacesTab()
    try {
      expect(cardFor(wrapper, 'explicit').find('.workspace-fs-tag').exists()).toBe(false)
      expect(summaryFor(wrapper, 'explicit')).toBe('Claude · Whole machine · no Google')
    } finally {
      wrapper.unmount()
    }
  })

  it('shows the engine default on a workspace with no choice', async () => {
    state.workspaces = response('workspace')
    const wrapper = await mountWorkspacesTab()
    try {
      expect(summaryFor(wrapper, 'defaulted')).toBe('Claude · Workspace only · no Google')
    } finally {
      wrapper.unmount()
    }
  })

  it('follows the engine default when that default is whole machine', async () => {
    state.workspaces = response('machine')
    const wrapper = await mountWorkspacesTab()
    try {
      expect(summaryFor(wrapper, 'defaulted')).toBe('Claude · Whole machine · no Google')
      // An explicit choice is shown as chosen, whatever the default is.
      expect(summaryFor(wrapper, 'explicit')).toBe('Claude · Whole machine · no Google')
    } finally {
      wrapper.unmount()
    }
  })
})
