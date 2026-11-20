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
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, onBeforeUnmount, onMounted } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { useTerminalStore } from '../../stores/terminal'
import { api } from '../../lib/api'

// A loader that never settles: the chunk is "in flight" for the whole test,
// which is the window the skeleton exists to cover. The rise test swaps in one
// resolution for a single mount.
const neverResolvingLoad = vi.fn((): Promise<unknown> => new Promise(() => {}))

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
        { path: '/settings/:tab?', component: EmptyStub },
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

  it('opens the lazy workspace pane on Settings without stealing focus or reopening on navigation', async () => {
    await router.push('/settings/providers')
    vi.spyOn(api, 'get').mockImplementation(async (path: string) => (path.endsWith('/status')
      ? { available: true, reason: '', platform: 'test', host: 'test', unlock_expires_in: 900, detached_ttl_seconds: 900 }
      : { sessions: Array.from({ length: 4 }, (_, i) => ({
        session_id: `s${i}`, folder: '/workspace', cols: 80, rows: 24, seq: 0,
        attached: false, exited: true, exit_code: 0,
      })) }) as never)
    const del = vi.spyOn(api, 'del')
    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      attachTo: document.body,
      global: { plugins: [router], stubs: {
        ProjectSidebar: EmptyStub, ProjectView: EmptyStub, SchedulePanel: EmptyStub,
        SettingsView: EmptyStub, FileViewerModal: EmptyStub, PinnedFilePanel: EmptyStub,
        PaneHeader: EmptyStub, HomeRecentChats: EmptyStub,
      } },
    })
    const opener = document.createElement('button')
    const other = document.createElement('button')
    document.body.append(opener, other)
    opener.focus()
    const terminal = useTerminalStore()
    const opening = terminal.openWorkspaceForSettings('personal')
    other.focus()
    await opening
    await flushPromises()
    await vi.waitFor(() => expect(wrapper.find('.terminal-pane--open').exists()).toBe(true))
    expect(document.activeElement).toBe(other)
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(terminal.visible).toBe(false)
    expect(del).not.toHaveBeenCalled()
    await router.push('/settings/providers')
    await flushPromises()
    expect(terminal.visible).toBe(false)
    opener.focus()
    await terminal.openWorkspaceForSettings('personal')
    await flushPromises()
    expect(document.activeElement).toBe(wrapper.get('.tp-close').element)
    await wrapper.get('.tp-close').trigger('click')
    await flushPromises()
    expect(document.activeElement).toBe(opener)
    wrapper.unmount()
    opener.remove()
    other.remove()
    terminal.dispose()
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

describe('workspace terminal lazy mount', () => {
  afterEach(() => {
    vi.doUnmock('../TerminalPane.vue')
    vi.restoreAllMocks()
    vi.resetModules()
    document.body.innerHTML = ''
  })

  it.each(['moved', 'opener', 'disconnected', 'superseded', 'navigation'] as const)(
    'uses the pre-import opener when mount is delayed (%s)', async (scenario) => {
      vi.resetModules()
      let release!: () => void
      let requested!: () => void
      const gate = new Promise<void>(resolve => { release = resolve })
      const loading = new Promise<void>(resolve => { requested = resolve })
      vi.doMock('../TerminalPane.vue', async (original) => {
        requested()
        await gate
        return await original()
      })
      const [pinia, vue, vueRouter, testUtils, projects, tasks, terminalModule, apiModule] = await Promise.all([
        import('pinia'), import('vue'), import('vue-router'), import('@vue/test-utils'),
        import('../../stores/projects'), import('../../stores/tasks'), import('../../stores/terminal'), import('../../lib/api'),
      ])
      pinia.setActivePinia(pinia.createPinia())
      const empty = vue.defineComponent({ setup: () => () => vue.h('div') })
      const router = vueRouter.createRouter({
        history: vueRouter.createMemoryHistory(),
        routes: [{ path: '/settings/:tab?', component: empty }, { path: '/chat/:chatId?', component: empty }],
      })
      await router.push('/settings/providers')
      await router.isReady()
      const projectStore = projects.useProjectStore()
      projectStore.projects = [{ project_id: 'p', name: 'General', workspace: 'personal' }] as unknown as typeof projectStore.projects
      projectStore.chats = [{ chat_id: 'chat-1', project_id: 'p', title: 'Chat' }] as unknown as typeof projectStore.chats
      projectStore.activeChatId = 'chat-1'
      projectStore.bootstrapped = true
      vi.spyOn(projectStore, 'fetchAll').mockResolvedValue()
      vi.spyOn(tasks.useTaskStore(), 'fetchSchedules').mockResolvedValue()
      vi.spyOn(apiModule.api, 'get').mockImplementation(async (path: string) => (path.endsWith('/status')
        ? { available: true, reason: '', platform: 'test', host: 'test', unlock_expires_in: 900, detached_ttl_seconds: 900 }
        : { sessions: Array.from({ length: 4 }, (_, i) => ({
          session_id: `s${i}`, folder: '/workspace', cols: 80, rows: 24, seq: 0,
          attached: false, exited: true, exit_code: 0,
        })) }) as never)
      const { default: Layout } = await import('../ChatLayout.vue')
      const wrapper = testUtils.mount(Layout, { attachTo: document.body, global: {
        plugins: [router], stubs: {
          ProjectSidebar: empty, ProjectView: empty, SchedulePanel: empty, SettingsView: empty,
          FileViewerModal: empty, PinnedFilePanel: empty, PaneHeader: empty, HomeRecentChats: empty,
        },
      } })
      const opener = document.createElement('button')
      const other = document.createElement('button')
      document.body.append(opener, other)
      opener.focus()
      const store = terminalModule.useTerminalStore()
      const opening = store.openWorkspaceForSettings('personal')
      await loading
      if (scenario === 'moved') other.focus()
      if (scenario === 'disconnected') { opener.remove(); other.focus() }
      if (scenario === 'superseded') {
        store.hide({ fromChatSwitch: true })
        other.focus()
      }
      if (scenario === 'navigation') {
        await router.push('/chat/chat-1')
        other.focus()
      }
      release()
      await opening
      await vi.waitFor(() => expect(wrapper.find('.terminal-pane').exists()).toBe(true))
      await testUtils.flushPromises()
      if (scenario === 'opener') {
        expect(document.activeElement).toBe(wrapper.get('.tp-close').element)
        await wrapper.get('.tp-close').trigger('click')
        await testUtils.flushPromises()
        expect(document.activeElement).toBe(opener)
      } else {
        expect(document.activeElement).toBe(other)
      }
      if (scenario === 'navigation') expect(store.visible).toBe(false)
      wrapper.unmount()
      store.dispose()
    },
  )
})

// #3086: the 340 ms Home-to-chat rise starts when the real main panel mounts,
// not when the chat is selected. The stub follows the ChatPanel contract: a
// per-instance token, `pane-mounted`/`pane-unmounted` from its real hooks, and
// `chat-rise` on its own root only when `riseForInstance` names its token. The
// lifecycle matrix (ordering, cancellation, replacement, navigation failure) is
// in ChatLayoutRiseLifecycle.test.ts; the production ChatPanel is in
// ChatPanelRiseContract.test.ts.
describe('chat rise waits for the real chat panel', () => {
  const RealPanelStub = defineComponent({
    name: 'RealPanelStub',
    props: {
      chatId: { type: String, required: true },
      beside: { type: Boolean, default: false },
      riseForInstance: { type: Symbol, default: null },
    },
    emits: ['pane-mounted', 'pane-unmounted'],
    setup(props, { emit }) {
      const instance = Symbol('chat-pane')
      const identity = () => ({ chatId: props.chatId, instance, beside: props.beside })
      onMounted(() => emit('pane-mounted', identity()))
      onBeforeUnmount(() => emit('pane-unmounted', identity()))
      return () => h('div', { class: ['chat-panel', { 'chat-rise': !props.beside && props.riseForInstance === instance }] })
    },
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  // delay 0: the chunk is ready at once. delay 1000: the chunk outlives the
  // 340 ms window, which is the case that used to leave the panel unmarked.
  it.each([0, 1000])('rises the real panel for the full window after a %i ms chunk wait', async (delay) => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    vi.setSystemTime(new Date('2026-11-20T10:00:00Z'))
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

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
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Test chat' }] as unknown as typeof store.chats
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()

    let releaseChunk!: (panel: typeof RealPanelStub) => void
    neverResolvingLoad.mockImplementationOnce(() => new Promise(resolve => { releaseChunk = resolve }))

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
    await flushPromises()

    // Past the 800 ms bootstrap suppression, then select the chat from Home.
    vi.advanceTimersByTime(900)
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()

    vi.advanceTimersByTime(delay)
    await flushPromises()
    // Still pending past the window: the skeleton is on screen and nothing is marked.
    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(true)
    expect(wrapper.find('.chat-panel').exists()).toBe(false)

    releaseChunk(RealPanelStub)
    await flushPromises()

    expect(wrapper.find('.chat-panel').exists()).toBe(true)
    expect(wrapper.find('.chat-panel.chat-rise').exists()).toBe(true)
    vi.advanceTimersByTime(339)
    await flushPromises()
    expect(wrapper.find('.chat-panel.chat-rise').exists()).toBe(true)
    vi.advanceTimersByTime(1)
    await flushPromises()
    expect(wrapper.find('.chat-panel.chat-rise').exists()).toBe(false)
    wrapper.unmount()
  })
})
