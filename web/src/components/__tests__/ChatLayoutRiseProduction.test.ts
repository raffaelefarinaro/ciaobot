// @vitest-environment jsdom
//
// #3086: the production ChatPanel inside the production ChatLayout. Only the
// panel's own dependencies are mocked (as in ChatPanelRiseContract.test.ts); the
// layout's chunk loader resolves to the real ChatPanel.vue. The branch swap
// (pinned file on the same chat) replaces the main panel during a running rise,
// and every className write is recorded, so a class written on the replacement
// root is caught even when it is gone by the time the DOM is read.

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { recordClassWrites } from '../../lib/testing/classWrites'

const apiGet = vi.hoisted(() => vi.fn((_path: string): Promise<unknown> => Promise.reject(new Error('no server'))))
const apiPost = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiPatch = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiDel = vi.hoisted(() => vi.fn(() => Promise.resolve({})))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel },
}))

vi.mock('../../router', () => ({
  router: { push: vi.fn(), currentRoute: { value: { params: {} } } },
}))

// The layout's chunk loaders: the chat panel is the real component.
vi.mock('../../lib/chatComponents', () => ({
  loadChatPanel: () => import('../ChatPanel.vue'),
  loadPinnedFilePanel: vi.fn(async () => ({ default: { template: '<div />' } })),
  loadFileViewerModal: vi.fn(async () => ({ default: { template: '<div />' } })),
  prefetchChatPanelForRoute: vi.fn(),
}))

const NoopStub = vi.hoisted(() => ({ name: 'NoopStub', render: () => null }))
const CommentPopoverStub = vi.hoisted(() => ({
  name: 'CommentPopoverStub',
  render: () => null,
  methods: {
    close: () => {},
    clearPendingClose: () => {},
    onTargetOver: () => {},
    onTargetOut: () => {},
    pinFromEvent: () => null,
    show: () => {},
  },
}))
vi.mock('../ModelSelector.vue', () => ({ default: NoopStub }))
vi.mock('../ChatCommentPopover.vue', () => ({ default: CommentPopoverStub }))
vi.mock('../CommentComposePopover.vue', () => ({ default: NoopStub }))

const EmptyStub = defineComponent({ name: 'EmptyStub', setup: () => () => h('div') })

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

describe('production ChatLayout + ChatPanel: the rise never reaches a replacement root (#3086)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    vi.setSystemTime(new Date('2026-11-20T10:00:00Z'))
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    localStorage.clear()
    vi.clearAllMocks()
    apiGet.mockImplementation(() => Promise.reject(new Error('no server')))
  })

  afterEach(() => {
    vi.useRealTimers()
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  test('a branch swap during a running rise writes chat-rise only to the original root', async () => {
    // Module loaded before mounting, so the loader resolves at once (a warm chunk).
    await import('../ChatPanel.vue')
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
        { path: '/settings/:tab?', component: EmptyStub },
      ],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = [{
      chat_id: 'chat-1', project_id: 'project-1', title: 'rise production', archived: false,
      provider: 'claude', mode: 'bypass',
    }] as unknown as typeof store.chats
    store.workspaces = [{ name: 'personal', vault_root: '', default_provider: 'claude' }]
    store.activeWorkspace = 'personal'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
    // The pin is reactive, so the layout's pinned-file computed follows it.
    const pinnedPath = ref<string | undefined>(undefined)
    vi.spyOn(store, 'pinnedFileFor').mockImplementation(id => (id === 'chat-1' ? pinnedPath.value : undefined))

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      attachTo: document.body,
      global: {
        plugins: [router],
        stubs: {
          ProjectSidebar: EmptyStub, ProjectView: EmptyStub, SchedulePanel: EmptyStub,
          SettingsView: EmptyStub, FileViewerModal: EmptyStub, PinnedFilePanel: EmptyStub,
          HomeRecentChats: EmptyStub, Teleport: true,
        },
      },
    })
    await flushPromises()
    // Past the 800 ms bootstrap suppression.
    vi.advanceTimersByTime(900)

    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('.chat-panel.chat-rise').exists()).toBe(true)
    const originalRoot = wrapper.find('.chat-panel').element

    const log = recordClassWrites()
    try {
      // Pinning a file on the same chat swaps the main branch in one render, while the rise runs.
      pinnedPath.value = 'notes/plan.md'
      await flushPromises()
      await flushPromises()
      expect(wrapper.find('.chat-split').exists()).toBe(true)
      const replacement = wrapper.find('.chat-panel').element
      expect(replacement).not.toBe(originalRoot)
      expect(log.writes.filter(w => w.el !== originalRoot && w.value.includes('chat-rise'))).toEqual([])
    } finally {
      log.restore()
    }
    wrapper.unmount()
  })
})
