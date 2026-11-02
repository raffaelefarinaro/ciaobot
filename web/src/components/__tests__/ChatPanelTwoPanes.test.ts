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
    return () => h('header', [slots.title?.(), h('div', slots.actions?.())])
  },
})
const ChatCommentPopoverStub = defineComponent({
  name: 'ChatCommentPopoverStub',
  setup(_, { expose }) {
    expose({
      openId: null, close: vi.fn(), clearPendingClose: vi.fn(), onTargetOver: vi.fn(),
      onTargetOut: vi.fn(), pinFromEvent: vi.fn(), show: vi.fn(),
    })
    return () => h('div')
  },
})
const ChildStub = defineComponent({ name: 'ChildStub', setup: () => () => h('div') })

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

const project: ProjectInfo = {
  project_id: 'project-1', name: 'General', workspace: 'personal',
  context: '', created_at: '', order: 0, vault_folder: '',
}
function chat(id: string): ChatInfo {
  return {
    chat_id: id, project_id: 'project-1', title: id, model: 'sonnet',
    provider: 'claude', mode: 'normal', session_id: '', created_at: '', archived: false,
  } as ChatInfo
}

const mounted: VueWrapper[] = []

function mountTwoPanes() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [project]
  store.chats = [chat('chat-1'), chat('chat-2')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [], 'chat-2': [] }
  store.bootstrapped = true
  vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })
  const stubs = {
    PaneHeader: PaneHeaderStub, SubagentPanel: ChildStub, RouterLink: ChildStub,
    ChatCommentPopover: ChatCommentPopoverStub, CommentComposePopover: ChildStub,
  }
  const a = shallowMount(ChatPanel, {
    attachTo: document.body,
    props: { chatId: 'chat-1' },
    global: { plugins: [pinia], stubs },
  })
  const b = shallowMount(ChatPanel, {
    attachTo: document.body,
    props: { chatId: 'chat-2', beside: true, inactive: true },
    global: { plugins: [pinia], stubs },
  })
  mounted.push(a, b)
  return { a, b, store }
}

describe('two ChatPanels on screen', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
  })

  afterEach(() => {
    for (const w of mounted.splice(0)) w.unmount()
    vi.restoreAllMocks()
  })

  it('each pane shows its own staged attachments', async () => {
    const { a, b, store } = mountTwoPanes()
    store.addPendingImageRefs('chat-2', ['b.png'])
    await flushPromises()

    expect(b.findAll('.image-preview')).toHaveLength(1)
    expect(a.findAll('.image-preview')).toHaveLength(0)
  })

  it('each pane shows its own history load', async () => {
    const { a, b, store } = mountTwoPanes()
    vi.spyOn(api, 'get').mockImplementation((path: string) => {
      if (path.includes('/messages')) return new Promise(() => {}) as never
      return Promise.resolve([]) as never
    })
    void store.loadMessages('chat-2')
    await flushPromises()

    expect(b.find('.history-skeleton-stack').exists()).toBe(true)
    expect(a.find('.history-skeleton-stack').exists()).toBe(false)
  })

  it('the second pane prefixes its element ids', async () => {
    const { a, b } = mountTwoPanes()
    await flushPromises()

    expect(b.find('[aria-controls="beside-chat-work-rail"]').exists()).toBe(true)
    expect(a.find('[aria-controls="chat-work-rail"]').exists()).toBe(true)
    expect(document.querySelectorAll('#chat-work-rail').length).toBeLessThanOrEqual(1)
  })

  it('an inactive pane does not take the keyboard when it mounts', async () => {
    const { a } = mountTwoPanes()
    await flushPromises()

    expect(document.activeElement).toBe(a.find('textarea.chat-input').element)
  })

  it('a staged chat comment lands in its own chat', () => {
    const { store } = mountTwoPanes()

    store.addPendingChatComment('chat-2', { selection: 's', comment: 'c' } as never)

    expect(store.pendingChatCommentsFor('chat-2')).toHaveLength(1)
    expect(store.pendingChatCommentsFor('chat-1')).toHaveLength(0)
  })
})
