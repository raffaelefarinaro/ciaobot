// @vitest-environment jsdom

// Settings > Workspaces: "Archive" replaced "Delete". Archiving keeps the
// workspace's files and never merges them into another workspace, so the
// wording must say so, the primary/last workspace must not offer the action,
// and archived workspaces must be listed with a working Restore.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { ArchivedWorkspace, WorkspacesResponse } from '../../lib/types'
import { archiveConfirmMessage } from '../../lib/workspaceArchive'

const state = vi.hoisted(() => ({
  workspaces: null as unknown,
  archived: [] as unknown[],
  archivedFails: false,
}))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') return Promise.resolve(state.workspaces)
    if (path === '/api/workspaces/archived') {
      return state.archivedFails
        ? Promise.reject(new Error('HTTP 500'))
        : Promise.resolve({ archived: state.archived })
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
    provider_options: [{ value: 'claude', label: 'Claude' }],
  }
}

function archived(overrides: Partial<ArchivedWorkspace> = {}): ArchivedWorkspace {
  return {
    id: 'work-20260923-101500',
    name: 'work',
    archived_at: '2026-09-23T10:15:00Z',
    path: '.archived-workspaces/work-20260923-101500',
    layout: 'per-root',
    color: 'cyan',
    default_provider: 'claude',
    gws_profile: 'work',
    schedules: 1,
    restorable: true,
    blocked_reason: '',
    ...overrides,
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

function cardFor(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>, name: string) {
  const card = wrapper.findAll('.workspace-list .workspace-card')
    .find(c => c.find('.workspace-title').text() === name)
  expect(card, `no card for ${name}`).toBeTruthy()
  return card!
}

describe('Settings > Workspaces archive', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    state.workspaces = workspaces(['personal', 'work'])
    state.archived = []
    state.archivedFails = false
    const { api } = await import('../../lib/api')
    vi.mocked(api.post).mockClear()
    vi.mocked(api.del).mockClear()
  })

  it('offers Archive, not Delete, and never on the primary workspace', async () => {
    const wrapper = await mountWorkspacesTab()
    try {
      const work = cardFor(wrapper, 'work')
      const labels = work.findAll('.workspace-actions button').map(b => b.text())
      expect(labels).toContain('Archive')
      expect(labels).not.toContain('Delete')
      const personal = cardFor(wrapper, 'personal')
      expect(personal.findAll('.workspace-actions button').map(b => b.text()))
        .not.toContain('Archive')
    } finally {
      wrapper.unmount()
    }
  })

  it('does not offer Archive on the last workspace', async () => {
    state.workspaces = workspaces(['solo'], 'solo')
    const wrapper = await mountWorkspacesTab()
    try {
      expect(cardFor(wrapper, 'solo').findAll('.workspace-actions button').map(b => b.text()))
        .not.toContain('Archive')
    } finally {
      wrapper.unmount()
    }
  })

  it('confirms with accurate wording and posts to the archive route', async () => {
    const { api } = await import('../../lib/api')
    const { pendingConfirm } = await import('../../lib/confirm')
    vi.mocked(api.post).mockImplementation((path: string) => {
      if (path === '/api/workspaces/work/archive') {
        return Promise.resolve({
          ...workspaces(['personal']),
          archived: { path: '.archived-workspaces/work-20260923-101500' },
        })
      }
      return Promise.resolve({})
    })
    const wrapper = await mountWorkspacesTab()
    try {
      const button = cardFor(wrapper, 'work').findAll('.workspace-actions button')
        .find(b => b.text() === 'Archive')!
      expect(button.attributes('aria-label')).toBe('Archive workspace work')
      await button.trigger('click')
      await nextTick()

      const request = pendingConfirm.value
      expect(request).toBeTruthy()
      expect(request!.title).toBe('Archive workspace')
      expect(request!.confirmLabel).toBe('Archive workspace')
      expect(request!.destructive).toBe(false)
      expect(request!.message).toBe(archiveConfirmMessage('work'))
      expect(request!.message).toContain('stops using its notes and memory')
      expect(request!.message).toContain('.archived-workspaces/')
      expect(request!.message).toContain('restore it from Settings → Workspaces')
      expect(request!.message).not.toMatch(/delete/i)

      state.workspaces = workspaces(['personal'])
      state.archived = [archived()]
      request!.resolve(true)
      await flushPromises()

      expect(api.post).toHaveBeenCalledWith('/api/workspaces/work/archive')
      expect(api.del).not.toHaveBeenCalled()
      expect(wrapper.findAll('.workspace-list .workspace-card').map(c => c.find('.workspace-title').text()))
        .toEqual(['personal'])
      expect(wrapper.find('.archived-item').text()).toContain('work')
    } finally {
      wrapper.unmount()
      vi.mocked(api.post).mockReset()
      vi.mocked(api.post).mockImplementation(() => Promise.resolve({}))
    }
  })

  it('cancelling the confirmation archives nothing', async () => {
    const { api } = await import('../../lib/api')
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountWorkspacesTab()
    try {
      await cardFor(wrapper, 'work').findAll('.workspace-actions button')
        .find(b => b.text() === 'Archive')!.trigger('click')
      await nextTick()
      pendingConfirm.value!.resolve(false)
      await flushPromises()
      expect(api.post).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })

  it('lists archived workspaces and restores one', async () => {
    const { api } = await import('../../lib/api')
    state.archived = [archived()]
    vi.mocked(api.post).mockImplementation((path: string) => {
      if (path === '/api/workspaces/archived/restore') {
        return Promise.resolve(workspaces(['personal', 'work']))
      }
      return Promise.resolve({})
    })
    const wrapper = await mountWorkspacesTab()
    try {
      const item = wrapper.find('.archived-item')
      expect(item.find('.workspace-title').text()).toBe('work')
      expect(item.text()).toContain('.archived-workspaces/work-20260923-101500')
      const restore = item.find('button')
      expect(restore.text()).toBe('Restore')
      expect(restore.attributes('aria-label')).toBe('Restore workspace work')
      expect(restore.attributes('disabled')).toBeUndefined()

      state.archived = []
      await restore.trigger('click')
      await flushPromises()

      expect(api.post).toHaveBeenCalledWith(
        '/api/workspaces/archived/restore',
        { id: 'work-20260923-101500' },
      )
      expect(wrapper.find('.archived-item').exists()).toBe(false)
      expect(wrapper.find('.archived-empty').text()).toBe('No archived workspaces.')
    } finally {
      wrapper.unmount()
      vi.mocked(api.post).mockReset()
      vi.mocked(api.post).mockImplementation(() => Promise.resolve({}))
    }
  })

  it('disables Restore and says why when the name is taken', async () => {
    state.archived = [archived({ restorable: false, blocked_reason: "a workspace named 'work' already exists" })]
    const wrapper = await mountWorkspacesTab()
    try {
      const item = wrapper.find('.archived-item')
      expect(item.find('button').attributes('disabled')).toBeDefined()
      expect(item.text()).toContain("a workspace named 'work' already exists")
    } finally {
      wrapper.unmount()
    }
  })

  it('shows a load failure with Retry instead of an empty-state claim', async () => {
    state.archivedFails = true
    const wrapper = await mountWorkspacesTab()
    try {
      const alert = wrapper.find('.archived-workspaces [role="alert"]')
      expect(alert.text()).toContain('Could not load archived workspaces')
      expect(wrapper.find('.archived-empty').exists()).toBe(false)
      state.archivedFails = false
      state.archived = [archived()]
      await alert.find('button').trigger('click')
      await flushPromises()
      expect(wrapper.find('.archived-item').exists()).toBe(true)
    } finally {
      wrapper.unmount()
    }
  })
})
