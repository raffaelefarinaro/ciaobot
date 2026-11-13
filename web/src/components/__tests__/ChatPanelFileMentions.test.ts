// @vitest-environment jsdom
//
// Checked file mentions in the final reply bubble: an untouched filename
// links only after the engine confirms it, the click hands the canonical path
// and line to the viewer, and a late answer from another context never paints.

import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
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
  void useTaskSignalsStore()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })

  const wrapper = shallowMount(ChatPanel, {
    props: { chatId: 'chat-1' },
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
  return { wrapper, store }
}

/** A user turn followed by one completed assistant reply. */
function turn(reply: string) {
  return [
    { role: 'user' as const, content: 'check the files', timestamp: '2026-11-13T09:00:00Z', turn_index: 0 },
    { role: 'assistant' as const, content: reply, timestamp: '2026-11-13T09:00:05Z' },
  ]
}

/** Answer the resolve route from `map`; any other POST answers `{}`. */
function answerResolve(map: Record<string, string | null>) {
  const post = vi.spyOn(api, 'post')
  post.mockImplementation((async (path: string, body?: unknown) => {
    if (!path.endsWith('/file-paths/resolve')) return {}
    const paths = (body as { paths: string[] }).paths
    return { results: paths.map(input => ({ input, path: map[input] ?? null })) }
  }) as never)
  return post
}

function resolveCalls(post: MockInstance<typeof api.post>): unknown[][] {
  return post.mock.calls.filter(c => String(c[0]).endsWith('/file-paths/resolve'))
}

const REPO = { path: '/repo', branch: 'main', dirty: false, missing: false }

describe('ChatPanel checked file mentions', () => {
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

  it('rerenders cached untouched filename after confirmation and delegates line open', async () => {
    const post = answerResolve({ 'docs/guide.md:17': '/r/docs/guide.md' })
    const { wrapper, store } = await mountPanel()
    const openLink = vi.spyOn(useFileViewerStore(), 'openLink').mockResolvedValue(true)
    store.messages['chat-1'] = turn('See docs/guide.md:17.')
    await flushPromises()

    expect(wrapper.find('.message.assistant .message-content a.file-link').exists()).toBe(true)
    expect(resolveCalls(post)).toHaveLength(1)
    const link = wrapper.get('.message.assistant .message-content a.file-link[data-file-path="/r/docs/guide.md"]')
    expect(link.attributes('data-line')).toBe('17')
    expect(link.text()).toBe('docs/guide.md:17')

    await link.trigger('click')
    expect(openLink).toHaveBeenCalledWith('/r/docs/guide.md', 17, 'chat-1')
  })

  it('preserves touched relative and absolute line links', async () => {
    answerResolve({})
    const { wrapper, store } = await mountPanel()
    const openLink = vi.spyOn(useFileViewerStore(), 'openLink').mockResolvedValue(true)
    store.messages['chat-1'] = [
      ...turn('Workspace/a.md:17 and /abs/Workspace/a.md:17 and [x](Workspace/a.md)'),
      {
        role: 'system' as const,
        tool_name: '_filecard',
        content: 'Workspace/a.md',
        file_path: 'Workspace/a.md',
        action: 'edited',
        timestamp: '2026-11-13T08:59:59Z',
      },
    ]
    await flushPromises()

    const lined = wrapper.findAll('.message.assistant .message-content a.file-link')
      .filter(a => a.attributes('data-line') === '17')
    expect(lined).toHaveLength(2)
    await lined[0].trigger('click')
    expect(openLink).toHaveBeenCalledWith(expect.any(String), 17, 'chat-1')
    expect(wrapper.findAll('.message.assistant .message-content a').some(a => a.text() === 'x')).toBe(true)
  })

  it('clears confirmed links when a new generation returns null', async () => {
    const post = answerResolve({ 'docs/guide.md': '/r/docs/guide.md' })
    const { wrapper, store } = await mountPanel()
    const openLink = vi.spyOn(useFileViewerStore(), 'openLink').mockResolvedValue(true)
    const toast = vi.spyOn(store, 'pushErrorToast')
    store.messages['chat-1'] = turn('Read docs/guide.md now.')
    await flushPromises()
    expect(wrapper.find('.message.assistant .message-content a.file-link').exists()).toBe(true)

    post.mockImplementation((async (path: string, body?: unknown) => {
      if (!path.endsWith('/file-paths/resolve')) return {}
      return { results: (body as { paths: string[] }).paths.map(input => ({ input, path: null })) }
    }) as never)
    store.streaming['chat-1'] = true
    await flushPromises()
    store.streaming['chat-1'] = false
    await flushPromises()

    expect(wrapper.find('.message.assistant .message-content a.file-link').exists()).toBe(false)
    expect(wrapper.get('.message.assistant .message-content').text()).toContain('docs/guide.md')
    expect(openLink).not.toHaveBeenCalled()
    expect(toast).not.toHaveBeenCalled()
  })

  it('isolates identical reply text across chat and repository contexts', async () => {
    const post = vi.spyOn(api, 'post')
    let releaseOld!: (value: unknown) => void
    post.mockImplementationOnce((() => new Promise(resolve => { releaseOld = resolve })) as never)
    post.mockImplementation((async (path: string, body?: unknown) => {
      if (!path.endsWith('/file-paths/resolve')) return {}
      return { results: (body as { paths: string[] }).paths.map(input => ({ input, path: '/repo/docs/guide.md' })) }
    }) as never)

    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = turn('Open docs/guide.md please.')
    await flushPromises()
    expect(resolveCalls(post)).toHaveLength(1)

    // The project gains a repo: a new context, so the pending plain-context answer is stale.
    store.projects[0].repo = { ...REPO }
    await flushPromises()
    expect(resolveCalls(post)).toHaveLength(1)

    releaseOld({ results: [{ input: 'docs/guide.md', path: '/plain/docs/guide.md' }] })
    await flushPromises()

    expect(resolveCalls(post)).toHaveLength(2)
    expect(wrapper.find('a.file-link[data-file-path="/plain/docs/guide.md"]').exists()).toBe(false)
    expect(wrapper.get('a.file-link[data-file-path="/repo/docs/guide.md"]').text()).toBe('docs/guide.md')

    // The sidebar workspace is not part of the context: changing it sends nothing.
    store.activeWorkspace = 'work'
    await flushPromises()
    expect(resolveCalls(post)).toHaveLength(2)
  })

  it('does not discover user or streaming turn text', async () => {
    const post = answerResolve({ 'docs/after.md': '/r/docs/after.md' })
    const { store } = await mountPanel()
    store.streaming['chat-1'] = true
    store.streamingText['chat-1'] = 'partial docs/stream.md'
    store.messages['chat-1'] = [
      { role: 'user', content: 'open docs/user.md', timestamp: '2026-11-13T09:00:00Z', turn_index: 0 },
    ]
    await flushPromises()
    expect(resolveCalls(post)).toHaveLength(0)

    store.streaming['chat-1'] = false
    store.streamingText['chat-1'] = ''
    store.messages['chat-1'] = [
      ...store.messages['chat-1'],
      { role: 'assistant', content: 'Done: docs/after.md', timestamp: '2026-11-13T09:00:08Z' },
    ]
    await flushPromises()
    expect(resolveCalls(post)).toHaveLength(1)
    expect((resolveCalls(post)[0][1] as { paths: string[] }).paths).toEqual(['docs/after.md'])
  })

  it('keeps image delegation and existing docking ownership', async () => {
    answerResolve({ 'shot.png': '/r/shot.png' })
    const { wrapper, store } = await mountPanel()
    const viewer = useFileViewerStore()
    const openLink = vi.spyOn(viewer, 'openLink').mockResolvedValue(true)
    const open = vi.spyOn(viewer, 'open').mockResolvedValue(true)
    const pin = vi.spyOn(store, 'pinFile').mockResolvedValue(undefined as never)
    store.messages['chat-1'] = turn('Screenshot: shot.png')
    await flushPromises()

    // Discovery alone opens, pins and docks nothing.
    expect(openLink).not.toHaveBeenCalled()
    expect(open).not.toHaveBeenCalled()
    expect(pin).not.toHaveBeenCalled()

    await wrapper.get('.message.assistant .message-content a.file-link[data-file-path="/r/shot.png"]').trigger('click')
    expect(openLink).toHaveBeenCalledWith('/r/shot.png', null, 'chat-1')
  })
})
