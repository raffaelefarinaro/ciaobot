// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import { readChatDraft, writeChatDraft } from '../../lib/chatDrafts'
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

function makeChat(chatId: string, mode: string = 'normal'): ChatInfo {
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title: chatId,
    model: 'sonnet',
    provider: 'claude',
    mode,
    session_id: '',
    created_at: '',
    archived: false,
  }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

type Harness = {
  wrapper: VueWrapper
  store: ReturnType<typeof useProjectStore>
  updateChat: ReturnType<typeof vi.spyOn>
  sendMessage: ReturnType<typeof vi.spyOn>
}

class MemoryStorage {
  private values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }

  removeItem(key: string): void {
    this.values.delete(key)
  }

  clear(): void {
    this.values.clear()
  }
}

async function mountPanel(options: { mode?: string; commandsFail?: boolean } = {}): Promise<Harness> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat('chat-1', options.mode), makeChat('chat-2')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [], 'chat-2': [] }
  store.bootstrapped = true

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path === '/api/commands') {
      if (options.commandsFail) return Promise.reject(new Error('commands unavailable')) as never
      return Promise.resolve({ commands: [] }) as never
    }
    return Promise.resolve([]) as never
  })

  const updateChat = vi.spyOn(store, 'updateChat')
  const sendMessage = vi.spyOn(store, 'sendMessage')
  const wrapper = shallowMount(ChatPanel, {
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
        VoiceRecorder: ChildStub,
        SubagentPanel: ChildStub,
        ProviderSubchatPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
      },
    },
  })
  await flushPromises()
  return { wrapper, store, updateChat, sendMessage }
}

function textareaValue(wrapper: VueWrapper): string {
  return (wrapper.get('textarea.chat-input').element as HTMLTextAreaElement).value
}

describe('ChatPanel /plan interactions', () => {
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

  it('updates the originating chat and never sends /plan as a message', async () => {
    const { wrapper, updateChat, sendMessage } = await mountPanel()
    updateChat.mockResolvedValue()

    await wrapper.get('textarea.chat-input').setValue('/plan')
    await wrapper.get('button.send-btn').trigger('click')
    await flushPromises()

    expect(updateChat).toHaveBeenCalledWith('chat-1', { mode: 'plan' })
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps staged images, file comments, and chat comments after a successful toggle', async () => {
    const { wrapper, store, updateChat } = await mountPanel()
    store.pendingImages = ['image-ref']
    store.pendingComments = [{
      id: 'file-comment',
      path: 'notes.md',
      selection: 'line',
      comment: 'Check this',
    }]
    store.pendingChatComments = [{
      id: 'chat-comment',
      selection: 'reply',
      comment: 'Follow up',
      messageId: 'message-1',
      messageIndex: 0,
      messageRole: 'assistant',
      occurrenceIndex: 0,
      paragraphIndex: 0,
    }]
    const before = {
      images: [...store.pendingImages],
      comments: [...store.pendingComments],
      chatComments: [...store.pendingChatComments],
    }
    updateChat.mockResolvedValue()

    await wrapper.get('textarea.chat-input').setValue('/plan')
    await wrapper.get('button.send-btn').trigger('click')
    await flushPromises()

    expect(store.pendingImages).toEqual(before.images)
    expect(store.pendingComments).toEqual(before.comments)
    expect(store.pendingChatComments).toEqual(before.chatComments)
    wrapper.unmount()
  })

  it('uses the originating chat when the plan chip leaves plan mode', async () => {
    const { wrapper, updateChat } = await mountPanel({ mode: 'plan' })
    const pending = deferred<void>()
    updateChat.mockReturnValue(pending.promise)
    const chip = wrapper.get('button.plan-mode-chip')

    expect(chip.attributes('aria-label')).toBe('Leave plan mode')
    expect(chip.attributes('aria-pressed')).toBe('true')
    expect(chip.classes()).toContain('touch-hit')
    expect(chip.attributes('disabled')).toBeUndefined()

    await chip.trigger('click')
    await nextTick()

    expect(updateChat).toHaveBeenCalledWith('chat-1', { mode: 'normal' })
    expect(wrapper.get('button.plan-mode-chip').attributes('disabled')).toBeDefined()

    pending.resolve()
    await flushPromises()

    expect(wrapper.get('button.plan-mode-chip').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('does not erase a new message typed while the PATCH is pending', async () => {
    const { wrapper, updateChat } = await mountPanel()
    const pending = deferred<void>()
    updateChat.mockReturnValue(pending.promise)

    await wrapper.get('textarea.chat-input').setValue('/plan')
    await wrapper.get('button.send-btn').trigger('click')
    await wrapper.get('textarea.chat-input').setValue('next message')
    pending.resolve()
    await flushPromises()

    expect(textareaValue(wrapper)).toBe('next message')
    expect(readChatDraft('chat-1')).toBe('next message')
    wrapper.unmount()
  })

  it('does not clear another chat draft when the active chat changes while pending', async () => {
    const { wrapper, store, updateChat } = await mountPanel()
    const pending = deferred<void>()
    updateChat.mockReturnValue(pending.promise)

    await wrapper.get('textarea.chat-input').setValue('/plan')
    await wrapper.get('button.send-btn').trigger('click')
    store.activeChatId = 'chat-2'
    writeChatDraft('chat-2', 'draft in chat two')
    pending.resolve()
    await flushPromises()

    expect(readChatDraft('chat-2')).toBe('draft in chat two')
    wrapper.unmount()
  })

  it('retains /plan and shows an error on failure, then allows retry', async () => {
    const { wrapper, store, updateChat } = await mountPanel()
    store.pendingImages = ['image-ref']
    store.pendingComments = [{
      id: 'file-comment',
      path: 'notes.md',
      selection: 'line',
      comment: 'Check this',
    }]
    store.pendingChatComments = [{
      id: 'chat-comment',
      selection: 'reply',
      comment: 'Follow up',
      messageId: 'message-1',
      messageIndex: 0,
      messageRole: 'assistant',
      occurrenceIndex: 0,
      paragraphIndex: 0,
    }]
    const before = {
      images: [...store.pendingImages],
      comments: [...store.pendingComments],
      chatComments: [...store.pendingChatComments],
    }
    const error = new Error('PATCH failed')
    updateChat.mockRejectedValueOnce(error).mockResolvedValueOnce()

    await wrapper.get('textarea.chat-input').setValue('/plan')
    await wrapper.get('button.send-btn').trigger('click')
    await flushPromises()

    expect(textareaValue(wrapper)).toBe('/plan')
    expect(store.toasts.at(-1)).toMatchObject({
      title: 'Could not change plan mode',
      variant: 'error',
    })
    expect(store.pendingImages).toEqual(before.images)
    expect(store.pendingComments).toEqual(before.comments)
    expect(store.pendingChatComments).toEqual(before.chatComments)

    await wrapper.get('button.send-btn').trigger('click')
    await flushPromises()

    expect(updateChat).toHaveBeenNthCalledWith(2, 'chat-1', { mode: 'plan' })
    expect(textareaValue(wrapper)).toBe('')
    wrapper.unmount()
  })

  it('keeps the built-in /plan picker entry when the command API fails', async () => {
    const { wrapper } = await mountPanel({ commandsFail: true })

    await wrapper.get('textarea.chat-input').setValue('/')

    expect(wrapper.find('.commands-picker-name').text()).toBe('/plan')
    wrapper.unmount()
  })
})
