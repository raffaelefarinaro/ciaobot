// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import ProjectSidebar from '../ProjectSidebar.vue'
import { useProjectStore } from '../../stores/projects'

const chatId = 'chat-1234-abcd'

describe('ProjectSidebar chat actions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [{
      name: 'personal',
      vault_root: '/tmp/vault',
      default_provider: 'claude',
      default_model: 'sonnet',
      gws_profile: '',
      model_bucket: '',
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
})
