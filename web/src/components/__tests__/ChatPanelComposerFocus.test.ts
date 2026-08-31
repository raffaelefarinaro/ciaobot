// @vitest-environment jsdom
//
// Focus of the chat composer when a chat is opened. Mounts the real ChatLayout
// around the real ChatPanel, like the question-shortcut suite next door: the
// rule is a negotiation between the two (the layout only offers 1-9 to a
// question card when focus is NOT in a text field), so a ChatPanel-only test
// would not exercise the part that can regress.

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

const apiGet = vi.hoisted(() => vi.fn(() => Promise.reject(new Error('no server'))))
const apiPost = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiPatch = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiDel = vi.hoisted(() => vi.fn(() => Promise.resolve({})))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel },
}))

// ChatPanel and the store import the app router singleton directly; ChatLayout
// uses the injected one from the plugin below.
vi.mock('../../router', () => ({
  router: { push: vi.fn(), currentRoute: { value: { params: {} } } },
}))

const NoopStub = vi.hoisted(() => ({ name: 'NoopStub', render: () => null }))
// ChatPanel calls popover methods from scroll and teardown paths, so the stub
// needs them as instance methods.
const CommentPopoverStub = vi.hoisted(() => ({
  name: 'CommentPopoverStub',
  render: () => null,
  methods: {
    close: () => {},
    clearPendingClose: () => {},
    onTargetOver: () => {},
    onTargetOut: () => {},
    pinFromEvent: () => null,
    show: () => {},
  },
}))
vi.mock('../PaneHeader.vue', () => ({ default: NoopStub }))
vi.mock('../VoiceRecorder.vue', () => ({ default: NoopStub }))
vi.mock('../SubagentPanel.vue', () => ({ default: NoopStub }))
vi.mock('../ModelSelector.vue', () => ({ default: NoopStub }))
vi.mock('../ChatCommentPopover.vue', () => ({ default: CommentPopoverStub }))
vi.mock('../CommentComposePopover.vue', () => ({ default: NoopStub }))

const EmptyStub = defineComponent({ name: 'EmptyStub', setup: () => () => h('div') })

const CHAT_ID = 'chat-focus'

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

type QuestionSeed = {
  multiSelect?: boolean
  allowOther?: boolean
  options?: Array<{ label: string; description?: string }>
}

function makeQuestion(seed: QuestionSeed = {}) {
  return {
    id: 'q0',
    question: 'Which direction?',
    header: 'direction',
    multiSelect: seed.multiSelect ?? false,
    allowOther: seed.allowOther ?? true,
    isSecret: false,
    requestId: '',
    options: seed.options ?? [
      { label: 'Refactor first', description: 'clean up before adding' },
      { label: 'Ship the feature', description: '' },
      { label: 'Write tests', description: '' },
    ],
  }
}

async function mountLayout(
  seed: { questions?: ReturnType<typeof makeQuestion>[]; seedPermission?: boolean } = {},
) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId', component: EmptyStub }],
  })
  await router.push('/')
  await router.isReady()

  const store = useProjectStore()
  store.projects = [{
    project_id: 'project-1',
    name: 'General',
    workspace: 'personal',
  }] as unknown as typeof store.projects
  store.chats = [{
    chat_id: CHAT_ID,
    project_id: 'project-1',
    title: 'question test',
    archived: false,
    provider: 'claude',
    mode: 'bypass',
  }] as unknown as typeof store.chats
  store.activeChatId = CHAT_ID
  store.workspaces = [
    { name: 'personal', vault_root: '', default_provider: 'claude', gws_profile: '' },
    { name: 'work', vault_root: '', default_provider: 'claude', gws_profile: '' },
  ]
  store.activeWorkspace = 'personal'
  store.bootstrapped = true
  if (seed.questions) store.activeQuestions = { [CHAT_ID]: seed.questions }
  if (seed.seedPermission) {
    store.pendingPermissions = {
      [CHAT_ID]: [{
        request_id: 'req-1',
        tool_name: 'Bash',
        tool_input: 'ls',
        message: 'Run ls?',
        received_at: Date.now(),
      }],
    }
  }
  vi.spyOn(store, 'fetchAll').mockResolvedValue()

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  const { default: ChatLayout } = await import('../ChatLayout.vue')
  const wrapper = mount(ChatLayout, {
    attachTo: document.body,
    global: {
      plugins: [router],
      stubs: {
        ProjectSidebar: EmptyStub,
        ProjectView: EmptyStub,
        SchedulePanel: EmptyStub,
        SettingsView: EmptyStub,
        FileViewerModal: EmptyStub,
        PinnedFilePanel: EmptyStub,
        HomeRecentChats: EmptyStub,
        Teleport: true,
      },
    },
  })
  await flushPromises()
  await nextTick()
  return { wrapper, store }
}

function pressKey(key: string, target: EventTarget = window) {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
  target.dispatchEvent(event)
  return event
}


beforeEach(() => {
  setActivePinia(createPinia())
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  localStorage.clear()
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('composer focus on opening a chat', () => {
  test('focuses the composer so typing just works', async () => {
    const { wrapper } = await mountLayout()

    const composer = wrapper.find('textarea.chat-input')
    expect(composer.exists()).toBe(true)
    expect(document.activeElement).toBe(composer.element)
    wrapper.unmount()
  })

  test('leaves focus alone while a question card is waiting', async () => {
    // The card's options are numbered on screen and 1-9 picks one, but the
    // layout only offers the key to the card when focus is not in a text
    // field. Focusing the composer here would turn "press 2" into typing "2",
    // in exactly the chats that are blocked waiting for that answer.
    const { wrapper } = await mountLayout({ questions: [makeQuestion()] })

    const composer = wrapper.find('textarea.chat-input')
    expect(document.activeElement).not.toBe(composer.element)

    const event = pressKey('2')
    expect(event.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  test('leaves focus alone while a permission card is waiting', async () => {
    const { wrapper, store } = await mountLayout({ seedPermission: true })

    expect(document.activeElement).not.toBe(wrapper.find('textarea.chat-input').element)
    expect(store.pendingPermissions[CHAT_ID]).toHaveLength(1)
    wrapper.unmount()
  })

  test('stays out of the way on a phone', async () => {
    // Focus is what raises the on-screen keyboard, so auto-focusing would
    // cover half the transcript on every chat the user taps into.
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 420 })
    const { wrapper } = await mountLayout()

    expect(document.activeElement).not.toBe(wrapper.find('textarea.chat-input').element)
    wrapper.unmount()
  })
})
