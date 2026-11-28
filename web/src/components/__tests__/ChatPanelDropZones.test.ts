// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'

const PaneHeaderStub = defineComponent({
  name: 'PaneHeaderStub',
  setup(_, { slots }) {
    return () => h('header', [slots.title?.(), h('div', slots.actions?.())])
  },
})
const ChatCommentPopoverStub = defineComponent({
  name: 'ChatCommentPopoverStub',
  setup(_, { expose }) {
    expose({
      openId: null, close: vi.fn(), clearPendingClose: vi.fn(), onTargetOver: vi.fn(),
      onTargetOut: vi.fn(), pinFromEvent: vi.fn(), show: vi.fn(),
    })
    return () => h('div')
  },
})
const ChildStub = defineComponent({ name: 'ChildStub', setup: () => () => h('div') })

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

const project: ProjectInfo = {
  project_id: 'project-1', name: 'General', workspace: 'personal',
  context: '', created_at: '', order: 0, vault_folder: '',
}
const chat: ChatInfo = {
  chat_id: 'chat-1', project_id: 'project-1', title: 'chat-1', model: 'sonnet',
  provider: 'claude', mode: 'normal', session_id: '', created_at: '', archived: false,
}

async function mountPanel() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [project]
  store.chats = [chat]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [] }
  store.bootstrapped = true
  vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })
  const wrapper = shallowMount(ChatPanel, {
      props: { chatId: 'chat-1' },
    global: {
      plugins: [pinia],
      stubs: { PaneHeader: PaneHeaderStub, RouterLink: ChildStub, ChatCommentPopover: ChatCommentPopoverStub, CommentComposePopover: ChildStub },
    },
  })
  await flushPromises()
  return { wrapper, store }
}

function setHost(hostname: string, native: boolean): void {
  Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, hostname } })
  const w = window as unknown as { webkit?: unknown }
  if (native) w.webkit = { messageHandlers: { ciaobotNativeDrop: { postMessage: vi.fn(async () => null) } } }
  else delete w.webkit
}

async function dragOver(wrapper: Awaited<ReturnType<typeof mountPanel>>['wrapper']) {
  await wrapper.find('.chat-panel').trigger('dragover')
}

describe('ChatPanel drop zones', () => {
  const originalLocation = window.location
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
    delete (window as unknown as { webkit?: unknown }).webkit
    vi.restoreAllMocks()
  })

  it('offers attach a copy and use in place in the Mac app on its own engine', async () => {
    setHost('127.0.0.1', true)
    const { wrapper } = await mountPanel()
    await dragOver(wrapper)

    const zones = wrapper.findAll('.drop-zone')
    expect(zones.map(z => z.find('.drop-zone__title').text())).toEqual(['Attach a copy', 'Use in place'])
    expect(zones[1].text()).toContain('Nothing is copied')
    expect(wrapper.find('.in-place-btn').attributes('aria-label')).toBe('Use a file or folder in place (path only, nothing copied)')
    wrapper.unmount()
  })

  it('says plainly why a browser tab on the engine machine can only attach a copy', async () => {
    setHost('127.0.0.1', false)
    const { wrapper } = await mountPanel()
    await dragOver(wrapper)

    expect(wrapper.findAll('.drop-zone')).toHaveLength(1)
    expect(wrapper.find('.drop-zone__note').text()).toContain('A browser tab cannot see where a file lives')
    expect(wrapper.find('.in-place-btn').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps a remote device to one attach line', async () => {
    setHost('mac.tailnet.ts.net', false)
    const { wrapper } = await mountPanel()
    await dragOver(wrapper)

    expect(wrapper.find('.drop-overlay').text()).toBe('Drop files to attach a copy to the project')
    expect(wrapper.find('.drop-zone').exists()).toBe(false)
    expect(wrapper.find('.in-place-btn').exists()).toBe(false)
    wrapper.unmount()
  })
})
