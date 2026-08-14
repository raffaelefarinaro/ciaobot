// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { useFontScale } from '../../composables/useFontScale'

const toggleDictation = vi.fn()
const toggleModelPicker = vi.fn()

const ChatPanelStub = defineComponent({
  name: 'ChatPanel',
  emits: ['close'],
  setup(_, { emit, expose }) {
    expose({ toggleDictation, toggleModelPicker })
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
    toggleModelPicker.mockReset()
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

    expect(wrapper.find('.empty-status-text').text()).toBe('1 chat needs your attention. no agents working.')
    wrapper.unmount()
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
      { name: 'personal', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', model_bucket: '' },
      { name: 'work', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', model_bucket: '' },
      { name: 'client', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', model_bucket: '' },
    ]
    store.activeWorkspace = 'personal'
    store.bootstrapped = true
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')
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
      { name: 'personal', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', model_bucket: '' },
      { name: 'work', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', model_bucket: '' },
    ]
    store.activeWorkspace = 'personal'
    store.bootstrapped = true
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')
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

  // The sidebar toggle follows the same split as the other modifier
  // shortcuts: Cmd+S in the desktop app, Option+S in the PWA, because a
  // browser has already spent Cmd+S on Save Page.
  it.each([
    ['the desktop app', true, { key: 's', metaKey: true }],
    ['the web PWA', undefined, { key: 's', altKey: true }],
  ] as const)('toggles the sidebar in %s', async (_label, desktopFlag, keyInit) => {
    window.__CIAOBOT_DESKTOP__ = desktopFlag
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

    // Starts expanded, so the first press collapses and the second restores —
    // asserting both directions, since "opens and closes" is the whole point.
    expect(wrapper.find('.chat-layout').classes()).toContain('sidebar-open')

    const collapse = new KeyboardEvent('keydown', { ...keyInit, cancelable: true })
    window.dispatchEvent(collapse)
    await nextTick()
    expect(wrapper.find('.chat-layout').classes()).not.toContain('sidebar-open')
    expect(collapse.defaultPrevented).toBe(true)

    window.dispatchEvent(new KeyboardEvent('keydown', { ...keyInit, cancelable: true }))
    await nextTick()
    expect(wrapper.find('.chat-layout').classes()).toContain('sidebar-open')

    wrapper.unmount()
  })

  // The model picker follows the same split: Cmd+Shift+M in the desktop app,
  // Option+M in the PWA, because macOS reserves plain Cmd+M for Minimize
  // Window. Needs an active chat, like dictation.
  it.each([
    ['the desktop app', true, { key: 'm', metaKey: true, shiftKey: true }],
    ['the web PWA', undefined, { key: 'm', altKey: true }],
  ] as const)('opens the model picker in %s', async (_label, desktopFlag, keyInit) => {
    window.__CIAOBOT_DESKTOP__ = desktopFlag
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

    const event = new KeyboardEvent('keydown', { ...keyInit, cancelable: true })
    window.dispatchEvent(event)

    expect(toggleModelPicker).toHaveBeenCalledOnce()
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it('leaves the sidebar alone when the shortcut fires inside a text field', async () => {
    // Option+S is how you type ß, so stealing it mid-composition would break
    // text entry for the sake of a view toggle.
    window.__CIAOBOT_DESKTOP__ = undefined
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

  it('increments --font-scale by the shared step on Cmd/Ctrl+Shift+=', async () => {
    // The shortcut should be available in both the desktop app and the PWA:
    // it is the platform's primary modifier. The step, bounds, and
    // persistence key must match Settings → Appearance so the two surfaces
    // stay in sync. Asserts both: the CSS variable is bumped and the value
    // is persisted to localStorage.
    window.__CIAOBOT_DESKTOP__ = true
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    // Seed through the composable, not localStorage: the scale ref is
    // module-scoped and shared, so writing storage after import would not
    // move it. Driving the real API is also what the app does.
    useFontScale().set(1.2)

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
    useFontScale().set(1.2)

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
    useFontScale().set(0.8)

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
    window.__CIAOBOT_DESKTOP__ = undefined
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
      ],
    })
    await router.push(startPath)
    await router.isReady()

    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', model_bucket: '' },
      { name: 'work', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', model_bucket: '' },
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
    taskStore.loops = [] as unknown as typeof taskStore.loops
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()

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
        },
      },
    })
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('renders the recent-chat lanes on the home screen', async () => {
    const wrapper = await mountHome()
    expect(wrapper.findAll('.home-lane')).toHaveLength(2)
    wrapper.unmount()
  })

  // The global "+ personal chat / + work chat" pair used to stay on screen at
  // narrow widths, where it duplicated each lane's own "+ new" and spent a
  // saturated fill on a non-blocking action.
  it('drops the global new-chat buttons once any chat exists', async () => {
    const wrapper = await mountHome()
    expect(wrapper.find('.empty-actions').exists()).toBe(false)
    expect(wrapper.findAll('.home-lane-new').length).toBeGreaterThan(0)
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

  it('moves focus across lanes from a window keydown', async () => {
    const wrapper = await mountHome()
    const cards = wrapper.findAll('.home-chat-item')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[2].element)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)
    wrapper.unmount()
  })

  it('works in the PWA, not just the desktop app', async () => {
    window.__CIAOBOT_DESKTOP__ = undefined
    const wrapper = await mountHome()
    const cards = wrapper.findAll('.home-chat-item')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)
    wrapper.unmount()
  })

  it('keeps number keys as workspace shortcuts from home', async () => {
    window.__CIAOBOT_DESKTOP__ = undefined
    const wrapper = await mountHome()
    const store = useProjectStore()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: '2', bubbles: true }))
    await flushPromises()
    expect(store.activeWorkspace).toBe('work')
    wrapper.unmount()
  })

  // Esc used to do nothing at all on settings and automations, because those
  // views are excluded from shortcutsActive. It is the universal way back.
  it('returns to home on Esc from a full-screen view', async () => {
    window.__CIAOBOT_DESKTOP__ = undefined
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
    window.__CIAOBOT_DESKTOP__ = undefined
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

  // Reported in review: activeChatId stays populated when Settings is opened
  // from a chat, so a chat-first Esc ran closeChat() on a chat that was not on
  // screen - disconnecting it, and deleting it outright when it was an unused
  // draft with an unsent composer message.
  it('leaves a retained hidden chat alone when escaping Settings', async () => {
    window.__CIAOBOT_DESKTOP__ = undefined
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    await router.push('/settings')
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
    expect(store.activeChatId).toBe('chat-1')
    wrapper.unmount()
  })

  it('does the same escaping Automations', async () => {
    window.__CIAOBOT_DESKTOP__ = undefined
    const wrapper = await mountHome()
    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    await router.push('/schedules')
    await flushPromises()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
    expect(store.activeChatId).toBe('chat-1')
    wrapper.unmount()
  })

  // A popover that handled Escape itself must not also navigate away.
  it('defers to a nested control that consumed Escape', async () => {
    window.__CIAOBOT_DESKTOP__ = undefined
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
    const cards = wrapper.findAll('.home-chat-item')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[0].element)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(cards[2].element)
    wrapper.unmount()
  })
})
