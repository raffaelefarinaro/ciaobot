// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ProjectSidebar from '../ProjectSidebar.vue'
import { useProjectStore } from '../../stores/projects'
import { useHousekeepingStore } from '../../stores/housekeeping'

const chatId = 'chat-1234-abcd'

describe('ProjectSidebar chat actions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.unread = {}
    store.workspaces = [{
      name: 'personal',
      vault_root: '/tmp/vault',
      default_provider: 'claude',
      gws_profile: '',
    }]
    store.projects = [{
      project_id: 'project-1',
      name: 'General',
      workspace: 'personal',
      context: '',
      created_at: '2026-07-29T00:00:00Z',
      order: 0,
      vault_folder: 'general',
      is_auto: true,
    }]
    store.chats = [{
      chat_id: chatId,
      project_id: 'project-1',
      title: 'Copy me',
      model: 'sonnet',
      provider: 'claude',
      mode: 'default',
      session_id: 'session-1',
      created_at: '2026-07-29T00:00:00Z',
      archived: false,
    }]
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('shows a notification dot beside an unread chat', async () => {
    const store = useProjectStore()
    store.chats[0].last_activity_at = '2026-08-12T10:00:00Z'
    store.chats[0].last_read_at = '2026-08-12T09:00:00Z'
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: {
        plugins: [router],
      },
    })

    const unread = wrapper.get('.chat-item .chat-signal--unread')
    expect(unread.attributes('aria-label')).toBe('Unread chat')
    expect(unread.attributes('title')).toBe('Unread chat')

    wrapper.unmount()
  })

  it('shows the global attention count on the chats rail item', async () => {
    const store = useProjectStore()
    store.chats[0].last_activity_at = '2026-08-12T10:00:00Z'
    store.chats[0].last_read_at = '2026-08-12T09:00:00Z'
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })

    const chatsLink = wrapper.get('a[href="/"]')
    expect(chatsLink.get('.nav-item-badge--count').text()).toBe('1')
    expect(chatsLink.attributes('aria-label')).toBe('Today — 1 chat needs attention')

    wrapper.unmount()
  })

  it('copies the selected chat ID from the action menu', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: {
        plugins: [router],
      },
    })

    await wrapper.get('[aria-label="Chat actions"]').trigger('click')
    const copyButton = Array.from(document.body.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Copy chat ID')
    expect(copyButton).toBeTruthy()
    copyButton!.click()
    await flushPromises()

    expect(writeText).toHaveBeenCalledOnce()
    expect(writeText).toHaveBeenCalledWith(chatId)
    expect(useProjectStore().toasts.at(-1)).toMatchObject({
      chat_id: chatId,
      title: 'Chat ID copied',
      body: chatId,
    })

    wrapper.unmount()
  })

  it('moves a chat when it is dropped onto another project', async () => {
    const store = useProjectStore()
    store.projects.push({
      project_id: 'project-2',
      name: 'Second project',
      workspace: 'personal',
      context: '',
      created_at: '2026-07-29T00:00:00Z',
      order: 1,
      vault_folder: '',
      is_auto: false,
    })
    const moveChat = vi.spyOn(store, 'moveChat').mockResolvedValue({
      ...store.chats[0],
      project_id: 'project-2',
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: {
        plugins: [router],
      },
    })

    const chat = wrapper.get('.chat-item')
    const dataTransfer = {
      effectAllowed: '',
      setData: vi.fn(),
    }
    const dragStart = new Event('dragstart', { bubbles: true })
    Object.defineProperty(dragStart, 'dataTransfer', { value: dataTransfer })
    chat.element.dispatchEvent(dragStart)

    const target = wrapper.findAll('.project-header')[1]
    await target.trigger('dragover')
    expect(target.classes()).toContain('drag-over')
    await target.trigger('drop')
    await flushPromises()

    expect(dataTransfer.setData).toHaveBeenCalledWith('application/x-ciaobot-chat', chatId)
    expect(moveChat).toHaveBeenCalledWith(chatId, 'project-2')

    wrapper.unmount()
  })

  it('collapses and expands a chat\'s running-subagent group', async () => {
    const store = useProjectStore()
    store.runningSubagents = {
      [chatId]: [
        { agent_id: 'a1b2c3d4', description: 'Sweep the callers', subagent_type: 'Explore', status: 'running' },
      ],
    }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/chat/:chatId/subagent/:agentId', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: {
        plugins: [router],
      },
    })

    // The chat row plus its one subagent row (which reuses .chat-item).
    expect(wrapper.findAll('.chat-item')).toHaveLength(2)
    const row = wrapper.get('.subagent-item')
    expect(row.text()).toContain('Sweep the callers')
    expect(row.attributes('href')).toBe(`/chat/${chatId}/subagent/a1b2c3d4`)

    const toggle = wrapper.get('[aria-label="Collapse subagents for Copy me"]')
    expect(toggle.attributes('aria-expanded')).toBe('true')

    await toggle.trigger('click')

    expect(wrapper.findAll('.chat-item')).toHaveLength(1)
    expect(toggle.attributes('aria-expanded')).toBe('false')

    // Opening the subagent's own view must reopen the group it lives in.
    await router.push(`/chat/${chatId}/subagent/a1b2c3d4`)
    await nextTick()

    expect(wrapper.findAll('.chat-item')).toHaveLength(2)
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('.subagent-item').classes()).toContain('active')

    wrapper.unmount()
  })

  // A finished subagent is not archived and leaves no row behind: the poll
  // stops listing it, and the transcript stays in the chat's Activity trace.
  it('drops a subagent row once the agent stops running', async () => {
    const store = useProjectStore()
    store.runningSubagents = {
      [chatId]: [{ agent_id: 'a1b2c3d4', description: 'Sweep the callers', status: 'running' }],
    }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/chat/:chatId/subagent/:agentId', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })
    expect(wrapper.findAll('.subagent-item')).toHaveLength(1)

    store.runningSubagents = {}
    await nextTick()

    expect(wrapper.findAll('.subagent-item')).toHaveLength(0)
    expect(wrapper.findAll('.chat-item')).toHaveLength(1)

    wrapper.unmount()
  })
})

describe('ProjectSidebar global new chat', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [{
      name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '',
    }]
    store.activeWorkspace = 'personal'
    store.projects = [{
      project_id: 'project-1', name: 'General', workspace: 'personal', context: '',
      created_at: '2026-07-29T00:00:00Z', order: 0, vault_folder: 'general', is_auto: true,
    }]
    store.chats = []
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  async function mountSidebar(mode: 'chat' | 'memory') {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()
    return mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode },
      global: { plugins: [router] },
    })
  }

  it('offers one New chat above the tree, opening the shared picker', async () => {
    const { pendingNewChat } = await import('../../lib/newChat')
    const wrapper = await mountSidebar('chat')
    const button = wrapper.get('.sidebar-new-chat')

    expect(button.attributes('aria-haspopup')).toBe('dialog')
    expect(button.attributes('aria-label')).toBe('New chat in Personal')
    expect(button.text()).toContain('New chat')

    await button.trigger('click')
    // The shared picker is the one project-selection path; the sidebar just
    // opens it in the active workspace rather than growing its own selector.
    expect(pendingNewChat.value?.options).toEqual({ workspace: 'personal', projectId: undefined })
    pendingNewChat.value?.resolve(null)
    await nextTick()
    wrapper.unmount()
  })

  it('hides the global New chat in modes without a project tree', async () => {
    const wrapper = await mountSidebar('memory')
    expect(wrapper.find('.sidebar-new-chat').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('ProjectSidebar update badge', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  function mountSidebar() {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    return router.push('/').then(() => router.isReady()).then(() => mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: {
        plugins: [router],
      },
    }))
  }

  it('shows a pulsing dot on the settings nav item when an update is available', async () => {
    const store = useProjectStore()
    store.packageStatus = {
      current_version: '0.9.1',
      latest_version: '9.9.9',
      update_available: true,
      mode: 'bundled_app',
    }
    const wrapper = await mountSidebar()
    await nextTick()

    const settingsLink = wrapper.get('a[href="/settings"]')
    expect(settingsLink.find('.nav-item-badge').exists()).toBe(true)

    wrapper.unmount()
  })

  it('shows no badge when already up to date', async () => {
    const store = useProjectStore()
    store.packageStatus = {
      current_version: '0.9.1',
      latest_version: '0.9.1',
      update_available: false,
      mode: 'bundled_app',
    }
    const wrapper = await mountSidebar()
    await nextTick()

    const settingsLink = wrapper.get('a[href="/settings"]')
    expect(settingsLink.find('.nav-item-badge').exists()).toBe(false)

    wrapper.unmount()
  })

  it('shows one warning dot for a blocking housekeeping action', async () => {
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [{
      id: 'gws-login',
      kind: 'gws_health',
      severity: 1,
      title: 'Sign in to gws',
      detail: 'The token needs attention.',
      glyph: '!',
      workspace: '',
      run_label: 'Open settings',
      chat_label: '',
      chat_prompt: '',
      view_label: 'Settings',
      view_route: '/settings',
      blocking: true,
    }]
    vi.spyOn(housekeeping, 'init').mockImplementation(() => {})

    const wrapper = await mountSidebar()
    const settingsLink = wrapper.get('a[href="/settings"]')

    expect(settingsLink.classes()).toContain('nav-item--warning')
    expect(settingsLink.find('.nav-item-badge--warning').exists()).toBe(true)
    expect(settingsLink.attributes('aria-label')).toBe('settings — action required')

    wrapper.unmount()
  })
})

describe('ProjectSidebar accessible context menus', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [{
      name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '',
    }]
    store.projects = [{
      project_id: 'project-1', name: 'Notes', workspace: 'personal', context: '',
      created_at: '2026-07-29T00:00:00Z', order: 0, vault_folder: '', is_auto: false,
    }, {
      project_id: 'project-2', name: 'Archive', workspace: 'personal', context: '',
      created_at: '2026-07-29T00:00:00Z', order: 1, vault_folder: '', is_auto: false,
    }]
    store.chats = [{
      chat_id: 'chat-menu', project_id: 'project-1', title: 'Menu chat', model: 'sonnet',
      provider: 'claude', mode: 'default', session_id: 'session-menu',
      created_at: '2026-07-29T00:00:00Z', archived: false,
    }]
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  async function mountSidebar() {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()
    return mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })
  }

  it('opens a right-click chat menu at the pointer and dismisses outside', async () => {
    const wrapper = await mountSidebar()
    const row = wrapper.get('.chat-item')
    const event = new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, clientX: 140, clientY: 90, button: 2,
    })
    row.element.dispatchEvent(event)
    await flushPromises()

    const menu = document.body.querySelector<HTMLElement>('[data-reka-menu-content]')
    expect(menu).not.toBeNull()
    expect(['right', 'left']).toContain(menu?.getAttribute('data-side'))
    expect(menu?.textContent).toContain('Copy chat ID')
    expect(event.defaultPrevented).toBe(true)

    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
    await flushPromises()
    expect(document.body.querySelector('[data-reka-menu-content]')).toBeNull()
    wrapper.unmount()
  })

  it('exposes the same menu through the 44px keyboard/touch action trigger', async () => {
    const wrapper = await mountSidebar()
    const trigger = wrapper.get<HTMLButtonElement>('[aria-label="Chat actions"]')
    trigger.element.focus()
    await trigger.trigger('click')
    await flushPromises()

    expect(document.body.querySelector('[data-reka-menu-content]')?.getAttribute('role')).toBe('menu')
    expect(document.body.textContent).toContain('Move to...')
    // The explicit trigger remains a native button with a touch-sized class;
    // unlike a hover-only affordance it is reachable on a phone.
    expect(trigger.element.tagName).toBe('BUTTON')
    expect(trigger.classes()).toContain('chat-actions-btn')
    wrapper.unmount()
  })

  it('keeps project actions keyboard-reachable while retaining pointer context placement', async () => {
    const wrapper = await mountSidebar()
    const projectHeader = wrapper.findAll('.project-header')[0]
    const event = new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, clientX: 220, clientY: 120, button: 2,
    })
    projectHeader.element.dispatchEvent(event)
    await flushPromises()
    expect(document.body.textContent).toContain('Rename')
    expect(document.body.textContent).toContain('Delete')

    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
    await flushPromises()
    const action = wrapper.get<HTMLButtonElement>('[aria-label="Project actions"]')
    await action.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[data-reka-menu-content]')).not.toBeNull()
    expect(action.classes()).toContain('project-actions-btn')
    wrapper.unmount()
  })

  it('keeps the Move submenu mounted for keyboard opening and returns to its parent', async () => {
    const wrapper = await mountSidebar()
    await wrapper.get('[aria-label="Chat actions"]').trigger('click')
    await flushPromises()

    const move = Array.from(document.body.querySelectorAll<HTMLElement>('button'))
      .find(button => button.textContent?.trim() === 'Move to...')!
    move.focus()
    move.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }))
    await flushPromises()

    const menus = document.body.querySelectorAll('[data-reka-menu-content]')
    expect(menus.length).toBe(2)
    expect(menus[1].textContent).toContain('Archive')

    const back = Array.from(document.body.querySelectorAll<HTMLElement>('button'))
      .find(button => button.textContent?.includes('Back'))!
    back.click()
    await flushPromises()
    expect(document.body.querySelectorAll('[data-reka-menu-content]')).toHaveLength(1)
    wrapper.unmount()
  })
})
