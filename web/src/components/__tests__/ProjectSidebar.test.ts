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
      default_model: 'sonnet',
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
    expect(chatsLink.attributes('aria-label')).toBe('chats — 1 need attention')

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
