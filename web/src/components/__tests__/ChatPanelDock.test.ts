// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick } from 'vue'
import { flushPromises, RouterLinkStub, shallowMount } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatInfo, ProjectInfo, Schedule } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatLoopChip from '../ChatLoopChip.vue'
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
  provider_models: {
    claude: ['sonnet', 'opus'],
    opencode: ['anthropic/claude-sonnet-4-6'],
  },
  provider_defaults: { claude: 'sonnet', opencode: 'anthropic/claude-sonnet-4-6' },
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

// The interval cadence that replaced loops: bound to this chat via
// `web_chat_id`, so it is what the context bar counts and renders.
function makeIntervalSchedule(scheduleId: string): Schedule {
  return {
    schedule_id: scheduleId,
    prompt: 'tick',
    web_chat_id: 'chat-1',
    created_at: '2026-08-01T00:00:00Z',
    frequency: 'interval',
    interval_minutes: 5,
    title: scheduleId,
    enabled: true,
    last_status: '',
    context_label: '',
    next_run: null,
  } as unknown as Schedule
}

async function mountPanel(
  setup: (store: ReturnType<typeof useProjectStore>) => void = () => {},
  schedules: Schedule[] = [],
  options: { pinia?: ReturnType<typeof createPinia>; apiGet?: (path: string) => unknown } = {},
) {
  const pinia = options.pinia ?? createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  if (!options.pinia) {
    store.projects = [makeProject()]
    store.chats = [makeChat('chat-1')]
    store.activeChatId = 'chat-1'
    store.messages = { 'chat-1': [] }
    store.workspaces = [
      { name: 'personal', color: 'emerald' },
      { name: 'work', color: 'pink' },
    ] as unknown as typeof store.workspaces
    store.bootstrapped = true
  }
  setup(store)

  const taskStore = useTaskStore()
  taskStore.schedules = schedules
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    const handled = options.apiGet?.(path)
    if (handled !== undefined) return Promise.resolve(handled) as never
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path === '/api/commands') return Promise.resolve({ commands: [] }) as never
    return Promise.resolve([]) as never
  })

  const wrapper = shallowMount(ChatPanel, {
      props: { chatId: 'chat-1' },
    attachTo: document.body,
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: RouterLinkStub,
        // Real: the shared title row holds the close button.
        ItemTitleRow: false,
      },
    },
  })
  await flushPromises()
  return { wrapper, store, pinia }
}

function approval(id: string, tool: string) {
  return { request_id: id, tool_name: tool, tool_input: { cmd: 'ls' } }
}

// ChatPanel's dock reads store.activeQuestions (the live, in-turn picker), while
// chatNeedsInput also considers the persisted pending_question on the chat.
function liveQuestion() {
  return [{ question: 'Which label?', options: [{ label: 'triage' }, { label: 'backlog' }] }]
}

describe('ChatPanel automation origin', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => vi.restoreAllMocks())

  // Automation controls used to sit above the transcript whenever the rail was
  // hidden. They are gone: the rail names the automation, its page holds the
  // controls.
  it('has no automation controls bar when Chat details is hidden', async () => {
    const { wrapper } = await mountPanel(undefined, [makeIntervalSchedule('schedule-1')])
    expect(wrapper.find('.ctx-bar').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Automation controls')
    wrapper.unmount()
  })

  // With Chat details shown as a rail, the automation is one line at the top of
  // it naming where the chat came from; cadence and controls stay on its page.
  it('names the automation at the top of the Chat details rail on a wide pane', async () => {
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
    const { wrapper } = await mountPanel(undefined, [makeIntervalSchedule('schedule-1')])
    observers.forEach(fire => fire())
    await nextTick()

    expect(wrapper.find('.ctx-bar').exists()).toBe(false)
    const rail = wrapper.get('#chat-work-rail')
    expect(rail.element.firstElementChild?.classList.contains('chat-rail-head')).toBe(true)
    const origin = rail.get('.chat-rail-origin')
    expect(rail.element.children[1]).toBe(origin.element)
    expect(origin.get('.cd-heading').text()).toBe('Origin')
    const row = origin.getComponent(RouterLinkStub)
    expect(row.props('to')).toBe('/schedules/schedule-1')
    expect(row.get('.cd-primary').text()).toBe('schedule-1')
    expect(row.get('.cd-detail').text()).toBe('Automation')
    expect(origin.findAll('button')).toHaveLength(0)
    expect(rail.findAll('.cd-heading').map(h => h.text())).toEqual(['Origin', 'Open', 'Files'])
    vi.unstubAllGlobals()
    wrapper.unmount()
  })

  // A chat's own loop reads as a loop in the origin row and gets its chip in
  // the composer bar; the same schedule bound to the chat without an owner is
  // still an automation.
  it('names a chat loop as a loop in the origin row and wires its chip into the composer bar', async () => {
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
    const loop = {
      ...makeIntervalSchedule('loop-1'),
      owner_chat_id: 'chat-1',
      expires_at: '2099-01-01T14:30:00Z',
      max_runs: 10,
      run_count: 2,
    } as Schedule
    const { wrapper } = await mountPanel(undefined, [loop, makeIntervalSchedule('schedule-1')])
    observers.forEach(fire => fire())
    await nextTick()

    const rows = wrapper.findAll('.chat-rail-origin-row')
    const details = rows.map(row => row.get('.cd-detail').text())
    expect(details).toContain('Automation')
    expect(details.some(text => text.startsWith('Loop · ends '))).toBe(true)
    // The panel is shallow-mounted here, so the chip is checked as wired: its
    // own chat, in the composer bar. Its loop display is tested in ChatLoopChip.test.ts.
    const chip = wrapper.get('.composer-bar').findComponent(ChatLoopChip)
    expect(chip.exists()).toBe(true)
    expect(chip.props('chatId')).toBe('chat-1')
    vi.unstubAllGlobals()
    wrapper.unmount()
  })

  it('shows no Origin section for an ordinary chat', async () => {
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
    const { wrapper } = await mountPanel(undefined, [])
    observers.forEach(fire => fire())
    await nextTick()

    const rail = wrapper.get('#chat-work-rail')
    expect(rail.find('.chat-rail-origin').exists()).toBe(false)
    expect(rail.get('.cd-heading').text()).toBe('Open')
    vi.unstubAllGlobals()
    wrapper.unmount()
  })

  it('stacks Open and Files in that order under one heading style', async () => {
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
    const { wrapper } = await mountPanel()
    observers.forEach(fire => fire())
    await nextTick()

    const rail = wrapper.get('#chat-work-rail')
    const headings = rail.findAll('.cd-heading').map(h => h.text())
    expect(headings).not.toContain('Running')
    expect(headings.indexOf('Open')).toBeGreaterThanOrEqual(0)
    expect(headings.indexOf('Files')).toBeGreaterThan(headings.indexOf('Open'))
    expect(rail.find('.chat-rail-open .chat-rail-terminal').exists()).toBe(true)
    expect(rail.get('.chat-rail-open').element.compareDocumentPosition(rail.get('.chat-rail-agent-context').element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    vi.unstubAllGlobals()
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
      store.pendingPermissions = {
        'chat-1': [approval('r1', 'Bash'), approval('r2', 'Write')],
      } as unknown as typeof store.pendingPermissions
    })
    const strip = wrapper.get('.dock-strip')
    expect(strip.element.tagName).toBe('BUTTON')
    expect(strip.attributes('role')).toBeUndefined()
    expect(strip.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.dock-strip-items').attributes('aria-live')).toBe('polite')
    wrapper.unmount()
  })
})


describe('ChatPanel project context', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => vi.restoreAllMocks())

  it('keeps the project name out of the chat title header', async () => {
    const { wrapper, store } = await mountPanel()
    expect(wrapper.find('.breadcrumb-workspace').exists()).toBe(false)
    expect(store.projects[0].name).toBe('Upwordo')
    expect(wrapper.find('.breadcrumb-project').exists()).toBe(false)
    expect(wrapper.find('.breadcrumb-scope').exists()).toBe(false)
    expect(wrapper.find('#project-context-popup').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the project name out of the title header for General too', async () => {
    const { wrapper } = await mountPanel(store => {
      store.projects = [{ ...store.projects[0], name: 'General' }]
    })
    expect(wrapper.find('.breadcrumb-project').exists()).toBe(false)
    expect(wrapper.find('.breadcrumb-scope').exists()).toBe(false)
    wrapper.unmount()
  })

  it('closes the chat from a labelled icon button', async () => {
    const { wrapper } = await mountPanel()
    const close = wrapper.find('.close-btn')
    expect(close.classes()).toContain('btn-icon')
    expect(close.attributes('aria-label')).toBe('Close chat')
    expect(close.find('svg').exists()).toBe(true)
    expect(close.text()).toBe('')
    await close.trigger('click')
    expect(wrapper.emitted('close')?.length).toBe(1)
    wrapper.unmount()
  })
})

describe('ChatPanel model summary', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => vi.restoreAllMocks())

  it('shows a provider default for legacy chats and hides the auto thinking label', async () => {
    const { wrapper } = await mountPanel(store => {
      store.chats = [makeChat('chat-1', {
        model: '',
        provider: 'opencode',
        mode: 'auto',
        thinking_level: 'auto',
      })]
    })

    const summary = wrapper.get('.model-picker-summary')
    // The readable name, never the raw id.
    expect(summary.text()).toContain('Claude Sonnet-4-6')
    expect(summary.text()).not.toContain('think:auto')
    wrapper.unmount()
  })
})

describe('ChatPanel Chat details skills', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })
  afterEach(() => vi.restoreAllMocks())

  // The rail's Skills and MCP section was missing an opencode skill because its
  // tool input names the skill `id`, not `name` (#2863). It must read the real
  // transcript both ways: the persisted rows a reload hydrates, and the live
  // timeline the socket fills.
  function wideRail() {
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
    return observers
  }

  const HISTORY = [
    { i: 0, role: 'user', content: 'Use the loop skill', sent_at: '2026-08-01T00:00:00Z', turn_index: 0 },
    { i: 1, role: 'system', content: '⚙️ skill {"id": "loop-engineering"}\n🧩 Skill pr-review', tool_name: '_activity' },
    { i: 2, role: 'assistant', content: 'Finished.', timestamp: '2026-08-01T00:00:10Z' },
  ]
  const ENVELOPE = { items: HISTORY, total: HISTORY.length, offset: 0, limit: 50, hasMore: false, nextOffset: null }

  it('shows the opencode skill id from persisted activity in a fresh panel', async () => {
    const apiGet = (path: string) => path === '/api/chats/chat-1/messages?limit=50' ? ENVELOPE : undefined
    const observers = wideRail()
    const { wrapper, store, pinia } = await mountPanel(undefined, [], { apiGet })
    await store.loadMessages('chat-1')
    wrapper.unmount()

    const fresh = await mountPanel(undefined, [], { pinia, apiGet })
    observers.forEach(fire => fire())
    await nextTick()

    const section = fresh.wrapper.get('#chat-work-rail .chat-rail-tools')
    expect(section.get('.cd-heading').text()).toBe('Skills and MCP')
    expect(section.get('.cd-count').text()).toBe('2')
    const rows = section.findAll('.chat-rail-tool')
    expect(rows.map(row => row.get('.cd-primary').text())).toEqual(['loop-engineering', 'pr-review'])
    for (const row of rows) {
      expect(row.get('.cd-detail').text()).toBe('Skill')
      expect(row.find('.cd-value').exists()).toBe(false)
    }
    vi.unstubAllGlobals()
    fresh.wrapper.unmount()
  })

  it('shows a live opencode skill once, before and after the turn settles', async () => {
    const sockets: FakeChatSocket[] = []
    class FakeChatSocket {
      static OPEN = 1
      static CLOSED = 3
      readyState = FakeChatSocket.OPEN
      onopen: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onclose: ((event: { code?: number }) => void) | null = null
      onerror: (() => void) | null = null
      constructor(public url: string) {
        sockets.push(this)
        queueMicrotask(() => this.onopen?.())
      }
      send = vi.fn()
      close() { this.readyState = FakeChatSocket.CLOSED; this.onclose?.({}) }
    }
    vi.stubGlobal('WebSocket', FakeChatSocket)
    const observers = wideRail()

    const { wrapper, store } = await mountPanel()
    observers.forEach(fire => fire())
    await nextTick()
    store.connectWs('chat-1')
    const socket = sockets[0]
    const send = (event: Record<string, unknown>) => socket.onmessage?.({ data: JSON.stringify(event) })

    send({ type: 'tool_use', tool_name: 'skill', tool_input: '{"id":"loop-engineering"}', tool_use_id: 'sk1' })
    send({ type: 'tool_use', tool_name: 'Skill', tool_input: 'pr-review' })
    await nextTick()

    const skillRows = () => wrapper.get('#chat-work-rail .chat-rail-tools')
    const loop = () => skillRows().findAll('.chat-rail-tool').find(row => row.text().includes('loop-engineering'))!
    expect(loop().get('.cd-detail').text()).toBe('Skill')
    expect(loop().find('.cd-value').exists()).toBe(false)
    expect(skillRows().findAll('.chat-rail-tool')).toHaveLength(2)

    send({ type: 'tool_result', tool_name: 'skill', tool_use_id: 'sk1' })
    send({ type: 'result', text: 'Finished.', is_error: false, effective_model: 'opus', usage: {}, session_id: 's1' })
    await flushPromises()

    expect(skillRows().findAll('.chat-rail-tool')).toHaveLength(2)
    expect(loop().get('.cd-detail').text()).toBe('Skill')
    expect(loop().find('.cd-value').exists()).toBe(false)
    store.disconnectWs('chat-1')
    vi.unstubAllGlobals()
    wrapper.unmount()
  })
})
