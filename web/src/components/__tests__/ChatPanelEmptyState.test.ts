// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount } from '@vue/test-utils'
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
const chat: ChatInfo = {
  chat_id: 'chat-1', project_id: 'project-1', title: 'chat-1', model: 'sonnet',
  provider: 'claude', mode: 'normal', session_id: '', created_at: '', archived: false,
}

async function mountPanel() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [project]
  store.chats = [chat]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [] }
  store.bootstrapped = true
  vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })
  const wrapper = shallowMount(ChatPanel, {
    global: {
      plugins: [pinia],
      stubs: { PaneHeader: PaneHeaderStub, SubagentPanel: ChildStub, RouterLink: ChildStub, ChatCommentPopover: ChatCommentPopoverStub, CommentComposePopover: ChildStub },
    },
  })
  await flushPromises()
  return { wrapper, store }
}

describe('ChatPanel empty state while history loads', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => vi.restoreAllMocks())

  it('shows the skeleton, not "Start with a request", for an empty transcript that is loading mid-turn', async () => {
    const { wrapper, store } = await mountPanel()
    // A history fetch that never answers keeps the loading flag up.
    vi.mocked(api.get).mockImplementation(() => new Promise(() => {}) as never)
    void store.loadMessages('chat-1')
    store.projectStreaming['chat-1'] = true
    await flushPromises()

    expect(wrapper.find('.chat-empty-state').exists()).toBe(false)
    expect(wrapper.find('.history-skeleton-stack').exists()).toBe(true)
    wrapper.unmount()
  })

  it('still shows the empty state once history has loaded with nothing in it', async () => {
    const { wrapper } = await mountPanel()
    expect(wrapper.find('.history-skeleton-stack').exists()).toBe(false)
    expect(wrapper.find('.chat-empty-state').exists()).toBe(true)
    wrapper.unmount()
  })
})
