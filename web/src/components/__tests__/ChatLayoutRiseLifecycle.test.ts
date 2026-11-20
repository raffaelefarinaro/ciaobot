// @vitest-environment jsdom

// #3086: the lifecycle of a chat's arrival rise. The rise is armed by selecting
// a chat from a non-chat page, and it starts only when the real main ChatPanel
// for that chat has mounted and the route has reached the chat. These tests
// drive ChatLayout through its real route and selection edges. The panel is a
// stub that follows the ChatPanel contract (per-instance token, lifecycle events
// from its real hooks, `chat-rise` on its own root only for its own token).
// Production ChatPanel is covered in ChatPanelRiseContract.test.ts.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, getCurrentInstance, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

const neverResolvingLoad = vi.fn((): Promise<unknown> => new Promise(() => {}))

vi.mock('../../lib/chatComponents', () => ({
  loadChatPanel: () => neverResolvingLoad(),
  loadPinnedFilePanel: vi.fn(async () => ({ default: { template: '<div />' } })),
  loadFileViewerModal: vi.fn(async () => ({ default: { template: '<div />' } })),
  prefetchChatPanelForRoute: vi.fn(),
}))

const EmptyStub = defineComponent({ name: 'EmptyStub', setup: () => () => h('div') })

interface PaneIdentity { chatId: string; instance: symbol; beside: boolean }
interface PaneHandle {
  identity: PaneIdentity
  /** The layout's own listeners, as the panel received them: calling them is the real handler. */
  onPaneMounted: (pane: PaneIdentity) => void
  onPaneUnmounted: (pane: PaneIdentity) => void
}
/** Every panel instance the layout mounted, in mount order. */
let panes: PaneHandle[] = []

const PanelStub = defineComponent({
  name: 'PanelStub',
  props: {
    chatId: { type: String, required: true },
    beside: { type: Boolean, default: false },
    riseForInstance: { type: Symbol, default: null },
  },
  emits: ['pane-mounted', 'pane-unmounted'],
  setup(props, { emit }) {
    const instance = Symbol('chat-pane')
    const identity = (): PaneIdentity => ({ chatId: props.chatId, instance, beside: props.beside })
    const listeners = getCurrentInstance()?.vnode.props ?? {}
    panes.push({
      identity: identity(),
      onPaneMounted: pane => (listeners.onPaneMounted as ((p: PaneIdentity) => void) | undefined)?.(pane),
      onPaneUnmounted: pane => (listeners.onPaneUnmounted as ((p: PaneIdentity) => void) | undefined)?.(pane),
    })
    onMounted(() => emit('pane-mounted', identity()))
    onBeforeUnmount(() => emit('pane-unmounted', identity()))
    return () => h('div', { class: ['chat-panel', { 'chat-rise': !props.beside && props.riseForInstance === instance }] })
  },
})

const stubs = {
  ProjectSidebar: EmptyStub, ProjectView: EmptyStub, SchedulePanel: EmptyStub,
  SettingsView: EmptyStub, FileViewerModal: EmptyStub, PinnedFilePanel: EmptyStub,
  PaneHeader: EmptyStub, HomeRecentChats: EmptyStub,
}

function setReducedMotion(on: boolean): void {
  window.matchMedia = vi.fn((query: string) => ({
    matches: on && query.includes('prefers-reduced-motion: reduce'),
    media: query, onchange: null,
    addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

/** Holds the first chunk load until the returned function is called. */
function holdChunk(): () => void {
  let resolve!: (panel: typeof PanelStub) => void
  const chunk = new Promise<typeof PanelStub>(r => { resolve = r })
  neverResolvingLoad.mockImplementationOnce(() => chunk)
  return () => resolve(PanelStub)
}

/** The first chunk load resolves at once (a warm chunk). */
function warmChunk(): void {
  neverResolvingLoad.mockImplementationOnce(() => Promise.resolve(PanelStub))
}

function chatRow(id: string) {
  return { chat_id: id, project_id: 'project-1', title: `Chat ${id}`, archived: false }
}

async function mountRise(rows: string[] = ['chat-1', 'chat-2', 'chat-3']) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: EmptyStub },
      { path: '/chat/:chatId?', component: EmptyStub },
      { path: '/settings/:tab?', component: EmptyStub },
      { path: '/tasks', component: EmptyStub },
    ],
  })
  await router.push('/')
  await router.isReady()

  const store = useProjectStore()
  store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
  store.chats = rows.map(chatRow) as unknown as typeof store.chats
  store.bootstrapped = true
  vi.spyOn(store, 'fetchAll').mockResolvedValue()
  vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()

  const { default: ChatLayout } = await import('../ChatLayout.vue')
  const wrapper = mount(ChatLayout, { global: { plugins: [router], stubs } })
  await flushPromises()
  // Past the 800 ms bootstrap suppression.
  vi.advanceTimersByTime(900)
  return { router, store, wrapper }
}

const panelCount = (w: { findAll: (s: string) => unknown[] }) => w.findAll('.chat-panel').length
const risingCount = (w: { findAll: (s: string) => unknown[] }) => w.findAll('.chat-panel.chat-rise').length

describe('chat rise lifecycle (#3086)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    vi.setSystemTime(new Date('2026-11-20T10:00:00Z'))
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    setReducedMotion(false)
    // A once-queued loader left unused by one case must not feed the next.
    neverResolvingLoad.mockReset()
    neverResolvingLoad.mockImplementation(() => new Promise(() => {}))
    panes = []
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('ordering: a panel mounted on the origin route waits, then rises on the committed chat route', async () => {
    warmChunk()
    const { router, store, wrapper } = await mountRise()
    // The selection moves while the URL is still Home: the warm panel mounts first.
    store.activeChatId = 'chat-1'
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    expect(panelCount(wrapper)).toBe(1)
    vi.advanceTimersByTime(900)
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)

    await router.push('/chat/chat-1')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)
    vi.advanceTimersByTime(339)
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)
    vi.advanceTimersByTime(1)
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('ordering: leaving for Home while the import is held cancels the rise', async () => {
    const release = holdChunk()
    const { router, store, wrapper } = await mountRise()
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    await router.push('/')
    await flushPromises()

    release()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it.each([0, 1000])('route before catalog row: the arrival waits for the row and rises on the %i ms-delayed panel', async (delay) => {
    const release = holdChunk()
    const { router, store, wrapper } = await mountRise(['chat-2'])
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/chat/chat-1')

    store.chats = [chatRow('chat-1'), chatRow('chat-2')] as unknown as typeof store.chats
    await flushPromises()
    vi.advanceTimersByTime(delay)
    await flushPromises()
    release()
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)
    vi.advanceTimersByTime(340)
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it.each(['settings', 'other-chat'] as const)('cancelled by %s, the abandoned panel never rises, and a later eligible arrival still does', async (kind) => {
    const release = holdChunk()
    const { router, store, wrapper } = await mountRise()
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    if (kind === 'settings') {
      await router.push('/settings/providers')
    } else {
      store.activeChatId = 'chat-2'
      await router.push('/chat/chat-2')
    }
    await flushPromises()
    release()
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)

    // A fresh eligible selection from nothing: the cancellation did not poison it.
    store.activeChatId = null
    await flushPromises()
    store.activeChatId = 'chat-3'
    await router.push('/chat/chat-3')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)
    wrapper.unmount()
  })

  it('unmounting the layout while the import is held leaves nothing to rise', async () => {
    const release = holdChunk()
    const { router, store, wrapper } = await mountRise()
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    wrapper.unmount()
    expect(() => release()).not.toThrow()
    await flushPromises()
    expect(panes).toHaveLength(0)
  })

  it('same-id replacement: the replaced instance cannot start the rise, the mounted replacement can', async () => {
    warmChunk()
    const { router, store, wrapper } = await mountRise()
    store.activeChatId = 'chat-1'
    await flushPromises()
    const first = panes[panes.length - 1]
    // The main branch is replaced before the route commits: token1 unmounts.
    store.activeChatId = null
    await flushPromises()
    store.activeChatId = 'chat-1'
    await flushPromises()
    const second = panes[panes.length - 1]
    expect(second.identity.instance).not.toBe(first.identity.instance)

    // Old token1 unmounting after token2 mounted cannot erase token2's readiness.
    first.onPaneUnmounted(first.identity)
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)
    expect(wrapper.find('.chat-panel.chat-rise').exists()).toBe(true)
    wrapper.unmount()
  })

  it('a same-id main replacement in the render that swaps the branch never inherits a running rise', async () => {
    warmChunk()
    const { router, store, wrapper } = await mountRise()
    // The pin is reactive, so the layout's pinned-file computed follows it.
    const pinnedPath = ref<string | undefined>(undefined)
    vi.spyOn(store, 'pinnedFileFor').mockImplementation(id => (id === 'chat-1' ? pinnedPath.value : undefined))
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)
    const original = panes[panes.length - 1]

    // Pinning a file on the same chat swaps the main branch in one render, while the rise runs.
    pinnedPath.value = 'notes/plan.md'
    await flushPromises()

    expect(wrapper.find('.chat-split').exists()).toBe(true)
    const replacement = panes[panes.length - 1]
    expect(replacement.identity.instance).not.toBe(original.identity.instance)
    expect(wrapper.find('.chat-panel.chat-rise').exists()).toBe(false)
    wrapper.unmount()
  })

  it('beside-pane mounted signals do not satisfy main readiness', async () => {
    warmChunk()
    const { router, store, wrapper } = await mountRise()
    store.activeChatId = 'chat-1'
    await flushPromises()
    const main = panes[panes.length - 1]
    // Main readiness is dropped, then a beside signal for the same chat arrives.
    main.onPaneUnmounted(main.identity)
    main.onPaneMounted({ chatId: 'chat-1', instance: Symbol('beside'), beside: true })
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('a rise running for one chat is cancelled by another chat, and the old timer cannot end the next rise', async () => {
    warmChunk()
    const { router, store, wrapper } = await mountRise()
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)

    vi.advanceTimersByTime(300)
    store.activeChatId = 'chat-2'
    await router.push('/chat/chat-2')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)

    store.activeChatId = null
    await flushPromises()
    store.activeChatId = 'chat-3'
    await router.push('/chat/chat-3')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)
    // The old 340 ms timer (due at 340 ms) fires here and must not end the new rise.
    vi.advanceTimersByTime(100)
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)
    vi.advanceTimersByTime(240)
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('reduced motion before selection: no rise starts', async () => {
    setReducedMotion(true)
    warmChunk()
    const { router, store, wrapper } = await mountRise()
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(panelCount(wrapper)).toBe(1)
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('reduced motion switched on while the import is pending: discarded, not resurrected by switching it off', async () => {
    const release = holdChunk()
    const { router, store, wrapper } = await mountRise()
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    setReducedMotion(true)
    release()
    await flushPromises()
    setReducedMotion(false)
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('a refused navigation (guard returns false) cancels the arrival, and a later successful visit does not rise', async () => {
    warmChunk()
    const { router, store, wrapper } = await mountRise()
    let refuse = true
    const removeGuard = router.beforeEach(to => (refuse && to.path === '/chat/chat-1' ? false : undefined))
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    refuse = false
    removeGuard()
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('a thrown guard error cancels the arrival the same way', async () => {
    warmChunk()
    const { router, store, wrapper } = await mountRise()
    const removeGuard = router.beforeEach(to => {
      if (to.path === '/chat/chat-1') throw new Error('guard failed')
    })
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1').catch(() => {})
    await flushPromises()
    removeGuard()
    await router.push('/chat/chat-1')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('a refusal before the catalog row arrives vetoes the late arrival', async () => {
    warmChunk()
    const { router, store, wrapper } = await mountRise(['chat-2'])
    const removeGuard = router.beforeEach(to => (to.path === '/chat/chat-1' ? false : undefined))
    store.activeChatId = 'chat-1'
    await router.push('/chat/chat-1')
    await flushPromises()
    removeGuard()
    store.chats = [chatRow('chat-1'), chatRow('chat-2')] as unknown as typeof store.chats
    await flushPromises()
    expect(panelCount(wrapper)).toBe(1)
    expect(risingCount(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('a refusal naming chat A cannot cancel the intent for the selected chat B', async () => {
    const release = holdChunk()
    const { router, store, wrapper } = await mountRise()
    router.beforeEach(to => (to.path === '/chat/chat-1' ? false : undefined))
    store.activeChatId = 'chat-2'
    await flushPromises()
    // A navigation to chat 1 is refused while chat 2 is the selection.
    await router.push('/chat/chat-1').catch(() => {})
    await flushPromises()
    release()
    await flushPromises()
    await router.push('/chat/chat-2')
    await flushPromises()
    expect(risingCount(wrapper)).toBe(1)
    wrapper.unmount()
  })
})
