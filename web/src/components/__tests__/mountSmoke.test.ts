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
    codex_haiku_model: '',
    codex_sonnet_model: '',
    codex_opus_model: '',
    codex_fable_model: '',
    title_model_effective: 'sonnet',
    insights_model_effective: 'haiku',

    critique_models_effective: 'anthropic/claude-sonnet-4.5,anthropic/claude-haiku-4.5',
    // Built-in defaults: opencode runs in bypass, everyone else auto.
    provider_default_modes_effective: {
      claude: 'auto',
      codex: 'auto',
      opencode: 'bypass',
    },

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
          label: 'Claude Code',
          short_label: 'Claude',
        },
        codex: {
          name: 'codex',
          ok: true,
          auth: 'chatgpt',
          command: 'ciao auth codex',
          version: 'codex-cli 0.144.0-alpha.4',
          account: 'ChatGPT account',
          protocol: 'app-server protocol compatible',
          label: 'OpenAI Codex',
          short_label: 'Codex',
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
          source: 'example-org/skill-pack',
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
      'Built-insession start',
      'Built-insession start',
      'Built-inon demand',
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
    expect(instructionRow!.text()).toContain('Custom')
    expect(instructionRow!.text()).toContain('CLAUDE.md and AGENTS.md are linked')
    await instructionRow!.trigger('click')
    await nextTick()
    // Single row: AGENTS.md is linked to CLAUDE.md, one link covers both.
    expect(instructionRow!.findAll('.inline-path-button').map((button) => button.text())).toEqual([
      'CLAUDE.md / AGENTS.md',
    ])
    // One guide, not one per CLI: say CLAUDE.md and mention the symlink once.
    expect(instructionRow!.text()).toContain('your user-level CLAUDE.md')
    expect(instructionRow!.text()).toContain('AGENTS.md is a symlink to it')

    const systemRow = wrapper.findAll('.skill-list > .instruction-row')
      .find((row) => row.text().includes('Ciaobot system instructions'))
    expect(systemRow).toBeTruthy()
    expect(systemRow!.text()).toContain('Built-in')
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
    expect(wrapper.text()).toContain('custom skills')
    expect(wrapper.text()).toContain('brainstorming')
    expect(wrapper.text()).toContain('github / package skills')
    expect(wrapper.text()).toContain('commands')
    expect(wrapper.text()).toContain('/remember')
    expect(wrapper.text()).toContain('Store a durable memory')
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
    await select.setValue('gpt-5.6-terra')
    await failing.find('.btn-run').trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith(
      '/api/automation/backfill-insights',
      { model: 'gpt-5.6-terra' },
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
    const codexOption = critiqueSelector.findAll('.model-selector__item')
      .find((el) => el.text() === 'codex:opus')
    expect(opusOption).toBeTruthy()
    expect(codexOption).toBeTruthy()

    await opusOption!.trigger('click')
    await flushPromises()
    await codexOption!.trigger('click')
    await flushPromises()

    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      critique_models: 'opus,codex:opus',
    })
    expect(wrapper.text()).toContain('codex:opus')
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
    expect(providerOptions).toContain('opencode')
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
    expect(names).toEqual(['Claude Code', 'OpenAI Codex', 'opencode'])

    // The unauthenticated provider offers Connect but not Log out.
    const rows = wrapper.findAll('.provider-connections .credential-row')
    const opencodeRow = rows[2]!
    expect(opencodeRow.text()).toContain('Not connected')
    const actions = opencodeRow.findAll('.provider-connection-actions button').map((b) => b.text())
    expect(actions).toEqual(['Connect', 'Verify'])
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
      'Anthropic (via Claude Code)',
      'OpenAI (via Codex)',
      // Registered but with no authenticated backend in this fixture.
      'opencode (not configured)',
    ])
    expect(wrapper.text()).toContain('model routing')
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
    // Runtime-provider pins go through the nested provider_routing map, not
    // the flat per-provider scalars (which the backend still accepts).
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      provider_routing: { codex: { haiku: 'gpt-5.6-terra' } },
    })
    expect(wrapper.find('.routing-model-catalog').exists()).toBe(false)

    wrapper.unmount()
  })

  it('SettingsView saves a per-provider default mode from the routing card', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // The routing card carries a "Default mode" row; the automatic option
    // names the effective default (opencode -> bypass from the backend).
    const modeSelect = wrapper.find('.tier-provider-section .routine-select:not(.alias-provider-select)')
    expect(modeSelect.exists()).toBe(true)
    const options = modeSelect.findAll('option').map((option) => option.text())
    expect(options[0]).toBe('Automatic (Auto)')

    await modeSelect.setValue('bypass')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      provider_default_modes: { codex: 'bypass' },
    })

    // Claude joins the provider list with read-only tier inputs, so its
    // default mode is settable even though its models cannot be re-pinned.
    const providerSelect = wrapper.find('.alias-provider-select')
    await providerSelect.setValue('claude')
    await flushPromises()
    await nextTick()
    expect(wrapper.findAll('.tier-provider-section .routing-model-input').length).toBe(4)
    const claudeMode = wrapper.find('.tier-provider-section .routine-select:not(.alias-provider-select)')
    expect(claudeMode.findAll('option').map((option) => option.text())[0]).toBe('Automatic (Auto)')

    wrapper.unmount()
  })

  it('SettingsView shows unconfigured tier providers disabled with a hint', async () => {
    // Drive the mock by patching /api/settings/routines: the mock merges the
    // body into the shared routineSettings, so a subsequent GET (which
    // fetchRoutines issues on mount) returns the flipped backends.
    const original = await api.get<Record<string, unknown>>('/api/settings/routines')
    const originalBackends = original.backends as Record<string, boolean>
    await api.patch('/api/settings/routines', {
      backends: { anthropic: true },
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
      // A signed-out provider stays listed rather than vanishing, so the user
      // can see it exists and what it needs.
      const labels = providerSelect.findAll('option').map((option) => option.text())
      expect(labels.some((l) => l.includes('opencode') && l.includes('not configured'))).toBe(true)

      // Select it: tier ModelSelectors render disabled, with the hint.
      await providerSelect.setValue('opencode')
      await flushPromises()
      await nextTick()
      const tierSelectors = wrapper.findAll('.tier-provider-section .model-selector')
      expect(tierSelectors.length).toBe(4)
      for (const selector of tierSelectors) {
        expect(selector.find('.model-selector__trigger').attributes('disabled')).toBeDefined()
      }
      const hint = wrapper.find('.tier-provider-note')
      expect(hint.exists()).toBe(true)
      expect(hint.text().toLowerCase()).toContain('opencode')
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

    // Anthropic tiers are the bare aliases: picking the provider stores the
    // routine's default tier, and the tier select stores that tier directly.
    await providerSelects[1].setValue('claude')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      insights_model: 'haiku',
    })

    const insightsTier = insightsControls.find('.routine-select--tier')
    expect(insightsTier.exists()).toBe(true)
    await insightsTier.setValue('opus')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      insights_model: 'opus',
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
