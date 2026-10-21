// @vitest-environment jsdom
// Settings → Models offers the same model pickers as the chat composer, so
// a model reads the same everywhere: a readable name, the raw id in the
// option's data and tooltip, and a badge from the namespace the id itself
// states.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { modelDisplayName } from '../../lib/modelLabels'
import { sectionsFromModelsResponse } from '../../lib/modelSections'
import type { ModelsResponse } from '../../lib/types'

// A namespaced id, the shape opencode's catalog carries.
const NAMESPACED = 'openai/gpt-5.6-luna'

const state = vi.hoisted(() => ({
  models: null as unknown,
  routines: null as unknown,
}))

vi.mock('../../lib/api', () => {
  const get = vi.fn((rawPath: string) => {
    // Keyed by route, so a query string must not miss the fixture.
    const path = rawPath.split('?')[0]
    if (path === '/api/models') return Promise.resolve(state.models)
    if (path === '/api/settings/routines') return Promise.resolve(state.routines)
    if (path === '/api/settings/providers') {
      return Promise.resolve({
        connections: {
          claude: {
            name: 'claude',
            ok: true,
            auth: 'oauth',
            command: 'claude auth login',
            version: '2.1.205 (Claude Code)',
            label: 'Claude Code',
          },
          opencode: {
            name: 'opencode',
            ok: true,
            auth: 'oauth',
            command: 'opencode auth login',
            version: '2.0.16',
            label: 'opencode',
          },
        },
      })
    }
    if (path === '/api/workspaces') {
      return Promise.resolve({ workspaces: [], active: null, provider_options: [] })
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

const MODELS: ModelsResponse = {
  models: ['haiku', 'sonnet', 'opus'],
  default: 'sonnet',
  provider_models: { claude: ['haiku', 'sonnet', 'opus'], opencode: [NAMESPACED] },
  provider_defaults: { claude: 'sonnet', opencode: NAMESPACED },
  opencode_models: [NAMESPACED],
  backends: { claude: true, opencode: true },
  thinking_levels: {},
}

const ROUTINES = {
  model_options: { anthropic: ['haiku', 'sonnet', 'opus'] },
  provider_default_models: {},
}

const Stub = defineComponent({ render: () => h('div') })

beforeEach(() => {
  setActivePinia(createPinia())
  state.models = MODELS
  state.routines = ROUTINES
  vi.stubGlobal('IntersectionObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function mountModelsTab(): Promise<VueWrapper> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/settings', component: Stub },
      { path: '/settings/:tab', component: Stub },
    ],
  })
  await router.push('/settings/models')
  await router.isReady()
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

/** One provider's row in the chat providers card. */
function providerRow(wrapper: VueWrapper, label: string) {
  const row = wrapper.findAll('.credential-row').find(
    (el) => el.find('.set-row-title').text() === label,
  )
  expect(row, `no provider row for ${label}`).toBeTruthy()
  return row!
}

/** The "Default model" picker of a provider row. */
function defaultModelTrigger(wrapper: VueWrapper, label: string) {
  const subrow = providerRow(wrapper, label).findAll('.set-subrow').find(
    (el) => el.find('.set-subrow-label').text() === 'Default model',
  )
  expect(subrow, `no default model row for ${label}`).toBeTruthy()
  return subrow!.get('.model-selector__trigger')
}

/** Open a provider's default model picker and return its options. */
async function openDefaultModelPicker(wrapper: VueWrapper, label: string) {
  await defaultModelTrigger(wrapper, label).trigger('click')
  await flushPromises()
  await nextTick()
  return wrapper.findAll('.model-selector__item')
}

function optionFor(items: ReturnType<VueWrapper['findAll']>, model: string) {
  const option = items.find((el) => el.attributes('data-model') === model)
  expect(option, `no option for ${model}`).toBeTruthy()
  return option!
}

describe('Settings → Models model names', () => {
  it('names a namespaced model the way the chat picker does', async () => {
    const wrapper = await mountModelsTab()
    try {
      const items = await openDefaultModelPicker(wrapper, 'opencode')
      const option = optionFor(items, NAMESPACED)

      expect(option.find('.model-selector__item-label').text()).toBe('GPT-5.6 Luna')
      // The same id reads the same in the chat picker, which builds its
      // sections from the same helper.
      const chat = sectionsFromModelsResponse(MODELS)
      const chatOpencode = chat.find((section) => section.key === 'opencode')
      expect(option.find('.model-selector__item-label').text()).toBe(
        chatOpencode?.modelLabels?.[NAMESPACED],
      )
      expect(modelDisplayName(NAMESPACED)).toBe('GPT-5.6 Luna')
    } finally {
      wrapper.unmount()
    }
  })

  it('keeps the raw id in the option data and tooltip', async () => {
    const wrapper = await mountModelsTab()
    try {
      const items = await openDefaultModelPicker(wrapper, 'opencode')
      const option = optionFor(items, NAMESPACED)

      expect(option.attributes('data-model')).toBe(NAMESPACED)
      expect(option.attributes('title')).toBe(NAMESPACED)
    } finally {
      wrapper.unmount()
    }
  })

  it('badges the provider namespace the id itself states', async () => {
    const wrapper = await mountModelsTab()
    try {
      const items = await openDefaultModelPicker(wrapper, 'opencode')
      const option = optionFor(items, NAMESPACED)

      expect(option.findAll('.model-selector__item-badge').map((el) => el.text())).toEqual([
        'openai',
      ])
      // The namespace is the id's own, never an inferred vendor.
      expect(option.text()).not.toContain('OpenAI')
    } finally {
      wrapper.unmount()
    }
  })

  it('keeps the Default entry reading Automatic, with the id it resolves to', async () => {
    const wrapper = await mountModelsTab()
    try {
      const items = await openDefaultModelPicker(wrapper, 'opencode')
      const automatic = items.find((el) => el.attributes('data-section') === 'default')
      expect(automatic, 'no Default entry').toBeTruthy()

      expect(automatic!.find('.model-selector__item-label').text()).toBe('Automatic (GPT-5.6 Luna)')
      // The technical id Automatic resolves to is still there beside it.
      expect(automatic!.findAll('.model-selector__item-badge').map((el) => el.text())).toEqual([
        NAMESPACED,
      ])
      // And the closed picker says the same on its trigger.
      expect(defaultModelTrigger(wrapper, 'opencode').text()).toContain('Automatic (GPT-5.6 Luna)')
    } finally {
      wrapper.unmount()
    }
  })

  it('leaves a tier alias without a provider badge', async () => {
    const wrapper = await mountModelsTab()
    try {
      const items = await openDefaultModelPicker(wrapper, 'Claude Code')
      const option = optionFor(items, 'sonnet')

      expect(option.find('.model-selector__item-label').text()).toBe('Sonnet')
      expect(option.findAll('.model-selector__item-badge')).toHaveLength(0)
    } finally {
      wrapper.unmount()
    }
  })
})
