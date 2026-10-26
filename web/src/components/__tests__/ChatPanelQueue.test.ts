// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick } from 'vue'
import { flushPromises, RouterLinkStub, shallowMount } from '@vue/test-utils'
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

const ChildStub = defineComponent({ name: 'ChildStub', setup: () => () => h('div') })

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
  models: ['sonnet', 'opus'],
  default: 'sonnet',
  provider_models: {
    claude: ['sonnet', 'opus'],
    opencode: ['anthropic/claude-sonnet-4-6'],
  },
  provider_defaults: { claude: 'sonnet', opencode: 'anthropic/claude-sonnet-4-6' },
  thinking_levels: {},
  model_reasoning_levels: {},
  model_options: {},
  backends: { anthropic: true },
}

function makeProject(): ProjectInfo {
  return {
    project_id: 'project-1',
    name: 'Upwordo',
    workspace: 'personal',
    context: '',
    created_at: '',
    order: 0,
    vault_folder: '',
  } as unknown as ProjectInfo
}

function makeChat(chatId: string, extra: Partial<ChatInfo> = {}): ChatInfo {
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title: chatId,
    model: 'sonnet',
    provider: 'claude',
    mode: 'normal',
    session_id: '',
    created_at: '2026-08-01T00:00:00Z',
    archived: false,
    local: true,
    ...extra,
  } as ChatInfo
}

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
  clear() { this.values.clear() }
}

async function mountPanel(
  setup: (store: ReturnType<typeof useProjectStore>) => void = () => {},
) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat('chat-1')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [] }
  store.workspaces = [
    { name: 'personal', color: 'emerald' },
    { name: 'work', color: 'pink' },
  ] as unknown as typeof store.workspaces
  store.bootstrapped = true
  setup(store)

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path === '/api/commands') return Promise.resolve({ commands: [] }) as never
    return Promise.resolve([]) as never
  })

  const wrapper = shallowMount(ChatPanel, {
    attachTo: document.body,
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: RouterLinkStub,
      },
    },
  })
  await flushPromises()
  return { wrapper, store }
}


const send = vi.fn()

function queue(store: ReturnType<typeof useProjectStore>, entries: Array<Record<string, unknown>>) {
  store.queuedMessages['chat-1'] = entries.map(e => ({ delivery: 'queue', steering: false, ...e })) as never
}

class FakeSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = 1
  onopen: (() => void) | null = null
  onmessage: unknown = null
  onclose: unknown = null
  onerror: unknown = null
  send = send
  close() {}
}

function openSocket(store: ReturnType<typeof useProjectStore>) {
  send.mockClear()
  vi.stubGlobal('WebSocket', FakeSocket)
  store.connectWs('chat-1')
}

describe('ChatPanel queued messages', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('renders a Queue/Steer button group with aria-pressed and a labelled excerpt', async () => {
    const { wrapper } = await mountPanel((store) => {
      store.streaming['chat-1'] = true
      queue(store, [{ id: 'a', text: 'one' }, { id: 'b', text: 'two', delivery: 'steer' }])
    })
    const groups = wrapper.findAll('.queued-mode')
    expect(groups).toHaveLength(2)
    expect(groups[0].attributes('role')).toBe('group')
    expect(groups[0].attributes('aria-label')).toContain('one')
    const first = groups[0].findAll('button')
    expect(first.map(r => r.text())).toEqual(['Queue', 'Steer'])
    expect(first.map(r => r.attributes('aria-pressed'))).toEqual(['true', 'false'])
    const second = groups[1].findAll('button')
    expect(second.map(r => r.attributes('aria-pressed'))).toEqual(['false', 'true'])
    wrapper.unmount()
  })

  it('choosing Steer sends one queue_set_delivery frame for that entry', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'one' }])
    })
    openSocket(store)
    await wrapper.findAll('.queued-mode button')[1].trigger('click')
    expect(JSON.parse(send.mock.calls[0][0])).toEqual({ type: 'queue_set_delivery', entry_ids: ['a'], delivery: 'steer' })
    wrapper.unmount()
  })

  it('steers a message with images', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'look', images: ['shot.png'] }])
    })
    openSocket(store)
    const steer = wrapper.findAll('.queued-mode button')[1]
    expect(steer.attributes('disabled')).toBeUndefined()
    await steer.trigger('click')
    expect(JSON.parse(send.mock.calls[0][0])).toMatchObject({ entry_ids: ['a'], delivery: 'steer' })
    wrapper.unmount()
  })

  it('Steer is disabled when not streaming; a waiting chip offers Send now instead', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      queue(s, [{ id: 'w', text: 'held' }])
    })
    expect(wrapper.find('.queued-mode').exists()).toBe(false)
    const sendNow = wrapper.get('.queued-send-now')
    expect(sendNow.text()).toBe('Send now')
    expect(wrapper.get('.queued-label').text()).toBe('Waiting')
    openSocket(store)
    await sendNow.trigger('click')
    expect(JSON.parse(send.mock.calls[0][0])).toEqual({ type: 'queue_set_delivery', entry_ids: ['w'], delivery: 'steer' })
    wrapper.unmount()
  })

  it('Steer all appears with two queue entries and sends both ids in list order', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'one' }, { id: 'b', text: 'two' }, { id: 'c', text: 'three', steering: true, delivery: 'steer' }])
    })
    openSocket(store)
    const all = wrapper.get('.queued-steer-all')
    expect(all.text()).toBe('Steer all')
    await all.trigger('click')
    expect(JSON.parse(send.mock.calls[0][0])).toEqual({ type: 'queue_set_delivery', entry_ids: ['a', 'b'], delivery: 'steer' })
    wrapper.unmount()
  })

  it('Steer all is absent with a single queue entry', async () => {
    const { wrapper } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'one' }])
    })
    expect(wrapper.find('.queued-steer-all').exists()).toBe(false)
    wrapper.unmount()
  })

  it('in-flight chip shows Steering… and hides the edit, reorder, remove and mode controls', async () => {
    const { wrapper } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'one', delivery: 'steer', steering: true }])
    })
    const status = wrapper.get('.queued-steering')
    expect(status.text()).toContain('Steering')
    // The locked row is gone rather than shown disabled.
    expect(wrapper.find('.queued-actions').exists()).toBe(false)
    expect(wrapper.find('.queued-mode').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the fallback notice as a status line outside the queue block', async () => {
    const { wrapper } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'one' }])
      s.steerNotice['chat-1'] = 'The reply Steering missed the reply. It will be sent as the next message.'
    })
    const notice = wrapper.get('.steer-notice')
    expect(notice.attributes('role')).toBe('status')
    expect(notice.text()).toContain('Steering missed the reply')
    expect(wrapper.find('.queued-messages .steer-notice').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the notice with no chips left', async () => {
    const { wrapper } = await mountPanel((s) => {
      s.steerNotice['chat-1'] = 'Stopped before it was steered. It stays in the queue.'
    })
    expect(wrapper.find('.queued-messages').exists()).toBe(false)
    expect(wrapper.get('.steer-notice').text()).toContain('Stopped')
    wrapper.unmount()
  })

  it('a waiting chip during a running turn reads Waiting with a Steer button, not Queue/Steer', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'w', text: 'held', waiting: true }, { id: 'l', text: 'live' }])
    })
    const chips = wrapper.findAll('.queued-chip')
    expect(chips[0].find('.queued-mode').exists()).toBe(false)
    expect(chips[0].get('.queued-label').text()).toBe('Waiting')
    const steer = chips[0].get('.queued-send-now')
    expect(steer.text()).toBe('Steer')
    expect(chips[1].find('.queued-mode').exists()).toBe(true)
    openSocket(store)
    await steer.trigger('click')
    expect(JSON.parse(send.mock.calls[0][0])).toEqual({ type: 'queue_set_delivery', entry_ids: ['w'], delivery: 'steer' })
    wrapper.unmount()
  })

  it('moves focus to the chip when the focused Steer button is replaced', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'one' }])
    })
    openSocket(store)
    const steer = wrapper.findAll('.queued-mode button')[1]
    ;(steer.element as HTMLElement).focus()
    await steer.trigger('click')
    await nextTick()
    expect(document.activeElement).toBe(wrapper.get('.queued-chip').element)
    wrapper.unmount()
  })

  it('announces steering through one persistent live region in the queue container', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
    })
    // The region exists before any chip does, so a later change is announced.
    const live = wrapper.get('.queue-live')
    expect(live.attributes('aria-live')).toBe('polite')
    expect(live.text()).toBe('')
    queue(store, [
      { id: 'a', text: 'one', delivery: 'steer', steering: true },
      { id: 'b', text: 'two', delivery: 'steer', steering: true },
    ])
    await nextTick()
    expect(wrapper.get('.queue-live').text()).toBe('Steering 2 messages')
    expect(wrapper.findAll('.queued-chip [aria-live]')).toHaveLength(0)
    wrapper.unmount()
  })

  it('re-focuses the composer when the focused chip unmounts', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'one' }])
    })
    ;(wrapper.get('.queued-chip').element as HTMLElement).focus()
    expect(document.activeElement).toBe(wrapper.get('.queued-chip').element)
    store.queuedMessages['chat-1'] = []
    await nextTick()
    await nextTick()
    expect(document.activeElement).toBe(wrapper.get('.chat-input').element)
    wrapper.unmount()
  })

  it('focuses the composer bar instead of opening the keyboard on a narrow pane', async () => {
    const originalWidth = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
    try {
      const { wrapper, store } = await mountPanel((s) => {
        s.streaming['chat-1'] = true
        queue(s, [{ id: 'a', text: 'one' }])
      })
      ;(wrapper.get('.queued-chip').element as HTMLElement).focus()
      store.queuedMessages['chat-1'] = []
      await nextTick()
      await nextTick()
      expect(document.activeElement).toBe(wrapper.get('.composer-bar').element)
      wrapper.unmount()
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    }
  })

  it('steered user bubble shows the steered mark', async () => {
    const { wrapper } = await mountPanel((s) => {
      s.messages['chat-1'] = [
        { role: 'user', content: 'go', timestamp: '', turn_index: 0 },
        { role: 'user', content: 'actually do X', timestamp: '', turn_index: 1, steered: true },
      ] as never
    })
    const marks = wrapper.findAll('.steered-mark')
    expect(marks).toHaveLength(1)
    expect(marks[0].text()).toBe('steered')
    wrapper.unmount()
  })
})
