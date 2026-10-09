// @vitest-environment jsdom

// Settings > Workspaces: Edit can rename a workspace. An unchanged name is the
// plain save and never confirms. A changed name asks first, then PATCHes the
// old path with `name` set to the new one. Cancelling sends nothing.

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

async function openEditor(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>, name: string) {
  await cardFor(wrapper, name).find(`[aria-label="Edit workspace ${name}"]`).trigger('click')
  await nextTick()
  const card = cardFor(wrapper, name)
  return card.find('.set-row-body input.routine-input')
}

describe('Settings > Workspaces rename from Edit', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    state.workspaces = workspaces(['personal', 'work'])
    const { api } = await import('../../lib/api')
    vi.mocked(api.patch).mockReset()
    vi.mocked(api.patch).mockImplementation(() => Promise.resolve({}))
  })

  it('shows the name as the first row of the Edit body', async () => {
    const wrapper = await mountWorkspacesTab()
    try {
      const nameInput = await openEditor(wrapper, 'work')
      expect((nameInput.element as HTMLInputElement).value).toBe('work')
      const firstRow = cardFor(wrapper, 'work').find('.set-row-body .set-subrow')
      expect(firstRow.find('.set-subrow-label').text()).toBe('Name')
      expect(firstRow.find('input').exists()).toBe(true)
    } finally {
      wrapper.unmount()
    }
  })

  it('an unchanged name saves without confirming and without sending a name', async () => {
    const { api } = await import('../../lib/api')
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountWorkspacesTab()
    try {
      await openEditor(wrapper, 'work')
      // Change a different field so Save is offered, leaving the name alone.
      const other = cardFor(wrapper, 'work').findAll('.workspace-color-swatch')
        .find(b => b.attributes('aria-checked') === 'false')
      await other!.trigger('click')
      await nextTick()
      expect(cardFor(wrapper, 'work').find('.workspace-save').exists()).toBe(true)

      await cardFor(wrapper, 'work').find('.workspace-save').trigger('click')
      await flushPromises()

      expect(pendingConfirm.value).toBeNull()
      expect(api.patch).toHaveBeenCalledTimes(1)
      const [path, body] = vi.mocked(api.patch).mock.calls[0] as unknown as [string, Record<string, unknown>]
      expect(path).toBe('/api/workspaces/work')
      expect(body).not.toHaveProperty('name')
    } finally {
      wrapper.unmount()
    }
  })

  it('a changed name asks first, PATCHes the old path with the new name, and refetches the title', async () => {
    const { api } = await import('../../lib/api')
    const { pendingConfirm } = await import('../../lib/confirm')
    vi.mocked(api.patch).mockImplementation(() => {
      state.workspaces = workspaces(['personal', 'santo'])
      return Promise.resolve({ ...workspaces(['personal', 'santo']), renamed: { from: 'work', to: 'santo' } })
    })
    const wrapper = await mountWorkspacesTab()
    try {
      const nameInput = await openEditor(wrapper, 'work')
      await nameInput.setValue('santo')
      await nextTick()
      await cardFor(wrapper, 'work').find('.workspace-save').trigger('click')
      await nextTick()

      const request = pendingConfirm.value
      expect(request).toBeTruthy()
      expect(request!.title).toBe('Rename workspace')
      expect(request!.confirmLabel).toBe('Rename')
      expect(request!.destructive).toBe(false)
      expect(request!.message).toBe(
        'Rename "work" to "santo". Projects and automations keep their ids and follow the new name.',
      )
      expect(api.patch).not.toHaveBeenCalled()

      request!.resolve(true)
      await flushPromises()

      expect(api.patch).toHaveBeenCalledTimes(1)
      const [path, body] = vi.mocked(api.patch).mock.calls[0] as unknown as [string, Record<string, unknown>]
      expect(path).toBe('/api/workspaces/work')
      expect(body.name).toBe('santo')
      expect(wrapper.findAll('.workspace-list .workspace-card').map(c => c.find('.workspace-title').text()))
        .toEqual(['personal', 'santo'])
    } finally {
      wrapper.unmount()
    }
  })

  it('cancelling the rename confirmation sends nothing', async () => {
    const { api } = await import('../../lib/api')
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountWorkspacesTab()
    try {
      const nameInput = await openEditor(wrapper, 'work')
      await nameInput.setValue('santo')
      await cardFor(wrapper, 'work').find('.workspace-save').trigger('click')
      await nextTick()
      pendingConfirm.value!.resolve(false)
      await flushPromises()

      expect(api.patch).not.toHaveBeenCalled()
      expect((cardFor(wrapper, 'work').find('.set-row-body input.routine-input').element as HTMLInputElement).value).toBe('santo')
    } finally {
      wrapper.unmount()
    }
  })

  it('renaming a first-listed personal keeps it the main workspace, so says nothing about it', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountWorkspacesTab()
    try {
      const nameInput = await openEditor(wrapper, 'personal')
      await nameInput.setValue('home')
      await cardFor(wrapper, 'personal').find('.workspace-save').trigger('click')
      await nextTick()

      expect(pendingConfirm.value!.message).toContain('Rename "personal" to "home".')
      expect(pendingConfirm.value!.message).not.toContain('main workspace')
      pendingConfirm.value!.resolve(false)
      await flushPromises()
    } finally {
      wrapper.unmount()
    }
  })

  it('renaming a personal that is not first names the workspace that becomes main', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    state.workspaces = workspaces(['work', 'personal'])
    const wrapper = await mountWorkspacesTab()
    try {
      const nameInput = await openEditor(wrapper, 'personal')
      await nameInput.setValue('home')
      await cardFor(wrapper, 'personal').find('.workspace-save').trigger('click')
      await nextTick()

      expect(pendingConfirm.value!.message).toContain('"work" becomes the main workspace.')
      pendingConfirm.value!.resolve(false)
      await flushPromises()
    } finally {
      wrapper.unmount()
    }
  })

  it('an empty name is refused before any confirmation or request', async () => {
    const { api } = await import('../../lib/api')
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountWorkspacesTab()
    try {
      const nameInput = await openEditor(wrapper, 'work')
      await nameInput.setValue('   ')
      await cardFor(wrapper, 'work').find('.workspace-save').trigger('click')
      await flushPromises()

      expect(pendingConfirm.value).toBeNull()
      expect(api.patch).not.toHaveBeenCalled()
      expect(wrapper.find('[role="alert"]').text()).toContain('Enter a workspace name.')
    } finally {
      wrapper.unmount()
    }
  })
})
