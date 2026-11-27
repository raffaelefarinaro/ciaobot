// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import { formatTime } from '../../lib/time'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'
import PermissionModeMenu from '../PermissionModeMenu.vue'
import type { BridgeMode } from '../../lib/permissionModes'

// The handover confirmation is a promise, not a dialog: the test answers it.
const askConfirm = vi.hoisted(() => vi.fn())
vi.mock('../../lib/confirm', () => ({ askConfirm }))

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

// The real ModelSelector is a Reka popover. The panel only needs its two
// slots (the recovery heading and the thinking chips) and the select event,
// so the stub renders the slots and answers with `select`.
const ModelSelectorStub = defineComponent({
  name: 'ModelSelectorStub',
  props: { labelledBy: { type: String, default: '' }, describedBy: { type: String, default: '' } },
  emits: ['select', 'close', 'update:modelValue'],
  setup(props, { slots }) {
    return () => h('div', {
      class: 'model-selector-stub',
      'aria-labelledby': props.labelledBy || undefined,
      'aria-describedby': props.describedBy || undefined,
    }, [
      // Mirror the real child: an absent header slot renders no band at all.
      slots.header ? h('div', { class: 'model-selector__header' }, slots.header()) : null,
      slots.footer?.(),
    ])
  },
})

const MODELS_RESPONSE = {
  models: ['haiku', 'sonnet', 'opus', 'fable'],
  default: 'sonnet',
  providers: [
    { id: 'claude', label: 'Anthropic (via Claude Code)', short_label: 'Claude Code', capabilities: {} },
    { id: 'opencode', label: 'OpenCode', short_label: 'OpenCode', capabilities: {} },
    { id: 'codex', label: 'OpenAI (via Codex)', short_label: 'Codex', capabilities: {} },
  ],
  provider_models: {
    claude: ['haiku', 'sonnet', 'opus', 'fable'],
    opencode: ['openai/gpt-5.6'],
    codex: ['gpt-5.5'],
  },
  provider_defaults: { claude: 'sonnet' },
  thinking_levels: { claude: ['low', 'medium', 'high'] },
  // opus cannot do 'high', fable can: the recovery validates a staged
  // thinking level against the model the user picks. haiku does 'high' and
  // nothing else.
  model_reasoning_levels: {
    sonnet: ['low', 'medium', 'high'],
    opus: ['low', 'medium'],
    fable: ['low', 'medium', 'high'],
    haiku: ['high'],
  },
  model_options: {},
  backends: { anthropic: true },
  opencode_models: ['openai/gpt-5.6'],
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

function makeChat(chatId: string, overrides: Partial<ChatInfo> = {}): ChatInfo {
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
    ...overrides,
  }
}

class MemoryStorage {
  private values = new Map<string, string>()
  get length(): number { return this.values.size }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null }
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

interface MountOptions {
  /** Overrides for the panel's chat (a locked bucket, an archived chat, ...). */
  chat?: Partial<ChatInfo>
  chatId?: string
  pinia?: ReturnType<typeof createPinia>
  /** A model catalog other than MODELS_RESPONSE. */
  models?: unknown
  /** Mount into the document, for focus assertions. */
  attach?: boolean
}

async function mountPanel(options: MountOptions = {}): Promise<{
  wrapper: VueWrapper
  store: ReturnType<typeof useProjectStore>
  sendMessage: ReturnType<typeof vi.spyOn>
  pinia: ReturnType<typeof createPinia>
}> {
  const pinia = options.pinia || createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  const chatId = options.chatId || 'chat-1'
  if (!options.pinia) {
    store.projects = [makeProject()]
    store.chats = [makeChat(chatId, options.chat)]
    store.activeChatId = chatId
    store.messages = { [chatId]: [] }
    store.bootstrapped = true
  }

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(options.models ?? MODELS_RESPONSE) as never
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })

  const sendMessage = vi.spyOn(store, 'sendMessage')
  const wrapper = shallowMount(ChatPanel, {
      props: { chatId },
    ...(options.attach ? { attachTo: document.body } : {}),
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ModelSelectorStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
        // The real notice card, so a test can read what the reader sees: the
        // stub would only carry the props.
        InjectedPromptCard: false,
        ChatTurnActivity: false,
        ChatRetryState: false,
      },
    },
  })
  await flushPromises()
  return { wrapper, store, sendMessage, pinia }
}

function verifyCurrentFailure(store: ReturnType<typeof useProjectStore>) {
  let count = 0
  const rows = store.messages['chat-1'].map((row, i) => ({
    ...row, i, ...(row.role === 'user' ? { turn_index: count++ } : {}),
  }))
  Object.assign(store.chats[0], { failure_id: 'failure-1', recovery_eligible: true, user_turn_count: count })
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path.endsWith('/messages?limit=50')) return Promise.resolve({ items: rows }) as never
    if (path === '/api/chats/chat-1') return Promise.resolve({ ...store.chats[0] }) as never
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    return Promise.resolve([]) as never
  })
  store.messages['chat-1'] = rows
  return vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
}

describe('ChatPanel retry from error bubble', () => {
  beforeEach(() => {
    Object.defineProperty(navigator, 'locks', { configurable: true, value: {
      request: async (_name: string, _options: unknown, callback: (lock: object) => Promise<unknown>) => callback({}),
    } })
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

  it('disables continuation actions until the displayed failure is verified, without blocking stop', async () => {
    const { wrapper, store } = await mountPanel()
    store.chats[0].retry = { status: 'pending', mode: 'continue_recovery', next_at: '', last_error: 'manual', attempts: 0, interval_seconds: 3600 }
    store.messages['chat-1'] = []
    await flushPromises()
    const card = wrapper.get('.injected-card--retry_scheduled')
    const buttons = card.findAll('.retry-state-actions button')
    expect(buttons[0].attributes('disabled')).toBeDefined()
    expect(buttons[1].attributes('disabled')).toBeDefined()
    expect(buttons[2].attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('uses the verified failure identity without resending the task or images', async () => {
    const failed = makeChat('chat-1', { failure_id: 'failure-1', recovery_eligible: true, user_turn_count: 1 })
    const { wrapper, store, sendMessage } = await mountPanel({ chat: failed })
    const rows = [
      { role: 'user', content: 'private task', images: ['private-image'], turn_index: 0, i: 0 },
      { role: 'assistant', content: 'usage limit', is_error: true, i: 1 },
    ]
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path.endsWith('/messages?limit=50')) return Promise.resolve({ items: rows }) as never
      if (path === '/api/chats/chat-1') return Promise.resolve(failed) as never
      return Promise.resolve([]) as never
    })
    const recover = vi.spyOn(store, 'continueRecovery').mockResolvedValue(failed)
    store.messages['chat-1'] = rows as never
    await flushPromises()
    const retry = wrapper.findAll('button').find(button => button.text() === 'Retry')!
    expect(retry.attributes('disabled')).toBeUndefined()
    await retry.trigger('click')
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', true)
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('verifies a failure when the last user turn is more than 50 rows back', async () => {
    const failed = makeChat('chat-1', { failure_id: 'failure-1', recovery_eligible: true, user_turn_count: 1 })
    const { wrapper, store } = await mountPanel({ chat: failed })
    const user = { role: 'user', content: 'task', turn_index: 0, i: 0 }
    const error = { role: 'assistant', content: 'usage limit', is_error: true, i: 101 }
    const trace = Array.from({ length: 100 }, (_, i) => ({ role: 'assistant', content: 'tool output', tool_name: 'tool', i: i + 1 }))
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path.endsWith('/messages?limit=50')) return Promise.resolve({ items: [...trace.slice(51), error], total: 102, hasMore: true, nextOffset: 50 }) as never
      if (path.endsWith('/messages?offset=50&limit=50')) return Promise.resolve({ items: trace.slice(1, 51), total: 102, hasMore: true, nextOffset: 100 }) as never
      if (path.endsWith('/messages?offset=100&limit=50')) return Promise.resolve({ items: [user, trace[0]], total: 102, hasMore: false, nextOffset: null }) as never
      if (path === '/api/chats/chat-1') return Promise.resolve(failed) as never
      return Promise.resolve([]) as never
    })
    store.messages['chat-1'] = [user, error] as never
    await flushPromises()
    expect(wrapper.findAll('button').find(button => button.text() === 'Retry')!.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('preserves the draft and staged attachments during verified recovery', async () => {
    const failed = makeChat('chat-1', { failure_id: 'failure-1', recovery_eligible: true, user_turn_count: 1 })
    const { wrapper, store, sendMessage } = await mountPanel({ chat: failed })
    const rows = [
      { role: 'user', content: 'original task', images: ['original.png'], turn_index: 0, i: 0 },
      { role: 'assistant', content: 'Error: connection refused', is_error: true, i: 1 },
    ]
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path.endsWith('/messages?limit=50')) return Promise.resolve({ items: rows }) as never
      if (path === '/api/chats/chat-1') return Promise.resolve(failed) as never
      return Promise.resolve([]) as never
    })
    const recover = vi.spyOn(store, 'continueRecovery').mockResolvedValue(failed)
    store.messages['chat-1'] = rows as never
    store.pendingImages = ['draft.png']
    await wrapper.get('textarea.chat-input').setValue('unfinished draft')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === 'Retry')!.trigger('click')
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', true)
    expect(sendMessage).not.toHaveBeenCalled()
    expect((wrapper.get('textarea.chat-input').element as HTMLTextAreaElement).value).toBe('unfinished draft')
    expect(store.pendingImages).toEqual(['draft.png'])
    wrapper.unmount()
  })

  it('keeps an older displayed error disabled when the authoritative identity changes', async () => {
    const old = makeChat('chat-1', { failure_id: 'old', recovery_eligible: true, user_turn_count: 1 })
    const { wrapper, store, sendMessage } = await mountPanel({ chat: old })
    const rows = [
      { role: 'user', content: 'task', turn_index: 0, i: 0 },
      { role: 'assistant', content: 'usage limit', is_error: true, i: 1 },
    ]
    let chatReads = 0
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path.endsWith('/messages?limit=50')) return Promise.resolve({ items: rows }) as never
      if (path === '/api/chats/chat-1') return Promise.resolve(++chatReads === 1 ? old : { ...old, failure_id: 'new' }) as never
      return Promise.resolve([]) as never
    })
    store.messages['chat-1'] = rows as never
    await flushPromises()
    const retry = wrapper.findAll('button').find(button => button.text() === 'Retry')!
    expect(retry.attributes('disabled')).toBeDefined()
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('shows the actual saved schedule and Stop after a recovery conflict', async () => {
    const failed = makeChat('chat-1', { failure_id: 'failure-1', recovery_eligible: true, user_turn_count: 1 })
    const { wrapper, store } = await mountPanel({ chat: failed })
    const rows = [
      { role: 'user', content: 'task', turn_index: 0, i: 0 },
      { role: 'assistant', content: 'you have reached your weekly usage limit', is_error: true, i: 1 },
    ]
    let conflict = false
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path.endsWith('/messages?limit=50')) return Promise.resolve({ items: rows }) as never
      if (path === '/api/chats/chat-1') return Promise.resolve(conflict ? {
        ...failed, retry: { status: 'pending', mode: 'literal', last_error: 'manual', next_at: '', attempts: 0, interval_seconds: 3600 },
      } : failed) as never
      return Promise.resolve([]) as never
    })
    const post = vi.spyOn(api, 'post').mockImplementation(async (_path, body) => {
      if ((body as { action: string }).action === 'issue_recovery_request') return { request_id: 'issued-id', expires: Date.now() / 1000 + 900 } as never
      conflict = true
      return { request_id: 'issued-id', status: 'completed', result: { http_status: 409 } } as never
    })
    store.messages['chat-1'] = rows as never
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === 'Retry every hour')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Stop trying')
    expect(wrapper.text()).toContain('Run scheduled prompt')
    expect(post).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('does not let an older history read enable recovery after a newer failure', async () => {
    const old = makeChat('chat-1', { failure_id: 'old', recovery_eligible: true, user_turn_count: 1 })
    const { wrapper, store } = await mountPanel({ chat: old })
    const rows = [
      { role: 'user', content: 'task', turn_index: 0, i: 0 },
      { role: 'assistant', content: 'usage limit', is_error: true, i: 1 },
    ]
    let resolveHistory!: (value: unknown) => void
    const oldHistory = new Promise(resolve => { resolveHistory = resolve })
    let calls = 0
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path.endsWith('/messages?limit=50')) return (++calls === 1 ? oldHistory : Promise.resolve({ items: [] })) as never
      if (path === '/api/chats/chat-1') return Promise.resolve(old) as never
      return Promise.resolve([]) as never
    })
    store.messages['chat-1'] = rows as never
    await flushPromises()
    store.chats[0].failure_id = 'new'
    await flushPromises()
    resolveHistory({ items: rows })
    await flushPromises()
    const retry = wrapper.findAll('button').find(button => button.text() === 'Retry')!
    expect(retry.attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it.each(['manual', 'mcp', '', '  '])('shows a neutral standalone retry for %j', async reason => {
    const { wrapper, store } = await mountPanel({ chat: { last_error: 'unrelated newer failure' } })
    store.chats[0].retry = { status: 'pending', next_at: '2026-10-12T11:00:00Z', last_error: reason, attempts: 0, interval_seconds: 3600 }
    await flushPromises()
    const card = wrapper.get('.injected-card--retry_scheduled')
    expect(card.attributes('aria-label')).toBe('Retry scheduled')
    expect(card.text()).not.toMatch(/Unknown error|manual|mcp|unrelated newer failure/)
    expect(card.get('.retry-state-title').text()).toBe('Retrying this turn every hour')
    expect(card.findAll('.retry-state-actions .btn-small').map(b => b.text())).toEqual(['Run scheduled prompt', 'Stop trying'])
    await card.get('.injected-card-toggle').trigger('click')
    expect(card.text()).toContain('A retry is scheduled.')
    expect(card.text()).not.toMatch(/Unknown error|manual|mcp|unrelated newer failure/)
    wrapper.unmount()
  })

  it('keeps a standalone provider failure classified and visible', async () => {
    const { wrapper, store } = await mountPanel()
    const error = 'you have reached your weekly usage limit, upgrade for higher limits'
    store.chats[0].retry = { status: 'pending', next_at: '2026-10-12T11:00:00Z', last_error: error, attempts: 1, interval_seconds: 3600 }
    await flushPromises()
    const card = wrapper.get('.injected-card--error')
    expect(card.text()).toContain('Usage limit reached')
    expect(card.text()).toContain(error)
    wrapper.unmount()
  })

  it.each(['manual', 'mcp', '', ' \n '])('keeps an unrelated final error separate from a %j retry', async reason => {
    const { wrapper, store } = await mountPanel({ chat: { last_error: 'unrelated newer failure' } })
    store.messages['chat-1'] = [
      { role: 'user', content: 'Hello', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'assistant', content: 'Error: unrelated provider failure', timestamp: '2026-10-11T18:01:01Z', is_error: true },
    ]
    store.chats[0].retry = { status: 'pending', next_at: '2026-10-12T11:00:00Z', last_error: reason, attempts: 0, interval_seconds: 3600 }
    await flushPromises()
    const error = wrapper.get('.injected-card--error')
    const retry = wrapper.get('.injected-card--retry_scheduled')
    expect(error.text()).toContain('unrelated provider failure')
    expect(error.find('.retry-state-title').exists()).toBe(false)
    expect(retry.text()).toContain('Retrying this turn every hour')
    expect(retry.text()).not.toMatch(/unrelated|Unknown error|manual|mcp/)
    await retry.get('.injected-card-toggle').trigger('click')
    expect(retry.text()).toContain('A retry is scheduled.')
    wrapper.unmount()
  })

  it.each([
    ['Error: strange failure 42', 'Fix this error'],
    ['Error: this model does not support image input', 'Switch model'],
    ['Not logged in · Please run /login', 'Retry'],
    ['you have reached your weekly usage limit', 'Retry every hour'],
  ])('suppresses error resend actions while a manual retry is pending: %s', async (content, action) => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Hello', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'assistant', content, timestamp: '2026-10-11T18:01:01Z', is_error: true },
    ]
    store.chats[0].retry = { status: 'pending', next_at: '2026-10-12T11:00:00Z', last_error: 'manual', attempts: 0, interval_seconds: 3600 }
    await flushPromises()
    const error = wrapper.get('.injected-card--error')
    expect(error.findAll('button').map(b => b.text())).not.toContain('Retry')
    if (action === 'Fix this error') expect(error.text()).toContain(action)
    else expect(error.text()).not.toContain(action)
    expect(error.findComponent({ name: 'ProviderSignInHint' }).exists()).toBe(false)
    expect(wrapper.get('.injected-card--retry_scheduled').text()).toContain('Run scheduled prompt')
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('refuses stale error resend and hourly scheduling handlers when a retry is pending', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    const schedule = vi.spyOn(store, 'setChatRetry').mockResolvedValue(undefined as never)
    store.messages['chat-1'] = [
      { role: 'user', content: 'Hello', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'assistant', content: 'Error: failure', timestamp: '2026-10-11T18:01:01Z', is_error: true },
    ]
    store.chats[0].retry = { status: 'pending', next_at: '2026-10-12T11:00:00Z', last_error: 'manual', attempts: 0, interval_seconds: 3600 }
    await flushPromises()
    const panel = wrapper.vm as unknown as { retryFromError: (i: number) => void; retryHourlyFromError: (i: number) => Promise<void> }
    panel.retryFromError(1)
    await panel.retryHourlyFromError(1)
    expect(sendMessage).not.toHaveBeenCalled()
    expect(schedule).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('disables unguarded literal execution and stop', async () => {
    const { wrapper, store } = await mountPanel()
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    const stop = vi.spyOn(store, 'stopChatRetry').mockResolvedValue(undefined as never)
    store.chats[0].retry = { status: 'pending', next_at: '2026-10-12T11:00:00Z', last_error: 'manual', attempts: 0, interval_seconds: 3600 }
    askConfirm.mockResolvedValue(true)
    await flushPromises()
    const buttons = wrapper.findAll('.retry-state-actions .btn-small')
    await buttons[0].trigger('click')
    await flushPromises()
    expect(buttons[0].attributes('disabled')).toBeDefined()
    expect(buttons[1].attributes('disabled')).toBeDefined()
    await buttons[1].trigger('click')
    expect(tryNow).not.toHaveBeenCalled()
    expect(stop).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('It may still run at the scheduled time')
    wrapper.unmount()
  })

  it.each([
    ['Run scheduled prompt', 'try_now'],
    ['Stop trying', 'stop'],
  ])('does not apply %s to a schedule replaced during confirmation', async (label, action) => {
    const { wrapper, store } = await mountPanel()
    const old = { status: 'pending', mode: 'literal', schedule_id: 'old', next_at: '', last_error: 'manual', attempts: 0, interval_seconds: 3600 } as const
    const replacement = { ...old, schedule_id: 'new' }
    store.chats[0].retry = old
    let confirm!: (value: boolean) => void
    askConfirm.mockReturnValueOnce(new Promise(resolve => { confirm = resolve }))
    const post = vi.spyOn(api, 'post').mockRejectedValue(Object.assign(new Error('changed'), { status: 409 }))
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/chats/chat-1'
      ? Promise.resolve({ ...store.chats[0], retry: replacement }) as never : Promise.resolve([]) as never)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === label)!.trigger('click')
    store.chats[0].retry = replacement
    confirm(true)
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith('/api/chats/chat-1/retry', { action, schedule_id: 'old' })
    expect(store.chats[0].retry?.schedule_id).toBe('new')
    wrapper.unmount()
  })

  it('continues without resending the prior turn or its images', async () => {
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
    const recover = verifyCurrentFailure(store)
    await flushPromises()

    const retryBtn = wrapper.findAll('.retry-btn').find(b => b.text() === 'Retry')
    expect(retryBtn).toBeTruthy()
    await retryBtn!.trigger('click')
    await flushPromises()

    expect(sendMessage).not.toHaveBeenCalled()
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', true)

    // The live composer stays untouched — a retry is not a fresh send.
    expect(store.pendingImages).toEqual([])
    wrapper.unmount()
  })

  it('continues without resending a text-only prior turn', async () => {
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
    const recover = verifyCurrentFailure(store)
    await flushPromises()

    const retryBtn = wrapper.findAll('.retry-btn').find(b => b.text() === 'Retry')
    expect(retryBtn).toBeTruthy()
    await retryBtn!.trigger('click')
    await flushPromises()

    expect(sendMessage).not.toHaveBeenCalled()
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', true)
    wrapper.unmount()
  })

  it('leaves the retry origin to the server instead of resending', async () => {
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
    const recover = verifyCurrentFailure(store)
    await flushPromises()

    const retryBtn = wrapper.findAll('.retry-btn').find(b => b.text() === 'Retry')
    expect(retryBtn).toBeTruthy()
    await retryBtn!.trigger('click')
    await flushPromises()

    expect(sendMessage).not.toHaveBeenCalled()
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', true)
    wrapper.unmount()
  })

  it('leaves the composer draft and the staged attachments alone', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.messages['chat-1'] = [
      {
        role: 'user',
        content: 'Check the video',
        timestamp: '2026-08-08T15:51:00Z',
      },
      {
        role: 'assistant',
        content: 'Error: connection refused',
        timestamp: '2026-08-08T15:51:01Z',
        is_error: true,
      },
    ]
    // Something else is being written and something else is staged: a retry
    // is not a fresh send, so it must not drain either.
    store.pendingImages = ['staged.png']
    await wrapper.get('textarea.chat-input').setValue('half typed')
    const recover = verifyCurrentFailure(store)
    await flushPromises()

    const retryBtn = wrapper.findAll('.retry-btn').find(b => b.text() === 'Retry')
    expect(retryBtn).toBeTruthy()
    await retryBtn!.trigger('click')
    await flushPromises()

    expect(sendMessage).not.toHaveBeenCalled()
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', true)
    const draft = wrapper.get('textarea.chat-input').element as HTMLTextAreaElement
    expect(draft.value).toBe('half typed')
    expect(store.pendingImages).toEqual(['staged.png'])
    wrapper.unmount()
  })

  it('sends an ordinary message with no retry origin', async () => {
    const { wrapper, sendMessage } = await mountPanel()
    sendMessage.mockResolvedValue()
    await flushPromises()

    await wrapper.get('textarea.chat-input').setValue('a fresh message')
    await wrapper.get('.send-btn').trigger('click')
    await flushPromises()

    expect(sendMessage).toHaveBeenCalledTimes(1)
    const [, text, prepared] = sendMessage.mock.calls[0]
    expect(text).toBe('a fresh message')
    // No prepared message, so no tag: an ordinary send is unchanged.
    expect(prepared).toBeUndefined()
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
        content: 'Error: tool crashed while reading the video',
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
    expect(args.errorText).toContain('tool crashed while reading the video')
    // context must be a string, not the {text, images} object — the prior
    // shape change in lastUserBefore would silently break fixError.
    expect(typeof args.context).toBe('string')
    expect(args.context).toBe('Check the video')
    wrapper.unmount()
  })

  it('offers an hourly retry instead of a fix chat under a usage limit', async () => {
    const { wrapper, store } = await mountPanel()
    const setRetry = vi.spyOn(store, 'setChatRetry').mockResolvedValue(undefined as never)
    store.messages['chat-1'] = [
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
      {
        role: 'assistant',
        content: 'you have reached your weekly usage limit, upgrade for higher limits',
        timestamp: '2026-10-11T18:01:01Z',
        is_error: true,
      },
    ]
    const recover = verifyCurrentFailure(store)
    await flushPromises()

    const labels = wrapper.findAll('.retry-btn').map(b => b.text())
    expect(labels).toEqual(['Retry', 'Retry every hour'])
    await wrapper.findAll('.retry-btn')[1].trigger('click')
    await flushPromises()
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', false)
    expect(setRetry).not.toHaveBeenCalled()

    // A manual schedule has no proven relation to the error, so it gets its own card.
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-12T11:00:00Z',
      last_error: 'manual',
      attempts: 0,
      interval_seconds: 3600,
    }
    await flushPromises()
    expect(wrapper.findAll('.injected-card--error')).toHaveLength(1)
    expect(wrapper.findAll('.injected-card--retry_scheduled')).toHaveLength(1)
    expect(wrapper.find('.retry-state-title').text()).toBe('Retrying this turn every hour')
    expect(wrapper.findAll('.retry-state-actions .btn-small').map(b => b.text()))
      .toEqual(['Run scheduled prompt', 'Stop trying'])
    wrapper.unmount()
  })

  it('shows a failed retry and its pending schedule as separate cards', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T19:01:00Z', origin: 'retry' },
      {
        role: 'assistant',
        content: 'you have reached your session usage limit',
        timestamp: '2026-10-11T19:01:01Z',
        is_error: true,
      },
    ]
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-11T23:00:00Z',
      last_error: 'session usage limit',
      attempts: 1,
      interval_seconds: 3600,
    }
    await flushPromises()

    expect(wrapper.text()).not.toContain('Retry started')
    const cards = wrapper.findAll('.injected-card')
    expect(cards).toHaveLength(2)
    expect(cards[0].find('.injected-card-facts').text()).toContain('Retry failed')
    expect(cards[0].find('.retry-state-title').exists()).toBe(false)
    expect(cards[1].find('.retry-state-title').text()).toBe('Retrying this turn every hour')
    expect(wrapper.find('.retry-card').exists()).toBe(false)
    wrapper.unmount()
  })

  describe('one card for a failed turn with a pending retry', () => {
    const failed = (content: string) => [
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'assistant', content, timestamp: '2026-10-11T18:01:01Z', is_error: true },
    ]
    const pending = (last_error: string) => ({
      status: 'pending' as const, next_at: '2026-10-11T23:00:00Z', last_error, attempts: 1, interval_seconds: 3600,
    })

    it('draws only the retry card when it carries the same message, with the failure time', async () => {
      const { wrapper, store } = await mountPanel()
      store.messages['chat-1'] = failed('Error: Provider quota limit reached.') as never
      store.chats[0].retry = { ...pending('Provider quota limit reached.'), kind: 'quota' }
      await flushPromises()
      expect(wrapper.findAll('.injected-card')).toHaveLength(1)
      const card = wrapper.get('.injected-card--error')
      expect(card.text()).toContain('Provider quota limit reached.')
      expect(card.find('.retry-state-title').exists()).toBe(true)
      expect(card.get('time').attributes('datetime')).toBe('2026-10-11T18:01:01Z')
      expect(card.findAll('.retry-state-actions .btn-small').map(b => b.text())).toEqual(['Run scheduled prompt', 'Stop trying'])
      expect(wrapper.find('.error-actions').exists()).toBe(false)
      wrapper.unmount()
    })

    it('keeps Fix this error as a quiet action on the retry card for an unknown error', async () => {
      const { wrapper, store } = await mountPanel()
      store.messages['chat-1'] = failed('Error: strange failure 42') as never
      store.chats[0].retry = pending('strange failure 42')
      await flushPromises()
      expect(wrapper.findAll('.injected-card')).toHaveLength(1)
      expect(wrapper.findAll('.fix-btn')).toHaveLength(0)
      const fix = wrapper.get('.retry-state-fix')
      expect(fix.text()).toBe('Fix this error')
      const labels = wrapper.findAll('.retry-state-actions button').map(b => b.text())
      expect(labels).toEqual(['Run scheduled prompt', 'Stop trying', 'Fix this error'])
      const fixError = vi.spyOn(store, 'fixError').mockResolvedValue(undefined as never)
      await fix.trigger('click')
      expect(fixError).toHaveBeenCalledWith({ errorText: 'Error: strange failure 42', context: 'Tidy my memory' })
      wrapper.unmount()
    })

    it('offers no Fix action when the error has its own fix', async () => {
      const { wrapper, store } = await mountPanel()
      store.messages['chat-1'] = failed('Error: you have reached your weekly usage limit') as never
      store.chats[0].retry = pending('you have reached your weekly usage limit')
      await flushPromises()
      expect(wrapper.findAll('.injected-card')).toHaveLength(1)
      expect(wrapper.find('.retry-state-fix').exists()).toBe(false)
      wrapper.unmount()
    })

    it('brings the error card back when the retry is stopped or exhausted', async () => {
      const { wrapper, store } = await mountPanel()
      store.messages['chat-1'] = failed('Error: strange failure 42') as never
      store.chats[0].retry = pending('strange failure 42')
      await flushPromises()
      expect(wrapper.findAll('.injected-card')).toHaveLength(1)
      store.chats[0].retry = undefined
      await flushPromises()
      expect(wrapper.findAll('.injected-card')).toHaveLength(1)
      expect(wrapper.find('.retry-state-title').exists()).toBe(false)
      expect(wrapper.get('.injected-card--error').text()).toContain('Fix this error')
      store.chats[0].retry = pending('strange failure 42')
      await flushPromises()
      expect(wrapper.find('.retry-state-title').exists()).toBe(true)
      expect(wrapper.findAll('.injected-card')).toHaveLength(1)
      store.chats[0].retry = { ...pending('strange failure 42'), status: 'exhausted' as never }
      await flushPromises()
      expect(wrapper.find('.retry-state-title').exists()).toBe(false)
      expect(wrapper.get('.injected-card--error').text()).toContain('Fix this error')
      wrapper.unmount()
    })

    const longError = `Error: ${'The provider could not finish the request because of a very long reason. '.repeat(6)}The end.`
    it.each([
      ['a multi-line usage limit', 'Error: you have reached your weekly usage limit\nupgrade for higher limits\n\nresets Friday', 'you have reached your weekly usage limit upgrade for higher limits resets Friday'],
      ['an annotated sign-in error', 'Not logged in · Please run /login', 'Not logged in · Please run /login (provider: claude, action: sign in again)'],
      ['an error over 280 characters', longError, `${longError.replace(/\s+/g, ' ').slice(7, 280 - 3)}...`],
      ['a Markdown error', 'Error: **Rate limit** hit: see [the docs](https://example.com/limits) and `retry` later', 'Rate limit hit: see the docs and retry later'],
    ])('treats %s as the retry\'s failure when the stored reason is the engine excerpt', async (_name, content, reason) => {
      const { wrapper, store } = await mountPanel()
      store.messages['chat-1'] = failed(content) as never
      store.chats[0].retry = { ...pending(reason), mode: 'continue_recovery' }
      await flushPromises()
      expect(wrapper.findAll('.injected-card')).toHaveLength(1)
      expect(wrapper.find('.retry-state-title').exists()).toBe(true)
      wrapper.unmount()
    })

    it.each([
      ['literal', 'Error: strange failure 42', 'something else entirely'],
      ['continue_recovery', 'Error: strange failure 42', 'something else entirely'],
      ['literal', longError, 'The provider could not finish the request because of a different long reason...'],
    ])('keeps an unrelated error on its own card (%s)', async (mode, content, reason) => {
      const { wrapper, store } = await mountPanel()
      store.messages['chat-1'] = failed(content) as never
      store.chats[0].retry = { ...pending(reason), mode: mode as never }
      await flushPromises()
      expect(wrapper.findAll('.injected-card')).toHaveLength(2)
      wrapper.unmount()
    })

    it('keeps the retry-failed marker on the retry card', async () => {
      const { wrapper, store } = await mountPanel()
      store.messages['chat-1'] = [
        { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
        { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T19:01:00Z', origin: 'retry' },
        { role: 'assistant', content: 'Error: strange failure 42', timestamp: '2026-10-11T19:01:01Z', is_error: true },
      ] as never
      store.chats[0].retry = pending('strange failure 42')
      await flushPromises()
      expect(wrapper.findAll('.injected-card--error')).toHaveLength(1)
      expect(wrapper.get('.injected-card--error .injected-card-facts').text()).toContain('Retry failed')
      wrapper.unmount()
    })

    it('shows the plain error card while the retry runs and the retry card again when it stops running', async () => {
      const { wrapper, store } = await mountPanel()
      store.messages['chat-1'] = failed('Error: strange failure 42') as never
      store.chats[0].retry = pending('strange failure 42')
      await flushPromises()
      expect(wrapper.find('.retry-state-title').exists()).toBe(true)
      store.streaming['chat-1'] = true
      await flushPromises()
      // While the retry runs the panel draws what it drew before this change:
      // no retry card, and the failed row as the live turn leaves it.
      expect(wrapper.find('.retry-state-title').exists()).toBe(false)
      const whileRunning = wrapper.findAll('.injected-card').length
      store.chats[0].retry = undefined
      await flushPromises()
      expect(wrapper.findAll('.injected-card')).toHaveLength(whileRunning)
      store.chats[0].retry = pending('strange failure 42')
      store.streaming['chat-1'] = false
      await flushPromises()
      expect(wrapper.findAll('.injected-card')).toHaveLength(1)
      expect(wrapper.find('.retry-state-title').exists()).toBe(true)
      wrapper.unmount()
    })

    const stopRetryBy = (store: ReturnType<typeof useProjectStore>) => {
      askConfirm.mockResolvedValue(true)
      return vi.spyOn(store, 'stopChatRetry').mockImplementation(async (id: string) => {
        const chat = store.chats.find(c => c.chat_id === id)!
        chat.retry = undefined
        return chat
      })
    }
    const stopButton = (wrapper: VueWrapper) => wrapper.findAll('.retry-state-actions button')[1]

    it('moves focus to the returning error card when the retry is stopped', async () => {
      const { wrapper, store } = await mountPanel({ attach: true })
      stopRetryBy(store)
      store.messages['chat-1'] = failed('Error: strange failure 42') as never
      store.chats[0].retry = { ...pending('strange failure 42'), schedule_id: 's1' }
      await flushPromises()
      await stopButton(wrapper).trigger('click')
      await flushPromises()
      expect(document.activeElement?.textContent).toBe('Fix this error')
      expect(document.activeElement?.closest('.injected-card--error')).not.toBeNull()
      wrapper.unmount()
    })

    it('focuses the returning card\'s toggle when it has no enabled action', async () => {
      const { wrapper, store } = await mountPanel({ attach: true })
      stopRetryBy(store)
      store.messages['chat-1'] = failed('Error: you have reached your weekly usage limit') as never
      store.chats[0].retry = { ...pending('you have reached your weekly usage limit'), schedule_id: 's1' }
      await flushPromises()
      await stopButton(wrapper).trigger('click')
      await flushPromises()
      expect(document.activeElement?.classList.contains('injected-card-toggle')).toBe(true)
      wrapper.unmount()
    })

    it('keeps focus in the pane where Stop trying was pressed when two panes are open', async () => {
      const first = await mountPanel({ attach: true })
      const { store, pinia } = first
      stopRetryBy(store)
      store.chats.push(makeChat('chat-2', { retry: { ...pending('strange failure 42'), schedule_id: 's2' } }))
      store.messages['chat-1'] = failed('Error: strange failure 42') as never
      store.messages['chat-2'] = failed('Error: strange failure 42') as never
      // The first pane shows the same row index as a plain error with its own Fix button.
      const second = await mountPanel({ attach: true, pinia, chatId: 'chat-2' })
      await flushPromises()
      expect(document.querySelectorAll('.retry-state')).toHaveLength(1)
      expect(first.wrapper.find('.fix-btn').exists()).toBe(true)
      await stopButton(second.wrapper).trigger('click')
      await flushPromises()
      const active = document.activeElement as HTMLElement
      expect(second.wrapper.element.contains(active)).toBe(true)
      expect(first.wrapper.element.contains(active)).toBe(false)
      expect(active.textContent).toBe('Fix this error')
      first.wrapper.unmount()
      second.wrapper.unmount()
    })

    it('does not move focus when the retry clears without a confirmed stop', async () => {
      const { wrapper, store } = await mountPanel({ attach: true })
      store.messages['chat-1'] = failed('Error: strange failure 42') as never
      store.chats[0].retry = { ...pending('strange failure 42'), schedule_id: 's1' }
      await flushPromises()
      const runNow = wrapper.findAll('.retry-state-actions button')[0].element as HTMLElement
      runNow.focus()
      store.chats[0].retry = undefined
      await flushPromises()
      expect(document.activeElement).not.toBe(wrapper.get('.fix-btn').element)
      expect(wrapper.element.contains(document.activeElement)).toBe(false)
      wrapper.unmount()
    })

    it('does not move focus when the retry fires', async () => {
      const { wrapper, store } = await mountPanel({ attach: true })
      store.messages['chat-1'] = failed('Error: strange failure 42') as never
      store.chats[0].retry = { ...pending('strange failure 42'), schedule_id: 's1' }
      await flushPromises()
      const stop = stopButton(wrapper).element as HTMLElement
      stop.focus()
      expect(document.activeElement).toBe(stop)
      store.streaming['chat-1'] = true
      await flushPromises()
      expect(document.activeElement?.closest('.injected-card--error')).toBeNull()
      wrapper.unmount()
    })

    it('leaves an older failed row alone once the chat moved on', async () => {
      const { wrapper, store } = await mountPanel()
      store.messages['chat-1'] = [
        ...failed('Error: strange failure 42'),
        { role: 'user', content: 'Try again', timestamp: '2026-10-11T18:05:00Z' },
        { role: 'assistant', content: 'Done.', timestamp: '2026-10-11T18:05:30Z' },
      ] as never
      store.chats[0].retry = pending('strange failure 42')
      await flushPromises()
      expect(wrapper.findAll('.injected-card--error')).toHaveLength(2)
      expect(wrapper.get('.injected-card--error').text()).toContain('strange failure 42')
      wrapper.unmount()
    })
  })

  it.each([
    ['network unreachable', 'Error: you have reached your weekly usage limit', 'Usage limit reached'],
    ['you have reached your weekly usage limit', 'Error: connection refused', 'Unknown error'],
  ])('keeps provider reason %s separate from unrelated error %s', async (reason, error, errorLabel) => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'assistant', content: error, timestamp: '2026-10-11T18:01:01Z', is_error: true },
    ]
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-11T23:00:00Z',
      last_error: reason,
      attempts: 1,
      interval_seconds: 30,
    }
    await flushPromises()
    const cards = wrapper.findAll('.injected-card')
    expect(cards).toHaveLength(2)
    expect(cards[0].text()).toContain(errorLabel)
    expect(cards[0].text()).toContain(error.replace('Error: ', ''))
    expect(cards[0].find('.retry-state-title').exists()).toBe(false)
    expect(cards[1].text()).toContain(reason)
    expect(cards[1].find('.retry-state-title').exists()).toBe(true)
    expect(cards[1].findAll('.retry-state-actions button').map(b => b.text())).toEqual(['Run scheduled prompt', 'Stop trying'])
    wrapper.unmount()
  })

  it('keeps the Retry started row when the retry has not failed', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T19:01:00Z', origin: 'retry' },
    ]
    await flushPromises()
    expect(wrapper.text()).toContain('Retry started')
    wrapper.unmount()
  })

  it('draws an error as a collapsed notice card', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
      {
        role: 'assistant',
        content: 'you have reached your weekly usage limit, upgrade for higher limits',
        timestamp: '2026-10-11T18:01:01Z',
        is_error: true,
      },
    ]
    await flushPromises()

    const card = wrapper.get('.injected-card--error')
    expect(card.get('.injected-card-kind').text()).toBe('Usage limit reached')
    expect(card.get('.injected-card-hint').text()).toContain('weekly usage limit')
    expect(wrapper.find('.message.assistant').exists()).toBe(false)
    expect(wrapper.find('.error-attribution').exists()).toBe(false)
    expect(wrapper.find('[data-msg-role="assistant"]').exists()).toBe(true)
    expect(card.find('.injected-card-body').exists()).toBe(false)
    await card.get('.injected-card-toggle').trigger('click')
    expect(card.get('.injected-card-note').text()).toContain('usage limit')
    wrapper.unmount()
  })

  it('draws a system error as the same notice card', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Hi', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'system', content: 'Error: connection refused', timestamp: '2026-10-11T18:01:01Z' },
    ]
    await flushPromises()
    expect(wrapper.get('.injected-card--error .injected-card-hint').text()).toBe('connection refused')
    expect(wrapper.find('.message.system').exists()).toBe(false)
    wrapper.unmount()
  })

  it('hides Retry once a later message exists', async () => {
    const { wrapper, store } = await mountPanel()
    const limit = 'you have reached your weekly usage limit, upgrade for higher limits'
    store.messages['chat-1'] = [
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'assistant', content: limit, timestamp: '2026-10-11T18:01:01Z', is_error: true },
    ]
    await flushPromises()
    expect(wrapper.findAll('.retry-btn').map(b => b.text())).toEqual(['Retry', 'Retry every hour'])

    store.messages['chat-1'] = [
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
      { role: 'assistant', content: limit, timestamp: '2026-10-11T18:01:01Z', is_error: true },
      { role: 'user', content: 'continue', timestamp: '2026-10-11T18:02:00Z' },
      { role: 'assistant', content: 'ok', timestamp: '2026-10-11T18:02:01Z' },
    ]
    await flushPromises()
    expect(wrapper.findAll('.retry-btn')).toEqual([])
    expect(wrapper.text()).not.toContain('Retry every hour')
    wrapper.unmount()
  })

  it('keeps Fix this error and drops sign-in steps once a later message exists', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Hi', timestamp: '2026-10-05T15:44:00Z' },
      { role: 'assistant', content: 'Error: connection refused', timestamp: '2026-10-05T15:44:01Z', is_error: true },
      { role: 'user', content: 'again', timestamp: '2026-10-05T15:45:00Z' },
    ]
    await flushPromises()
    expect(wrapper.findAll('.retry-btn').map(b => b.text())).toEqual(['Fix this error'])

    store.messages['chat-1'] = [
      { role: 'user', content: 'Summarise my inbox', timestamp: '2026-10-05T15:44:00Z' },
      {
        role: 'assistant',
        content: 'Failed to authenticate: OAuth session expired and could not be refreshed',
        timestamp: '2026-10-05T15:44:01Z',
        is_error: true,
      },
    ]
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ProviderSignInHint' }).exists()).toBe(true)
    store.messages['chat-1'].push({ role: 'user', content: 'continue', timestamp: '2026-10-05T15:45:00Z' })
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ProviderSignInHint' }).exists()).toBe(false)
    expect(wrapper.findAll('.retry-btn')).toEqual([])
    wrapper.unmount()
  })

  it('keeps a failed turn\'s file outputs on its activity trace', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Write it', timestamp: '2026-10-11T18:01:00Z', turn_index: 0 },
      { role: 'system', tool_name: '_activity', content: 'Write notes.md', timestamp: '2026-10-11T18:01:01Z' },
      {
        role: 'system',
        tool_name: '_filecard',
        content: 'Workspace/notes.md',
        file_path: 'Workspace/notes.md',
        action: 'created',
        timestamp: '2026-10-11T18:01:02Z',
      },
      { role: 'assistant', content: 'Error: connection refused', timestamp: '2026-10-11T18:01:03Z', is_error: true },
    ]
    await flushPromises()
    expect(wrapper.find('.injected-card--error').exists()).toBe(true)
    // Outputs live in the trace body, which opens on its summary.
    await wrapper.get('.trace-summary').trigger('click')
    expect(wrapper.find('.outputs-summary').exists()).toBe(true)
    wrapper.unmount()
  })

  it('draws no empty actions row when nothing is offered', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Tidy my memory', timestamp: '2026-10-11T18:01:00Z' },
      {
        role: 'assistant',
        content: 'you have reached your weekly usage limit, upgrade for higher limits',
        timestamp: '2026-10-11T18:01:01Z',
        is_error: true,
      },
      { role: 'user', content: 'continue', timestamp: '2026-10-11T18:02:00Z' },
    ]
    await flushPromises()
    expect(wrapper.find('.injected-card--error').exists()).toBe(true)
    expect(wrapper.find('.injected-card-actions').exists()).toBe(false)
    wrapper.unmount()
  })

  it('still offers Retry when only a trace follows the error', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Hi', timestamp: '2026-10-11T18:01:00Z', turn_index: 0 },
      { role: 'assistant', content: 'Error: connection refused', timestamp: '2026-10-11T18:01:01Z', is_error: true },
      // A tool activity row after the error renders as a trace item.
      { role: 'system', content: '· read a.md', timestamp: '2026-10-11T18:01:02Z', tool_name: '_activity' },
    ]
    await flushPromises()
    expect(wrapper.findAll('.retry-btn').map(b => b.text())).toContain('Retry')
    wrapper.unmount()
  })

  it('falls back to a fix chat when there is no turn to retry hourly', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      {
        role: 'assistant',
        content: 'you have reached your weekly usage limit, upgrade for higher limits',
        timestamp: '2026-10-11T18:01:01Z',
        is_error: true,
      },
    ]
    await flushPromises()
    expect(wrapper.findAll('.retry-btn').map(b => b.text())).toEqual(['Fix this error'])
    wrapper.unmount()
  })

  it('offers a model switch instead of a fix chat when the model cannot take the turn', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Check the video', timestamp: '2026-08-08T15:51:00Z' },
      {
        role: 'assistant',
        content: 'Error: this model does not support image input',
        timestamp: '2026-08-08T15:51:01Z',
        is_error: true,
      },
    ]
    await flushPromises()
    expect(wrapper.findAll('.retry-btn').map(b => b.text())).toEqual(['Retry', 'Switch model'])
    wrapper.unmount()
  })

  it('leaves a signed-out error to the sign-in steps, without a fix chat', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Summarise my inbox', timestamp: '2026-10-05T15:44:00Z' },
      { role: 'assistant', content: 'Not logged in · Please run /login', timestamp: '2026-10-05T15:44:01Z', is_error: true },
    ]
    await flushPromises()
    expect(wrapper.findAll('.retry-btn').map(b => b.text())).toEqual(['Retry'])
    wrapper.unmount()
  })

  it('offers sign-in steps under a signed-out error and retries from them', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Summarise my inbox', timestamp: '2026-10-05T15:44:00Z' },
      {
        role: 'assistant',
        content: 'Failed to authenticate: OAuth session expired and could not be refreshed',
        timestamp: '2026-10-05T15:44:01Z',
        is_error: true,
      },
    ]
    const recover = verifyCurrentFailure(store)
    await flushPromises()

    const hint = wrapper.findComponent({ name: 'ProviderSignInHint' })
    expect(hint.exists()).toBe(true)
    expect(hint.props('provider')).toBe('claude')
    expect(hint.props('actionLabel')).toBe('Retry')
    hint.vm.$emit('action')
    await flushPromises()
    expect(sendMessage).not.toHaveBeenCalled()
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', true)
    wrapper.unmount()
  })

  it('shows no sign-in steps under an unrelated error', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: 'Hi', timestamp: '2026-10-05T15:44:00Z' },
      { role: 'assistant', content: 'Error: connection refused', timestamp: '2026-10-05T15:44:01Z', is_error: true },
    ]
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ProviderSignInHint' }).exists()).toBe(false)
    wrapper.unmount()
  })

  it('puts sign-in steps on a pending auth retry and wires them to Try now', async () => {
    const { wrapper, store } = await mountPanel()
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-05T15:45:00Z',
      last_error: 'Not logged in · Please run /login',
      attempts: 2,
      interval_seconds: 30,
    }
    await flushPromises()

    expect(wrapper.find('.retry-state-title').text()).toBe('Signed out — retrying this turn')
    const hint = wrapper.findComponent({ name: 'ProviderSignInHint' })
    expect(hint.props('actionLabel')).toBe('Try now')
    hint.vm.$emit('action')
    await flushPromises()
    expect(tryNow).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('shows the scheduled time for a weekly or monthly quota reset', async () => {
    const { wrapper, store } = await mountPanel()
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-05T16:45:00Z',
      last_error: 'weekly usage limit',
      attempts: 0,
      interval_seconds: 3600,
      kind: 'quota',
    }
    await flushPromises()
    expect(wrapper.find('.retry-state-title').text()).toBe('Retrying this turn')
    expect(wrapper.text()).toContain('Next try')
    expect(wrapper.text()).not.toContain('once a day')
    expect(wrapper.text()).not.toContain('every few hours')
    wrapper.unmount()
  })

  it('keeps the quota date after the chat hint and preserves manual hourly wording', async () => {
    const sockets: Array<{ url: string; emit(event: Record<string, unknown>): void }> = []
    class FakeRetryWebSocket {
      static CONNECTING = 0
      static OPEN = 1
      static CLOSING = 2
      static CLOSED = 3
      readyState = FakeRetryWebSocket.CONNECTING
      onopen: ((event: Event) => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      onclose: ((event: CloseEvent) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      constructor(readonly url: string) { sockets.push(this) }
      send() {}
      close() { this.readyState = FakeRetryWebSocket.CLOSED }
      emit(event: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(event) } as MessageEvent) }
    }
    vi.stubGlobal('WebSocket', FakeRetryWebSocket)

    const { wrapper, store } = await mountPanel()
    const resetDate = new Date()
    resetDate.setDate(resetDate.getDate() + 2)
    resetDate.setHours(16, 45, 0, 0)
    const nextAt = resetDate.toISOString()
    const expectedNextTry = `Next try ${formatTime(nextAt)}`

    store.connectEventsWs()
    const eventsSocket = sockets.find(socket => socket.url.endsWith('/ws/events'))!
    eventsSocket.emit({
      type: 'chat_retry', chat_id: 'chat-1', project_id: 'project-1',
      status: 'pending', next_at: nextAt, last_error: 'Provider quota limit reached.',
      attempts: 0, interval_seconds: 3600, kind: 'quota', mode: 'literal', schedule_id: 'quota-1',
    })
    await flushPromises()
    expect(wrapper.get('.retry-state-title').text()).toBe('Retrying this turn')
    expect(wrapper.get('.retry-state-meta').text()).toContain(expectedNextTry)

    store.connectWs('chat-1')
    const chatSocket = sockets.find(socket => socket.url.endsWith('/ws/chat/chat-1'))!
    chatSocket.emit({
      type: 'chat_retry', status: 'pending', next_at: nextAt,
      last_error: 'Provider quota limit reached.', attempts: 0,
      interval_seconds: 3600, kind: 'quota', mode: 'literal', schedule_id: 'quota-1',
    })
    await flushPromises()
    expect(wrapper.get('.retry-state-title').text()).toBe('Retrying this turn')
    expect(wrapper.get('.retry-state-meta').text()).toContain(expectedNextTry)

    eventsSocket.emit({
      type: 'chat_retry', chat_id: 'chat-1', project_id: 'project-1',
      status: 'pending', next_at: new Date(Date.now() + 2 * 60 * 60 * 1000).toISOString(),
      last_error: 'manual recovery', attempts: 0, interval_seconds: 3600,
      kind: '', mode: 'literal', schedule_id: 'manual-1',
    })
    await flushPromises()
    expect(wrapper.get('.retry-state-title').text()).toBe('Retrying this turn every hour')
    store.disconnectWs('chat-1')
    wrapper.unmount()
  })

  it('does not promise an hourly retry for a 30s connection retry', async () => {
    const { wrapper, store } = await mountPanel()
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-05T15:45:00Z',
      last_error: 'connection reset by peer',
      attempts: 1,
      interval_seconds: 30,
    }
    await flushPromises()
    expect(wrapper.find('.retry-state-title').text()).toBe('Retrying this turn shortly')
    expect(wrapper.findComponent({ name: 'ProviderSignInHint' }).exists()).toBe(false)
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
})

// A recovery is the model picker opened from a failed turn: the retry card's
// "Continue with..." and the error bubble's "Switch model". A pick there
// retries that turn; the composer's own picker only changes the model.
describe('ChatPanel recovery model picker', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
    askConfirm.mockReset()
    askConfirm.mockResolvedValue(true)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  function picker(wrapper: VueWrapper) {
    return wrapper.findComponent(ModelSelectorStub)
  }

  async function pickModel(wrapper: VueWrapper, model: string, section = 'anthropic') {
    picker(wrapper).vm.$emit('select', model, section)
    await flushPromises()
  }

  // The menu button itself (Reka's menu, keys, labels) is covered by
  // PermissionModeMenu.test.ts; here it is the shallow stub, and a pick is the
  // event the real one emits. A disabled button does not open, so it emits nothing.
  const MODE_BY_NAME: Record<string, BridgeMode> = { Manual: 'normal', Auto: 'auto', Bypass: 'bypass', Plan: 'plan' }

  function permissionMenu(wrapper: VueWrapper) {
    return wrapper.findComponent(PermissionModeMenu)
  }

  async function choosePermissionMode(wrapper: VueWrapper, name: string) {
    const menu = permissionMenu(wrapper)
    expect(menu.exists()).toBe(true)
    if (menu.props('disabled')) return
    menu.vm.$emit('select', MODE_BY_NAME[name])
    await flushPromises()
  }

  async function openComposerPicker(wrapper: VueWrapper) {
    const chip = wrapper.find('.model-picker-wrap.composer-model .composer-chip')
    expect(chip.exists()).toBe(true)
    await chip.trigger('click')
    await flushPromises()
  }

  async function openCardRecovery(wrapper: VueWrapper) {
    const btn = wrapper.findAll('.retry-state-actions .btn-small').find(b => b.text() === 'Continue with...')
    expect(btn).toBeTruthy()
    await btn!.trigger('click')
    await flushPromises()
  }

  async function openErrorRecovery(wrapper: VueWrapper) {
    const btn = wrapper.findAll('.retry-btn').find(b => b.text() === 'Switch model')
    expect(btn).toBeTruthy()
    await btn!.trigger('click')
    await flushPromises()
  }

  // A recovery stages its level with selectThinking, which the chips call.
  // The chips are the current model's set, so a level the picker does not
  // offer is staged the same way the picker stages the ones it does.
  function stageThinkingLevel(wrapper: VueWrapper, level: string) {
    const panel = wrapper.vm as unknown as { selectThinking: (level: string) => Promise<void> }
    return panel.selectThinking(level)
  }

  function armPendingRetry(store: ReturnType<typeof useProjectStore>) {
    failWithUnsupportedModel(store)
    store.chats[0].retry = {
      status: 'pending',
      mode: 'continue_recovery',
      next_at: '2026-10-22T10:00:00Z',
      last_error: 'weekly usage limit',
      attempts: 1,
      interval_seconds: 3600,
    }
  }

  function failWithUnsupportedModel(store: ReturnType<typeof useProjectStore>) {
    Object.assign(store.chats[0], { failure_id: 'failure-1', recovery_eligible: true, user_turn_count: 1 })
    store.messages['chat-1'] = [
      {
        role: 'user',
        i: 0,
        turn_index: 0,
        content: 'Check the video',
        timestamp: '2026-08-08T15:51:00Z',
        images: ['web_5a4d7145.png'],
      },
      {
        role: 'assistant',
        i: 1,
        content: 'Error: this model does not support image input',
        timestamp: '2026-08-08T15:51:01Z',
        is_error: true,
      },
    ]
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
      if (path.endsWith('/messages?limit=50')) return Promise.resolve({ items: store.messages['chat-1'] }) as never
      if (path === '/api/chats/chat-1') return Promise.resolve({ ...store.chats[0] }) as never
      return Promise.resolve([]) as never
    })
  }

  it('opens the recovery picker with its heading from the retry card', async () => {
    const { wrapper, store } = await mountPanel()
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)

    expect(wrapper.find('.model-picker-recovery__title').text()).toBe('Continue with the current model')
    expect(wrapper.find('.model-picker-recovery__note').text()).toContain('may incur usage charges')
    expect(wrapper.find('.model-picker-recovery__note').text()).toContain(
      'Model and Thinking changes are unavailable during recovery.',
    )
    // The listbox is named by the heading and the search field describes
    // itself with the consequence, so the warning is not visual-only.
    const stub = wrapper.getComponent({ name: 'ModelSelectorStub' })
    expect(stub.attributes('aria-labelledby')).toBe('model-picker-recovery-title')
    expect(stub.attributes('aria-describedby')).toBe('model-picker-recovery-note')
    wrapper.unmount()
  })

  it('does not apply an error recovery model or resend if a retry becomes pending while the picker is open', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    const update = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const handover = vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    failWithUnsupportedModel(store)
    await flushPromises()
    await openErrorRecovery(wrapper)
    store.chats[0].retry = { status: 'pending', next_at: '2026-10-12T11:00:00Z', last_error: 'manual', attempts: 0, interval_seconds: 3600 }
    await flushPromises()
    await pickModel(wrapper, 'opus')
    expect(update).not.toHaveBeenCalled()
    expect(handover).not.toHaveBeenCalled()
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('opens the same recovery picker from the error bubble', async () => {
    const { wrapper, store } = await mountPanel()
    failWithUnsupportedModel(store)
    await flushPromises()

    await openErrorRecovery(wrapper)

    expect(wrapper.find('.model-picker-recovery__title').text()).toBe('Continue with the current model')
    expect(wrapper.find('.model-picker-recovery__note').text()).toContain('may incur usage charges')
    wrapper.unmount()
  })

  it('keeps the composer picker free of the recovery heading', async () => {
    const { wrapper } = await mountPanel()

    await openComposerPicker(wrapper)

    expect(wrapper.find('.model-picker-recovery').exists()).toBe(false)
    // No empty header band above the search field either.
    expect(wrapper.find('.model-selector__header').exists()).toBe(false)
    // And nothing points at the recovery wording that is not there.
    const stub = wrapper.getComponent({ name: 'ModelSelectorStub' })
    expect(stub.attributes('aria-labelledby')).toBeUndefined()
    expect(stub.attributes('aria-describedby')).toBeUndefined()
    wrapper.unmount()
  })

  it('does not change model or retry when a different model is picked during recovery', async () => {
    const { wrapper, store } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'opus')

    expect(updateChat).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('retries in a recovery even when the picked model is the one already in use', async () => {
    const { wrapper, store } = await mountPanel()
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'sonnet')

    expect(tryNow).toHaveBeenCalledTimes(1)
    expect(tryNow).toHaveBeenCalledWith('chat-1', 'failure-1', true)
    wrapper.unmount()
  })

  it('never retries from the composer picker, even with a deferred retry waiting', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openComposerPicker(wrapper)
    await pickModel(wrapper, 'opus')

    expect(updateChat).toHaveBeenCalledTimes(1)
    expect(tryNow).not.toHaveBeenCalled()
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does nothing when the composer picker picks the model already in use', async () => {
    const { wrapper, store } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openComposerPicker(wrapper)
    await pickModel(wrapper, 'sonnet')

    expect(updateChat).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('continues without replaying the failed turn when a model is picked', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const recover = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    failWithUnsupportedModel(store)
    sendMessage.mockResolvedValue()
    await flushPromises()

    await openErrorRecovery(wrapper)
    await pickModel(wrapper, 'sonnet')

    expect(sendMessage).not.toHaveBeenCalled()
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', true)
    // The composer is not drained by a recovery.
    expect(store.pendingImages).toEqual([])
    wrapper.unmount()
  })

  it('does not mutate or recover when the displayed failure changes before selection', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    const update = vi.spyOn(store, 'updateChat')
    const recover = vi.spyOn(store, 'continueRecovery').mockRejectedValue(new Error('stale'))
    failWithUnsupportedModel(store)
    await flushPromises()
    await openErrorRecovery(wrapper)
    store.chats[0].failure_id = 'failure-2'
    picker(wrapper).vm.$emit('select', 'sonnet', 'anthropic')
    await flushPromises()
    expect(update).not.toHaveBeenCalled()
    expect(recover).not.toHaveBeenCalled()
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not change model or recover if another client advances the failure while the picker is open', async () => {
    const { wrapper, store } = await mountPanel()
    const update = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const handover = vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    const recover = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    failWithUnsupportedModel(store)
    await flushPromises()
    await openErrorRecovery(wrapper)
    vi.mocked(api.get).mockImplementation((path: string) => path === '/api/chats/chat-1'
      ? Promise.resolve({ ...store.chats[0], failure_id: 'newer-failure' }) as never : Promise.resolve([]) as never)
    await pickModel(wrapper, 'opus')
    expect(update).not.toHaveBeenCalled()
    expect(handover).not.toHaveBeenCalled()
    expect(recover).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('rejects cross-provider recovery before handover', async () => {
    const { wrapper, store } = await mountPanel({ chat: { session_id: 'sess-1' } })
    const handover = vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    expect(askConfirm).not.toHaveBeenCalled()
    expect(handover).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not apply a staged thinking level or hand over for cross-provider recovery', async () => {
    const { wrapper, store } = await mountPanel({ chat: { session_id: 'sess-1' } })
    const handover = vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await wrapper.findAll('.thinking-chip').find(c => c.text() === 'High')!.trigger('click')
    await flushPromises()
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    expect(handover).not.toHaveBeenCalled()
    expect(updateChat).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not retry after a handover when the staged level cannot be saved', async () => {
    const { wrapper, store } = await mountPanel({ chat: { session_id: 'sess-1' } })
    vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    vi.spyOn(store, 'updateChat').mockRejectedValue(new Error('engine refused'))
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    const toast = vi.spyOn(store, 'pushErrorToast')
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await wrapper.findAll('.thinking-chip').find(c => c.text() === 'High')!.trigger('click')
    await flushPromises()
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    expect(toast).toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('changes no model and sends nothing when the handover is cancelled', async () => {
    const { wrapper, store, sendMessage } = await mountPanel({ chat: { session_id: 'sess-1' } })
    const handover = vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    askConfirm.mockResolvedValue(false)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    expect(askConfirm).not.toHaveBeenCalled()
    expect(handover).not.toHaveBeenCalled()
    expect(updateChat).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('sends nothing when the handover fails', async () => {
    const { wrapper, store, sendMessage } = await mountPanel({ chat: { session_id: 'sess-1' } })
    const handover = vi.spyOn(store, 'handoverChat').mockRejectedValue(new Error('engine refused'))
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    const toast = vi.spyOn(store, 'pushErrorToast')
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    expect(handover).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    expect(sendMessage).not.toHaveBeenCalled()
    // The failure is reported, not swallowed.
    expect(toast).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('sends the turn once when the recovery is picked twice in a row', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const recover = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    failWithUnsupportedModel(store)
    sendMessage.mockResolvedValue()
    await flushPromises()

    await openErrorRecovery(wrapper)
    const selector = picker(wrapper)
    selector.vm.$emit('select', 'sonnet', 'anthropic')
    selector.vm.$emit('select', 'sonnet', 'anthropic')
    await flushPromises()

    expect(sendMessage).not.toHaveBeenCalled()
    expect(recover).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('drops the captured recovery when the chat changes', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    failWithUnsupportedModel(store)
    sendMessage.mockResolvedValue()
    await flushPromises()

    await openErrorRecovery(wrapper)
    // The user moves to another chat before choosing a model.
    store.chats.push(makeChat('chat-2'))
    store.activeChatId = 'chat-2'
    await wrapper.setProps({ chatId: 'chat-2' })
    store.messages['chat-2'] = []
    await flushPromises()

    await pickModel(wrapper, 'opus')

    // The retry belongs to the chat it was captured in: nothing is sent.
    expect(sendMessage).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    // The pick itself is an ordinary model change on the chat now on screen.
    expect(updateChat.mock.calls[0][0]).toBe('chat-2')
    wrapper.unmount()
  })

  it('disables thinking changes and rejects another model during recovery', async () => {
    const { wrapper, store } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    const high = wrapper.findAll('.thinking-chip').find(c => c.text() === 'High')
    expect(high).toBeTruthy()
    expect(high!.attributes('disabled')).toBeDefined()
    await high!.trigger('click')
    await flushPromises()

    // Staged only: nothing is saved and the picker stays open.
    expect(updateChat).not.toHaveBeenCalled()
    expect(store.chats[0].thinking_level).toBeUndefined()
    expect(wrapper.find('.model-picker-recovery__title').exists()).toBe(true)

    await pickModel(wrapper, 'fable')

    expect(updateChat).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not change the model or thinking level during recovery', async () => {
    const { wrapper, store } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    const high = wrapper.findAll('.thinking-chip').find(c => c.text() === 'High')
    await high!.trigger('click')
    await flushPromises()

    await pickModel(wrapper, 'opus')

    expect(updateChat).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('blocks even a programmatically staged thinking level during recovery', async () => {
    const { wrapper, store } = await mountPanel({ chat: { model: 'sonnet' } })
    const tryNow = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    const patch = vi.spyOn(api, 'patch').mockImplementation(async (path: string, body?: unknown) => {
      if (path === '/api/chats/chat-1') {
        const updates = (body ?? {}) as Partial<ChatInfo>
        return makeChat('chat-1', {
          model: String(updates.model ?? 'sonnet'),
          thinking_level: String(updates.thinking_level ?? ''),
        }) as never
      }
      return {} as never
    })
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    // The chips are the current model's set: sonnet stops at 'high', so the
    // picker does not offer 'xhigh'.
    expect(wrapper.findAll('.thinking-chip').map(c => c.text())).toEqual(['Auto', 'Low', 'Medium', 'High'])
    await stageThinkingLevel(wrapper, 'xhigh')
    await flushPromises()

    // haiku does 'high' and nothing else, so the staged level is out of range
    // for it and the retry falls back to the provider default.
    await pickModel(wrapper, 'haiku')

    const call = patch.mock.calls.find(c => c[0] === '/api/chats/chat-1')
    expect(call).toBeUndefined()
    expect(tryNow).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('saves the thinking level straight away outside a recovery', async () => {
    const { wrapper, store } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)

    await openComposerPicker(wrapper)
    const high = wrapper.findAll('.thinking-chip').find(c => c.text() === 'High')
    expect(high).toBeTruthy()
    await high!.trigger('click')
    await flushPromises()

    expect(updateChat).toHaveBeenCalledWith('chat-1', { thinking_level: 'high' })
    wrapper.unmount()
  })

  describe("Claude Code's own pick", () => {
    // Claude Code lists `default` and describes it as Opus 4.5, which is also
    // the `opus` row: the list shows that row once.
    const PICK_MODELS = {
      ...MODELS_RESPONSE,
      provider_models: { ...MODELS_RESPONSE.provider_models, claude: ['default', 'opus', 'sonnet'] },
      provider_catalog_evidence: { claude: { status: 'observed', source: null, reason: null, models: [
        { id: 'default', label: 'Default (recommended)', description: 'Opus 4.5' },
        { id: 'opus', label: 'Opus 4.5' },
        { id: 'sonnet', label: 'Sonnet 4.5' },
      ] } },
    }
    type Section = { key: string; models: string[]; modelLabels?: Record<string, string> }

    it('a chat on the pick shows and highlights its concrete row, and picking that row changes nothing', async () => {
      const { wrapper, store } = await mountPanel({ chat: { provider: 'claude', model: 'default' }, models: PICK_MODELS })
      const updateChat = vi.spyOn(store, 'updateChat')
      expect(wrapper.get('.model-picker-wrap.composer-model .composer-chip').text()).toBe('Claude Code · Opus 4.5')
      await openComposerPicker(wrapper)
      const claude = (picker(wrapper).vm.$attrs.sections as Section[]).find(section => section.key === 'anthropic')!
      expect(claude.models).toEqual(['opus', 'sonnet'])
      expect(picker(wrapper).vm.$attrs['active-models']).toEqual(['opus'])
      picker(wrapper).vm.$emit('select', 'opus', 'anthropic')
      await flushPromises()
      expect(updateChat).not.toHaveBeenCalled()
      wrapper.unmount()
    })

    it('a chat pinned to another model can go back to the pick from its last row', async () => {
      const { wrapper } = await mountPanel({ chat: { provider: 'claude', model: 'sonnet' }, models: PICK_MODELS })
      await openComposerPicker(wrapper)
      const claude = (picker(wrapper).vm.$attrs.sections as Section[]).find(section => section.key === 'anthropic')!
      expect(claude.models).toEqual(['opus', 'sonnet', 'default'])
      expect(claude.modelLabels?.default).toBe("Use Claude Code's pick (Opus 4.5)")
      wrapper.unmount()
    })
  })

  it('shows confirmed permission modes and exact engine mappings', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockImplementation(async (_chatId, updates) => {
      Object.assign(store.chats[0], updates)
    })
    const handover = vi.spyOn(store, 'handoverChat')
    await openComposerPicker(wrapper)
    const menu = permissionMenu(wrapper)
    expect(menu.props('modes')).toEqual(['normal', 'auto', 'bypass', 'plan'])
    expect(menu.props('modelValue')).toBe('normal')
    expect(menu.props('disabled')).toBe(false)
    for (const [name, value] of [['Auto', 'auto'], ['Bypass', 'bypass'], ['Plan', 'plan'], ['Manual', 'normal']]) {
      await choosePermissionMode(wrapper, name)
      await flushPromises()
      expect(updateChat).toHaveBeenLastCalledWith('chat-1', { mode: value })
    }
    expect(updateChat).toHaveBeenCalledTimes(4)
    expect(sendMessage).not.toHaveBeenCalled()
    expect(handover).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps a pending mode unconfirmed and reports a failed write', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    let rejectWrite!: (error: Error) => void
    vi.spyOn(api, 'patch').mockReturnValue(new Promise((_, reject) => { rejectWrite = reject }) as never)
    sendMessage.mockResolvedValue()
    store.pendingImages = ['staged.png']
    await wrapper.get('.chat-input').setValue('keep this draft')
    await openComposerPicker(wrapper)
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    expect(store.pendingChatModeWrites['chat-1']).toBe(1)
    expect(wrapper.text()).toContain('Saving permission mode… Sending from this composer is paused.')
    expect(permissionMenu(wrapper).props('modelValue')).toBe('normal')
    expect(permissionMenu(wrapper).props('disabled')).toBe(true)
    expect(wrapper.findAll('.thinking-chip').filter(button => ['Auto', 'Low', 'Medium', 'High'].includes(button.text())).every(button => button.attributes('disabled') !== undefined)).toBe(true)
    rejectWrite(new Error('connection lost'))
    await flushPromises()
    expect(store.pendingChatModeWrites['chat-1']).toBeUndefined()
    expect(store.toasts.at(-1)).toMatchObject({
      title: 'Could not change permission mode',
      body: 'connection lost The server may have saved it. Reopen the chat to check.',
    })
    expect(wrapper.get<HTMLTextAreaElement>('.chat-input').element.value).toBe('keep this draft')
    expect(store.pendingImages).toEqual(['staged.png'])
    expect(sendMessage).not.toHaveBeenCalled()
    await wrapper.get('.send-btn').trigger('click')
    expect(sendMessage).toHaveBeenCalledWith('chat-1', 'keep this draft', undefined, expect.any(Function))
    wrapper.unmount()
  })

  it('unknown mode selects no option', async () => {
    const { wrapper } = await mountPanel({ chat: { mode: 'future-mode' } })
    await openComposerPicker(wrapper)
    expect(wrapper.text()).toContain('Current mode is unknown.')
    expect(permissionMenu(wrapper).props('modelValue')).toBe('future-mode')
    wrapper.unmount()
  })

  it('blocks button and keyboard composer sends while mode is pending but not Stop', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    let resolveWrite!: (chat: ChatInfo) => void
    vi.spyOn(api, 'patch').mockReturnValue(new Promise(resolve => { resolveWrite = resolve }) as never)
    await openComposerPicker(wrapper)
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    const textarea = wrapper.get<HTMLTextAreaElement>('.chat-input')
    await textarea.setValue('preserved draft')
    await wrapper.get('.send-btn').trigger('click')
    await textarea.trigger('keydown', { key: 'Enter', ctrlKey: true })
    const panel = wrapper.vm as unknown as { handleSendShortcut: () => boolean }
    expect(panel.handleSendShortcut()).toBe(false)
    expect(sendMessage).not.toHaveBeenCalled()
    expect(textarea.element.value).toBe('preserved draft')
    expect(wrapper.get('.send-btn').attributes('title')).toBe('Wait for the permission mode to save')
    resolveWrite({ ...store.chats[0], mode: 'auto' })
    await flushPromises()
    await wrapper.get('.send-btn').trigger('click')
    expect(sendMessage).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps Stop available during a pending mode write', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.streaming['chat-1'] = true
    store.pendingChatModeWrites['chat-1'] = 1
    const stopChat = vi.spyOn(store, 'stopChat').mockResolvedValue(undefined as never)
    await flushPromises()
    const send = wrapper.get<HTMLButtonElement>('.send-btn')
    expect(send.attributes('disabled')).toBeUndefined()
    expect(send.attributes('aria-label')).toBe('Stop generation')
    await send.trigger('click')
    expect(stopChat).toHaveBeenCalledWith('chat-1')
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('mcp shortcut remains available while a mode write is pending', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    let resolveWrite!: (chat: ChatInfo) => void
    vi.spyOn(api, 'patch').mockReturnValue(new Promise(resolve => { resolveWrite = resolve }) as never)
    await openComposerPicker(wrapper)
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    const textarea = wrapper.get<HTMLTextAreaElement>('.chat-input')
    await textarea.setValue('/mcp')
    await wrapper.get('.send-btn').trigger('click')
    expect((wrapper.vm as unknown as { detailsTarget: string }).detailsTarget).toBe('mcp')
    expect(sendMessage).not.toHaveBeenCalled()
    resolveWrite({ ...store.chats[0], mode: 'auto' })
    await flushPromises()
    wrapper.unmount()
  })

  it('recovery remains usable while a mode write is pending', async () => {
    const { wrapper, store } = await mountPanel()
    failWithUnsupportedModel(store)
    await flushPromises()
    let resolveModeWrite!: (chat: ChatInfo) => void
    const patch = vi.spyOn(api, 'patch').mockReturnValue(new Promise(resolve => { resolveModeWrite = resolve }) as never)
    await openComposerPicker(wrapper)
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    expect(store.pendingChatModeWrites['chat-1']).toBe(1)
    const continueRecovery = vi.spyOn(store, 'continueRecovery').mockResolvedValue(undefined as never)
    const switchModel = wrapper.findAll('.retry-btn').find(button => button.text() === 'Switch model')
    expect(switchModel).toBeTruthy()
    await switchModel!.trigger('click')
    await flushPromises()
    expect(wrapper.find('.permission-modes').exists()).toBe(false)
    expect(wrapper.findAll('.thinking-chip').some(button => button.attributes('disabled') !== undefined)).toBe(true)
    await pickModel(wrapper, 'sonnet')
    expect(continueRecovery).toHaveBeenCalled()
    expect(patch).toHaveBeenCalledTimes(1)
    resolveModeWrite({ ...store.chats[0], mode: 'auto' })
    await flushPromises()
    expect(store.pendingChatModeWrites['chat-1']).toBeUndefined()
    wrapper.unmount()
  })

  it('mode write follows its chat across A B A and two panels', async () => {
    const { wrapper, store, pinia } = await mountPanel()
    store.chats.push(makeChat('chat-2'))
    store.messages['chat-2'] = []
    const secondPanel = await mountPanel({ pinia, chatId: 'chat-1' })
    let resolveChatA!: (chat: ChatInfo) => void
    const patch = vi.spyOn(api, 'patch').mockImplementation((path: string, body?: unknown) => {
      if (path === '/api/chats/chat-1') {
        return new Promise(resolve => { resolveChatA = resolve }) as never
      }
      return Promise.resolve({ ...makeChat('chat-2'), ...(body as Partial<ChatInfo>) }) as never
    })
    await openComposerPicker(wrapper)
    await openComposerPicker(secondPanel.wrapper)
    expect(permissionMenu(secondPanel.wrapper).exists()).toBe(true)
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    expect(patch).toHaveBeenCalledTimes(1)
    expect(patch).toHaveBeenCalledWith('/api/chats/chat-1', { mode: 'auto' })
    expect(store.pendingChatModeWrites['chat-1']).toBe(1)
    expect(permissionMenu(wrapper).props('disabled')).toBe(true)
    expect(permissionMenu(secondPanel.wrapper).exists()).toBe(true)
    expect(permissionMenu(secondPanel.wrapper).props('disabled')).toBe(true)
    await choosePermissionMode(secondPanel.wrapper, 'Auto')
    await flushPromises()
    expect(patch).toHaveBeenCalledTimes(1)
    await wrapper.setProps({ chatId: 'chat-2' })
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    expect(patch).toHaveBeenCalledWith('/api/chats/chat-2', { mode: 'auto' })
    expect(store.chats.find(chat => chat.chat_id === 'chat-2')?.mode).toBe('auto')
    await wrapper.setProps({ chatId: 'chat-1' })
    expect(permissionMenu(wrapper).props('disabled')).toBe(true)
    resolveChatA({ ...makeChat('chat-1'), mode: 'auto' })
    await flushPromises()
    expect(store.pendingChatModeWrites['chat-1']).toBeUndefined()
    expect(store.chats.find(chat => chat.chat_id === 'chat-1')?.mode).toBe('auto')
    expect(permissionMenu(secondPanel.wrapper).exists()).toBe(true)
    expect(permissionMenu(secondPanel.wrapper).props('disabled')).toBe(false)
    expect(picker(wrapper).exists()).toBe(true)
    expect(wrapper.get('.model-picker-wrap.composer-model .composer-chip').attributes('aria-expanded')).toBe('true')
    secondPanel.wrapper.unmount()
    wrapper.unmount()
  })

  it('keeps an unmounted panel mode write tracked until its API response settles', async () => {
    const { wrapper, store, pinia } = await mountPanel()
    const secondPanel = await mountPanel({ pinia, chatId: 'chat-1' })
    let resolveModeWrite!: (chat: ChatInfo) => void
    const patch = vi.spyOn(api, 'patch').mockReturnValue(new Promise(resolve => { resolveModeWrite = resolve }) as never)
    await openComposerPicker(wrapper)
    await openComposerPicker(secondPanel.wrapper)
    expect(permissionMenu(secondPanel.wrapper).exists()).toBe(true)
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    expect(store.pendingChatModeWrites['chat-1']).toBe(1)
    wrapper.unmount()
    expect(permissionMenu(secondPanel.wrapper).exists()).toBe(true)
    expect(permissionMenu(secondPanel.wrapper).props('disabled')).toBe(true)
    resolveModeWrite({ ...store.chats[0], mode: 'auto' })
    await flushPromises()
    expect(store.pendingChatModeWrites['chat-1']).toBeUndefined()
    expect(store.chats[0].mode).toBe('auto')
    expect(patch).toHaveBeenCalledTimes(1)
    expect(permissionMenu(secondPanel.wrapper).exists()).toBe(true)
    expect(permissionMenu(secondPanel.wrapper).props('disabled')).toBe(false)
    secondPanel.wrapper.unmount()
  })

  it('thinking completion cannot close a reopened or different chat picker', async () => {
    const { wrapper, store } = await mountPanel()
    store.chats.push(makeChat('chat-2'))
    store.messages['chat-2'] = []
    let resolveThinking!: (chat: ChatInfo) => void
    let serverChat = { ...store.chats[0] }
    const patch = vi.spyOn(api, 'patch').mockImplementation((_path: string, body?: unknown) => {
      const updates = body as Partial<ChatInfo>
      if (updates.thinking_level !== undefined) {
        return new Promise(resolve => { resolveThinking = resolve }) as never
      }
      serverChat = { ...serverChat, ...updates }
      return Promise.resolve(serverChat) as never
    })
    await openComposerPicker(wrapper)
    await wrapper.findAll('.thinking-chip').find(button => button.text() === 'High')!.trigger('click')
    await flushPromises()
    expect(patch).toHaveBeenCalledWith('/api/chats/chat-1', { thinking_level: 'high' })
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    expect(store.pendingChatModeWrites['chat-1']).toBe(1)
    expect(patch).toHaveBeenCalledTimes(1)
    picker(wrapper).vm.$emit('close', 'other')
    await wrapper.get('.model-picker-wrap.composer-model .composer-chip').trigger('click')
    await wrapper.setProps({ chatId: 'chat-2' })
    await wrapper.setProps({ chatId: 'chat-1' })
    serverChat = { ...makeChat('chat-1'), thinking_level: 'high' }
    resolveThinking(serverChat)
    await flushPromises()
    expect(patch).toHaveBeenCalledTimes(2)
    expect(picker(wrapper).exists()).toBe(true)
    expect(wrapper.get('.model-picker-wrap.composer-model .composer-chip').attributes('aria-expanded')).toBe('true')
    expect(store.chats.find(chat => chat.chat_id === 'chat-1')).toMatchObject({
      thinking_level: 'high',
      mode: 'auto',
    })
    wrapper.unmount()
  })

  it('hands a registry provider\'s pick over to that provider, by its section or by its catalog', async () => {
    const { wrapper, store } = await mountPanel({ chat: { session_id: 'sess-1' } })
    const handover = vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    // The section names the provider even for a model no catalog lists.
    await openComposerPicker(wrapper)
    await pickModel(wrapper, 'codex-preview', 'codex')
    expect(handover).toHaveBeenLastCalledWith('chat-1', expect.objectContaining({ provider: 'codex', model: 'codex-preview' }))
    // With no section named, the model's own provider catalog decides the route.
    await openComposerPicker(wrapper)
    await pickModel(wrapper, 'gpt-5.5', '')
    expect(handover).toHaveBeenLastCalledWith('chat-1', expect.objectContaining({ provider: 'codex', model: 'gpt-5.5' }))
    expect(handover).toHaveBeenCalledTimes(2)
  })

  it('handover completion cannot close a reopened picker', async () => {
    const { wrapper, store } = await mountPanel({ chat: { session_id: 'sess-1' } })
    store.chats.push(makeChat('chat-2'))
    store.messages['chat-2'] = []
    let resolveHandover!: (value: unknown) => void
    const post = vi.spyOn(api, 'post').mockReturnValue(new Promise(resolve => { resolveHandover = resolve }) as never)
    let serverChat = { ...store.chats[0] }
    const patch = vi.spyOn(api, 'patch').mockImplementation(async (_path: string, body?: unknown) => {
      serverChat = { ...serverChat, ...(body as Partial<ChatInfo>) }
      return serverChat as never
    })
    await openComposerPicker(wrapper)
    picker(wrapper).vm.$emit('select', 'openai/gpt-5.6', 'opencode')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/chats/chat-1/handover', expect.objectContaining({
      provider: 'opencode',
      model: 'openai/gpt-5.6',
    }))
    picker(wrapper).vm.$emit('close', 'other')
    await wrapper.get('.model-picker-wrap.composer-model .composer-chip').trigger('click')
    await wrapper.setProps({ chatId: 'chat-2' })
    await wrapper.setProps({ chatId: 'chat-1' })
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    expect(store.pendingChatModeWrites['chat-1']).toBe(1)
    expect(patch).not.toHaveBeenCalled()
    serverChat = { ...store.chats[0], provider: 'opencode', model: 'openai/gpt-5.6' }
    resolveHandover(serverChat)
    await flushPromises()
    expect(patch).toHaveBeenCalledWith('/api/chats/chat-1', { mode: 'auto' })
    expect(picker(wrapper).exists()).toBe(true)
    expect(wrapper.get('.model-picker-wrap.composer-model .composer-chip').attributes('aria-expanded')).toBe('true')
    expect(store.chats.find(chat => chat.chat_id === 'chat-1')).toMatchObject({
      provider: 'opencode',
      model: 'openai/gpt-5.6',
      mode: 'auto',
    })
    wrapper.unmount()
  })

  it('handover failure cannot close a reopened picker', async () => {
    const { wrapper, store } = await mountPanel({ chat: { session_id: 'sess-1' } })
    store.chats.push(makeChat('chat-2'))
    store.messages['chat-2'] = []
    let rejectHandover!: (error: Error) => void
    const post = vi.spyOn(api, 'post').mockReturnValue(new Promise((_, reject) => { rejectHandover = reject }) as never)
    await openComposerPicker(wrapper)
    picker(wrapper).vm.$emit('select', 'openai/gpt-5.6', 'opencode')
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)
    picker(wrapper).vm.$emit('close', 'other')
    await wrapper.get('.model-picker-wrap.composer-model .composer-chip').trigger('click')
    await wrapper.setProps({ chatId: 'chat-2' })
    await wrapper.setProps({ chatId: 'chat-1' })
    rejectHandover(new Error('network failure'))
    await flushPromises()
    expect(picker(wrapper).exists()).toBe(true)
    expect(wrapper.get('.model-picker-wrap.composer-model .composer-chip').attributes('aria-expanded')).toBe('true')
    expect(store.toasts.at(-1)?.body).toContain('network failure')
    wrapper.unmount()
  })

  it('streaming explains subsequent turns and the queued-turn limitation', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.streaming['chat-1'] = true
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    await openComposerPicker(wrapper)
    expect(wrapper.text()).toContain('Changes apply to subsequent turns, not the reply already running. Already queued or external turns may start before a change is saved.')
    // The visible note is one line; the full rule is the control's description.
    const note = wrapper.get('.permission-modes__note')
    expect(note.get('[aria-hidden="true"]').text()).toBe('Applies to your next message, not a reply already running.')
    const describedBy = permissionMenu(wrapper).props('describedBy') as string
    expect(wrapper.get(`#${describedBy}`).text()).toBe('Changes apply to subsequent turns, not the reply already running. Already queued or external turns may start before a change is saved.')
    await choosePermissionMode(wrapper, 'Auto')
    await flushPromises()
    expect(updateChat).toHaveBeenCalledTimes(1)
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('restores focus only to the initiating normal composer trigger on Escape', async () => {
    const { wrapper } = await mountPanel()
    document.body.appendChild(wrapper.element)
    await openComposerPicker(wrapper)
    const trigger = wrapper.get<HTMLButtonElement>('.model-picker-wrap.composer-model .composer-chip')
    const child = picker(wrapper)
    child.vm.$emit('close', 'escape')
    await flushPromises()
    expect(document.activeElement).toBe(trigger.element)

    await openComposerPicker(wrapper)
    const textarea = wrapper.get<HTMLTextAreaElement>('.chat-input')
    textarea.element.focus()
    picker(wrapper).vm.$emit('close', 'other')
    await flushPromises()
    expect(document.activeElement).not.toBe(trigger.element)

    await openComposerPicker(wrapper)
    picker(wrapper).vm.$emit('close', 'escape')
    trigger.element.click()
    textarea.element.focus()
    await flushPromises()
    expect(document.activeElement).toBe(textarea.element)
    wrapper.unmount()
  })

  it('does not steal focus from a textarea focused after Escape before nextTick', async () => {
    const { wrapper } = await mountPanel()
    document.body.appendChild(wrapper.element)
    await openComposerPicker(wrapper)
    const textarea = wrapper.get<HTMLTextAreaElement>('.chat-input')

    picker(wrapper).vm.$emit('close', 'escape')
    textarea.element.focus()
    await flushPromises()

    expect(document.activeElement).toBe(textarea.element)
    wrapper.unmount()
  })

  it('does not restore composer focus when Escape starts with focus outside the picker', async () => {
    const { wrapper } = await mountPanel()
    document.body.appendChild(wrapper.element)
    await openComposerPicker(wrapper)
    const textarea = wrapper.get<HTMLTextAreaElement>('.chat-input')
    textarea.element.focus()

    picker(wrapper).vm.$emit('close', 'escape')
    await flushPromises()

    expect(document.activeElement).toBe(textarea.element)
    wrapper.unmount()
  })

  it('does not restore composer focus on recovery Escape or unmount', async () => {
    const { wrapper, store } = await mountPanel()
    document.body.appendChild(wrapper.element)
    failWithUnsupportedModel(store)
    await flushPromises()
    await openErrorRecovery(wrapper)
    const trigger = wrapper.get<HTMLButtonElement>('.model-picker-wrap.composer-model .composer-chip')
    const textarea = wrapper.get<HTMLTextAreaElement>('.chat-input')
    textarea.element.focus()
    picker(wrapper).vm.$emit('close', 'escape')
    await flushPromises()
    expect(document.activeElement).toBe(textarea.element)

    await openComposerPicker(wrapper)
    picker(wrapper).vm.$emit('close', 'escape')
    wrapper.unmount()
    await flushPromises()
    expect(trigger.element.isConnected).toBe(false)
  })
})

// A turn the engine recorded as a retry is a notice, not a second copy of the
// request: the request stays in its own turn and the retry says so.
describe('ChatPanel retry turn rendering', () => {
  const ORIGINAL = 'Check the video'
  const IMAGE = 'web_5a4d7145.png'

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

  it('draws the live optimistic retry as the same notice, not a second bubble', async () => {
    // While the retry streams, the store's own bubble already carries the
    // origin and a send id. The transcript must show the notice once and no
    // duplicate user bubble for the same turn.
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      {
        role: 'user',
        content: ORIGINAL,
        timestamp: '2026-08-08T15:51:00Z',
        turn_index: 0,
      },
      {
        role: 'user',
        content: ORIGINAL,
        timestamp: '2026-08-08T15:52:00Z',
        turn_index: 1,
        origin: 'retry',
        send_id: 'send-retry-live',
      },
    ]
    await flushPromises()

    expect(wrapper.findAll('.injected-card--retry')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('send-retry-live')
    const bubbles = wrapper.findAll('.message-wrap.user')
    expect(bubbles).toHaveLength(1)
    expect(bubbles[0].text()).toContain(ORIGINAL)
  })

  it('draws a persisted retry turn as a notice and keeps the request in one bubble', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      {
        role: 'user',
        content: ORIGINAL,
        timestamp: '2026-08-08T15:51:00Z',
        turn_index: 0,
        images: [IMAGE],
      },
      {
        role: 'assistant',
        content: 'Error: connection refused',
        timestamp: '2026-08-08T15:51:01Z',
        is_error: true,
      },
      {
        role: 'user',
        content: ORIGINAL,
        timestamp: '2026-08-08T15:52:00Z',
        turn_index: 1,
        origin: 'retry',
        images: [IMAGE],
      },
    ]
    await flushPromises()

    // The retried turn is a notice, not another bubble.
    const notice = wrapper.get('.injected-card--retry')
    expect(notice.attributes('aria-label')).toBe('Retry started')
    expect(wrapper.findAll('.injected-card--retry')).toHaveLength(1)
    // Collapsed by default: the repeated request is not read by accident.
    expect(wrapper.find('.injected-card--retry .injected-card-body').exists()).toBe(false)

    // The original request stays one full bubble.
    expect(wrapper.findAll('.message-wrap.user')).toHaveLength(1)
    expect(wrapper.get('.message-wrap.user .message-content').text()).toContain(ORIGINAL)
    wrapper.unmount()
  })

  it('never turns an ordinary turn into a notice', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'user', content: ORIGINAL, timestamp: '2026-08-08T15:51:00Z', turn_index: 0 },
    ]
    await flushPromises()

    expect(wrapper.find('.injected-card').exists()).toBe(false)
    expect(wrapper.findAll('.message-wrap.user')).toHaveLength(1)
    wrapper.unmount()
  })

  it('continues the latest failure after an earlier retry notice without replay', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.messages['chat-1'] = [
      {
        role: 'user',
        content: ORIGINAL,
        timestamp: '2026-08-08T15:51:00Z',
        turn_index: 0,
        images: [IMAGE],
      },
      {
        role: 'assistant',
        content: 'Error: connection refused',
        timestamp: '2026-08-08T15:51:01Z',
        is_error: true,
      },
      {
        role: 'user',
        content: ORIGINAL,
        timestamp: '2026-08-08T15:52:00Z',
        turn_index: 1,
        origin: 'retry',
        images: [IMAGE],
      },
      {
        role: 'assistant',
        content: 'Error: connection refused again',
        timestamp: '2026-08-08T15:52:01Z',
        is_error: true,
      },
    ]
    const recover = verifyCurrentFailure(store)
    await flushPromises()

    const retryBtns = wrapper.findAll('.retry-btn').filter(b => b.text() === 'Retry')
    // Only the last error offers Retry; the earlier one has later messages.
    expect(retryBtns).toHaveLength(1)
    // The retry under the notice that follows it: the notice is in between.
    await retryBtns[0]!.trigger('click')
    await flushPromises()

    expect(sendMessage).not.toHaveBeenCalled()
    expect(recover).toHaveBeenCalledWith('chat-1', 'failure-1', true)
    wrapper.unmount()
  })
})
