// @vitest-environment jsdom

// File access links (`/settings/workspace?workspace=NAME#file-access`) switch
// the active workspace to NAME and land on Workspace > Overview with the File
// access selector focused. Following one is navigation: it never chooses a
// value and never writes. Leaving a workspace with unsaved edits asks first.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, disposePinia, getActivePinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { WorkspacesResponse } from '../../lib/types'

const state = vi.hoisted(() => ({
  workspaces: null as unknown,
  error: null as unknown,
  defer: false,
  pending: null as null | {
    resolve: (value: unknown) => void
    reject: (reason?: unknown) => void
  },
  deferredMcp: [] as string[],
  pendingMcp: [] as Array<{ workspace: string; resolve: (value: unknown) => void; reject: (reason: unknown) => void }>,
  failedMcp: [] as string[],
  mcpWorkspaceOverride: {} as Record<string, string>,
}))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') {
      if (state.defer) {
        return new Promise((resolve, reject) => {
          state.pending = { resolve, reject }
        })
      }
      if (state.error) return Promise.reject(state.error)
      return Promise.resolve(state.workspaces)
    }
    if (path === '/api/workspaces/archived') return Promise.resolve({ archived: [] })
    if (path.startsWith('/api/mcp/status')) {
      const workspace = new URL(path, 'http://localhost').searchParams.get('workspace') || 'personal'
      if (state.failedMcp.includes(workspace)) return Promise.reject(new Error('inventory unavailable'))
      const result = { workspace: state.mcpWorkspaceOverride[workspace] || workspace, agent_root: `/${workspace}`, shell: { kind: 'posix', cd_prefix: '' }, providers: [
        { provider: 'claude', available: true, inventory_state: 'loaded', servers: [] },
        { provider: 'opencode', available: true, inventory_state: 'loaded', servers: [] },
      ] }
      if (state.deferredMcp.includes(workspace)) return new Promise((resolve, reject) => state.pendingMcp.push({ workspace, resolve: () => resolve(result), reject }))
      return Promise.resolve(result)
    }
    if (path.startsWith('/api/proposals/handover')) {
      return Promise.resolve({
        workspace: 'personal',
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
      return Promise.resolve({ installed: false, binary_path: '', cli_available: false, profiles: [] })
    }
    // The Overview's Chats and models card reads the engine defaults.
    if (path === '/api/settings/routines') return Promise.resolve({ model_options: { anthropic: [] } })
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
const OVERVIEW = '/settings/workspace'

function response(): WorkspacesResponse {
  return {
    workspaces: [
      {
        name: 'personal',
        vault_root: 'personal/memory-vault',
        default_provider: 'claude',
        gws_profile: '',
        color: 'pink',
        agent_fs_scope: 'workspace',
      },
      {
        name: 'work',
        vault_root: 'work/memory-vault',
        default_provider: 'claude',
        gws_profile: '',
        color: 'pink',
        agent_fs_scope: 'machine',
      },
    ],
    active: 'personal',
    primary: 'personal',
    default_agent_fs_scope: 'workspace',
    provider_options: [{ value: 'claude', label: 'Claude' }],
  }
}

function target(name: string): string {
  return `${OVERVIEW}?workspace=${name}#file-access`
}

async function settle(): Promise<void> {
  await flushPromises()
  await nextTick()
  await flushPromises()
  await nextTick()
}

async function mountSettings(path = OVERVIEW): Promise<{
  wrapper: VueWrapper
  router: Router
}> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push(path)
  await router.isReady()
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  return { wrapper, router }
}

function nameInput(wrapper: VueWrapper) {
  return wrapper.find('#workspace-name-input')
}

async function activeWorkspace(): Promise<string> {
  const { useProjectStore } = await import('../../stores/projects')
  return useProjectStore().activeWorkspace
}

function fsSelect(wrapper: VueWrapper, name: string) {
  return wrapper.find(`#workspace-file-access-${name}`)
}

function rect(top: number): DOMRect {
  return {
    top,
    bottom: top + 100,
    left: 0,
    right: 100,
    width: 100,
    height: 100,
    x: 0,
    y: top,
    toJSON: () => ({}),
  } as DOMRect
}

async function expectNoWrites(): Promise<void> {
  const { api } = await import('../../lib/api')
  expect(api.post).not.toHaveBeenCalled()
  expect(api.patch).not.toHaveBeenCalled()
  expect(api.del).not.toHaveBeenCalled()
}

let scrollTo: ReturnType<typeof vi.fn>
let windowScrollTo: ReturnType<typeof vi.fn>
let reduceMotion = false

beforeEach(() => {
  // switchWorkspace persists the active workspace; start every test on personal.
  localStorage.clear()
  setActivePinia(createPinia())
  state.workspaces = response()
  state.error = null
  state.defer = false
  state.pending = null
  state.deferredMcp = []
  state.pendingMcp = []
  state.failedMcp = []
  state.mcpWorkspaceOverride = {}
  reduceMotion = false
  document.body.innerHTML = ''
  scrollTo = vi.fn()
  windowScrollTo = vi.fn()
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: scrollTo })
  Object.defineProperty(window, 'scrollTo', { configurable: true, value: windowScrollTo })
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: reduceMotion && query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
})

afterEach(() => {
  const pinia = getActivePinia()
  if (pinia) disposePinia(pinia)
  document.body.innerHTML = ''
})

describe('serialized latest workspace', () => {
  it('prevents a stale guarded switch and displays the latest workspace inventory', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    let answer!: (allow: boolean) => void
    const guard = vi.fn(() => new Promise<boolean>(resolve => { answer = resolve }))
    const remove = store.registerWorkspaceSwitchGuard(guard)
    try {
      await settle()
      await router.push('/settings/mcp?workspace=work&provider=opencode')
      await settle()
      expect(guard).toHaveBeenCalledOnce()
      await router.push('/settings/mcp?workspace=personal&provider=claude')
      await settle()
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      answer(true)
      await settle()
      expect(store.activeWorkspace).toBe('personal')
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
      expect(wrapper.get('#mcp-tab-claude').attributes('aria-selected')).toBe('true')
    } finally {
      remove()
      wrapper.unmount()
    }
  })
  it('drains A/B/A after a real guard and leaves the final A globally active', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    let answer!: (allow: boolean) => void
    const guard = vi.fn().mockImplementationOnce(() => new Promise<boolean>(resolve => { answer = resolve })).mockResolvedValue(true)
    const remove = store.registerWorkspaceSwitchGuard(guard)
    try {
      await settle()
      await router.push('/settings/mcp?workspace=work&provider=opencode')
      await settle()
      await router.push('/settings/mcp?workspace=personal&provider=claude')
      await router.push('/settings/mcp?workspace=work&provider=opencode')
      await settle()
      expect(guard).toHaveBeenCalledTimes(1)
      answer(true)
      await settle()
      expect(guard).toHaveBeenCalledTimes(2)
      expect(store.activeWorkspace).toBe('work')
      expect(wrapper.get('#mcp-servers').text()).toContain('work')
      expect(wrapper.get('#mcp-tab-opencode').attributes('aria-selected')).toBe('true')
    } finally { remove(); wrapper.unmount() }
  })
  it('reloads the shared inventory after completed personal, work, personal visits', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp?workspace=personal&provider=claude')
    const { api } = await import('../../lib/api')
    const reads = (name: string) => vi.mocked(api.get).mock.calls.filter(([path]) => path === `/api/mcp/status?workspace=${name}`).length
    try {
      await settle()
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
      expect(wrapper.get('#mcp-tab-claude').attributes('aria-selected')).toBe('true')
      const personalReads = reads('personal')
      const workReads = reads('work')
      await router.push('/settings/mcp?workspace=work&provider=opencode')
      await settle()
      expect(reads('work')).toBe(workReads + 1)
      expect(wrapper.get('#mcp-servers').text()).toContain('work')
      expect(wrapper.get('#mcp-tab-opencode').attributes('aria-selected')).toBe('true')
      await router.push('/settings/mcp?workspace=personal&provider=opencode')
      await settle()
      expect(reads('personal')).toBe(personalReads + 1)
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
      expect(wrapper.get('#mcp-tab-opencode').attributes('aria-selected')).toBe('true')
    } finally { wrapper.unmount() }
  })
  it('does not reuse a completed load after the shared inventory changes workspace', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp?workspace=personal&provider=claude')
    const { api } = await import('../../lib/api')
    const reads = () => vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/mcp/status?workspace=personal').length
    try {
      await settle()
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
      state.mcpWorkspaceOverride.personal = 'work'
      await wrapper.get('.mcp-refresh').trigger('click')
      await settle()
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      const previousReads = reads()
      delete state.mcpWorkspaceOverride.personal
      await router.push('/settings/mcp?workspace=personal&provider=claude')
      await settle()
      expect(reads()).toBe(previousReads + 1)
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
      expect(wrapper.get('#mcp-tab-claude').attributes('aria-selected')).toBe('true')
    } finally { wrapper.unmount() }
  })
  it('does not reuse a completed load while a separate refresh has cleared the inventory', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp?workspace=personal&provider=claude')
    try {
      await settle()
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
      state.deferredMcp = ['personal']
      await wrapper.get('.mcp-refresh').trigger('click')
      await settle()
      expect(state.pendingMcp).toHaveLength(1)
      await router.push('/settings/mcp?workspace=personal&provider=claude')
      await settle()
      expect(state.pendingMcp).toHaveLength(2)
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      const [older, latest] = state.pendingMcp.splice(0)
      latest!.resolve(undefined)
      await settle()
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
      older!.reject(new Error('stale refresh failed'))
      await settle()
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
      expect(wrapper.find('[role=alert]').exists()).toBe(false)
    } finally { wrapper.unmount() }
  })
  it('waits for an already committed switch to settle before starting the latest switch', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    const actualSwitch = store.switchWorkspace.bind(store)
    let finish!: () => void
    const hold = new Promise<void>(resolve => { finish = resolve })
    let outstanding = 0
    let maximum = 0
    const spy = vi.spyOn(store, 'switchWorkspace').mockImplementation(async (name, options) => {
      maximum = Math.max(maximum, ++outstanding)
      try {
        const result = await actualSwitch(name, options)
        if (name === 'work') await hold
        return result
      } finally { outstanding-- }
    })
    try {
      await settle()
      await router.push('/settings/mcp?workspace=work')
      await settle()
      expect(store.activeWorkspace).toBe('work')
      await router.push('/settings/mcp?workspace=personal')
      await settle()
      expect(spy).toHaveBeenCalledTimes(1)
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      finish()
      await settle()
      expect(maximum).toBe(1)
      expect(store.activeWorkspace).toBe('personal')
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
    } finally { finish(); spy.mockRestore(); wrapper.unmount() }
  })
  it('withdraws a personal switch when the later work target is already active', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    await store.switchWorkspace('work', { transition: false })
    let answer!: (allow: boolean) => void
    const remove = store.registerWorkspaceSwitchGuard(() => new Promise(resolve => { answer = resolve }))
    try {
      await settle()
      await router.push('/settings/mcp?workspace=personal')
      await settle()
      await router.push('/settings/mcp?workspace=work')
      await settle()
      answer(true)
      await settle()
      expect(store.activeWorkspace).toBe('work')
      expect(wrapper.get('#mcp-servers').text()).toContain('work')
    } finally { remove(); wrapper.unmount() }
  })
  it('honours explicit relinks but refresh keeps a manual provider choice', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp?workspace=personal&provider=opencode')
    try {
      await settle()
      expect(wrapper.get('#mcp-tab-opencode').attributes('aria-selected')).toBe('true')
      await wrapper.get('#mcp-tab-claude').trigger('click')
      await wrapper.get('.mcp-refresh').trigger('click')
      await settle()
      expect(wrapper.get('#mcp-tab-claude').attributes('aria-selected')).toBe('true')
      await router.push('/settings/mcp?workspace=personal&provider=opencode')
      await settle()
      expect(wrapper.get('#mcp-tab-opencode').attributes('aria-selected')).toBe('true')
      await wrapper.get('#mcp-tab-claude').trigger('click')
      await router.push({ path: '/settings/mcp', query: { workspace: 'personal', provider: 'opencode' }, force: true })
      await settle()
      expect(wrapper.get('#mcp-tab-opencode').attributes('aria-selected')).toBe('true')
    } finally { wrapper.unmount() }
  })
  it('shows an explicit failure and no inventory when the latest guard refuses', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    const remove = store.registerWorkspaceSwitchGuard(async () => false)
    try {
      await settle()
      await router.push('/settings/mcp?workspace=work&provider=opencode')
      await settle()
      expect(store.activeWorkspace).toBe('personal')
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      expect(wrapper.get('[role=alert]').text()).toContain('was not opened')
    } finally { remove(); wrapper.unmount() }
  })
  it('keeps A after an already-committed A is followed by refused B without displaying A as B', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    const actualSwitch = store.switchWorkspace.bind(store)
    let finish!: () => void
    const hold = new Promise<void>(resolve => { finish = resolve })
    const spy = vi.spyOn(store, 'switchWorkspace').mockImplementation(async (name, options) => {
      const result = await actualSwitch(name, options)
      if (name === 'work') await hold
      return result
    })
    try {
      await settle()
      await router.push('/settings/mcp?workspace=work')
      await settle()
      expect(store.activeWorkspace).toBe('work')
      const remove = store.registerWorkspaceSwitchGuard(async () => false)
      try {
        await router.push('/settings/mcp?workspace=personal')
        await settle()
        finish()
        await settle()
        expect(store.activeWorkspace).toBe('work')
        expect(wrapper.find('#mcp-servers').exists()).toBe(false)
        expect(wrapper.get('[role=alert]').text()).toContain('was not opened')
      } finally { remove() }
    } finally { finish(); spy.mockRestore(); wrapper.unmount() }
  })
  it('falls back to the workspace default for an invalid provider without changing the query', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp?workspace=personal&provider=other')
    try {
      await settle()
      expect(wrapper.get('#mcp-tab-claude').attributes('aria-selected')).toBe('true')
      expect(router.currentRoute.value.query.provider).toBe('other')
    } finally { wrapper.unmount() }
  })
  it('shows an explicit failure if the workspace guard errors', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    const remove = store.registerWorkspaceSwitchGuard(async () => { throw new Error('guard failed') })
    try {
      await settle()
      await router.push('/settings/mcp?workspace=work')
      await settle()
      expect(store.activeWorkspace).toBe('personal')
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      expect(wrapper.get('[role=alert]').text()).toContain('could not be opened')
    } finally { remove(); wrapper.unmount() }
  })
  it('withdraws a pending switch on unmount', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    let answer!: (allow: boolean) => void
    const remove = store.registerWorkspaceSwitchGuard(() => new Promise(resolve => { answer = resolve }))
    await settle()
    await router.push('/settings/mcp?workspace=work')
    await settle()
    wrapper.unmount()
    answer(true)
    await settle()
    expect(store.activeWorkspace).toBe('personal')
    remove()
  })
  it('hides stale inventory and drains the later workspace after a late inventory', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    try {
      await settle()
      state.deferredMcp = ['work']
      await router.push('/settings/mcp?workspace=work&provider=opencode')
      await settle()
      expect(store.activeWorkspace).toBe('work')
      expect(state.pendingMcp.length).toBeGreaterThan(0)
      state.deferredMcp = ['work', 'personal']
      await router.push('/settings/mcp?workspace=personal&provider=claude')
      await settle()
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      state.pendingMcp.splice(0).forEach(pending => pending.resolve(undefined))
      await settle()
      expect(store.activeWorkspace).toBe('personal')
      expect(state.pendingMcp.some(pending => pending.workspace === 'personal')).toBe(true)
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      state.deferredMcp = []
      state.pendingMcp.splice(0).forEach(pending => pending.resolve(undefined))
      await settle()
      expect(store.activeWorkspace).toBe('personal')
      expect(wrapper.get('#mcp-servers').text()).toContain('personal')
    } finally { wrapper.unmount() }
  })
  it('shares the drain with File access links', async () => {
    const { wrapper, router } = await mountSettings('/settings/mcp')
    const { useProjectStore } = await import('../../stores/projects')
    const store = useProjectStore()
    let answer!: (allow: boolean) => void
    const remove = store.registerWorkspaceSwitchGuard(() => new Promise(resolve => { answer = resolve }))
    try {
      await settle()
      await router.push('/settings/mcp?workspace=work')
      await settle()
      await router.push(target('personal'))
      await settle()
      answer(true)
      await settle()
      expect(store.activeWorkspace).toBe('personal')
      expect(router.currentRoute.value.fullPath).toBe(`${OVERVIEW}#file-access`)
      expect(document.activeElement?.id).toBe('workspace-file-access-personal')
    } finally { remove(); wrapper.unmount() }
  })
  it.each(['/settings/mcp?workspace=missing', '/settings/mcp?workspace=work&workspace=personal'])('refuses invalid target %s', async path => {
    const { wrapper } = await mountSettings(path)
    try {
      await settle()
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      expect(wrapper.get('[role=alert]').text()).toContain('workspace')
      expect(await activeWorkspace()).toBe('personal')
    } finally { wrapper.unmount() }
  })
  it('reports workspace-list or inventory load failure rather than showing another workspace', async () => {
    state.error = new Error('registry unavailable')
    const first = await mountSettings('/settings/mcp?workspace=work')
    try {
      await settle()
      expect(first.wrapper.find('#mcp-servers').exists()).toBe(false)
      expect(first.wrapper.get('[role=alert]').text()).toContain('Could not load workspaces')
    } finally { first.wrapper.unmount() }
    state.error = null
    state.failedMcp = ['work']
    const second = await mountSettings('/settings/mcp?workspace=work')
    try {
      await settle()
      expect(second.wrapper.find('#mcp-servers').exists()).toBe(false)
      expect(second.wrapper.get('[role=alert]').text()).toContain('Could not load MCP settings')
    } finally { second.wrapper.unmount() }
  })
  it('retries a failed inventory only when the same explicit link is revisited', async () => {
    state.failedMcp = ['work']
    const { wrapper, router } = await mountSettings('/settings/mcp?workspace=work&provider=opencode')
    const { api } = await import('../../lib/api')
    const reads = () => vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/mcp/status?workspace=work').length
    try {
      await settle()
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      expect(wrapper.get('[role=alert]').text()).toContain('Could not load MCP settings')
      const failedReads = reads()
      expect(failedReads).toBeGreaterThan(0)
      state.failedMcp = []
      await settle()
      expect(reads()).toBe(failedReads)
      await router.push('/settings/mcp?workspace=work&provider=opencode')
      await settle()
      expect(reads()).toBe(failedReads + 1)
      expect(wrapper.get('#mcp-servers').text()).toContain('work')
      expect(wrapper.get('#mcp-tab-opencode').attributes('aria-selected')).toBe('true')
    } finally { wrapper.unmount() }
  })
  it.each(['identical', 'provider change'] as const)('retries a stale failed in-flight inventory after %s', async kind => {
    state.deferredMcp = ['work']
    const { wrapper, router } = await mountSettings('/settings/mcp?workspace=work&provider=claude')
    const { api } = await import('../../lib/api')
    const reads = () => vi.mocked(api.get).mock.calls.filter(([path]) => path === '/api/mcp/status?workspace=work').length
    try {
      await settle()
      expect(state.pendingMcp).toHaveLength(1)
      const before = reads()
      const first = state.pendingMcp.shift()!
      const provider = kind === 'identical' ? 'claude' : 'opencode'
      await router.push(`/settings/mcp?workspace=work&provider=${provider}`)
      await settle()
      expect(reads()).toBe(before)
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      first.reject(new Error('first inventory failed'))
      await settle()
      expect(reads()).toBe(before + 1)
      expect(state.pendingMcp).toHaveLength(1)
      expect(wrapper.find('#mcp-servers').exists()).toBe(false)
      state.pendingMcp.shift()!.resolve(undefined)
      await settle()
      expect(wrapper.get('#mcp-servers').text()).toContain('work')
      expect(wrapper.get(`#mcp-tab-${provider}`).attributes('aria-selected')).toBe('true')
      expect(wrapper.find('[role=alert]').exists()).toBe(false)
    } finally { wrapper.unmount() }
  })
})

describe('Settings > Workspace File access deep link', () => {
  it('switches to the linked workspace, focuses File access and writes nothing', async () => {
    const { wrapper, router } = await mountSettings(target('work'))
    try {
      await settle()

      expect(router.currentRoute.value.fullPath).toBe(`${OVERVIEW}#file-access`)
      expect(await activeWorkspace()).toBe('work')
      expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('work')
      const row = wrapper.find('#workspace-file-access-row-work')
      expect(row.classes()).toContain('workspace-card--target')
      const select = fsSelect(wrapper, 'work')
      expect(select.exists()).toBe(true)
      expect((select.element as HTMLSelectElement).value).toBe('machine')
      expect(document.activeElement).toBe(select.element)

      expect(scrollTo).toHaveBeenCalledTimes(1)
      expect(vi.mocked(scrollTo).mock.contexts[0]).toBe(wrapper.find('.pane-body').element)
      expect(windowScrollTo).not.toHaveBeenCalled()
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it("a File access link opened directly focuses the NAMED workspace's select, not the previously active one", async () => {
    const { wrapper, router } = await mountSettings(`${OVERVIEW}?workspace=work#file-access`)
    try {
      await settle()

      expect(await activeWorkspace()).toBe('work')
      expect(document.activeElement?.id).toBe('workspace-file-access-work')
      expect(fsSelect(wrapper, 'personal').exists()).toBe(false)
      expect(router.currentRoute.value.fullPath).toBe(`${OVERVIEW}#file-access`)
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('scrolls the File access field row, not the card header, from the pane top', async () => {
    const { wrapper, router } = await mountSettings(target('work'))
    try {
      await settle()
      scrollTo.mockClear()

      // Clear the hash first so the next link is a fresh arrival on the page.
      await router.replace(OVERVIEW)
      await settle()
      scrollTo.mockClear()

      const body = wrapper.find('.pane-body').element as HTMLElement
      const card = wrapper.find('.card').element as HTMLElement
      const fieldRow = wrapper.find('#workspace-file-access-row-work').element as HTMLElement
      // Distinct rects make the two candidates unambiguous: measuring the
      // card would scroll 500px short of the field row.
      Object.defineProperty(body, 'scrollTop', { configurable: true, value: 500 })
      body.getBoundingClientRect = () => rect(40)
      card.getBoundingClientRect = () => rect(200)
      fieldRow.getBoundingClientRect = () => rect(700)

      await router.replace(`${OVERVIEW}#file-access`)
      await settle()

      // 500 (pane) + 700 - 40 (measured field row) - 16 (offset).
      expect(scrollTo).toHaveBeenCalledWith({ top: 1144, behavior: 'smooth' })
    } finally {
      wrapper.unmount()
    }
  })

  it('a same-address navigation does not scroll to or refocus File access again', async () => {
    const { wrapper, router } = await mountSettings(target('work'))
    try {
      await settle()
      expect(document.activeElement?.id).toBe('workspace-file-access-work')
      scrollTo.mockClear()
      const other = document.createElement('button')
      document.body.append(other)
      other.focus()

      await router.push({ path: OVERVIEW, hash: '#file-access', force: true })
      await settle()

      expect(scrollTo).not.toHaveBeenCalled()
      expect(document.activeElement).toBe(other)
    } finally {
      wrapper.unmount()
    }
  })

  it('waits for a delayed workspace list before switching', async () => {
    state.defer = true
    const { wrapper, router } = await mountSettings()
    try {
      await settle()
      expect(wrapper.find('.workspace-list .workspace-card').exists()).toBe(false)

      await router.push(target('work'))
      await nextTick()
      expect(router.currentRoute.value.path).toBe(OVERVIEW)
      expect(await activeWorkspace()).toBe('personal')

      state.defer = false
      state.pending!.resolve(state.workspaces)
      state.pending = null
      await settle()

      expect(router.currentRoute.value.fullPath).toBe(`${OVERVIEW}#file-access`)
      expect(await activeWorkspace()).toBe('work')
      expect(document.activeElement).toBe(fsSelect(wrapper, 'work').element)
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('moves to a new target when the route changes', async () => {
    const { wrapper, router } = await mountSettings(target('personal'))
    try {
      await settle()
      expect(await activeWorkspace()).toBe('personal')
      expect(fsSelect(wrapper, 'personal').exists()).toBe(true)

      await router.push(target('work'))
      await settle()

      expect(await activeWorkspace()).toBe('work')
      expect(fsSelect(wrapper, 'personal').exists()).toBe(false)
      expect(document.activeElement).toBe(fsSelect(wrapper, 'work').element)
    } finally {
      wrapper.unmount()
    }
  })

  it('does not let a superseded link steal focus after a later one', async () => {
    state.defer = true
    const { wrapper, router } = await mountSettings()
    try {
      await router.push(target('personal'))
      await nextTick()
      await router.push(target('work'))
      await nextTick()

      state.defer = false
      state.pending!.resolve(state.workspaces)
      state.pending = null
      await settle()

      expect(await activeWorkspace()).toBe('work')
      expect(fsSelect(wrapper, 'personal').exists()).toBe(false)
      expect(document.activeElement).toBe(fsSelect(wrapper, 'work').element)
    } finally {
      wrapper.unmount()
    }
  })

  it('keeps the same draft when that workspace is linked again', async () => {
    const { wrapper, router } = await mountSettings(target('work'))
    try {
      await settle()
      await nameInput(wrapper).setValue('work-renamed')
      await fsSelect(wrapper, 'work').setValue('workspace')
      await nextTick()

      await router.push(OVERVIEW)
      await settle()
      await router.push(target('work'))
      await settle()

      expect(router.currentRoute.value.fullPath).toBe(`${OVERVIEW}#file-access`)
      expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('work-renamed')
      expect((fsSelect(wrapper, 'work').element as HTMLSelectElement).value).toBe('workspace')
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('asks before leaving a different workspace with an unsaved name draft, and stays when declined', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    const { wrapper, router } = await mountSettings(OVERVIEW)
    try {
      await settle()
      await nameInput(wrapper).setValue('home')

      await router.push(target('work'))
      await settle()

      expect(pendingConfirm.value).toBeTruthy()
      expect(pendingConfirm.value!.message).toBe('Discard unsaved changes to personal?')
      pendingConfirm.value!.resolve(false)
      await settle()

      expect(await activeWorkspace()).toBe('personal')
      expect(router.currentRoute.value.path).toBe(OVERVIEW)
      const status = wrapper.find('.workspace-link-status')
      expect(status.exists()).toBe(true)
      expect(status.text()).toContain('personal')
      expect(status.text()).toContain('work')
      expect(status.text()).toContain('kept')
      expect(scrollTo).not.toHaveBeenCalled()

      // The draft is still there when the user goes back.
      await router.push(OVERVIEW)
      await settle()
      expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('home')
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('switches and drops the draft when the discard is confirmed', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    const { wrapper, router } = await mountSettings(OVERVIEW)
    try {
      await settle()
      await nameInput(wrapper).setValue('home')

      await router.push(target('work'))
      await settle()
      pendingConfirm.value!.resolve(true)
      await settle()

      expect(await activeWorkspace()).toBe('work')
      expect(router.currentRoute.value.fullPath).toBe(`${OVERVIEW}#file-access`)
      expect(document.activeElement).toBe(fsSelect(wrapper, 'work').element)

      const { useProjectStore } = await import('../../stores/projects')
      await useProjectStore().switchWorkspace('personal', { transition: false })
      await settle()
      expect((nameInput(wrapper).element as HTMLInputElement).value).toBe('personal')
      expect(wrapper.find('.workspace-save').exists()).toBe(false)
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('also asks over an unsaved File access change alone', async () => {
    const { pendingConfirm } = await import('../../lib/confirm')
    const { wrapper, router } = await mountSettings(OVERVIEW)
    try {
      await settle()
      await fsSelect(wrapper, 'personal').setValue('machine')
      await nextTick()

      await router.push(target('work'))
      await settle()

      expect(pendingConfirm.value!.message).toBe('Discard unsaved changes to personal?')
      pendingConfirm.value!.resolve(false)
      await settle()

      expect(await activeWorkspace()).toBe('personal')
      expect(wrapper.find('.workspace-link-status').text()).toContain('kept')
      await router.push(OVERVIEW)
      await settle()
      expect((fsSelect(wrapper, 'personal').element as HTMLSelectElement).value).toBe('machine')
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('does nothing for a plain tab or a malformed link', async () => {
    const { wrapper, router } = await mountSettings()
    try {
      await settle()
      for (const path of [
        OVERVIEW,
        `${OVERVIEW}?workspace=work`,
        `${OVERVIEW}?workspace=work#other`,
        `${OVERVIEW}?workspace=work&action=save#file-access`,
        `${OVERVIEW}?workspace=work&workspace=personal#file-access`,
        `/settings/providers?workspace=work#file-access`,
      ]) {
        await router.push(path)
        await settle()
        expect(router.currentRoute.value.fullPath, path).toBe(path)
        expect(await activeWorkspace(), path).toBe('personal')
        expect(wrapper.find('.workspace-link-status').exists(), path).toBe(false)
      }
      expect(scrollTo).not.toHaveBeenCalled()
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('names a missing workspace and says nothing changed', async () => {
    const { wrapper, router } = await mountSettings(target('ghost'))
    try {
      await settle()

      expect(router.currentRoute.value.path).toBe(OVERVIEW)
      expect(await activeWorkspace()).toBe('personal')
      const status = wrapper.find('.workspace-link-status')
      expect(status.exists()).toBe(true)
      expect(status.text()).toContain('ghost')
      expect(status.text()).toContain('No settings changed')
      expect(scrollTo).not.toHaveBeenCalled()
      await expectNoWrites()

      await router.push(OVERVIEW)
      await settle()
      expect(wrapper.find('.workspace-link-status').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('reports a load failure as an error rather than a missing workspace', async () => {
    state.error = new Error('boom')
    const { wrapper, router } = await mountSettings(target('work'))
    try {
      await settle()

      expect(wrapper.text()).toContain('Failed to load workspaces: boom')
      expect(wrapper.find('.workspace-link-status').exists()).toBe(false)
      expect(router.currentRoute.value.path).toBe(OVERVIEW)
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('keeps the target reachable when motion is reduced', async () => {
    reduceMotion = true
    const { wrapper } = await mountSettings(target('work'))
    try {
      await settle()

      expect(document.activeElement).toBe(fsSelect(wrapper, 'work').element)
      expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ behavior: 'auto' }))
      expect(wrapper.find('#workspace-file-access-row-work').classes()).toContain('workspace-card--target')
    } finally {
      wrapper.unmount()
    }
  })
})
