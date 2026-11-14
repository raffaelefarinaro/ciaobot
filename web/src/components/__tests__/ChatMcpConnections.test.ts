// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, reactive } from 'vue'
import { flushPromises, mount, shallowMount } from '@vue/test-utils'
import { createPinia as createRawPinia, disposePinia, setActivePinia } from 'pinia'
import { useChatMcpConnections } from '../../composables/useChatMcpConnections'
import ChatMcpConnections from '../ChatMcpConnections.vue'
import ChatPanel from '../ChatPanel.vue'
import NeedsYouCard from '../NeedsYouCard.vue'
import * as loopback from '../../lib/loopback'
import { useProjectStore } from '../../stores/projects'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import type { ChatAction, McpNativeServer, McpStatus } from '../../lib/types'

const get = vi.hoisted(() => vi.fn())
const post = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({ api: { get, post, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() } }))

const server = (over: Partial<McpNativeServer> = {}): McpNativeServer => ({
  name: 'linear', scope: 'project', source: 'settings', kind: 'http', enabled: true, allowed: true,
  connections: [], ...over,
})
const status = (servers: McpNativeServer[], workspace = 'work', provider: 'claude' | 'opencode' = 'claude'): McpStatus => ({
  workspace, agent_root: '/work', shell: { kind: 'posix', cd_prefix: 'cd' },
  providers: [{ provider, available: true, inventory_state: 'loaded', servers }],
})
const action = (state: ChatAction['state'], id = 'a', serverName = 'linear'): ChatAction => ({
  id, kind: 'mcp_auth', origin: 'engine', state, params: { provider: 'claude', server: serverName }, error: '', created_at: 1,
})

describe('useChatMcpConnections observer', () => {
  const piniaInstances: Array<ReturnType<typeof createRawPinia>> = []
  function createPinia() {
    const pinia = createRawPinia()
    piniaInstances.push(pinia)
    return pinia
  }
  const observers: Array<ReturnType<typeof mount>> = []
  function observe(options: Parameters<typeof useChatMcpConnections>[0]) {
    let controller!: ReturnType<typeof useChatMcpConnections>
    observers.push(mount(defineComponent({ setup() {
      controller = useChatMcpConnections(options)
      return () => h('div')
    } })))
    return controller
  }
  afterEach(() => { observers.splice(0).forEach(wrapper => wrapper.unmount()); piniaInstances.splice(0).forEach(disposePinia); get.mockReset(); post.mockReset(); vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals() })

  it.each(['claude', 'opencode'] as const)('%s closes placeholder and shows error for creation and start refusals', async provider => {
    for (const refusal of ['create', 'start']) {
      const pinia = createPinia()
      setActivePinia(pinia)
      get.mockResolvedValue(status([server({ connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] })], 'work', provider))
      const auth = { ...action('open'), params: { provider, server: 'linear' } }
      post.mockImplementation((path: string) => refusal === 'create' || path.endsWith('/start')
        ? Promise.reject(new Error('refused')) : Promise.resolve({ action: auth }))
      const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
      const popup = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
      let controller!: ReturnType<typeof useChatMcpConnections>
      const wrapper = mount(defineComponent({ setup() {
        controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => [], archived: () => false })
        return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
      } }), { global: { plugins: [pinia] } })
      await flushPromises()
      await wrapper.get('.chat-mcp-sign-in').trigger('click')
      await flushPromises()
      expect(tab.close).toHaveBeenCalled()
      expect(wrapper.get('[role=alert]').text()).toContain('could not be started')
      expect(post).toHaveBeenCalledTimes(refusal === 'create' ? 1 : 2)
      expect(popup).toHaveBeenCalledOnce()
      wrapper.unmount()
      popup.mockRestore()
      post.mockReset()
    }
  })

  it('selects local-disabled over user-enabled identically in both input orders', async () => {
    const local = server({ scope: 'local', source: 'a', enabled: false, connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] })
    const user = server({ scope: 'user', source: 'z', enabled: true, connections: [{ chat_id: 'chat-a', state: 'connected' }] })
    const options = { workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [action('waiting')], archived: () => false }
    get.mockResolvedValue(status([local, user]))
    const first = observe(options)
    await flushPromises()
    get.mockResolvedValue(status([user, local]))
    const reversed = observe(options)
    await flushPromises()
    expect(first.rows.value).toEqual(reversed.rows.value)
    expect(first.rows.value[0]).toMatchObject({ server: local, enabled: false, state: 'disabled' })
    expect(first.problemCount.value).toBe(0)
    expect(first.canStart(first.rows.value[0]!)).toBe(false)
  })
  it.each(['claude', 'opencode'] as const)('%s rejects malformed native authorization metadata without starting', async provider => {
    const add = vi.spyOn(document, 'addEventListener')
    const remove = vi.spyOn(document, 'removeEventListener')
    for (const bad of [{ enabled: 'false' }, { allowed: 'false' }, { allowed: 'true' }, { kind: false }, { allowed: undefined }]) {
      const pinia = createPinia()
      setActivePinia(pinia)
      get.mockResolvedValue(status([server({ ...bad, connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] } as never)], 'work', provider))
      let controller!: ReturnType<typeof useChatMcpConnections>
      const popup = vi.spyOn(window, 'open').mockReturnValue(null)
      const wrapper = mount(defineComponent({ setup() {
        controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => [], archived: () => false })
        return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
      } }), { global: { plugins: [pinia] } })
      await flushPromises()
      expect(controller.status.value).toBeNull()
      expect(wrapper.find('.chat-mcp-sign-in').exists()).toBe(false)
      expect(popup).not.toHaveBeenCalled()
      expect(post).not.toHaveBeenCalled()
      wrapper.unmount()
      const lastAdd = add.mock.calls.filter(([event]) => event === 'visibilitychange').at(-1)
      expect(remove.mock.calls).toContainEqual(lastAdd)
      popup.mockRestore()
    }
  })

  it('scopes to exact workspace, provider and chat; foreign observations are never healthy', async () => {
    const options = { workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [], archived: () => false }
    get.mockResolvedValue(status([server({ connections: [
      { chat_id: 'chat-b', state: 'connected' }, { chat_id: 'chat-a', state: 'needs_auth' },
    ] })]))
    const controller = observe(options)
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(get).toHaveBeenCalledWith('/api/mcp/status?workspace=work')
    expect(controller.rows.value[0]?.state).toBe('needs_auth')
    expect(controller.problemCount.value).toBe(1)
  })

  it('uses conservative metadata and observation precedence, action winner, and disabled dominance', async () => {
    const actions = [action('waiting', 'a1'), action('failed', 'a2')]
    const options = { workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => actions, archived: () => false }
    get.mockResolvedValue(status([
      server({ scope: 'user', source: 'z', connections: [{ chat_id: 'chat-a', state: 'connected' }] }),
      server({ scope: 'local', source: 'a', enabled: false, connections: [{ chat_id: 'chat-a', state: 'failed', error: 'wrong' }] }),
      server({ name: 'orphan', connections: [] }),
    ]))
    const controller = observe(options)
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(controller.rows.value.map(row => row.name)).toEqual(['linear', 'orphan'])
    expect(controller.rows.value[0]).toMatchObject({ enabled: false, state: 'disabled', action: actions[1], error: '' })
    expect(controller.rows.value[1]).toMatchObject({ state: 'unknown', enabled: true })
    expect(controller.problemCount.value).toBe(0)
  })

  it('clears unresolved and archived contexts without requesting an active-workspace fallback', async () => {
    const options = { workspace: () => '', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [], archived: () => false }
    get.mockResolvedValue(status([server()]))
    const controller = observe(options)
    await nextTick()
    expect(get).not.toHaveBeenCalled()
    expect(controller.rows.value).toEqual([])
  })

  it('ignores late context responses and coalesces refreshes without resetting on action-only changes', async () => {
    setActivePinia(createPinia())
    const calls: Array<{ resolve: (value: McpStatus) => void; reject: (error: Error) => void }> = []
    get.mockImplementation(() => new Promise((resolve, reject) => calls.push({ resolve, reject })))
    const context = reactive({ workspace: 'first', provider: 'claude', chatId: 'chat-a', archived: false })
    let controller!: ReturnType<typeof useChatMcpConnections>
    const actions = reactive<ChatAction[]>([])
    const wrapper = mount(defineComponent({
      setup() {
        controller = useChatMcpConnections({
          workspace: () => context.workspace, provider: () => context.provider,
          chatId: () => context.chatId, archived: () => context.archived, actions: () => actions,
        })
        return () => h('div')
      },
    }))
    expect(calls).toHaveLength(1)
    context.workspace = 'second'
    expect(calls).toHaveLength(2)
    calls[1]!.resolve(status([server({ connections: [{ chat_id: 'chat-a', state: 'connected' }] })], 'second'))
    await vi.waitFor(() => expect(controller.status.value?.workspace).toBe('second'))
    calls[0]!.reject(new Error('late old error'))
    await nextTick()
    expect(controller.status.value?.workspace).toBe('second')
    expect(controller.error.value).toBe('')
    actions.push(action('waiting'))
    await nextTick()
    expect(controller.status.value?.workspace).toBe('second')

    const firstRefresh = controller.refresh()
    const secondRefresh = controller.refresh()
    expect(calls).toHaveLength(3)
    calls[2]!.resolve(status([server()], 'second'))
    await vi.waitFor(() => expect(calls).toHaveLength(4))
    calls[3]!.resolve(status([server()], 'second'))
    await Promise.all([firstRefresh, secondRefresh])
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    wrapper.unmount()
  })

  it('polls once per mounted observer, pauses while hidden, refreshes on visibility and cleans up', async () => {
    vi.useFakeTimers()
    const hidden = vi.spyOn(document, 'hidden', 'get')
    hidden.mockReturnValue(false)
    get.mockResolvedValue(status([]))
    const wrapper = mount(defineComponent({ setup() {
      useChatMcpConnections({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [], archived: () => false })
      return () => h('div')
    } }))
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(get).toHaveBeenCalledTimes(2)
    hidden.mockReturnValue(true)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(get).toHaveBeenCalledTimes(2)
    hidden.mockReturnValue(false)
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(3)
    wrapper.unmount()
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(30_000)
    expect(get).toHaveBeenCalledTimes(3)
    vi.useRealTimers()
  })

  it('ignores a pending inventory response after unmount', async () => {
    let resolve!: (value: McpStatus) => void
    get.mockReturnValue(new Promise(r => { resolve = r }))
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [], archived: () => false })
      return () => h('div')
    } }))
    wrapper.unmount()
    resolve(status([server({ connections: [{ chat_id: 'chat-a', state: 'connected' }] })]))
    await flushPromises()
    expect(controller.status.value).toBeNull()
    expect(controller.rows.value).toEqual([])
  })

  it('derives duplicate metadata, exact-chat errors and actions deterministically', async () => {
    const actions = [action('waiting', 'a1'), { ...action('failed', 'a2'), created_at: 2 }, { ...action('done', 'a3'), created_at: 2 }]
    const options = { workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => actions, archived: () => false }
    const base = [
      server({ scope: 'user', source: 'z', connections: [{ chat_id: 'chat-a', state: 'connected' }] }),
      server({ scope: 'local', source: 'a', connections: [
        { chat_id: 'chat-a', state: 'failed', error: 'zeta' },
        { chat_id: 'chat-a', state: 'failed', error: 'alpha' },
        { chat_id: 'chat-other', state: 'failed', error: 'foreign' },
      ] }),
    ]
    get.mockResolvedValue(status(base))
    const controller = observe(options)
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(controller.rows.value[0]).toMatchObject({ server: base[1], state: 'failed', error: 'alpha', action: actions[2] })
    expect(controller.problemCount.value).toBe(1)
    get.mockResolvedValue(status([...base].reverse()))
    const reversed = observe({ ...options, chatId: () => 'chat-a' })
    await vi.waitFor(() => expect(reversed.loading.value).toBe(false))
    expect(reversed.rows.value).toEqual(controller.rows.value)
  })

  it('does not trust observations from duplicate provider inventories unless all are available', async () => {
    const snapshot = status([server({ connections: [{ chat_id: 'chat-a', state: 'connected' }] })])
    snapshot.providers.push({ ...snapshot.providers[0]!, available: false, servers: [] })
    get.mockResolvedValue(snapshot)
    const actions = [action('waiting', 'a', 'orphan')]
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => actions, archived: () => false })
      return () => h('div')
    } }))
    await flushPromises()
    expect(controller.rows.value).toMatchObject([
      { name: 'linear', state: 'unknown', enabled: true },
      { name: 'orphan', state: 'unknown', enabled: null, action: actions[0] },
    ])
    expect(controller.problemCount.value).toBe(1)
    expect(controller.canStart(controller.rows.value[0]!)).toBe(false)
    wrapper.unmount()
  })

  it.each([
    ['provider', 'resolve'], ['provider', 'reject'], ['chatId', 'resolve'], ['chatId', 'reject'],
  ] as const)('ignores late %s context %s and finally', async (field, outcome) => {
    const calls: Array<{ resolve: (value: McpStatus) => void; reject: (reason: Error) => void }> = []
    get.mockImplementation(() => new Promise((resolve, reject) => calls.push({ resolve, reject })))
    const context = reactive({ provider: 'claude', chatId: 'chat-a' })
    const controller = observe({ workspace: () => 'work', provider: () => context.provider, chatId: () => context.chatId, actions: () => [], archived: () => false })
    context[field] = field === 'provider' ? 'opencode' : 'chat-b'
    expect(calls).toHaveLength(2)
    const newProvider = field === 'provider' ? 'opencode' : 'claude'
    calls[1]!.resolve(status([server({ connections: [{ chat_id: context.chatId, state: 'needs_auth' }] })], 'work', newProvider))
    await flushPromises()
    if (outcome === 'resolve') calls[0]!.resolve(status([server({ connections: [{ chat_id: 'chat-a', state: 'connected' }] })]))
    else calls[0]!.reject(new Error('stale'))
    await flushPromises()
    expect(controller.rows.value[0]?.state).toBe('needs_auth')
    expect(controller.loading.value).toBe(false)
    expect(controller.error.value).toBe('')
  })

  it('uses deterministic tie breaks and counts connected plus failed action once', async () => {
    const actions = [action('waiting', 'a1'), { ...action('failed', 'a2'), created_at: 2, error: 'action failure' }]
    const disabled = server({ name: 'blocked', enabled: false })
    const rows = [
      server({ source: 'same', allowed: true, connections: [{ chat_id: 'chat-a', state: 'connected' }] }),
      server({ source: 'same', allowed: false, connections: [{ chat_id: 'foreign', state: 'failed', error: 'foreign secret' }] }),
      disabled,
    ]
    get.mockResolvedValue(status(rows))
    const controller = observe({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => actions, archived: () => false })
    await flushPromises()
    expect(controller.rows.value).toMatchObject([
      { name: 'blocked', enabled: false, state: 'disabled' },
      { name: 'linear', state: 'connected', server: { allowed: false }, error: 'action failure', action: actions[1] },
    ])
    expect(controller.problemCount.value).toBe(1)
    get.mockResolvedValue(status([...rows].reverse()))
    const reversed = observe({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [...actions].reverse(), archived: () => false })
    await flushPromises()
    expect(reversed.rows.value).toEqual(controller.rows.value)
  })

  it('uses the static failure fallback only when target failure and action lack an error', async () => {
    get.mockResolvedValue(status([server({ connections: [
      { chat_id: 'foreign', state: 'failed', error: 'foreign error' },
      { chat_id: 'chat-a', state: 'failed' },
    ] })]))
    const controller = observe({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [], archived: () => false })
    await flushPromises()
    expect(controller.rows.value[0]).toMatchObject({ state: 'failed', error: 'Connection failed. Check the provider settings.' })
    expect(controller.problemCount.value).toBe(1)
  })

  it('reports rejected reads separately from malformed responses and never calls unknown healthy', async () => {
    get.mockRejectedValue(new Error('offline'))
    const controller = observe({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [action('open')], archived: () => false })
    await flushPromises()
    expect(controller.error.value).toContain('offline')
    expect(controller.rows.value[0]).toMatchObject({ name: 'linear', enabled: null, state: 'unknown' })
    expect(controller.canStart(controller.rows.value[0]!)).toBe(false)
  })

  it.each([
    ['open', false, null, 1], ['waiting', false, null, 1], ['done', false, null, 0],
    ['waiting', true, false, 0],
  ] as const)('counts action-only %s and disabled=%s correctly', async (state, disabled, enabled, count) => {
    const actions = [action(state)]
    get.mockResolvedValue(status(disabled ? [server({ enabled: false })] : []))
    const controller = observe({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => actions, archived: () => false })
    await flushPromises()
    expect(controller.rows.value[0]).toMatchObject({ enabled, action: actions[0] })
    expect(controller.problemCount.value).toBe(count)
    expect(controller.canStart(controller.rows.value[0]!)).toBe(false)
  })

  it.each([null, {}, { workspace: 'work', providers: null }, { workspace: 'work', providers: [{ provider: 'claude', available: true, inventory_state: 'loaded', servers: [{}] }] }])(
    'treats malformed inventory as unavailable: %j', async response => {
      get.mockResolvedValue(response)
      let controller!: ReturnType<typeof useChatMcpConnections>
      const wrapper = mount(defineComponent({ setup() {
        controller = useChatMcpConnections({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [], archived: () => false })
        return () => h('div')
      } }))
      await flushPromises()
      expect(controller.status.value).toBeNull()
      expect(controller.error.value).toContain('unavailable')
      expect(controller.rows.value).toEqual([])
      wrapper.unmount()
    },
  )

  it.each(['claude', 'opencode'] as const)('starts the eligible validated %s no-action observation through the shared owner', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = action('open')
    auth.params.provider = provider
    post.mockImplementation((path: string) => path === '/api/chats/chat-a/actions/mcp-auth'
      ? Promise.resolve({ action: auth })
      : Promise.resolve({ action: { ...auth, state: 'working' } }))
    vi.spyOn(window, 'open').mockReturnValue(null)
    const eligibleServer = server({ kind: 'remote', connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] })
    get.mockResolvedValue(status([eligibleServer], 'work', provider))
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({
      setup() {
        controller = useChatMcpConnections({
          workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a',
          actions: () => [], archived: () => false,
        })
        return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
      },
    }), { global: { plugins: [pinia] } })
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(wrapper.get('.chat-mcp-sign-in').text()).toBe('Sign in')
    await wrapper.get('.chat-mcp-sign-in').trigger('click')
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post).toHaveBeenNthCalledWith(1, '/api/chats/chat-a/actions/mcp-auth', { server: 'linear' })
    expect(post).toHaveBeenNthCalledWith(2, '/api/chats/chat-a/actions/a/start', {})
    wrapper.unmount()
  })

  it.each(['claude', 'opencode'] as const)('shares busy state with real %s inline and sidebar controls', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const current = action('open')
    current.params.provider = provider
    const store = useProjectStore()
    store.chatActions['chat-a'] = [current]
    get.mockResolvedValue(status([server({ connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] })], 'work', provider))
    let resolve!: (value: unknown) => void
    post.mockReturnValue(new Promise(r => { resolve = r }))
    vi.spyOn(window, 'open').mockReturnValue(null)
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({
        workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a',
        actions: () => [], archived: () => false,
      })
      return () => h('div', [
        h(NeedsYouCard, { chatId: 'chat-a', action: current }),
        h(ChatMcpConnections, { chatId: 'chat-a', provider, controller }),
      ])
    } }), { global: { plugins: [pinia] } })
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    const sidebar = wrapper.get('.chat-mcp-sign-in')
    expect(sidebar.attributes('disabled')).toBeUndefined()
    await wrapper.get('.needs-you-card .needs-you-btn--primary').trigger('click')
    expect(sidebar.attributes('disabled')).toBeDefined()
    resolve({ action: { ...current, state: 'working' } })
    await flushPromises()
    expect(sidebar.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it.each(['claude', 'opencode'] as const)('disables the second real %s action card until the first start finishes', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const current = action('open')
    current.params.provider = provider
    useProjectStore().chatActions['chat-a'] = [current]
    let resolve!: (value: unknown) => void
    post.mockReturnValue(new Promise(r => { resolve = r }))
    const popup = vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = mount(defineComponent({ setup() {
      return () => h('div', [
        h(NeedsYouCard, { chatId: 'chat-a', action: current }),
        h(NeedsYouCard, { chatId: 'chat-a', action: current }),
      ])
    } }), { global: { plugins: [pinia] } })
    const buttons = wrapper.findAll('.needs-you-card .needs-you-btn--primary')
    await buttons[0]!.trigger('click')
    expect(buttons[1]!.attributes('disabled')).toBeDefined()
    await buttons[1]!.trigger('click')
    expect(popup).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledTimes(1)
    resolve({ action: { ...current, state: 'working' } })
    await flushPromises()
    expect(buttons[1]!.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it.each(['claude', 'opencode'] as const)('%s real sidebar callback refuses safely, accepts, waits for verification and continues', async provider => {
    vi.spyOn(loopback, 'isLoopbackPage').mockReturnValue(false)
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    const waiting = { ...action('waiting'), params: { provider, server: 'linear', url: 'https://auth.example.invalid/' } }
    store.chatActions['chat-a'] = [waiting]
    get.mockRejectedValue(new Error('inventory offline'))
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => store.chatActions['chat-a'] ?? [], archived: () => false })
      return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
    } }), { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.get('.chat-mcp-message').text()).toContain('offline')
    expect(wrapper.get('.needs-you-card a').attributes('href')).toBe('https://auth.example.invalid/')
    const redirect = 'http://localhost:1234/callback?code=secret&state=s'
    post.mockRejectedValueOnce(Object.assign(new Error('state_mismatch'), { status: 400, payload: { error: 'state_mismatch', action: null } }))
    await wrapper.get('input[type=url]').setValue(redirect)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role=alert]').text()).toContain('different sign-in')
    expect((wrapper.get('input[type=url]').element as HTMLInputElement).value).toBe('')
    post.mockResolvedValueOnce({ action: { ...waiting, params: { ...waiting.params, callback_used: '1' } } })
    await wrapper.get('input[type=url]').setValue(redirect)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(post).toHaveBeenLastCalledWith('/api/chats/chat-a/actions/a/callback', { url: redirect })
    expect(store.chatActions['chat-a']?.[0]?.state).toBe('waiting')
    store.chatActions['chat-a'] = [{ ...waiting, state: 'done' }]
    await nextTick()
    expect(wrapper.get('.needs-you-card').text()).toContain('Continue')
    post.mockResolvedValueOnce({ actions: [], delivery: 'sent' })
    const continueButton = wrapper.findAll('.needs-you-card button').find(button => button.text().includes('Continue'))!
    await continueButton.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenLastCalledWith('/api/chats/chat-a/actions/a/continue', {})
    expect(wrapper.find('.needs-you-card').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each(['claude', 'opencode'] as const)('%s real sidebar dismiss and turn-off refusal preserve action semantics', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    const current = { ...action('open'), params: { provider, server: 'linear' } }
    store.chatActions['chat-a'] = [current]
    get.mockRejectedValue(new Error('offline'))
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => store.chatActions['chat-a'] ?? [], archived: () => false })
      return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
    } }), { attachTo: document.body, global: { plugins: [pinia] } })
    await flushPromises()
    post.mockRejectedValueOnce(Object.assign(new Error('config_has_comments'), { status: 400, payload: { error: 'config_has_comments' } }))
    await wrapper.get('.needs-you-turn-off').trigger('click')
    await flushPromises()
    expect(wrapper.get('.needs-you-card [role=alert]').text()).not.toBe('')
    const dismiss = wrapper.findAll('.needs-you-card button').find(button => button.text() === 'Not now')!
    ;(dismiss.element as HTMLElement).focus()
    post.mockResolvedValueOnce({ actions: [] })
    await dismiss.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenLastCalledWith('/api/chats/chat-a/actions/a/dismiss', {})
    expect(wrapper.find('.needs-you-card').exists()).toBe(false)
    expect(document.activeElement).toBe(wrapper.get('.chat-mcp-heading').element)
    wrapper.unmount()
  })

  it.each(['claude', 'opencode'] as const)('%s inline dismissal cancels a pending sidebar start without resurrecting or navigating', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    const current = { ...action('open'), params: { provider, server: 'linear' } }
    store.chatActions['chat-a'] = [current]
    get.mockRejectedValue(new Error('offline'))
    let finishStart!: (value: unknown) => void
    post.mockImplementation((path: string) => path.endsWith('/start')
      ? new Promise(resolve => { finishStart = resolve })
      : Promise.resolve({ actions: [] }))
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    const popup = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => store.chatActions['chat-a'] ?? [], archived: () => false })
      return () => h('div', [
        h(NeedsYouCard, { chatId: 'chat-a', action: store.chatActions['chat-a']?.[0] ?? current }),
        h(ChatMcpConnections, { chatId: 'chat-a', provider, controller }),
      ])
    } }), { global: { plugins: [pinia] } })
    await flushPromises()
    await wrapper.get('.chat-mcp-connections .needs-you-btn--primary').trigger('click')
    expect(popup).toHaveBeenCalledOnce()
    const inline = wrapper.findAll('.needs-you-card')[0]!
    await inline.findAll('button').find(button => button.text() === 'Not now')!.trigger('click')
    await flushPromises()
    expect(tab.close).toHaveBeenCalled()
    finishStart({ action: { ...current, state: 'waiting', params: { ...current.params, url: 'https://auth.example.invalid/' } } })
    await flushPromises()
    expect(tab.location.href).toBe('')
    expect(store.chatActions['chat-a'] ?? []).toEqual([])
    expect(wrapper.find('.chat-mcp-connections .needs-you-card').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each(['claude', 'opencode'] as const)('%s retains a safe waiting link when the gesture popup is blocked', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    const current = { ...action('open'), params: { provider, server: 'linear' } }
    store.chatActions['chat-a'] = [current]
    get.mockRejectedValue(new Error('offline'))
    post.mockResolvedValue({ action: { ...current, state: 'waiting', params: { ...current.params, url: 'https://auth.example.invalid/' } } })
    const popup = vi.spyOn(window, 'open').mockReturnValue(null)
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => store.chatActions['chat-a'] ?? [], archived: () => false })
      return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
    } }), { global: { plugins: [pinia] } })
    await flushPromises()
    await wrapper.get('.needs-you-btn--primary').trigger('click')
    await flushPromises()
    expect(popup).toHaveBeenCalledOnce()
    expect(wrapper.get('.needs-you-card a').attributes('href')).toBe('https://auth.example.invalid/')
    expect(wrapper.find('.needs-you-steps .is-done').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each(['claude', 'opencode'] as const)('%s real sidebar owns its placeholder until the exact delayed action supplies a URL', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    const current = { ...action('open'), params: { provider, server: 'linear' } }
    store.chatActions['chat-a'] = [current]
    get.mockRejectedValue(new Error('offline'))
    post.mockResolvedValue({ action: { ...current, state: 'working' } })
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    const popup = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => store.chatActions['chat-a'] ?? [], archived: () => false })
      return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
    } }), { global: { plugins: [pinia] } })
    await flushPromises()
    await wrapper.get('.needs-you-btn--primary').trigger('click')
    await flushPromises()
    expect(tab.location.href).toBe('')
    expect(tab.close).not.toHaveBeenCalled()
    store.chatActions['chat-a'] = [{ ...current, state: 'waiting', params: { ...current.params, url: 'https://auth.example.invalid/' } }]
    await nextTick()
    expect(tab.location.href).toBe('https://auth.example.invalid/')
    expect(tab.opener).toBeNull()
    expect(popup).toHaveBeenCalledOnce()
    expect(wrapper.get('.needs-you-steps .is-done').text()).toBe('Sign-in page opened')
    wrapper.unmount()
    expect(tab.close).not.toHaveBeenCalled()
  })

  it.each(['claude', 'opencode'] as const)('%s real failed card Try again navigates the original popup on a later waiting URL', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    const failed = { ...action('failed'), params: { provider, server: 'linear' } }
    store.chatActions['chat-a'] = [failed]
    get.mockRejectedValue(new Error('offline'))
    post.mockResolvedValueOnce({ action: { ...failed, state: 'working' } })
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    const popup = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => store.chatActions['chat-a'] ?? [], archived: () => false })
      return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
    } }), { global: { plugins: [pinia] } })
    await flushPromises()
    const retry = wrapper.get('.needs-you-card .needs-you-btn--primary')
    expect(retry.text()).toBe('Try again')
    await retry.trigger('click')
    await flushPromises()
    expect(popup).toHaveBeenCalledOnce()
    expect(post).toHaveBeenCalledTimes(1)
    expect(tab.close).not.toHaveBeenCalled()
    expect(tab.location.href).toBe('')
    post.mockResolvedValueOnce({ actions: [{ ...failed, state: 'waiting', params: { ...failed.params, url: 'https://auth.example.invalid/' } }] })
    await store.dismissChatAction('chat-a', failed.id)
    await nextTick()
    expect(tab.location.href).toBe('https://auth.example.invalid/')
    expect(tab.opener).toBeNull()
    expect(popup).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  for (const provider of ['claude', 'opencode'] as const) {
    it.each([400, 409] as const)(`${provider} real card refusal %s keeps a safe explicit link without navigating`, async statusCode => {
      const pinia = createPinia()
      setActivePinia(pinia)
      const store = useProjectStore()
      const current = { ...action('open'), params: { provider, server: 'linear' } }
      const waiting = { ...current, state: 'waiting' as const, params: { ...current.params, url: 'https://auth.example.invalid/' } }
      store.chatActions['chat-a'] = [current]
      get.mockRejectedValue(new Error('offline'))
      post.mockRejectedValueOnce(Object.assign(new Error('refused'), {
        status: statusCode,
        payload: statusCode === 409 ? { error: 'chat_action_conflict', actions: [waiting] } : { error: 'invalid_state', action: waiting },
      }))
      const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
      const popup = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
      let controller!: ReturnType<typeof useChatMcpConnections>
      const wrapper = mount(defineComponent({ setup() {
        controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => store.chatActions['chat-a'] ?? [], archived: () => false })
        return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
      } }), { global: { plugins: [pinia] } })
      await flushPromises()
      await wrapper.get('.needs-you-card .needs-you-btn--primary').trigger('click')
      await flushPromises()
      expect(tab.location.href).toBe('')
      expect(tab.close).toHaveBeenCalledOnce()
      expect(popup).toHaveBeenCalledOnce()
      expect(post).toHaveBeenCalledTimes(1)
      expect(wrapper.get('.needs-you-card [role=status]').text()).toContain('card changed')
      const link = wrapper.get('.needs-you-card a.needs-you-btn')
      expect(link.attributes('href')).toBe('https://auth.example.invalid/')
      expect(link.attributes('target')).toBe('_blank')
      expect(link.attributes('rel')).toBe('noopener noreferrer')
      wrapper.unmount()
    })
  }

  it.each(['claude', 'opencode'] as const)('%s starts existing failed actions even when inventory is unavailable', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    const current = { ...action('failed'), params: { provider, server: 'linear' } }
    store.chatActions['chat-a'] = [current]
    get.mockRejectedValue(new Error('offline'))
    post.mockResolvedValue({ action: { ...current, state: 'working' } })
    const popup = vi.spyOn(window, 'open').mockReturnValue(null)
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => store.chatActions['chat-a'] ?? [], archived: () => false })
      return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
    } }), { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.get('.chat-mcp-state').text()).toBe('Configuration unknown')
    await wrapper.get('.needs-you-btn--primary').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/chats/chat-a/actions/a/start', {})
    expect(post).not.toHaveBeenCalledWith('/api/chats/chat-a/actions/mcp-auth', expect.anything())
    expect(popup).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it.each(['claude', 'opencode'] as const)('%s real sidebar turn-off removes the action and done can be dismissed', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    const current = { ...action('open'), params: { provider, server: 'linear' } }
    store.chatActions['chat-a'] = [current]
    get.mockRejectedValue(new Error('offline'))
    post.mockResolvedValue({ actions: [], reload: {} })
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => store.chatActions['chat-a'] ?? [], archived: () => false })
      return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
    } }), { global: { plugins: [pinia] } })
    await flushPromises()
    await wrapper.get('.needs-you-turn-off').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/chats/chat-a/actions/a/turn-off', {})
    expect(wrapper.find('.needs-you-card').exists()).toBe(false)
    store.chatActions['chat-a'] = [{ ...current, state: 'done' }]
    await nextTick()
    const done = wrapper.findAll('.needs-you-card button').find(button => button.text() === 'Done')!
    await done.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/chats/chat-a/actions/a/dismiss', {})
    expect(wrapper.find('.needs-you-card').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each(['claude', 'opencode'] as const)('%s permits allowed=null for an exact native needs-auth observation', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const current = { ...action('open'), params: { provider, server: 'linear' } }
    post.mockImplementation((path: string) => Promise.resolve({ action: { ...current, state: path.endsWith('/start') ? 'working' : 'open' } }))
    get.mockResolvedValue(status([server({ allowed: null, connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] })], 'work', provider))
    const popup = vi.spyOn(window, 'open').mockReturnValue(null)
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => [], archived: () => false })
      return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
    } }), { global: { plugins: [pinia] } })
    await flushPromises()
    await wrapper.get('.chat-mcp-sign-in').trigger('click')
    await flushPromises()
    expect(popup).toHaveBeenCalledOnce()
    expect(post).toHaveBeenCalledWith('/api/chats/chat-a/actions/mcp-auth', { server: 'linear' })
    wrapper.unmount()
  })

  for (const provider of ['claude', 'opencode'] as const) {
  it.each([
    ['unavailable provider', { available: false }, true, 'needs_auth', 'remote', 'project', 'linear'],
    ['unknown inventory', { inventory_state: 'unknown' }, true, 'needs_auth', 'remote', 'project', 'linear'],
    ['wrong workspace', {}, true, 'needs_auth', 'remote', 'project', 'linear', 'other'],
    ['explicitly disabled', {}, false, 'needs_auth', 'remote', 'project', 'linear'],
    ['blocked tool', {}, true, 'needs_auth', 'remote', 'project', 'linear', 'work', false],
    ['claude.ai scope', {}, true, 'needs_auth', 'remote', 'claudeai', 'linear'],
    ['invalid raw name', {}, true, 'needs_auth', 'remote', 'project', 'bad;touch'],
    ['stdio kind', {}, true, 'needs_auth', 'stdio', 'project', 'linear'],
    ['local kind', {}, true, 'needs_auth', 'local', 'project', 'linear'],
    ['missing kind', {}, true, 'needs_auth', undefined, 'project', 'linear'],
    ['connected observation', {}, true, 'connected', 'remote', 'project', 'linear'],
    ['unknown observation', {}, true, 'unknown', 'remote', 'project', 'linear'],
    ['failed observation', {}, true, 'failed', 'remote', 'project', 'linear'],
    ['disabled observation', {}, true, 'disabled', 'remote', 'project', 'linear'],
    ['foreign-chat-only observation', {}, true, 'needs_auth', 'remote', 'project', 'linear', 'work', true, 'other-chat'],
  ])(`${provider} does not offer a new start for %s`, async (_label, inventory, enabled, observation, kind, scope, name, workspace = 'work', allowed = true, observationChat = 'chat-a') => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const rowServer = server({
      name, kind, scope, enabled, allowed,
      connections: [{ chat_id: observationChat, state: observation as never }],
    })
    const snapshot = status([rowServer], workspace as string, provider)
    Object.assign(snapshot.providers[0]!, inventory)
    get.mockResolvedValue(snapshot)
    let controller!: ReturnType<typeof useChatMcpConnections>
    const popup = vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = mount(defineComponent({
      setup() {
        controller = useChatMcpConnections({
          workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a',
          actions: () => [], archived: () => false,
        })
        return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
      },
    }), { global: { plugins: [pinia] } })
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(wrapper.find('.chat-mcp-sign-in').exists()).toBe(false)
    expect(post).not.toHaveBeenCalled()
    expect(popup).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  }

  it('rechecks current generation, inventory errors and actions synchronously at the click seam', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const actions = reactive<ChatAction[]>([])
    const context = reactive({ workspace: 'work', archived: false })
    get.mockResolvedValue(status([server({ connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] })]))
    const controller = observe({ workspace: () => context.workspace, provider: () => 'claude', chatId: () => 'chat-a', actions: () => actions, archived: () => context.archived })
    await flushPromises()
    const row = controller.rows.value[0]!
    expect(controller.canStart(row)).toBe(true)
    actions.push(action('open'))
    expect(controller.canStart(row)).toBe(false)
    actions.splice(0)
    controller.error.value = 'offline'
    expect(controller.canStart(row)).toBe(false)
    controller.error.value = ''
    context.archived = true
    expect(controller.canStart(row)).toBe(false)
    context.archived = false
    context.workspace = 'new-workspace'
    expect(controller.canStart(row)).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('does not offer sign-in when the requested provider inventory is absent', async () => {
    get.mockResolvedValue(status([server({ connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] })], 'work', 'opencode'))
    const controller = observe({ workspace: () => 'work', provider: () => 'claude', chatId: () => 'chat-a', actions: () => [action('waiting')], archived: () => false })
    await flushPromises()
    expect(controller.rows.value[0]).toMatchObject({ state: 'unknown', enabled: null })
    expect(controller.canStart(controller.rows.value[0]!)).toBe(false)
  })

  it.each(['claude', 'opencode'] as const)('%s absent inventory and action-only rows never offer a new start', async provider => {
    for (const actionOnly of [false, true]) {
      const pinia = createPinia()
      setActivePinia(pinia)
      const supplied = actionOnly ? [{ ...action('waiting'), params: { provider, server: 'linear' } }] : []
      get.mockResolvedValue(status([], 'work', provider === 'claude' ? 'opencode' : 'claude'))
      const popup = vi.spyOn(window, 'open').mockReturnValue(null)
      let controller!: ReturnType<typeof useChatMcpConnections>
      const wrapper = mount(defineComponent({ setup() {
        controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => supplied, archived: () => false })
        return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
      } }), { global: { plugins: [pinia] } })
      await flushPromises()
      expect(wrapper.find('.chat-mcp-sign-in').exists()).toBe(false)
      expect(popup).not.toHaveBeenCalled()
      expect(post).not.toHaveBeenCalled()
      wrapper.unmount()
      popup.mockRestore()
    }
  })

  it.each(['claude', 'opencode'] as const)('%s loaded matching empty inventory excludes action-only start', async provider => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const supplied = [{ ...action('waiting'), params: { provider, server: 'linear' } }]
    get.mockResolvedValue(status([], 'work', provider))
    const popup = vi.spyOn(window, 'open').mockReturnValue(null)
    let controller!: ReturnType<typeof useChatMcpConnections>
    const wrapper = mount(defineComponent({ setup() {
      controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => supplied, archived: () => false })
      return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
    } }), { global: { plugins: [pinia] } })
    await flushPromises()
    expect(controller.rows.value[0]).toMatchObject({ name: 'linear', server: null })
    expect(wrapper.find('.chat-mcp-sign-in').exists()).toBe(false)
    expect(popup).not.toHaveBeenCalled()
    expect(post).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  for (const provider of ['claude', 'opencode'] as const) {
    it.each(['connected', 'disabled', 'blocked', 'removed'] as const)('%s refreshed inventory rejects a stale mounted button for ' + provider, async change => {
      const pinia = createPinia()
      setActivePinia(pinia)
      const eligible = server({ connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] })
      get.mockResolvedValue(status([eligible], 'work', provider))
      post.mockResolvedValue({})
      const popup = vi.spyOn(window, 'open').mockReturnValue(null)
      let controller!: ReturnType<typeof useChatMcpConnections>
      const wrapper = mount(defineComponent({ setup() {
        controller = useChatMcpConnections({ workspace: () => 'work', provider: () => provider, chatId: () => 'chat-a', actions: () => [], archived: () => false })
        return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
      } }), { attachTo: document.body, global: { plugins: [pinia] } })
      try {
        await flushPromises()
        const button = wrapper.get('.chat-mcp-sign-in').element as HTMLButtonElement
        const next = change === 'connected' ? { ...eligible, connections: [{ chat_id: 'chat-a', state: 'connected' as const }] }
          : change === 'disabled' ? { ...eligible, enabled: false }
            : { ...eligible, allowed: false }
        controller.status.value = status(change === 'removed' ? [] : [next], 'work', provider)
        expect(button.isConnected).toBe(true)
        button.click()
        expect(popup).not.toHaveBeenCalled()
        expect(post).not.toHaveBeenCalled()
      } finally {
        await flushPromises()
        wrapper.unmount()
      }
    })

    it.each(['context', 'generation', 'eligibility', 'error'] as const)('%s stale mounted button cannot start for ' + provider, async reason => {
      const pinia = createPinia()
      setActivePinia(pinia)
      const context = reactive({ workspace: 'work', archived: false })
      const actions = reactive<ChatAction[]>([])
      get.mockResolvedValue(status([server({ connections: [{ chat_id: 'chat-a', state: 'needs_auth' }] })], 'work', provider))
      const popup = vi.spyOn(window, 'open').mockReturnValue(null)
      let controller!: ReturnType<typeof useChatMcpConnections>
      const wrapper = mount(defineComponent({ setup() {
        controller = useChatMcpConnections({ workspace: () => context.workspace, provider: () => provider, chatId: () => 'chat-a', actions: () => actions, archived: () => context.archived })
        return () => h(ChatMcpConnections, { chatId: 'chat-a', provider, controller })
      } }), { global: { plugins: [pinia] } })
      await flushPromises()
      const button = wrapper.get('.chat-mcp-sign-in').element as HTMLButtonElement
      if (reason === 'context') context.archived = true
      if (reason === 'generation') context.workspace = 'other'
      if (reason === 'eligibility') actions.push({ ...action('open'), params: { provider, server: 'linear' } })
      if (reason === 'error') controller.error.value = 'offline'
      button.click()
      expect(popup).not.toHaveBeenCalled()
      expect(post).not.toHaveBeenCalled()
      wrapper.unmount()
    })
  }

  it.each([
    ['', 'claude', 'chat-a', false], ['work', 'codex', 'chat-a', false],
    ['work', 'claude', '', false], ['work', 'claude', 'chat-a', true],
  ])('invalid context (%s, %s, %s, archived=%s) never requests or exposes a row', async (workspace, provider, chatId, archived) => {
    const controller = observe({ workspace: () => workspace, provider: () => provider, chatId: () => chatId, archived: () => archived, actions: () => [action('open')] })
    await nextTick()
    expect(get).not.toHaveBeenCalled()
    expect(controller.rows.value).toEqual([])
  })

  it('ChatPanel observes the owning workspace with details closed and stops for archived or unresolved chats', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    const project: ProjectInfo = {
      project_id: 'owner-project', name: 'Owner', workspace: 'owning-workspace',
      context: '', created_at: '', order: 0, vault_folder: '',
    }
    const chat: ChatInfo = {
      chat_id: 'chat-owned', project_id: project.project_id, title: 'Owned chat',
      model: 'sonnet', provider: 'opencode', mode: 'normal', session_id: '',
      created_at: '', archived: false,
    }
    store.projects = [project]
    store.activeWorkspace = 'different-global-workspace'
    store.chats = [chat]
    store.activeChatId = chat.chat_id
    store.messages = { [chat.chat_id]: [] }
    store.bootstrapped = true
    get.mockImplementation((path: string) => path === '/api/mcp/status?workspace=owning-workspace'
      ? Promise.resolve(status([], 'owning-workspace', 'opencode'))
      : Promise.resolve({}))
    const commentPopoverStub = defineComponent({
      setup(_, { expose }) {
        expose({ clearPendingClose: vi.fn(), close: vi.fn() })
        return () => h('div')
      },
    })
    const panelStubs = { RouterLink: true, ChatCommentPopover: commentPopoverStub }
    const wrapper = shallowMount(ChatPanel, {
      props: { chatId: chat.chat_id },
      global: { plugins: [pinia], stubs: panelStubs },
    })
    await flushPromises()
    expect(get).toHaveBeenCalledWith('/api/mcp/status?workspace=owning-workspace')
    expect(wrapper.find('.chat-rail').exists()).toBe(false)
    wrapper.unmount()

    get.mockClear()
    const archived = { ...chat, archived: true }
    store.chats = [archived]
    const archivedPanel = shallowMount(ChatPanel, {
      props: { chatId: archived.chat_id },
      global: { plugins: [pinia], stubs: panelStubs },
    })
    await flushPromises()
    expect(get).not.toHaveBeenCalledWith(expect.stringContaining('/api/mcp/status'))
    archivedPanel.unmount()

    get.mockClear()
    store.chats = [chat]
    store.projects = []
    const unresolvedPanel = shallowMount(ChatPanel, {
      props: { chatId: chat.chat_id },
      global: { plugins: [pinia], stubs: panelStubs },
    })
    await flushPromises()
    expect(get).not.toHaveBeenCalledWith(expect.stringContaining('/api/mcp/status'))
    unresolvedPanel.unmount()
  })

  it('ChatPanel renders the real MCP section alongside historical tool usage', async () => {
    localStorage.removeItem('ciao:chat-details-closed')
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    store.projects = [{ project_id: 'owner', name: 'Owner', workspace: 'work', context: '', created_at: '', order: 0, vault_folder: '' }]
    store.chats = [{ chat_id: 'history-chat', project_id: 'owner', title: 'History', model: 'sonnet', provider: 'opencode', mode: 'normal', session_id: '', created_at: '', archived: false }]
    store.activeChatId = 'history-chat'
    store.bootstrapped = true
    store.messages = { 'history-chat': [
      { i: 0, role: 'user', content: 'Use the loop skill', sent_at: '2026-08-01T00:00:00Z', turn_index: 0 },
      { i: 1, role: 'system', content: '⚙️ skill {"id": "loop-engineering"}\n🧩 Skill pr-review', tool_name: '_activity' },
      { i: 2, role: 'assistant', content: 'Finished.', timestamp: '2026-08-01T00:00:10Z' },
    ] as never }
    get.mockImplementation((path: string) => path.startsWith('/api/mcp/status')
      ? Promise.resolve(status([server({ connections: [{ chat_id: 'history-chat', state: 'connected' }] })], 'work', 'opencode'))
      : Promise.resolve(path === '/api/schedules' ? [] : {}))
    const resize: Array<() => void> = []
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe() { resize.push(() => this.callback([{ contentRect: { width: 1200 } } as ResizeObserverEntry], this as unknown as ResizeObserver)) }
      disconnect() {}
      unobserve() {}
    })
    const commentPopoverStub = defineComponent({ setup(_, { expose }) { expose({ clearPendingClose: vi.fn(), close: vi.fn() }); return () => h('div') } })
    const wrapper = shallowMount(ChatPanel, {
      props: { chatId: 'history-chat' },
      global: { plugins: [pinia], stubs: { RouterLink: true, ChatCommentPopover: commentPopoverStub, ChatMcpConnections: false } },
    })
    resize.forEach(notify => notify())
    await flushPromises()
    expect(wrapper.get('.chat-mcp-connections .chat-mcp-name').text()).toBe('linear')
    expect(wrapper.get('.chat-mcp-state').text()).toBe('Connected')
    expect(wrapper.get('.chat-rail-tools').text()).toContain('loop-engineering')
    expect(wrapper.get('.chat-rail-tools').text()).toContain('pr-review')
    wrapper.unmount()
  })
})
