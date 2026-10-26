// @vitest-environment jsdom
//
// Workspace > Overview > "Chats and models", mounted on its own with the real
// useWorkspaceAdmin provided under WORKSPACE_ADMIN_KEY. Pinned: the first option
// of every field names the engine default, an empty choice removes the override,
// each provider keeps its own overrides, and edits ride the page's dirty tracking.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { computed, effectScope, type EffectScope } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsWorkspaceModels from '../settings/SettingsWorkspaceModels.vue'
import ModelSelector from '../ModelSelector.vue'
import { WORKSPACE_ADMIN_KEY, useWorkspaceAdmin, type WorkspaceAdmin } from '../../composables/useWorkspaceAdmin'
import { useProjectStore } from '../../stores/projects'

const state = vi.hoisted(() => ({ routines: {} as unknown, models: {} as unknown }))

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn((path: string) => {
      if (path === '/api/settings/routines') return Promise.resolve(state.routines)
      if (path === '/api/models') return Promise.resolve(state.models)
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
  setActivePinia(createPinia())
  state.routines = routines()
  state.models = modelsResponse()
  store = useProjectStore()
  store.activeWorkspace = 'work'
  store.workspaces = [{ name: 'work', vault_root: 'work', default_provider: 'claude', gws_profile: '' }]
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
    expect(view.text()).toContain('Connect OpenCode on Settings > Providers to choose a model.')
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
    vi.mocked(api.get).mockImplementation((path: string) => Promise.reject(new Error(`boom ${path}`)))
    const view = await mountCard()
    const alerts = view.findAll('[role="alert"]').map((a) => a.text())
    expect(alerts.some((t) => t.startsWith('Could not load the provider defaults:'))).toBe(true)
    expect(alerts.some((t) => t.startsWith('Could not load the model list:'))).toBe(true)
  })
})
