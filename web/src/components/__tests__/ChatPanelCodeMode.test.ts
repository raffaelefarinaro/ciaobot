// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { api } from '../../lib/api'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'
import ChatRepoSection from '../ChatRepoSection.vue'

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
    return () => h('div')
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
  models: ['haiku', 'sonnet', 'opus', 'fable'],
  default: 'sonnet',
  provider_models: { claude: ['haiku', 'sonnet', 'opus', 'fable'] },
  provider_defaults: { claude: 'sonnet' },
  thinking_levels: {},
  model_reasoning_levels: {},
  model_options: {},
  backends: { anthropic: true },
}

function makeProject(): ProjectInfo {
  return {
    project_id: 'project-1',
    name: 'General',
    workspace: 'personal',
    context: '',
    created_at: '',
    order: 0,
    vault_folder: '',
  }
}

function makeChat(chatId: string): ChatInfo {
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title: chatId,
    model: 'sonnet',
    provider: 'opencode',
    mode: 'normal',
    session_id: '',
    created_at: '',
    archived: false,
  }
}

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

async function mountPanel(opts: { realRepoSection?: boolean } = {}): Promise<{ wrapper: VueWrapper }> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat('chat-1')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [] }
  store.bootstrapped = true

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })

  const wrapper = shallowMount(ChatPanel, {
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
        ...(opts.realRepoSection ? { ChatRepoSection: false, HostTerminalButton: ChildStub } : {}),
      },
    },
  })
  await flushPromises()
  return { wrapper }
}

describe('ChatPanel code mode rows', () => {
  const scrollProto = Element.prototype as unknown as { scrollTo?: unknown }
  const originalScrollTo = scrollProto.scrollTo

  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
    scrollProto.scrollTo = function scrollTo() {}
  })

  afterEach(() => {
    scrollProto.scrollTo = originalScrollTo
    vi.restoreAllMocks()
    localStorage.clear()
  })

  const turns = () => [
    { role: 'user' as const, content: 'first', timestamp: '2026-08-30T13:23:00Z', turn_index: 0 },
    { role: 'assistant' as const, content: 'one', timestamp: '2026-08-30T13:23:10Z' },
    { role: 'user' as const, content: 'second', timestamp: '2026-08-30T13:24:00Z', turn_index: 1 },
    { role: 'assistant' as const, content: 'two', timestamp: '2026-08-30T13:24:10Z' },
  ]

  it('shows no code mode divider in the transcript', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.chats[0].code_mode = true
    store.messages['chat-1'] = turns()
    await flushPromises()
    expect(wrapper.text()).not.toContain('Switched to code mode')
    expect(wrapper.text()).not.toContain('Left code mode')
  })

  it('locks the switch on with a hint once the chat has messages', async () => {
    const { wrapper } = await mountPanel({ realRepoSection: true })
    const store = useProjectStore()
    store.projects[0].repo = { path: '/r/app', branch: 'main', dirty: false, missing: false, instructions: null }
    store.chats[0].code_mode = true
    store.chats[0].user_turn_count = 0
    await flushPromises()
    await wrapper.get('.chat-code-badge').trigger('click')
    await flushPromises()
    const section = wrapper.getComponent(ChatRepoSection)
    expect(section.props('codeModeLocked')).toBe(false)
    expect(section.get('[role="switch"]').attributes('aria-disabled')).toBe('false')
    expect(section.text()).not.toContain('Code mode stays on for the rest of this chat.')

    store.chats[0].user_turn_count = 1
    await flushPromises()
    expect(section.props('codeModeLocked')).toBe(true)
    const sw = section.get('[role="switch"]')
    expect(sw.attributes('aria-disabled')).toBe('true')
    expect(sw.attributes('aria-checked')).toBe('true')
    expect(section.text()).toContain('Code mode stays on for the rest of this chat.')
    // The reason is announced with the switch, not only shown under it.
    const lockedHint = section.get('.repo-code-locked')
    expect(sw.attributes('aria-describedby')?.split(' ')).toContain(lockedHint.attributes('id'))
  })
})

describe('ChatPanel code badge', () => {
  const scrollProto = Element.prototype as unknown as { scrollTo?: unknown }
  const originalScrollTo = scrollProto.scrollTo
  const REPO = { path: '/r/app', branch: 'main', dirty: false, missing: false, instructions: null }

  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    localStorage.clear()
    scrollProto.scrollTo = function scrollTo() {}
  })

  afterEach(() => {
    scrollProto.scrollTo = originalScrollTo
    document.body.innerHTML = ''
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('shows a code badge beside the title only while code mode is on', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.projects[0].repo = REPO
    store.chats[0].code_mode = true
    await flushPromises()
    expect(wrapper.get('.chat-title-row .chat-code-badge').text()).toBe('code')
    expect(wrapper.find('.header-actions .repo-chip').exists()).toBe(false)

    store.chats[0].code_mode = false
    await flushPromises()
    expect(wrapper.find('.chat-code-badge').exists()).toBe(false)
    expect(wrapper.find('.header-actions .repo-chip').exists()).toBe(false)
  })

  it('shows no badge for an unlinked chat in code mode', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.chats[0].code_mode = true
    await flushPromises()
    expect(wrapper.find('.chat-code-badge').exists()).toBe(false)
  })

  it('opens Chat details at the Repository section from the badge', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.projects[0].repo = REPO
    store.chats[0].code_mode = true
    await flushPromises()
    await wrapper.get('.chat-code-badge').trigger('click')
    await flushPromises()
    const rail = document.querySelector('#chat-work-rail') ?? wrapper.element.querySelector('#chat-work-rail')
    expect(rail).not.toBeNull()
    expect(rail!.querySelector('.chat-rail-repo')).not.toBeNull()
  })
})

describe('ChatPanel naming placeholder', () => {
  const scrollProto = Element.prototype as unknown as { scrollTo?: unknown }
  const originalScrollTo = scrollProto.scrollTo
  const REPO = { path: '/r/app', branch: 'main', dirty: false, missing: false, instructions: null }

  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    localStorage.clear()
    scrollProto.scrollTo = function scrollTo() {}
  })

  afterEach(() => {
    scrollProto.scrollTo = originalScrollTo
    document.body.innerHTML = ''
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('shows the shared naming placeholder while the title is pending', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.projects[0].repo = REPO
    store.chats[0].code_mode = true
    store.chats[0].title = 'New Chat'
    store.chats[0].title_status = 'pending'
    await flushPromises()

    const shimmer = wrapper.get('.chat-title-row .chat-title-shimmer')
    expect(shimmer.attributes('role')).toBe('status')
    expect(shimmer.attributes('aria-label')).toBe('Naming conversation')
    expect(shimmer.attributes('title')).toBe('Naming conversation')
    // The placeholder stands in for the title, so the temporary one never shows.
    expect(wrapper.find('.chat-title-row .chat-title').exists()).toBe(false)
    expect(wrapper.get('.chat-title-row').text()).not.toContain('New Chat')
    // The close control and the code badge stay put while the conversation is
    // being named.
    expect(wrapper.find('.chat-title-row .close-btn').exists()).toBe(true)
    expect(wrapper.find('.chat-title-row .chat-code-badge').exists()).toBe(true)
  })

  it('replaces the placeholder when the title settles', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.chats[0].title = 'New Chat'
    store.chats[0].title_status = 'pending'
    await flushPromises()
    expect(wrapper.find('.chat-title-shimmer').exists()).toBe(true)

    store.chats[0].title = 'Deploy the API'
    store.chats[0].title_status = 'ready'
    await flushPromises()

    expect(wrapper.find('.chat-title-shimmer').exists()).toBe(false)
    expect(wrapper.get('.chat-title-row .chat-title').text()).toBe('Deploy the API')
  })

  it('keeps the close control, the title and the code badge in one header row', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    store.projects[0].repo = REPO
    store.projects[0].name = 'Launch notes'
    store.chats[0].code_mode = true
    await flushPromises()

    const row = wrapper.get('.chat-title-row')
    expect(row.find('.close-btn').exists()).toBe(true)
    expect(row.find('.chat-title').exists()).toBe(true)
    expect(row.find('.chat-code-badge').exists()).toBe(true)
    // Close, then title, then badge: the order the header documents.
    const order = Array.from(row.element.children).map(el => el.className)
    expect(order).toEqual(['btn-icon close-btn', 'pane-title chat-title', 'chat-code-badge'])
    // The project sits beside the row, never inside it, so a second line
    // under the title cannot drag the close control off the title's centre.
    // A named project is required here: General renders no crumb at all, and
    // the assertion would then pass wherever the crumb lived.
    expect(wrapper.find('.header-breadcrumb > .breadcrumb-scope').exists()).toBe(true)
    expect(row.find('.breadcrumb-scope').exists()).toBe(false)

    store.chats[0].code_mode = false
    await flushPromises()
    const plainRow = wrapper.get('.chat-title-row')
    expect(plainRow.find('.close-btn').exists()).toBe(true)
    expect(plainRow.find('.chat-title').exists()).toBe(true)
    expect(plainRow.find('.chat-code-badge').exists()).toBe(false)
  })

  it('still renames the title from the header', async () => {
    const { wrapper } = await mountPanel()
    const store = useProjectStore()
    const rename = vi.spyOn(store, 'renameChat').mockResolvedValue()
    await flushPromises()

    await wrapper.get('.chat-title-row .chat-title').trigger('dblclick')
    const input = wrapper.get('.chat-title-row .title-input')
    await input.setValue('Renamed chat')
    await input.trigger('keyup.enter')
    await flushPromises()

    expect(rename).toHaveBeenCalledWith('chat-1', 'Renamed chat')
    expect(wrapper.find('.chat-title-row .title-input').exists()).toBe(false)
  })

  it('pulls in the shared naming placeholder stylesheet', () => {
    // The header placeholder and the sidebar's are the same bar, so the header
    // has to load the shared file: without the link the placeholder renders as
    // an empty span, which is a hole in the header rather than a placeholder.
    const here = join(__dirname, '..')
    const source = readFileSync(join(here, 'ChatPanel.vue'), 'utf8')
    expect(source).toContain('<style scoped src="./titleShimmer.css">')
    const css = readFileSync(join(here, 'titleShimmer.css'), 'utf8')
    expect(css).toMatch(/\.chat-title-shimmer\s*\{[^}]*flex:\s*0 1 10rem/)
    // Reduced motion: a still bar, not a pulse.
    expect(css).toMatch(/prefers-reduced-motion: reduce\)\s*\{[\s\S]*?animation:\s*none/)
  })
})
