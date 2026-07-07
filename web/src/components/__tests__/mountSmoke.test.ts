// @vitest-environment jsdom

// Mount-smoke tests catch the class of bug that browser-only failures (TDZ
// errors in setup, computed/template throwing on undefined fields) produce.
// Each component is mounted with minimal stubs; the assertion is that setup
// runs without throwing and the first render doesn't crash.
//
// API calls are mocked to return shapes that mirror the real backend
// responses, including some optional fields left undefined, so template
// expressions that forgot to guard against undefined will throw here.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { config, flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => {
  let routineSettings = {
    title_model: '',
    insights_model: '',

    critique_models: '',
    ollama_haiku_model: '',
    ollama_sonnet_model: '',
    ollama_opus_model: '',
    openrouter_haiku_model: '',
    openrouter_sonnet_model: '',
    openrouter_opus_model: '',
    title_model_effective: 'sonnet',
    insights_model_effective: 'haiku',

    critique_models_effective: 'anthropic/claude-sonnet-4.5,anthropic/claude-haiku-4.5',
    alias_tiers: {
      ollama: {
        haiku: 'deepseek-v4-flash:cloud',
        sonnet: 'kimi-k2.7-code:cloud',
        opus: 'minimax-m3:cloud',
      },
      openrouter: {
        haiku: 'anthropic/claude-haiku-4.5',
        sonnet: 'anthropic/claude-sonnet-4.5',
        opus: 'anthropic/claude-opus-4.8',
      },
    },
    transcription: {
      engine: 'local',
      local_model: 'mlx-community/whisper-small',
      local_available: true,
      cloud_available: true,
    },
    model_options: {
      anthropic: ['anthropic/claude-sonnet-4.5', 'anthropic/claude-haiku-4.5'],
      ollama_cloud: ['kimi-k2.7-code:cloud'],
      ollama_local: ['llama3.1:latest'],
      openrouter: ['openai/gpt-5.1'],
    },
    backends: { ollama: true, openrouter: true, anthropic: true },
    workspace_context: {
      workspace_root: '/tmp/workspace',
      vault_root: '/tmp/workspace/memory-vault',
    },
  }
  const responses: Record<string, unknown> = {
    '/api/settings': {},
    '/api/settings/providers': {
      keys: {
        ANTHROPIC_API_KEY: { label: 'Anthropic API key', description: '', configured: true },
        OPENAI_API_KEY: { label: 'OpenAI API key', description: '', configured: false },
      },
      requires_restart: true,
      env_path: '/tmp/workspace/.env',
    },
    '/api/local/status': { git_repo: true, branch: 'main', dirty: false },
    '/api/admin/skills': {
      counts: { custom: 1, github: 1 },
      skills: [
        {
          name: 'airtable-projects',
          label: 'custom',
          source: 'skills/',
          source_type: 'custom',
          description: 'Create Airtable projects',
          content: '# airtable-projects\ncustom skill content',
          installed_targets: ['claude'],
        },
        {
          name: 'brainstorming',
          label: 'github',
          source: 'obra/superpowers',
          source_type: 'github',
          description: 'Explore design before implementation',
          content: '# brainstorming\ngithub skill content',
          installed_targets: ['claude'],
        },
      ],
    },
    '/api/commands': {
      commands: [
        {
          name: 'remember',
          description: 'Store a durable memory',
          argument_hint: '<note>',
          source: 'project',
          path: '/tmp/workspace/.claude/commands/remember.md',
        },
      ],
    },
    '/api/agent-assets': {
      instructions: [
        {
          id: 'claude-code-project-instructions',
          title: 'Claude Code project instructions',
          description: 'Project-local Claude Code instructions loaded by the CLI.',
          source: 'file',
          path: 'CLAUDE.md',
          editable: true,
          content: '# Instructions\n',
        },
        {
          id: 'ciaobot-system-prompt',
          title: 'Ciaobot system prompt append',
          description: 'Generated instructions appended to Claude Code.',
          source: 'generated',
          path: '',
          editable: false,
          content: '# Ciaobot System Instructions\n',
        },
      ],
      subagents: [
        {
          name: 'researcher',
          description: 'Research current external information.',
          source: 'project',
          scope: 'installed',
          path: '.claude/agents/researcher.md',
          editable: false,
          vault_path: '',
          content: '',
        },
      ],
      commands: [
        {
          name: 'remember',
          description: 'Store a durable memory',
          argument_hint: '<note>',
          source: 'project',
          scope: 'installed',
          path: '.claude/commands/remember.md',
          editable: false,
          vault_path: '',
          content: '',
        },
      ],
    },
    '/api/models': { providers: {}, default_provider: 'claude' },
    '/api/projects': [],
    '/api/chats': [],
    '/api/tasks': { tasks: [] },
    '/api/schedules': [],
    '/api/workspaces': {
      workspaces: [],
      active: null,
      provider_options: [
        { value: 'claude', label: 'Claude' },
        { value: 'ollama', label: 'Ollama' },
        { value: 'openrouter', label: 'OpenRouter' },
      ],
    },
  }
  // Default to an empty array — most list endpoints return arrays and a
  // bare `{}` breaks `.reduce`/`.map` calls in stores during the smoke test.
  const get = vi.fn((path: string) => {
    if (path === '/api/settings/routines') return Promise.resolve(routineSettings)
    if (path in responses) return Promise.resolve(responses[path])
    if (path.startsWith('/api/chats/')) return Promise.resolve({})
    return Promise.resolve([])
  })
  const post = vi.fn(() => Promise.resolve({}))
  const patch = vi.fn((path: string, body: Record<string, string>) => {
    if (path === '/api/settings/routines') {
      routineSettings = { ...routineSettings, ...body }
      return Promise.resolve(routineSettings)
    }
    return Promise.resolve({})
  })
  return {
    api: { get, post, patch, del: vi.fn(() => Promise.resolve({})) },
  }
})

vi.mock('../../lib/push', () => ({
  pushSupported: () => false,
  pushEnabled: () => false,
  enablePush: vi.fn(),
  disablePush: vi.fn(),
}))

// Stub heavy/leaf children that aren't relevant to the smoke test. We mock
// the module path because Vue SFCs import siblings directly via ESM, which
// bypasses `config.global.stubs`.
const NoopStub = { name: 'NoopStub', render: () => h('div') }
vi.mock('../VoiceRecorder.vue', () => ({ default: NoopStub }))
vi.mock('../ChatPanel.vue', () => ({ default: NoopStub }))
vi.mock('../SubagentPanel.vue', () => ({ default: NoopStub }))
vi.mock('../PinnedFilePanel.vue', () => ({ default: NoopStub }))
vi.mock('../FileViewerModal.vue', () => ({ default: NoopStub }))
vi.mock('../NewScheduleForm.vue', () => ({ default: NoopStub }))
vi.mock('../SchedulePanel.vue', () => ({ default: NoopStub }))
vi.mock('../NotificationBell.vue', () => ({ default: NoopStub }))
vi.mock('../ProjectSidebar.vue', () => ({ default: NoopStub }))
vi.mock('../InAppToast.vue', () => ({ default: NoopStub }))

const Stub = defineComponent({ render: () => h('div') })

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Stub },
      { path: '/chat/:chatId?', name: 'chat-detail', component: Stub },
      { path: '/project/:projectId', name: 'project', component: Stub },
      { path: '/schedules', name: 'schedules', component: Stub },
      { path: '/settings', name: 'settings', component: Stub },
      { path: '/settings/:tab', name: 'settings-tab', component: Stub },
      { path: '/login', name: 'login', component: Stub },
    ],
  })
}

// `config.global.stubs` only intercepts auto-resolved component names,
// not direct ESM imports like `import ChatPanel from './ChatPanel.vue'`.
// We mock those modules at the top of the file (see `vi.mock` calls below);
// the entries here cover the remaining auto-resolved cases (e.g. Teleport).
beforeEach(() => {
  setActivePinia(createPinia())
  config.global.stubs = {
    Teleport: true,
  }
})

afterEach(() => {
  vi.clearAllMocks()
})

async function mountAndSettle(loader: () => Promise<{ default: unknown }>) {
  const errors: unknown[] = []
  const errorHandler = (err: unknown) => { errors.push(err) }
  const router = makeRouter()
  await router.push('/')
  await router.isReady()

  const mod = await loader()
  const wrapper = mount(mod.default as never, {
    global: {
      plugins: [router],
      config: { errorHandler },
    },
  })
  await flushPromises()
  await nextTick()
  await flushPromises()
  wrapper.unmount()
  return errors
}

describe('component mount smoke', () => {
  it('LoginView mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../LoginView.vue'))
    expect(errors).toEqual([])
  })

  it('ChatLayout mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../ChatLayout.vue'))
    expect(errors).toEqual([])
  })

  it('ChatLayout empty chat view still renders the pane header controls', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()
    const mod = await import('../ChatLayout.vue')
    const wrapper = mount(mod.default as never, {
      global: {
        plugins: [router],
        stubs: { Teleport: true },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('button[aria-label="Open sidebar"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('SettingsView mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../SettingsView.vue'))
    expect(errors).toEqual([])
  })

  it('SettingsView renders skills with custom and github labels on /settings/skills', async () => {
    const router = makeRouter()
    await router.push('/settings/skills')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Skills')
    expect(wrapper.text()).toContain('airtable-projects')
    expect(wrapper.text()).toContain('Custom Skills')
    expect(wrapper.text()).toContain('brainstorming')
    expect(wrapper.text()).toContain('GitHub / Package Skills')
    expect(wrapper.text()).toContain('Commands')
    expect(wrapper.text()).toContain('/remember')
    expect(wrapper.text()).toContain('Store a durable memory')
    wrapper.unmount()
  })

  it('SettingsView saves multiple critique models from the picker', async () => {
    const router = makeRouter()
    await router.push('/settings/models')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // The critique picker is now a searchable ModelSelector.
    const critiqueSelector = wrapper.find('.critique-model-picker .model-selector')
    expect(critiqueSelector.exists()).toBe(true)
    await critiqueSelector.find('.model-selector__trigger').trigger('click')
    await flushPromises()
    await nextTick()

    const localOption = critiqueSelector.findAll('.model-selector__item')
      .find((el) => el.text() === 'llama3.1:latest')
    const anthropicOption = critiqueSelector.findAll('.model-selector__item')
      .find((el) => el.text() === 'anthropic/claude-sonnet-4.5')
    expect(localOption).toBeTruthy()
    expect(anthropicOption).toBeTruthy()

    await localOption!.trigger('click')
    await flushPromises()
    await anthropicOption!.trigger('click')
    await flushPromises()

    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      critique_models: 'llama3.1:latest,anthropic/claude-sonnet-4.5',
    })
    expect(wrapper.text()).toContain('llama3.1:latest')
    expect(wrapper.text()).toContain('anthropic/claude-sonnet-4.5')
    wrapper.unmount()
  })

  it('SettingsView renders configured workspace providers', async () => {
    const router = makeRouter()
    await router.push('/settings/workspaces')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    const addButton = wrapper.findAll('button').find((button) => button.text().includes('Add workspace'))
    expect(addButton).toBeTruthy()
    await addButton!.trigger('click')
    await nextTick()

    const providerOptions = wrapper.findAll('select.routine-input option').map((option) => option.text())
    expect(providerOptions).toContain('Claude')
    expect(providerOptions).toContain('Ollama')
    expect(providerOptions).toContain('OpenRouter')
    wrapper.unmount()
  })

  it('SettingsView saves provider alias tier models', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    const providerSelect = wrapper.find('.alias-provider-select')
    expect(providerSelect.exists()).toBe(true)
    await providerSelect.setValue('ollama')
    await flushPromises()
    await nextTick()

    // The tier model picker is now a searchable ModelSelector.
    const tierSelector = wrapper.findAll('.tier-provider-section .model-selector')
      .find((el) => el.find('.model-selector__trigger').exists())
    expect(tierSelector).toBeTruthy()
    await tierSelector!.find('.model-selector__trigger').trigger('click')
    await flushPromises()
    await nextTick()

    const tierOption = tierSelector!.findAll('.model-selector__item')
      .find((el) => el.attributes('data-model') === 'llama3.1:latest')
    expect(tierOption).toBeTruthy()
    await tierOption!.trigger('click')
    await flushPromises()

    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      ollama_haiku_model: 'llama3.1:latest',
    })
    wrapper.unmount()
  })

  it('SettingsView shows unconfigured tier providers disabled with a hint', async () => {
    // Drive the mock by patching /api/settings/routines: the mock merges the
    // body into the shared routineSettings, so a subsequent GET (which
    // fetchRoutines issues on mount) returns the flipped backends.
    const original = await api.get<Record<string, unknown>>('/api/settings/routines')
    const originalBackends = original.backends as Record<string, boolean>
    await api.patch('/api/settings/routines', {
      backends: { ollama: false, openrouter: false, anthropic: true },
    })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    try {
      const providerSelect = wrapper.find('.alias-provider-select')
      // Ollama and OpenRouter are still listed (not hidden), marked unconfigured.
      const labels = providerSelect.findAll('option').map((option) => option.text())
      expect(labels.some((l) => l.includes('Ollama') && l.includes('not configured'))).toBe(true)
      expect(labels.some((l) => l.includes('OpenRouter') && l.includes('not configured'))).toBe(true)

      // Select OpenRouter: tier ModelSelectors render disabled, hint shown.
      await providerSelect.setValue('openrouter')
      await flushPromises()
      await nextTick()
      const tierSelectors = wrapper.findAll('.tier-provider-section .model-selector')
      expect(tierSelectors.length).toBe(3)
      for (const selector of tierSelectors) {
        expect(selector.find('.model-selector__trigger').attributes('disabled')).toBeDefined()
      }
      const hint = wrapper.find('.tier-provider-note')
      expect(hint.exists()).toBe(true)
      expect(hint.text().toLowerCase()).toContain('openrouter api key')
    } finally {
      await api.patch('/api/settings/routines', { backends: originalBackends })
      wrapper.unmount()
    }
  })

  it('SettingsView saves routine models by provider and tier', async () => {
    const router = makeRouter()
    await router.push('/settings/models')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // title_model (0) and insights_model (1) each carry a provider select;
    // the title tier select is hidden for tier-less providers (apple/automatic),
    // so scope the tier lookup to the insights block rather than a fixed index.
    const controls = wrapper.findAll('.routine-model-controls')
    const providerSelects = wrapper.findAll('.routine-model-controls .routine-select--provider')
    expect(providerSelects.length).toBeGreaterThanOrEqual(2)
    const insightsControls = controls[1]

    await providerSelects[1].setValue('openrouter')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      insights_model: 'anthropic/claude-haiku-4.5',
    })

    const insightsTier = insightsControls.find('.routine-select--tier')
    expect(insightsTier.exists()).toBe(true)
    await insightsTier.setValue('opus')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      insights_model: 'anthropic/claude-opus-4.8',
    })
    wrapper.unmount()
  })

  it('ProjectView mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../ProjectView.vue'))
    expect(errors).toEqual([])
  })

  it('SchedulesView mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../SchedulesView.vue'))
    expect(errors).toEqual([])
  })
})
