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
        VoiceRecorder: ChildStub,
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

  it('keeps the latest reply’s action row visible and overlays the older ones', async () => {
    const wrapper = await mountPanel(TURNS)
    const replies = wrapper.findAll('.message-wrap.assistant')
    expect(replies).toHaveLength(2)

    const older = replies[0].get('.message-actions')
    const latest = replies[1].get('.message-actions')
    expect(older.classes()).not.toContain('message-actions--pinned')
    expect(latest.classes()).toContain('message-actions--pinned')
    // Text actions, with the turn footer on the right of the latest row.
    expect(latest.text()).toContain('Copy')
    expect(latest.text()).toContain('Fork from here')
    expect(latest.get('.message-meta').text()).toContain('7.0s')

    // The user's own message offers Copy only.
    const request = wrapper.findAll('.message-wrap.user')[0].get('.message-actions')
    expect(request.text()).toContain('Copy')
    expect(request.text()).not.toContain('Fork')
    wrapper.unmount()
  })

  it('shows Work details as a rail on a wide pane and toggles it from the header', async () => {
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
    expect(rail.text()).toContain('injected with each message')
    // The project is named in the rail, so the header no longer repeats it.
    expect(rail.find('.chat-rail-project').exists()).toBe(true)
    expect(wrapper.find('.breadcrumb-scope').exists()).toBe(false)
    expect(rail.text()).toContain('Launch planning for the public beta.')
    expect(rail.text()).toContain('Current state')

    const toggle = wrapper.get('.work-inspector-trigger')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.attributes('aria-controls')).toBe('chat-work-rail')
    await toggle.trigger('click')
    expect(wrapper.find('#chat-work-rail').exists()).toBe(false)
    // With the rail hidden the header names the project again.
    expect(wrapper.find('.breadcrumb-scope').exists()).toBe(true)
    // The drawer is the narrow-pane form; it does not open on a wide pane.
    expect(wrapper.find('.chat-work-inspector').exists()).toBe(false)
    wrapper.unmount()
  })
})
