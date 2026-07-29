// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { useProductTourStore } from '../../stores/productTour'

const ChatPanelStub = defineComponent({
  name: 'ChatPanel',
  emits: ['close'],
  setup(_, { emit }) {
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
    }] as any
    store.chats = [{
      chat_id: 'chat-1',
      project_id: 'project-1',
      title: 'Test chat',
    }] as any
    store.activeChatId = 'chat-1'
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()
    vi.spyOn(useProductTourStore(), 'maybeAutoStart').mockResolvedValue()

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
          ProductTour: EmptyStub,
          OnboardingCard: EmptyStub,
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
})
