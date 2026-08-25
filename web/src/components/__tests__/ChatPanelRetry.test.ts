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
    provider: 'claude',
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
  sendMessage: ReturnType<typeof vi.spyOn>
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
  vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })

  const sendMessage = vi.spyOn(store, 'sendMessage')
  const wrapper = shallowMount(ChatPanel, {
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
        VoiceRecorder: ChildStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
      },
    },
  })
  await flushPromises()
  return { wrapper, store, sendMessage }
}

describe('ChatPanel retry from error bubble', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('resends the prior turn text plus its images', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.messages['chat-1'] = [
      {
        role: 'user',
        content: 'Check the video',
        timestamp: '2026-08-08T15:51:00Z',
        images: ['web_5a4d7145.png'],
      },
      {
        role: 'assistant',
        content: 'Error: this model does not support image input',
        timestamp: '2026-08-08T15:51:01Z',
        is_error: true,
      },
    ]
    sendMessage.mockResolvedValue()
    await flushPromises()

    const retryBtn = wrapper.findAll('.retry-btn').find(b => b.text() === 'Retry')
    expect(retryBtn).toBeTruthy()
    await retryBtn!.trigger('click')
    await flushPromises()

    expect(sendMessage).toHaveBeenCalledTimes(1)
    const [, text, prepared] = sendMessage.mock.calls[0]
    expect(text).toBe('Check the video')
    // The prior bubble's images must travel with the retry, and the
    // composer bucket must not be drained (no prepared message would
    // touch pendingImages, but verify imageRefs on the payload).
    expect(prepared).toBeDefined()
    expect(prepared.imageRefs).toEqual(['web_5a4d7145.png'])

    // The live composer stays untouched — a retry is not a fresh send.
    expect(store.pendingImages).toEqual([])
    wrapper.unmount()
  })

  it('resends text only when the prior turn had no images', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.messages['chat-1'] = [
      {
        role: 'user',
        content: 'Just text this time',
        timestamp: '2026-08-08T15:51:00Z',
      },
      {
        role: 'assistant',
        content: 'Error: connection refused',
        timestamp: '2026-08-08T15:51:01Z',
        is_error: true,
      },
    ]
    sendMessage.mockResolvedValue()
    await flushPromises()

    const retryBtn = wrapper.findAll('.retry-btn').find(b => b.text() === 'Retry')
    expect(retryBtn).toBeTruthy()
    await retryBtn!.trigger('click')
    await flushPromises()

    expect(sendMessage).toHaveBeenCalledTimes(1)
    const [, text, prepared] = sendMessage.mock.calls[0]
    expect(text).toBe('Just text this time')
    expect(prepared.imageRefs).toBeUndefined()
    wrapper.unmount()
  })

  it('passes the prior text (not the whole bubble object) to fixError', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.messages['chat-1'] = [
      {
        role: 'user',
        content: 'Check the video',
        timestamp: '2026-08-08T15:51:00Z',
        images: ['web_5a4d7145.png'],
      },
      {
        role: 'assistant',
        content: 'Error: this model does not support image input',
        timestamp: '2026-08-08T15:51:01Z',
        is_error: true,
      },
    ]
    sendMessage.mockResolvedValue()
    // Spy after first call resolves so we can isolate fixError behavior.
    const fixError = vi.spyOn(store, 'fixError').mockResolvedValue(undefined as never)
    await flushPromises()

    const fixBtn = wrapper.findAll('.retry-btn').find(b => b.text() === 'Fix this error')
    expect(fixBtn).toBeTruthy()
    await fixBtn!.trigger('click')
    await flushPromises()

    expect(fixError).toHaveBeenCalledTimes(1)
    const args = fixError.mock.calls[0][0] as { errorText: string; context?: string }
    expect(args.errorText).toContain('this model does not support image input')
    // context must be a string, not the {text, images} object — the prior
    // shape change in lastUserBefore would silently break fixError.
    expect(typeof args.context).toBe('string')
    expect(args.context).toBe('Check the video')
    wrapper.unmount()
  })

  it('suppresses scroll anchoring only while pinned at the bottom', async () => {
    const { wrapper } = await mountPanel()
    const messages = wrapper.find('.messages').element as HTMLElement

    expect(messages.style.overflowAnchor).toBe('none')

    Object.defineProperty(messages, 'scrollHeight', { configurable: true, value: 200 })
    Object.defineProperty(messages, 'clientHeight', { configurable: true, value: 100 })
    messages.scrollTop = 0
    await wrapper.find('.messages').trigger('scroll')
    expect(messages.style.overflowAnchor).toBe('auto')

    messages.scrollTop = 100
    await wrapper.find('.messages').trigger('scroll')
    expect(messages.style.overflowAnchor).toBe('none')
    wrapper.unmount()
  })

  it('renders a re-entry summary as a tagged assistant bubble', async () => {
    localStorage.setItem('ciao-reentry-summary-enabled', 'true')
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Earlier prompt', timestamp: '2026-08-08T15:50:00Z' },
      { role: 'assistant', content: 'Earlier answer', timestamp: '2026-08-08T15:51:00Z' },
    ]
    store.reentrySummaries = {
      'chat-1': '**Resume here**\n\n- Finish the handoff',
    }
    await flushPromises()

    const summary = wrapper.find('.reentry-summary-message')
    expect(summary.exists()).toBe(true)
    expect(summary.classes()).toContain('assistant')
    expect(summary.attributes('role')).toBe('status')
    expect(summary.attributes('aria-label')).toBe('Apple Intelligence summary')
    expect(summary.find('.reentry-summary-badge').text()).toBe('Summary')
    expect(summary.find('.reentry-summary-source').text()).toBe('Apple Intelligence')
    expect(summary.find('.message-content strong').text()).toBe('Resume here')
    expect(wrapper.findAll('.message-wrap').at(-1)?.classes()).toContain('reentry-summary-wrap')
    wrapper.unmount()
  })
})
