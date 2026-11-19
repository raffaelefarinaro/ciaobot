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
      props: { chatId: 'chat-1' },
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
let socket: FakeSocket

function queue(store: ReturnType<typeof useProjectStore>, entries: Array<Record<string, unknown>>) {
  store.queuedMessages['chat-1'] = entries.map(e => ({ ...e })) as never
}

class FakeSocket {
  constructor() { socket = this }
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = 1
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
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

  it('moves a queued message and its images into the empty composer', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'edit me', images: ['shot.png'] }])
    })
    openSocket(store)
    await wrapper.get('[aria-label="Edit queued message"]').trigger('click')
    expect((wrapper.get('.chat-input').element as HTMLTextAreaElement).value).toBe('')
    expect(wrapper.find('.queued-chip').exists()).toBe(true)
    const request = JSON.parse(send.mock.calls[0][0])
    expect(request).toMatchObject({ type: 'queue_take', entry_id: 'a' })
    socket.onmessage?.({ data: JSON.stringify({ type: 'queue_take_result', request_id: request.request_id, entry_id: 'a', ok: true, text: 'edit me', images: ['shot.png'] }) })
    await flushPromises()
    expect((wrapper.get('.chat-input').element as HTMLTextAreaElement).value).toBe('edit me')
    expect(store.pendingImagesFor('chat-1')).toEqual(['shot.png'])
    expect(JSON.parse(send.mock.calls[1][0])).toEqual({ type: 'queue_take_ack', entry_id: 'a' })
    expect(wrapper.find('.queued-chip').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the chip when the server refuses the take', async () => {
    const { wrapper, store } = await mountPanel(s => queue(s, [{ id: 'a', text: 'queued' }]))
    openSocket(store)
    await wrapper.get('[aria-label="Edit queued message"]').trigger('click')
    const request = JSON.parse(send.mock.calls[0][0])
    socket.onmessage?.({ data: JSON.stringify({ type: 'queue_take_result', request_id: request.request_id, entry_id: 'a', ok: false, text: '', images: [] }) })
    await flushPromises()
    expect(wrapper.get('.queued-chip').text()).toContain('queued')
    expect(wrapper.get('.queue-edit-notice').text()).toContain('Could not')
    wrapper.unmount()
  })

  it('does not overwrite a draft typed while a take is pending, and applies it once the draft clears', async () => {
    const { wrapper, store } = await mountPanel(s => queue(s, [{ id: 'a', text: 'queued' }]))
    openSocket(store)
    await wrapper.get('[aria-label="Edit queued message"]').trigger('click')
    const request = JSON.parse(send.mock.calls[0][0])
    await wrapper.get('.chat-input').setValue('new draft')
    socket.onmessage?.({ data: JSON.stringify({ type: 'queue_take_result', request_id: request.request_id, entry_id: 'a', ok: true, text: 'queued', images: [] }) })
    await flushPromises()
    expect((wrapper.get('.chat-input').element as HTMLTextAreaElement).value).toBe('new draft')
    expect(wrapper.get('.queue-edit-notice').text()).toContain('was taken out of the queue')
    // The text is still only in the receipt, so nothing is acknowledged yet and
    // it cannot be lost: clearing the draft puts it in the composer.
    expect(send.mock.calls.map(c => JSON.parse(c[0]))).not.toContainEqual(expect.objectContaining({ type: 'queue_take_ack' }))
    await wrapper.get('.chat-input').setValue('')
    await flushPromises()
    expect((wrapper.get('.chat-input').element as HTMLTextAreaElement).value).toBe('queued')
    expect(send.mock.calls.map(c => JSON.parse(c[0]))).toContainEqual({ type: 'queue_take_ack', entry_id: 'a' })
    wrapper.unmount()
  })

  it('restores a pending take before the socket opens and acknowledges on connect', async () => {
    const { wrapper, store } = await mountPanel(s => {
      s.pendingQueueTakes['chat-1'] = [{ entry_id: 'a', text: 'recovered', images: ['shot.png'] }]
    })
    expect((wrapper.get('.chat-input').element as HTMLTextAreaElement).value).toBe('recovered')
    expect(store.pendingImagesFor('chat-1')).toEqual(['shot.png'])
    openSocket(store)
    socket.onopen?.()
    expect(send.mock.calls.map(call => JSON.parse(call[0]))).toContainEqual({ type: 'queue_take_ack', entry_id: 'a' })
    wrapper.unmount()
  })

  it('keeps the draft and queued message when the composer contains text', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'queued' }])
    })
    openSocket(store)
    await wrapper.get('.chat-input').setValue('existing draft')
    await wrapper.get('[aria-label="Edit queued message"]').trigger('click')
    expect((wrapper.get('.chat-input').element as HTMLTextAreaElement).value).toBe('existing draft')
    expect(wrapper.get('.queued-chip').text()).toContain('queued')
    expect(wrapper.get('[role="status"].queue-edit-notice').text()).toContain('delete')
    expect(send).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('leaves the message queued if the chat is disconnected', async () => {
    const { wrapper } = await mountPanel((s) => {
      queue(s, [{ id: 'a', text: 'queued' }])
    })
    await wrapper.get('[aria-label="Edit queued message"]').trigger('click')
    expect((wrapper.get('.chat-input').element as HTMLTextAreaElement).value).toBe('')
    expect(wrapper.get('.queued-chip').text()).toContain('queued')
    expect(wrapper.get('.queue-edit-notice').text()).toContain('connection')
    wrapper.unmount()
  })

  it('a running turn shows Send next only on waiting chips', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      s.streaming['chat-1'] = true
      queue(s, [{ id: 'a', text: 'one' }, { id: 'b', text: 'two', waiting: true }])
    })
    openSocket(store)
    expect(wrapper.find('.queued-mode').exists()).toBe(false)
    expect(wrapper.find('.queued-send-all').exists()).toBe(false)
    const buttons = wrapper.findAll('.queued-send-now')
    expect(buttons.map(b => b.text())).toEqual(['Send next'])
    expect(buttons[0].attributes('title')).toBe('Run after this reply')
    await buttons[0].trigger('click')
    expect(JSON.parse(send.mock.calls[0][0])).toEqual({ type: 'queue_send_now', entry_ids: ['b'] })
    const labels = wrapper.findAll('.queued-chip .queued-label').map(l => l.text())
    expect(labels).toEqual(['Queued', 'Waiting'])
    wrapper.unmount()
  })

  it('idle waiting chips offer Send now and Send all now', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      queue(s, [{ id: 'a', text: 'one', waiting: true }, { id: 'b', text: 'two', waiting: true }])
    })
    openSocket(store)
    const all = wrapper.get('.queued-send-all')
    expect(all.text()).toBe('Send all now')
    await all.trigger('click')
    expect(JSON.parse(send.mock.calls[0][0])).toEqual({ type: 'queue_send_now', entry_ids: ['a', 'b'] })
    send.mockClear()
    const one = wrapper.findAll('.queued-send-now')
    expect(one.map(b => b.text())).toEqual(['Send now', 'Send now'])
    await one[1].trigger('click')
    expect(JSON.parse(send.mock.calls[0][0])).toEqual({ type: 'queue_send_now', entry_ids: ['b'] })
    wrapper.unmount()
  })

  it('Send all now is absent with a single waiting entry', async () => {
    const { wrapper } = await mountPanel((s) => {
      queue(s, [{ id: 'a', text: 'one', waiting: true }])
    })
    expect(wrapper.find('.queued-send-all').exists()).toBe(false)
    expect(wrapper.find('.queued-send-now').exists()).toBe(true)
    wrapper.unmount()
  })

  it('focus moves to the chip when its Send now is clicked and the chip stays', async () => {
    const { wrapper, store } = await mountPanel((s) => {
      queue(s, [{ id: 'a', text: 'one', waiting: true }])
    })
    openSocket(store)
    const sendNow = wrapper.get('.queued-send-now')
    ;(sendNow.element as HTMLElement).focus()
    await sendNow.trigger('click')
    await nextTick()
    expect(document.activeElement).toBe(wrapper.get('.queued-chip').element)
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
})
