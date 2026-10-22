// @vitest-environment jsdom

// Settings > Workspace > Overview: the Name field renames the active workspace. An unchanged name is the
// plain save and never confirms. A changed name asks first, then PATCHes the
// old path with `name` set to the new one. Cancelling sends nothing.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { WorkspacesResponse } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'

const state = vi.hoisted(() => ({
  workspaces: null as unknown,
}))

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
      return Promise.resolve({
        installed: false,
        binary_path: '',
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
  useProjectStore().activeWorkspace = active
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

type Wrapper = Awaited<ReturnType<typeof mountOverview>>

// The Workspace card is the first card on the page.
function workspaceCard(wrapper: Wrapper) {
  const card = wrapper.findAll('.card').find(c => c.find('.section-title').text() === 'Workspace')
  expect(card, 'no Workspace card').toBeTruthy()
  return card!
}

function nameInput(wrapper: Wrapper) {
  return wrapper.find('#workspace-name-input')
}

describe('Settings > Workspace Overview rename', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    state.workspaces = workspaces(['personal', 'work'])
    const { api } = await import('../../lib/api')
    vi.mocked(api.patch).mockReset()
    vi.mocked(api.patch).mockImplementation(() => Promise.resolve({}))
  })

  it('shows the name as the first row of the Workspace card', async () => {
    const wrapper = await mountOverview()
    try {
      expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('work')
      const firstRow = workspaceCard(wrapper).find('.set-subrow')
      expect(firstRow.find('.set-subrow-label').text()).toBe('Name')
      expect(firstRow.find('input').exists()).toBe(true)
    } finally {
      wrapper.unmount()
    }
  })

  it('an unchanged name saves without confirming and without sending a name', async () => {
    const { api } = await import('../../lib/api')
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountOverview()
    try {
            // Change a different field so Save is offered, leaving the name alone.
      const other = workspaceCard(wrapper).findAll('.workspace-color-swatch')
        .find(b => b.attributes('aria-checked') === 'false')
      await other!.trigger('click')
      await nextTick()
      expect(wrapper.find('.workspace-save').exists()).toBe(true)

      await wrapper.find('.workspace-save').trigger('click')
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
    const wrapper = await mountOverview()
    try {
      const input = nameInput(wrapper)
      await input.setValue('santo')
      await nextTick()
      await wrapper.find('.workspace-save').trigger('click')
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
      // The rename follows through the store and the Name field shows the new name.
      expect(useProjectStore().activeWorkspace).toBe('santo')
      expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('santo')
    } finally {
      wrapper.unmount()
    }
  })

  it('cancelling the rename confirmation sends nothing', async () => {
    const { api } = await import('../../lib/api')
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountOverview()
    try {
      const input = nameInput(wrapper)
      await input.setValue('santo')
      await wrapper.find('.workspace-save').trigger('click')
      await nextTick()
      pendingConfirm.value!.resolve(false)
      await flushPromises()

      expect(api.patch).not.toHaveBeenCalled()
      expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('santo')
    } finally {
      wrapper.unmount()
    }
  })

  it('renaming a first-listed personal keeps it the main workspace, so says nothing about it', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountOverview('personal')
    try {
      const input = nameInput(wrapper)
      await input.setValue('home')
      await wrapper.find('.workspace-save').trigger('click')
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
    const wrapper = await mountOverview('personal')
    try {
      const input = nameInput(wrapper)
      await input.setValue('home')
      await wrapper.find('.workspace-save').trigger('click')
      await nextTick()

      expect(pendingConfirm.value!.message).toContain('"work" becomes the main workspace.')
      pendingConfirm.value!.resolve(false)
      await flushPromises()
    } finally {
      wrapper.unmount()
    }
  })

  it('renaming a workspace to personal says it becomes the main workspace', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    state.workspaces = workspaces(['work', 'home'], 'work')
    const wrapper = await mountOverview('home')
    try {
      const input = nameInput(wrapper)
      await input.setValue('personal')
      await wrapper.find('.workspace-save').trigger('click')
      await nextTick()

      expect(pendingConfirm.value!.message).toBe(
        'Rename "home" to "personal". Projects and automations keep their ids and follow the new name. It becomes the main workspace.',
      )
      pendingConfirm.value!.resolve(false)
      await flushPromises()
    } finally {
      wrapper.unmount()
    }
  })

  it('renaming the current main workspace to personal says nothing about the main workspace', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    state.workspaces = workspaces(['work', 'home'], 'work')
    const wrapper = await mountOverview()
    try {
      const input = nameInput(wrapper)
      await input.setValue('personal')
      await wrapper.find('.workspace-save').trigger('click')
      await nextTick()

      expect(pendingConfirm.value!.message).not.toContain('main workspace')
      pendingConfirm.value!.resolve(false)
      await flushPromises()
    } finally {
      wrapper.unmount()
    }
  })

  it('an empty name is refused before any confirmation or request', async () => {
    const { api } = await import('../../lib/api')
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountOverview()
    try {
      const input = nameInput(wrapper)
      await input.setValue('   ')
      await wrapper.find('.workspace-save').trigger('click')
      await flushPromises()

      expect(pendingConfirm.value).toBeNull()
      expect(api.patch).not.toHaveBeenCalled()
      expect(wrapper.find('[role="alert"]').text()).toContain('Enter a workspace name.')
    } finally {
      wrapper.unmount()
    }
  })
})

// The editor is the page itself, so it has no Close. Changing a field offers
// the primary Save next to a bordered Discard; a clean form offers neither.
describe('Settings > Workspace Overview editor controls', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    state.workspaces = workspaces(['personal', 'work'])
    const { api } = await import('../../lib/api')
    vi.mocked(api.patch).mockReset()
    vi.mocked(api.patch).mockImplementation(() => Promise.resolve({}))
  })

  async function changeAccent(wrapper: Wrapper) {
    const other = workspaceCard(wrapper).findAll('.workspace-color-swatch')
      .find(b => b.attributes('aria-checked') === 'false')
    await other!.trigger('click')
    await nextTick()
  }

  it('offers no Close and no Save when clean', async () => {
    const wrapper = await mountOverview()
    try {
      expect(wrapper.find('.workspace-save').exists()).toBe(false)
      expect(wrapper.find('.workspace-discard').exists()).toBe(false)
      expect(wrapper.find('.workspace-close').exists()).toBe(false)
      expect(wrapper.find('.workspace-edit-actions').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('offers Save changes and Discard when dirty', async () => {
    const wrapper = await mountOverview()
    try {
      await changeAccent(wrapper)

      const actions = wrapper.find('.workspace-edit-actions')
      expect(wrapper.findAll('.workspace-save')).toHaveLength(1)
      expect(wrapper.findAll('.workspace-discard')).toHaveLength(1)
      expect(actions.find('.workspace-save').text()).toBe('Save changes')
      const discard = actions.find('.workspace-discard')
      expect(discard.text()).toBe('Discard')
      expect(discard.classes()).toContain('btn-small')
      expect(wrapper.find('.workspace-close').exists()).toBe(false)
      expect(actions.text()).not.toContain('Done')
    } finally {
      wrapper.unmount()
    }
  })

  it('discards unsaved workspace edits without changing permissions', async () => {
    const { api } = await import('../../lib/api')
    const wrapper = await mountOverview()
    try {
      await changeAccent(wrapper)
      await wrapper.find('#workspace-file-access-work').setValue('machine')
      await nextTick()

      await wrapper.find('.workspace-discard').trigger('click')
      await flushPromises()

      expect(api.patch).not.toHaveBeenCalled()
      expect(wrapper.find('.workspace-save').exists()).toBe(false)

      // The form shows the saved accent and the saved File access again.
      const active = workspaceCard(wrapper).findAll('.workspace-color-swatch')
        .filter(b => b.attributes('aria-checked') === 'true')
      expect(active).toHaveLength(1)
      expect(active[0]!.attributes('aria-label')).toBe('Rose Pink')
      expect((wrapper.find('#workspace-file-access-work').element as HTMLSelectElement).value).toBe('')
    } finally {
      wrapper.unmount()
    }
  })
})
