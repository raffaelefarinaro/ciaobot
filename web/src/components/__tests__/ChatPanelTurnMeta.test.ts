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
        VoiceRecorder: ChildStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
      },
    },
  })
  await flushPromises()
  return { wrapper, store }
}

describe('ChatPanel turn footer placement', () => {
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

  it('shows the turn footer once, on the last assistant bubble', async () => {
    const { wrapper, store } = await mountPanel()
    // The reported shape: a turn whose model + token usage rides on a bubble
    // that is not the one the reader ends on, while the completion time and
    // duration were overlaid onto the turn's last assistant row.
    store.messages['chat-1'] = [
      { role: 'user', content: 'rewrite the speech', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
      {
        role: 'assistant',
        content: 'Here is the corrected language allocation: my family Italian, Ipek\u2019s family Italian, friends English.',
        timestamp: '2026-08-30T13:23:10Z',
        effective_model: 'openai/gpt-5.6-luna',
        usage: { input_tokens: '10007', output_tokens: '92', context_pct: '13.2%' },
      },
      { role: 'system', content: '· read wedding-speech.md', timestamp: '2026-08-30T13:23:12Z', tool_name: '_activity' },
      {
        role: 'assistant',
        content: 'The language allocation is already correct.',
        timestamp: '2026-08-30T13:23:23Z',
        duration_ms: 23000,
      },
    ]
    await flushPromises()

    const bubbles = wrapper.findAll('.message.assistant')
    expect(bubbles.length).toBe(2)
    expect(bubbles[0].find('.message-meta').exists()).toBe(false)

    const footer = bubbles[1].find('.message-meta')
    expect(footer.exists()).toBe(true)
    // Every fact the turn produced, on the bubble that closes it.
    expect(footer.text()).toContain('openai/gpt-5.6-luna')
    expect(footer.text()).toContain('23s')
    expect(footer.html()).toContain('10,007')
    expect(footer.html()).toContain('13.2%')
  })

  it('puts the footer on a single-bubble turn too', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'hi', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
      {
        role: 'assistant',
        content: 'hello',
        timestamp: '2026-08-30T13:23:02Z',
        effective_model: 'sonnet',
        usage: { input_tokens: '10', output_tokens: '3' },
      },
    ]
    await flushPromises()

    const bubbles = wrapper.findAll('.message.assistant')
    expect(bubbles.length).toBe(1)
    expect(bubbles[0].find('.message-meta').html()).toContain('sonnet')
  })
})
