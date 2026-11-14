// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, nextTick, ref } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import { readChatDraft, readSentPromptHistory, recordSentPrompt } from '../../lib/chatDrafts'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { applyKeyboardSettings } from '../../composables/useKeyboardSettings'
import ChatPanel from '../ChatPanel.vue'

let pinia: ReturnType<typeof createPinia>
const wrappers: VueWrapper[] = []
const Stub = defineComponent({ setup: (_, { slots }) => () => h('div', slots.default?.()) })
const HeaderStub = defineComponent({ setup: (_, { slots }) => () => h('header', [slots.title?.(), slots.actions?.()]) })
const MenuStub = defineComponent({
  setup(_, { emit, expose }) {
    const trigger = ref<HTMLButtonElement | null>(null)
    expose({ focusTrigger: () => trigger.value?.focus() })
    return () => h('button', { ref: trigger, class: 'chat-more-btn', onClick: () => emit('details') }, 'Chat details')
  },
})
const CommentStub = defineComponent({ setup(_, { expose }) {
  expose({ clearPendingClose: vi.fn(), close: vi.fn(), openId: null })
  return () => h('div')
} })
const chat = (id: string) => ({
  chat_id: id, project_id: 'project', title: id, model: 'sonnet', provider: 'opencode',
  mode: 'normal', session_id: '', created_at: '', archived: false,
})

beforeEach(() => {
  localStorage.clear()
  pinia = createPinia()
  setActivePinia(pinia)
  vi.spyOn(api, 'get').mockResolvedValue({} as never)
  vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: vi.fn() })
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})
afterEach(() => {
  wrappers.splice(0).forEach(w => w.unmount())
  disposePinia(pinia)
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  applyKeyboardSettings({ keyboard_shortcuts: {}, keyboard_send_mode: 'modifier' })
})

async function setup(path: string, ids = ['chat-a'], loaded = true, withAction = false, realMenu = false) {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/chat/:chatId', component: Stub },
    { path: '/settings/:tab', component: Stub },
    { path: '/', component: Stub },
  ] })
  await router.push(path)
  await router.isReady()
  const removed = vi.fn()
  const afterEach = router.afterEach.bind(router)
  vi.spyOn(router, 'afterEach').mockImplementation(guard => {
    const remove = afterEach(guard)
    return () => { removed(); remove() }
  })
  const store = useProjectStore()
  store.projects = [{ project_id: 'project', name: 'Test', workspace: 'work space', context: '', created_at: '', order: 0, vault_folder: '' }]
  store.chats = loaded ? ids.map(id => chat(id) as never) : []
  store.activeWorkspace = 'personal'
  store.activeChatId = ids[0]!
  store.messages = Object.fromEntries(ids.map(id => [id, []]))
  if (withAction) store.chatActions['chat-a'] = [{ id: 'action-a', kind: 'mcp_auth', origin: 'engine', state: 'open', params: { server: 'linear', provider: 'opencode' }, error: '', created_at: 1 }]
  store.bootstrapped = true
  const panels = ids.map((id, i) => {
    const Host = defineComponent({
      setup: () => () => store.chatById(id)
        ? h(ChatPanel, { chatId: id, beside: i > 0, inactive: i > 0 })
        : h('div'),
    })
    const wrapper = shallowMount(loaded ? ChatPanel : Host, {
      attachTo: document.body,
      props: loaded ? { chatId: id, beside: i > 0, inactive: i > 0 } : {},
      global: { plugins: [pinia, router], stubs: { ChatPanel: false, ChatMcpConnections: false, ...(realMenu ? { ChatHeaderMoreMenu: MenuStub } : {}), NeedsYouCard: defineComponent({ setup: () => () => h('div', { class: 'needs-you-card' }) }), PaneHeader: HeaderStub, ChatCommentPopover: CommentStub, RouterLink: Stub } },
    })
    wrappers.push(wrapper)
    return wrapper
  })
  await flushPromises()
  return { router, store, panels, removed }
}

describe('query reveal tokens', () => {
  it('opens only the route-owning pane and focuses its MCP heading', async () => {
    const { panels, router } = await setup('/chat/chat-a?details=mcp&keep=yes#anchor', ['chat-a', 'chat-b'])
    expect(panels[0]!.find('.chat-mcp-heading').exists()).toBe(true)
    expect(panels[1]!.find('.chat-mcp-heading').exists()).toBe(false)
    expect(document.activeElement).toBe(panels[0]!.get('.chat-mcp-heading').element)
    expect(router.currentRoute.value.fullPath).toBe('/chat/chat-a?details=mcp&keep=yes#anchor')
  })
  it('keeps the heading focused without scrolling page ancestors when an action exists', async () => {
    const intoView = vi.spyOn(HTMLElement.prototype, 'scrollIntoView')
    const { panels } = await setup('/chat/chat-a?details=mcp', ['chat-a'], true, true)
    expect(panels[0]!.find('.needs-you-card').exists()).toBe(true)
    expect(document.activeElement).toBe(panels[0]!.get('.chat-mcp-heading').element)
    expect(intoView).not.toHaveBeenCalled()
  })
  it('reveals a later action in the owning drawer after the one-shot heading focus', async () => {
    const { panels, store } = await setup('/chat/chat-a?details=mcp')
    const panel = panels[0]!
    const heading = panel.get('.chat-mcp-heading')
    expect(document.activeElement).toBe(heading.element)
    const rail = panel.get('.chat-rail--drawer').element as HTMLElement
    const scroll = vi.fn()
    Object.defineProperty(rail, 'scrollTo', { configurable: true, value: scroll })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this === rail) return { top: 0, bottom: 200, height: 200 } as DOMRect
      if (this.classList.contains('needs-you-card')) return { top: 600, bottom: 700, height: 100 } as DOMRect
      return { top: 0, bottom: 0, height: 0 } as DOMRect
    })
    store.chatActions['chat-a'] = [{ id: 'new-action', kind: 'mcp_auth', origin: 'engine', state: 'open', params: { server: 'linear', provider: 'opencode' }, error: '', created_at: 1 }]
    await flushPromises()
    expect(panel.find('.needs-you-card').exists()).toBe(true)
    expect(scroll).toHaveBeenCalledWith({ top: 600, behavior: 'auto' })
    expect(document.activeElement).toBe(heading.element)
  })
  it('does not reopen for unrelated query changes, but reopens for an identical link', async () => {
    const { panels, router } = await setup('/chat/chat-a?details=mcp&keep=yes')
    const panel = panels[0]!
    await panel.get('.chat-rail-hide').trigger('click')
    await router.push('/chat/chat-a?details=mcp&keep=no#other')
    await flushPromises()
    expect(panel.find('.chat-mcp-heading').exists()).toBe(false)
    await router.push('/chat/chat-a?details=mcp&keep=no#other')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
    await panel.get('.chat-rail-hide').trigger('click')
    await router.push({ path: '/chat/chat-a', query: { details: 'mcp', keep: 'no' }, hash: '#other', force: true })
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
  })
  it('reopens after leaving and returning and Escape consumes the reveal', async () => {
    const { panels, router } = await setup('/chat/chat-a?details=mcp')
    const panel = panels[0]!
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(panel.find('.chat-mcp-heading').exists()).toBe(false)
    await router.push('/chat/chat-a')
    await router.push('/chat/chat-a?details=mcp')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
  })
  it('rearms on browser back and forward to the link', async () => {
    const { panels, router } = await setup('/chat/chat-a?details=mcp')
    const panel = panels[0]!
    await router.push('/chat/chat-a')
    await router.push('/chat/chat-a?details=mcp')
    await flushPromises()
    await panel.get('.chat-rail-hide').trigger('click')
    router.back()
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/chat/chat-a'))
    router.forward()
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/chat/chat-a?details=mcp'))
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
  })
  it('focuses the section in the wide rail without starting authorization', async () => {
    class WideObserver {
      callback: ResizeObserverCallback
      constructor(callback: ResizeObserverCallback) { this.callback = callback }
      observe() { this.callback([{ contentRect: { width: 1200 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', WideObserver)
    const popup = vi.spyOn(window, 'open').mockReturnValue(null)
    const post = vi.spyOn(api, 'post')
    const { panels } = await setup('/chat/chat-a?details=mcp')
    expect(panels[0]!.get('.chat-rail').classes()).not.toContain('chat-rail--drawer')
    expect(document.activeElement).toBe(panels[0]!.get('.chat-mcp-heading').element)
    expect(popup).not.toHaveBeenCalled()
    expect(post).not.toHaveBeenCalled()
  })
  it('opens settings for the chat workspace and actual CLI, not active workspace', async () => {
    const { panels, router } = await setup('/chat/chat-a?details=mcp')
    const button = panels[0]!.get('.chat-mcp-settings')
    expect(button.text()).toBe('Chat workspace MCP settings')
    expect(button.attributes('title')).toContain("this chat's workspace and CLI")
    await button.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/settings/mcp?workspace=work+space&provider=opencode')
  })
  it('refuses settings navigation when the chat workspace is unresolved', async () => {
    const { panels, store, router } = await setup('/chat/chat-a?details=mcp')
    store.projects = []
    await panels[0]!.get('.chat-mcp-settings').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/chat/chat-a')
    expect(panels[0]!.get('[role=alert]').text()).toContain('could not be resolved')
  })
  it('waits for the chat provider before revealing', async () => {
    const { panels, store } = await setup('/chat/chat-a?details=mcp')
    await panels[0]!.get('.chat-rail-hide').trigger('click')
    store.chats[0]!.provider = '' as never
    await flushPromises()
    const router = (panels[0]!.vm as unknown as { $router: ReturnType<typeof createRouter> }).$router
    await router.push('/chat/chat-a?details=mcp')
    await flushPromises()
    expect(panels[0]!.find('.chat-mcp-heading').exists()).toBe(false)
    store.chats[0]!.provider = 'opencode'
    await flushPromises()
    expect(document.activeElement).toBe(panels[0]!.get('.chat-mcp-heading').element)
  })
  it('reveals after the route-owning chat loads and the pane mounts', async () => {
    const { panels, store } = await setup('/chat/chat-a?details=mcp', ['chat-a'], false)
    expect(panels[0]!.find('.chat-mcp-heading').exists()).toBe(false)
    store.chats = [chat('chat-a') as never]
    await flushPromises()
    expect(document.activeElement).toBe(panels[0]!.get('.chat-mcp-heading').element)
  })
  it('removes its navigation hook when the pane unmounts', async () => {
    const { panels, removed, router } = await setup('/chat/chat-a?details=mcp')
    const panel = panels[0]!
    wrappers.splice(wrappers.indexOf(panel), 1)
    panel.unmount()
    expect(removed).toHaveBeenCalledOnce()
    await router.push('/chat/chat-a')
    await router.push('/chat/chat-a?details=mcp')
    expect(removed).toHaveBeenCalledOnce()
  })
})

describe('quiet MCP discovery', () => {
  it('focuses MCP first and then reveals an existing action in the drawer', async () => {
    const focusAtScroll: boolean[] = []
    const scroll = vi.spyOn(HTMLElement.prototype, 'scrollTo').mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains('chat-rail--drawer')) {
        focusAtScroll.push(document.activeElement === this.querySelector('.chat-mcp-heading'))
      }
    })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains('chat-rail')) return { top: 0, bottom: 200, height: 200 } as DOMRect
      if (this.classList.contains('needs-you-card')) return { top: 600, bottom: 700, height: 100 } as DOMRect
      return { top: 0, bottom: 0, height: 0 } as DOMRect
    })
    const { panels } = await setup('/chat/chat-a', ['chat-a'], true, true)
    const panel = panels[0]!
    await panel.get('.work-inspector-trigger--header').trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
    expect(scroll).toHaveBeenCalledWith({ top: 600, behavior: 'auto' })
    expect(focusAtScroll).toEqual([true])
  })

  it('marks a closed narrow details trigger, opens MCP, and observes while open', async () => {
    const { panels, store } = await setup('/chat/chat-a', ['chat-a'], true, true)
    const panel = panels[0]!
    const trigger = panel.get('.work-inspector-trigger--header')
    expect(trigger.attributes('aria-label')).toBe('Show chat details, 1 MCP connections need attention')
    expect(trigger.find('.chat-mcp-indicator').exists()).toBe(true)
    expect(panel.findComponent({ name: 'ChatHeaderMoreMenu' }).props('mcpProblems')).toBe(1)
    await trigger.trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
    expect(panel.get('.work-inspector-trigger--header').find('.chat-mcp-indicator').exists()).toBe(false)
    expect(panel.findComponent({ name: 'ChatHeaderMoreMenu' }).props('mcpProblems')).toBe(0)
    store.chatActions['chat-a'] = []
    await flushPromises()
    await panel.get('.chat-rail-hide').trigger('click')
    expect(panel.get('.work-inspector-trigger--header').attributes('aria-label')).toBe('Show chat details')
  })

  it('marks a closed wide rail and targets MCP; an unmarked trigger opens general details', async () => {
    class WideObserver {
      callback: ResizeObserverCallback
      constructor(callback: ResizeObserverCallback) { this.callback = callback }
      observe() { this.callback([{ contentRect: { width: 1200 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', WideObserver)
    const { panels, store } = await setup('/chat/chat-a', ['chat-a'], true, true)
    const panel = panels[0]!
    await panel.get('.chat-rail-hide').trigger('click')
    const trigger = panel.get('.work-inspector-trigger')
    expect(trigger.find('.chat-mcp-indicator').exists()).toBe(true)
    await trigger.trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
    expect(panel.find('.work-inspector-trigger').exists()).toBe(false)
    store.chatActions['chat-a'] = []
    await panel.get('.chat-rail-hide').trigger('click')
    await flushPromises()
    await panel.get('.work-inspector-trigger').trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-rail-hide').element)
  })

  it('keeps observing exact-chat inventory while the drawer is open', async () => {
    const { panels } = await setup('/chat/chat-a')
    const panel = panels[0]!
    let state = 'failed'
    vi.mocked(api.get).mockImplementation(async path => path.startsWith('/api/mcp/status?')
      ? { workspace: 'work space', providers: [{ provider: 'opencode', available: true, inventory_state: 'loaded', servers: [
        { name: 'linear', scope: 'project', source: 'config', allowed: true, connections: [{ chat_id: 'chat-a', state }, { chat_id: 'other', state: 'failed' }] },
      ] }] } as never : {} as never)
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(panel.get('.work-inspector-trigger--header').find('.chat-mcp-indicator').exists()).toBe(true)
    await panel.get('.work-inspector-trigger--header').trigger('click')
    await flushPromises()
    state = 'connected'
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    await panel.get('.chat-rail-hide').trigger('click')
    expect(panel.get('.work-inspector-trigger--header').find('.chat-mcp-indicator').exists()).toBe(false)
  })

  it('updates the marker from exact-chat observations while details stay closed', async () => {
    const { panels } = await setup('/chat/chat-a')
    const panel = panels[0]!
    let state = 'connected'
    vi.mocked(api.get).mockImplementation(async path => path.startsWith('/api/mcp/status?')
      ? { workspace: 'work space', providers: [{ provider: 'opencode', available: true, inventory_state: 'loaded', servers: [{
        name: 'linear', scope: 'project', source: 'config', allowed: true,
        connections: [{ chat_id: 'chat-a', state }, { chat_id: 'other', state: 'failed' }],
      }] }] } as never : {} as never)
    const trigger = panel.get('.work-inspector-trigger--header')
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(trigger.find('.chat-mcp-indicator').exists()).toBe(false)
    state = 'failed'
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(trigger.attributes('aria-label')).toBe('Show chat details, 1 MCP connections need attention')
    expect(trigger.find('.chat-mcp-indicator').exists()).toBe(true)
    expect(panel.find('.chat-rail').exists()).toBe(false)
    state = 'connected'
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(trigger.attributes('aria-label')).toBe('Show chat details')
    expect(trigger.find('.chat-mcp-indicator').exists()).toBe(false)
    expect(panel.find('.chat-rail').exists()).toBe(false)
  })

  it('opens marked menu details at MCP and returns focus to More on Escape', async () => {
    const { panels } = await setup('/chat/chat-a', ['chat-a'], true, true, true)
    const panel = panels[0]!
    const more = panel.get('.chat-more-btn')
    await more.trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(document.activeElement).toBe(more.element)
  })
})

describe('local MCP shortcut', () => {
  it.each(['claude', 'opencode'] as const)('keeps Stop enabled after %s local navigation while upload continues', async provider => {
    const { panels, store } = await setup('/chat/chat-a')
    store.chats[0]!.provider = provider
    store.streaming['chat-a'] = true
    let finish: (refs: string[]) => void = () => {}
    vi.spyOn(store, 'uploadImages').mockReturnValue(new Promise(resolve => { finish = resolve }))
    const stop = vi.spyOn(store, 'stopChat').mockImplementation(() => {})
    const send = vi.spyOn(store, 'sendMessage')
    const panel = panels[0]!
    const picker = panel.get('.image-btn input')
    Object.defineProperty(picker.element, 'files', { value: [new File(['x'], 'image.png', { type: 'image/png' })], configurable: true })
    await picker.trigger('change')
    await panel.get('.chat-input').setValue('/mcp')
    await panel.get('[aria-label="Open MCP connections"]').trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
    await panel.get('.chat-rail-hide').trigger('click')
    const button = panel.get('[aria-label="Stop generation"]')
    expect(button.attributes('disabled')).toBeUndefined()
    await button.trigger('click')
    expect(stop).toHaveBeenCalledOnce()
    expect(send).not.toHaveBeenCalled()
    expect(panel.find('.chat-mcp-heading').exists()).toBe(false)
    expect(panel.findComponent({ name: 'ComposerUploadProgress' }).props('uploads')).toHaveLength(1)
    finish([])
    await flushPromises()
  })

  it('offers Stop for an empty streaming composer during an ordinary upload', async () => {
    const { panels, store } = await setup('/chat/chat-a')
    store.streaming['chat-a'] = true
    let finish: (refs: string[]) => void = () => {}
    vi.spyOn(store, 'uploadImages').mockReturnValue(new Promise(resolve => { finish = resolve }))
    const panel = panels[0]!
    const picker = panel.get('.image-btn input')
    Object.defineProperty(picker.element, 'files', { value: [new File(['x'], 'image.png', { type: 'image/png' })], configurable: true })
    await picker.trigger('change')
    expect(panel.get('[aria-label="Stop generation"]').attributes('disabled')).toBeUndefined()
    finish([])
    await flushPromises()
  })

  it.each(['claude', 'opencode'] as const)('navigates %s with every staged bucket, queue, stream, upload and disconnected socket intact through remount', async provider => {
    const { panels, store, router } = await setup('/chat/chat-a')
    wrappers.splice(wrappers.indexOf(panels[0]!), 1)
    panels[0]!.unmount()
    store.chats[0]!.provider = provider
    store.activeWorkspace = 'different-active-workspace'
    store.streaming['chat-a'] = true
    store.pendingImages = ['image.png']
    store.addPendingFiles('chat-a', [{ ref: 'file-ref', name: 'file.txt' }])
    store.pendingComments = [{ id: 'fc', path: 'file.txt', selection: 'selection', comment: 'note' }]
    store.pendingChatComments = [{ id: 'cc', selection: 'selection', comment: 'note' } as never]
    store.queuedMessages['chat-a'] = [{ id: 'queued', text: 'queued', delivery: 'queue', steering: false } as never]
    const buckets = () => [store.pendingImagesFor('chat-a'), store.pendingFilesFor('chat-a'), store.pendingCommentsFor('chat-a'), store.pendingChatCommentsFor('chat-a')]
    const before = buckets()
    const contents = before.map(bucket => JSON.stringify(bucket))
    const queue = store.queuedMessages['chat-a']
    const queueContents = JSON.stringify(queue)
    recordSentPrompt('chat-a', 'prior real prompt')
    const history = readSentPromptHistory('chat-a')
    expect(history).toEqual(['prior real prompt'])
    const frames = vi.fn()
    class Socket { static OPEN = 1; readyState = 1; send = frames; close() {} }
    vi.stubGlobal('WebSocket', Socket)
    store.connectWs('chat-a')
    store.disconnectWs('chat-a')
    const send = vi.spyOn(store, 'sendMessage')
    const stop = vi.spyOn(store, 'stopChat')
    const post = vi.spyOn(api, 'post')
    let finish: (refs: string[]) => void = () => {}
    const upload = vi.spyOn(store, 'uploadImages').mockReturnValue(new Promise(resolve => { finish = resolve }))
    const shown = ref(true)
    const Host = defineComponent({ setup: () => () => shown.value ? h(ChatPanel, { chatId: 'chat-a' }) : h('div') })
    const host = shallowMount(Host, {
      attachTo: document.body,
      global: { plugins: [pinia, router], stubs: { ChatPanel: false, ChatMcpConnections: false, PaneHeader: HeaderStub, ChatCommentPopover: CommentStub, RouterLink: Stub } },
    })
    wrappers.push(host)
    await flushPromises()
    const picker = host.get('.image-btn input')
    Object.defineProperty(picker.element, 'files', { value: [new File(['x'], 'image.png', { type: 'image/png' })], configurable: true })
    await picker.trigger('change')
    expect(upload).toHaveBeenCalledOnce()
    await host.get('.chat-input').setValue(' \t/McP \n')
    await host.get('[aria-label="Open MCP connections"]').trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(host.get('.chat-mcp-heading').element)
    expect(host.get('.chat-mcp-provider').text()).toBe(provider === 'claude' ? 'Claude' : 'OpenCode')
    expect(vi.mocked(api.get).mock.calls.some(([path]) => path === '/api/mcp/status?workspace=work%20space')).toBe(true)
    expect(vi.mocked(api.get).mock.calls.some(([path]) => path === '/api/mcp/status?workspace=different-active-workspace')).toBe(false)
    expect(readChatDraft('chat-a')).toBe('')
    expect(send).not.toHaveBeenCalled()
    expect(stop).not.toHaveBeenCalled()
    expect(post).not.toHaveBeenCalled()
    expect(frames).not.toHaveBeenCalled()
    shown.value = false
    await nextTick()
    shown.value = true
    await flushPromises()
    expect((host.get('.chat-input').element as HTMLTextAreaElement).value).toBe('')
    expect(readChatDraft('chat-a')).toBe('')
    buckets().forEach((bucket, i) => { expect(bucket).toBe(before[i]); expect(JSON.stringify(bucket)).toBe(contents[i]) })
    expect(store.queuedMessages['chat-a']).toBe(queue)
    expect(JSON.stringify(store.queuedMessages['chat-a'])).toBe(queueContents)
    expect(readSentPromptHistory('chat-a')).toEqual(history)
    finish([])
    await flushPromises()
  })
  it.each(['claude', 'opencode'] as const)('opens %s MCP details without sending', async provider => {
    const { panels, store } = await setup('/chat/chat-a')
    store.chats[0]!.provider = provider
    const send = vi.spyOn(store, 'sendMessage')
    const panel = panels[0]!
    await panel.get('textarea').setValue(' \t/McP \n')
    expect(panel.get('.send-btn').attributes('aria-label')).toBe('Open MCP connections')
    await panel.get('.send-btn').trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
    expect((panel.get('textarea').element as HTMLTextAreaElement).value).toBe('')
    expect(send).not.toHaveBeenCalled()
  })

  it.each(['claude', 'opencode'] as const)('keeps Stop separate from local navigation while %s streams', async provider => {
    const { panels, store } = await setup('/chat/chat-a')
    store.chats[0]!.provider = provider
    store.streaming['chat-a'] = true
    store.pendingImages = ['image.png']
    const send = vi.spyOn(store, 'sendMessage')
    const stop = vi.spyOn(store, 'stopChat').mockImplementation(() => {})
    const panel = panels[0]!
    await panel.get('.chat-input').setValue('/mcp')
    await panel.get('[aria-label="Stop generation"]').trigger('click')
    expect(stop).toHaveBeenCalledOnce()
    expect((panel.get('.chat-input').element as HTMLTextAreaElement).value).toBe('/mcp')
    expect(panel.find('.chat-mcp-heading').exists()).toBe(false)
    await panel.get('[aria-label="Open MCP connections"]').trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
    expect(store.pendingImagesFor('chat-a')).toEqual(['image.png'])
    expect(panel.get('.send-btn').attributes('aria-label')).toBe('Queue message')
    expect(send).not.toHaveBeenCalled()
    expect(readChatDraft('chat-a')).toBe('')
    expect(readSentPromptHistory('chat-a')).toEqual([])
  })

  it.each([
    ['unsupported', '', "MCP details are unavailable for this chat's provider."],
    ['claude', '', "This chat's workspace is not available yet. Try again after it loads."],
    ['opencode', '', "This chat's workspace is not available yet. Try again after it loads."],
  ])('reserves unavailable %s token without sending and allows retry', async (provider, workspace, reason) => {
    const { panels, store } = await setup('/chat/chat-a')
    store.chats[0]!.provider = provider as never
    store.projects[0]!.workspace = workspace
    store.pendingImages = ['image.png']
    const send = vi.spyOn(store, 'sendMessage')
    const panel = panels[0]!
    const raw = ' \t/McP \n'
    await panel.get('.chat-input').setValue(raw)
    await panel.get('[aria-label="Why MCP details are unavailable"]').trigger('click')
    expect(panel.text()).toContain(reason)
    expect(readChatDraft('chat-a')).toBe(raw)
    expect(store.pendingImagesFor('chat-a')).toEqual(['image.png'])
    expect(panel.find('.chat-mcp-heading').exists()).toBe(false)
    expect(send).not.toHaveBeenCalled()
    store.chats[0]!.provider = 'claude'
    store.projects[0]!.workspace = 'work'
    await flushPromises()
    await panel.get('[aria-label="Open MCP connections"]').trigger('click')
    await flushPromises()
    expect(readChatDraft('chat-a')).toBe('')
    expect(store.pendingImagesFor('chat-a')).toEqual(['image.png'])
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
  })

  it.each([true, false])('preserves all staged buckets through parent remount (destination valid: %s)', async valid => {
    const { panels, store, router } = await setup('/chat/chat-a')
    wrappers.splice(wrappers.indexOf(panels[0]!), 1)
    panels[0]!.unmount()
    store.pendingImages = ['image.png']
    store.addPendingFiles('chat-a', [{ ref: 'file-ref', name: 'file.txt' }])
    store.pendingComments = [{ id: 'file-comment', path: 'file.txt', selection: 'selection', comment: 'note' }]
    store.pendingChatComments = [{ id: 'chat-comment', selection: 'selection', comment: 'note' } as never]
    store.queuedMessages['chat-a'] = [{ id: 'queued', text: 'queued', delivery: 'queue', steering: false } as never]
    if (!valid) store.projects = []
    const buckets = [store.pendingImagesFor('chat-a'), store.pendingFilesFor('chat-a'), store.pendingCommentsFor('chat-a'), store.pendingChatCommentsFor('chat-a')]
    const contents = buckets.map(bucket => JSON.stringify(bucket))
    const queue = store.queuedMessages['chat-a']
    const shown = ref(true)
    const Host = defineComponent({ setup: () => () => shown.value ? h(ChatPanel, { chatId: 'chat-a' }) : h('div') })
    const host = shallowMount(Host, {
      attachTo: document.body,
      global: { plugins: [pinia, router], stubs: { ChatPanel: false, ChatMcpConnections: false, PaneHeader: Stub, ChatCommentPopover: CommentStub, RouterLink: Stub } },
    })
    wrappers.push(host)
    await flushPromises()
    const raw = ' \t/McP \n'
    await host.get('.chat-input').setValue(raw)
    await host.get(valid ? '[aria-label="Open MCP connections"]' : '[aria-label="Why MCP details are unavailable"]').trigger('click')
    shown.value = false
    await nextTick()
    shown.value = true
    await flushPromises()
    expect((host.get('.chat-input').element as HTMLTextAreaElement).value).toBe(valid ? '' : raw)
    expect(readChatDraft('chat-a')).toBe(valid ? '' : raw)
    const after = [store.pendingImagesFor('chat-a'), store.pendingFilesFor('chat-a'), store.pendingCommentsFor('chat-a'), store.pendingChatCommentsFor('chat-a')]
    after.forEach((bucket, i) => {
      expect(bucket).toBe(buckets[i])
      expect(JSON.stringify(bucket)).toBe(contents[i])
    })
    expect(store.queuedMessages['chat-a']).toBe(queue)
    expect(readSentPromptHistory('chat-a')).toEqual([])
  })

  it.each(['/mcp status', '/mcp\nstatus', '/mcps', 'ask /mcp'])('sends ordinary prompt %s and records it', async text => {
    const { panels, store } = await setup('/chat/chat-a')
    const send = vi.spyOn(store, 'sendMessage').mockReturnValue(true)
    const panel = panels[0]!
    await panel.get('.chat-input').setValue(text)
    await panel.get('.send-btn').trigger('click')
    expect(send).toHaveBeenCalledOnce()
    expect(send.mock.calls[0]![1]).toBe(text)
    expect(readSentPromptHistory('chat-a')).toEqual([text])
    expect(panel.find('.chat-mcp-heading').exists()).toBe(false)
  })

  it.each(['claude', 'opencode'] as const)('honors composition and modifier chord for %s', async provider => {
    const { panels, store } = await setup('/chat/chat-a')
    store.chats[0]!.provider = provider
    const send = vi.spyOn(store, 'sendMessage')
    const panel = panels[0]!
    const input = panel.get('.chat-input')
    await input.setValue('/mcp')
    await input.trigger('keydown', { key: 'Enter', isComposing: true, metaKey: true })
    await input.trigger('keydown', { key: 'Enter', keyCode: 229, ctrlKey: true })
    expect(readChatDraft('chat-a')).toBe('/mcp')
    expect(panel.find('.chat-mcp-heading').exists()).toBe(false)
    await input.trigger('keydown', { key: 'Enter', metaKey: true })
    await flushPromises()
    expect(document.activeElement).toBe(panel.get('.chat-mcp-heading').element)
    expect(send).not.toHaveBeenCalled()
  })

  it.each([true, false])('handles the local action during an upload (destination valid: %s)', async valid => {
    const { panels, store } = await setup('/chat/chat-a')
    if (!valid) store.projects = []
    let finish: (refs: string[]) => void = () => {}
    const upload = vi.spyOn(store, 'uploadImages').mockReturnValue(new Promise(resolve => { finish = resolve }))
    const send = vi.spyOn(store, 'sendMessage')
    const panel = panels[0]!
    const file = new File([new Uint8Array([1])], 'image.png', { type: 'image/png' })
    const picker = panel.get('.image-btn input')
    Object.defineProperty(picker.element, 'files', { value: [file], configurable: true })
    await picker.trigger('change')
    expect(upload).toHaveBeenCalledOnce()
    await panel.get('.chat-input').setValue('/mcp')
    const action = panel.get(valid ? '[aria-label="Open MCP connections"]' : '[aria-label="Why MCP details are unavailable"]')
    expect(action.attributes('disabled')).toBeUndefined()
    await action.trigger('click')
    await flushPromises()
    expect(readChatDraft('chat-a')).toBe(valid ? '' : '/mcp')
    expect(panel.find('.chat-mcp-heading').exists()).toBe(valid)
    expect(send).not.toHaveBeenCalled()
    finish([])
    await flushPromises()
  })

  it('keeps Stop after navigation in an empty streaming composer', async () => {
    const { panels, store } = await setup('/chat/chat-a')
    store.streaming['chat-a'] = true
    const panel = panels[0]!
    await panel.get('.chat-input').setValue('/mcp')
    await panel.get('[aria-label="Open MCP connections"]').trigger('click')
    await flushPromises()
    expect(panel.get('.send-btn').attributes('aria-label')).toBe('Stop generation')
  })

  it('reserves an unavailable token on the streaming chord', async () => {
    const { panels, store } = await setup('/chat/chat-a')
    store.streaming['chat-a'] = true
    store.projects = []
    const send = vi.spyOn(store, 'sendMessage')
    const stop = vi.spyOn(store, 'stopChat')
    const panel = panels[0]!
    const input = panel.get('.chat-input')
    await input.setValue(' \t/McP \n')
    await input.trigger('keydown', { key: 'Enter', ctrlKey: true })
    expect(readChatDraft('chat-a')).toBe(' \t/McP \n')
    expect(panel.text()).toContain("This chat's workspace is not available yet. Try again after it loads.")
    expect(panel.find('.chat-mcp-heading').exists()).toBe(false)
    expect(send).not.toHaveBeenCalled()
    expect(stop).not.toHaveBeenCalled()
  })

  it('opens only the pane owning the invoked send shortcut', async () => {
    const { panels, store } = await setup('/chat/chat-a', ['chat-a', 'chat-b'])
    store.chats[0]!.provider = 'claude'
    store.chats[1]!.provider = 'opencode'
    await panels[0]!.get('.chat-input').setValue('/mcp')
    await panels[1]!.get('.chat-input').setValue('/mcp')
    const second = panels[1]!.vm as unknown as { handleSendShortcut: () => boolean }
    expect(second.handleSendShortcut()).toBe(true)
    await flushPromises()
    expect(panels[0]!.find('.chat-mcp-heading').exists()).toBe(false)
    expect(document.activeElement).toBe(panels[1]!.get('.chat-mcp-heading').element)
    expect(readChatDraft('chat-a')).toBe('/mcp')
    expect(readChatDraft('chat-b')).toBe('')
  })

  it('leaves touch bare Enter to the keyboard and navigates with a modifier', async () => {
    vi.stubGlobal('matchMedia', vi.fn().mockImplementation((query: string) => ({
      matches: query === '(pointer: coarse)', media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })))
    applyKeyboardSettings({ keyboard_send_mode: 'enter' })
    const { panels, store } = await setup('/chat/chat-a')
    const send = vi.spyOn(store, 'sendMessage')
    const input = panels[0]!.get('.chat-input')
    await input.setValue('/mcp')
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    expect(input.element.dispatchEvent(enter)).toBe(true)
    expect(panels[0]!.find('.chat-mcp-heading').exists()).toBe(false)
    expect(send).not.toHaveBeenCalled()
    // jsdom does not perform the browser's default textarea newline insertion.
    const textarea = input.element as HTMLTextAreaElement
    textarea.setRangeText('\n', textarea.value.length, textarea.value.length, 'end')
    await input.trigger('input')
    expect(textarea.value).toBe('/mcp\n')
    await input.trigger('keydown', { key: 'Enter', ctrlKey: true })
    await flushPromises()
    expect(panels[0]!.find('.chat-mcp-heading').exists()).toBe(true)
    expect(send).not.toHaveBeenCalled()
  })

  it('declines the exposed shortcut while editing a staged chat comment and when archived', async () => {
    const { panels, store } = await setup('/chat/chat-a')
    store.pendingChatComments = [{ id: 'edit-me', selection: 'selection', comment: 'note' } as never]
    const send = vi.spyOn(store, 'sendMessage')
    const panel = panels[0]!
    await panel.get('.chat-input').setValue('/mcp')
    panel.findComponent(CommentStub).vm.$emit('edit', { id: 'edit-me', comment: 'note' })
    await nextTick()
    const shortcut = panel.vm as unknown as { handleSendShortcut: () => boolean }
    expect(shortcut.handleSendShortcut()).toBe(false)
    expect(readChatDraft('chat-a')).toBe('/mcp')
    expect(panel.find('.chat-mcp-heading').exists()).toBe(false)
    store.chats[0]!.archived = true
    await nextTick()
    expect(shortcut.handleSendShortcut()).toBe(false)
    expect(readChatDraft('chat-a')).toBe('/mcp')
    expect(send).not.toHaveBeenCalled()
  })

  const unavailableCases = [
    { provider: 'unsupported', workspace: null, reason: "MCP details are unavailable for this chat's provider." },
    ...(['claude', 'opencode'] as const).flatMap(provider => [null, ''].map(workspace => ({
      provider, workspace, reason: "This chat's workspace is not available yet. Try again after it loads.",
    }))),
  ]
  const matrix = unavailableCases.flatMap(destination =>
    [false, true].flatMap(streaming =>
      [false, true].flatMap(uploading =>
        ['click', 'chord'].map(entry => ({ ...destination, streaming, uploading, entry })),
      ),
    ),
  )
  it.each(matrix)('preserves invalid destination and retries: $provider workspace=$workspace streaming=$streaming uploading=$uploading via $entry', async ({ provider, workspace, reason, streaming, uploading, entry }) => {
    const { panels, store, router } = await setup('/chat/chat-a')
    wrappers.splice(wrappers.indexOf(panels[0]!), 1)
    panels[0]!.unmount()
    store.chats[0]!.provider = provider as never
    if (workspace === null) store.projects = []
    else store.projects[0]!.workspace = workspace
    store.activeWorkspace = 'personal'
    store.streaming['chat-a'] = streaming
    store.pendingImages = ['image.png']
    store.addPendingFiles('chat-a', [{ ref: 'file-ref', name: 'file.txt' }])
    store.pendingComments = [{ id: 'fc', path: 'file.txt', selection: 'selection', comment: 'note' }]
    store.pendingChatComments = [{ id: 'cc', selection: 'selection', comment: 'note' } as never]
    store.queuedMessages['chat-a'] = [{ id: 'queued', text: 'queued', delivery: 'queue', steering: false } as never]
    const buckets = () => [store.pendingImagesFor('chat-a'), store.pendingFilesFor('chat-a'), store.pendingCommentsFor('chat-a'), store.pendingChatCommentsFor('chat-a')]
    const before = buckets()
    const contents = before.map(bucket => JSON.stringify(bucket))
    const queue = store.queuedMessages['chat-a']
    const send = vi.spyOn(store, 'sendMessage')
    const stop = vi.spyOn(store, 'stopChat')
    const post = vi.spyOn(api, 'post')
    const frames = vi.fn()
    class Socket {
      static OPEN = 1
      readyState = 1
      send = frames
      close() {}
    }
    vi.stubGlobal('WebSocket', Socket)
    store.connectWs('chat-a')
    let finish: (refs: string[]) => void = () => {}
    if (uploading) vi.spyOn(store, 'uploadImages').mockReturnValue(new Promise(resolve => { finish = resolve }))
    const shown = ref(true)
    const Host = defineComponent({ setup: () => () => shown.value ? h(ChatPanel, { chatId: 'chat-a' }) : h('div') })
    const host = shallowMount(Host, {
      attachTo: document.body,
      global: { plugins: [pinia, router], stubs: { ChatPanel: false, ChatMcpConnections: false, PaneHeader: Stub, ChatCommentPopover: CommentStub, RouterLink: Stub } },
    })
    wrappers.push(host)
    await flushPromises()
    if (uploading) {
      const picker = host.get('.image-btn input')
      Object.defineProperty(picker.element, 'files', { value: [new File(['x'], 'image.png', { type: 'image/png' })], configurable: true })
      await picker.trigger('change')
    }
    const raw = ' \t/McP \n'
    await host.get('.chat-input').setValue(raw)
    frames.mockClear()
    if (entry === 'click') await host.get('[aria-label="Why MCP details are unavailable"]').trigger('click')
    else await host.get('.chat-input').trigger('keydown', { key: 'Enter', metaKey: true })
    expect(host.text()).toContain(reason)
    expect(host.find('.chat-mcp-heading').exists()).toBe(false)
    expect(send).not.toHaveBeenCalled()
    expect(stop).not.toHaveBeenCalled()
    expect(post).not.toHaveBeenCalled()
    expect(frames).not.toHaveBeenCalled()
    shown.value = false
    await nextTick()
    shown.value = true
    await flushPromises()
    expect((host.get('.chat-input').element as HTMLTextAreaElement).value).toBe(raw)
    expect(readChatDraft('chat-a')).toBe(raw)
    buckets().forEach((bucket, i) => { expect(bucket).toBe(before[i]); expect(JSON.stringify(bucket)).toBe(contents[i]) })
    expect(store.queuedMessages['chat-a']).toBe(queue)
    expect(readSentPromptHistory('chat-a')).toEqual([])
    store.chats[0]!.provider = 'claude'
    store.projects = [{ project_id: 'project', name: 'Test', workspace: 'work', context: '', created_at: '', order: 0, vault_folder: '' }]
    await flushPromises()
    await host.get('[aria-label="Open MCP connections"]').trigger('click')
    await flushPromises()
    expect(readChatDraft('chat-a')).toBe('')
    buckets().forEach((bucket, i) => { expect(bucket).toBe(before[i]); expect(JSON.stringify(bucket)).toBe(contents[i]) })
    expect(store.queuedMessages['chat-a']).toBe(queue)
    expect(readSentPromptHistory('chat-a')).toEqual([])
    expect(send).not.toHaveBeenCalled()
    expect(post).not.toHaveBeenCalled()
    expect(frames).not.toHaveBeenCalled()
    finish([])
    await flushPromises()
    store.disconnectWs('chat-a')
  })
})
