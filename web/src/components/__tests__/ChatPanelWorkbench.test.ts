// @vitest-environment jsdom
//
// The aligned chat layout: the model trigger in the composer, the per-message
// action row, and Work details shown as a rail on a wide pane.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatInfo, ChatMessage, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'

const PaneHeaderStub = defineComponent({
  name: 'PaneHeaderStub',
  setup(_, { slots }) {
    return () => h('header', { class: 'pane-header' }, [
      slots.title?.(),
      h('div', { class: 'header-actions' }, slots.actions?.()),
    ])
  },
})

const ChildStub = defineComponent({
  name: 'ChildStub',
  setup() {
    return () => h('div', { class: 'child-stub' })
  },
})

const ChatCommentPopoverStub = defineComponent({
  name: 'ChatCommentPopoverStub',
  setup(_, { expose }) {
    expose({
      openId: null,
      close: vi.fn(),
      clearPendingClose: vi.fn(),
      onTargetOver: vi.fn(),
      onTargetOut: vi.fn(),
      pinFromEvent: vi.fn(),
      show: vi.fn(),
    })
    return () => h('div')
  },
})

const MODELS_RESPONSE = {
  models: ['haiku', 'sonnet', 'opus'],
  default: 'sonnet',
  provider_models: { claude: ['haiku', 'sonnet', 'opus'] },
  provider_defaults: { claude: 'sonnet' },
  thinking_levels: {},
  model_reasoning_levels: {},
  model_options: {},
  backends: { anthropic: true },
}

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

function project(): ProjectInfo {
  return {
    project_id: 'project-1',
    name: 'Launch notes',
    workspace: 'personal',
    context: 'Launch planning for the public beta.',
    created_at: '',
    order: 0,
    vault_folder: '',
  }
}

function chat(): ChatInfo {
  return {
    chat_id: 'chat-1',
    project_id: 'project-1',
    title: 'Prepare launch brief',
    model: 'sonnet',
    provider: 'claude',
    mode: 'normal',
    session_id: '',
    created_at: '',
    archived: false,
  }
}

const TURNS: ChatMessage[] = [
  { role: 'user', content: 'first request', timestamp: '2026-09-25T10:00:00Z', turn_index: 0 },
  { role: 'assistant', content: 'first answer', timestamp: '2026-09-25T10:00:05Z', effective_model: 'sonnet', duration_ms: 5000 },
  { role: 'user', content: 'second request', timestamp: '2026-09-25T10:01:00Z', turn_index: 1 },
  { role: 'assistant', content: 'second answer', timestamp: '2026-09-25T10:01:07Z', effective_model: 'sonnet', duration_ms: 7000 },
]

async function mountPanel(messages: ChatMessage[] = []): Promise<VueWrapper> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [project()]
  store.chats = [chat()]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': messages }
  store.bootstrapped = true
  vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })
  const wrapper = shallowMount(ChatPanel, {
    attachTo: document.body,
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
      },
    },
  })
  await flushPromises()
  return wrapper
}

describe('ChatPanel aligned layout', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('puts the model trigger in the composer bar, not the header', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.find('.pane-header .model-picker-summary').exists()).toBe(false)
    const trigger = wrapper.get('.composer-bar .model-picker-summary')
    expect(trigger.text()).toContain('sonnet')
    expect(trigger.attributes('aria-expanded')).toBe('false')

    await trigger.trigger('click')
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('.composer-model .child-stub').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a message\'s actions only once it is selected, and Esc puts it back', async () => {
    const wrapper = await mountPanel(TURNS)
    const replies = wrapper.findAll('.message-wrap.assistant')
    expect(replies).toHaveLength(2)
    expect(wrapper.find('.message-wrap--selected').exists()).toBe(false)
    expect(wrapper.find('.message-select-backdrop').exists()).toBe(false)

    // Click the reply (not a link or button inside it) to select it.
    await replies[0].get('.message-row').trigger('click')
    expect(replies[0].classes()).toContain('message-wrap--selected')
    expect(wrapper.find('.message-select-backdrop').exists()).toBe(true)
    const actions = replies[0].get('.message-actions')
    expect(actions.text()).toContain('Copy')
    expect(actions.text()).toContain('Fork from here')

    // Esc deselects without closing the chat.
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await nextTick()
    expect(wrapper.find('.message-wrap--selected').exists()).toBe(false)
    expect(wrapper.emitted('close')).toBeUndefined()

    // Enter on a focused message selects it too; the user's own offers Copy only.
    const request = wrapper.findAll('.message-wrap.user')[0]
    await request.get('.message-row').trigger('keydown', { key: 'Enter' })
    expect(request.classes()).toContain('message-wrap--selected')
    expect(request.get('.message-actions').text()).toContain('Copy')
    expect(request.get('.message-actions').text()).not.toContain('Fork')

    // Clicking the veil deselects.
    await wrapper.get('.message-select-backdrop').trigger('click')
    expect(wrapper.find('.message-wrap--selected').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows Work details as a rail on a wide pane, hidden from its heading and reopened from the info tab', async () => {
    // ChatPanel observes more than one element; report a wide pane to all.
    const observers: Array<() => void> = []
    vi.stubGlobal('ResizeObserver', class {
      private cb: ResizeObserverCallback
      constructor(cb: ResizeObserverCallback) { this.cb = cb }
      observe() {
        observers.push(() => this.cb([{ contentRect: { width: 1200 } } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver))
      }
      disconnect() {}
      unobserve() {}
    })
    const wrapper = await mountPanel(TURNS)
    observers.forEach(fire => fire())
    await nextTick()

    const rail = wrapper.get('#chat-work-rail')
    // The project is named in the rail's Agent context, so the header no
    // longer repeats it.
    expect(rail.findComponent({ name: 'AgentContextSection' }).exists()).toBe(true)
    expect(wrapper.find('.breadcrumb-scope').exists()).toBe(false)
    expect(rail.text()).not.toContain('Current state')

    // The header carries Archive only, as a labelled primary button; the
    // toggle lives with Work details.
    const actions = wrapper.get('.pane-header .header-actions')
    expect(actions.findAll('button').map(b => b.text())).toEqual(['Archive'])
    expect(actions.get('button').classes()).toContain('btn-primary')
    expect(wrapper.find('.work-inspector-trigger').exists()).toBe(false)
    const hide = rail.get('.chat-rail-hide')
    expect(hide.attributes('aria-controls')).toBe('chat-work-rail')
    await hide.trigger('click')
    expect(wrapper.find('#chat-work-rail').exists()).toBe(false)
    // With the rail hidden the header names the project again.
    expect(wrapper.find('.breadcrumb-scope').exists()).toBe(true)

    const reopen = wrapper.get('.work-inspector-trigger')
    expect(reopen.attributes('aria-controls')).toBe('chat-work-rail')
    await reopen.trigger('click')
    expect(wrapper.find('#chat-work-rail').exists()).toBe(true)
    // The drawer is the narrow-pane form; it does not open on a wide pane.
    expect(wrapper.find('.chat-work-inspector').exists()).toBe(false)
    wrapper.unmount()
  })
})
