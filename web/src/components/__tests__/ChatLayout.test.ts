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
  beforeEach(() => {
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      value: 390,
    })
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

  async function mountHome() {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
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
