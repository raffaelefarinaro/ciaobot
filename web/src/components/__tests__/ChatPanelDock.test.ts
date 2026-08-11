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
    return () => h('header', { class: 'pane-header' }, [
      slots.title?.(),
      h('div', { class: 'header-actions' }, slots.actions?.()),
    ])
  },
})

const ChildStub = defineComponent({ name: 'ChildStub', setup: () => () => h('div') })

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
  models: ['sonnet', 'opus'],
  default: 'sonnet',
  provider_models: { claude: ['sonnet', 'opus'] },
  provider_defaults: { claude: 'sonnet' },
  thinking_levels: {},
  model_reasoning_levels: {},
  model_options: {},
  backends: { anthropic: true },
}

function makeProject(): ProjectInfo {
  return {
    project_id: 'project-1',
    name: 'Upwordo',
    workspace: 'personal',
    context: '',
    created_at: '',
    order: 0,
    vault_folder: '',
  } as unknown as ProjectInfo
}

function makeChat(chatId: string, extra: Partial<ChatInfo> = {}): ChatInfo {
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title: chatId,
    model: 'sonnet',
    provider: 'claude',
    mode: 'normal',
    session_id: '',
    created_at: '2026-08-01T00:00:00Z',
    archived: false,
    local: true,
    ...extra,
  } as ChatInfo
}

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
  clear() { this.values.clear() }
}

async function mountPanel(setup: (store: ReturnType<typeof useProjectStore>) => void = () => {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat('chat-1')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [] }
  store.workspaces = [
    { name: 'personal', color: 'emerald' },
    { name: 'work', color: 'pink' },
  ] as unknown as typeof store.workspaces
  store.bootstrapped = true
  setup(store)

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path === '/api/commands') return Promise.resolve({ commands: [] }) as never
    return Promise.resolve([]) as never
  })

  const wrapper = shallowMount(ChatPanel, {
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
  return { wrapper, store }
}

function approval(id: string, tool: string) {
  return { request_id: id, tool_name: tool, tool_input: { cmd: 'ls' } }
}

function questionPayload() {
  return JSON.stringify({ questions: [{ question: 'Which label?', options: [{ label: 'triage' }] }] })
}

// ChatPanel's dock reads store.activeQuestions (the live, in-turn picker), while
// chatNeedsInput also considers the persisted pending_question on the chat.
function liveQuestion() {
  return [{ question: 'Which label?', options: [{ label: 'triage' }, { label: 'backlog' }] }]
}

describe('ChatPanel context bar', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => vi.restoreAllMocks())

  it('is absent when the chat has no relations', async () => {
    const { wrapper } = await mountPanel()
    expect(wrapper.find('.ctx-bar').exists()).toBe(false)
    wrapper.unmount()
  })

  // The four banner blocks were each a v-for, so a chat with all of them opened
  // with its first message below the fold. Collapsed is now one line.
  it('collapses every relation into counted chips, detail hidden', async () => {
    const { wrapper } = await mountPanel(store => {
      store.chats = [
        makeChat('chat-1', { spawned_from_chat_id: 'parent-1' }),
        makeChat('parent-1', { title: 'Supervisor' }),
        makeChat('kid-1', { spawned_from_chat_id: 'chat-1' }),
        makeChat('kid-2', { spawned_from_chat_id: 'chat-1' }),
      ]
    })

    expect(wrapper.find('.ctx-bar').exists()).toBe(true)
    // Chip text carries a leading glyph, so match on substring.
    const labels = wrapper.findAll('.ctx-chip').map(c => c.text())
    expect(labels.some(l => l.includes('delegate'))).toBe(true)
    expect(labels.some(l => l.includes('2 subchats'))).toBe(true)
    // Detail rows stay behind the disclosure.
    expect(wrapper.find('.ctx-detail').exists()).toBe(false)
    expect(wrapper.findAll('.loop-banner-row')).toHaveLength(0)
    wrapper.unmount()
  })

  it('expands to the detail rows on click', async () => {
    const { wrapper } = await mountPanel(store => {
      store.chats = [
        makeChat('chat-1', { spawned_from_chat_id: 'parent-1' }),
        makeChat('parent-1', { title: 'Supervisor' }),
      ]
    })

    await wrapper.get('.ctx-summary').trigger('click')
    expect(wrapper.find('.ctx-detail').exists()).toBe(true)
    expect(wrapper.find('.loop-banner-row').exists()).toBe(true)
    wrapper.unmount()
  })

  // A blocked subchat must not be hidden behind a disclosure.
  it('surfaces a needs-you subchat on the collapsed bar', async () => {
    const { wrapper } = await mountPanel(store => {
      store.chats = [
        makeChat('chat-1'),
        makeChat('kid-1', { spawned_from_chat_id: 'chat-1', pending_question: questionPayload() }),
      ]
    })
    expect(wrapper.find('.ctx-attn').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('ChatPanel action dock', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => vi.restoreAllMocks())

  // A permission blocks a tool mid-turn; a question blocks the turn's end. The
  // tighter deadline wins.
  it('expands the permission and defers the question', async () => {
    const { wrapper } = await mountPanel(store => {
      store.activeQuestions = { 'chat-1': liveQuestion() } as unknown as typeof store.activeQuestions
      store.pendingPermissions = { 'chat-1': [approval('r1', 'Bash')] } as unknown as typeof store.pendingPermissions
    })

    expect(wrapper.find('.permission-card').exists()).toBe(true)
    expect(wrapper.find('.question-card').exists()).toBe(false)
    expect(wrapper.find('.dock-strip').text()).toContain('a question waiting')
    wrapper.unmount()
  })

  it('shows only the first of several permissions and counts the rest', async () => {
    const { wrapper } = await mountPanel(store => {
      store.pendingPermissions = {
        'chat-1': [approval('r1', 'Bash'), approval('r2', 'Write'), approval('r3', 'Edit')],
      } as unknown as typeof store.pendingPermissions
    })

    expect(wrapper.findAll('.permission-card')).toHaveLength(1)
    expect(wrapper.find('.dock-strip').text()).toContain('2 more permissions')
    wrapper.unmount()
  })

  it('reveals every deferred item when the strip is expanded', async () => {
    const { wrapper } = await mountPanel(store => {
      store.activeQuestions = { 'chat-1': liveQuestion() } as unknown as typeof store.activeQuestions
      store.pendingPermissions = {
        'chat-1': [approval('r1', 'Bash'), approval('r2', 'Write')],
      } as unknown as typeof store.pendingPermissions
    })

    await wrapper.get('.dock-strip').trigger('click')
    expect(wrapper.findAll('.permission-card')).toHaveLength(2)
    expect(wrapper.find('.question-card').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows the question expanded when nothing outranks it', async () => {
    const { wrapper } = await mountPanel(store => {
      store.activeQuestions = { 'chat-1': liveQuestion() } as unknown as typeof store.activeQuestions
    })
    expect(wrapper.find('.question-card').exists()).toBe(true)
    wrapper.unmount()
  })

  // The count used to render three times in this one view.
  it('reports background agents on the strip and not as a separate bar', async () => {
    const { wrapper } = await mountPanel(store => {
      store.backgroundAgents = { 'chat-1': 3 }
    })
    expect(wrapper.find('.bg-agents-bar').exists()).toBe(false)
    expect(wrapper.find('.dock-strip').text()).toContain('3 agents running')
    wrapper.unmount()
  })

  // The strip used to be gated purely on dockDeferred, every entry of which
  // disappears once dockExpanded is true — so clicking it unmounted the control
  // and left no way back, with focus dropping to <body>.
  it('survives expansion so the disclosure works both ways', async () => {
    const { wrapper } = await mountPanel(store => {
      store.pendingPermissions = {
        'chat-1': [approval('r1', 'Bash'), approval('r2', 'Write')],
      } as unknown as typeof store.pendingPermissions
    })

    expect(wrapper.find('.dock-strip').exists()).toBe(true)
    await wrapper.get('.dock-strip').trigger('click')
    expect(wrapper.findAll('.permission-card')).toHaveLength(2)
    expect(wrapper.find('.dock-strip').exists()).toBe(true)

    await wrapper.get('.dock-strip').trigger('click')
    expect(wrapper.findAll('.permission-card')).toHaveLength(1)
    wrapper.unmount()
  })

  // An explicit role="status" on the button would override its implicit button
  // role and drop aria-expanded.
  it('keeps the strip an operable button for assistive tech', async () => {
    const { wrapper } = await mountPanel(store => {
      store.backgroundAgents = { 'chat-1': 2 }
    })
    const strip = wrapper.get('.dock-strip')
    expect(strip.element.tagName).toBe('BUTTON')
    expect(strip.attributes('role')).toBeUndefined()
    expect(strip.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.dock-strip-items').attributes('aria-live')).toBe('polite')
    wrapper.unmount()
  })
})


describe('ChatPanel workspace breadcrumb', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => vi.restoreAllMocks())

  it('names the workspace with its number key and hue', async () => {
    const { wrapper } = await mountPanel()
    const crumb = wrapper.find('.breadcrumb-workspace')
    expect(crumb.exists()).toBe(true)
    expect(crumb.text()).toContain('personal')
    expect(crumb.attributes('data-workspace-color')).toBe('emerald')
    expect(wrapper.find('.breadcrumb-key').text()).toBe('1')
    wrapper.unmount()
  })
})
