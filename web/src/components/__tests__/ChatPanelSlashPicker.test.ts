// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import { readChatDraft } from '../../lib/chatDrafts'
import type { ChatInfo, ProjectInfo, SlashCommand } from '../../lib/types'
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

function command(name: string): SlashCommand {
  return { name, description: `run ${name}`, argument_hint: '', source: 'user', path: `${name}.md` }
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

async function mountPanel(): Promise<VueWrapper> {
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
    if (path.startsWith('/api/commands')) {
      return Promise.resolve({ commands: [command('review')], skills: [] }) as never
    }
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
  return wrapper
}

function textareaValue(wrapper: VueWrapper): string {
  return (wrapper.get('textarea.chat-input').element as HTMLTextAreaElement).value
}

describe('ChatPanel slash-command picker', () => {
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

  it('Escape dismisses the picker without touching the draft', async () => {
    // The trigger is caret-local, so the picker opens MID-message. Escape used
    // to clear inputText, which wiped a whole paragraph because its last word
    // happened to start with a slash — and the draft watcher persisted the empty
    // string, so reopening the chat could not bring it back.
    const wrapper = await mountPanel()
    const textarea = wrapper.get('textarea.chat-input')
    const draft = 'summarise the release and then run /rev'

    await textarea.setValue(draft)
    await nextTick()
    expect(wrapper.find('.commands-picker').exists()).toBe(true)

    await textarea.trigger('keydown', { key: 'Escape' })
    await nextTick()

    expect(wrapper.find('.commands-picker').exists()).toBe(false)
    expect(textareaValue(wrapper)).toBe(draft)
    expect(readChatDraft('chat-1')).toBe(draft)
    // The picker claimed the key, so the chat stays open.
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })

  it('Escape still closes the chat when no picker is open', async () => {
    const wrapper = await mountPanel()
    const textarea = wrapper.get('textarea.chat-input')

    await textarea.setValue('a plain draft')
    await nextTick()
    expect(wrapper.find('.commands-picker').exists()).toBe(false)

    await textarea.trigger('keydown', { key: 'Escape' })

    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(textareaValue(wrapper)).toBe('a plain draft')
    wrapper.unmount()
  })
})
