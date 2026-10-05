// @vitest-environment jsdom
//
// The Outputs section of an assistant turn: one collapsed disclosure listing
// the files the turn produced. Guards the two reported faults — the same file
// listed twice because two tool calls spelled its path differently, and a
// heavy always-open pill grid sitting between the answer and the next turn.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useFileViewerStore } from '../../stores/fileViewer'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { useTaskSignalsStore } from '../../stores/taskSignals'
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

const mounted: VueWrapper[] = []

async function mountPanel(): Promise<{
  wrapper: VueWrapper
  store: ReturnType<typeof useProjectStore>
  pinia: ReturnType<typeof createPinia>
}> {
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
      },
    },
  })
  await flushPromises()
  mounted.push(wrapper)
  return { wrapper, store, pinia }
}

/** One turn that wrote a resume twice under two path spellings and edited a
 *  second file — the shape from the reported screenshot. */
function turnWithOutputs() {
  return [
    { role: 'user' as const, content: 'update my resume', timestamp: '2026-09-20T09:00:00Z', turn_index: 0 },
    {
      role: 'system' as const,
      tool_name: '_filecard',
      content: '/Users/me/vault/Workspace/Resume 2026-09.md',
      file_path: '/Users/me/vault/Workspace/Resume 2026-09.md',
      action: 'created',
      timestamp: '2026-09-20T09:00:05Z',
    },
    {
      role: 'system' as const,
      tool_name: '_filecard',
      content: 'Workspace/Resume 2026-09.md',
      file_path: 'Workspace/Resume 2026-09.md',
      action: 'edited',
      timestamp: '2026-09-20T09:00:07Z',
    },
    {
      role: 'system' as const,
      tool_name: '_filecard',
      content: 'Workspace/cover-letter.md',
      file_path: 'Workspace/cover-letter.md',
      action: 'edited',
      timestamp: '2026-09-20T09:00:09Z',
    },
    {
      role: 'assistant' as const,
      content: 'Done. The resume and the cover letter are updated.',
      timestamp: '2026-09-20T09:00:12Z',
    },
  ]
}

describe('ChatPanel Outputs section', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
  })

  afterEach(() => {
    for (const w of mounted.splice(0)) w.unmount()
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('lists each file once and starts collapsed', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnWithOutputs()
    await flushPromises()

    const summary = wrapper.find('.answer-outputs .outputs-summary')
    expect(summary.exists()).toBe(true)
    // Two files, not three: the absolute and workspace-relative spellings of
    // the resume are one file.
    expect(summary.text()).toContain('Outputs')
    expect(summary.text()).toContain('2 files')
    expect(summary.attributes('aria-expanded')).toBe('false')
    // Collapsed by default: no list rendered until the reader asks for it.
    expect(wrapper.find('.outputs-list').exists()).toBe(false)
  })

  it('expands into one bullet row per file with an action tag', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnWithOutputs()
    await flushPromises()

    const summary = wrapper.find('.answer-outputs .outputs-summary')
    await summary.trigger('click')

    expect(summary.attributes('aria-expanded')).toBe('true')
    const list = wrapper.find('.outputs-list')
    expect(list.exists()).toBe(true)
    // The summary controls the list it reveals.
    expect(summary.attributes('aria-controls')).toBe(list.attributes('id'))

    const rows = list.findAll('.outputs-row')
    expect(rows.length).toBe(2)

    // Filename links, kept as the first spelling seen for that file.
    const links = rows.map(r => r.find('.outputs-link'))
    expect(links[0].text()).toContain('Resume 2026-09.md')
    expect(links[0].attributes('title')).toBe('/Users/me/vault/Workspace/Resume 2026-09.md')
    expect(links[1].text()).toContain('cover-letter.md')

    // Action tags map from the backend's values: created -> new, edited.
    expect(rows[0].find('.outputs-tag').text()).toBe('new')
    expect(rows[1].find('.outputs-tag').text()).toBe('edited')
  })

  it('collapses again on a second activation', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turnWithOutputs()
    await flushPromises()

    const summary = wrapper.find('.answer-outputs .outputs-summary')
    await summary.trigger('click')
    expect(wrapper.find('.outputs-list').exists()).toBe(true)
    await summary.trigger('click')
    expect(summary.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.outputs-list').exists()).toBe(false)
  })

  it('links an archived source chat to its memory pass', async () => {
    const { wrapper, store } = await mountPanel()
    // The pass runs in a project the sidebar hides, so the archived chat is the
    // durable way back to it.
    const chat = store.chats[0]
    chat.archived = true
    chat.postprocess = {
      steps: { memory_pass: { status: 'ok', extra: { chat_id: 'pass-1' } } },
    }
    await flushPromises()

    const button = wrapper.find('.archive-memory-pass-btn')
    expect(button.exists()).toBe(true)
    expect(button.text()).toBe('Open memory pass')

    const switchChat = vi.spyOn(store, 'switchChat').mockResolvedValue(undefined)
    await button.trigger('click')
    expect(switchChat).toHaveBeenCalledWith('pass-1')
  })

  it('offers no memory pass link on an archived chat that spawned none', async () => {
    const { wrapper, store } = await mountPanel()
    const chat = store.chats[0]
    chat.archived = true
    chat.postprocess = { steps: {} }
    await flushPromises()

    expect(wrapper.find('.archive-memory-pass-btn').exists()).toBe(false)

    // A record that is not even a chat id must not become a navigation target.
    chat.postprocess = { steps: { memory_pass: { status: 'queued', extra: { chat_id: 7 } } } } as never
    await flushPromises()
    expect(wrapper.find('.archive-memory-pass-btn').exists()).toBe(false)
  })

  it('renders the archived notice and no composer for an archived chat', async () => {
    // #619: the panel already had an archived branch, but nothing could ever
    // reach it — a deep link to an archived chat resolved to no active chat, so
    // ChatLayout mounted no panel at all and this footer was unreachable. Now
    // that the store can select one, what renders here is the whole contract:
    // read-only, with a way forward and no way to type.
    const { wrapper, store } = await mountPanel()
    store.chats[0].archived = true
    store.chats[0].archive_path = 'archive/chat-1.jsonl'
    await flushPromises()

    const notice = wrapper.find('.archived-notice')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('This chat is archived.')
    expect(notice.find('.continue-chat-btn').text()).toBe('Continue in new chat')
    expect(wrapper.find('textarea.chat-input').exists()).toBe(false)
  })

  it('continues an archived task chat as a new attempt, not a plain chat', async () => {
    // A plain continuation carries no delegation: its report is refused and the
    // card stays stuck on the archived chat's interrupted attempt.
    const { wrapper, store } = await mountPanel()
    const attemptId = 'b'.repeat(32)
    const chat = store.chats[0]
    chat.archived = true
    chat.helper = { kind: 'task_delegation', task_id: 't1', task_revision: 'r'.repeat(64), attempt_id: attemptId }
    const signals = useTaskSignalsStore()
    signals.loadedWorkspace = 'personal'
    signals.tasks = [{
      id: 't1', title: 'Decide on proposals', status: 'in_progress', chat_id: chat.chat_id,
      attempt_id: attemptId, attempt_state: 'interrupted', live_attempt_id: '',
    } as never]
    const post = vi.spyOn(api, 'post').mockResolvedValue({
      workspace: 'personal', chat_id: 'chat-next', attempt: { attempt_id: 'c'.repeat(32), chat_id: 'chat-next' },
    })
    const switchChat = vi.spyOn(store, 'switchChat').mockResolvedValue()
    const continuePlain = vi.spyOn(store, 'continueArchivedChat')
    await flushPromises()

    const button = wrapper.get('.continue-chat-btn')
    expect(button.text()).toBe('Continue the task in a new chat')
    await button.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(`/api/tasks/t1/attempt/${attemptId}/retry`, { workspace: 'personal' })
    expect(switchChat).toHaveBeenCalledWith('chat-next')
    expect(continuePlain).not.toHaveBeenCalled()
  })

  it('deduplicates repeated action/path pairs in the Work inspector', async () => {
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      ...turnWithOutputs(),
      {
        role: 'system' as const,
        tool_name: '_filecard',
        content: 'Workspace/cover-letter.md',
        file_path: 'Workspace/cover-letter.md',
        action: 'edited',
        timestamp: '2026-09-20T09:01:00Z',
      },
    ]
    await flushPromises()

    await wrapper.get('.work-inspector-trigger').trigger('click')
    await wrapper.get('#work-tab-output').trigger('click')
    const rows = wrapper.findAll('.chat-work-output')
    expect(rows).toHaveLength(2)
    expect(new Set(rows.map(row => row.get('.chat-work-output-action').text())).size).toBe(2)
  })

  it('opens the file viewer from a row link, as the old pill did', async () => {
    const { wrapper, store, pinia } = await mountPanel()
    const viewer = useFileViewerStore(pinia)
    const open = vi.spyOn(viewer, 'open').mockResolvedValue(undefined as never)
    store.messages['chat-1'] = turnWithOutputs()
    await flushPromises()

    await wrapper.find('.answer-outputs .outputs-summary').trigger('click')
    const link = wrapper.find('.outputs-link')
    // Native button: keyboard activation and focus come for free.
    expect(link.element.tagName).toBe('BUTTON')
    await link.trigger('click')
    await flushPromises()
    expect(open).toHaveBeenCalledWith('/Users/me/vault/Workspace/Resume 2026-09.md', null, 'chat-1')
  })
})
