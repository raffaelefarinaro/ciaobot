// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ProjectSidebar from '../ProjectSidebar.vue'
import { useProjectStore } from '../../stores/projects'

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
        stubs: { NotificationBell: true },
      },
    })

    const unread = wrapper.get('.chat-item .chat-signal--unread')
    expect(unread.attributes('aria-label')).toBe('Unread chat')
    expect(unread.attributes('title')).toBe('Unread chat')

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
        stubs: { NotificationBell: true },
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
        stubs: { NotificationBell: true },
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

  it('collapses and expands a supervisor subchat group', async () => {
    const store = useProjectStore()
    store.chats.push({
      ...store.chats[0],
      chat_id: 'subchat-5678-efgh',
      title: 'Child chat',
      spawned_from_chat_id: chatId,
      created_at: '2026-07-29T00:01:00Z',
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
        stubs: { NotificationBell: true },
      },
    })

    expect(wrapper.findAll('.chat-item')).toHaveLength(2)
    const toggle = wrapper.get('[aria-label="Collapse subchats for Copy me"]')
    expect(toggle.attributes('aria-expanded')).toBe('true')

    await toggle.trigger('click')

    expect(wrapper.findAll('.chat-item')).toHaveLength(1)
    expect(toggle.attributes('aria-expanded')).toBe('false')

    store.activeChatId = 'subchat-5678-efgh'
    await nextTick()

    expect(wrapper.findAll('.chat-item')).toHaveLength(2)
    expect(toggle.attributes('aria-expanded')).toBe('true')

    await toggle.trigger('click')
    expect(wrapper.findAll('.chat-item')).toHaveLength(1)

    await toggle.trigger('click')

    expect(wrapper.findAll('.chat-item')).toHaveLength(2)
    expect(toggle.attributes('aria-expanded')).toBe('true')

    wrapper.unmount()
  })
})
