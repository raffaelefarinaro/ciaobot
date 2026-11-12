// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import { toChatMessage, type ServerRow } from '../../lib/chatHistory'
import { formatTime } from '../../lib/time'
import type { ChatInfo, ChatMessage, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatTurnActivity from '../ChatTurnActivity.vue'
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
      props: { chatId: 'chat-1' },
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
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
  // jsdom implements neither Element.scrollTo nor any layout, so selecting a
  // message (which scrolls the transcript when the new footer would fall below
  // it) throws an unhandled rejection out of the component's nextTick. Stub it,
  // as ChatPanelWorkbench.test.ts does for the same reason.
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

    // Only the closing reply is a bubble; the earlier text is in the trace (#1395).
    const bubbles = wrapper.findAll('.message-wrap.assistant')
    expect(bubbles.length).toBe(1)
    expect(bubbles[0].text()).toContain('The language allocation is already correct.')
    expect(bubbles[0].text()).not.toContain('Here is the corrected language allocation')

    const footer = bubbles[0].find('.message-meta')
    expect(footer.exists()).toBe(true)
    // Every fact the turn produced, on the bubble that closes it — except the
    // context-window occupancy, which the Chat details rail states as a meter
    // against the window's size. The footer reports cost, not occupancy.
    expect(footer.text()).toContain('openai/gpt-5.6-luna')
    expect(footer.text()).toContain('23s')
    expect(footer.html()).toContain('10,007')
    expect(footer.text()).not.toContain('13.2%')
  })

  it('shows one footer when a system notice splits the turn', async () => {
    const { wrapper, store } = await mountPanel()
    // A non-trace system row flushes the turn mid-way, so the turn is built in
    // two passes. Each pass used to attach a footer from its own half of the
    // buffer, which put two footers on one turn — the duplication the single
    // footer rule exists to prevent.
    store.messages['chat-1'] = [
      { role: 'user', content: 'rewrite the speech', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
      {
        role: 'assistant',
        content: 'Working on it.',
        timestamp: '2026-08-30T13:23:10Z',
        effective_model: 'openai/gpt-5.6-luna',
        usage: { input_tokens: '10007', output_tokens: '92' },
      },
      { role: 'system', content: 'Connection restored.', timestamp: '2026-08-30T13:23:12Z' },
      {
        role: 'assistant',
        content: 'The language allocation is already correct.',
        timestamp: '2026-08-30T13:23:23Z',
        duration_ms: 23000,
      },
    ]
    await flushPromises()

    const footers = wrapper.findAll('.message-wrap.assistant .message-meta')
    expect(footers.length).toBe(1)
    const bubbles = wrapper.findAll('.message-wrap.assistant')
    // On the bubble that closes the turn, carrying both halves' facts.
    expect(bubbles[0].find('.message-meta').exists()).toBe(false)
    expect(bubbles[bubbles.length - 1].find('.message-meta').text()).toContain('openai/gpt-5.6-luna')
    expect(bubbles[bubbles.length - 1].find('.message-meta').text()).toContain('23s')
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

    const bubbles = wrapper.findAll('.message-wrap.assistant')
    expect(bubbles.length).toBe(1)
    expect(bubbles[0].find('.message-meta').html()).toContain('sonnet')
  })

  it('puts a user message time in its action row', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'hi', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
    ]
    await flushPromises()

    const bubble = wrapper.get('.message-wrap.user')
    expect(bubble.find('.message.user .message-meta').exists()).toBe(false)
    expect(bubble.find('.message-actions .message-meta').exists()).toBe(true)
  })

  it.each([
    { label: 'absent', phase: undefined },
    { label: 'present', phase: 'final_answer' as const },
  ])(
    'shows settled continuation replies in order with one closing footer and Outputs (second phase $label)',
    async ({ phase }) => {
      const { wrapper, store } = await mountPanel()
      const first = "I've built it, and you can look at it now."
      const second = 'Nothing in the code changed since my last message.'
      const rows: ServerRow[] = [
        { role: 'user', content: 'Build the invented preview.', turn_index: 0 },
        {
          role: 'assistant',
          content: first,
          phase: 'final_answer',
          effective_model: 'claude-opus-5',
          usage: { input_tokens: '10007', output_tokens: '92' },
        },
        { role: 'assistant', content: 'Now let me finish the housekeeping.' },
        { role: 'system', content: '$ Bash git status', tool_name: '_activity' },
        {
          role: 'system',
          content: 'Workspace/preview.html',
          tool_name: '_filecard',
          file_path: 'Workspace/preview.html',
          action: 'created',
        },
        {
          role: 'assistant',
          content: second,
          sent_at: '2026-11-09T13:31:41Z',
          duration_ms: 23000,
          ...(phase ? { phase } : {}),
        },
      ]
      store.messages['chat-1'] = rows.map(toChatMessage)
      await flushPromises()

      // Both completed replies are bubbles, in order; the closing one keeps
      // the completion time the server mapped.
      const bubbles = wrapper.findAll('.message-wrap.assistant')
      expect(bubbles.length).toBe(2)
      expect(bubbles[0].text()).toContain(first)
      expect(bubbles[1].text()).toContain(second)
      expect(store.messages['chat-1'][5].timestamp).toBe('2026-11-09T13:31:41Z')

      // One footer, on the bubble that closes the turn: model and usage from
      // the earlier reply, completion time and duration from the closing one.
      expect(bubbles[0].find('.message-meta').exists()).toBe(false)
      const footer = bubbles[1].find('.message-meta')
      expect(footer.exists()).toBe(true)
      expect(footer.text()).toContain('claude-opus-5')
      expect(footer.text()).toContain('23s')
      expect(footer.text()).toContain(formatTime('2026-11-09T13:31:41Z'))
      expect(footer.html()).toContain('10,007')

      // One Outputs disclosure, only on the closing bubble, one file once.
      const outputs = wrapper.findAll('.outputs-summary')
      expect(outputs.length).toBe(1)
      expect(bubbles[0].find('.outputs-summary').exists()).toBe(false)
      expect(bubbles[1].find('.outputs-summary').exists()).toBe(true)
      await outputs[0].trigger('click')
      const listed = wrapper.findAll('.outputs-row .outputs-name')
      expect(listed.map(n => n.text())).toEqual(['preview.html'])

      // Neither reply sits in the Activity trace.
      const traces = wrapper.findAllComponents(ChatTurnActivity)
      expect(traces.length).toBeGreaterThan(0)
      const steps = traces.flatMap(t => (t.props('steps') ?? []) as ChatMessage[])
      const contents = steps.map(s => s.content)
      expect(contents).not.toContain(first)
      expect(contents).not.toContain(second)
      expect(contents).toContain('$ Bash git status')
    },
  )
})
