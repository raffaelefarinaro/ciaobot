// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { useMcpAuthStart } from '../useMcpAuthStart'
import type { ChatAction } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'

const get = vi.hoisted(() => vi.fn())
const post = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({ api: { get, post, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() } }))

const openAction: ChatAction = { id: 'mcp-1', kind: 'mcp_auth', origin: 'engine', state: 'open', params: { provider: 'opencode', server: 'linear' }, error: '', created_at: 1 }
const pending = () => {
  let resolve!: (value: unknown) => void
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}

describe('useMcpAuthStart shared owner', () => {
  afterEach(() => { vi.restoreAllMocks(); get.mockReset(); post.mockReset() })
  it.each(['claude', 'opencode'] as const)('%s failed retry navigates its original gesture popup after accepted waiting', async provider => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    const failed = { ...openAction, state: 'failed' as const, params: { provider, server: 'linear' } }
    store.chatActions['chat-a'] = [failed]
    post.mockResolvedValue({ action: { ...failed, state: 'waiting', params: { ...failed.params, url: 'https://auth.example.invalid/' } } })
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => provider, server: () => 'linear', onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    await controller.start(failed.id)
    expect(tab.close).not.toHaveBeenCalled()
    expect(tab.location.href).toBe('https://auth.example.invalid/')
    root.unmount()
  })

  it.each(['failed', 'done', 'working'] as const)('real store keeps newer %s over a discarded late waiting reply', async state => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    const response = pending()
    post.mockReturnValueOnce(response.promise)
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', action: () => openAction, onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    const started = controller.start(openAction.id)
    const updated = { ...openAction, state }
    post.mockResolvedValueOnce({ actions: [updated] })
    await store.dismissChatAction('chat-a', openAction.id)
    response.resolve({ action: { ...openAction, state: 'waiting', params: { ...openAction.params, url: 'https://old.example.invalid/' } } })
    await started
    expect(store.chatActions['chat-a']?.[0]?.state).toBe(state)
    expect(tab.location.href).toBe('')
    if (state === 'working') {
      expect(tab.close).not.toHaveBeenCalled()
      post.mockResolvedValueOnce({ actions: [{ ...updated, state: 'waiting', params: { ...updated.params, url: 'https://new.example.invalid/' } }] })
      await store.dismissChatAction('chat-a', openAction.id)
      expect(tab.location.href).toBe('https://new.example.invalid/')
    } else expect(tab.close).toHaveBeenCalled()
    root.unmount()
  })

  it.each([400, 409] as const)('same-ID waiting refusal %s adopts the safe link without navigating', async status => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    const waiting = { ...openAction, state: 'waiting' as const, params: { ...openAction.params, url: 'https://auth.example.invalid/' } }
    post.mockRejectedValueOnce(Object.assign(new Error('refused'), {
      status, payload: status === 409 ? { error: 'chat_action_conflict', actions: [waiting] } : { error: 'invalid_state', action: waiting },
    }))
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    const notice = vi.fn()
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', onNotice: notice, onError: vi.fn() })
      return () => h('div')
    } }))
    await controller.start(openAction.id)
    expect(store.chatActions['chat-a']?.[0]?.params.url).toBe('https://auth.example.invalid/')
    expect(tab.location.href).toBe('')
    expect(tab.close).toHaveBeenCalled()
    expect(notice).toHaveBeenCalledWith(expect.stringContaining('changed'))
    expect(post).toHaveBeenCalledTimes(1)
    root.unmount()
  })
  it.each(['failed', 'done'] as const)('newer store %s beats stale props and a late waiting response', async state => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    const response = pending()
    vi.spyOn(store, 'startChatAction').mockReturnValue(response.promise as Promise<ChatAction | null>)
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', action: () => openAction, onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    const started = controller.start(openAction.id)
    store.chatActions['chat-a'] = [{ ...openAction, state }]
    expect(tab.close).toHaveBeenCalled()
    response.resolve({ ...openAction, state: 'waiting', params: { ...openAction.params, url: 'https://auth.example.invalid/' } })
    await started
    expect(tab.location.href).toBe('')
    expect(controller.opened.value).toBe(false)
    root.unmount()
  })
  it('reserves synchronously so a competing caller opens no second popup', async () => {
    setActivePinia(createPinia())
    const request = pending()
    post.mockReturnValue(request.promise)
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    const popup = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let first!: ReturnType<typeof useMcpAuthStart>, second!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({
      setup() {
        first = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', onNotice: vi.fn(), onError: vi.fn() })
        second = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', onNotice: vi.fn(), onError: vi.fn() })
        return () => h('div')
      },
    }))
    const one = first.start()
    expect(second.busy.value).toBe(true)
    const two = second.start()
    expect(popup).toHaveBeenCalledTimes(1)
    request.resolve({ action: openAction })
    await Promise.all([one, two])
    expect(second.busy.value).toBe(false)
    root.unmount()
  })

  it('closes the unused placeholder when its owner is disposed during action creation', async () => {
    setActivePinia(createPinia())
    const request = pending()
    post.mockReturnValue(request.promise)
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    const started = controller.start()
    root.unmount()
    request.resolve({ action: openAction })
    await started
    expect(tab.close).toHaveBeenCalled()
    expect(post).not.toHaveBeenCalledWith('/api/chats/chat-a/actions/mcp-1/start', expect.anything())
  })

  it('preserves opened across same-context action updates and resets on a real context change', async () => {
    setActivePinia(createPinia())
    const waiting: ChatAction = {
      ...openAction, state: 'waiting',
      params: { ...openAction.params, url: 'https://auth.example.invalid/authorize' },
    }
    const current = ref<ChatAction>(openAction)
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    post.mockResolvedValue({ action: waiting })
    let controller!: ReturnType<typeof useMcpAuthStart>
    const context = ref('chat-a')
    const root = mount(defineComponent({
      setup() {
        controller = useMcpAuthStart({
          chatId: () => context.value, provider: () => 'opencode', server: () => 'linear',
          action: () => current.value, onNotice: vi.fn(), onError: vi.fn(),
        })
        return () => h('div')
      },
    }))
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    await controller.start(openAction.id)
    expect(controller.opened.value).toBe(true)
    expect(tab.location.href).toBe('https://auth.example.invalid/authorize')
    current.value = { ...waiting, state: 'working', params: { provider: 'opencode', server: 'linear' } }
    await Promise.resolve()
    expect(controller.opened.value).toBe(true)
    context.value = 'chat-b'
    expect(controller.opened.value).toBe(false)
    root.unmount()
  })

  it('never creates an action for an explicitly stale ID', async () => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    const notice = vi.fn()
    const popup = vi.spyOn(window, 'open')
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', onNotice: notice, onError: vi.fn() })
      return () => h('div')
    } }))
    store.chatActions['chat-a'] = []
    await controller.start('dismissed')
    expect(post).not.toHaveBeenCalled()
    expect(popup).not.toHaveBeenCalled()
    expect(notice).toHaveBeenCalledWith(expect.stringContaining('changed'))
    root.unmount()
  })

  it.each(['reject', 'resolve'])('keeps a new context independent when an old request later %ss', async result => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    const context = ref('chat-a')
    let reject!: (error: Error) => void
    let resolve!: (value: ChatAction | null) => void
    const old = new Promise<ChatAction | null>((yes, no) => { resolve = yes; reject = no })
    const newer = pending()
    vi.spyOn(store, 'requestMcpAuth').mockImplementation(chat => chat === 'chat-a' ? old : newer.promise as Promise<ChatAction | null>)
    const tabs = [0, 1].map(() => ({ closed: false, opener: {}, location: { href: '' }, close: vi.fn() }))
    vi.spyOn(window, 'open').mockReturnValueOnce(tabs[0] as unknown as Window).mockReturnValueOnce(tabs[1] as unknown as Window)
    const error = vi.fn()
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => context.value, provider: () => 'opencode', server: () => 'linear', onNotice: vi.fn(), onError: error })
      return () => h('div')
    } }))
    const first = controller.start()
    context.value = 'chat-b'
    expect(controller.busy.value).toBe(false)
    const second = controller.start()
    if (result === 'reject') reject(new Error('old failed'))
    else resolve(openAction)
    await first
    expect(tabs[1]!.close).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalledWith('The sign-in could not be started. Try again.')
    expect(controller.busy.value).toBe(true)
    newer.resolve(null)
    await second
    root.unmount()
  })

  it('does not navigate a late waiting response after another surface removes the action', async () => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    const response = pending()
    vi.spyOn(store, 'startChatAction').mockReturnValue(response.promise as Promise<ChatAction | null>)
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    const started = controller.start(openAction.id)
    store.chatActions['chat-a'] = []
    expect(tab.close).toHaveBeenCalled()
    response.resolve({ ...openAction, state: 'waiting', params: { ...openAction.params, url: 'https://auth.example.invalid/' } })
    await started
    expect(tab.location.href).toBe('')
    expect(controller.opened.value).toBe(false)
    root.unmount()
  })

  it.each(['claude', 'opencode'] as const)('%s closes on disposal after start and never navigates the late answer', async provider => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    const current = { ...openAction, params: { ...openAction.params, provider } }
    store.chatActions['chat-a'] = [current]
    const response = pending()
    vi.spyOn(store, 'startChatAction').mockReturnValue(response.promise as Promise<ChatAction | null>)
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    const popup = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => provider, server: () => 'linear', onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    const started = controller.start(current.id)
    root.unmount()
    expect(tab.close).toHaveBeenCalledOnce()
    response.resolve({ ...current, state: 'waiting', params: { ...current.params, url: 'https://auth.example.invalid/' } })
    await started
    expect(tab.location.href).toBe('')
    expect(popup).toHaveBeenCalledOnce()
  })

  it('ignores another action ID and rejects unsafe delayed URLs', async () => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    post.mockResolvedValue({ action: { ...openAction, state: 'working' } })
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    await controller.start(openAction.id)
    store.chatActions['chat-a'] = [
      { ...openAction, state: 'working' },
      { ...openAction, id: 'other', state: 'waiting', params: { ...openAction.params, url: 'https://auth.example.invalid/' } },
    ]
    expect(tab.location.href).toBe('')
    store.chatActions['chat-a'] = [{ ...openAction, state: 'waiting', params: { ...openAction.params, url: 'javascript:alert(1)' } }]
    expect(tab.close).toHaveBeenCalled()
    expect(tab.location.href).toBe('')
    root.unmount()
  })

  it('synchronously cancels when the store removes its list even while card props are stale', async () => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    const response = pending()
    vi.spyOn(store, 'startChatAction').mockReturnValue(response.promise as Promise<ChatAction | null>)
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', action: () => openAction, onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    const started = controller.start(openAction.id)
    delete store.chatActions['chat-a']
    expect(tab.close).toHaveBeenCalledOnce()
    response.resolve({ ...openAction, state: 'waiting', params: { ...openAction.params, url: 'https://auth.example.invalid/' } })
    await started
    expect(tab.location.href).toBe('')
    root.unmount()
  })

  it('adopts a conflict without retrying or navigating its replacement action', async () => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    const replacement = { ...openAction, id: 'new-id', state: 'waiting', params: { ...openAction.params, url: 'https://auth.example.invalid/' } }
    post.mockRejectedValueOnce(Object.assign(new Error('conflict'), { status: 409, payload: { actions: [replacement] } }))
    const tab = { closed: false, opener: {}, location: { href: '' }, close: vi.fn() }
    const popup = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    await controller.start(openAction.id)
    expect(store.chatActions['chat-a']).toEqual([replacement])
    expect(post).toHaveBeenCalledTimes(1)
    expect(popup).toHaveBeenCalledTimes(1)
    expect(tab.close).toHaveBeenCalled()
    expect(tab.location.href).toBe('')
    root.unmount()
  })

  it('does not let a previous watched ID cancel a newly created action', async () => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    post.mockResolvedValueOnce({ action: { ...openAction, state: 'failed' } })
    const fresh = { ...openAction, id: 'fresh' }
    post.mockResolvedValueOnce({ action: fresh })
    post.mockResolvedValueOnce({ action: { ...fresh, state: 'working' } })
    vi.spyOn(window, 'open').mockReturnValue(null)
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => 'chat-a', provider: () => 'opencode', server: () => 'linear', onNotice: vi.fn(), onError: vi.fn() })
      return () => h('div')
    } }))
    await controller.start(openAction.id)
    store.chatActions['chat-a'] = []
    await controller.start()
    expect(post).toHaveBeenCalledWith('/api/chats/chat-a/actions/fresh/start', {})
    root.unmount()
  })

  it.each(['resolve', 'reject'] as const)('old start %s cannot affect a new context popup or its lease', async outcome => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.chatActions['chat-a'] = [openAction]
    const context = ref('chat-a')
    let resolve!: (value: ChatAction | null) => void
    let reject!: (error: Error) => void
    const old = new Promise<ChatAction | null>((yes, no) => { resolve = yes; reject = no })
    const newer = pending()
    vi.spyOn(store, 'startChatAction').mockReturnValue(old)
    vi.spyOn(store, 'requestMcpAuth').mockReturnValue(newer.promise as Promise<ChatAction | null>)
    const tabs = [0, 1].map(() => ({ closed: false, opener: {}, location: { href: '' }, close: vi.fn() }))
    vi.spyOn(window, 'open').mockReturnValueOnce(tabs[0] as unknown as Window).mockReturnValueOnce(tabs[1] as unknown as Window)
    const notice = vi.fn(), error = vi.fn()
    let controller!: ReturnType<typeof useMcpAuthStart>
    const root = mount(defineComponent({ setup() {
      controller = useMcpAuthStart({ chatId: () => context.value, provider: () => 'opencode', server: () => 'linear', onNotice: notice, onError: error })
      return () => h('div')
    } }))
    const first = controller.start(openAction.id)
    context.value = 'chat-b'
    const second = controller.start()
    if (outcome === 'reject') reject(new Error('old failed'))
    else resolve({ ...openAction, state: 'waiting', params: { ...openAction.params, url: 'https://auth.example.invalid/' } })
    await first
    expect(tabs[1]!.close).not.toHaveBeenCalled()
    expect(tabs[1]!.location.href).toBe('')
    expect(controller.busy.value).toBe(true)
    expect(error).not.toHaveBeenCalledWith('The sign-in could not be started. Try again.')
    newer.resolve(null)
    await second
    root.unmount()
  })
})
