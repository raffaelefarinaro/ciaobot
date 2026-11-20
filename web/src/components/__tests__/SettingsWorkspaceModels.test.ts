// @vitest-environment jsdom
//
// Workspace > Overview > "Chats and models", mounted on its own with the real
// useWorkspaceAdmin provided under WORKSPACE_ADMIN_KEY. Pinned: the first option
// of every field names the engine default, an empty choice removes the override,
// each provider keeps its own overrides, and edits ride the page's dirty tracking.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { computed, effectScope, nextTick, type EffectScope } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsWorkspaceModels from '../settings/SettingsWorkspaceModels.vue'
import SettingsWorkspaceOverview from '../settings/SettingsWorkspaceOverview.vue'
import ModelSelector from '../ModelSelector.vue'
import { WORKSPACE_ADMIN_KEY, useWorkspaceAdmin, type WorkspaceAdmin } from '../../composables/useWorkspaceAdmin'
import { useProjectStore } from '../../stores/projects'

const state = vi.hoisted(() => ({ routines: {} as unknown, models: {} as unknown }))
const askConfirm = vi.hoisted(() => vi.fn())
vi.mock('../../lib/confirm', () => ({ askConfirm }))

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn((path: string) => {
      if (path === '/api/settings/routines') return Promise.resolve(state.routines)
      if (path.startsWith('/api/models?') && new URLSearchParams(path.split('?')[1]).has('workspace')) return Promise.resolve(state.models)
      return Promise.resolve({})
    }),
    post: vi.fn(), patch: vi.fn(), del: vi.fn(),
  },
}))

function routines(over: Record<string, unknown> = {}) {
  return {
    provider_default_models: { claude: 'sonnet' },
    provider_default_modes: { claude: 'bypass' },
    provider_default_thinking: { claude: 'high' },
    provider_insights_models: { claude: 'haiku' },
    model_options: { anthropic: ['haiku', 'sonnet', 'opus'] },
    ...over,
  }
}

function modelsResponse(over: Record<string, unknown> = {}) {
  return {
    models: ['sonnet'],
    default: 'sonnet',
    provider_models: { claude: ['sonnet'] },
    provider_defaults: { claude: 'sonnet' },
    thinking_levels: { claude: ['low', 'medium', 'high'], opencode: ['minimal', 'max'] },
    opencode_models: ['openai/gpt-5'],
    ...over,
  }
}

let scope: EffectScope
let admin: WorkspaceAdmin
let store: ReturnType<typeof useProjectStore>
const mounted: VueWrapper[] = []

async function mountCard() {
  const view = mount(SettingsWorkspaceModels, { global: { provide: { [WORKSPACE_ADMIN_KEY as symbol]: admin } } })
  mounted.push(view)
  await flushPromises()
  return view
}

beforeEach(async () => {
  const { api } = await import('../../lib/api')
  vi.mocked(api.get).mockClear()
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === '/api/settings/routines') return Promise.resolve(state.routines) as never
    if (path.startsWith('/api/models?') && new URLSearchParams(path.split('?')[1]).has('workspace')) return Promise.resolve(state.models) as never
    return Promise.resolve({}) as never
  })
  setActivePinia(createPinia())
  state.routines = routines()
  state.models = modelsResponse()
  store = useProjectStore()
  store.activeWorkspace = 'work'
  store.workspaces = [
    { name: 'work', vault_root: 'work', default_provider: 'claude', gws_profile: '' },
    { name: 'home', vault_root: 'home', default_provider: 'claude', gws_profile: '' },
  ]
  store.workspaceProviderOptions = [
    { value: 'claude', label: 'Claude Code' },
    { value: 'opencode', label: 'OpenCode' },
  ]
  store.fetchWorkspaces = vi.fn().mockImplementation(async () => ({ workspaces: store.workspaces })) as never
  scope = effectScope()
  admin = scope.run(() => useWorkspaceAdmin({
    notifySaved: vi.fn(),
    notifyFailed: vi.fn(),
    gwsProfileOptions: computed(() => []),
    fetchGwsIntegration: vi.fn().mockResolvedValue(undefined),
  }))!
  await admin.fetchWorkspacesList()
})

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  scope.stop()
  document.body.innerHTML = ''
})

const form = () => admin.activeWorkspaceForm.value!

function select(view: VueWrapper, field: string, provider = 'claude') {
  return view.get(`#ws-override-${field}-${provider}`)
}

function optionTexts(view: VueWrapper, field: string, provider = 'claude') {
  return select(view, field, provider).findAll('option').map((o) => o.text())
}

describe('SettingsWorkspaceModels', () => {
  it('ignores a queued thinking change from the prior workspace before rendering', async () => {
    const view = await mountCard()
    const oldSelect = select(view, 'thinking').element as HTMLSelectElement
    oldSelect.value = 'high'
    store.activeWorkspace = 'home'
    oldSelect.dispatchEvent(new Event('change', { bubbles: true }))
    expect(form().provider_overrides).toEqual({})
    await nextTick()
  })

  it.each(['home', 'work'])('rejects a detached thinking event after switching to %s', async (destination) => {
    const view = await mountCard()
    const oldSelect = select(view, 'thinking').element as HTMLSelectElement
    oldSelect.value = 'high'
    store.activeWorkspace = 'home'
    await flushPromises()
    if (destination === 'work') {
      store.activeWorkspace = 'work'
      await flushPromises()
    }
    expect(select(view, 'thinking').element).not.toBe(oldSelect)
    oldSelect.dispatchEvent(new Event('change', { bubbles: true }))
    expect(form().provider_overrides).toEqual({})
  })

  it('rejects a detached thinking change after a provider round trip', async () => {
    const view = await mountCard()
    const oldSelect = select(view, 'thinking').element as HTMLSelectElement
    oldSelect.value = 'high'
    form().default_provider = 'opencode'
    await nextTick()
    form().default_provider = 'claude'
    await nextTick()
    expect(select(view, 'thinking').element).not.toBe(oldSelect)
    oldSelect.dispatchEvent(new Event('change', { bubbles: true }))
    expect(form().provider_overrides).toEqual({})
  })

  it.each([0, 1])('rejects picker %i events after a provider switch before rendering', async (index) => {
    const view = await mountCard()
    const picker = view.findAllComponents(ModelSelector)[index]
    form().default_provider = 'opencode'
    picker.vm.$emit('update:modelValue', 'sonnet')
    expect(form().provider_overrides).toEqual({})
    await nextTick()
  })

  it.each([0, 1])('rejects picker %i detached events after provider changes and returns', async (index) => {
    const view = await mountCard()
    const old = view.findAllComponents(ModelSelector)[index]
    await old.get('.model-selector__trigger').trigger('click')
    await flushPromises()
    const item = old.findAll('.model-selector__item').find((el) => el.text().includes('Sonnet'))!
    form().default_provider = 'opencode'
    await nextTick()
    expect(view.findAllComponents(ModelSelector)[index].vm).not.toBe(old.vm)
    item.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(form().provider_overrides).toEqual({})
    form().default_provider = 'claude'
    await nextTick()
    item.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(form().provider_overrides).toEqual({})
  })

  it.each([0, 1])('rejects picker %i callback from before a provider round trip', async (index) => {
    const view = await mountCard()
    const picker = view.findAllComponents(ModelSelector)[index]
    const oldCallback = picker.vm.$.vnode.props!['onUpdate:modelValue'] as (value: string) => void
    form().default_provider = 'opencode'
    await nextTick()
    form().default_provider = 'claude'
    await nextTick()
    oldCallback('sonnet')
    expect(form().provider_overrides).toEqual({})
  })

  it.each([0, 1])('rejects picker %i callback after a synchronous provider round trip', async (index) => {
    const view = await mountCard()
    const picker = view.findAllComponents(ModelSelector)[index]
    const oldCallback = picker.vm.$.vnode.props!['onUpdate:modelValue'] as (value: string) => void
    form().default_provider = 'opencode'
    form().default_provider = 'claude'
    oldCallback('sonnet')
    expect(form().provider_overrides).toEqual({})
  })

  it('announces loading, unavailable workspace, and switch loading distinctly', async () => {
    const { api } = await import('../../lib/api')
    const pending: ((value: unknown) => void)[] = []
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.resolve(state.routines) : new Promise((resolve) => pending.push(resolve)))
    const view = mount(SettingsWorkspaceModels, { global: { provide: { [WORKSPACE_ADMIN_KEY as symbol]: admin } } })
    mounted.push(view)
    expect(view.get('[role="status"]').text()).toContain('Loading model options')
    pending[0](state.models)
    await flushPromises()
    store.activeWorkspace = 'home'
    await nextTick()
    expect(view.get('[role="status"]').text()).toContain('Loading model options')
    store.activeWorkspace = ''
    await nextTick()
    expect(view.get('[role="status"]').text()).toContain('Workspace unavailable')
  })

  it('preserves keyboard focus through retry failure and restores it on success', async () => {
    const { api } = await import('../../lib/api')
    const requests: { resolve: (value: unknown) => void; reject: (error: Error) => void }[] = []
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.resolve(state.routines) : new Promise((resolve, reject) => requests.push({ resolve, reject })))
    const view = mount(SettingsWorkspaceModels, { attachTo: document.body, global: { provide: { [WORKSPACE_ADMIN_KEY as symbol]: admin } } })
    mounted.push(view)
    requests[0].reject(new Error('first'))
    await flushPromises()
    const retry = view.get('button.btn-small')
    ;(retry.element as HTMLButtonElement).focus()
    await retry.trigger('click')
    expect(document.activeElement).toBe(retry.element)
    expect(view.get('[role="status"]').text()).toContain('Loading model options')
    await retry.trigger('click')
    expect(requests).toHaveLength(2)
    requests[1].reject(new Error('again'))
    await flushPromises()
    expect(document.activeElement).toBe(retry.element)
    await retry.trigger('click')
    requests[2].resolve(state.models)
    await flushPromises()
    expect(document.activeElement).toBe(view.get('[role="status"]').element)
    expect(view.get('[role="status"]').text()).toContain('Model options loaded')
  })

  it('does not steal focus if the user leaves Retry while it is loading', async () => {
    const { api } = await import('../../lib/api')
    const requests: { resolve: (value: unknown) => void; reject: (error: Error) => void }[] = []
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.resolve(state.routines) : new Promise((resolve, reject) => requests.push({ resolve, reject })))
    const view = mount(SettingsWorkspaceModels, { attachTo: document.body, global: { provide: { [WORKSPACE_ADMIN_KEY as symbol]: admin } } })
    mounted.push(view)
    requests[0].reject(new Error('first'))
    await flushPromises()
    const retry = view.get('button.btn-small')
    ;(retry.element as HTMLButtonElement).focus()
    await retry.trigger('click')
    const permission = select(view, 'permission_mode')
    ;(permission.element as HTMLSelectElement).focus()
    requests[1].resolve(state.models)
    await flushPromises()
    expect(document.activeElement).toBe(permission.element)
  })

  it('requests models for the registered workspace, not the rename draft', async () => {
    const { api } = await import('../../lib/api')
    vi.mocked(api.get).mockClear()
    const view = await mountCard()
    admin.workspaceNameDraft.value = 'unregistered'
    await nextTick()
    expect(vi.mocked(api.get).mock.calls.map(([path]) => path)).toContain('/api/models?workspace=work')
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path.startsWith('/api/models'))).toHaveLength(1)
    expect(view.text()).not.toContain('Could not load the model list')
  })

  it('does not request models without an active workspace', async () => {
    const { api } = await import('../../lib/api')
    store.activeWorkspace = 'missing'
    vi.mocked(api.get).mockClear()
    await mountCard()
    expect(vi.mocked(api.get).mock.calls.some(([path]) => path.startsWith('/api/models'))).toBe(false)
  })

  it('does not request models for an empty registered name', async () => {
    const { api } = await import('../../lib/api')
    admin.workspaceForms.value[0].name = ''
    store.activeWorkspace = ''
    vi.mocked(api.get).mockClear()
    const view = await mountCard()
    expect(vi.mocked(api.get).mock.calls.some(([path]) => path.startsWith('/api/models'))).toBe(false)
    expect(view.text()).toContain('Workspace unavailable')
    expect(view.findAllComponents(ModelSelector).every((item) => item.props('disabled'))).toBe(true)
  })

  it('encodes the exact registered workspace name', async () => {
    const { api } = await import('../../lib/api')
    admin.workspaceForms.value[0].name = 'Work & life'
    store.activeWorkspace = 'Work & life'
    vi.mocked(api.get).mockClear()
    await mountCard()
    const paths = vi.mocked(api.get).mock.calls.map(([path]) => path).filter((path) => path.startsWith('/api/models'))
    expect(paths).toHaveLength(1)
    expect(new URL(paths[0], 'http://localhost').searchParams.get('workspace')).toBe('Work & life')
  })

  it('invalidates pending work through an absent form without an unscoped read', async () => {
    const { api } = await import('../../lib/api')
    let resolve!: (value: unknown) => void
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.resolve(state.routines) : new Promise((done) => { resolve = done }))
    const view = mount(SettingsWorkspaceModels, { global: { provide: { [WORKSPACE_ADMIN_KEY as symbol]: admin } } })
    mounted.push(view)
    store.activeWorkspace = ''
    await nextTick()
    expect(view.find('.workspace-models').exists()).toBe(false)
    resolve(state.models)
    await flushPromises()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path.startsWith('/api/models')).map(([path]) => path))
      .toEqual(['/api/models?workspace=work'])
  })

  it('the overview mounts the card only while its active form exists', async () => {
    const { api } = await import('../../lib/api')
    store.activeWorkspace = 'missing'
    const view = mount(SettingsWorkspaceOverview, { global: {
      provide: { [WORKSPACE_ADMIN_KEY as symbol]: admin },
      stubs: ['SettingsClaudeTrim', 'WorkspaceGoogleAccount', 'SettingsMemoryHandover', 'SettingsUpdateTasks', 'SettingsAllWorkspaces', 'WorkspaceTile', 'WorkspaceColorPicker'],
    } })
    mounted.push(view)
    expect(view.findComponent(SettingsWorkspaceModels).exists()).toBe(false)
    expect(vi.mocked(api.get).mock.calls.some(([path]) => path.startsWith('/api/models'))).toBe(false)
    store.activeWorkspace = 'work'
    await flushPromises()
    expect(view.findComponent(SettingsWorkspaceModels).exists()).toBe(true)
    expect(vi.mocked(api.get).mock.calls.map(([path]) => path)).toContain('/api/models?workspace=work')
    store.activeWorkspace = 'missing'
    await nextTick()
    expect(view.findComponent(SettingsWorkspaceModels).exists()).toBe(false)
  })

  it('fences old responses across a synchronous work-home-work transition', async () => {
    const { api } = await import('../../lib/api')
    const requests: { path: string; resolve: (value: unknown) => void; reject: (reason: Error) => void }[] = []
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.resolve(state.routines)
      : new Promise((resolve, reject) => requests.push({ path, resolve, reject })))
    const view = mount(SettingsWorkspaceModels, { global: { provide: { [WORKSPACE_ADMIN_KEY as symbol]: admin } } })
    mounted.push(view)
    store.activeWorkspace = 'home'
    store.activeWorkspace = 'work'
    expect(requests.map((r) => r.path)).toEqual([
      '/api/models?workspace=work', '/api/models?workspace=home', '/api/models?workspace=work',
    ])
    await nextTick()
    expect(view.text()).toContain('Loading effort options')
    requests[0].resolve(modelsResponse({ thinking_levels: { claude: ['old'] } }))
    requests[1].reject(new Error('old home failure'))
    await flushPromises()
    expect(view.text()).toContain('Loading effort options')
    expect(view.text()).not.toContain('old home failure')
    requests[2].resolve(modelsResponse({ thinking_levels: { claude: ['current'] } }))
    await flushPromises()
    expect(optionTexts(view, 'thinking')).toContain('Current')
    expect(optionTexts(view, 'thinking')).not.toContain('Old')
  })

  it('exposes reported model options while routines are pending', async () => {
    const { api } = await import('../../lib/api')
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? new Promise(() => {})
      : Promise.resolve(modelsResponse({ provider_models: { claude: ['workspace-model'] } })))
    const view = await mountCard()
    const selectors = view.findAllComponents(ModelSelector)
    expect(optionTexts(view, 'thinking')[0]).toBe('Engine default (unknown)')
    expect(optionTexts(view, 'permission_mode')[0]).toBe('Engine default (unknown)')
    for (const selector of selectors) {
      expect(selector.props('disabled')).toBe(false)
      expect((selector.props('sections') as { models: string[] }[])[1].models).toEqual(['workspace-model'])
    }
    selectors[0].vm.$emit('update:modelValue', 'workspace-model')
    selectors[1].vm.$emit('update:modelValue', 'workspace-model')
    expect(form().provider_overrides.claude).toEqual({ model: 'workspace-model', insights_model: 'workspace-model' })
  })

  it('reports empty model catalogs as unknown, not disconnected', async () => {
    state.models = modelsResponse({ models: [], provider_models: { claude: [] }, opencode_models: [] })
    const view = await mountCard()
    expect(view.text()).toContain('No models reported; availability unknown.')
    expect(view.text()).not.toContain('Connect')
    expect(view.findAllComponents(ModelSelector).every((selector) => selector.props('disabled'))).toBe(true)
  })

  it('keeps reported choices available if routines fail', async () => {
    const { api } = await import('../../lib/api')
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.reject(new Error('routines offline'))
      : Promise.resolve(modelsResponse({ provider_models: { claude: ['workspace-model'] } })))
    const view = await mountCard()
    expect(view.text()).toContain('Could not load the provider defaults')
    expect(view.text()).not.toContain('Could not load the model list')
    expect(view.findAllComponents(ModelSelector)[0].props('disabled')).toBe(false)
  })

  it.each(['pending', 'failed'])('selects both reported models with routines %s', async (routineState) => {
    const { api } = await import('../../lib/api')
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? routineState === 'pending' ? new Promise(() => {}) : Promise.reject(new Error('offline'))
      : Promise.resolve(modelsResponse({ provider_models: { claude: ['actual-model'] } })))
    const view = await mountCard()
    for (const index of [0, 1]) {
      const selector = view.findAllComponents(ModelSelector)[index]
      await selector.get('.model-selector__trigger').trigger('click')
      await flushPromises()
      const item = selector.findAll('.model-selector__item').find((el) => /actual.model/i.test(el.text()))
      expect(item).toBeDefined()
      await item!.trigger('click')
      await flushPromises()
    }
    expect(form().provider_overrides.claude).toEqual({ model: 'actual-model', insights_model: 'actual-model' })
  })

  it.each([0, 1])('closes picker %i on switch and rejects detached pointer and keyboard events', async (index) => {
    const { api } = await import('../../lib/api')
    vi.mocked(api.get).mockImplementation((path: string) => path.includes('workspace=home')
      ? new Promise(() => {}) : Promise.resolve(path.includes('/api/models') ? state.models : state.routines))
    const view = await mountCard()
    const picker = view.findAllComponents(ModelSelector)[index]
    await picker.get('.model-selector__trigger').trigger('click')
    await flushPromises()
    const oldItem = picker.findAll('.model-selector__item').find((item) => item.text().includes('Sonnet'))!
    const oldSearch = picker.get('.model-selector__search')
    store.activeWorkspace = 'home'
    await nextTick()
    expect(view.find('.model-selector__popover').exists()).toBe(false)
    oldItem.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    oldSearch.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(form().provider_overrides).toEqual({})
  })

  it.each([0, 1])('rejects picker %i detached events after work-home-work', async (index) => {
    const view = await mountCard()
    const picker = view.findAllComponents(ModelSelector)[index]
    await picker.get('.model-selector__trigger').trigger('click')
    await flushPromises()
    const item = picker.findAll('.model-selector__item').find((el) => el.text().includes('Sonnet'))!
    const search = picker.get('.model-selector__search')
    const oldCallback = picker.vm.$.vnode.props!['onUpdate:modelValue'] as (value: string) => void
    store.activeWorkspace = 'home'
    store.activeWorkspace = 'work'
    await flushPromises()
    item.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    search.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    oldCallback('sonnet')
    expect(form().provider_overrides).toEqual({})
    expect(view.find('.model-selector__popover').exists()).toBe(false)
  })

  it('does not reload when the active form object is replaced with the same name', async () => {
    const { api } = await import('../../lib/api')
    await mountCard()
    vi.mocked(api.get).mockClear()
    const index = admin.workspaceForms.value.findIndex((item) => item.name === 'work')
    admin.workspaceForms.value[index] = { ...admin.workspaceForms.value[index] }
    await nextTick()
    expect(vi.mocked(api.get).mock.calls.some(([path]) => path.startsWith('/api/models'))).toBe(false)
  })

  it('ignores stale completion after current success and after unmount', async () => {
    const { api } = await import('../../lib/api')
    const requests: { resolve: (value: unknown) => void; reject: (error: Error) => void }[] = []
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.resolve(state.routines)
      : new Promise((resolve, reject) => requests.push({ resolve, reject })))
    const view = mount(SettingsWorkspaceModels, { global: { provide: { [WORKSPACE_ADMIN_KEY as symbol]: admin } } })
    mounted.push(view)
    store.activeWorkspace = 'home'
    requests[1].resolve(modelsResponse({ thinking_levels: { claude: ['home-only'] } }))
    await flushPromises()
    requests[0].reject(new Error('obsolete failure'))
    await flushPromises()
    expect(optionTexts(view, 'thinking')).toContain('Home-only')
    expect(view.text()).not.toContain('obsolete failure')
    store.activeWorkspace = 'work'
    view.unmount()
    mounted.pop()
    requests[2].resolve(state.models)
    await flushPromises()
    expect(requests).toHaveLength(3)
  })

  it('ignores an old successful response after the new workspace has loaded', async () => {
    const { api } = await import('../../lib/api')
    const resolve: ((value: unknown) => void)[] = []
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.resolve(state.routines) : new Promise((done) => resolve.push(done)))
    const view = mount(SettingsWorkspaceModels, { global: { provide: { [WORKSPACE_ADMIN_KEY as symbol]: admin } } })
    mounted.push(view)
    store.activeWorkspace = 'home'
    resolve[1](modelsResponse({ thinking_levels: { claude: ['home'] }, provider_models: { claude: ['home-model'] } }))
    await flushPromises()
    resolve[0](modelsResponse({ thinking_levels: { claude: ['work'] }, provider_models: { claude: ['work-model'] } }))
    await flushPromises()
    expect(optionTexts(view, 'thinking')).toContain('Home')
    expect(optionTexts(view, 'thinking')).not.toContain('Work')
    expect((view.findAllComponents(ModelSelector)[0].props('sections') as { models: string[] }[])[1].models).toEqual(['home-model'])
  })

  it('retries a failed scoped request without disabling permission settings', async () => {
    const { api } = await import('../../lib/api')
    let attempts = 0
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.resolve(state.routines)
      : ++attempts === 1 ? Promise.reject(new Error('offline')) : Promise.resolve(state.models))
    const view = await mountCard()
    expect(view.text()).toContain('Could not load the model list')
    expect(select(view, 'permission_mode').attributes('disabled')).toBeUndefined()
    await view.get('button.btn-small').trigger('click')
    await flushPromises()
    expect(attempts).toBe(2)
    expect(view.text()).not.toContain('Could not load the model list')
    expect(optionTexts(view, 'thinking')).toContain('High')
  })

  it('allows another retry after a retry fails', async () => {
    const { api } = await import('../../lib/api')
    let attempts = 0
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/settings/routines'
      ? Promise.resolve(state.routines)
      : ++attempts < 3 ? Promise.reject(new Error(`failure ${attempts}`)) : Promise.resolve(state.models))
    const view = await mountCard()
    for (const message of ['failure 1', 'failure 2']) {
      expect(view.text()).toContain('Could not load the model list')
      expect(view.text()).not.toContain(message)
      await view.get('button.btn-small').trigger('click')
      await flushPromises()
    }
    expect(attempts).toBe(3)
    expect(view.text()).not.toContain('Could not load the model list')
    expect(optionTexts(view, 'thinking')).toContain('High')
  })

  it('rejects selection callbacks from a previous visit to the same workspace', async () => {
    const view = await mountCard()
    const old = view.findAllComponents(ModelSelector)
      .map((selector) => selector.vm.$.vnode.props!['onUpdate:modelValue'] as (value: string) => void)
    store.activeWorkspace = 'home'
    store.activeWorkspace = 'work'
    await flushPromises()
    old[0]('stale-model')
    old[1]('stale-insights')
    expect(form().provider_overrides).toEqual({})
  })

  it('keeps a declined draft and discards it only after a confirmed switch', async () => {
    const { api } = await import('../../lib/api')
    const view = await mountCard()
    await select(view, 'permission_mode').setValue('manual')
    await view.findAllComponents(ModelSelector)[0].get('.model-selector__trigger').trigger('click')
    await flushPromises()
    expect(view.find('.model-selector__popover').exists()).toBe(true)
    vi.mocked(api.get).mockClear()
    askConfirm.mockResolvedValueOnce(false)
    expect(await store.switchWorkspace('home', { transition: false })).toBe(false)
    expect(store.activeWorkspace).toBe('work')
    expect(form().provider_overrides.claude.permission_mode).toBe('manual')
    expect(view.find('.model-selector__popover').exists()).toBe(true)
    expect(vi.mocked(api.get).mock.calls.some(([path]) => path.startsWith('/api/models'))).toBe(false)
    askConfirm.mockResolvedValueOnce(true)
    expect(await store.switchWorkspace('home', { transition: false })).toBe(true)
    await flushPromises()
    expect(form().name).toBe('home')
    expect(vi.mocked(api.get).mock.calls.map(([path]) => path)).toContain('/api/models?workspace=home')
    expect(admin.workspaceForms.value.find((item) => item.name === 'work')!.provider_overrides).toEqual({})
  })

  it('shows the engine default as the first option of every field', async () => {
    const view = await mountCard()
    expect(view.get('.section-title').text()).toBe('Chats and models')
    expect(optionTexts(view, 'thinking')[0]).toBe('Engine default (High)')
    expect(optionTexts(view, 'permission_mode')[0]).toBe('Engine default (Bypass)')
    const [model, insights] = view.findAllComponents(ModelSelector)
    const label = (c: typeof model) =>
      Object.values((c.props('sections') as { modelLabels?: Record<string, string> }[])[0].modelLabels!)[0]
    expect(label(model)).toMatch(/^Engine default \(.+\)$/)
    expect(label(insights)).toMatch(/^Engine default \(.*[Hh]aiku.*\)$/)
  })

  it('names each model picker trigger after its row label and the current value', async () => {
    const view = await mountCard()
    const triggers = view.findAll('.model-selector__trigger')
    expect(triggers).toHaveLength(2)
    const parts = (i: number) => (triggers[i].attributes('aria-labelledby') || '')
      .split(' ')
      .map((id) => view.find(`#${CSS.escape(id)}`).text())
    const value = (i: number) => triggers[i].get('.model-selector__label').text()
    expect(parts(0)).toEqual(['Default model', value(0)])
    expect(parts(1)).toEqual(['Session insights', value(1)])
  })

  it('names Auto and Auto-mode when the engine sets nothing', async () => {
    state.routines = routines({ provider_default_modes: {}, provider_default_thinking: {}, provider_insights_models: {} })
    const view = await mountCard()
    expect(optionTexts(view, 'thinking')[0]).toBe('Engine default (Auto)')
    expect(optionTexts(view, 'permission_mode')[0]).toBe('Engine default (Auto)')
    const insights = view.findAllComponents(ModelSelector)[1]
    expect(Object.values((insights.props('sections') as { modelLabels: Record<string, string> }[])[0].modelLabels)[0])
      .toBe('Engine default (same as chat model)')
  })

  it('an empty selection removes the override', async () => {
    const view = await mountCard()
    const model = view.findAllComponents(ModelSelector)[0]
    model.vm.$emit('update:modelValue', 'opus')
    await flushPromises()
    expect(form().provider_overrides).toEqual({ claude: { model: 'opus' } })
    model.vm.$emit('update:modelValue', '__ciao_ws_default__')
    await flushPromises()
    expect(form().provider_overrides).toEqual({})

    await select(view, 'thinking').setValue('low')
    expect(form().provider_overrides).toEqual({ claude: { thinking: 'low' } })
    await select(view, 'thinking').setValue('')
    expect(form().provider_overrides).toEqual({})
  })

  it("switching the provider shows that provider's overrides and keeps the other's", async () => {
    const view = await mountCard()
    await select(view, 'thinking').setValue('high')
    const radios = view.findAll('button[role="radio"]')
    await radios[1].trigger('click')
    await flushPromises()

    expect(form().default_provider).toBe('opencode')
    expect(form().provider_overrides).toEqual({ claude: { thinking: 'high' } })
    expect((select(view, 'thinking', 'opencode').element as HTMLSelectElement).value).toBe('')
    await select(view, 'permission_mode', 'opencode').setValue('manual')
    expect(form().provider_overrides).toEqual({ claude: { thinking: 'high' }, opencode: { permission_mode: 'manual' } })

    await view.findAll('button[role="radio"]')[0].trigger('click')
    await flushPromises()
    expect((select(view, 'thinking').element as HTMLSelectElement).value).toBe('high')
  })

  it("the hint under Permission mode states what bypass is and links the selected provider's docs", async () => {
    const view = await mountCard()
    const note = () => view.get('[data-testid="permission-note"]')
    expect(note().text()).toContain('full user access')
    expect(note().get('a').attributes('href')).toBe('https://code.claude.com/docs/en/permissions')
    expect(note().get('a').attributes('target')).toBe('_blank')
    expect(note().get('a').attributes('rel')).toBe('noopener')

    await view.findAll('button[role="radio"]')[1].trigger('click')
    await flushPromises()
    expect(form().default_provider).toBe('opencode')
    expect(note().get('a').attributes('href')).toBe('https://opencode.ai/docs/permissions/')
  })

  it('the provider radiogroup takes arrow keys', async () => {
    const view = await mountCard()
    const group = view.get('[role="radiogroup"]')
    expect(view.findAll('button[role="radio"]').map((b) => b.attributes('tabindex'))).toEqual(['0', '-1'])
    await group.trigger('keydown', { key: 'ArrowRight' })
    expect(form().default_provider).toBe('opencode')
    await group.trigger('keydown', { key: 'ArrowRight' })
    expect(form().default_provider).toBe('claude')
  })

  it('edits make the form dirty and Discard restores', async () => {
    const view = await mountCard()
    expect(admin.workspaceDirty(form())).toBe(false)
    await select(view, 'permission_mode').setValue('manual')
    expect(admin.workspaceDirty(form())).toBe(true)
    admin.discardWorkspace('work')
    await flushPromises()
    expect(admin.workspaceDirty(form())).toBe(false)
    expect(form().provider_overrides).toEqual({})
  })

  it('option lists come from the engine sources', async () => {
    const view = await mountCard()
    const sections = view.findAllComponents(ModelSelector)[0].props('sections') as { models: string[] }[]
    expect(sections[1].models).toEqual(['sonnet'])
    expect(select(view, 'thinking').findAll('option').slice(1).map((o) => o.attributes('value')))
      .toEqual(['low', 'medium', 'high'])
    expect(select(view, 'permission_mode').findAll('option').map((o) => o.attributes('value')))
      .toEqual(['', 'manual', 'auto', 'bypass'])
  })

  it('a provider that is not available disables its model selectors', async () => {
    state.models = modelsResponse({ opencode_models: [], provider_models: { claude: ['sonnet'] } })
    const view = await mountCard()
    await view.findAll('button[role="radio"]')[1].trigger('click')
    await flushPromises()
    const selectors = view.findAllComponents(ModelSelector)
    expect(selectors.map((s) => s.props('disabled'))).toEqual([true, true])
    expect(view.text()).toContain('No models reported; availability unknown.')
  })

  it('offers OpenCode models reported in provider_models when the legacy array is empty', async () => {
    state.models = modelsResponse({ opencode_models: [], provider_models: { claude: ['sonnet'], opencode: ['openai/example'] } })
    const view = await mountCard()
    await view.findAll('button[role="radio"]')[1].trigger('click')
    const selector = view.findAllComponents(ModelSelector)[0]
    expect(selector.props('disabled')).toBe(false)
    expect((selector.props('sections') as { models: string[] }[])[1].models).toEqual(['openai/example'])
  })

  it('disables every control while the workspace is saving', async () => {
    const view = await mountCard()
    admin.workspacesSaving.value = 'work'
    await flushPromises()
    expect(view.findAll('select').every((s) => s.attributes('disabled') !== undefined)).toBe(true)
    expect(view.findAll('button[role="radio"]').every((b) => b.attributes('disabled') !== undefined)).toBe(true)
  })

  it('with no overrides the permission mode select sits on Engine default', async () => {
    const view = await mountCard()
    const el = select(view, 'permission_mode').element as HTMLSelectElement
    expect(el.value).toBe('')
    expect(el.selectedOptions[0].textContent!.trim()).toMatch(/^Engine default \(/)
  })

  it('names what failed when a fetch errors', async () => {
    const { api } = await import('../../lib/api')
    vi.mocked(api.get).mockImplementation(() => Promise.reject(new Error('private provider error')))
    const view = await mountCard()
    const alerts = view.findAll('[role="alert"]').map((a) => a.text())
    expect(alerts.some((t) => t.includes('Could not load the provider defaults'))).toBe(true)
    expect(alerts.some((t) => t.includes('Could not load the model list'))).toBe(true)
    expect(view.text()).not.toContain('private provider error')
  })
})
