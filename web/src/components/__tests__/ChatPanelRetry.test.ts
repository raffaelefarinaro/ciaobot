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
  provider_models: { claude: ['haiku', 'sonnet', 'opus', 'fable'] },
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
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

interface MountOptions {
  /** Overrides for the panel's chat (a locked bucket, an archived chat, ...). */
  chat?: Partial<ChatInfo>
}

async function mountPanel(options: MountOptions = {}): Promise<{
  wrapper: VueWrapper
  store: ReturnType<typeof useProjectStore>
  sendMessage: ReturnType<typeof vi.spyOn>
}> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat('chat-1', options.chat)]
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

  const sendMessage = vi.spyOn(store, 'sendMessage')
  const wrapper = shallowMount(ChatPanel, {
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ModelSelectorStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
        // The real notice card, so a test can read what the reader sees: the
        // stub would only carry the props.
        InjectedPromptCard: false,
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

  // A deliberate retry is a new turn the user asked for, so it is tagged as
  // one: the engine records the tag per turn and the PWA can tell it apart
  // from what the user typed. The tag is the only thing the client chooses.
  it('tags the resend as a retry', async () => {
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
    expect(prepared.origin).toBe('retry')
    // The failed turn's images travel with the retry.
    expect(prepared.imageRefs).toEqual(['web_5a4d7145.png'])
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
    sendMessage.mockResolvedValue()
    await flushPromises()

    const retryBtn = wrapper.findAll('.retry-btn').find(b => b.text() === 'Retry')
    expect(retryBtn).toBeTruthy()
    await retryBtn!.trigger('click')
    await flushPromises()

    expect(sendMessage).toHaveBeenCalledTimes(1)
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
    await flushPromises()

    const labels = wrapper.findAll('.retry-btn').map(b => b.text())
    expect(labels).toEqual(['Retry', 'Retry every hour'])
    await wrapper.findAll('.retry-btn')[1].trigger('click')
    await flushPromises()
    expect(setRetry).toHaveBeenCalledWith('chat-1', 'Tidy my memory', [])

    // Once the retry is armed the retry card owns it.
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-12T11:00:00Z',
      last_error: 'manual',
      attempts: 0,
      interval_seconds: 3600,
    }
    await flushPromises()
    expect(wrapper.findAll('.retry-btn').map(b => b.text())).toEqual(['Retry'])
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
    sendMessage.mockResolvedValue()
    await flushPromises()

    const hint = wrapper.findComponent({ name: 'ProviderSignInHint' })
    expect(hint.exists()).toBe(true)
    expect(hint.props('provider')).toBe('claude')
    expect(hint.props('actionLabel')).toBe('Retry')
    hint.vm.$emit('action')
    await flushPromises()
    expect(sendMessage).toHaveBeenCalledTimes(1)
    expect(sendMessage.mock.calls[0][1]).toBe('Summarise my inbox')
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

    expect(wrapper.find('.retry-card-title').text()).toBe('Signed out — retrying this turn')
    const hint = wrapper.findComponent({ name: 'ProviderSignInHint' })
    expect(hint.props('actionLabel')).toBe('Try now')
    hint.vm.$emit('action')
    await flushPromises()
    expect(tryNow).toHaveBeenCalledWith('chat-1')
    wrapper.unmount()
  })

  it('says a weekly or monthly limit retries once a day', async () => {
    const { wrapper, store } = await mountPanel()
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-06T15:45:00Z',
      last_error: 'weekly usage limit',
      attempts: 0,
      interval_seconds: 86400,
    }
    await flushPromises()
    expect(wrapper.find('.retry-card-title').text()).toBe('Retrying this turn once a day')
    wrapper.unmount()
  })

  it('does not promise an hourly retry once a quota retry has backed off', async () => {
    const { wrapper, store } = await mountPanel()
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-05T19:45:00Z',
      last_error: 'session usage limit',
      attempts: 2,
      interval_seconds: 4 * 3600,
    }
    await flushPromises()
    expect(wrapper.find('.retry-card-title').text()).toBe('Retrying this turn every few hours')
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
    expect(wrapper.find('.retry-card-title').text()).toBe('Retrying this turn shortly')
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

  async function openComposerPicker(wrapper: VueWrapper) {
    const chip = wrapper.find('.model-picker-wrap.composer-model .composer-chip')
    expect(chip.exists()).toBe(true)
    await chip.trigger('click')
    await flushPromises()
  }

  async function openCardRecovery(wrapper: VueWrapper) {
    const btn = wrapper.findAll('.retry-card-actions .btn-small').find(b => b.text() === 'Continue with...')
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
    store.chats[0].retry = {
      status: 'pending',
      next_at: '2026-10-22T10:00:00Z',
      last_error: 'weekly usage limit',
      attempts: 1,
      interval_seconds: 3600,
    }
  }

  function failWithUnsupportedModel(store: ReturnType<typeof useProjectStore>) {
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
  }

  it('opens the recovery picker with its heading from the retry card', async () => {
    const { wrapper, store } = await mountPanel()
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)

    expect(wrapper.find('.model-picker-recovery__title').text()).toBe('Choose a model to retry immediately')
    expect(wrapper.find('.model-picker-recovery__note').text()).toContain('may incur usage charges')
    // The note names the limit of a staged level: the chips are the current
    // model's set, so a level the picked model does not offer is dropped.
    expect(wrapper.find('.model-picker-recovery__note').text()).toContain(
      'A Thinking level you pick here applies to that retry, within the levels the current model offers.',
    )
    // The listbox is named by the heading and the search field describes
    // itself with the consequence, so the warning is not visual-only.
    const stub = wrapper.getComponent({ name: 'ModelSelectorStub' })
    expect(stub.attributes('aria-labelledby')).toBe('model-picker-recovery-title')
    expect(stub.attributes('aria-describedby')).toBe('model-picker-recovery-note')
    wrapper.unmount()
  })

  it('opens the same recovery picker from the error bubble', async () => {
    const { wrapper, store } = await mountPanel()
    failWithUnsupportedModel(store)
    await flushPromises()

    await openErrorRecovery(wrapper)

    expect(wrapper.find('.model-picker-recovery__title').text()).toBe('Choose a model to retry immediately')
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

  it('retries the deferred turn once when the recovery picker picks a model', async () => {
    const { wrapper, store } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'opus')

    expect(updateChat).toHaveBeenCalledTimes(1)
    expect(updateChat.mock.calls[0][0]).toBe('chat-1')
    expect(updateChat.mock.calls[0][1]).toMatchObject({ model: 'opus', provider: 'claude' })
    expect(tryNow).toHaveBeenCalledTimes(1)
    expect(tryNow).toHaveBeenCalledWith('chat-1')
    wrapper.unmount()
  })

  it('retries in a recovery even when the picked model is the one already in use', async () => {
    const { wrapper, store } = await mountPanel()
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'sonnet')

    expect(tryNow).toHaveBeenCalledTimes(1)
    expect(tryNow).toHaveBeenCalledWith('chat-1')
    wrapper.unmount()
  })

  it('never retries from the composer picker, even with a deferred retry waiting', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
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
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openComposerPicker(wrapper)
    await pickModel(wrapper, 'sonnet')

    expect(updateChat).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('resends the failed turn when the error bubble recovery picks a model', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    failWithUnsupportedModel(store)
    sendMessage.mockResolvedValue()
    await flushPromises()

    await openErrorRecovery(wrapper)
    await pickModel(wrapper, 'opus')

    expect(sendMessage).toHaveBeenCalledTimes(1)
    const [, text, prepared] = sendMessage.mock.calls[0]
    expect(text).toBe('Check the video')
    // The failed turn's images travel with the retry.
    expect(prepared.imageRefs).toEqual(['web_5a4d7145.png'])
    // The picker retries through the same helper, so the turn is tagged too.
    expect(prepared.origin).toBe('retry')
    // The composer is not drained by a recovery.
    expect(store.pendingImages).toEqual([])
    wrapper.unmount()
  })

  it('asks before a handover and retries only after it succeeded', async () => {
    const { wrapper, store } = await mountPanel({ chat: { session_id: 'sess-1' } })
    const handover = vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    expect(askConfirm).toHaveBeenCalledTimes(1)
    // The existing handover confirmation is kept.
    expect(askConfirm.mock.calls[0][0]).toContain('Hand over this chat to')
    expect(handover).toHaveBeenCalledTimes(1)
    expect(handover.mock.calls[0][0]).toBe('chat-1')
    expect(handover.mock.calls[0][1]).toMatchObject({ model: 'openai/gpt-5.6', provider: 'opencode' })
    expect(tryNow).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('applies a staged thinking level after a handover, before retrying', async () => {
    const { wrapper, store } = await mountPanel({ chat: { session_id: 'sess-1' } })
    const handover = vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await wrapper.findAll('.thinking-chip').find(c => c.text() === 'High')!.trigger('click')
    await flushPromises()
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    // The handover route takes only the route; the level rides its own PATCH.
    expect(handover.mock.calls[0][1]).toEqual({ provider: 'opencode', model: 'openai/gpt-5.6' })
    expect(updateChat.mock.calls[0][1]).toMatchObject({ thinking_level: 'high' })
    expect(updateChat.mock.invocationCallOrder[0]!).toBeLessThan(tryNow.mock.invocationCallOrder[0]!)
    wrapper.unmount()
  })

  it('still retries at the provider default when the staged level cannot be saved', async () => {
    const { wrapper, store } = await mountPanel({ chat: { session_id: 'sess-1' } })
    vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    vi.spyOn(store, 'updateChat').mockRejectedValue(new Error('engine refused'))
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    const toast = vi.spyOn(store, 'pushErrorToast')
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await wrapper.findAll('.thinking-chip').find(c => c.text() === 'High')!.trigger('click')
    await flushPromises()
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    expect(toast).toHaveBeenCalled()
    // The retry is not lost to a failed level write.
    expect(tryNow).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('changes no model and sends nothing when the handover is cancelled', async () => {
    const { wrapper, store, sendMessage } = await mountPanel({ chat: { session_id: 'sess-1' } })
    const handover = vi.spyOn(store, 'handoverChat').mockResolvedValue(undefined as never)
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    askConfirm.mockResolvedValue(false)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(handover).not.toHaveBeenCalled()
    expect(updateChat).not.toHaveBeenCalled()
    expect(tryNow).not.toHaveBeenCalled()
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('sends nothing when the handover fails', async () => {
    const { wrapper, store, sendMessage } = await mountPanel({ chat: { session_id: 'sess-1' } })
    const handover = vi.spyOn(store, 'handoverChat').mockRejectedValue(new Error('engine refused'))
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    const toast = vi.spyOn(store, 'pushErrorToast')
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    await pickModel(wrapper, 'openai/gpt-5.6', 'opencode')

    expect(handover).toHaveBeenCalledTimes(1)
    expect(tryNow).not.toHaveBeenCalled()
    expect(sendMessage).not.toHaveBeenCalled()
    // The failure is reported, not swallowed.
    expect(toast).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('sends the turn once when the recovery is picked twice in a row', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    failWithUnsupportedModel(store)
    sendMessage.mockResolvedValue()
    await flushPromises()

    await openErrorRecovery(wrapper)
    const selector = picker(wrapper)
    selector.vm.$emit('select', 'opus', 'anthropic')
    selector.vm.$emit('select', 'opus', 'anthropic')
    await flushPromises()

    expect(sendMessage).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('drops the captured recovery when the chat changes', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    failWithUnsupportedModel(store)
    sendMessage.mockResolvedValue()
    await flushPromises()

    await openErrorRecovery(wrapper)
    // The user moves to another chat before choosing a model.
    store.chats.push(makeChat('chat-2'))
    store.activeChatId = 'chat-2'
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

  it('stages a thinking level and applies it when the chosen model supports it', async () => {
    const { wrapper, store } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    const high = wrapper.findAll('.thinking-chip').find(c => c.text() === 'High')
    expect(high).toBeTruthy()
    await high!.trigger('click')
    await flushPromises()

    // Staged only: nothing is saved and the picker stays open.
    expect(updateChat).not.toHaveBeenCalled()
    expect(store.chats[0].thinking_level).toBeUndefined()
    expect(wrapper.find('.model-picker-recovery__title').exists()).toBe(true)

    await pickModel(wrapper, 'fable')

    expect(updateChat.mock.calls[0][1]).toMatchObject({ model: 'fable', thinking_level: 'high' })
    expect(tryNow).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('falls back to the provider default when the chosen model cannot do the staged level', async () => {
    const { wrapper, store } = await mountPanel()
    const updateChat = vi.spyOn(store, 'updateChat').mockResolvedValue(undefined as never)
    armPendingRetry(store)
    await flushPromises()

    await openCardRecovery(wrapper)
    const high = wrapper.findAll('.thinking-chip').find(c => c.text() === 'High')
    await high!.trigger('click')
    await flushPromises()

    await pickModel(wrapper, 'opus')

    expect(updateChat.mock.calls[0][1]).toMatchObject({ model: 'opus', thinking_level: '' })
    wrapper.unmount()
  })

  it('staged thinking level unsupported by the target model falls back to the provider default', async () => {
    const { wrapper, store } = await mountPanel({ chat: { model: 'sonnet' } })
    const tryNow = vi.spyOn(store, 'tryChatRetryNow').mockResolvedValue(undefined as never)
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
    expect(call).toBeTruthy()
    expect(call![1]).toMatchObject({ model: 'haiku', thinking_level: '' })
    expect(tryNow).toHaveBeenCalledTimes(1)
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

  it('finds the request and its images after an earlier retry notice', async () => {
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
    sendMessage.mockResolvedValue()
    await flushPromises()

    const retryBtns = wrapper.findAll('.retry-btn').filter(b => b.text() === 'Retry')
    // Only the last error offers Retry; the earlier one has later messages.
    expect(retryBtns).toHaveLength(1)
    // The retry under the notice that follows it: the notice is in between.
    await retryBtns[0]!.trigger('click')
    await flushPromises()

    expect(sendMessage).toHaveBeenCalledTimes(1)
    const [, text, prepared] = sendMessage.mock.calls[0]
    expect(text).toBe(ORIGINAL)
    expect(prepared.imageRefs).toEqual([IMAGE])
    expect(prepared.origin).toBe('retry')
    wrapper.unmount()
  })
})
