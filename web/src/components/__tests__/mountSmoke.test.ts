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
      connections: {
        claude: {
          name: 'claude',
          ok: true,
          auth: 'oauth',
          state: 'ready',
          command: 'claude auth login',
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
          state: 'failed',
          command: 'opencode auth login',
          version: '1.18.18',
          detail: 'login required',
          label: 'opencode',
          short_label: 'opencode',
        },
      },
    },
    '/api/local/status': { git_repo: true, branch: 'main', dirty: false },
    '/api/admin/skills': {
      counts: { custom: 1, stock: 0 },
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
        opencode: ['openai/gpt-5.6-luna', 'anthropic/claude-sonnet-4-6'],
      },
      provider_defaults: { claude: 'sonnet', opencode: 'opus' },
      providers: [
        { id: 'claude', label: 'Anthropic (via Claude Code)', short_label: 'Claude', capabilities: {} },
        { id: 'opencode', label: 'OpenCode', short_label: 'OpenCode', capabilities: {} },
      ],
      opencode_models: ['openai/gpt-5.6-luna', 'anthropic/claude-sonnet-4-6'],
      backends: { opencode: true },
      thinking_levels: { claude: ['low', 'medium', 'high', 'xhigh', 'max'], opencode: ['low', 'medium', 'high', 'max'] },
    },
    '/api/projects': [],
    '/api/chats': [],
    '/api/tasks': { tasks: [] },
    '/api/schedules': [],
    // The Automations overview reads the webhook triggers section on mount, so
    // the shared fixture answers it. A bare `[]` would break `triggers.map`.
    '/api/webhooks': { triggers: [] },
    '/api/workspaces': {
      workspaces: [],
      active: null,
      provider_options: [
        { value: 'claude', label: 'Anthropic (via Claude Code)' },
        { value: 'opencode', label: 'OpenCode' },
      ],
    },
    '/api/local/backup': {
      state: 'ready',
      scope: 'memory-vault, skills, subagents, commands',
      branch: 'main',
      remote: 'https://github.com/person/memory.git',
      last_remote: 'https://github.com/person/memory.git',
      enabled: true,
      interval_s: 300,
      last_attempt_at: '2026-09-01T10:00:00+00:00',
      last_success_at: '2026-09-01T10:00:04+00:00',
      last_success_commit: 'abc1234',
      pending_changes: 0,
      pending_commits: 0,
      reason: 'up to date',
    },
    '/api/proposals/handover': {
      workspace: 'personal',
      handover: 'off',
      memory_approval: {},
      accept_resolved: { memory: { mode: 'approve', inherited: true }, profile: { mode: 'approve', inherited: true } },
      acting_available: true,
      agreement: {
        decided: 0, matched: 0, rate: null, window: 50, min_decisions: 20, threshold: 0.9,
        predicted_accept: 0, accept_matched: 0, predicted_dismiss: 0, dismiss_matched: 0,
        suggest_handover: false,
      },
    },
    '/api/local/backup/setup-prompt': {
      context: { folder: '/Users/person/Ciao', scope: 'memory-vault' },
      prompt: 'Set up a private repository for this folder.',
    },
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
  const patch = vi.fn((path: string, body: Record<string, unknown>) => {
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

const runInTerminal = vi.hoisted(() => vi.fn(async (_command: string, _opts?: unknown) => 'typed' as string))
vi.mock('../../composables/useRunInTerminal', async () => {
  const { ref } = await import('vue')
  return { useRunInTerminal: () => ({ run: runInTerminal, busy: ref(false) }) }
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
vi.mock('../ChatPanel.vue', () => ({ default: NoopStub }))
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
      { path: '/tasks', name: 'tasks', component: Stub },
      { path: '/stats', name: 'stats', component: Stub },
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

/** How long a component import may take before the smoke test fails on it (well under the 20 s testTimeout in vite.config.ts). */
const IMPORT_DEADLINE_MS = 12_000

/** Resolve the loader, or fail fast with the import named: a hung import must not look like a slow test. */
export async function importWithin<T>(
  loader: () => Promise<T>,
  label: string,
  deadlineMs: number = IMPORT_DEADLINE_MS,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`component import ${label} did not settle within ${deadlineMs} ms`)),
      deadlineMs,
    )
  })
  try {
    return await Promise.race([
      loader().catch((err: unknown) => {
        throw new Error(`component import ${label} failed: ${err instanceof Error ? err.message : String(err)}`, { cause: err })
      }),
      deadline,
    ])
  } finally {
    clearTimeout(timer)
  }
}

async function mountAndSettle(loader: () => Promise<{ default: unknown }>, label = loader.toString()) {
  const errors: unknown[] = []
  const errorHandler = (err: unknown) => { errors.push(err) }
  const router = makeRouter()
  await router.push('/')
  await router.isReady()

  const mod = await importWithin(loader, label)
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

  it('TaskBoardView mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../TaskBoardView.vue'))
    expect(errors).toEqual([])
  })

  it('ChatLayout leaves Esc to an open dialog or menu, and leaves the page otherwise', async () => {
    const router = makeRouter()
    await router.push('/tasks')
    await router.isReady()
    const mod = await import('../ChatLayout.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    const esc = () => document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))

    // Reka marks every open dialog, menu and popover this way; its own Esc
    // listener runs after ChatLayout's, so the page must not act first.
    const layer = document.createElement('div')
    layer.setAttribute('data-dismissable-layer', '')
    document.body.appendChild(layer)
    esc()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/tasks')

    layer.remove()
    esc()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    wrapper.unmount()
  })

  it('ChatLayout renders the task board at /tasks', async () => {
    const router = makeRouter()
    await router.push('/tasks')
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

    // The pane is a `viewMode` branch, so the route is what puts it on screen.
    // The shared `/api/tasks` fixture answers `{ tasks: [] }`, which is an empty
    // board rather than a load error — the mount must not mistake one for the
    // other and throw.
    expect(wrapper.text()).toContain('No tasks in')
    expect(wrapper.text()).not.toContain('Could not load')
    wrapper.unmount()
  })

  it('UsageView mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../UsageView.vue'))
    expect(errors).toEqual([])
  })

  it('ChatLayout renders the usage page at /stats and Esc leaves it', async () => {
    const router = makeRouter()
    await router.push('/stats')
    await router.isReady()
    const mod = await import('../ChatLayout.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // The shared api mock answers [] for /api/stats: every value reads as a dash and nothing throws.
    expect(wrapper.find('.usage-view').exists()).toBe(true)
    expect(wrapper.text()).toContain('Chats opened')

    const esc = () => document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    esc()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    wrapper.unmount()
  })

  it('WebhookTriggers mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../WebhookTriggers.vue'))
    expect(errors).toEqual([])
  })

  it('WebhookTriggers renders the Automations section against the shared fixture', async () => {
    // The section is mounted here rather than through SchedulePanel, which this
    // file stubs out as an async no-op — the overview itself is covered by
    // SchedulePanelCards.test.ts.
    const mod = await import('../WebhookTriggers.vue')
    const wrapper = mount(mod.default as never, { global: { stubs: { Teleport: true } } })
    await flushPromises()
    await nextTick()

    // The shared `/api/webhooks` fixture answers `{ triggers: [] }`, which is an
    // empty list rather than a load error: the mount must not mistake one for the
    // other, or it will render a failure over a workspace that simply has none.
    expect(wrapper.text()).toContain('Webhook triggers')
    expect(wrapper.text()).toContain('No webhook triggers yet')
    expect(wrapper.text()).not.toContain('Could not load webhook triggers')
    wrapper.unmount()
  })

  it('SettingsView mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../SettingsView.vue'))
    expect(errors).toEqual([])
  })

  it('SettingsMemoryBackup mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../settings/SettingsMemoryBackup.vue'))
    expect(errors).toEqual([])
  })

  it('SettingsMemoryHandover mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../settings/SettingsMemoryHandover.vue'))
    expect(errors).toEqual([])
  })

  it('SettingsSharedBrains mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../settings/SettingsSharedBrains.vue'))
    expect(errors).toEqual([])
  })

  it('SettingsCareCycle mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../settings/SettingsCareCycle.vue'))
    expect(errors).toEqual([])
  })

  it('SettingsView shows the memory backup section on /settings/advanced', async () => {
    const router = makeRouter()
    await router.push('/settings/advanced')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // The section reads the same page as the program's own repository
    // controls, and is its own section in the On this page rail.
    const backup = wrapper.findAll('.card').find(c => c.text().startsWith('Memory backup'))
    expect(backup).toBeTruthy()
    expect(backup!.text()).toContain('github.com/person/memory.git')
    expect(wrapper.findAll('.settings-toc-item').map(i => i.text())).toContain('Memory backup')
    wrapper.unmount()
  })


  it('SettingsView renders skills with custom and stock labels on /settings/skills', async () => {
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
    expect(wrapper.text()).toContain('Custom skills')
    expect(wrapper.text()).toContain('Built-in skills')
    wrapper.unmount()
  })

  it('SettingsView shows the skills empty state when the inventory has no skills list', async () => {
    // A partial response ({} from an older engine or a stub) used to throw in
    // render (`skills.filter` on undefined) and blank the whole Settings pane.
    const testApi = api as typeof api & {
      setResponse: (path: string, value: unknown) => void
      getResponse: (path: string) => unknown
    }
    const original = testApi.getResponse('/api/admin/skills')
    testApi.setResponse('/api/admin/skills', {})
    const router = makeRouter()
    await router.push('/settings/skills')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    try {
      await flushPromises()
      await nextTick()
      expect(wrapper.text()).toContain('No custom skills yet.')
      expect(wrapper.find('.settings-rail-assets').text()).toContain('Custom')
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/admin/skills', original)
    }
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

  it('SettingsView has no session insights switch on the General tab', async () => {
    const router = makeRouter()
    await router.push('/settings')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[aria-label="Automatic session insights"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Automatic session insights')
    expect(wrapper.text()).not.toContain('Trajectory capture')
    wrapper.unmount()
  })

  it('SettingsView renders configured workspace providers', async () => {
    const router = makeRouter()
    await router.push('/settings/workspace')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    const addButton = wrapper.findAll('button').find((button) => button.text() === 'New workspace')
    expect(addButton).toBeTruthy()
    await addButton!.trigger('click')
    await nextTick()

    const providerOptions = wrapper.findAll('select.routine-input option').map((option) => option.text())
    expect(providerOptions).toContain('Anthropic (via Claude Code)')
    expect(providerOptions).toContain('OpenCode')
    // Provider is a native select;
    // Default model uses the custom ModelSelector component, not a second <select>.
    expect(wrapper.findAll('select.workspace-select')).toHaveLength(1)

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
      }],
      active: 'legacy',
      provider_options: [
        { value: 'claude', label: 'Anthropic (via Claude Code)' },
        { value: 'opencode', label: 'OpenCode' },
      ],
    })

    const router = makeRouter()
    await router.push('/settings/workspace')
    const overviewStore = (await import('../../stores/projects')).useProjectStore()
    overviewStore.activeWorkspace = 'legacy'
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    try {
      // The provider control lives in the Overview's Chats and models card.
      const card = wrapper.findAll('.card').find((c) => c.find('.section-title').text() === 'Chats and models')
      expect(card).toBeTruthy()
      const radios = card!.findAll('[role="radiogroup"] button[role="radio"]')
      expect(radios.map((r) => r.text())).toEqual(['Anthropic (via Claude Code)', 'OpenCode'])
      expect(radios.map((r) => r.attributes('aria-checked'))).toEqual(['true', 'false'])
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
      }],
      active: 'personal',
      provider_options: [
        { value: 'claude', label: 'Anthropic (via Claude Code)' },
        { value: 'opencode', label: 'OpenCode' },
      ],
    })

    const patchSpy = vi.spyOn(api, 'patch').mockRejectedValueOnce(Object.assign(
      new Error('HTTP 400'),
      { payload: { error: 'default_provider must be one of: claude, opencode' } },
    ))
    const router = makeRouter()
    await router.push('/settings/workspace')
    const overviewStore = (await import('../../stores/projects')).useProjectStore()
    overviewStore.activeWorkspace = 'personal'
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    try {
      // Save appears only after a change on the page.
      expect(wrapper.find('.workspace-save').exists()).toBe(false)
      await wrapper.get('.workspace-color-swatch[aria-label="Terminal Cyan"]').trigger('click')
      await nextTick()
      await wrapper.get('.workspace-save').trigger('click')
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

  it('SettingsView providers tab shows the chat providers card, and its Status rail with one card', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    const titles = wrapper.findAll('.section-title').map((el) => el.text())
    expect(titles).toEqual(['Chat providers'])
    expect(wrapper.find('#chat-providers').exists()).toBe(true)
    expect(wrapper.findAll('.settings-toc-item')).toHaveLength(0)
    expect(wrapper.find('.settings-rail-status').exists()).toBe(true)
    wrapper.unmount()
  })

  it('SettingsView scrolls to the chat providers card once, not after every refetch', async () => {
    const proto = Element.prototype as Element & { scrollIntoView?: unknown }
    const original = proto.scrollIntoView
    const scrolled = vi.fn()
    proto.scrollIntoView = scrolled
    const router = makeRouter()
    await router.push('/settings/providers#chat-providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    // Attached, so document.getElementById can find the card.
    const wrapper = mount(mod.default as never, {
      attachTo: document.body,
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    try {
      await flushPromises()
      await nextTick()
      expect(scrolled).toHaveBeenCalledTimes(1)

      // Verify refetches the connections; the hash is still in the URL but
      // the page must not jump back to the top of the card.
      const verify = wrapper.findAll('.provider-connection-actions button').find((b) => b.text() === 'Verify')
      await verify!.trigger('click')
      await flushPromises()
      await nextTick()
      expect(scrolled).toHaveBeenCalledTimes(1)

      // An in-app link to the anchor (this view stays mounted) scrolls again.
      await router.push('/settings/skills')
      await router.push('/settings/providers#chat-providers')
      await flushPromises()
      await nextTick()
      expect(scrolled).toHaveBeenCalledTimes(2)
    } finally {
      wrapper.unmount()
      proto.scrollIntoView = original
    }
  })

  it('SettingsView folds what each CLI brings behind a collapsed disclosure', async () => {
    const testApi = api as typeof api & {
      setResponse: (path: string, value: unknown) => void
      getResponse: (path: string) => unknown
    }
    const original = testApi.getResponse('/api/settings/providers') as {
      connections: Record<string, Record<string, unknown>>
    }
    testApi.setResponse('/api/settings/providers', {
      ...original,
      connections: {
        ...original.connections,
        claude: {
          ...original.connections.claude,
          skills: ['frontend-design'],
          bundled_skills: ['code-review', 'dataviz'],
        },
      },
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
      const disclosures = wrapper.findAll('.provider-connections details.provider-brings')
      expect(disclosures).toHaveLength(2)
      // Collapsed by default: the chip lists are there but folded away.
      for (const d of disclosures) {
        expect((d.element as HTMLDetailsElement).open).toBe(false)
      }
      const claude = disclosures[0]!
      expect(claude.find('summary').text()).toMatch(/What this CLI brings \(1 skill or plugin, 2 built in\)/)
      expect(claude.text()).toContain('frontend-design')
      expect(claude.text()).toMatch(/Built into .+ \(2\)/)
      expect(claude.text()).toContain('dataviz')
      expect(disclosures[1]!.find('summary').text()).toContain('0 skills & plugins)')
      // The defaults and actions stay outside the disclosure.
      expect(wrapper.find('details.provider-brings .provider-inline-defaults').exists()).toBe(false)
      expect(wrapper.find('details.provider-brings .provider-connection-actions').exists()).toBe(false)
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/settings/providers', original)
    }
  })

  it('shows the Built into list only for a CLI that reports bundled skills', async () => {
    const testApi = api as typeof api & {
      setResponse: (path: string, value: unknown) => void
      getResponse: (path: string) => unknown
    }
    const original = testApi.getResponse('/api/settings/providers') as {
      connections: Record<string, Record<string, unknown>>
    }
    // The Claude CLI reports no bundled skills: the key is absent, not an empty list.
    const { bundled_skills: _claudeBundled, ...claudeWithout } = original.connections.claude!
    testApi.setResponse('/api/settings/providers', {
      ...original,
      connections: {
        ...original.connections,
        claude: claudeWithout,
        opencode: { ...original.connections.opencode, ok: true, bundled_skills: [] },
      },
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
      const disclosures = wrapper.findAll('.provider-connections details.provider-brings')
      expect(disclosures).toHaveLength(2)
      expect(disclosures[0]!.text()).not.toMatch(/Built into/)
      expect(disclosures[0]!.text()).not.toMatch(/Listed after the next chat/)
      expect(disclosures[1]!.text()).toMatch(/Built into .+ \(0\)/)
      expect(disclosures[1]!.text()).toContain('None reported by this CLI')
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/settings/providers', original)
    }
  })

  async function mountModelsWith(releaseChecks: Record<string, Record<string, unknown>>) {
    const testApi = api as typeof api & {
      setResponse: (path: string, value: unknown) => void
      getResponse: (path: string) => unknown
    }
    const original = testApi.getResponse('/api/settings/providers') as {
      connections: Record<string, Record<string, unknown>>
    }
    const connections = { ...original.connections }
    for (const [key, release_check] of Object.entries(releaseChecks)) {
      connections[key] = { ...connections[key], release_check }
    }
    testApi.setResponse('/api/settings/providers', { ...original, connections })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()
    return { wrapper, restore: () => testApi.setResponse('/api/settings/providers', original) }
  }

  function providerRow(wrapper: ReturnType<typeof mount>, label: RegExp) {
    return wrapper.findAll('.provider-connections .credential-row').find(r => label.test(r.find('.set-row-title').text()))!
  }

  it('SettingsView shows the release-tested version and an update command for an older OpenCode', async () => {
    const { wrapper, restore } = await mountModelsWith({
      opencode: { state: 'below_tested', installed: '2.0.32', tested: '2.1.0', command: 'opencode upgrade 2.1.0', terminal_action: 'opencode-update' },
    })
    try {
      const row = providerRow(wrapper, /opencode/i)
      expect(row.text()).toContain('Older than 2.1.0')
      expect(row.text()).toContain('opencode upgrade 2.1.0')
      expect(row.find('.provider-release-check button').text()).toBe('Copy')
    } finally {
      wrapper.unmount()
      restore()
    }
  })

  const belowTested = {
    opencode: { state: 'below_tested', installed: '2.0.32', tested: '2.1.0', command: 'opencode upgrade 2.1.0', terminal_action: 'opencode-update' },
  }

  it.each(['install', 'update', 'not_applicable'] as const)('SettingsView hides raw host-terminal %s errors in the rendered alert', async (action) => {
    const testApi = api as typeof api & { setResponse: (path: string, value: unknown) => void, getResponse: (path: string) => unknown }
    const original = testApi.getResponse('/api/settings/providers') as { connections: Record<string, Record<string, unknown>> }
    const oldCaps = testApi.getResponse('/api/host/terminal')
    const host = await import('../../composables/useHostTerminal')
    const loopback = await import('../../lib/loopback')
    const local = vi.spyOn(loopback, 'isLoopbackPage').mockReturnValue(true)
    host.resetHostTerminalForTests()
    testApi.setResponse('/api/host/terminal', { available: true, platform: 'macos', actions: ['opencode-install', 'opencode-update'] })
    const opencode = action === 'install'
      ? { ...original.connections.opencode, auth: 'not_installed', state: 'not_installed', command: 'install', terminal_action: 'opencode-install' }
      : { ...original.connections.opencode, release_check: { state: 'below_tested', tested: '2.1', command: 'update', terminal_action: 'opencode-update' } }
    testApi.setResponse('/api/settings/providers', { connections: { ...original.connections, opencode } })
    vi.mocked(api.post).mockRejectedValueOnce({ message: 'SECRET_EXCEPTION', payload: { reason: action === 'not_applicable' ? 'not_applicable' : 'launch_failed', error: 'SECRET_PAYLOAD' } })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await flushPromises()
      const row = providerRow(wrapper, /opencode/i)
      const readsBefore = vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers').length
      await row.get(`button[aria-label="${action === 'install' ? 'Install opencode in Terminal' : 'Update opencode in Terminal'}"]`).trigger('click')
      await flushPromises()
      expect(row.get('[role="alert"]').text()).toBe(action === 'not_applicable' ? 'This update is no longer needed.' : 'Could not open a terminal. Please try again.')
      expect(row.text()).not.toContain('SECRET_')
      if (action === 'not_applicable') {
        expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(readsBefore + 1)
      }
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/settings/providers', original)
      testApi.setResponse('/api/host/terminal', oldCaps)
      local.mockRestore()
      host.resetHostTerminalForTests()
    }
  })

  it('SettingsView says the copy failed, in text, when the clipboard is refused', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    const { wrapper, restore } = await mountModelsWith(belowTested)
    try {
      const row = providerRow(wrapper, /opencode/i)
      await row.get('.provider-release-check button').trigger('click')
      await flushPromises()
      expect(row.get('.provider-release-check [role=status]').text()).toBe('Copy failed; select the command above.')
      expect(row.get('.provider-release-check button').text()).toBe('Copy')
    } finally {
      wrapper.unmount()
      restore()
      vi.unstubAllGlobals()
      vi.restoreAllMocks()
    }
  })

  it('SettingsView reverts Copied after a moment', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
    const { wrapper, restore } = await mountModelsWith(belowTested)
    vi.useFakeTimers()
    try {
      const button = providerRow(wrapper, /opencode/i).get('.provider-release-check button')
      await button.trigger('click')
      await flushPromises()
      expect(button.text()).toBe('Copied')
      await vi.advanceTimersByTimeAsync(3000)
      expect(button.text()).toBe('Copy')
    } finally {
      vi.useRealTimers()
      wrapper.unmount()
      restore()
      vi.unstubAllGlobals()
    }
  })

  it('SettingsView says Tested with for a current provider', async () => {
    const { wrapper, restore } = await mountModelsWith({
      opencode: { state: 'current', installed: '2.1.0', tested: '2.1.0' },
    })
    try {
      const row = providerRow(wrapper, /opencode/i)
      expect(row.text()).toContain('Tested with 2.1.0')
      expect(row.text()).not.toContain('Older than')
    } finally {
      wrapper.unmount()
      restore()
    }
  })

  it('SettingsView provides manual sign-in without claiming OpenCode verification', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Ciaobot verifies every connection')
    expect(wrapper.text()).toContain('Provider inventory unavailable')
    expect(wrapper.text()).toContain('claude auth login')
    expect(wrapper.text()).toContain('OAuth')
    expect(wrapper.text()).toContain('person@example.com')
    expect(wrapper.text()).toContain('opencode auth login')
    expect(runInTerminal).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not expose routines GET errors and allows a successful retry', async () => {
    const get = vi.mocked(api.get)
    const original = get.getMockImplementation()!
    let fail = true
    get.mockImplementation(((path: string) => path === '/api/settings/routines' && fail
      ? Promise.reject(new Error('SECRET_ROUTINES_GET')) : original(path)) as typeof api.get)
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await flushPromises()
      expect(wrapper.text()).toContain('Failed to load model settings.')
      expect(wrapper.text()).not.toContain('SECRET_ROUTINES_GET')
      fail = false
      await wrapper.findAll('button').find(button => button.text() === 'Retry model settings')!.trigger('click')
      await flushPromises()
      expect(wrapper.text()).not.toContain('Failed to load model settings.')
    } finally {
      wrapper.unmount()
      get.mockImplementation(original)
    }
  })

  it('does not expose routines PATCH errors in the UI or failure toast and can retry', async () => {
    const projects = (await import('../../stores/projects')).useProjectStore()
    const toast = vi.spyOn(projects, 'pushErrorToast')
    vi.mocked(api.patch).mockRejectedValueOnce(new Error('SECRET_ROUTINES_PATCH'))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await flushPromises()
      const state = (wrapper.vm as unknown as { $: { setupState: { saveRoutines: (patch: Record<string, unknown>) => Promise<void> } } }).$.setupState
      await state.saveRoutines({ provider_default_modes: { claude: 'manual' } })
      expect(toast).toHaveBeenCalledWith('Could not save model settings', 'Please try again.')
      expect(JSON.stringify(toast.mock.calls)).not.toContain('SECRET_ROUTINES_PATCH')
      expect(wrapper.text()).not.toContain('SECRET_ROUTINES_PATCH')
      await state.saveRoutines({ provider_default_modes: { claude: 'manual' } })
      expect(vi.mocked(api.patch)).toHaveBeenCalledTimes(2)
    } finally {
      wrapper.unmount()
      toast.mockRestore()
    }
  })

  it('shows Codex sign-in guidance with the command its own row reports', async () => {
    const testApi = api as typeof api & { setResponse: (path: string, value: unknown) => void, getResponse: (path: string) => unknown }
    const original = testApi.getResponse('/api/settings/providers') as { connections: Record<string, Record<string, unknown>> }
    testApi.setResponse('/api/settings/providers', { connections: { ...original.connections, codex: {
      name: 'codex', label: 'Codex', short_label: 'Codex', ok: false, auth: 'logged_out', state: 'failed', command: 'codex login',
      detail: 'Codex is installed but not signed in.',
    } } })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await flushPromises()
      const row = wrapper.findAll('.provider-connections .credential-row').find(el => el.text().includes('Codex'))!
      expect(row.find('details.provider-sign-in summary').text()).toBe('Connect Codex')
      expect(row.find('details code').text()).toBe('codex login')
      expect(row.text()).not.toContain('opencode auth login')
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/settings/providers', original)
    }
  })

  it('hides what a CLI brings while that CLI is not installed', async () => {
    const testApi = api as typeof api & { setResponse: (path: string, value: unknown) => void, getResponse: (path: string) => unknown }
    const original = testApi.getResponse('/api/settings/providers') as { connections: Record<string, Record<string, unknown>> }
    testApi.setResponse('/api/settings/providers', { connections: { ...original.connections, codex: {
      name: 'codex', label: 'Codex', short_label: 'Codex', ok: false, auth: 'not_installed', command: 'codex login',
      detail: 'Codex is not installed on this host.',
    } } })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await flushPromises()
      const row = wrapper.findAll('.provider-connections .credential-row').find(el => el.text().includes('Codex'))!
      expect(row.text()).toContain('Codex is not installed on this host.')
      expect(row.find('details.provider-brings').exists()).toBe(false)
      expect(row.text()).not.toContain('0 skills & plugins')
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/settings/providers', original)
    }
  })

  it.each([
    ['oauth', 'person@example.com', true],
    ['oauth', 'Claude OAuth', true],
    ['missing', 'untrusted account', false],
    ['not_installed', 'untrusted account', false],
  ] as const)('shows only known Claude %s account and method', async (auth, account, showAccount) => {
    const testApi = api as typeof api & { setResponse: (path: string, value: unknown) => void, getResponse: (path: string) => unknown }
    const original = testApi.getResponse('/api/settings/providers') as { connections: Record<string, Record<string, unknown>> }
    testApi.setResponse('/api/settings/providers', { connections: { ...original.connections, claude: {
      ...original.connections.claude, auth, account, ok: showAccount,
      state: auth === 'not_installed' ? 'not_installed' : showAccount ? 'ready' : 'failed',
      // The sign-in command comes from the payload; a missing CLI's payload command is its install line.
      command: auth === 'not_installed' ? 'install-command-not-login' : 'claude auth login',
    } } })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await flushPromises()
      const row = wrapper.findAll('.provider-connections .credential-row')[0]!
      const details = row.find('.provider-connection-detail').text()
      expect(details.includes(account)).toBe(showAccount)
      expect(details.includes('OAuth')).toBe(showAccount)
      if (auth === 'not_installed') {
        // The install line is never offered as a sign-in command.
        expect(row.find('details.provider-sign-in').exists()).toBe(false)
      } else {
        expect(row.find('details code').text()).toBe('claude auth login')
      }
      expect(runInTerminal).not.toHaveBeenCalled()
      expect(vi.mocked(api.post).mock.calls.some(([path]) => String(path).endsWith('/connect'))).toBe(false)
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/settings/providers', original)
    }
  })

  it('SettingsView renders configured OpenCode metadata but not credential fields', async () => {
    const testApi = api as typeof api & { setResponse: (path: string, value: unknown) => void, getResponse: (path: string) => unknown }
    const original = testApi.getResponse('/api/settings/providers') as { connections: Record<string, Record<string, unknown>> }
    testApi.setResponse('/api/settings/providers', { connections: { ...original.connections, opencode: {
      ...original.connections.opencode, credential_inventory: { status: 'complete', providers: [
        { id: 'custom', name: '<example>', auth_types: ['environment'], configured: true, verified: null, value: 'secret-value', label: 'secret-label', env: 'SECRET_ENV' },
      ] },
    } } })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await flushPromises()
      expect(wrapper.text()).toContain('<example>')
      expect(wrapper.text()).toContain('Environment · Configured')
      expect(wrapper.text()).not.toContain('verification unknown')
      expect(wrapper.text()).not.toContain('secret-value')
      expect(wrapper.text()).not.toContain('secret-label')
      expect(wrapper.text()).not.toContain('SECRET_ENV')
      expect(wrapper.find('example').exists()).toBe(false)
      expect(vi.mocked(api.post).mock.calls.some(([path]) => String(path).endsWith('/connect'))).toBe(false)
      expect(runInTerminal).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/settings/providers', original)
    }
  })

  it('offers OpenCode refresh when its provider row is missing', async () => {
    const testApi = api as typeof api & { setResponse: (path: string, value: unknown) => void, getResponse: (path: string) => unknown }
    const original = testApi.getResponse('/api/settings/providers') as { connections: Record<string, unknown> }
    testApi.setResponse('/api/settings/providers', { connections: { claude: original.connections.claude } })
    const project = (await import('../../stores/projects')).useProjectStore()
    const tasks = (await import('../../stores/tasks')).useTaskStore()
    project.workspaces = [{ name: 'work', vault_root: 'work' }] as never
    project.activeWorkspace = 'work'
    tasks.fetchModels = vi.fn().mockResolvedValue({ status: 'success', workspace: 'work', generation: 1 }) as never
    vi.mocked(api.post).mockResolvedValueOnce({ name: 'opencode', ok: false, auth: 'missing', state: 'failed' })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await flushPromises()
      expect(wrapper.text()).toContain('Provider inventory unavailable')
      const refresh = wrapper.findAll('button').find(button => button.text() === 'Refresh after sign-in')
      expect(refresh).toBeDefined()
      await refresh!.trigger('click')
      await flushPromises()
      expect(vi.mocked(api.post)).toHaveBeenCalledWith('/api/settings/providers/opencode/verify')
      expect(wrapper.text()).toContain('Catalog availability and credential verification are unknown')
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/settings/providers', original)
    }
  })

  it('releases pending ownership when the model transition changes during verify', async () => {
    const project = (await import('../../stores/projects')).useProjectStore()
    const tasks = (await import('../../stores/tasks')).useTaskStore() as unknown as { modelTransition: number, fetchModels: () => Promise<unknown> }
    project.workspaces = [{ name: 'work', vault_root: 'work' }] as never
    project.activeWorkspace = 'work'
    tasks.modelTransition = 1
    tasks.fetchModels = vi.fn()
    let finish!: (value: unknown) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    const row = wrapper.findAll('.provider-connections .credential-row')[1]!
    const verify = row.findAll('button').find(button => button.text() === 'Verify')!
    await verify.trigger('click')
    expect(verify.attributes('disabled')).toBeDefined()
    tasks.modelTransition++
    finish({ name: 'opencode', ok: true, auth: 'oauth', state: 'ready' })
    await flushPromises()
    const nextVerify = wrapper.findAll('.provider-connections .credential-row')[1]!.findAll('button').find(button => button.text() === 'Verify')!
    expect(nextVerify.attributes('disabled')).toBeUndefined()
    expect(tasks.fetchModels).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Provider and model reads completed')
    wrapper.unmount()
  })

  it('discards a verify after an A-B-A workspace transition before either model reader', async () => {
    const project = (await import('../../stores/projects')).useProjectStore()
    const tasks = (await import('../../stores/tasks')).useTaskStore()
    project.workspaces = [{ name: 'A', vault_root: 'A' }, { name: 'B', vault_root: 'B' }] as never
    project.activeWorkspace = 'A'
    const shared = vi.fn().mockResolvedValue({ status: 'success', workspace: 'A', generation: 1 })
    tasks.fetchModels = shared as never
    let finish!: (value: unknown) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    const row = wrapper.findAll('.provider-connections .credential-row')[1]!
    await row.findAll('button').find(button => button.text() === 'Verify')!.trigger('click')
    const providersBefore = vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers').length
    project.activeWorkspace = 'B'
    project.activeWorkspace = 'A'
    await nextTick()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(providersBefore)
    const readsBefore = vi.mocked(api.get).mock.calls.filter(([path]) => String(path).startsWith('/api/models')).length
    finish({ name: 'opencode', ok: true, auth: 'oauth', state: 'ready' })
    await flushPromises()
    const readsAfter = vi.mocked(api.get).mock.calls.filter(([path]) => String(path).startsWith('/api/models')).length
    expect(shared).not.toHaveBeenCalled()
    expect(readsAfter).toBe(readsBefore)
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(providersBefore + 1)
    expect(wrapper.text()).not.toContain('Provider and model reads completed')
    wrapper.unmount()
  })

  it.each([false, true])('does not supersede a real open-picker model read after old verify (return to A: %s)', async (returnToA) => {
    const project = (await import('../../stores/projects')).useProjectStore()
    const tasks = (await import('../../stores/tasks')).useTaskStore()
    project.workspaces = [{ name: 'A', vault_root: 'A' }, { name: 'B', vault_root: 'B' }] as never
    project.workspaceRegistryStatus = 'ready'
    project.activeWorkspace = 'A'
    const get = vi.mocked(api.get)
    const originalGet = get.getMockImplementation()!
    const actualFetch = tasks.fetchModels
    let sharedCall = false
    let holdShared = false
    let finishModel!: (value: unknown) => void
    let finishVerify!: (value: unknown) => void
    const shared = vi.spyOn(tasks, 'fetchModels').mockImplementation((workspace?: string) => {
      sharedCall = true
      try { return actualFetch(workspace) } finally { sharedCall = false }
    })
    get.mockImplementation(((path: string) => {
      if (path.startsWith('/api/models?') && sharedCall && holdShared) {
        return new Promise<unknown>(resolve => { finishModel = resolve })
      }
      return originalGet(path)
    }) as typeof api.get)
    vi.mocked(api.post).mockImplementationOnce(() => new Promise(resolve => { finishVerify = resolve }))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const settingsModule = await import('../SettingsView.vue')
    const homeModule = await import('../HomeIntake.vue')
    const settings = mount(settingsModule.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    const home = mount(homeModule.default as never)
    try {
      await flushPromises()
      await home.get('.home-intake-model-trigger').trigger('click')
      await flushPromises()
      const verify = settings.findAll('.provider-connections .credential-row')[1]!.findAll('button').find(button => button.text() === 'Verify')!
      await verify.trigger('click')
      holdShared = true
      project.activeWorkspace = 'B'
      if (returnToA) project.activeWorkspace = 'A'
      await nextTick()
      expect(tasks.modelLoading).toBe(true)
      expect(tasks.models).toBeNull()
      const calls = shared.mock.calls.length
      const modelsCalls = get.mock.calls.filter(([path]) => String(path).startsWith('/api/models?')).length
      const transition = tasks.modelTransition
      finishVerify({ name: 'opencode', ok: true, auth: 'oauth', state: 'ready' })
      await flushPromises()
      expect(shared).toHaveBeenCalledTimes(calls)
      expect(get.mock.calls.filter(([path]) => String(path).startsWith('/api/models?')).length).toBe(modelsCalls)
      expect(tasks.modelTransition).toBe(transition)
      expect(tasks.modelLoading).toBe(true)
      expect(tasks.modelError).toBe('')
      expect(tasks.models).toBeNull()
      expect(settings.text()).not.toContain('Provider and model reads completed')
      finishModel({ models: ['new-model'], default: 'new-model', provider_models: { claude: ['new-model'] } })
      await flushPromises()
      expect(tasks.modelLoading).toBe(false)
      expect(tasks.models?.models).toEqual(['new-model'])
      await home.get('.home-intake-model-trigger').trigger('click')
      await flushPromises()
      expect(home.find('[role="option"][data-model="new-model"]').exists()).toBe(true)
    } finally {
      get.mockImplementation(originalGet)
      shared.mockRestore()
      settings.unmount()
      home.unmount()
    }
  })

  it('refreshes both workspace model readers but never exposes model errors', async () => {
    const project = (await import('../../stores/projects')).useProjectStore()
    const tasks = (await import('../../stores/tasks')).useTaskStore()
    project.workspaces = [{ name: 'work & home', vault_root: 'work' }] as never
    project.activeWorkspace = 'work & home'
    const shared = vi.fn().mockResolvedValue({ status: 'error', workspace: 'work & home', generation: 1, error: 'private error' })
    tasks.fetchModels = shared as never
    vi.mocked(api.post).mockResolvedValueOnce({ name: 'opencode', ok: true, auth: 'oauth', state: 'ready' })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    await wrapper.findAll('.provider-connections .credential-row')[1]!.findAll('button').find(button => button.text() === 'Verify')!.trigger('click')
    await flushPromises()
    expect(shared).toHaveBeenCalledWith('work & home')
    expect(vi.mocked(api.get).mock.calls.some(([path]) => String(path) === '/api/models?workspace=work+%26+home')).toBe(true)
    expect(wrapper.text()).toContain('Model reads failed or are stale')
    expect(wrapper.text()).not.toContain('private error')
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('defers a remounted view’s provider read until verify %s', async (outcome) => {
    const project = (await import('../../stores/projects')).useProjectStore()
    project.workspaces = [{ name: 'A', vault_root: 'A' }] as never
    project.activeWorkspace = 'A'
    let fail!: (reason: Error) => void
    let settle!: (value: unknown) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise((resolve, reject) => { settle = resolve; fail = reject }))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const options = { global: { plugins: [router], stubs: { Teleport: true } } }
    const first = mount(mod.default as never, options)
    await flushPromises()
    await first.findAll('.provider-connections .credential-row')[1]!.findAll('button').find(button => button.text() === 'Verify')!.trigger('click')
    first.unmount()
    const before = vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers').length
    const second = mount(mod.default as never, options)
    await nextTick()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before)
    if (outcome === 'success') settle({ name: 'opencode', ok: true, auth: 'oauth', state: 'ready' })
    else fail(new Error('private failure'))
    await flushPromises()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before + 1)
    expect(second.text()).not.toContain('private failure')
    expect(second.text()).not.toContain('Provider and model reads completed')
    expect(second.text()).toContain('Chat providers')
    second.unmount()
  })

  it.each(['success', 'superseded'] as const)('reports %s model results without claiming catalog availability', async (status) => {
    const project = (await import('../../stores/projects')).useProjectStore()
    const tasks = (await import('../../stores/tasks')).useTaskStore()
    project.workspaces = [{ name: 'work', vault_root: 'work' }] as never
    project.activeWorkspace = 'work'
    tasks.fetchModels = vi.fn().mockResolvedValue({ status, workspace: 'work', generation: 1 }) as never
    vi.mocked(api.post).mockResolvedValueOnce({ name: 'opencode', ok: true, auth: 'oauth', state: 'ready' })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    await wrapper.findAll('.provider-connections .credential-row')[1]!.findAll('button').find(button => button.text() === 'Verify')!.trigger('click')
    await flushPromises()
    if (status === 'success') expect(wrapper.text()).toContain('Catalog availability and credential verification are unknown')
    else expect(wrapper.text()).not.toContain('Provider and model reads completed')
    expect(wrapper.text()).not.toContain('catalog refreshed successfully')
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('coalesces logout rereads across an A-B-A transition on %s', async (outcome) => {
    const project = (await import('../../stores/projects')).useProjectStore()
    const { pendingConfirm } = await import('../../lib/confirm')
    project.workspaces = [{ name: 'A', vault_root: 'A' }, { name: 'B', vault_root: 'B' }] as never
    project.activeWorkspace = 'A'
    let settle!: (value: unknown) => void
    let fail!: (reason: Error) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise((resolve, reject) => { settle = resolve; fail = reject }))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    const state = (wrapper.vm as unknown as { $: { setupState: unknown } }).$.setupState as {
      providerConnectionAction: (provider: string, action: 'logout') => Promise<void>
      fetchProviderKeys: () => Promise<void>
    }
    const logout = state.providerConnectionAction('claude', 'logout')
    expect(pendingConfirm.value?.destructive).toBe(true)
    pendingConfirm.value!.resolve(true)
    await nextTick()
    expect(vi.mocked(api.post)).toHaveBeenCalledWith('/api/settings/providers/claude/logout')
    const before = vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers').length
    const read1 = state.fetchProviderKeys()
    const read2 = state.fetchProviderKeys()
    project.activeWorkspace = 'B'
    project.activeWorkspace = 'A'
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before)
    if (outcome === 'success') settle({ ok: true })
    else fail(new Error('private logout error'))
    await Promise.all([logout, read1, read2])
    await flushPromises()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before + 1)
    expect(wrapper.text()).not.toContain('Logged out.')
    expect(wrapper.text()).not.toContain('private logout error')
    expect(wrapper.text()).not.toContain('Could not update provider status')
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('preserves the current logout result while rereads are queued on %s', async (outcome) => {
    const { pendingConfirm } = await import('../../lib/confirm')
    let settle!: (value: unknown) => void
    let fail!: (reason: Error) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise((resolve, reject) => { settle = resolve; fail = reject }))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    const state = (wrapper.vm as unknown as { $: { setupState: unknown } }).$.setupState as {
      providerConnectionAction: (provider: string, action: 'logout') => Promise<void>
      fetchProviderKeys: () => Promise<void>
    }
    const logout = state.providerConnectionAction('claude', 'logout')
    pendingConfirm.value!.resolve(true)
    await nextTick()
    const before = vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers').length
    const reads = [state.fetchProviderKeys(), state.fetchProviderKeys()]
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before)
    if (outcome === 'success') settle({ ok: true })
    else fail(new Error('private logout error'))
    await Promise.all([logout, ...reads])
    await flushPromises()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before + 1)
    expect(wrapper.text()).toContain(outcome === 'success' ? 'Logged out.' : 'Could not update provider status')
    expect(wrapper.text()).not.toContain('private logout error')
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('releases logout lock for a remounted Settings view on %s', async (outcome) => {
    const { pendingConfirm } = await import('../../lib/confirm')
    let settle!: (value: unknown) => void
    let fail!: (reason: Error) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise((resolve, reject) => { settle = resolve; fail = reject }))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const options = { global: { plugins: [router], stubs: { Teleport: true } } }
    const first = mount(mod.default as never, options)
    await flushPromises()
    const state = (first.vm as unknown as { $: { setupState: unknown } }).$.setupState as { providerConnectionAction: (provider: string, action: 'logout') => Promise<void> }
    const logout = state.providerConnectionAction('claude', 'logout')
    pendingConfirm.value!.resolve(true)
    await nextTick()
    first.unmount()
    const before = vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers').length
    const second = mount(mod.default as never, options)
    await nextTick()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before)
    if (outcome === 'success') settle({ ok: true })
    else fail(new Error('private logout error'))
    await logout
    await flushPromises()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before + 1)
    expect(second.text()).not.toContain('Logged out.')
    expect(second.text()).not.toContain('private logout error')
    second.unmount()
  })

  it.each(['success', 'failure'] as const)('coalesces provider rereads during verify on %s', async (outcome) => {
    const project = (await import('../../stores/projects')).useProjectStore()
    const tasks = (await import('../../stores/tasks')).useTaskStore()
    project.workspaces = [{ name: 'A', vault_root: 'A' }] as never
    project.activeWorkspace = 'A'
    tasks.fetchModels = vi.fn().mockResolvedValue({ status: 'success', workspace: 'A', generation: 1 }) as never
    let settle!: (value: unknown) => void
    let fail!: (reason: Error) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise((resolve, reject) => { settle = resolve; fail = reject }))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    const state = (wrapper.vm as unknown as { $: { setupState: unknown } }).$.setupState as {
      providerConnectionAction: (provider: string, action: 'verify') => Promise<void>
      fetchProviderKeys: () => Promise<void>
    }
    const verify = state.providerConnectionAction('opencode', 'verify')
    const before = vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers').length
    const reads = [state.fetchProviderKeys(), state.fetchProviderKeys()]
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before)
    if (outcome === 'success') settle({ name: 'opencode', ok: true, auth: 'oauth', state: 'ready' })
    else fail(new Error('private verify error'))
    await Promise.all([verify, ...reads])
    await flushPromises()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before + 1)
    expect(wrapper.text()).toContain(outcome === 'success' ? 'Catalog availability and credential verification are unknown' : 'Could not update provider status')
    expect(wrapper.text()).not.toContain('private verify error')
    wrapper.unmount()
  })

  it('coalesces post-mutation reads across two simultaneously mounted Settings views', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    let finish!: (value: unknown) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const options = { global: { plugins: [router], stubs: { Teleport: true } } }
    const first = mount(mod.default as never, options)
    const second = mount(mod.default as never, options)
    await flushPromises()
    type State = { providerConnectionAction: (provider: string, action: 'logout') => Promise<void>, fetchProviderKeys: () => Promise<void> }
    const a = (first.vm as unknown as { $: { setupState: State } }).$.setupState
    const b = (second.vm as unknown as { $: { setupState: State } }).$.setupState
    const logout = a.providerConnectionAction('claude', 'logout')
    pendingConfirm.value!.resolve(true)
    await nextTick()
    const before = vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers').length
    const reads = [a.fetchProviderKeys(), b.fetchProviderKeys()]
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before)
    finish({ ok: true })
    await Promise.all([logout, ...reads])
    await flushPromises()
    expect(vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/settings/providers')).toHaveLength(before + 1)
    expect(first.text()).toContain('Logged out.')
    first.unmount()
    second.unmount()
  })

  it('does not publish a provider GET started in another view before a mutation', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    let finishRead!: (value: unknown) => void
    let finishMutation!: (value: unknown) => void
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const options = { global: { plugins: [router], stubs: { Teleport: true } } }
    const first = mount(mod.default as never, options)
    const second = mount(mod.default as never, options)
    await flushPromises()
    type State = { providerConnectionAction: (provider: string, action: 'logout') => Promise<void>, fetchProviderKeys: () => Promise<void> }
    const a = (first.vm as unknown as { $: { setupState: State } }).$.setupState
    const b = (second.vm as unknown as { $: { setupState: State } }).$.setupState
    const get = vi.mocked(api.get)
    const original = get.getMockImplementation()!
    let held = false
    get.mockImplementation(((path: string) => {
      if (path === '/api/settings/providers' && !held) {
        held = true
        return new Promise<unknown>(resolve => { finishRead = resolve })
      }
      return original(path)
    }) as typeof api.get)
    vi.mocked(api.post).mockImplementationOnce(() => new Promise(resolve => { finishMutation = resolve }))
    try {
      const oldRead = a.fetchProviderKeys()
      const logout = b.providerConnectionAction('claude', 'logout')
      pendingConfirm.value!.resolve(true)
      await nextTick()
      finishMutation({ ok: true })
      await logout
      await flushPromises()
      expect(first.text()).toContain('Claude Code')
      finishRead({ connections: { claude: { name: 'claude', label: 'STALE SNAPSHOT', ok: true, auth: 'oauth', state: 'ready' } } })
      await oldRead
      await nextTick()
      expect(first.text()).not.toContain('STALE SNAPSHOT')
      expect(first.text()).toContain('Claude Code')
    } finally {
      get.mockImplementation(original)
      first.unmount()
      second.unmount()
    }
  })

  it.each([false, true])('starts a fresh provider read across a workspace transition (return to A: %s)', async (returnToA) => {
    const project = (await import('../../stores/projects')).useProjectStore()
    project.workspaces = [{ name: 'A', vault_root: 'A' }, { name: 'B', vault_root: 'B' }] as never
    project.activeWorkspace = 'A'
    const get = vi.mocked(api.get)
    const original = get.getMockImplementation()!
    const pending: ((value: unknown) => void)[] = []
    get.mockImplementation(((path: string) => path === '/api/settings/providers'
      ? new Promise<unknown>(resolve => pending.push(resolve)) : original(path)) as typeof api.get)
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await nextTick()
      expect(pending).toHaveLength(1)
      project.activeWorkspace = 'B'
      if (returnToA) project.activeWorkspace = 'A'
      expect(pending).toHaveLength(returnToA ? 3 : 2)
      pending[pending.length - 1]!({ connections: { claude: { name: 'claude', label: 'Fresh', ok: true, auth: 'oauth', state: 'ready' } } })
      await flushPromises()
      expect(wrapper.text()).toContain('Fresh')
      for (const resolve of pending.slice(0, -1)) resolve({ connections: { claude: { name: 'claude', label: 'Stale', ok: true, auth: 'oauth', state: 'ready' } } })
      await flushPromises()
      expect(wrapper.text()).toContain('Fresh')
      expect(wrapper.text()).not.toContain('Stale')
    } finally {
      for (const resolve of pending) resolve({ connections: {} })
      get.mockImplementation(original)
      wrapper.unmount()
    }
  })

  it.each([false, true])('rereads after another view mutates while inactive (initial read pending: %s)', async (initialPending) => {
    const { pendingConfirm } = await import('../../lib/confirm')
    const routerA = makeRouter()
    const routerB = makeRouter()
    await routerA.push('/settings/providers')
    await routerB.push('/settings/providers')
    await Promise.all([routerA.isReady(), routerB.isReady()])
    const mod = await import('../SettingsView.vue')
    const second = mount(mod.default as never, { global: { plugins: [routerB], stubs: { Teleport: true } } })
    await flushPromises()
    const get = vi.mocked(api.get)
    const original = get.getMockImplementation()!
    let finishOld: ((value: unknown) => void) | undefined
    let providerCalls = 0
    const fresh = { connections: { claude: { name: 'claude', label: 'Fresh provider', ok: true, auth: 'oauth', state: 'ready' } } }
    const old = { connections: { claude: { name: 'claude', label: 'Old provider', ok: true, auth: 'oauth', state: 'ready' } } }
    get.mockImplementation(((path: string) => {
      if (path !== '/api/settings/providers') return original(path)
      providerCalls++
      if (initialPending && providerCalls === 1) {
        return new Promise<unknown>(resolve => { finishOld = resolve })
      }
      if (!initialPending && providerCalls === 1) return Promise.resolve(old)
      return Promise.resolve(fresh)
    }) as typeof api.get)
    const first = mount(mod.default as never, { global: { plugins: [routerA], stubs: { Teleport: true } } })
    try {
      if (initialPending) await nextTick()
      else {
        await flushPromises()
        expect(first.text()).toContain('Old provider')
        expect(first.text()).not.toContain('Fresh provider')
      }
      await routerA.push('/settings/home')
      await nextTick()
      const state = (second.vm as unknown as { $: { setupState: { providerConnectionAction: (p: string, a: 'logout') => Promise<void> } } }).$.setupState
      const logout = state.providerConnectionAction('claude', 'logout')
      pendingConfirm.value!.resolve(true)
      await logout
      await flushPromises()
      if (finishOld) finishOld(old)
      const callsBeforeReturn = providerCalls
      await routerA.push('/settings/providers')
      await flushPromises()
      expect(providerCalls).toBe(callsBeforeReturn + 1)
      expect(first.text()).toContain('Fresh provider')
      expect(first.text()).not.toContain('Old provider')
      expect(first.find('[aria-label="Loading providers"]').exists()).toBe(false)
    } finally {
      finishOld?.({ connections: {} })
      get.mockImplementation(original)
      first.unmount()
      second.unmount()
    }
  })

  it('fences the Settings model reader across A-B-A including an old failure', async () => {
    const project = (await import('../../stores/projects')).useProjectStore()
    project.workspaces = [{ name: 'A', vault_root: 'A' }, { name: 'B', vault_root: 'B' }] as never
    project.activeWorkspace = 'A'
    const get = vi.mocked(api.get)
    const original = get.getMockImplementation()!
    const pending: { path: string, resolve: (value: unknown) => void, reject: (reason: Error) => void }[] = []
    get.mockImplementation(((path: string) => {
      if (path.startsWith('/api/models?')) return new Promise<unknown>((resolve, reject) => pending.push({ path, resolve, reject }))
      return original(path)
    }) as typeof api.get)
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    try {
      await nextTick()
      project.activeWorkspace = 'B'
      project.activeWorkspace = 'A'
      expect(pending.map(item => item.path)).toEqual(['/api/models?workspace=A', '/api/models?workspace=B', '/api/models?workspace=A'])
      pending[2]!.resolve({ models: [], provider_models: {}, thinking_levels: {} })
      await flushPromises()
      pending[0]!.reject(new Error('old sensitive error'))
      pending[1]!.reject(new Error('old B error'))
      await flushPromises()
      expect(wrapper.text()).not.toContain('Could not load model and effort options')
      expect(wrapper.text()).not.toContain('sensitive error')
    } finally {
      get.mockImplementation(original)
      wrapper.unmount()
    }
  })

  it('marks same-workspace model options stale on failure and clears the warning after retry', async () => {
    const project = (await import('../../stores/projects')).useProjectStore()
    project.workspaces = [{ name: 'A', vault_root: 'A' }] as never
    project.activeWorkspace = 'A'
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    const state = (wrapper.vm as unknown as { $: { setupState: unknown } }).$.setupState as { fetchWorkspaceModels: () => Promise<string> }
    const get = vi.mocked(api.get)
    const original = get.getMockImplementation()!
    get.mockImplementation(((path: string) => path.startsWith('/api/models?')
      ? Promise.reject(new Error('private catalog error')) : original(path)) as typeof api.get)
    try {
      expect(await state.fetchWorkspaceModels()).toBe('error')
      await nextTick()
      expect(wrapper.text()).toContain('showing stale options')
      expect(wrapper.text()).not.toContain('private catalog error')
      get.mockImplementation(original)
      await wrapper.findAll('button').find(button => button.text() === 'Retry model options')!.trigger('click')
      await flushPromises()
      expect(wrapper.text()).not.toContain('showing stale options')
    } finally {
      get.mockImplementation(original)
      wrapper.unmount()
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

    // OpenCode readiness is not a credential verification claim.
    const rows = wrapper.findAll('.provider-connections .credential-row')
    const opencodeRow = rows[1]!
    expect(opencodeRow.text()).toContain('CLI not ready')
    const actions = opencodeRow.findAll('.provider-connection-actions button').map((b) => b.text())
    expect(actions).toEqual(['Verify'])
    wrapper.unmount()
  })

  it('SettingsView renders the shared install block for a missing provider CLI', async () => {
    // Both providers share one branch: the install command, the PATH line, and
    // the docs link. opencode used to report `auth: 'missing'` with a bare
    // `opencode` command, so this block never rendered for it.
    const testApi = api as typeof api & {
      setResponse: (path: string, value: unknown) => void
      getResponse: (path: string) => unknown
    }
    const original = testApi.getResponse('/api/settings/providers') as {
      connections: Record<string, unknown>
    }
    testApi.setResponse('/api/settings/providers', {
      connections: {
        ...original.connections,
        claude: {
          ...(original.connections.claude as object),
          ok: false,
          auth: 'not_installed',
          state: 'not_installed',
          command: 'curl -fsSL https://claude.ai/install.sh | bash',
          detail: 'Claude Code is not installed on this machine.',
          install_url: 'https://code.claude.com/docs/en/quickstart#step-1-install-claude-code',
          path_command: 'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc',
        },
        opencode: {
          name: 'opencode',
          label: 'opencode',
          ok: false,
          auth: 'not_installed',
          state: 'not_installed',
          command: 'curl -fsSL https://opencode.ai/v2/install | bash',
          detail: 'opencode is not installed on this machine.',
          version: 'not installed',
          install_url: 'https://opencode.ai/download',
          path_command: 'echo \'export PATH="$HOME/.opencode/bin:$PATH"\' >> ~/.zshrc',
        },
      },
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
      const text = wrapper.text()
      expect(text).toContain('curl -fsSL https://opencode.ai/v2/install | bash')
      expect(text).toContain('Install it with')
      // The link is not glued to the PATH code (the whitespace a newline-only
      // text node used to swallow). `&nbsp;` renders as U+00A0.
      expect(text).toMatch(/~\/\.zshrc\s+installation guide/)
      const opencodeRow = wrapper.findAll('.provider-connections .credential-row')[1]!
      const link = opencodeRow.find('a[href="https://opencode.ai/download"]')
      expect(link.exists()).toBe(true)
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/settings/providers', original)
    }
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
    // Both providers expose an editable default-model selector inside their
    // cards. Claude's options are the Anthropic tiers; a save routes through
    // the same provider_default_models map as every other provider.
    const claudeSelector = wrapper.find('.provider-connections .model-selector')
    expect(claudeSelector.exists()).toBe(true)
    await claudeSelector.find('.model-selector__trigger').trigger('click')
    await flushPromises()
    const claudeOption = claudeSelector.findAll('.model-selector__item')
      .find((el) => el.attributes('data-model') === 'sonnet')
    expect(claudeOption).toBeTruthy()
    await claudeOption!.trigger('click')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      provider_default_models: { claude: 'sonnet' },
    })
    // The trigger names the model the way every picker does; the raw id is
    // what gets saved, and what the option's data still carries.
    expect(claudeSelector.find('.model-selector__trigger').text()).toContain('Sonnet')
    expect(wrapper.findAll('.provider-inline-defaults')[0]!.text()).toContain('Automatic — same as chat model')
    expect(wrapper.text()).not.toContain('Same as default')

    // Each card holds its default-model selector first, then Session insights.
    const opencodeSelector = wrapper.findAll('.provider-inline-defaults')[1]!
      .findAll('.model-selector')[0]!
    await opencodeSelector.find('.model-selector__trigger').trigger('click')
    await flushPromises()
    const opencodeOption = opencodeSelector.findAll('.model-selector__item')
      .find((el) => el.attributes('data-model') === 'openai/gpt-5.6-luna')
    expect(opencodeOption).toBeTruthy()
    await opencodeOption!.trigger('click')
    await flushPromises()
    // Per-provider default models go through the provider_default_models map.
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      provider_default_models: { claude: 'sonnet', opencode: 'openai/gpt-5.6-luna' },
    })

    wrapper.unmount()
  })

  it('SettingsView loads the model catalog once on tab open and saves effort', async () => {
    const originalGet = vi.mocked(api.get).getMockImplementation()!
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/api/models?refresh=1') return Promise.reject(new Error('catalog unavailable'))
      return originalGet(path)
    })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    try {
      await flushPromises()
      const effort = wrapper.find('select[aria-labelledby="thinking-label-claude"]')
      expect(effort.findAll('option').map((el) => el.attributes('value'))).toContain('high')
      expect(api.get).not.toHaveBeenCalledWith('/api/models?refresh=1')
      await effort.setValue('high')
      await flushPromises()
      expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
        provider_default_thinking: { claude: 'high' },
      })
      expect((effort.element as HTMLSelectElement).value).toBe('high')
    } finally {
      wrapper.unmount()
      vi.mocked(api.get).mockImplementation(originalGet)
    }
  })

  it('SettingsView reports catalog load failure instead of showing an Auto-only effort control', async () => {
    const originalGet = vi.mocked(api.get).getMockImplementation()!
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path.startsWith('/api/models')) return Promise.reject(new Error('catalog unavailable'))
      return originalGet(path)
    })
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    try {
      await flushPromises()
      expect(wrapper.text()).toContain('Effort options unavailable.')
      expect(wrapper.find('[role="alert"]').text()).toContain('Could not load model and effort options')
      expect(wrapper.text()).not.toContain('catalog unavailable')
      expect(wrapper.find('[aria-labelledby="thinking-label-claude"]').exists()).toBe(false)
    } finally {
      wrapper.unmount()
      vi.mocked(api.get).mockImplementation(originalGet)
    }
  })

  it('SettingsView offers a per-provider default permission mode', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // The permission-mode selector is back: manual / auto / bypass per provider.
    expect(wrapper.findAll('span.ws-label').some((el) => el.text() === 'Permission mode')).toBe(true)
    const modeLabels = wrapper.findAll('select option').map((el) => el.text())
    expect(modeLabels.join('\n')).toContain('Bypass — allow everything')

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
      // inline defaults are hidden when its catalog is empty. Claude still
      // renders its selector (its tiers are always available), so exactly one
      // defaults row shows.
      const inlineBlocks = wrapper.findAll('.provider-inline-defaults')
      expect(inlineBlocks.length).toBe(1)
      expect(wrapper.text()).toContain('Default model')
      // Claude's default model and Session insights model.
      expect(wrapper.findAll('.provider-connections .model-selector')).toHaveLength(2)
    } finally {
      wrapper.unmount()
      testApi.setResponse('/api/models', originalModels)
    }
  })

  it('SettingsView saves the Session insights model per provider', async () => {
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
    await router.push('/settings/providers')
    await router.isReady()
    const mod = await import('../SettingsView.vue')
    const wrapper = mount(mod.default as never, {
      global: { plugins: [router], stubs: { Teleport: true } },
    })
    await flushPromises()
    await nextTick()

    // Session insights is picked per provider, on that provider's card, and
    // stores a bare model id for that provider.
    expect(wrapper.find('.routine-select--provider').exists()).toBe(false)
    const pick = async (card: number, model: string) => {
      const selector = wrapper.findAll('.provider-inline-defaults')[card]!
        .findAll('.model-selector')[1]!
      await selector.find('.model-selector__trigger').trigger('click')
      await flushPromises()
      const option = selector.findAll('.model-selector__item')
        .find((el) => el.attributes('data-model') === model)
      expect(option).toBeTruthy()
      await option!.trigger('click')
      await flushPromises()
    }

    await pick(0, 'haiku')
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      provider_insights_models: { claude: 'haiku' },
    })

    await pick(1, 'anthropic/claude-opus-4.5')
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      provider_insights_models: { claude: 'haiku', opencode: 'anthropic/claude-opus-4.5' },
    })

    // Automatic clears just that provider's entry.
    await pick(0, '__ciao_insights_default__')
    expect(api.patch).toHaveBeenLastCalledWith('/api/settings/routines', {
      provider_insights_models: { opencode: 'anthropic/claude-opus-4.5' },
    })

    wrapper.unmount()
    mockApi.setResponse('/api/models', originalModels)
  })

  it('ProjectView mounts without throwing', async () => {
    const errors = await mountAndSettle(() => import('../ProjectView.vue'))
    expect(errors).toEqual([])
  })
})

describe('importWithin', () => {
  it('fails fast with the import named when the loader rejects', async () => {
    await expect(importWithin(() => Promise.reject(new Error('boom')), 'X.vue')).rejects.toThrow(
      'component import X.vue failed: boom',
    )
  })

  it('fails with the import named when the loader never settles', async () => {
    await expect(importWithin(() => new Promise(() => {}), 'Y.vue', 50)).rejects.toThrow(
      'component import Y.vue did not settle within 50 ms',
    )
  })

  it('returns the module when the loader resolves', async () => {
    await expect(importWithin(async () => ({ default: 1 }), 'Z.vue')).resolves.toEqual({ default: 1 })
  })
})
