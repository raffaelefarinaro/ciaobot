// @vitest-environment jsdom

// The 1-9 workspace shortcuts, the sidebar's scope marks and the New chat
// picker all read `store.workspaceOptions`, so a persisted reorder must change
// what each digit does without any of them being edited.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import ProjectSidebar from '../ProjectSidebar.vue'
import NewChatPicker from '../NewChatPicker.vue'
import { openNewChatPicker, pendingNewChat } from '../../lib/newChat'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import type { WorkspaceInfo } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn().mockResolvedValue({ rows: [] }), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const EmptyStub = defineComponent({ name: 'EmptyStub', setup: () => () => h('div') })
const ChatPanelStub = defineComponent({
  name: 'ChatPanel',
  emits: ['close'],
  setup(_, { expose }) {
    expose({ toggleModelPicker: vi.fn(), handleSendShortcut: vi.fn(() => true), archiveActiveChat: vi.fn() })
    return () => h('div')
  },
})

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

const ws = (name: string): WorkspaceInfo =>
  ({ name, vault_root: '', default_provider: 'claude', gws_profile: '' }) as WorkspaceInfo

const original = ['alpha', 'beta', 'gamma']
const reordered = ['gamma', 'alpha', 'beta']

function useOrder(store: ReturnType<typeof useProjectStore>, names: string[]) {
  store.workspaces = names.map(ws)
}

beforeEach(() => {
  setActivePinia(createPinia())
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
})

afterEach(() => {
  document.body.innerHTML = ''
  pendingNewChat.value?.resolve(null)
  vi.restoreAllMocks()
})

describe('workspace shortcuts follow the persisted order', () => {
  it('the sidebar switcher marks each workspace with its new position', async () => {
    const store = useProjectStore()
    useOrder(store, original)
    store.activeWorkspace = 'alpha'
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })
    const marks = async () => {
      const trigger = wrapper.get('.workspace-scope-trigger')
      if (!wrapper.find('.workspace-scope-menu').exists()) await trigger.trigger('click')
      return wrapper.findAll('.workspace-scope-menu .workspace-scope-option')
        .filter(option => option.find('.workspace-scope-mark').exists() && option.attributes('role') === 'menuitem')
        .filter(option => !option.classes().includes('workspace-scope-link'))
        .map(option => `${option.get('.workspace-scope-name').text()}=${option.get('.workspace-scope-mark').text()}`)
    }

    expect((await marks()).slice(0, 3)).toEqual(['Alpha=1', 'Beta=2', 'Gamma=3'])
    useOrder(store, reordered)
    await nextTick()
    expect((await marks()).slice(0, 3)).toEqual(['Gamma=1', 'Alpha=2', 'Beta=3'])
    wrapper.unmount()
  })

  it('digit 1 in ChatLayout switches to the new first workspace', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()
    const store = useProjectStore()
    useOrder(store, reordered)
    store.activeWorkspace = 'beta'
    store.bootstrapped = true
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
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

    window.dispatchEvent(new KeyboardEvent('keydown', { key: '1', cancelable: true }))
    await flushPromises()
    expect(switchWorkspace).toHaveBeenLastCalledWith('gamma', { transition: true })

    window.dispatchEvent(new KeyboardEvent('keydown', { key: '3', cancelable: true }))
    await flushPromises()
    expect(switchWorkspace).toHaveBeenLastCalledWith('beta', { transition: true })
    wrapper.unmount()
  })

  it('digits in the New chat picker preview the new order', async () => {
    const store = useProjectStore()
    useOrder(store, reordered)
    store.projects = reordered.map(name => ({
      project_id: `p-${name}`, name: `${name}-proj`, workspace: name, is_auto: false, context: '', created_at: '', order: 0, vault_folder: '',
    })) as unknown as typeof store.projects
    store.activeWorkspace = 'beta'
    const wrapper = mount(NewChatPicker, { attachTo: document.body })
    void openNewChatPicker()
    await nextTick()
    await nextTick()
    await new Promise<void>(resolve => setTimeout(resolve, 0))

    window.dispatchEvent(new KeyboardEvent('keydown', { key: '1', cancelable: true }))
    await nextTick()
    await nextTick()
    // The picker previews the workspace in slot 1 (gamma), not the old first.
    const names = wrapper.findAll('.newchat-name').map(el => el.text())
    expect(names).toContain('gamma-proj')
    expect(names).not.toContain('beta-proj')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: '2', cancelable: true }))
    await nextTick()
    await nextTick()
    const second = wrapper.findAll('.newchat-name').map(el => el.text())
    expect(second).toContain('alpha-proj')
    expect(second).not.toContain('gamma-proj')
    wrapper.unmount()
  })
})
