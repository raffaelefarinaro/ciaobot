// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
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

function makeChat(archived = false): ChatInfo {
  return {
    chat_id: 'chat-1',
    project_id: 'project-1',
    title: 'chat-1',
    model: 'sonnet',
    provider: 'claude',
    mode: 'normal',
    session_id: '',
    created_at: '',
    archived,
  }
}

class MemoryStorage {
  private values = new Map<string, string>()

  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

type Panel = VueWrapper & { vm: { handleSendShortcut: () => boolean } }

async function mountPanel(archived = false): Promise<{
  wrapper: Panel
  store: ReturnType<typeof useProjectStore>
  sendMessage: ReturnType<typeof vi.spyOn>
}> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat(archived)]
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

  const sendMessage = vi.spyOn(store, 'sendMessage').mockImplementation(() => true)
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
  }) as Panel
  await flushPromises()
  return { wrapper, store, sendMessage }
}

// Cmd/Ctrl+Enter arrives here from ChatLayout when focus is not in the
// composer -- which is where it sits right after attaching an image or a
// comment, the exact moment the user presses the chord again to send.
describe('ChatPanel send shortcut', () => {
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

  it('sends an attachments-only message with no typed text', async () => {
    const { wrapper, store, sendMessage } = await mountPanel()
    store.pendingImages = ['chat-1/img.png']

    expect(wrapper.vm.handleSendShortcut()).toBe(true)
    expect(sendMessage).toHaveBeenCalledOnce()
    expect(sendMessage.mock.calls[0][1]).toBe('[Image 1]')
    wrapper.unmount()
  })

  it('sends the typed draft too', async () => {
    const { wrapper, sendMessage } = await mountPanel()
    await wrapper.get('textarea.chat-input').setValue('hello')

    expect(wrapper.vm.handleSendShortcut()).toBe(true)
    expect(sendMessage.mock.calls[0][1]).toBe('hello')
    wrapper.unmount()
  })

  it('declines with an empty composer so the key keeps its usual meaning', async () => {
    const { wrapper, sendMessage } = await mountPanel()

    expect(wrapper.vm.handleSendShortcut()).toBe(false)
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('declines in an archived chat', async () => {
    const { wrapper, store, sendMessage } = await mountPanel(true)
    store.pendingImages = ['chat-1/img.png']

    expect(wrapper.vm.handleSendShortcut()).toBe(false)
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
