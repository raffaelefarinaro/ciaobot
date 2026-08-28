// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import { readChatDraft, readSentPromptHistory, recordSentPrompt } from '../../lib/chatDrafts'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import ChatPanel from '../ChatPanel.vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

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
  return { wrapper, sendMessage }
}

function textareaValue(wrapper: VueWrapper): string {
  return (wrapper.get('textarea.chat-input').element as HTMLTextAreaElement).value
}

describe('ChatPanel sent-prompt history', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('walks older and newer prompts, then restores the pre-recall draft', async () => {
    recordSentPrompt('chat-1', 'first prompt')
    recordSentPrompt('chat-1', 'latest prompt')
    const { wrapper } = await mountPanel()
    const textarea = wrapper.get('textarea.chat-input')

    await textarea.trigger('keydown', { key: 'ArrowUp' })
    expect(textareaValue(wrapper)).toBe('latest prompt')
    await textarea.trigger('keydown', { key: 'ArrowUp' })
    expect(textareaValue(wrapper)).toBe('first prompt')
    await textarea.trigger('keydown', { key: 'ArrowDown' })
    expect(textareaValue(wrapper)).toBe('latest prompt')
    await textarea.trigger('keydown', { key: 'ArrowDown' })
    expect(textareaValue(wrapper)).toBe('')
    expect(readChatDraft('chat-1')).toBe('')
    wrapper.unmount()
  })

  it('keeps normal cursor editing when history is not active and text is non-empty', async () => {
    recordSentPrompt('chat-1', 'sent prompt')
    const { wrapper } = await mountPanel()
    const textarea = wrapper.get('textarea.chat-input')

    await textarea.setValue('current draft')
    await textarea.trigger('keydown', { key: 'ArrowUp' })
    expect(textareaValue(wrapper)).toBe('current draft')
    await textarea.trigger('keydown', { key: 'ArrowDown' })
    expect(textareaValue(wrapper)).toBe('current draft')
    wrapper.unmount()
  })

  it('records a trimmed prompt when the composer sends', async () => {
    const { wrapper, sendMessage } = await mountPanel()
    sendMessage.mockImplementation(() => true)

    await wrapper.get('textarea.chat-input').setValue('  sent prompt  ')
    await wrapper.get('button.send-btn').trigger('click')

    expect(readSentPromptHistory('chat-1')).toEqual(['sent prompt'])
    wrapper.unmount()
  })
})
