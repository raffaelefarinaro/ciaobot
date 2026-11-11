// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, mount, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'
import ChatRepoSection from '../ChatRepoSection.vue'
import ComposerUploadProgress from '../ComposerUploadProgress.vue'
import TaskOriginNote from '../TaskOriginNote.vue'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import type { ProjectRepo, Task } from '../../lib/types'

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
function chat(id: string): ChatInfo {
  return {
    chat_id: id, project_id: 'project-1', title: id, model: 'sonnet',
    provider: 'claude', mode: 'normal', session_id: '', created_at: '', archived: false,
  } as ChatInfo
}

const mounted: VueWrapper[] = []

function mountTwoPanes() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [project]
  store.chats = [chat('chat-1'), chat('chat-2')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [], 'chat-2': [] }
  store.bootstrapped = true
  vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })
  const stubs = {
    PaneHeader: PaneHeaderStub, SubagentPanel: ChildStub, RouterLink: ChildStub,
    ChatCommentPopover: ChatCommentPopoverStub, CommentComposePopover: ChildStub,
  }
  const a = shallowMount(ChatPanel, {
    attachTo: document.body,
    props: { chatId: 'chat-1' },
    global: { plugins: [pinia], stubs },
  })
  const b = shallowMount(ChatPanel, {
    attachTo: document.body,
    props: { chatId: 'chat-2', beside: true, inactive: true },
    global: { plugins: [pinia], stubs },
  })
  mounted.push(a, b)
  return { a, b, store }
}

describe('two ChatPanels on screen', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
  })

  afterEach(() => {
    for (const w of mounted.splice(0)) w.unmount()
    vi.restoreAllMocks()
  })

  it('each pane shows its own staged attachments', async () => {
    const { a, b, store } = mountTwoPanes()
    store.addPendingImageRefs('chat-2', ['b.png'])
    await flushPromises()

    expect(b.findAll('.image-preview')).toHaveLength(1)
    expect(a.findAll('.image-preview')).toHaveLength(0)
  })

  it('each pane shows its own staged files', async () => {
    const { a, b, store } = mountTwoPanes()
    store.addPendingFiles('chat-2', [{ ref: `drop_${'a'.repeat(32)}`, name: 'brief.pdf' }])
    await flushPromises()

    expect(b.findAll('.file-chip')).toHaveLength(1)
    expect(b.find('.file-chip').text()).toContain('brief.pdf')
    expect(a.findAll('.file-chip')).toHaveLength(0)
  })

  it('a staged file chip names the full file on hover and has a labelled remove control', async () => {
    const { b, store } = mountTwoPanes()
    const longName = `${'quarterly-planning-notes-'.repeat(4)}final.pdf`
    store.addPendingFiles('chat-2', [{ ref: `drop_${'b'.repeat(32)}`, name: longName }])
    await flushPromises()

    const chip = b.get('.file-chip')
    expect(chip.attributes('title')).toBe(longName)
    expect(chip.get('button').attributes('aria-label')).toBe(`Remove ${longName}`)
    await chip.get('button').trigger('click')
    expect(b.findAll('.file-chip')).toHaveLength(0)
  })

  it('a sent message lists its files as chips, not tokens or paths', async () => {
    const { a, store } = mountTwoPanes()
    store.messages['chat-1'] = [{
      role: 'user',
      content: 'Summarise\n\nAttached files:\n- brief.pdf: `/v/brief.pdf`',
      timestamp: '2026-11-04T10:00:00Z',
    }] as never
    await flushPromises()

    expect(a.find('.message-file-name').text()).toBe('brief.pdf')
    const bubble = a.find('.message.user').text()
    expect(bubble).toContain('Summarise')
    expect(bubble).not.toContain('Attached files:')
    expect(bubble).not.toContain('/v/brief.pdf')
  })

  it('Send stays disabled while an upload is in flight', async () => {
    const { a, store } = mountTwoPanes()
    let finish: (refs: string[]) => void = () => {}
    const upload = vi.spyOn(store, 'uploadImages').mockReturnValue(new Promise<string[]>((resolve) => { finish = resolve }))
    await flushPromises()

    const png = new File([new Uint8Array([1])], 'shot.png', { type: 'image/png' })
    const input = a.find('.image-btn input').element as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [png], configurable: true })
    await a.find('.image-btn input').trigger('change')
    await a.find('textarea.chat-input').setValue('hi')

    expect(upload).toHaveBeenCalled()
    expect(a.findComponent(ComposerUploadProgress).props('uploads')).toHaveLength(1)
    expect(a.find('.send-btn').attributes('disabled')).toBeDefined()
    expect(a.find('.send-btn').attributes('title')).toBe('Wait for the upload to finish')

    finish([])
    await flushPromises()
    expect(a.find('.send-btn').attributes('disabled')).toBeUndefined()
  })

  it('each pane shows its own history load', async () => {
    const { a, b, store } = mountTwoPanes()
    vi.spyOn(api, 'get').mockImplementation((path: string) => {
      if (path.includes('/messages')) return new Promise(() => {}) as never
      return Promise.resolve([]) as never
    })
    void store.loadMessages('chat-2')
    await flushPromises()

    expect(b.find('.history-skeleton-stack').exists()).toBe(true)
    expect(a.find('.history-skeleton-stack').exists()).toBe(false)
  })

  it('the second pane prefixes its element ids', async () => {
    const { a, b } = mountTwoPanes()
    await flushPromises()

    expect(b.find('[aria-controls="beside-chat-work-rail"]').exists()).toBe(true)
    expect(a.find('[aria-controls="chat-work-rail"]').exists()).toBe(true)
    expect(document.querySelectorAll('#chat-work-rail').length).toBeLessThanOrEqual(1)
  })

  it('an inactive pane does not take the keyboard when it mounts', async () => {
    const { a } = mountTwoPanes()
    await flushPromises()

    expect(document.activeElement).toBe(a.find('textarea.chat-input').element)
  })

  it('an inactive pane does not tell the service worker which chat is focused', async () => {
    const postMessage = vi.fn()
    Object.defineProperty(navigator, 'serviceWorker', {
      value: {
        controller: { postMessage }, ready: Promise.resolve(),
        addEventListener: vi.fn(), removeEventListener: vi.fn(),
      },
      configurable: true,
    })
    try {
      mountTwoPanes()
      await flushPromises()
      await mounted[1].setProps({ chatId: 'chat-2' })
      await flushPromises()

      const focused = postMessage.mock.calls.map(([m]) => (m as { chat_id: string }).chat_id)
      expect(focused).toContain('chat-1')
      expect(focused).not.toContain('chat-2')
    } finally {
      Reflect.deleteProperty(navigator, 'serviceWorker')
    }
  })

  it('a staged chat comment lands in its own chat', () => {
    const { store } = mountTwoPanes()

    store.addPendingChatComment('chat-2', { selection: 's', comment: 'c' } as never)

    expect(store.pendingChatCommentsFor('chat-2')).toHaveLength(1)
    expect(store.pendingChatCommentsFor('chat-1')).toHaveLength(0)
  })
})

describe('the rails of two panes', () => {
  afterEach(() => { document.body.innerHTML = '' })

  it('no element id repeats when both panes render their repo and task-origin sections open', async () => {
    setActivePinia(createPinia())
    vi.spyOn(api, 'get').mockResolvedValue({ workspace: 'personal', tasks: [] } as never)
    const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
    const signals = useTaskSignalsStore()
    signals.tasks = [{
      id: 'ship', title: 'Ship', status: 'in_progress', project_id: '', due: '', assignee: 'agent',
      chat_id: 'chat-1', attempt_id: 'att-1', linked_chats: [], created_at: '', updated_at: '',
      revision: 'a'.repeat(64), relative_path: 'Tasks/ship.md', attempt_state: 'needs_you',
      attempt_outcome: 'needs_input', attempt_summary: '', attempt_detail: '', live_attempt_id: 'att-1',
      completed_at: null, has_resolution: false,
    } as Task]
    signals.loadedWorkspace = 'personal'
    const delegated = { chat_id: 'chat-1', helper: { kind: 'task_delegation', task_id: 'ship', task_revision: 'a'.repeat(64), attempt_id: 'att-1' } }

    // Both panes live in one app, as they do in ChatLayout: useId is per app, so
    // two separate mounts would repeat its generated ids and prove nothing.
    const bothPanes = defineComponent({
      setup: () => () => h('div', [0, 1].flatMap(() => [
        h(ChatRepoSection, { repo, location: null, chatId: 'chat-1' }),
        h(TaskOriginNote, { chat: delegated as never }),
      ])),
    })
    const wrapper = mount(bothPanes, { attachTo: document.body, global: { stubs: { RouterLink: true } } })
    try {
      for (const openNote of wrapper.findAll('button').filter(b => b.text() === 'Mark done…')) {
        await openNote.trigger('click')
      }
      await flushPromises()
      expect(document.querySelectorAll('textarea.task-origin-note')).toHaveLength(2)

      const ids = Array.from(document.querySelectorAll('[id]')).map(el => el.id)
      const repeated = ids.filter((id, i) => ids.indexOf(id) !== i)
      expect(repeated).toEqual([])
    } finally {
      wrapper.unmount()
    }
  })
})
