// @vitest-environment jsdom

// The lazy `ChatPanel` chunk is the one piece of the pane that arrives late:
// the first chat opened after an update has to fetch it, and `defineAsyncComponent`
// renders nothing while the loader is in flight (#1651). The skeleton is the
// loading component, and it is on screen from the first frame (`delay: 0`).
//
// These tests live in their own file because they replace the chunk loader
// with one that never resolves, which is a module-level mock: doing it in
// `ChatLayout.test.ts` would change the chunk for every other case there.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

// A loader that never settles: the chunk is "in flight" for the whole test,
// which is the window the skeleton exists to cover.
const neverResolvingLoad = vi.fn(() => new Promise<never>(() => {}))

vi.mock('../../lib/chatComponents', () => ({
  loadChatPanel: () => neverResolvingLoad(),
  loadPinnedFilePanel: vi.fn(async () => ({ default: { template: '<div />' } })),
  loadFileViewerModal: vi.fn(async () => ({ default: { template: '<div />' } })),
  prefetchChatPanelForRoute: vi.fn(),
}))

const EmptyStub = defineComponent({
  name: 'EmptyStub',
  setup() {
    return () => h('div')
  },
})

describe('ChatLayout while the chat chunk is in flight', () => {
  class MemoryStorage {
    private values = new Map<string, string>()
    getItem(key: string): string | null { return this.values.get(key) ?? null }
    setItem(key: string, value: string): void { this.values.set(key, value) }
    removeItem(key: string): void { this.values.delete(key) }
    clear(): void { this.values.clear() }
  }

  let router: ReturnType<typeof createRouter>

  beforeEach(async () => {
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })

    router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
    })
    await router.push('/chat/chat-1')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Test chat' }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  // The acceptance criterion: the placeholder is on screen from the first
  // frame. The assertion runs synchronously after `mount`, with no
  // `await nextTick()` and no `await flushPromises()`: Vue's default 200 ms
  // delay would still be waiting out its timer at that point, so the
  // placeholder would not be there yet and this test would fail.
  it('paints the pane skeleton on the first render tick', async () => {
    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      global: {
        plugins: [router],
        stubs: {
          ProjectSidebar: EmptyStub, ProjectView: EmptyStub, SchedulePanel: EmptyStub,
          SettingsView: EmptyStub, FileViewerModal: EmptyStub, PinnedFilePanel: EmptyStub,
          PaneHeader: EmptyStub, HomeRecentChats: EmptyStub,
        },
      },
    })

    const status = wrapper.find('[role="status"][aria-label="Loading conversation"]')
    expect(status.exists()).toBe(true)
    wrapper.unmount()
  })

  // #1651 review: the template ref on `<ChatPanel>` resolves to the loading
  // component while the chunk is in flight, so the ref is no longer a
  // ChatPanel instance and has none of its methods. The global shortcuts
  // reach for two of them; guarding the ref but not the method threw
  // "is not a function" in exactly the window the skeleton covers.
  it('keeps the archive and model-picker shortcuts inert while the chunk loads', async () => {
    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      global: {
        plugins: [router],
        stubs: {
          ProjectSidebar: EmptyStub, ProjectView: EmptyStub, SchedulePanel: EmptyStub,
          SettingsView: EmptyStub, FileViewerModal: EmptyStub, PinnedFilePanel: EmptyStub,
          PaneHeader: EmptyStub, HomeRecentChats: EmptyStub,
        },
      },
    })

    // jsdom reports a throw inside a listener as an error event on the
    // window; catching it here turns the failure into an assertion on this
    // test instead of an uncaught exception the runner attributes to
    // whichever case happened to be running.
    const thrown: Error[] = []
    const onError = (event: ErrorEvent) => {
      event.preventDefault()
      if (event.error instanceof Error) thrown.push(event.error)
    }
    window.addEventListener('error', onError)

    // Alt+Backspace archives the open chat, Alt+M opens the model picker.
    const archive = new KeyboardEvent('keydown', { altKey: true, key: 'Backspace', code: 'Backspace', bubbles: true, cancelable: true })
    window.dispatchEvent(archive)
    const model = new KeyboardEvent('keydown', { altKey: true, key: 'm', code: 'KeyM', bubbles: true, cancelable: true })
    window.dispatchEvent(model)

    window.removeEventListener('error', onError)
    expect(thrown.map(e => e.message)).toEqual([])
    wrapper.unmount()
  })
})
