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
    insights_model: '',

    critique_models: '',
    insights_model_effective: 'haiku',

    critique_models_effective: 'anthropic/claude-sonnet-4.5,anthropic/claude-haiku-4.5',

    transcription: {
      engine: 'local',
      cloud_model: 'gpt-transcribe',
      locale: 'en-US',
      local_available: true,
      local_unavailable_reason: '',
      cloud_available: true,
    },
    speech: {
      engine: 'cloud',
      cloud_voice: 'nova',
      local_voice: '',
      local_available: true,
      local_voices: [
        { id: 'com.apple.voice.compact.en-US.Samantha', name: 'Samantha', locale: 'en-US', quality: 'default' },
      ],
      cloud_available: true,
    },
    model_options: {
      anthropic: ['haiku', 'sonnet', 'opus', 'fable'],
    },
    backends: { anthropic: true },
    workspace_context: {
      workspace_root: '/tmp/workspace',
      vault_root: '/tmp/workspace/memory-vault',
    },
  }
  const responses: Record<string, unknown> = {
    '/api/settings': {},
    '/api/settings/providers': {
      keys: {},
      service_keys: {
        OPENAI_API_KEY: {
          label: 'OpenAI voice API key',
          description: 'Used directly by Ciaobot for cloud transcription and speech.',
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
          label: 'Claude Code',
          short_label: 'Claude',
        },
        opencode: {
          name: 'opencode',
          ok: false,
          auth: 'login_required',
          command: 'opencode auth login',
          version: '1.18.18',
          detail: 'login required',
          label: 'opencode',
          short_label: 'opencode',
        },
      },
      requires_restart: true,
      env_path: '/tmp/workspace/.env',
    },
    '/api/local/status': { git_repo: true, branch: 'main', dirty: false },
    '/api/admin/skills': {
      counts: { custom: 1, github: 1, stock: 0 },
      skills: [
        {
          name: 'airtable-projects',
          label: 'custom',
          source: 'skills/',
          source_type: 'custom',
          description: 'Create Airtable projects',
          path: 'skills/airtable-projects/SKILL.md',
          content: '# airtable-projects\ncustom skill content',
          installed_targets: ['claude'],
        },
        {
          name: 'brainstorming',
          label: 'github',
          source: 'example-org/skill-pack',
          source_type: 'github',
          description: 'Explore design before implementation',
          path: '.claude/skills/brainstorming/SKILL.md',
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
          source: 'ciaobot',
          scope: 'built-in',
          path: 'commands/remember.md',
          editable: true,
          vault_path: '',
          content: '',
        },
        {
          name: 'summarize-decision',
          description: 'A user-authored slash command',
          argument_hint: '<notes>',
          source: 'workspace',
          scope: 'custom',
          path: 'commands/summarize-decision.md',
          editable: true,
          vault_path: '',
          content: '',
        },
      ],
    },
    '/api/models': {
      models: ['haiku', 'sonnet', 'opus', 'fable'],
      default: 'sonnet',
      provider_models: {
        claude: ['haiku', 'sonnet', 'opus', 'fable'],
        opencode: ['opus', 'sonnet', 'haiku'],
      },
      provider_defaults: { claude: 'sonnet', opencode: 'opus' },
      opencode_models: ['opus', 'sonnet', 'haiku'],
      backends: { opencode: true },
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
        { value: 'opencode', label: 'opencode' },
      ],
    },
    '/api/automation': [
      {
        job: 'insights',
        label: 'Session insights',
        category: 'content',
        description: 'Extracts durable insights from an archived session transcript.',
        uses_model: true,
        produces_outcome: true,
        trigger: 'When a chat is archived.',
        schedule_id: '',
        one_time: false,
        last_run: {
          job: 'insights',
          label: 'Session insights',
          category: 'content',
          started_at: '2026-08-03T20:00:00+00:00',
          ended_at: '2026-08-03T20:06:14+00:00',
          duration_ms: 374000,
          status: 'error',
          model: 'deepseek-v4-flash:cloud',
          provider: 'claude',
          error: 'TimeoutError',
          extra: {},
        },
        recent: [],
        stats: {
          total_runs: 4,
          success_rate: 0.25,
          avg_duration_ms: 300000,
          last_error: { error: 'TimeoutError', ts: '2026-08-03T20:06:14+00:00' },
        },
        sub_jobs: [
          {
            job: 'backfill_insights',
            label: 'Insights backfill',
            category: 'system',
            description: 'Runs session insights over every archive that is missing them.',
            trigger: 'On server startup, and on demand from this page.',
            last_run: null,
            recent: [],
            stats: { total_runs: 0, success_rate: null, avg_duration_ms: 0, last_error: null },
          },
        ],
      },
      {
        job: 'title',
        label: 'Title generation',
        category: 'content',
        description: 'Names a chat from its first message.',
        uses_model: true,
        produces_outcome: true,
        trigger: 'When a new chat gets its first message.',
        schedule_id: '',
        one_time: false,
        last_run: {
          job: 'title',
          label: 'Title generation',
          category: 'content',
          started_at: '2026-08-04T09:38:40+00:00',
          ended_at: '2026-08-04T09:38:45+00:00',
          duration_ms: 5000,
          status: 'ok',
          model: 'haiku',
          provider: 'claude',
          error: null,
          extra: {},
        },
        recent: [],
        stats: { total_runs: 9, success_rate: 1, avg_duration_ms: 5000, last_error: null },
      },
      {
        job: 'memory_migration',
        label: 'Legacy memory migration',
        category: 'system',
        description: 'One-time move of legacy memory files into the CLAUDE.md memory regions.',
        uses_model: false,
        produces_outcome: true,
        trigger: 'Once, on the first skills sync after upgrading.',
        schedule_id: '',
        one_time: true,
        last_run: {
          job: 'memory_migration',
          label: 'Legacy memory migration',
          category: 'system',
          started_at: '2026-08-03T16:44:54+00:00',
          ended_at: '2026-08-03T16:44:54+00:00',
          duration_ms: 12,
          status: 'ok',
          model: '',
          provider: '',
          error: null,
          extra: {},
        },
        recent: [],
        stats: { total_runs: 1, success_rate: 1, avg_duration_ms: 12, last_error: null },
      },
    ],
  }
  // Default to an empty array — most list endpoints return arrays and a
  // bare `{}` breaks `.reduce`/`.map` calls in stores during the smoke test.
  const get = vi.fn((rawPath: string) => {
    // Keyed by route, so a query string (e.g. `/api/models?refresh=1`) must not
    // miss the fixture the way a real server would not serve a different route.
    const path = rawPath.split('?')[0]
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
  const setResponse = (path: string, value: unknown) => {
    responses[path] = value
  }
  const getResponse = (path: string) => responses[path]
  return {
    api: { get, post, patch, del: vi.fn(() => Promise.resolve({})), setResponse, getResponse },
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
// `__esModule: true` on the destination stubs is load-bearing: ChatLayout
// imports those four with defineAsyncComponent, and Vue only unwraps a
// resolved module's `default` when it can recognise the object as an ES
// module. Without the flag it hands the whole mock namespace to the renderer
// as the component.
const AsyncNoopStub = { default: NoopStub, __esModule: true }
vi.mock('../VoiceRecorder.vue', () => ({ default: NoopStub }))
vi.mock('../ChatPanel.vue', () => ({ default: NoopStub }))
vi.mock('../SubagentPanel.vue', () => ({ default: NoopStub }))
vi.mock('../PinnedFilePanel.vue', () => ({ default: NoopStub }))
vi.mock('../FileViewerModal.vue', () => ({ default: NoopStub }))
vi.mock('../NewScheduleForm.vue', () => ({ default: NoopStub }))
vi.mock('../SchedulePanel.vue', () => AsyncNoopStub)
const MemoryMapStub = { name: 'MemoryMapStub', render: () => h('div', { 'data-testid': 'memory-map-stub' }) }
vi.mock('../MemoryMapView.vue', () => ({ default: MemoryMapStub, __esModule: true }))
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
      { path: '/memory', name: 'memory', component: Stub },
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

  it('ChatLayout renders the memory view at /memory', async () => {
    const router = makeRouter()
    await router.push('/memory')
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

    expect(wrapper.find('[data-testid="memory-map-stub"]').exists()).toBe(true)
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
    expect(wrapper.text()).toContain('custom skills')
    expect(wrapper.text()).toContain('brainstorming')
    expect(wrapper.text()).toContain('github / package skills')
    wrapper.unmount()
  })

  it('SettingsView renders the notifications card on /settings/notifications', async () => {
    const router = makeRouter()
    await router.push('/settings/notifications')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('notifications')
    expect(wrapper.text()).toContain('Get a notification when a chat replies')
    wrapper.unmount()
  })

  it('SettingsView keeps subagents and commands on separate settings pages', async () => {
    const router = makeRouter()
    await router.push('/settings/subagents')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('researcher')
    expect(wrapper.text()).not.toContain('/remember')
    expect(wrapper.text()).not.toContain('+ New command')
    wrapper.unmount()
  })

  it('SettingsView labels an unmodified stock command as Built-in on /settings/commands', async () => {
    const router = makeRouter()
    await router.push('/settings/commands')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    const rememberRow = wrapper.findAll('.command-row').find((row) => row.text().includes('/remember'))
    expect(rememberRow).toBeTruthy()
    expect(rememberRow!.text()).toContain('Built-in')

    const customRow = wrapper.findAll('.command-row').find((row) => row.text().includes('/summarize-decision'))
    expect(customRow).toBeTruthy()
    expect(customRow!.text()).toContain('Custom')
    wrapper.unmount()
  })

  it('SettingsView leads the automations tab with what is broken', async () => {
    const router = makeRouter()
    await router.push('/settings/automations')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // The headline answers "is anything broken?" without expanding a row.
    expect(wrapper.text()).toContain('1 of 2 automations needs attention: Session insights.')
    // Failing row explains itself: when it runs, and what went wrong.
    const failing = wrapper.findAll('.automation-row--error')
    expect(failing).toHaveLength(1)
    expect(failing[0].text()).toContain('When a chat is archived.')
    expect(failing[0].text()).toContain('TimeoutError')
    // The old separate "Insights backfill" row is gone; it is this row's action.
    expect(wrapper.text()).not.toContain('Insights backfill —')
    expect(failing[0].find('.btn-run').text()).toBe('Run for all sessions')
    // A settled one-time migration is folded away, not presented as live work.
    expect(wrapper.find('.automation-settled').text()).toContain('Legacy memory migration')
    wrapper.unmount()
  })

  it('SettingsView retries failing insights with a different model', async () => {
    const router = makeRouter()
    await router.push('/settings/automations')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    const failing = wrapper.find('.automation-row--error')
    const select = failing.find('select')
    expect(select.exists()).toBe(true)
    // Default keeps the configured model; options are concrete model ids.
    expect(select.findAll('option')[0].text()).toContain('Configured')
    await select.setValue('opus')
    await failing.find('.btn-run').trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith(
      '/api/automation/backfill-insights',
      { model: 'opus' },
    )
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

    // The panel is one voice per vendor: a bare tier is Anthropic, a prefixed
    // entry routes to that provider's app-server.
    const opusOption = critiqueSelector.findAll('.model-selector__item')
      .find((el) => el.text() === 'opus')
    const opencodeOption = critiqueSelector.findAll('.model-selector__item')
      .find((el) => el.text() === 'opencode:opus')
    expect(opusOption).toBeTruthy()
    expect(opencodeOption).toBeTruthy()

    await opusOption!.trigger('click')
    await flushPromises()
    await opencodeOption!.trigger('click')
    await flushPromises()

    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      critique_models: 'opus,opencode:opus',
    })
    expect(wrapper.text()).toContain('opencode:opus')
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
    expect(providerOptions).toContain('opencode')
    // Provider and GWS profile are native selects; Default model uses the
    // custom ModelSelector component, not a third native <select>.
    expect(wrapper.findAll('select.workspace-select')).toHaveLength(2)

    const providerField = wrapper.findAll('label.settings-field')
      .find((field) => field.find('.ws-label').text() === 'Agent CLI/Runtime')
    expect(providerField).toBeTruthy()
    await providerField!.find('select').setValue('opencode')
    await nextTick()
    wrapper.unmount()
  })

  it('keeps a legacy workspace provider on a valid registry selection', async () => {
    const testApi = api as typeof api & {
      setResponse: (path: string, value: unknown) => void
      getResponse: (path: string) => unknown
    }
    const originalWorkspaces = testApi.getResponse('/api/workspaces')
    testApi.setResponse('/api/workspaces', {
      workspaces: [{
        name: 'legacy',
        vault_root: 'memory-vault/legacy',
        default_provider: 'ollama',
        default_model: 'qwen3:latest',
        gws_profile: '',
      }],
      active: 'legacy',
      provider_options: [
        { value: 'claude', label: 'Anthropic (via Claude Code)' },
        { value: 'opencode', label: 'opencode' },
      ],
    })

    const router = makeRouter()
    await router.push('/settings/workspaces')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    try {
      const providerField = wrapper.findAll('label.settings-field')
        .find((field) => field.find('.ws-label').text() === 'Agent CLI/Runtime')
      expect(providerField).toBeTruthy()
      const providerSelect = providerField!.find('select')
      expect((providerSelect.element as HTMLSelectElement).value).toBe('claude')
      expect(providerSelect.findAll('option').map((option) => option.attributes('value')))
        .toContain('claude')
      expect(providerSelect.text()).toContain('Anthropic (via Claude Code)')
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/workspaces', originalWorkspaces)
    }
  })

  it('shows the API explanation for a failed workspace save', async () => {
    const testApi = api as typeof api & {
      setResponse: (path: string, value: unknown) => void
      getResponse: (path: string) => unknown
    }
    const originalWorkspaces = testApi.getResponse('/api/workspaces')
    testApi.setResponse('/api/workspaces', {
      workspaces: [{
        name: 'personal',
        vault_root: 'memory-vault/personal',
        default_provider: 'claude',
        default_model: '',
        gws_profile: '',
      }],
      active: 'personal',
      provider_options: [
        { value: 'claude', label: 'Anthropic (via Claude Code)' },
        { value: 'opencode', label: 'opencode' },
      ],
    })

    const patchSpy = vi.spyOn(api, 'patch').mockRejectedValueOnce(Object.assign(
      new Error('HTTP 400'),
      { payload: { error: 'default_provider must be one of: claude, opencode' } },
    ))
    const router = makeRouter()
    await router.push('/settings/workspaces')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    try {
      await wrapper.find('.workspace-actions .btn-small').trigger('click')
      await flushPromises()

      const result = wrapper.find('.action-result[role="alert"]')
      expect(result.exists()).toBe(true)
      expect(result.classes()).toContain('action-result--error')
      expect(result.text()).toContain('default_provider must be one of: claude, opencode')
      const store = (await import('../../stores/projects')).useProjectStore()
      expect(store.toasts).toContainEqual(expect.objectContaining({
        title: 'Workspace "personal" not saved',
        body: 'default_provider must be one of: claude, opencode',
        variant: 'error',
      }))
    } finally {
      wrapper.unmount()
      patchSpy.mockRestore()
      testApi.setResponse('/api/workspaces', originalWorkspaces)
    }
  })

  it('SettingsView labels every provider connection from the backend registry', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    const names = wrapper.findAll('.provider-connections .routine-name').map((el) => el.text())
    // Names come from the payload, so a provider the PWA has never heard of
    // still gets its own card rather than another provider's label.
    expect(names).toEqual(['Claude Code', 'opencode'])

    // The unauthenticated provider offers Connect but not Log out.
    const rows = wrapper.findAll('.provider-connections .credential-row')
    const opencodeRow = rows[1]!
    expect(opencodeRow.text()).toContain('Not connected')
    const actions = opencodeRow.findAll('.provider-connection-actions button').map((b) => b.text())
    expect(actions).toEqual(['Connect', 'Verify'])
    wrapper.unmount()
  })

  it('SettingsView renders no API-key entry UI even when the payload advertises keys', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // Provider auth goes through each CLI (`ciao auth <provider>`), so the old
    // key-entry rows and Save Keys button are gone — even if a stale payload
    // still advertises key metadata.
    expect(wrapper.text()).not.toContain('OpenAI voice API key')
    expect(wrapper.findAll('input[type="password"]').length).toBe(0)
    expect(wrapper.text()).not.toContain('Save Keys')
    expect(wrapper.text()).not.toContain('Agent SDK ready')
    expect(wrapper.text()).not.toContain('app-server protocol compatible')
    expect(wrapper.text()).not.toContain('connection and protocol')
    wrapper.unmount()
  })

  it('SettingsView shows per-provider defaults and saves a default model', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // Defaults are now per-CLI inside the connection rows (no separate "defaults per provider" card).
    expect(wrapper.text()).toContain('Default model')
    expect(wrapper.text()).toContain('Default thinking')
    expect(wrapper.text()).not.toContain('defaults per provider')
    expect(wrapper.text()).not.toContain('model routing')
    // opencode exposes an editable default-model selector inside its provider card.
    const opencodeSelector = wrapper.find('.provider-connections .model-selector')
    expect(opencodeSelector.exists()).toBe(true)
    await opencodeSelector.find('.model-selector__trigger').trigger('click')
    await flushPromises()
    const opencodeOption = opencodeSelector.findAll('.model-selector__item')
      .find((el) => el.attributes('data-model') === 'opus')
    expect(opencodeOption).toBeTruthy()
    await opencodeOption!.trigger('click')
    await flushPromises()
    // Per-provider default models go through the provider_default_models map.
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      provider_default_models: { opencode: 'opus' },
    })

    wrapper.unmount()
  })

  it('SettingsView no longer offers a per-provider default mode', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // The default execution mode is fixed at auto for every provider; the
    // per-provider "Default mode" selector is gone.
    const modeSelect = wrapper.find('.routine-select[data-provider="opencode"]')
    expect(modeSelect.exists()).toBe(false)
    expect(wrapper.findAll('span.ws-label').some((el) => el.text() === 'Default mode')).toBe(false)

    wrapper.unmount()
  })

  it('SettingsView omits the defaults row for a signed-out provider', async () => {
    // aliasProviderSections (SettingsView.vue) derives availability from
    // /api/models' provider_models / {provider}_models, not from
    // /api/settings/routines' backends field. The shared fixture already
    // omits opencode there, so it is "signed out" by default: its
    // "defaults per provider" row must not render at all.
    const testApi = api as typeof api & {
      setResponse: (path: string, value: unknown) => void
      getResponse: (path: string) => unknown
    }
    const originalModels = testApi.getResponse('/api/models') as Record<string, unknown>
    testApi.setResponse('/api/models', {
      ...originalModels,
      provider_models: { claude: ['haiku', 'sonnet', 'opus', 'fable'] },
      opencode_models: [],
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
      // Per-CLI defaults live inside the connection rows now; opencode's
      // inline defaults are hidden when its catalog is empty.
      const inlineBlocks = wrapper.findAll('.provider-inline-defaults')
      expect(inlineBlocks.length).toBe(1)
      expect(wrapper.text()).toContain('Automatic — Claude Code picks its own default.')
      expect(wrapper.findAll('.provider-connections .model-selector')).toHaveLength(0)
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/models', originalModels)
    }
  })

  it('SettingsView saves routine models by provider', async () => {
    const mockApi = api as typeof api & {
      getResponse(path: string): unknown
      setResponse(path: string, value: unknown): void
    }
    const originalModels = mockApi.getResponse('/api/models') as Record<string, unknown>
    const opencodeModels = [
      'anthropic/claude-haiku-4.5',
      'anthropic/claude-sonnet-4.5',
      'anthropic/claude-opus-4.5',
    ]
    mockApi.setResponse('/api/models', {
      ...originalModels,
      provider_models: {
        ...(originalModels.provider_models as Record<string, string[]>),
        opencode: opencodeModels,
      },
      opencode_models: opencodeModels,
    })
    const router = makeRouter()
    await router.push('/settings/models')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // insights_model carries a provider select.
    const providerSelects = wrapper.findAll('.routine-model-controls .routine-select--provider')
    expect(providerSelects.length).toBeGreaterThanOrEqual(1)

    // Picking Claude stores the effective default as the concrete model.
    await providerSelects[0].setValue('claude')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      insights_model: 'sonnet',
    })

    // Picking opencode stores a provider-qualified concrete model.
    await providerSelects[0].setValue('opencode')
    await flushPromises()
    const patchMock = api.patch as unknown as { mock: { calls: Array<[string, unknown]> } }
    const last = patchMock.mock.calls[patchMock.mock.calls.length - 1]
    const body = last[1] as Record<string, string>
    expect(body.insights_model).toBe('opencode:opus')

    wrapper.unmount()
    mockApi.setResponse('/api/models', originalModels)
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
