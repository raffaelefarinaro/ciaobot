// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

const toggleDictation = vi.fn()

const ChatPanelStub = defineComponent({
  name: 'ChatPanel',
  emits: ['close'],
  setup(_, { emit, expose }) {
    expose({ toggleDictation })
    return () => h('button', {
      'data-testid': 'close-chat',
      onClick: () => emit('close'),
    }, 'Close chat')
  },
})

const EmptyStub = defineComponent({
  name: 'EmptyStub',
  setup() {
    return () => h('div')
  },
})

describe('ChatLayout', () => {
  // Vitest 4's default jsdom env does not provide localStorage, and a few
  // shortcuts (font zoom) round-trip through it. Wire a tiny in-memory
  // shim that mirrors the bits of Storage our shortcuts touch.
  class MemoryStorage {
    private values = new Map<string, string>()
    getItem(key: string): string | null { return this.values.get(key) ?? null }
    setItem(key: string, value: string): void { this.values.set(key, value) }
    removeItem(key: string): void { this.values.delete(key) }
    clear(): void { this.values.clear() }
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      value: 390,
    })
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
  })

  afterEach(() => {
    window.__CIAOBOT_DESKTOP__ = undefined
    toggleDictation.mockReset()
    vi.restoreAllMocks()
  })

  it('keeps the mobile sidebar closed when the chat close button is clicked', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{
      project_id: 'project-1',
      name: 'General',
      workspace: 'personal',
    }] as unknown as typeof store.projects
    store.chats = [{
      chat_id: 'chat-1',
      project_id: 'project-1',
      title: 'Test chat',
    }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      global: {
        plugins: [router],
        stubs: {
          ChatPanel: ChatPanelStub,
          ProjectSidebar: EmptyStub,
          ProjectView: EmptyStub,
          SchedulePanel: EmptyStub,
          SettingsView: EmptyStub,
          FileViewerModal: EmptyStub,
          PinnedFilePanel: EmptyStub,
          PaneHeader: EmptyStub,
          HomeRecentChats: EmptyStub,
        },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.classes()).not.toContain('sidebar-open')
    await wrapper.get('[data-testid="close-chat"]').trigger('click')

    expect(store.activeChatId).toBeNull()
    expect(wrapper.classes()).not.toContain('sidebar-open')
    wrapper.unmount()
  })

  it('routes Cmd+D to the active chat composer in the desktop app', async () => {
    window.__CIAOBOT_DESKTOP__ = true
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      value: 1180,
    })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{
      project_id: 'project-1',
      name: 'General',
      workspace: 'personal',
    }] as unknown as typeof store.projects
    store.chats = [{
      chat_id: 'chat-1',
      project_id: 'project-1',
      title: 'Test chat',
    }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      global: {
        plugins: [router],
        stubs: {
          ChatPanel: ChatPanelStub,
          ProjectSidebar: EmptyStub,
          ProjectView: EmptyStub,
          SchedulePanel: EmptyStub,
          SettingsView: EmptyStub,
          FileViewerModal: EmptyStub,
          PinnedFilePanel: EmptyStub,
          PaneHeader: EmptyStub,
          HomeRecentChats: EmptyStub,
        },
      },
    })
    await flushPromises()

    const event = new KeyboardEvent('keydown', { key: 'd', metaKey: true, cancelable: true })
    window.dispatchEvent(event)

    expect(toggleDictation).toHaveBeenCalledOnce()
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it('routes Alt+D to the active chat composer in the web PWA', async () => {
    window.__CIAOBOT_DESKTOP__ = undefined
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      value: 1180,
    })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{
      project_id: 'project-1',
      name: 'General',
      workspace: 'personal',
    }] as unknown as typeof store.projects
    store.chats = [{
      chat_id: 'chat-1',
      project_id: 'project-1',
      title: 'Test chat',
    }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      global: {
        plugins: [router],
        stubs: {
          ChatPanel: ChatPanelStub,
          ProjectSidebar: EmptyStub,
          ProjectView: EmptyStub,
          SchedulePanel: EmptyStub,
          SettingsView: EmptyStub,
          FileViewerModal: EmptyStub,
          PinnedFilePanel: EmptyStub,
          PaneHeader: EmptyStub,
          HomeRecentChats: EmptyStub,
        },
      },
    })
    await flushPromises()

    const event = new KeyboardEvent('keydown', { key: 'd', altKey: true, cancelable: true })
    window.dispatchEvent(event)

    expect(toggleDictation).toHaveBeenCalledOnce()
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it('increments --font-scale by the shared step on Cmd/Ctrl+Shift+=', async () => {
    // The shortcut should be available in both the desktop app and the PWA:
    // it is the platform's primary modifier. The step, bounds, and
    // persistence key must match Settings → Appearance so the two surfaces
    // stay in sync. Asserts both: the CSS variable is bumped and the value
    // is persisted to localStorage.
    window.__CIAOBOT_DESKTOP__ = true
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    // Pre-seed localStorage so the read at mount time is deterministic and
    // unrelated to whatever the previous test left in the CSS variable.
    try { localStorage.setItem('ciao-font-scale', '1.2') } catch { /* ignore */ }

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Test chat' }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      global: {
        plugins: [router],
        stubs: {
          ChatPanel: ChatPanelStub, ProjectSidebar: EmptyStub, ProjectView: EmptyStub,
          SchedulePanel: EmptyStub, SettingsView: EmptyStub, FileViewerModal: EmptyStub,
          PinnedFilePanel: EmptyStub, PaneHeader: EmptyStub, HomeRecentChats: EmptyStub,
        },
      },
    })
    await flushPromises()

    const event = new KeyboardEvent('keydown', { key: '=', metaKey: true, shiftKey: true, cancelable: true })
    window.dispatchEvent(event)

    // The composable writes the live value to both the CSS variable and
    // localStorage on every adjust; both must agree.
    const cssValue = parseFloat(document.documentElement.style.getPropertyValue('--font-scale'))
    expect(cssValue).toBe(1.25)
    expect(localStorage.getItem('ciao-font-scale')).toBe('1.25')
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it('decrements --font-scale on Cmd/Ctrl+Shift+- (matches the shared step)', async () => {
    // Mirror of the increment case above, exercising the minus path with a
    // pre-seeded value so the assertion is deterministic regardless of
    // any cross-test CSS-variable carryover.
    window.__CIAOBOT_DESKTOP__ = true
    try { localStorage.setItem('ciao-font-scale', '1.2') } catch { /* ignore */ }

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Test chat' }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      global: {
        plugins: [router],
        stubs: {
          ChatPanel: ChatPanelStub, ProjectSidebar: EmptyStub, ProjectView: EmptyStub,
          SchedulePanel: EmptyStub, SettingsView: EmptyStub, FileViewerModal: EmptyStub,
          PinnedFilePanel: EmptyStub, PaneHeader: EmptyStub, HomeRecentChats: EmptyStub,
        },
      },
    })
    await flushPromises()

    const event = new KeyboardEvent('keydown', { key: '-', metaKey: true, shiftKey: true, cancelable: true })
    window.dispatchEvent(event)

    const cssValue = parseFloat(document.documentElement.style.getPropertyValue('--font-scale'))
    expect(cssValue).toBe(1.15)
    expect(localStorage.getItem('ciao-font-scale')).toBe('1.15')
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it('clamps Cmd+Ctrl+Shift+- at the lower font-scale bound', async () => {
    // Start at exactly the floor (0.8) and confirm the shortcut does not
    // underflow past the bound. Without clamp, --font-scale would become
    // 0.75 and persist, breaking the "reset back to default" workflow
    // because the slider/buttons would have nowhere to go down.
    window.__CIAOBOT_DESKTOP__ = true
    try { localStorage.setItem('ciao-font-scale', '0.8') } catch { /* ignore */ }

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Test chat' }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      global: {
        plugins: [router],
        stubs: {
          ChatPanel: ChatPanelStub, ProjectSidebar: EmptyStub, ProjectView: EmptyStub,
          SchedulePanel: EmptyStub, SettingsView: EmptyStub, FileViewerModal: EmptyStub,
          PinnedFilePanel: EmptyStub, PaneHeader: EmptyStub, HomeRecentChats: EmptyStub,
        },
      },
    })
    await flushPromises()

    const event = new KeyboardEvent('keydown', { key: '-', metaKey: true, shiftKey: true, cancelable: true })
    window.dispatchEvent(event)

    // Both writes must land at the floor. We do not read the CSS variable
    // before dispatch because jsdom carries it across tests; the localStorage
    // seed is the authoritative pre-state for this test.
    const afterValue = parseFloat(document.documentElement.style.getPropertyValue('--font-scale'))
    expect(afterValue).toBe(0.8)
    expect(localStorage.getItem('ciao-font-scale')).toBe('0.8')
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

})

describe('ChatLayout home arrow navigation', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    if (!Element.prototype.scrollIntoView) {
      Element.prototype.scrollIntoView = () => {}
    }
  })

  afterEach(() => {
    window.__CIAOBOT_DESKTOP__ = undefined
    vi.restoreAllMocks()
  })

  async function mountHome(startPath = '/') {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/project/:projectId', component: EmptyStub },
      ],
    })
    await router.push(startPath)
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{
      project_id: 'project-1',
      name: 'General',
      workspace: 'personal',
    }] as unknown as typeof store.projects
    store.chats = Array.from({ length: 4 }, (_, i) => ({
      chat_id: `chat-${i + 1}`,
      project_id: 'project-1',
      title: `Chat ${i + 1}`,
      created_at: `2026-08-0${i + 1}T00:00:00Z`,
      last_activity_at: `2026-08-0${i + 1}T00:00:00Z`,
      archived: false,
      local: true,
    })) as unknown as typeof store.chats
    store.activeChatId = null      // home screen: no chat open
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    taskStore.loops = [] as unknown as typeof taskStore.loops
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    // HomeRecentChats is deliberately NOT stubbed: the grid is the thing under
    // test. Every other ChatLayout test stubs it, which is why the keyboard
    // path here was never covered.
    const wrapper = mount(ChatLayout, {
      attachTo: document.body,
      global: {
        plugins: [router],
        stubs: {
          ChatPanel: ChatPanelStub,
          ProjectSidebar: EmptyStub,
          ProjectView: EmptyStub,
          SchedulePanel: EmptyStub,
          SettingsView: EmptyStub,
          FileViewerModal: EmptyStub,
          PinnedFilePanel: EmptyStub,
          PaneHeader: EmptyStub,
        },
      },
    })
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('renders the recent-chat grid on the home screen', async () => {
    const wrapper = await mountHome()
    expect(wrapper.findAll('.home-recent-card').length).toBeGreaterThan(0)
    wrapper.unmount()
  })

  it('moves focus across the grid from a window keydown', async () => {
    const wrapper = await mountHome()
    const cards = wrapper.findAll('.home-recent-card')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[1].element)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)
    wrapper.unmount()
  })

  it('works in the PWA, not just the desktop app', async () => {
    window.__CIAOBOT_DESKTOP__ = undefined
    const wrapper = await mountHome()
    const cards = wrapper.findAll('.home-recent-card')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)
    wrapper.unmount()
  })

  it('closes the open chat on Esc, including while typing in the composer', async () => {
    // Requested behaviour: escaping a chat should not require clicking out of
    // the composer first. Widgets that own Esc claim it with stopPropagation.
    window.__CIAOBOT_DESKTOP__ = undefined
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    await nextTick()

    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    textarea.focus()
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    expect(store.activeChatId).toBeNull()
    textarea.remove()
    wrapper.unmount()
  })

  it('closes the chat with Esc on a project route too', async () => {
    // viewMode is 'project' on /project/:projectId, but it is the same layout
    // with the same open chat. Gating the shortcut on viewMode === 'chat' made
    // Esc (and the arrow keys) dead for anyone who opened a chat through a
    // project, which read as "Esc only works after I click somewhere else".
    window.__CIAOBOT_DESKTOP__ = undefined
    const wrapper = await mountHome('/project/project-1')
    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    await nextTick()

    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    textarea.focus()
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    expect(store.activeChatId).toBeNull()
    textarea.remove()
    wrapper.unmount()
  })

  it('leaves Esc alone when a widget claims it with stopPropagation', async () => {
    window.__CIAOBOT_DESKTOP__ = undefined
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    await nextTick()

    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    textarea.addEventListener('keydown', (e) => {
      if ((e as KeyboardEvent).key === 'Escape') e.stopPropagation()
    })
    textarea.focus()
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()

    expect(store.activeChatId).toBe('chat-1')
    textarea.remove()
    wrapper.unmount()
  })

  it('advances exactly one card per press in the desktop app', async () => {
    // The desktop app binds a second keydown listener (onShortcutKeydown) on
    // top of the unconditional one. When both handled arrows, one press ran
    // onArrow twice and focus skipped a card -- and only in the desktop app,
    // so the PWA looked fine. Mode must not change how far an arrow moves.
    window.__CIAOBOT_DESKTOP__ = true
    const wrapper = await mountHome()
    const cards = wrapper.findAll('.home-recent-card')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[1].element)
    wrapper.unmount()
  })
})
