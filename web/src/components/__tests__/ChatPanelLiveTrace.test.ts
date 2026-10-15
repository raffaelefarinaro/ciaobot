// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'

const PaneHeaderStub = defineComponent({
  name: 'PaneHeaderStub',
  setup(_, { slots }) {
    return () => h('header', { class: 'pane-header' }, [
      slots.title?.(),
      h('div', { class: 'header-actions' }, slots.actions?.()),
    ])
  },
})

const ChildStub = defineComponent({
  name: 'ChildStub',
  setup() {
    return () => h('div')
  },
})

const ChatCommentPopoverStub = defineComponent({
  name: 'ChatCommentPopoverStub',
  setup(_, { expose }) {
    expose({
      openId: null,
      close: vi.fn(),
      clearPendingClose: vi.fn(),
      onTargetOver: vi.fn(),
      onTargetOut: vi.fn(),
      pinFromEvent: vi.fn(),
      show: vi.fn(),
    })
    return () => h('div')
  },
})

const MODELS_RESPONSE = {
  models: ['haiku', 'sonnet', 'opus', 'fable'],
  default: 'sonnet',
  provider_models: { claude: ['haiku', 'sonnet', 'opus', 'fable'] },
  provider_defaults: { claude: 'sonnet' },
  thinking_levels: {},
  model_reasoning_levels: {},
  model_options: {},
  backends: { anthropic: true },
}

function makeProject(): ProjectInfo {
  return {
    project_id: 'project-1',
    name: 'General',
    workspace: 'personal',
    context: '',
    created_at: '',
    order: 0,
    vault_folder: '',
  }
}

function makeChat(chatId: string): ChatInfo {
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title: chatId,
    model: 'sonnet',
    provider: 'opencode',
    mode: 'normal',
    session_id: '',
    created_at: '',
    archived: false,
  }
}

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

async function mountPanel(): Promise<{
  wrapper: VueWrapper
  store: ReturnType<typeof useProjectStore>
}> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat('chat-1')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [] }
  store.bootstrapped = true

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })

  const wrapper = shallowMount(ChatPanel, {
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
        ChatTurnActivity: false,
      },
    },
  })
  await flushPromises()
  return { wrapper, store }
}

const MID = 'Mid-turn update that is long enough to once have been its own bubble. '.repeat(4)

function turnMessages() {
  return [
    { role: 'user' as const, content: 'do it', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
    { role: 'assistant' as const, content: MID, timestamp: '2026-08-30T13:23:05Z' },
    { role: 'system' as const, content: '· read a.md', timestamp: '2026-08-30T13:23:06Z', tool_name: '_activity' },
    { role: 'assistant' as const, content: 'All done, final reply.', timestamp: '2026-08-30T13:23:10Z' },
  ]
}

function stubMetrics(el: Element, m: { scrollHeight: number; clientHeight: number; scrollTop: number }) {
  for (const [k, v] of Object.entries(m)) {
    Object.defineProperty(el, k, { configurable: true, writable: true, value: v })
  }
}

describe('ChatPanel live trace', () => {
  const scrollProto = Element.prototype as unknown as { scrollTo?: unknown }
  const originalScrollTo = scrollProto.scrollTo

  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
    scrollProto.scrollTo = function scrollTo() {}
  })

  afterEach(() => {
    scrollProto.scrollTo = originalScrollTo
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('opens the live trace by default while streaming', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnMessages().slice(0, 1)
    store.streaming['chat-1'] = true
    await flushPromises()
    expect(wrapper.get('.trace-block.live').classes()).toContain('open')
  })

  it('reopens the live trace for the next turn after the user closed it', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnMessages().slice(0, 1)
    store.streaming['chat-1'] = true
    await flushPromises()
    await wrapper.get('.trace-block.live .trace-summary').trigger('click')
    expect(wrapper.get('.trace-block.live').classes()).not.toContain('open')

    store.streaming['chat-1'] = false
    await flushPromises()
    store.streaming['chat-1'] = true
    await flushPromises()
    expect(wrapper.get('.trace-block.live').classes()).toContain('open')
  })

  it('renders the finished turn collapsed with only the final reply as a bubble', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnMessages().slice(0, 1)
    store.streaming['chat-1'] = true
    await flushPromises()
    store.messages['chat-1'] = turnMessages()
    store.streaming['chat-1'] = false
    await flushPromises()

    const bubbles = wrapper.findAll('.message.assistant')
    expect(bubbles.length).toBe(1)
    expect(bubbles[0].text()).toContain('All done, final reply.')
    expect(wrapper.get('.trace-block').classes()).not.toContain('open')
  })

  it('keeps the finished trace open when the user was reading, and collapses it on return to bottom', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnMessages().slice(0, 1)
    store.streaming['chat-1'] = true
    await flushPromises()

    const messagesEl = wrapper.get('.messages').element
    // End the open-time pin-to-bottom window (a rAF loop that re-pins for 700ms
    // after mount) so it cannot overwrite the scroll positions stubbed below.
    messagesEl.dispatchEvent(new Event('wheel'))
    stubMetrics(messagesEl, { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    stubMetrics(messagesEl, { scrollHeight: 2000, clientHeight: 500, scrollTop: 600 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()

    store.messages['chat-1'] = turnMessages()
    store.streaming['chat-1'] = false
    await flushPromises()
    expect(wrapper.get('.trace-block').classes()).toContain('open')

    stubMetrics(messagesEl, { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()
    expect(wrapper.get('.trace-block').classes()).not.toContain('open')
  })

  async function streamingPanel(releaseOpenPin = true) {
    const ctx = await mountPanel()
    ctx.store.messages['chat-1'] = turnMessages().slice(0, 1)
    ctx.store.streaming['chat-1'] = true
    await flushPromises()
    const messagesEl = ctx.wrapper.get('.messages').element
    // End the open-time pin-to-bottom window so only turn-end logic moves the view.
    if (releaseOpenPin) messagesEl.dispatchEvent(new Event('wheel'))
    return { ...ctx, messagesEl }
  }

  async function finishTurn(store: ReturnType<typeof useProjectStore>) {
    store.messages['chat-1'] = turnMessages()
    store.streaming['chat-1'] = false
    await flushPromises()
  }

  // Resolve the turn-end history reload the way the real one lands: loading
  // flips on, then off, after the turn has already ended.
  async function reloadHistory(store: ReturnType<typeof useProjectStore>, background = false) {
    let release: (v: unknown) => void = () => {}
    ;(api.get as unknown as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
      if (path.includes('/messages')) return new Promise(r => { release = r })
      if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE)
      if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] })
      return Promise.resolve([])
    })
    const p = store.loadMessages('chat-1', background ? { background: true } : undefined)
    await flushPromises()
    release([])
    await p
    await flushPromises()
    await new Promise(r => setTimeout(r, 60))
    await flushPromises()
  }

  // The reply starts 5300px into the transcript, so its on-screen top moves
  // with the transcript's scrollTop (800px below the top at scrollTop 4500).
  function stubReplyGeometry(messagesEl: Element) {
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.matches('.message.assistant') ? 1000 : 0
    })
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      const top = this.matches('.message.assistant') ? 5300 - (messagesEl as HTMLElement).scrollTop : 0
      return { top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top, toJSON() {} } as DOMRect
    })
  }

  it('lands on the start of a tall reply when the turn-end background reload settles after', async () => {
    const { wrapper, store, messagesEl } = await streamingPanel()
    stubReplyGeometry(messagesEl)
    stubMetrics(messagesEl, { scrollHeight: 5000, clientHeight: 500, scrollTop: 4500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()

    await finishTurn(store)
    await reloadHistory(store, true)

    expect((messagesEl as HTMLElement).scrollTop).toBe(4500 + 800 - 16)
    expect(wrapper.get('.trace-block').classes()).not.toContain('open')
  })

  it('lands on a tall reply even while the chat-open bottom pin is still running', async () => {
    const { store, messagesEl } = await streamingPanel(false)
    stubReplyGeometry(messagesEl)
    stubMetrics(messagesEl, { scrollHeight: 5000, clientHeight: 500, scrollTop: 4500 })

    await finishTurn(store)
    await reloadHistory(store, true)

    expect((messagesEl as HTMLElement).scrollTop).not.toBe(5000)
  })

  it('pins to the bottom when a foreground history load settles after a finished turn', async () => {
    const { store, messagesEl } = await streamingPanel()
    stubReplyGeometry(messagesEl)
    stubMetrics(messagesEl, { scrollHeight: 5000, clientHeight: 500, scrollTop: 4500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()

    await finishTurn(store)
    expect((messagesEl as HTMLElement).scrollTop).toBe(4500 + 800 - 16)
    await reloadHistory(store)

    expect((messagesEl as HTMLElement).scrollTop).toBe(5000)
  })

  it('pins to the bottom of an idle chat when switching away from a streaming one', async () => {
    const { wrapper, store, messagesEl } = await streamingPanel()
    stubReplyGeometry(messagesEl)
    store.chats = [...store.chats, { ...store.chats[0], chat_id: 'chat-2', title: 'chat-2' }]
    store.messages['chat-2'] = turnMessages()
    stubMetrics(messagesEl, { scrollHeight: 5000, clientHeight: 500, scrollTop: 4500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()

    store.activeChatId = 'chat-2'
    await flushPromises()
    await new Promise(r => setTimeout(r, 60))

    expect((messagesEl as HTMLElement).scrollTop).toBe(5000)
    expect(wrapper.findAll('.trace-block.open').length).toBe(0)
  })

  it('follows the live trace tail again after the user closed and reopened it', async () => {
    const { wrapper, store } = await streamingPanel()
    store.streamingText['chat-1'] = 'working on it'
    await flushPromises()
    const first = wrapper.get('.trace-block.live .trace-body').element
    stubMetrics(first, { scrollHeight: 900, clientHeight: 300, scrollTop: 600 })
    first.dispatchEvent(new Event('scroll'))
    stubMetrics(first, { scrollHeight: 900, clientHeight: 300, scrollTop: 100 })
    first.dispatchEvent(new Event('scroll'))
    await flushPromises()

    const summary = wrapper.get('.trace-block.live .trace-summary')
    await summary.trigger('click')
    await summary.trigger('click')
    const body = wrapper.get('.trace-block.live .trace-body').element
    stubMetrics(body, { scrollHeight: 900, clientHeight: 300, scrollTop: 0 })
    store.streamingText['chat-1'] = 'working on it, more'
    await flushPromises()

    expect((body as HTMLElement).scrollTop).toBe(900)
  })

  it('keeps a reading hold in place through the turn-end background reload', async () => {
    const { wrapper, store, messagesEl } = await streamingPanel()
    stubMetrics(messagesEl, { scrollHeight: 5000, clientHeight: 500, scrollTop: 4500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    stubMetrics(messagesEl, { scrollHeight: 5000, clientHeight: 500, scrollTop: 1000 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()

    await finishTurn(store)
    await reloadHistory(store, true)

    expect((messagesEl as HTMLElement).scrollTop).toBe(1000)
    expect(wrapper.get('.trace-block').classes()).toContain('open')
  })

  it('does not collapse a trace the user closed then reopened until they scroll away and back', async () => {
    const { wrapper, store, messagesEl } = await streamingPanel()
    stubMetrics(messagesEl, { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()
    const summary = wrapper.get('.trace-block.live .trace-summary')
    await summary.trigger('click')
    await summary.trigger('click')

    await finishTurn(store)
    expect(wrapper.get('.trace-block').classes()).toContain('open')
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()
    expect(wrapper.get('.trace-block').classes()).toContain('open')

    stubMetrics(messagesEl, { scrollHeight: 2000, clientHeight: 500, scrollTop: 600 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()
    expect(wrapper.get('.trace-block').classes()).toContain('open')
    stubMetrics(messagesEl, { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()
    expect(wrapper.get('.trace-block').classes()).not.toContain('open')
  })

  it('does not hold the trace for a scroll event caused by the transcript growing', async () => {
    const { wrapper, store, messagesEl } = await streamingPanel()
    stubMetrics(messagesEl, { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    // Content grows under a stationary viewport: not at the bottom, but the
    // user did not scroll up.
    stubMetrics(messagesEl, { scrollHeight: 2600, clientHeight: 500, scrollTop: 1500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()

    store.messages['chat-1'] = turnMessages()
    store.streaming['chat-1'] = false
    stubMetrics(messagesEl, { scrollHeight: 2600, clientHeight: 500, scrollTop: 2100 })
    await flushPromises()
    expect(wrapper.get('.trace-block').classes()).not.toContain('open')
  })

  it('holds the trace after an upward scroll inside the live trace body', async () => {
    const { wrapper, store } = await streamingPanel()
    store.streamingText['chat-1'] = 'working on it'
    await flushPromises()
    const body = wrapper.get('.trace-block.live .trace-body').element
    stubMetrics(body, { scrollHeight: 900, clientHeight: 300, scrollTop: 600 })
    body.dispatchEvent(new Event('scroll'))
    stubMetrics(body, { scrollHeight: 900, clientHeight: 300, scrollTop: 100 })
    body.dispatchEvent(new Event('scroll'))
    await flushPromises()

    await finishTurn(store)
    expect(wrapper.get('.trace-block').classes()).toContain('open')
  })

  it('ignores a growth-only scroll inside the live trace body', async () => {
    const { wrapper, store } = await streamingPanel()
    store.streamingText['chat-1'] = 'working on it'
    await flushPromises()
    const body = wrapper.get('.trace-block.live .trace-body').element
    stubMetrics(body, { scrollHeight: 900, clientHeight: 300, scrollTop: 600 })
    body.dispatchEvent(new Event('scroll'))
    stubMetrics(body, { scrollHeight: 1200, clientHeight: 300, scrollTop: 600 })
    body.dispatchEvent(new Event('scroll'))
    await flushPromises()

    await finishTurn(store)
    expect(wrapper.get('.trace-block').classes()).not.toContain('open')
  })

  it('lands on the bottom, not an earlier turn reply, when the new turn has no reply', async () => {
    const { store, messagesEl } = await streamingPanel()
    stubReplyGeometry(messagesEl)
    stubMetrics(messagesEl, { scrollHeight: 5000, clientHeight: 500, scrollTop: 4500 })
    messagesEl.dispatchEvent(new Event('scroll'))
    await flushPromises()
    // Earlier turn with a (tall) reply, then a new turn that only ran tools.
    store.messages['chat-1'] = [
      ...turnMessages(),
      { role: 'user', content: 'again', timestamp: '2026-08-30T13:30:00Z', turn_index: 1 },
      { role: 'system', content: '· read b.md', timestamp: '2026-08-30T13:30:02Z', tool_name: '_activity' },
    ]
    store.streaming['chat-1'] = false
    await flushPromises()
    await new Promise(r => setTimeout(r, 60))
    expect((messagesEl as HTMLElement).scrollTop).toBe(5000)
  })
})
