// @vitest-environment jsdom

import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useProjectStore } from '../../stores/projects'
import CommandPaletteModal from '../CommandPaletteModal.vue'

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/chat/:chatId', component: { template: '<div />' } },
      { path: '/project/:projectId', component: { template: '<div />' } },
      { path: '/schedules', component: { template: '<div />' } },
      { path: '/settings', component: { template: '<div />' } },
    ],
  })
}

describe('CommandPaletteModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('creates a chat in the active project', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const router = makeRouter()
    await router.push('/chat/chat-1')
    await router.isReady()
    const store = useProjectStore()
    store.projects = [{
      project_id: 'project-1',
      name: 'Project One',
      workspace: 'personal',
    }] as typeof store.projects
    store.chats = [{
      chat_id: 'chat-1',
      project_id: 'project-1',
      title: 'Existing chat',
    }] as typeof store.chats
    store.activeChatId = 'chat-1'
    const createChat = vi.spyOn(store, 'createChat').mockResolvedValue({
      chat_id: 'chat-2',
      project_id: 'project-1',
      title: 'New Chat',
    } as Awaited<ReturnType<typeof store.createChat>>)

    const wrapper = mount(CommandPaletteModal, {
      props: { modelValue: true },
      global: { plugins: [pinia, router] },
    })
    const newChat = wrapper.findAll('button').find(button => button.text().includes('New Chat'))

    await newChat?.trigger('click')
    await flushPromises()

    expect(createChat).toHaveBeenCalledWith('project-1')
    expect(router.currentRoute.value.path).toBe('/chat/chat-2')
    wrapper.unmount()
  })

  it('keeps keyboard selection valid when filtering narrows the list', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const router = makeRouter()
    await router.push('/')
    await router.isReady()
    const store = useProjectStore()
    store.projects = [{
      project_id: 'project-1',
      name: 'Project One',
      workspace: 'personal',
    }] as typeof store.projects
    store.chats = Array.from({ length: 12 }, (_, index) => ({
      chat_id: `chat-${index}`,
      project_id: 'project-1',
      title: `Chat ${index}`,
    })) as typeof store.chats

    const wrapper = mount(CommandPaletteModal, {
      props: { modelValue: true },
      global: { plugins: [pinia, router] },
    })
    const input = wrapper.get('input')
    for (let index = 0; index < 10; index++) {
      await input.trigger('keydown', { key: 'ArrowDown' })
    }

    await input.setValue('Settings')
    await input.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/settings')
    wrapper.unmount()
  })
})
