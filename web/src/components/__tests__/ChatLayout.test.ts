// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick, ref } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTerminalStore } from '../../stores/terminal'
import { useFileViewerStore } from '../../stores/fileViewer'
import { useItemPeekStore } from '../../stores/itemPeek'
import { useTaskStore } from '../../stores/tasks'
import { useProposalsStore } from '../../stores/proposals'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { useHousekeepingStore } from '../../stores/housekeeping'
import { useNotificationInboxStore } from '../../stores/notificationInbox'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import { useFontScale } from '../../composables/useFontScale'
import { homeNewChatProjectId, pendingNewChat } from '../../lib/newChat'
import { terminalPanelWanted } from '../../lib/terminalPanel'

const toggleModelPicker = vi.fn()
const handleSendShortcut = vi.fn(() => true)
const archiveActiveChat = vi.fn()

const ChatPanelStub = defineComponent({
  name: 'ChatPanel',
  emits: ['close'],
  setup(_, { emit, expose }) {
    expose({ toggleModelPicker, handleSendShortcut, archiveActiveChat })
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
    toggleModelPicker.mockReset()
    handleSendShortcut.mockReset()
    handleSendShortcut.mockReturnValue(true)
    archiveActiveChat.mockReset()
    vi.restoreAllMocks()
  })

  it('counts unread chats in the home attention summary', async () => {
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
      chat_id: 'unread-chat',
      project_id: 'project-1',
      title: 'Unread reply',
      archived: false,
      local: true,
      last_activity_at: '2026-08-12T10:00:00Z',
      last_read_at: '2026-08-12T09:00:00Z',
    }] as unknown as typeof store.chats
    store.activeChatId = null
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    // HomeRecentChats is NOT stubbed: the glanceable status now lives inside
    // its lane header, under the workspace line, not in ChatLayout.
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
        },
      },
    })
    await flushPromises()

    expect(wrapper.find('.home-lane-status-text').text()).toBe('1 chat needs your attention. no agents working.')
    // The greeting face is the first-run only header; with activity it stays
    // off the screen.
    expect(wrapper.find('.empty-home-header').exists()).toBe(false)
    wrapper.unmount()
  })

  it('mounts the housekeeping strip on the home screen', async () => {
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
    store.chats = []
    store.activeChatId = null
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [{
      id: 'a',
      kind: 'test',
      severity: 10,
      title: 'A condition needs you',
      detail: 'It can be fixed.',
      glyph: '▲',
      workspace: 'personal',
      run_label: '',
      chat_label: '',
      chat_prompt: '',
      view_label: '',
      blocking: false,
      view_route: '',
    }]

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

    // The strip renders once per home branch. Only the no-pinned-file branch
    // is mounted here (no pinned file), so exactly one strip is on screen.
    expect(wrapper.findAll('.housekeeping')).toHaveLength(1)
    expect(wrapper.find('.housekeeping').text()).toContain('A condition needs you')
    wrapper.unmount()
  })

  describe('Home alerts placement', () => {
    const originalObserver = globalThis.ResizeObserver

    afterEach(() => {
      globalThis.ResizeObserver = originalObserver
    })

    const disconnect = vi.fn()

    async function mountHome(paneWidth: number | null) {
      disconnect.mockReset()
      if (paneWidth === null) {
        // jsdom has none; the pane width stays unknown and `isMobile` decides.
        ;(globalThis as { ResizeObserver?: unknown }).ResizeObserver = undefined
      } else {
        globalThis.ResizeObserver = class {
          constructor(private readonly cb: ResizeObserverCallback) {}
          observe() {
            this.cb([{ contentRect: { width: paneWidth } } as ResizeObserverEntry], this as unknown as ResizeObserver)
          }
          unobserve() {}
          disconnect() { disconnect() }
        } as unknown as typeof ResizeObserver
      }

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
      store.chats = []
      store.activeChatId = null
      store.bootstrapped = true
      vi.spyOn(store, 'fetchAll').mockResolvedValue()
      vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()

      useHousekeepingStore().actions = [{
        id: 'a',
        kind: 'test',
        severity: 10,
        title: 'A condition needs you',
        detail: 'It can be fixed.',
        glyph: '▲',
        workspace: 'personal',
        run_label: '',
        chat_label: '',
        chat_prompt: '',
        view_label: '',
        blocking: false,
        view_route: '',
      }]

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
      return wrapper
    }

    it("puts Home's alerts in the rail when the pane is wide", async () => {
      const wrapper = await mountHome(1200)
      expect(wrapper.find('.home-rail .housekeeping').exists()).toBe(true)
      expect(wrapper.find('.home-main .housekeeping').exists()).toBe(false)
      const rail = wrapper.get('.home-rail').html()
      expect(rail.indexOf('class="housekeeping"')).toBeGreaterThan(-1)
      expect(rail.indexOf('class="housekeeping"')).toBeLessThan(rail.indexOf('home-review-summary'))
      wrapper.unmount()
    })

    it("keeps Home's alerts under the composer when the pane is narrow", async () => {
      const wrapper = await mountHome(600)
      expect(wrapper.find('.home-main .housekeeping').exists()).toBe(true)
      expect(wrapper.find('.home-rail .housekeeping').exists()).toBe(false)
      wrapper.unmount()
    })

    it('puts the alerts in the rail at 941px and under the composer at 940px', async () => {
      // `@container chat-pane (max-width: 940px)` is inclusive.
      const edge = await mountHome(940)
      expect(edge.find('.home-main .housekeeping').exists()).toBe(true)
      expect(edge.find('.home-rail .housekeeping').exists()).toBe(false)
      edge.unmount()

      const over = await mountHome(941)
      expect(over.find('.home-rail .housekeeping').exists()).toBe(true)
      expect(over.find('.home-main .housekeeping').exists()).toBe(false)
      over.unmount()
    })

    it('stops observing the pane on unmount', async () => {
      const wrapper = await mountHome(1200)
      expect(disconnect).not.toHaveBeenCalled()
      wrapper.unmount()
      expect(disconnect).toHaveBeenCalled()
    })

    it('falls back to the viewport width without a ResizeObserver', async () => {
      // beforeEach sets innerWidth to 390, so `isMobile` is true.
      const wrapper = await mountHome(null)
      expect(wrapper.find('.home-main .housekeeping').exists()).toBe(true)
      expect(wrapper.find('.home-rail .housekeeping').exists()).toBe(false)
      wrapper.unmount()
    })
  })

  it('reports post-archive memory work in the home attention summary', async () => {
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
    // The status line renders next to the compact face even when the archived
    // chat is the only thing left. It counts neither as attention nor as a
    // working agent — only in the in-flight memory fragment.
    store.chats = [{
      chat_id: 'tidy-chat',
      project_id: 'project-1',
      title: 'Archived chat',
      archived: true,
      local: true,
      last_activity_at: '2026-08-12T11:00:00Z',
      last_read_at: '2026-08-12T11:00:00Z',
    }] as unknown as typeof store.chats
    store.archivingChats = { 'tidy-chat': true }
    store.activeChatId = null
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    // HomeRecentChats is NOT stubbed: the glanceable status now lives inside
    // its lane header, under the workspace line, not in ChatLayout.
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
        },
      },
    })
    await flushPromises()

    expect(wrapper.find('.home-lane-status-text').text()).toBe('nothing needs your attention. no agents working. 1 updating memory.')
    wrapper.unmount()
  })

  it('a project route sets no dock target and shows no split', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/project/:projectId', component: EmptyStub },
      ],
    })
    await router.push('/project/project-1')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{
      project_id: 'project-1',
      name: 'Upwordo',
      workspace: 'personal',
    }] as unknown as typeof store.projects
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(store, 'pinnedFileFor').mockImplementation(
      (key: string) => (key === 'project-1' ? 'notes/a.md' : undefined),
    )

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    expect(useFileViewerStore().dockTarget).toBe('')
    expect(wrapper.find('.chat-split-side').exists()).toBe(false)
    wrapper.unmount()
  })

  it('a chat route sets the chat as dock target', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
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

    expect(useFileViewerStore().dockTarget).toBe('chat-1')
    wrapper.unmount()
  })

  it('keeps the mobile sidebar closed when the chat close button is clicked', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
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

  // Regression: the sidebar's "chats" nav tab links straight to `/` instead
  // of calling closeChat(), so activeChatId used to stay pointed at the chat
  // the user left. That stale id kept ChatPanel/the home-arrow shortcut
  // gate thinking a chat was still open even though the URL said otherwise.
  it('clears the active chat when a plain route change lands on the bare chat route', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
    })
    await router.push('/chat/chat-1')
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
    expect(store.activeChatId).toBe('chat-1')

    // Simulates clicking the sidebar's "chats" nav-item / any plain link to
    // `/`: a route change that never goes through closeChat().
    await router.push('/')
    await flushPromises()

    expect(store.activeChatId).toBeNull()
    wrapper.unmount()
  })

  // Secondary pages keep the selection underneath them, but Home does not:
  // from /settings, route.params.chatId is undefined both before and after
  // landing on `/` via the "chats" nav tab, so the old chatId-keyed watcher
  // never fired and the retained chat resurfaced. The path watcher closes that
  // hole (#1116). The parameterized version below covers every secondary
  // destination; this one keeps the nav-tab scenario named on its own, since
  // it is the reproduction from the report.
  it('clears the retained chat when the nav tab lands home from Settings', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
        { path: '/settings', component: EmptyStub },
      ],
    })
    await router.push('/settings')
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
    // Settings retains the chat underneath it - until Home is asked for.
    expect(store.activeChatId).toBe('chat-1')

    // Simulates clicking the sidebar's "chats" nav-item from Settings:
    // route.params.chatId stays undefined the whole way through.
    await router.push('/')
    await flushPromises()

    expect(store.activeChatId).toBeNull()
    // Cleared, not closed: the conversation is still in the list.
    expect(store.chats).toHaveLength(1)
    wrapper.unmount()
  })

  it('switches to the workspace matching the displayed number', async () => {
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
    store.workspaces = [
      { name: 'personal', vault_root: '', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '', default_provider: 'claude', gws_profile: '' },
      { name: 'client', vault_root: '', default_provider: 'claude', gws_profile: '' },
    ]
    store.activeWorkspace = 'personal'
    store.bootstrapped = true
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    const event = new KeyboardEvent('keydown', { key: '2', cancelable: true })
    window.dispatchEvent(event)
    await flushPromises()

    // The chat view transitions into the target workspace's chat; only the
    // schedules view passes transition: false.
    expect(switchWorkspace).toHaveBeenCalledWith('work', { transition: true })
    expect(store.activeWorkspace).toBe('work')
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it('does not switch workspaces while typing', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '', default_provider: 'claude', gws_profile: '' },
    ]
    store.activeWorkspace = 'personal'
    store.bootstrapped = true
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()
    const event = new KeyboardEvent('keydown', { key: '2', bubbles: true, cancelable: true })
    input.dispatchEvent(event)

    expect(switchWorkspace).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
    input.remove()
    wrapper.unmount()
  })

  it('Option+N starts in the project Home already names instead of asking again', async () => {
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
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    const newChatInProject = vi.spyOn(store, 'newChatInProject').mockResolvedValue(undefined)

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    const event = new KeyboardEvent('keydown', { key: 'n', code: 'KeyN', altKey: true, cancelable: true })
    window.dispatchEvent(event)
    await flushPromises()

    expect(event.defaultPrevented).toBe(true)
    expect(pendingNewChat.value).toBeNull()
    expect(newChatInProject).toHaveBeenCalledWith('project-1')
    wrapper.unmount()
  })

  // The sidebar toggle is Option+S, not Cmd+S: a browser has already spent
  // Cmd+S on Save Page.
  it('toggles the sidebar on Option+S', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    // Starts expanded, so the first press collapses and the second restores —
    // asserting both directions, since "opens and closes" is the whole point.
    expect(wrapper.find('.chat-layout').classes()).toContain('sidebar-open')

    const collapse = new KeyboardEvent('keydown', { key: 's', code: 'KeyS', altKey: true, cancelable: true })
    window.dispatchEvent(collapse)
    await nextTick()
    expect(wrapper.find('.chat-layout').classes()).not.toContain('sidebar-open')
    expect(collapse.defaultPrevented).toBe(true)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 's', code: 'KeyS', altKey: true, cancelable: true }))
    await nextTick()
    expect(wrapper.find('.chat-layout').classes()).toContain('sidebar-open')

    wrapper.unmount()
  })

  // The model picker is Option+M: macOS reserves the Cmd chords (plain Cmd+M
  // for Minimize Window) above the page. Needs an active chat.
  it('opens the model picker on Option+M', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
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

    const event = new KeyboardEvent('keydown', { key: 'm', code: 'KeyM', altKey: true, cancelable: true })
    window.dispatchEvent(event)

    expect(toggleModelPicker).toHaveBeenCalledOnce()
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  // Option+Backspace is the macOS delete-previous-word command while editing.
  it('leaves Option+Backspace to the focused text field', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
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

    // Focus sits in a text field, as it would mid-compose.
    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    textarea.focus()

    const event = new KeyboardEvent('keydown', {
      key: 'Backspace',
      code: 'Backspace',
      altKey: true,
      cancelable: true,
    })
    textarea.dispatchEvent(event)

    expect(archiveActiveChat).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
    textarea.remove()
    wrapper.unmount()
  })

  // No open chat: nothing to archive, so the chord keeps its default meaning
  // (e.g. the browser's history-back on Option+Backspace).
  it('leaves Option+Backspace alone when no chat is open', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    const event = new KeyboardEvent('keydown', { key: 'Backspace', code: 'Backspace', altKey: true, cancelable: true })
    window.dispatchEvent(event)

    expect(archiveActiveChat).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  // Cmd/Ctrl+Enter has to reach the composer from outside it. Attaching an
  // image or a comment leaves focus on the control that closed, so without
  // this a message carrying only attachments could not be sent by keyboard.
  it('sends the composer draft on Cmd+Enter from outside the composer', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
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

    const event = new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, cancelable: true })
    window.dispatchEvent(event)

    expect(handleSendShortcut).toHaveBeenCalledOnce()
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  // Nothing to send: the panel declines, and the key is left alone so it keeps
  // whatever meaning the focused control gives it.
  it('leaves Cmd+Enter alone when the composer declines it', async () => {
    handleSendShortcut.mockReturnValue(false)
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
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

    const event = new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, cancelable: true })
    window.dispatchEvent(event)

    expect(handleSendShortcut).toHaveBeenCalledOnce()
    expect(event.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  // The composer and the comment popovers bind Cmd+Enter themselves, so the
  // layout must never claim it from a text field: sending from the popover
  // would fire alongside the popover's own save.
  it('leaves Cmd+Enter to the focused text field', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
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

    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    textarea.focus()
    textarea.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter',
      metaKey: true,
      bubbles: true,
      cancelable: true,
    }))
    await nextTick()

    expect(handleSendShortcut).not.toHaveBeenCalled()
    textarea.remove()
    wrapper.unmount()
  })

  // PinnedFilePanel renders its own comment popover outside ChatPanel, and that
  // popover's Save/Cancel buttons are not text fields -- isTypingTarget alone
  // would miss focus sitting on one of them. Without this, Cmd+Enter there
  // would send the unrelated chat draft and swallow the button's own Enter
  // activation instead of saving the file comment.
  it('defers to an open comment popover in the pinned file panel', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

    // Started on the chat's own route, not on Home: the pinned pane is scoped
    // to the route on screen, so since #1116 a Home mount no longer renders a
    // retained chat's pinned file and there would be no panel here to defer to.
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
    })
    await router.push('/chat/chat-1')
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
    store.applyChatPinState('chat-1', { path: 'src/pinned-file.ts', dismissed_paths: [], revision: 1 }, { source: 'test' })
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

    const BusyPinnedFilePanelStub = defineComponent({
      name: 'PinnedFilePanel',
      setup(_, { expose }) {
        expose({ isBusyAuthoring: true })
        return () => h('div')
      },
    })

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
          PinnedFilePanel: BusyPinnedFilePanelStub,
          PaneHeader: EmptyStub,
          HomeRecentChats: EmptyStub,
        },
      },
    })
    await flushPromises()

    const event = new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, cancelable: true })
    window.dispatchEvent(event)

    expect(handleSendShortcut).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  it('leaves the sidebar alone when the shortcut fires inside a text field', async () => {
    // Option+S is how you type ß, so stealing it mid-composition would break
    // text entry for the sake of a view toggle.
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()

    const event = new KeyboardEvent('keydown', { key: 's', altKey: true, cancelable: true })
    input.dispatchEvent(event)
    await nextTick()

    expect(wrapper.find('.chat-layout').classes()).toContain('sidebar-open')
    expect(event.defaultPrevented).toBe(false)

    input.remove()
    wrapper.unmount()
  })

  it('increments --font-scale by the shared step on Option+=', async () => {
    // Option+= is the chord a browser can still claim: Cmd+Shift+= *is*
    // Cmd++ on a US layout, which the browser zooms with above the page, so it
    // ignores preventDefault. The step, bounds, and persistence key must match
    // Settings → Appearance so the two surfaces stay in sync. Asserts both: the
    // CSS variable is bumped and the value is persisted to localStorage.
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    // Seed through the composable, not localStorage: the scale ref is
    // module-scoped and shared, so writing storage after import would not
    // move it. Driving the real API is also what the app does.
    useFontScale().set(1.2)

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Test chat' }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    const event = new KeyboardEvent('keydown', { key: '=', code: 'Equal', altKey: true, cancelable: true })
    window.dispatchEvent(event)

    // The composable writes the live value to both the CSS variable and
    // localStorage on every adjust; both must agree.
    const cssValue = parseFloat(document.documentElement.style.getPropertyValue('--font-scale'))
    expect(cssValue).toBe(1.25)
    expect(localStorage.getItem('ciao-font-scale')).toBe('1.25')
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it('decrements --font-scale on Option+- (matches the shared step)', async () => {
    // Mirror of the increment case above, exercising the minus path with a
    // pre-seeded value so the assertion is deterministic regardless of
    // any cross-test CSS-variable carryover.
    useFontScale().set(1.2)

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Test chat' }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    const event = new KeyboardEvent('keydown', { key: '-', code: 'Minus', altKey: true, cancelable: true })
    window.dispatchEvent(event)

    const cssValue = parseFloat(document.documentElement.style.getPropertyValue('--font-scale'))
    expect(cssValue).toBe(1.15)
    expect(localStorage.getItem('ciao-font-scale')).toBe('1.15')
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it('clamps Option+- at the lower font-scale bound', async () => {
    // Start at exactly the floor (0.8) and confirm the shortcut does not
    // underflow past the bound. Without clamp, --font-scale would become
    // 0.75 and persist, breaking the "reset back to default" workflow
    // because the slider/buttons would have nowhere to go down.
    useFontScale().set(0.8)

    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Test chat' }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    const event = new KeyboardEvent('keydown', { key: '-', code: 'Minus', altKey: true, cancelable: true })
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

// #1116: reaching Home must show Home - no conversation, no pinned file -
// whatever was selected before. Secondary pages keep the selection underneath
// them; only the bare Home route clears it, and clearing is a view change, not
// a deletion.
describe('ChatLayout Home navigation clears the selected chat', () => {
  class MemoryStorage {
    private values = new Map<string, string>()
    getItem(key: string): string | null { return this.values.get(key) ?? null }
    setItem(key: string, value: string): void { this.values.set(key, value) }
    removeItem(key: string): void { this.values.delete(key) }
    clear(): void { this.values.clear() }
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  const secondaryRoutes = [
    ['Settings', '/settings'],
    ['Automations', '/schedules'],
    ['Tasks', '/tasks'],
    ['Memory', '/memory'],
    ['Project', '/project/project-1'],
  ] as const

  // Mounts with a chat selected and a file pinned to it, on `startPath`.
  async function mountWithSelection(startPath = '/chat/chat-1') {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
        { path: '/project/:projectId', component: EmptyStub },
        { path: '/settings', component: EmptyStub },
        { path: '/schedules', component: EmptyStub },
        { path: '/tasks', component: EmptyStub },
        { path: '/memory', component: EmptyStub },
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
    store.chats = [
      { chat_id: 'chat-1', project_id: 'project-1', title: 'Chat one' },
      { chat_id: 'chat-2', project_id: 'project-1', title: 'Chat two' },
    ] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    store.applyChatPinState('chat-1', { path: 'src/pinned-file.ts', dismissed_paths: [], revision: 1 }, { source: 'test' })
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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
    return { wrapper, router, store }
  }

  it('shows the chat and its pinned file before navigating away', async () => {
    const { wrapper, store } = await mountWithSelection()
    // Fixture check: the split layout is genuinely on screen here, so the
    // assertions after navigating Home mean something.
    expect(store.activeChatId).toBe('chat-1')
    expect(wrapper.find('[data-testid="close-chat"]').exists()).toBe(true)
    expect(wrapper.find('.chat-split').exists()).toBe(true)
    wrapper.unmount()
  })

  it('a reload of Home does not reopen the chat restored from storage', async () => {
    // The store restores `ciao-active-chat` when it is created, before the
    // layout mounts, so a reload of `/` mounts with that chat selected.
    const { wrapper, store } = await mountWithSelection('/')
    expect(wrapper.find('.empty-shell').exists()).toBe(true)
    expect(wrapper.find('[data-testid="close-chat"]').exists()).toBe(false)
    expect(store.activeChatId).toBeNull()
    wrapper.unmount()
  })

  it.each(secondaryRoutes)('Home clears a retained chat after %s', async (_label, secondary) => {
    const { wrapper, router, store } = await mountWithSelection()
    await router.push(secondary)
    await flushPromises()
    // The secondary page keeps the selection underneath it - that is what makes
    // the Home navigation below meaningful.
    expect(store.activeChatId).toBe('chat-1')

    await router.push('/')
    await flushPromises()

    // Home content, and nothing else: no chat pane, no pinned file pane.
    expect(wrapper.find('.empty-shell').exists()).toBe(true)
    expect(wrapper.find('[data-testid="close-chat"]').exists()).toBe(false)
    expect(wrapper.find('.chat-split').exists()).toBe(false)
    expect(store.activeChatId).toBeNull()
    // Cleared, not closed: both conversations remain available in the list.
    expect(store.chats.map(c => c.chat_id)).toEqual(['chat-1', 'chat-2'])
    wrapper.unmount()
  })

  it('direct chat to Home and bare chat route clear the view', async () => {
    const fromChat = await mountWithSelection()
    await fromChat.router.push('/chat')
    await flushPromises()
    // A bare `/chat` is the sidebar's chats nav tab: Home, not an open chat.
    expect(fromChat.store.activeChatId).toBeNull()
    expect(fromChat.wrapper.find('[data-testid="close-chat"]').exists()).toBe(false)
    expect(fromChat.wrapper.find('.chat-split').exists()).toBe(false)
    fromChat.wrapper.unmount()

    const fromHome = await mountWithSelection('/')
    // Re-select, so this leg starts from a real chat route.
    await fromHome.router.push('/chat/chat-1')
    await flushPromises()
    expect(fromHome.store.activeChatId).toBe('chat-1')

    await fromHome.router.push('/chat')
    await flushPromises()
    expect(fromHome.store.activeChatId).toBeNull()
    fromHome.wrapper.unmount()
  })

  it('Home navigation does not delete an empty draft or its staged content', async () => {
    const { wrapper, router, store } = await mountWithSelection()
    const draftId = 'chat-draft'
    // An unused New Chat: exactly what closeChat() would DELETE.
    store.chats = [
      ...store.chats,
      { chat_id: draftId, project_id: 'project-1', title: 'New Chat', session_id: '' },
    ] as unknown as typeof store.chats
    store.messages[draftId] = []
    store.activeChatId = draftId
    store.pendingImages = ['img-1']
    localStorage.setItem('ciao-chat-drafts', JSON.stringify({ [draftId]: 'half a thought' }))

    const closeChat = vi.spyOn(store, 'closeChat')
    const deleteChat = vi.spyOn(store, 'deleteChat')

    await router.push('/settings')
    await flushPromises()
    await router.push('/')
    await flushPromises()

    // The navigation is not a close gesture, so the destructive path must not
    // run at all - not closeChat() (which owns the delete policy), not DELETE.
    expect(closeChat).not.toHaveBeenCalled()
    expect(deleteChat).not.toHaveBeenCalled()
    expect(store.activeChatId).toBeNull()
    // Everything survives: the chat, its unsent text and its staged image.
    // `pendingImages` is a computed over the *active* chat's bucket, so with
    // nothing selected it reads empty by design - the storage key is where the
    // staged attachment has to still be.
    expect(store.chats.map(c => c.chat_id)).toContain(draftId)
    expect(JSON.parse(localStorage.getItem('ciao-chat-drafts') || '{}')[draftId]).toBe('half a thought')
    expect(JSON.parse(localStorage.getItem('ciao-pending-images') || '{}')[draftId]).toEqual(['img-1'])

    // Reopening the chat brings both back with it.
    await router.push('/chat/draft')
    await flushPromises()
    store.activeChatId = draftId
    await flushPromises()
    expect(store.pendingImages).toEqual(['img-1'])
    wrapper.unmount()
  })

  it('back and forward between Home and a chat follow the route', async () => {
    const { wrapper, router, store } = await mountWithSelection()
    await router.push('/')
    await flushPromises()
    expect(store.activeChatId).toBeNull()

    // Back to the chat route: the chat comes back, with its pin.
    router.back()
    await flushPromises()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/chat/chat-1')
    expect(store.activeChatId).toBe('chat-1')
    expect(wrapper.find('.chat-split').exists()).toBe(true)

    // Forward to Home again: cleared, and no auto-selected replacement.
    router.forward()
    await flushPromises()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    expect(store.activeChatId).toBeNull()
    expect(wrapper.find('[data-testid="close-chat"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('Home reconciliation does not clear a chat selected while bootstrap is pending', async () => {
    // fetchAll() is held open, so the route can move to a chat while the layout
    // is still bootstrapping. The post-fetchAll reconciliation must read the
    // route as it is *then*, not the one captured before the await.
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
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
      chat_id: 'chat-2',
      project_id: 'project-1',
      title: 'Chat two',
    }] as unknown as typeof store.chats
    store.activeChatId = null
    store.bootstrapped = true

    let releaseFetch: () => void = () => {}
    vi.spyOn(store, 'fetchAll').mockImplementation(() => new Promise<void>((resolve) => {
      releaseFetch = () => resolve()
    }))
    const openChatFromDeepLink = vi.spyOn(store, 'openChatFromDeepLink').mockResolvedValue(undefined)

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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

    // The user opens a chat while the boot is still in flight.
    await router.push('/chat/chat-2')
    await flushPromises()
    store.activeChatId = 'chat-2'
    expect(openChatFromDeepLink).toHaveBeenCalledWith('chat-2')

    releaseFetch()
    await flushPromises()
    await flushPromises()

    // The reconciliation saw `/chat/chat-2`, so it left the fresh selection be.
    expect(store.activeChatId).toBe('chat-2')
    wrapper.unmount()
  })

  it('restores a chat and its pin when that chat is reopened from Home', async () => {
    const { wrapper, router, store } = await mountWithSelection()
    await router.push('/')
    await flushPromises()
    expect(store.activeChatId).toBeNull()
    expect(store.pinnedFileFor('chat-1')).toBe('src/pinned-file.ts')

    await router.push('/chat/chat-1')
    await flushPromises()

    expect(store.activeChatId).toBe('chat-1')
    // Home had no pin key at all; the chat route brings back *its* pin.
    expect(wrapper.find('.chat-split').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('ChatLayout shared chat pin', () => {
  class MemoryStorage {
    private values = new Map<string, string>()
    getItem(key: string): string | null { return this.values.get(key) ?? null }
    setItem(key: string, value: string): void { this.values.set(key, value) }
    removeItem(key: string): void { this.values.delete(key) }
    clear(): void { this.values.clear() }
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  async function mountChat(width: number, path = '/chat/chat-1') {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
    })
    await router.push(path)
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
      title: 'Chat one',
    }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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
    return { wrapper, router, store }
  }

  it('restores the shared pin on desktop and clears it on a remote close', async () => {
    const { wrapper, store } = await mountChat(1180)
    store.applyChatPinState('chat-1', { path: 'src/pinned-file.ts', dismissed_paths: [], revision: 1 }, { source: 'event' })
    await nextTick()
    expect(wrapper.find('.chat-split').exists()).toBe(true)

    // A remote unpin removes the dock without navigating away.
    store.applyChatPinState('chat-1', { path: '', dismissed_paths: ['src/pinned-file.ts'], revision: 2 }, { source: 'event' })
    await nextTick()
    expect(wrapper.find('.chat-split').exists()).toBe(false)
    expect(store.activeChatId).toBe('chat-1')
    wrapper.unmount()
  })

  it('renders a keyboard-accessible narrow opener that never auto-opens a modal', async () => {
    const { wrapper, store } = await mountChat(390)
    store.applyChatPinState('chat-1', { path: 'src/pinned-file.ts', dismissed_paths: [], revision: 1 }, { source: 'event' })
    await nextTick()

    const opener = wrapper.find('.narrow-pin-opener')
    expect(opener.exists()).toBe(true)
    const openBtn = opener.find('.narrow-pin-btn')
    expect(openBtn.exists()).toBe(true)
    // Accessible name carries the filename; the control is a real button.
    expect(openBtn.attributes('aria-label')).toContain('pinned-file.ts')
    // A pin restore must not auto-open the viewer modal.
    expect(useFileViewerStore().isOpen).toBe(false)

    // A remote unpin removes the opener.
    store.applyChatPinState('chat-1', { path: '', dismissed_paths: ['src/pinned-file.ts'], revision: 2 }, { source: 'event' })
    await nextTick()
    expect(wrapper.find('.narrow-pin-opener').exists()).toBe(false)
    wrapper.unmount()
  })

  it('Home shows no shared-pin opener even when a chat is pinned', async () => {
    const { wrapper, store } = await mountChat(390, '/')
    store.applyChatPinState('chat-1', { path: 'src/pinned-file.ts', dismissed_paths: [], revision: 1 }, { source: 'event' })
    await nextTick()
    expect(wrapper.find('.narrow-pin-opener').exists()).toBe(false)
    expect(wrapper.find('.chat-split').exists()).toBe(false)
    wrapper.unmount()
  })

  it('opens the narrow shared pin through openSharedPin, not open', async () => {
    const { wrapper, store } = await mountChat(390)
    store.applyChatPinState('chat-1', { path: 'src/pinned-file.ts', dismissed_paths: [], revision: 1 }, { source: 'event' })
    await nextTick()

    const viewer = useFileViewerStore()
    const openSharedPin = vi.spyOn(viewer, 'openSharedPin').mockResolvedValue(true)
    const open = vi.spyOn(viewer, 'open')

    await wrapper.find('.narrow-pin-btn').trigger('click')
    await flushPromises()

    // The opener must mark the preview as the chat's shared-pin surface so a
    // remote unpin reconciles it and a user close unpins through the server.
    expect(openSharedPin).toHaveBeenCalledWith('src/pinned-file.ts', 'chat-1')
    expect(open).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps a dirty pinned panel alive across a remote pin change, applying it once idle', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
    })
    await router.push('/chat/chat-1')
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
      title: 'Chat one',
    }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

    const busy = ref(true)
    const BusyPanelStub = defineComponent({
      name: 'PinnedFilePanel',
      props: ['filePath'],
      setup(_, { expose }) {
        expose({ isBusyAuthoring: busy })
        return () => h('div', { class: 'busy-panel' })
      },
    })

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
          PinnedFilePanel: BusyPanelStub,
          PaneHeader: EmptyStub,
          HomeRecentChats: EmptyStub,
        },
      },
    })
    await flushPromises()

    store.applyChatPinState('chat-1', { path: 'src/a.ts', dismissed_paths: [], revision: 1 }, { source: 'event' })
    await nextTick()
    expect(wrapper.find('.chat-split').exists()).toBe(true)

    const panel = wrapper.findComponent(BusyPanelStub)
    expect(panel.props('filePath')).toBe('src/a.ts')

    // A remote replacement while the panel is mid-edit must not remount or
    // discard the edit: the panel holds its current path.
    store.applyChatPinState('chat-1', { path: 'src/b.ts', dismissed_paths: [], revision: 2 }, { source: 'event' })
    await nextTick()
    expect(wrapper.find('.chat-split').exists()).toBe(true)
    expect(panel.props('filePath')).toBe('src/a.ts')

    // A remote unpin while mid-edit likewise leaves the dirty panel alive.
    store.applyChatPinState('chat-1', { path: '', dismissed_paths: ['src/b.ts'], revision: 3 }, { source: 'event' })
    await nextTick()
    expect(wrapper.find('.chat-split').exists()).toBe(true)
    expect(panel.props('filePath')).toBe('src/a.ts')

    // Once the user finishes editing, the deferred change is applied.
    busy.value = false
    await nextTick()
    expect(wrapper.find('.chat-split').exists()).toBe(false)
    wrapper.unmount()
  })

  async function mountPinned(firstPanelBusy: boolean) {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    const router = createRouter({
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
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Chat one' }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(store, 'clearActiveChatView').mockImplementation(() => { store.activeChatId = null })
    vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()

    // Each mount is its own panel: only the first one holds unsaved work, as a
    // real remount (the panel is keyed by the route's chat) starts clean.
    let mounts = 0
    const PanelStub = defineComponent({
      name: 'PinnedFilePanel',
      props: ['filePath'],
      setup(_, { expose }) {
        expose({ isBusyAuthoring: firstPanelBusy && mounts++ === 0 })
        return () => h('div', { class: 'pinned-panel-stub' })
      },
    })

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
          PinnedFilePanel: PanelStub,
          PaneHeader: EmptyStub,
          HomeRecentChats: EmptyStub,
        },
      },
    })
    await flushPromises()
    store.applyChatPinState('chat-1', { path: 'notes/draft.md', dismissed_paths: [], revision: 1 }, { source: 'event' })
    await nextTick()
    expect(wrapper.find('.chat-split').exists()).toBe(true)
    return { wrapper, store, router }
  }

  it('drops the pinned page when the chat is no longer on screen, though the route still names it', async () => {
    const { wrapper, store } = await mountPinned(false)
    // Archive, delete and a refresh that no longer lists the chat all clear the
    // selection without a navigation: the screen falls back to Home, and the
    // chat's pinned page must not stay beside it.
    store.activeChatId = null
    await flushPromises()
    expect(wrapper.find('.chat-split').exists()).toBe(false)
    wrapper.unmount()
  })

  it('closes the pinned page on the way Home even while it holds unsaved work', async () => {
    const { wrapper, router } = await mountPinned(true)
    await router.push('/')
    await flushPromises()
    expect(wrapper.find('.chat-split').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('ChatLayout home arrow navigation', () => {
  beforeEach(() => {
    // The store restores the persisted active workspace on creation, and an
    // earlier test persists 'work' via switchWorkspace. Without a clean slate
    // the arrow-anchoring tests here would start on a different lane than the
    // fixture describes, depending on which tests ran first in the file.
    localStorage.clear()
    setActivePinia(createPinia())
    if (!Element.prototype.scrollIntoView) {
      Element.prototype.scrollIntoView = () => {}
    }
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  // Hoisted so the Esc tests below can assert where navigation ended up.
  let router: ReturnType<typeof createRouter>

  async function mountHome(startPath = '/') {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/project/:projectId', component: EmptyStub },
        { path: '/settings', component: EmptyStub },
        { path: '/schedules', component: EmptyStub },
        { path: '/tasks', component: EmptyStub },
        { path: '/memory', component: EmptyStub },
      ],
    })
    await router.push(startPath)
    await router.isReady()

    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '', default_provider: 'claude', gws_profile: '' },
    ]
    store.projects = [
      { project_id: 'personal-project', name: 'General', workspace: 'personal' },
      { project_id: 'work-project', name: 'General', workspace: 'work' },
    ] as unknown as typeof store.projects
    store.chats = Array.from({ length: 4 }, (_, i) => ({
      chat_id: `chat-${i + 1}`,
      project_id: i < 2 ? 'personal-project' : 'work-project',
      title: `Chat ${i + 1}`,
      created_at: new Date(Date.now() - (i + 1) * 60 * 60 * 1000).toISOString(),
      last_activity_at: new Date(Date.now() - (i + 1) * 60 * 60 * 1000).toISOString(),
      archived: false,
      local: true,
    })) as unknown as typeof store.chats
    store.activeChatId = null      // home screen: no chat open
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    // HomeRecentChats is deliberately NOT stubbed: the lanes are the thing under
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
          // Bare /memory is not this test's route. The real map replaces it
          // with /memory/<section> (To decide on a first visit), and that
          // replace races the assertion that the chord landed on /memory.
          MemoryMapView: EmptyStub,
        },
      },
    })
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('renders the recent-chat lane of the selected workspace on home', async () => {
    const wrapper = await mountHome()
    expect(wrapper.findAll('.home-lane')).toHaveLength(1)
    expect(wrapper.find('[data-lane-key="personal"]').exists()).toBe(true)
    wrapper.unmount()
  })

  // The global "+ personal chat / + work chat" pair used to stay on screen at
  // narrow widths, where it duplicated each lane's own "+ new" and spent a
  // saturated fill on a non-blocking action.
  it('drops the global new-chat buttons once any chat exists', async () => {
    const wrapper = await mountHome()
    expect(wrapper.find('.empty-actions').exists()).toBe(false)
    // The composer is home's one way to start work; the selected lane no
    // longer repeats it with its own "+ new".
    expect(wrapper.find('.home-intake-new').exists()).toBe(true)
    wrapper.unmount()
  })

  it('keeps every lane expanded, with no peek row to collapse into', async () => {
    const wrapper = await mountHome()
    expect(wrapper.find('.home-lane-peek').exists()).toBe(false)
    for (const lane of wrapper.findAll('.home-lane')) {
      expect(lane.find('.home-lane-body').exists()).toBe(true)
    }
    wrapper.unmount()
  })

  // One keydown listener owns the unmodified keys: handling arrows here AND
  // again in onShortcutKeydown ran onArrow twice and skipped a card per press.
  it('moves focus through the home grid from a window keydown', async () => {
    const wrapper = await mountHome()
    const cards = wrapper.findAll('.home-chat-item')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[1].element)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)
    wrapper.unmount()
  })

  it('moves into the grid with ArrowRight', async () => {
    const wrapper = await mountHome()
    const cards = wrapper.findAll('.home-chat-item')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)
    wrapper.unmount()
  })

  it('keeps number keys as workspace shortcuts from home', async () => {
    const wrapper = await mountHome()
    const store = useProjectStore()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: '2', bubbles: true }))
    await flushPromises()
    expect(store.activeWorkspace).toBe('work')
    wrapper.unmount()
  })

  it('switches sections with Option+Arrow', async () => {
    const wrapper = await mountHome()
    expect(router.currentRoute.value.path).toBe('/')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', altKey: true, bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/schedules')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', altKey: true, bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/tasks')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', altKey: true, bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/memory')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', altKey: true, bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/tasks')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', altKey: true, bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/schedules')
    wrapper.unmount()
  })

  it('option_arrow_sections_skip_notifications', async () => {
    const wrapper = await mountHome('/memory')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', altKey: true, bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/settings')
    wrapper.unmount()
  })

  // `/tasks` has no `viewMode` arm of its own in the index arithmetic, so it fell
  // through to Settings: Option+Right off the board jumped to Home and
  // Option+Left jumped to Memory, neither of them the neighbouring section.
  it('walks to and from the task board rather than over it', async () => {
    const wrapper = await mountHome('/tasks')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', altKey: true, bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/memory')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', altKey: true, bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/tasks')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', altKey: true, bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/schedules')
    wrapper.unmount()
  })

  it('does not treat bare arrow keys as section switches', async () => {
    const wrapper = await mountHome()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    wrapper.unmount()
  })

  // Esc used to do nothing at all on settings and automations, because those
  // views are excluded from shortcutsActive. It is the universal way back.
  it('returns to home on Esc from a full-screen view', async () => {
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = null
    await router.push('/settings')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/settings')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
    wrapper.unmount()
  })

  it('leaves Esc alone when already on home with no chat open', async () => {
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = null
    await router.push('/')
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
    wrapper.unmount()
  })

  it('closes the open chat on Esc, including while typing in the composer', async () => {
    // Requested behaviour: escaping a chat should not require clicking out of
    // the composer first. Widgets that own Esc claim it with stopPropagation.
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

  it('Esc with the task overlay open leaves the chat open', async () => {
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    await nextTick()
    const peek = useItemPeekStore()
    peek.target = { kind: 'task', id: 'ship', workspace: 'personal' }
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(store.activeChatId).toBe('chat-1')
    expect(peek.isOpen).toBe(true)
    peek.close()
    wrapper.unmount()
  })

  // Reported in review: activeChatId stays populated when Settings is opened
  // from a chat, so a chat-first Esc ran closeChat() on a chat that was not on
  // screen - disconnecting it, and deleting it outright when it was an unused
  // draft with an unsent composer message. Esc from a secondary page pushes
  // `/`, and that is now a Home navigation, so the view clears (#1116) - the
  // protection that matters is that nothing is deleted.
  it('clears without deleting when escaping Settings', async () => {
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    await router.push('/settings')
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
    expect(store.activeChatId).toBeNull()
    // The conversation is still there, and still on screen in the list.
    expect(store.chats.map(c => c.chat_id)).toContain('chat-1')
    wrapper.unmount()
  })

  it('does the same escaping Automations', async () => {
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    await router.push('/schedules')
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
    expect(store.activeChatId).toBeNull()
    expect(store.chats.map(c => c.chat_id)).toContain('chat-1')
    wrapper.unmount()
  })

  // A popover that handled Escape itself must not also navigate away.
  it('defers to a nested control that consumed Escape', async () => {
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = null
    await router.push('/settings')
    await flushPromises()

    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    event.preventDefault()
    window.dispatchEvent(event)
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/settings')
    wrapper.unmount()
  })

  it('closes the chat with Esc on a project route too', async () => {
    // viewMode is 'project' on /project/:projectId, but it is the same layout
    // with the same open chat. Gating the shortcut on viewMode === 'chat' made
    // Esc (and the arrow keys) dead for anyone who opened a chat through a
    // project, which read as "Esc only works after I click somewhere else".
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
})

// On macOS, Option rewrites KeyboardEvent.key into the character the chord
// would type (⌥D is "∂", ⌥S "ß", ⌥N a dead key), so the PWA's Option chords
// must match the physical key (`code`). Windows/Linux Alt leaves `key` alone;
// both shapes have to reach the same action.
describe('ChatLayout PWA Option/Alt chords', () => {
  class MemoryStorage {
    private values = new Map<string, string>()
    getItem(key: string): string | null { return this.values.get(key) ?? null }
    setItem(key: string, value: string): void { this.values.set(key, value) }
    removeItem(key: string): void { this.values.delete(key) }
    clear(): void { this.values.clear() }
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })

  afterEach(() => {
    toggleModelPicker.mockReset()
    archiveActiveChat.mockReset()
    pendingNewChat.value?.resolve(null)
    vi.restoreAllMocks()
  })

  async function mountWebLayout() {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
    })
    await router.push('/chat/chat-1')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Test chat' }] as unknown as typeof store.chats
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

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
    return wrapper
  }

  function press(init: KeyboardEventInit, target: EventTarget = window): KeyboardEvent {
    const event = new KeyboardEvent('keydown', { altKey: true, bubbles: true, cancelable: true, ...init })
    target.dispatchEvent(event)
    return event
  }

  const platforms = [
    ['Mac Option', { n: 'Dead', m: 'µ', s: 'ß', eq: '≠', minus: '–' }],
    ['Windows Alt', { n: 'n', m: 'm', s: 's', eq: '=', minus: '-' }],
  ] as const

  it.each(platforms)('%s+M opens the model picker', async (_label, keys) => {
    const wrapper = await mountWebLayout()
    const event = press({ key: keys.m, code: 'KeyM' })
    expect(toggleModelPicker).toHaveBeenCalledOnce()
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it.each(platforms)('%s+N opens the new-chat picker', async (_label, keys) => {
    const wrapper = await mountWebLayout()
    const event = press({ key: keys.n, code: 'KeyN' })
    await flushPromises()
    expect(event.defaultPrevented).toBe(true)
    expect(pendingNewChat.value).not.toBeNull()
    wrapper.unmount()
  })

  it.each(platforms)('%s+Backspace archives the open chat outside text fields', async () => {
    const wrapper = await mountWebLayout()
    const event = press({ key: 'Backspace', code: 'Backspace' })
    expect(archiveActiveChat).toHaveBeenCalledOnce()
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it.each(platforms)('%s+S toggles the sidebar', async (_label, keys) => {
    const wrapper = await mountWebLayout()
    expect(wrapper.find('.chat-layout').classes()).toContain('sidebar-open')
    const event = press({ key: keys.s, code: 'KeyS' })
    await nextTick()
    expect(wrapper.find('.chat-layout').classes()).not.toContain('sidebar-open')
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it.each(platforms)('%s+= and %s+- step the font scale', async (_label, keys) => {
    useFontScale().set(1.2)
    const wrapper = await mountWebLayout()
    const zoomIn = press({ key: keys.eq, code: 'Equal' })
    expect(localStorage.getItem('ciao-font-scale')).toBe('1.25')
    expect(zoomIn.defaultPrevented).toBe(true)
    const zoomOut = press({ key: keys.minus, code: 'Minus' })
    expect(localStorage.getItem('ciao-font-scale')).toBe('1.2')
    expect(zoomOut.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  // In a text field Option+S types ß and Option+= types ≠ on a Mac; matching
  // by physical key must not start stealing them.
  it.each(platforms)('%s+S and %s+= stay inert while a text field is focused', async (_label, keys) => {
    useFontScale().set(1.2)
    const wrapper = await mountWebLayout()
    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()

    const sidebar = press({ key: keys.s, code: 'KeyS' }, input)
    const zoom = press({ key: keys.eq, code: 'Equal' }, input)
    await nextTick()

    expect(wrapper.find('.chat-layout').classes()).toContain('sidebar-open')
    expect(sidebar.defaultPrevented).toBe(false)
    expect(zoom.defaultPrevented).toBe(false)
    expect(localStorage.getItem('ciao-font-scale')).toBe('1.2')

    input.remove()
    wrapper.unmount()
  })

  // Option+letter is how a Mac types accents and symbols (⌥N then N is ñ), so
  // inside a text field a Mac Option press must reach the field
  // untouched. The same events outside a text field are shortcuts.
  it.each([
    ['⌥N', { key: 'Dead', code: 'KeyN' }],
    ['⌥M', { key: 'µ', code: 'KeyM' }],
  ] as const)('Mac %s in a focused textarea types the character instead of firing', async (_label, init) => {
    const wrapper = await mountWebLayout()
    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    textarea.focus()
    homeNewChatProjectId.value = ''

    const event = press(init, textarea)
    await flushPromises()

    expect(event.defaultPrevented).toBe(false)
    expect(toggleModelPicker).not.toHaveBeenCalled()
    expect(pendingNewChat.value).toBeNull()

    textarea.remove()
    wrapper.unmount()
  })

  it('Mac ⌥N fires its shortcut when focus is on the body', async () => {
    const wrapper = await mountWebLayout()
    homeNewChatProjectId.value = ''
    const newChat = press({ key: 'Dead', code: 'KeyN' }, document.body)
    await flushPromises()
    expect(newChat.defaultPrevented).toBe(true)
    expect(pendingNewChat.value).not.toBeNull()
    pendingNewChat.value?.resolve(null)
    await flushPromises()

    wrapper.unmount()
  })

  it('leaves Alt+N alone while a textarea is focused', async () => {
    const wrapper = await mountWebLayout()
    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    textarea.focus()
    homeNewChatProjectId.value = ''

    const event = press({ key: 'n', code: 'KeyN' }, textarea)

    expect(event.defaultPrevented).toBe(false)
    expect(pendingNewChat.value).toBeNull()
    expect(archiveActiveChat).not.toHaveBeenCalled()

    textarea.remove()
    wrapper.unmount()
  })

  // The physical key decides when `code` is present: Option on the M key is
  // the model picker whatever character the layout reports.
  it('ignores the Option-produced character when code names another key', async () => {
    const wrapper = await mountWebLayout()
    const event = press({ key: 'm', code: 'KeyE' })
    expect(toggleModelPicker).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
    wrapper.unmount()
  })
})

// #1651: a chat route whose catalog entry has not arrived used to paint a
// completely empty pane. The route names a chat, the store has it selected,
// but `activeChat` is still null (the catalog is in flight), and every branch
// in the layout fell through — no header, no skeleton, no composer.
describe('ChatLayout chat route without a catalog entry', () => {
  class MemoryStorage {
    private values = new Map<string, string>()
    getItem(key: string): string | null { return this.values.get(key) ?? null }
    setItem(key: string, value: string): void { this.values.set(key, value) }
    removeItem(key: string): void { this.values.delete(key) }
    clear(): void { this.values.clear() }
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the pane skeleton while a chat route has no catalog entry', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
    })
    await router.push('/chat/chat-missing')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    // The catalog does not know this chat yet, so `activeChat` reads null
    // while the route and the selection already name it.
    store.chats = []
    store.activeChatId = 'chat-missing'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()

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
    await nextTick()

    // The pane says it is loading instead of showing nothing.
    const status = wrapper.find('[role="status"][aria-label="Loading conversation"]')
    expect(status.exists()).toBe(true)
    // Home is not the answer here: the route names a chat, so the home empty
    // state would be a different lie.
    expect(wrapper.find('.empty-shell').exists()).toBe(false)
    wrapper.unmount()
  })

  // The skeleton above covers a lookup that is still running. It must not be
  // the answer for a chat that does not exist: a dead deep link, a chat
  // deleted in another tab, or a chat the catalog dropped all resolve to
  // "no such chat", and the pane has to leave the placeholder behind.
  it('falls back to the home empty state when the chat route names a chat that does not exist', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
    })
    await router.push('/chat/chat-missing')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = []
    store.activeChatId = null
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
    // The deep link resolves to nothing: the chat is not in the catalog and
    // the server has no record of it either.
    vi.spyOn(store, 'openChatFromDeepLink').mockResolvedValue(undefined)

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
    await nextTick()

    // The lookup settled without producing a chat, so the placeholder is
    // gone and the pane is the home empty state, as it was before #1651.
    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(false)
    expect(wrapper.find('.empty-shell').exists()).toBe(true)
    wrapper.unmount()
  })

  // The two halves of the same story: the placeholder is up while the
  // lookup runs, and gone once the lookup comes back empty. A bound that
  // only knew "the route names a chat" would show the placeholder in both
  // moments; one that only knew "a lookup is running" would never show it
  // for the deep link the lookup never answers.
  it('shows the placeholder while the lookup runs and drops it when the lookup finds nothing', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
    })
    await router.push('/chat/chat-missing')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = []
    store.activeChatId = null
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
    // The lookup is still running.
    let releaseLookup: () => void = () => {}
    vi.spyOn(store, 'openChatFromDeepLink').mockImplementation(() => new Promise<void>((resolve) => {
      releaseLookup = () => resolve()
    }))

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
    await nextTick()

    // The lookup is in flight: the pane says it is loading.
    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(true)

    // The lookup comes back empty: the chat does not exist.
    releaseLookup()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(false)
    expect(wrapper.find('.empty-shell').exists()).toBe(true)
    wrapper.unmount()
  })

  // The same for a chat that was on screen and then went away: another tab
  // deleted it, so the `chat_deleted` event dropped it from the catalog and
  // cleared the selection while the route still names it. Nothing is on its
  // way any more, so the placeholder has to go with it.
  it('drops the placeholder when the open chat is deleted in another tab', async () => {
    const router = createRouter({
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
    vi.spyOn(store, 'openChatFromDeepLink').mockResolvedValue(undefined)

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
    await nextTick()

    // The chat is on screen, so there is no placeholder.
    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(false)

    // Another tab deletes it: the catalog loses the row and the selection is
    // cleared, but the route still names it.
    store.chats = []
    store.activeChatId = null
    await nextTick()

    // The pane does not fall back to "Loading conversation" for a chat that
    // is gone: it is the home empty state, as it was before #1651.
    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(false)
    expect(wrapper.find('.empty-shell').exists()).toBe(true)
    wrapper.unmount()
  })

  // The third way a chat stops being there: the 15s poll refreshes the
  // catalog and the chat is no longer in it, so the row disappears under the
  // open route. Same fall-through to the home empty state.
  it('drops the placeholder when the catalog poll drops the open chat', async () => {
    const router = createRouter({
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
    vi.spyOn(store, 'openChatFromDeepLink').mockResolvedValue(undefined)

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
    await nextTick()

    // The chat is on screen, so there is no placeholder.
    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(false)

    // The poll's payload no longer holds the chat: the row goes, and with
    // it the entry the pane was rendering.
    store.reconcileChatList([])
    await nextTick()

    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(false)
    expect(wrapper.find('.empty-shell').exists()).toBe(true)
    wrapper.unmount()
  })

  // The hole the round 2 review found: the selection still names the chat
  // after the poll dropped the row (`reconcileChatList` replaces `chats` and
  // does not touch `activeChatId`), so the route watcher's "the selection
  // already names this chat" branch runs and no lookup ever starts. Nothing
  // can then clear the pending state, and the pane sits on "Loading
  // conversation" for a conversation that does not exist.
  it('drops the placeholder when the selection names a chat the poll dropped', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/settings', component: EmptyStub },
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
    vi.spyOn(store, 'openChatFromDeepLink').mockResolvedValue(undefined)

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
    await nextTick()

    // The poll drops the row. The selection is not cleared with it, so the
    // store now names a chat the catalog does not have.
    store.reconcileChatList([])
    expect(store.activeChatId).toBe('chat-1')
    expect(store.activeChat).toBeNull()

    // Leaving to a secondary destination and back re-runs the route watcher
    // with the selection still naming the route's chat.
    await router.push('/settings')
    await flushPromises()
    await router.push('/chat/chat-1')
    await flushPromises()
    await nextTick()

    // No lookup is running and no chat is on screen, so the pane must not
    // sit on the placeholder for a conversation that does not exist.
    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(false)
    expect(wrapper.find('.empty-shell').exists()).toBe(true)
    wrapper.unmount()
  })

  // The same hole on the boot path: `onMounted` awaits `fetchAll()` without
  // a catch, so when the engine is unreachable the rejection skipped the
  // whole tail of the hook — including the deep-link lookup — and nothing
  // could clear the pending state. The pane sat on "Loading conversation"
  // instead of the home-boot skeleton that says the workspaces are still on
  // their way.
  it('shows the home-boot skeleton when the bootstrap cannot reach the engine', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: EmptyStub },
        { path: '/settings', component: EmptyStub },
        { path: '/chat/:chatId?', component: EmptyStub },
      ],
    })
    await router.push('/chat/chat-1')
    await router.isReady()

    const store = useProjectStore()
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
    store.chats = []
    store.activeChatId = 'chat-1'
    store.bootstrapped = false
    // The engine is unreachable: the bootstrap fails.
    vi.spyOn(store, 'fetchAll').mockRejectedValue(new Error('engine unreachable'))
    vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()

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
    await nextTick()

    // The workspaces are still on their way, and that is what the pane says:
    // not "Loading conversation", which claims a conversation is coming.
    expect(wrapper.find('[role="status"][aria-label="Loading conversation"]').exists()).toBe(false)
    expect(wrapper.find('[role="status"][aria-label="Loading your workspaces"]').exists()).toBe(true)
    wrapper.unmount()
  })

  describe('the slot beside the chat', () => {
    // The right-hand slot is one slot with two possible occupants: a pinned
    // file and the terminal. Whoever wins it, the divider and the ratio are
    // the same.
    const originalObserver = globalThis.ResizeObserver

    const PinnedFilePanelStub = defineComponent({
      name: 'PinnedFilePanel',
      setup: () => () => h('div', { 'data-testid': 'pinned-file-panel' }),
    })

    /**
     * The observers the layout created, so a test can report a new width to
     * them the way a real resize does, and the width they last reported.
     */
    let observers: Array<(width: number) => void> = []
    let reportedWidth = 0

    afterEach(() => {
      globalThis.ResizeObserver = originalObserver
      terminalPanelWanted.value = false
      observers = []
    })

    async function mountSlot(
      paneWidth: number,
      options: { pinned?: boolean; visible?: boolean; chats?: Array<Record<string, string>> } = {},
    ) {
      observers = []
      reportedWidth = paneWidth
      globalThis.ResizeObserver = class {
        constructor(private readonly cb: ResizeObserverCallback) {}
        observe() {
          const report = (width: number) => this.cb(
            [{ contentRect: { width } } as ResizeObserverEntry],
            this as unknown as ResizeObserver,
          )
          observers.push(report)
          report(reportedWidth)
        }
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver

      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })

      const router = createRouter({
        history: createMemoryHistory(),
        routes: [{ path: '/chat/:chatId?', component: EmptyStub }],
      })
      await router.push('/chat/chat-1')
      await router.isReady()

      const store = useProjectStore()
      store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
      store.chats = (options.chats ?? [{ chat_id: 'chat-1', project_id: 'project-1', title: 'Chat one' }]) as unknown as typeof store.chats
      store.activeChatId = 'chat-1'
      store.bootstrapped = true
      if (options.pinned) {
        store.applyChatPinState('chat-1', { path: 'src/pinned-file.ts', dismissed_paths: [], revision: 1 }, { source: 'test' })
      }
      vi.spyOn(store, 'fetchAll').mockResolvedValue()
      vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()

      // The terminal's panel is asked for, and the terminal is open: the
      // pane owns that state and tells the layout about it.
      terminalPanelWanted.value = true
      const terminal = useTerminalStore()
      terminal.visible = options.visible !== false
      if (terminal.visible) terminal.target = { chatId: 'chat-1' }

      const { default: ChatLayout } = await import('../ChatLayout.vue')
      const wrapper = mount(ChatLayout, {
        global: {
          plugins: [router],
          stubs: {
            ChatPanel: ChatPanelStub, ProjectSidebar: EmptyStub, ProjectView: EmptyStub,
            SchedulePanel: EmptyStub, SettingsView: EmptyStub, FileViewerModal: EmptyStub,
            PinnedFilePanel: PinnedFilePanelStub, PaneHeader: EmptyStub, HomeRecentChats: EmptyStub,
          },
        },
      })
      await flushPromises()
      await vi.waitFor(() => {
        expect(wrapper.find('.terminal-pane').exists()).toBe(true)
      })
      await flushPromises()
      return wrapper
    }

    /** A resize: the layout's observers report the new width. */
    function resizePane(width: number) {
      reportedWidth = width
      for (const notify of observers) notify(width)
    }

    it('gives the tile to the terminal when no file is pinned and the pane has room', async () => {
      const wrapper = await mountSlot(1200)
      // The tile is the slot's window, and the terminal is in it.
      const tile = wrapper.find('.chat-split-side')
      expect(tile.exists()).toBe(true)
      expect(tile.find('.terminal-pane--tile').exists()).toBe(true)
      // The divider is there, and it is a separator.
      const divider = wrapper.find('.chat-split-resizer')
      expect(divider.exists()).toBe(true)
      expect(divider.attributes('role')).toBe('separator')
      expect(divider.attributes('aria-orientation')).toBe('vertical')
      wrapper.unmount()
    })

    it('gives the tile to a pinned file and the terminal the drawer over the pane', async () => {
      const wrapper = await mountSlot(1200, { pinned: true })
      const tile = wrapper.find('.chat-split-side')
      expect(tile.exists()).toBe(true)
      // The file keeps the slot: the tile is the file panel's window.
      expect(tile.find('[data-testid="pinned-file-panel"]').exists()).toBe(true)
      // And the terminal is the drawer, out of the tile, over the pane.
      expect(tile.find('.terminal-pane').exists()).toBe(false)
      expect(wrapper.find('.terminal-pane--drawer').exists()).toBe(true)
      wrapper.unmount()
    })

    it('leaves the terminal the drawer when the pane is too narrow for both columns', async () => {
      const wrapper = await mountSlot(560)
      // The slot is closed: the chat takes the whole pane.
      expect(wrapper.find('.chat-split').exists()).toBe(false)
      expect(wrapper.find('.chat-split-side--closed').exists()).toBe(true)
      // And the terminal is the drawer over the pane.
      expect(wrapper.find('.terminal-pane--drawer').exists()).toBe(true)
      expect(wrapper.find('.terminal-pane--tile').exists()).toBe(false)
      wrapper.unmount()
    })

    it('needs room for the chat column, the tile and the slot chrome before the tile is used', async () => {
      // 360 chat + 360 tile + 24 of chrome = 744. A pane one pixel short of
      // that cannot hold both, so the terminal is the drawer.
      const narrow = await mountSlot(730)
      expect(narrow.find('.chat-split-side--closed').exists()).toBe(true)
      expect(narrow.find('.terminal-pane--drawer').exists()).toBe(true)
      narrow.unmount()

      const exact = await mountSlot(744)
      expect(exact.find('.chat-split-side--terminal').exists()).toBe(true)
      expect(exact.find('.terminal-pane--tile').exists()).toBe(true)
      exact.unmount()
    })

    it('moves the terminal between the tile and the drawer as the pane narrows and widens', async () => {
      const wrapper = await mountSlot(1200)
      expect(wrapper.find('.terminal-pane--tile').exists()).toBe(true)

      // The pane narrows: there is no longer room for both columns, so the
      // terminal becomes the drawer over the pane.
      resizePane(700)
      await flushPromises()
      expect(wrapper.find('.terminal-pane--tile').exists()).toBe(false)
      expect(wrapper.find('.terminal-pane--drawer').exists()).toBe(true)
      // It moved; it did not close.
      expect(wrapper.find('.terminal-pane--open').exists()).toBe(true)

      // And back again.
      resizePane(1200)
      await flushPromises()
      expect(wrapper.find('.terminal-pane--tile').exists()).toBe(true)
      expect(wrapper.find('.terminal-pane--drawer').exists()).toBe(false)
      expect(wrapper.find('.terminal-pane--open').exists()).toBe(true)
      wrapper.unmount()
    })

    it('gives the slot back when the terminal is hidden', async () => {
      const wrapper = await mountSlot(1200, { visible: false })
      // The slot is closed: no row, no divider, and the chat column takes
      // the whole pane (it carries no width of its own).
      expect(wrapper.find('.chat-split').exists()).toBe(false)
      expect(wrapper.find('.chat-split-resizer').exists()).toBe(false)
      expect(wrapper.find('.chat-split-side--closed').exists()).toBe(true)
      expect(wrapper.find('.chat-body').attributes('style')).toBeUndefined()
      // The terminal is still mounted behind the closed slot, and it is not
      // on screen.
      expect(wrapper.find('.terminal-pane').exists()).toBe(true)
      expect(wrapper.find('.terminal-pane--open').exists()).toBe(false)
      wrapper.unmount()
    })

    it('hides the terminal when another chat is opened', async () => {
      const wrapper = await mountSlot(1200, {
        chats: [
          { chat_id: 'chat-1', project_id: 'project-1', title: 'Chat one' },
          { chat_id: 'chat-2', project_id: 'project-1', title: 'Chat two' },
        ],
      })
      const store = useProjectStore()
      const follow = vi.spyOn(useTerminalStore(), 'followChat')
      vi.spyOn(store, 'openChatFromDeepLink').mockResolvedValue(undefined)
      expect(wrapper.find('.terminal-pane--open').exists()).toBe(true)

      await wrapper.vm.$router.push('/chat/chat-2')
      store.activeChatId = 'chat-2'
      await flushPromises()

      expect(follow).toHaveBeenCalledWith('chat-2')
      expect(wrapper.find('.terminal-pane--open').exists()).toBe(false)
      expect(wrapper.find('.chat-split-resizer').exists()).toBe(false)
      expect(wrapper.find('.chat-split-side--closed').exists()).toBe(true)
      wrapper.unmount()
    })
  })

  describe('notification inbox lifecycle', () => {
    const stubs = {
      ChatPanel: ChatPanelStub,
      ProjectSidebar: EmptyStub,
      ProjectView: EmptyStub,
      SchedulePanel: EmptyStub,
      SettingsView: EmptyStub,
      FileViewerModal: EmptyStub,
      PinnedFilePanel: EmptyStub,
      PaneHeader: EmptyStub,
    }

    async function chatRouter(path: string) {
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId?', component: EmptyStub }],
      })
      await router.push(path)
      await router.isReady()
      return router
    }

    it('direct_chat_load_populates_inbox_without_home', async () => {
      const router = await chatRouter('/chat/chat-aaaaaaaa')
      const store = useProjectStore()
      const signals = useTaskSignalsStore()
      const inbox = useNotificationInboxStore()
      vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
      const housekeeping = useHousekeepingStore()
      housekeeping.actionsHealth = { status: 'ready', lastSuccessAt: '2026-08-12T10:00:00.000Z', error: '' }
      housekeeping.updateTasksWorkspace = 'personal'
      housekeeping.updateTasksHealth = { status: 'ready', lastSuccessAt: '2026-08-12T10:00:00.000Z', error: '' }
      useTaskStore().schedulesLoaded = true
      useProposalsStore().loaded = true
      useVaultReviewStore().loadedWorkspace = 'personal'
      vi.spyOn(store, 'fetchAll').mockImplementation(async () => {
        store.projects = [{
          project_id: 'project-1', name: 'General', workspace: 'personal',
        }] as unknown as typeof store.projects
        // The routed chat is the one on screen, so it is read by definition;
        // the other chat carries the unread reply.
        store.chats = [{
          chat_id: 'chat-aaaaaaaa',
          project_id: 'project-1',
          title: 'Open chat',
          archived: false,
          local: true,
          last_activity_at: '2026-08-12T10:00:00Z',
          last_read_at: '2026-08-12T10:00:00Z',
        }, {
          chat_id: 'chat-bbbbbbbb',
          project_id: 'project-1',
          title: 'Unread reply',
          archived: false,
          local: true,
          last_activity_at: '2026-08-12T10:00:00Z',
          last_read_at: '2026-08-12T09:00:00Z',
        }] as unknown as typeof store.chats
        store.chatsHealth = { status: 'ready', lastSuccessAt: '2026-08-12T10:00:00.000Z', error: '' }
        signals.healthWorkspace = 'personal'
        signals.health = { status: 'ready', lastSuccessAt: '2026-08-12T10:00:00.000Z', error: '' }
        store.bootstrapped = true
      })

      const { default: ChatLayout } = await import('../ChatLayout.vue')
      const wrapper = mount(ChatLayout, { global: { plugins: [router], stubs } })
      await flushPromises()

      expect(inbox.initialized).toBe(true)
      expect(inbox.count).toBe(1)
      expect(inbox.rows.map((r) => r.key)).toEqual(['chat:chat-bbbbbbbb'])
      expect(inbox.overall).toBe('ready')
      expect(wrapper.find('.home-lane-status-text').exists()).toBe(false)
      wrapper.unmount()
    })

    it('init_called_once_after_bootstrap_and_disposed_on_unmount', async () => {
      const router = await chatRouter('/')
      const store = useProjectStore()
      const inbox = useNotificationInboxStore()
      const init = vi.spyOn(inbox, 'init')
      const dispose = vi.spyOn(inbox, 'dispose')
      vi.spyOn(store, 'fetchAll').mockResolvedValue()
      vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
      store.bootstrapped = true

      const { default: ChatLayout } = await import('../ChatLayout.vue')
      const wrapper = mount(ChatLayout, { global: { plugins: [router], stubs } })
      await flushPromises()
      expect(init).toHaveBeenCalledTimes(1)
      expect(dispose).not.toHaveBeenCalled()
      wrapper.unmount()
      expect(dispose).toHaveBeenCalledTimes(1)
    })

    it('init_still_runs_when_bootstrap_fails', async () => {
      const router = await chatRouter('/')
      const store = useProjectStore()
      const inbox = useNotificationInboxStore()
      const init = vi.spyOn(inbox, 'init')
      vi.spyOn(store, 'fetchAll').mockRejectedValue(new Error('engine down'))
      const fetchSchedules = vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()

      const { default: ChatLayout } = await import('../ChatLayout.vue')
      const wrapper = mount(ChatLayout, { global: { plugins: [router], stubs } })
      await flushPromises()
      expect(init).toHaveBeenCalledTimes(1)
      expect(fetchSchedules).not.toHaveBeenCalled()
      wrapper.unmount()
    })
  })
})
