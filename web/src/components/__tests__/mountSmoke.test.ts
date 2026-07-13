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
    ollama_fable_model: '',
    openrouter_haiku_model: '',
    openrouter_sonnet_model: '',
    openrouter_opus_model: '',
    openrouter_fable_model: '',
    codex_haiku_model: '',
    codex_sonnet_model: '',
    codex_opus_model: '',
    codex_fable_model: '',
    title_model_effective: 'sonnet',
    insights_model_effective: 'haiku',

    critique_models_effective: 'anthropic/claude-sonnet-4.5,anthropic/claude-haiku-4.5',
    tier_defaults: {
      ollama: {
        haiku: 'deepseek-v4-flash:cloud',
        sonnet: 'kimi-k2.7-code:cloud',
        opus: 'minimax-m3:cloud',
        fable: 'glm-5.2:cloud',
      },
      openrouter: {
        haiku: 'anthropic/claude-haiku-4.5',
        sonnet: 'anthropic/claude-sonnet-4.5',
        opus: 'anthropic/claude-opus-4.8',
        fable: 'anthropic/claude-fable-latest',
      },
    },
    alias_tiers: {
      ollama: {
        haiku: 'deepseek-v4-flash:cloud',
        sonnet: 'kimi-k2.7-code:cloud',
        opus: 'minimax-m3:cloud',
        fable: 'glm-5.2:cloud',
      },
      openrouter: {
        haiku: 'anthropic/claude-haiku-4.5',
        sonnet: 'anthropic/claude-sonnet-4.5',
        opus: 'anthropic/claude-opus-4.8',
        fable: 'anthropic/claude-fable-latest',
      },
    },
    transcription: {
      engine: 'local',
      local_model: 'mlx-community/whisper-small',
      local_available: true,
      cloud_available: true,
    },
    speech: {
      engine: 'cloud',
      cloud_voice: 'nova',
      local_voice: 'af_heart',
      local_available: false,
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
        CIAO_OLLAMA_API_KEY: { label: 'Ollama Cloud API key', description: '', configured: true },
      },
      service_keys: {
        OPENAI_API_KEY: {
          label: 'OpenAI voice API key',
          description: 'Used directly by Ciaobot for cloud transcription and speech, not for Codex login.',
          configured: false,
        },
      },
      connections: {
        claude: {
          name: 'claude',
          ok: true,
          auth: 'oauth',
          command: 'ciao auth claude',
          version: '2.1.205 (Claude Code)',
          account: 'person@example.com',
          protocol: 'Agent SDK ready',
        },
        codex: {
          name: 'codex',
          ok: true,
          auth: 'chatgpt',
          command: 'ciao auth codex',
          version: 'codex-cli 0.144.0-alpha.4',
          account: 'ChatGPT account',
          protocol: 'app-server protocol compatible',
        },
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
      context: [
        {
          id: 'claude-code-project-instructions',
          title: 'Project CLAUDE.md',
          description: 'Project-local Claude Code instructions loaded by the CLI.',
          source: 'file',
          path: 'CLAUDE.md',
          editable: true,
          content: '# Instructions\n',
          scope: 'project',
          provider: 'claude',
        },
        {
          id: 'claude-import-rtk',
          title: 'Import: RTK.md',
          description: 'Imported by Claude Code global instructions.',
          source: 'file-import',
          path: '/tmp/.claude/RTK.md',
          editable: false,
          content: '# Claude-only instructions\n',
          scope: 'import',
          provider: 'claude',
        },
        {
          id: 'codex-project-instructions',
          title: 'Project AGENTS.md',
          description: 'Project-local Codex instructions loaded by the CLI.',
          source: 'file',
          path: 'AGENTS.md',
          editable: true,
          content: '# Codex instructions\n',
          scope: 'project',
          provider: 'codex',
        },
        {
          id: 'ciaobot-system-prompt',
          title: 'Ciaobot system prompt append',
          description: 'Generated instructions appended for both providers.',
          source: 'generated',
          path: '',
          editable: false,
          content: '# Ciaobot System Instructions\n',
          scope: 'generated',
          provider: 'shared',
        },
        {
          id: 'ciaobot-memory',
          title: 'Agent memory',
          description: 'Bounded memory injected at session start.',
          source: 'file',
          path: '/tmp/.ciao/memory.md',
          editable: false,
          content: 'Prefer concise answers.\n',
          scope: 'bounded-memory',
          provider: 'shared',
        },
        {
          id: 'ciaobot-user',
          title: 'User profile',
          description: 'Bounded user profile injected at session start.',
          source: 'file',
          path: '/tmp/.ciao/user.md',
          editable: false,
          content: 'Name: Ada\n',
          scope: 'bounded-memory',
          provider: 'shared',
        },
        {
          id: 'workspace-memory-personal',
          title: 'Workspace memory (personal)',
          description: 'Durable personal workspace memory.',
          source: 'file',
          path: 'memory-vault/personal/MEMORY.md',
          editable: true,
          content: '# Personal memory\n',
          scope: 'vault',
          provider: 'shared',
        },
        {
          id: 'workspace-memory-work',
          title: 'Workspace memory (work)',
          description: 'Durable work workspace memory.',
          source: 'file',
          path: 'memory-vault/work/MEMORY.md',
          editable: true,
          content: '# Work memory\n',
          scope: 'vault',
          provider: 'shared',
        },
        {
          id: 'runtime-context-hook',
          title: 'Per-turn runtime context hook',
          description: 'Project context and runtime details sent with each turn.',
          source: 'generated',
          path: '',
          editable: false,
          content: '<ciao-runtime>\nworkspace=personal\n</ciao-runtime>',
          scope: 'generated',
          provider: 'shared',
        },
        {
          id: 'memory-proposals',
          title: 'Memory proposals',
          description: 'Not injected.',
          source: 'proposal-queue',
          path: 'memory-vault/Workspace/Memory-Proposals.md',
          editable: true,
          content: '- [memory] proposal\n',
          scope: 'review',
          provider: 'shared',
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
    '/api/models': {
      models: ['haiku', 'sonnet', 'opus', 'fable'],
      default: 'sonnet',
      provider_models: {
        codex: ['gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol'],
      },
      provider_defaults: { codex: 'gpt-5.6-terra' },
      codex_models: ['gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol'],
      alias_tiers: {
        codex: {
          haiku: 'gpt-5.6-luna',
          sonnet: 'gpt-5.6-terra',
          opus: 'gpt-5.6-sol',
          fable: 'gpt-5.6-sol',
        },
      },
      codex_tier_defaults: {
        haiku: 'gpt-5.6-luna',
        sonnet: 'gpt-5.6-terra',
        opus: 'gpt-5.6-sol',
        fable: 'gpt-5.6-sol',
      },
      backends: { codex: true },
    },
    '/api/projects': [],
    '/api/chats': [],
    '/api/tasks': { tasks: [] },
    '/api/schedules': [],
    '/api/workspaces': {
      workspaces: [],
      active: null,
      provider_options: [
        { value: 'claude', label: 'Anthropic (via Claude Code)' },
        { value: 'codex', label: 'OpenAI (via Codex)' },
        { value: 'ollama', label: 'Ollama (via Claude Code)' },
        { value: 'openrouter', label: 'OpenRouter (via Claude Code)' },
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

  it('ProductTour mounts without throwing', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()
    const pinia = createPinia()
    setActivePinia(pinia)
    const mod = await import('../ProductTour.vue')
    const wrapper = mount(mod.default as never, {
      global: {
        plugins: [pinia, router],
        stubs: { Teleport: true },
      },
    })
    await flushPromises()
    wrapper.unmount()
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

  it('SettingsView explains the generic context recipe for each CLI', async () => {
    const router = makeRouter()
    await router.push('/settings/context')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    const visibleContextRows = () => wrapper.findAll('.skill-list > .instruction-row')
      .map((row) => row.text())
      .join('\n')

    expect(wrapper.findAll('.memory-context-row')).toHaveLength(1)
    expect(wrapper.findAll('.memory-source')).toHaveLength(3)
    expect(wrapper.text()).toContain('independent of the current chat, project, and workspace')
    expect(wrapper.text()).toContain('Global session memory is appended at chat start')
    expect(wrapper.text()).toContain('Global · included automatically at chat start')
    expect(wrapper.text()).toContain('Workspace-specific · opened only when relevant')
    expect(wrapper.text()).toContain('Global remembered facts')
    expect(wrapper.text()).toContain('Global user profile')
    expect(wrapper.text()).toContain('Workspace notes (MEMORY.md)')
    expect(wrapper.findAll('.memory-source-summary-copy').map((row) => row.text())).toEqual([
      'Cross-session facts, conventions, and lessons shared across all workspaces.',
      'Your identity and response preferences, shared across all workspaces.',
      'Durable notes from whichever workspace the chat uses. This file is not inserted automatically.',
    ])
    expect(wrapper.findAll('.memory-source-badges').map((row) => row.text())).toEqual([
      'session startautomatically generatednot editable',
      'session startautomatically generatednot editable',
      'on demandautomatically generatednot editable',
    ])
    expect(wrapper.findAll('.memory-source-file .inline-path-button').map((row) => row.text())).toEqual([
      '/tmp/.ciao/memory.md',
      '/tmp/.ciao/user.md',
      'memory-vault/personal/MEMORY.md',
      'memory-vault/work/MEMORY.md',
    ])
    expect(wrapper.text()).not.toContain('3 sources')
    expect(visibleContextRows()).toContain('CLI instructions (CLAUDE.md · AGENTS.md)')
    expect(wrapper.findAll('.context-provider-toggle')).toHaveLength(0)
    expect(wrapper.findAll('.skill-list > .instruction-row .skill-name').map((row) => row.text())).toEqual([
      'CLI instructions (CLAUDE.md · AGENTS.md)',
      'Ciaobot system instructions',
      'Memory sources',
      'Per-turn runtime context hook',
    ])
    expect(wrapper.text()).not.toContain('Review queue')
    expect(wrapper.text()).not.toContain('Memory proposals')
    expect(wrapper.text()).toContain('memory-vault/personal/MEMORY.md')
    expect(wrapper.text()).toContain('memory-vault/work/MEMORY.md')
    expect(wrapper.text()).not.toContain('Name: Ada')
    expect(wrapper.text()).not.toContain('Import: RTK.md')
    expect(wrapper.text()).not.toContain('Imported by Claude Code global instructions')

    const instructionRow = wrapper.findAll('.skill-list > .instruction-row')
      .find((row) => row.text().includes('CLI instructions (CLAUDE.md · AGENTS.md)'))
    expect(instructionRow).toBeTruthy()
    expect(instructionRow!.text()).toContain('editable')
    expect(instructionRow!.text()).toContain('CLAUDE.md and AGENTS.md are linked')
    await instructionRow!.trigger('click')
    await nextTick()
    // Single row: AGENTS.md is linked to CLAUDE.md, one link covers both.
    expect(instructionRow!.findAll('.inline-path-button').map((button) => button.text())).toEqual([
      'CLAUDE.md / AGENTS.md',
    ])
    expect(instructionRow!.text()).toContain('AGENTS.md is linked to CLAUDE.md, so every CLI reads the same instructions.')

    const systemRow = wrapper.findAll('.skill-list > .instruction-row')
      .find((row) => row.text().includes('Ciaobot system instructions'))
    expect(systemRow).toBeTruthy()
    expect(systemRow!.text()).toContain('not editable')
    await systemRow!.trigger('click')
    await nextTick()
    expect(systemRow!.find('.inline-path-button').text()).toBe('ciao/system_prompt.md')

    expect(visibleContextRows()).not.toContain('RTK.md')

    const runtimeRow = wrapper.findAll('.skill-list > .instruction-row')
      .find((row) => row.text().includes('Per-turn runtime context hook'))
    expect(runtimeRow).toBeTruthy()
    await runtimeRow!.trigger('click')
    await nextTick()
    expect(runtimeRow!.text()).toContain('Project context:')
    expect(runtimeRow!.text()).toContain('Project document:')
    expect(runtimeRow!.text()).toContain('README.md or canonical document')
    expect(runtimeRow!.text()).not.toContain('<ciao-runtime>')
    wrapper.unmount()
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
    expect(providerOptions).toContain('Anthropic (via Claude Code)')
    expect(providerOptions).toContain('OpenAI (via Codex)')
    expect(providerOptions).toContain('Ollama (via Claude Code)')
    expect(providerOptions).toContain('OpenRouter (via Claude Code)')
    expect(wrapper.findAll('select.workspace-select')).toHaveLength(3)

    expect(wrapper.find('[aria-label="Claude.ai MCPs"]').exists()).toBe(true)
    const providerField = wrapper.findAll('label.settings-field')
      .find((field) => field.find('.ws-label').text() === 'Provider')
    expect(providerField).toBeTruthy()
    await providerField!.find('select').setValue('codex')
    await nextTick()
    expect(wrapper.find('[aria-label="Claude.ai MCPs"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('SettingsView shows the OpenAI voice key without provider protocol labels', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    const voiceKeyRow = wrapper.findAll('.credential-row')
      .find((row) => row.text().includes('OpenAI voice API key'))
    expect(voiceKeyRow).toBeTruthy()
    expect(voiceKeyRow!.text()).toContain('cloud transcription and speech')
    expect(voiceKeyRow!.find('input[type="password"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('Agent SDK ready')
    expect(wrapper.text()).not.toContain('app-server protocol compatible')
    expect(wrapper.text()).not.toContain('connection and protocol')
    wrapper.unmount()
  })

  it('SettingsView shows OpenAI routing and saves configurable tier routes', async () => {
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
    expect(providerSelect.classes()).toContain('routine-select')
    expect(providerSelect.findAll('option').map((option) => option.text())).toEqual([
      'OpenAI (via Codex)',
      'Ollama (via Claude Code)',
      'OpenRouter (via Claude Code)',
    ])
    expect(wrapper.text()).toContain('Model Routing')
    expect(wrapper.text()).not.toContain('Claude Code model routing')
    // Codex tiers are editable pins whose default reflects the automatic
    // catalog mapping.
    const codexSelectors = wrapper.findAll('.tier-provider-section .model-selector')
    expect(codexSelectors.map((selector) => selector.find('.model-selector__trigger').text())).toEqual([
      'Automatic (gpt-5.6-luna)▾',
      'Automatic (gpt-5.6-terra)▾',
      'Automatic (gpt-5.6-sol)▾',
      'Automatic (gpt-5.6-sol)▾',
    ])
    await codexSelectors[0]!.find('.model-selector__trigger').trigger('click')
    await flushPromises()
    const codexOption = codexSelectors[0]!.findAll('.model-selector__item')
      .find((el) => el.attributes('data-model') === 'gpt-5.6-terra')
    expect(codexOption).toBeTruthy()
    await codexOption!.trigger('click')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      codex_haiku_model: 'gpt-5.6-terra',
    })
    expect(wrapper.find('.routing-model-catalog summary').text()).toBe('Available OpenAI models (3)')
    expect(wrapper.findAll('.routing-model-catalog code').map((model) => model.text())).toEqual([
      'gpt-5.6-luna',
      'gpt-5.6-terra',
      'gpt-5.6-sol',
    ])
    expect(wrapper.find('.routing-model-catalog').text()).toContain('Haiku')
    expect(wrapper.find('.routing-model-catalog').text()).toContain('Sonnet')
    expect(wrapper.find('.routing-model-catalog').text()).toContain('Opus')
    expect(wrapper.find('.routing-model-catalog').text()).toContain('Fable')

    await providerSelect.setValue('ollama')
    await flushPromises()
    await nextTick()

    const tierSelectors = wrapper.findAll('.tier-provider-section .model-selector')
    expect(tierSelectors.map((selector) => selector.find('.model-selector__trigger').text())).toEqual([
      'Default (deepseek-v4-flash:cloud)▾',
      'Default (kimi-k2.7-code:cloud)▾',
      'Default (minimax-m3:cloud)▾',
      'Default (glm-5.2:cloud)▾',
    ])

    // Each searchable picker includes an explicit default option.
    const tierSelector = tierSelectors[0]
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

    await tierSelector.find('.model-selector__trigger').trigger('click')
    await flushPromises()
    const defaultOption = tierSelector.findAll('.model-selector__item')
      .find((el) => el.text() === 'Default (deepseek-v4-flash:cloud)')
    expect(defaultOption).toBeTruthy()
    await defaultOption!.trigger('click')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      ollama_haiku_model: '',
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
      expect(tierSelectors.length).toBe(4)
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
