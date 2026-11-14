// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'

let pinia: ReturnType<typeof createPinia>
const wrappers: VueWrapper[] = []
const Stub = defineComponent({ setup: (_, { slots }) => () => h('div', slots.default?.()) })
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
})

async function setup(path: string, ids = ['chat-a'], loaded = true, withAction = false) {
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
      global: { plugins: [pinia, router], stubs: { ChatPanel: false, ChatMcpConnections: false, NeedsYouCard: defineComponent({ setup: () => () => h('div', { class: 'needs-you-card' }) }), PaneHeader: Stub, ChatCommentPopover: CommentStub, RouterLink: Stub } },
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
