// @vitest-environment jsdom
//
// Digit shortcuts for the AskUserQuestion picker. These mount the real
// ChatLayout around the real ChatPanel on purpose: the shortcut is a
// negotiation between the two (the layout owns the only window keydown
// listener and offers 1-9 to the card before spending them on workspace
// switching), so a ChatPanel-only test would prove nothing about the part
// that can actually regress.

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

const CHAT_ID = 'chat-q'

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

async function mountLayout(seed: { questions?: ReturnType<typeof makeQuestion>[] } = {}) {
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
    { name: 'personal', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '' },
    { name: 'work', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '' },
  ]
  store.activeWorkspace = 'personal'
  store.bootstrapped = true
  if (seed.questions) store.activeQuestions = { [CHAT_ID]: seed.questions }
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

function optionButtons(wrapper: Awaited<ReturnType<typeof mountLayout>>['wrapper']) {
  return wrapper.find('.question-card').findAll('button.question-option')
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

describe('AskUserQuestion keyboard shortcuts', () => {
  test('renders a keyboard badge 1..n on the first question options', async () => {
    const { wrapper } = await mountLayout({ questions: [makeQuestion()] })

    const badges = wrapper.find('.question-card').findAll('.question-option-key')
    expect(badges.map(b => b.text())).toEqual(['1', '2', '3'])
    // The hint is not part of the label, and screen readers get the real thing.
    expect(optionButtons(wrapper)[0].find('.question-option-label').text()).toBe('Refactor first')
    expect(optionButtons(wrapper)[0].attributes('aria-keyshortcuts')).toBe('1')

    wrapper.unmount()
  })

  test('badges stop at the first question, which is the only one bound', async () => {
    const second = { ...makeQuestion(), id: 'q1', question: 'Then what?' }
    const { wrapper } = await mountLayout({ questions: [makeQuestion(), second] })

    const blocks = wrapper.find('.question-card').findAll('.question-block')
    expect(blocks[0].findAll('.question-option-key')).toHaveLength(3)
    expect(blocks[1].findAll('.question-option-key')).toHaveLength(0)

    wrapper.unmount()
  })

  test('Digit1 selects the first option and beats the workspace shortcut', async () => {
    const { wrapper, store } = await mountLayout({ questions: [makeQuestion()] })
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')

    const event = pressKey('1')
    await nextTick()

    expect(event.defaultPrevented).toBe(true)
    expect(optionButtons(wrapper)[0].classes()).toContain('selected')
    expect(switchWorkspace).not.toHaveBeenCalled()
    expect(store.activeWorkspace).toBe('personal')

    wrapper.unmount()
  })

  test('a digit past the last option is left alone', async () => {
    const { wrapper, store } = await mountLayout({ questions: [makeQuestion()] })
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')

    // Option 4 does not exist, but workspace 2 does: the card only claims the
    // digits it can actually use.
    pressKey('4')
    await nextTick()
    expect(optionButtons(wrapper).filter(b => b.classes().includes('selected'))).toHaveLength(0)
    expect(switchWorkspace).not.toHaveBeenCalled()

    pressKey('2')
    await nextTick()
    expect(optionButtons(wrapper)[1].classes()).toContain('selected')

    wrapper.unmount()
  })

  test('single-select replaces the previous pick', async () => {
    const { wrapper } = await mountLayout({ questions: [makeQuestion()] })

    pressKey('1')
    await nextTick()
    pressKey('3')
    await nextTick()

    const selected = optionButtons(wrapper).filter(b => b.classes().includes('selected'))
    expect(selected).toHaveLength(1)
    expect(selected[0].text()).toContain('Write tests')

    wrapper.unmount()
  })

  test('multi-select toggles the same option on and off', async () => {
    const { wrapper } = await mountLayout({ questions: [makeQuestion({ multiSelect: true })] })

    pressKey('2')
    await nextTick()
    expect(optionButtons(wrapper)[1].classes()).toContain('selected')

    pressKey('1')
    await nextTick()
    expect(optionButtons(wrapper).filter(b => b.classes().includes('selected'))).toHaveLength(2)

    pressKey('2')
    await nextTick()
    const selected = optionButtons(wrapper).filter(b => b.classes().includes('selected'))
    expect(selected).toHaveLength(1)
    expect(selected[0].text()).toContain('Refactor first')

    wrapper.unmount()
  })

  test('typing a digit in the chat composer does not select an option', async () => {
    const { wrapper, store } = await mountLayout({ questions: [makeQuestion()] })
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')

    const composer = wrapper.find('textarea.chat-input')
    expect(composer.exists()).toBe(true)
    const event = pressKey('1', composer.element)
    await nextTick()

    expect(event.defaultPrevented).toBe(false)
    expect(optionButtons(wrapper).filter(b => b.classes().includes('selected'))).toHaveLength(0)
    expect(switchWorkspace).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  test('typing a digit in the "Other (free text)" input does not select an option', async () => {
    const { wrapper } = await mountLayout({ questions: [makeQuestion()] })

    const other = wrapper.find('input.question-other')
    expect(other.exists()).toBe(true)
    const event = pressKey('1', other.element)
    await nextTick()

    expect(event.defaultPrevented).toBe(false)
    expect(optionButtons(wrapper).filter(b => b.classes().includes('selected'))).toHaveLength(0)

    wrapper.unmount()
  })

  test('Enter submits a single-select answer once an option is picked', async () => {
    const { wrapper, store } = await mountLayout({ questions: [makeQuestion()] })
    const sendMessage = vi.spyOn(store, 'sendMessage').mockImplementation(() => true)

    // Nothing picked yet: Enter is not the card's key.
    expect(pressKey('Enter').defaultPrevented).toBe(false)
    expect(sendMessage).not.toHaveBeenCalled()

    pressKey('2')
    await nextTick()
    const enter = pressKey('Enter')
    await nextTick()

    expect(enter.defaultPrevented).toBe(true)
    expect(sendMessage).toHaveBeenCalledTimes(1)
    expect(sendMessage.mock.calls[0][1]).toContain('Ship the feature')

    wrapper.unmount()
  })

  test('Enter stays with a focused button and never auto-sends multi-select', async () => {
    const { wrapper, store } = await mountLayout({ questions: [makeQuestion({ multiSelect: true })] })
    const sendMessage = vi.spyOn(store, 'sendMessage').mockImplementation(() => true)

    pressKey('1')
    await nextTick()
    expect(pressKey('Enter').defaultPrevented).toBe(false)
    expect(sendMessage).not.toHaveBeenCalled()

    // ...and on a single-select card, a focused control keeps its native Enter.
    wrapper.unmount()
    setActivePinia(createPinia())
    const single = await mountLayout({ questions: [makeQuestion()] })
    const singleSend = vi.spyOn(single.store, 'sendMessage').mockImplementation(() => true)
    pressKey('1')
    await nextTick()
    const onButton = pressKey('Enter', optionButtons(single.wrapper)[0].element)
    await nextTick()
    expect(onButton.defaultPrevented).toBe(false)
    expect(singleSend).not.toHaveBeenCalled()

    single.wrapper.unmount()
  })

  test('workspace digit switching still works with no question card open', async () => {
    const { wrapper, store } = await mountLayout()
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')

    expect(wrapper.find('.question-card').exists()).toBe(false)
    const event = pressKey('2')
    await flushPromises()

    expect(event.defaultPrevented).toBe(true)
    expect(switchWorkspace).toHaveBeenCalledWith('work', { transition: true })

    wrapper.unmount()
  })

  test('workspace digit switching returns once the card is dismissed', async () => {
    const { wrapper, store } = await mountLayout({ questions: [makeQuestion()] })
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')

    pressKey('2')
    await nextTick()
    expect(switchWorkspace).not.toHaveBeenCalled()

    await wrapper.find('.question-card-dismiss').trigger('click')
    await nextTick()
    expect(wrapper.find('.question-card').exists()).toBe(false)

    pressKey('2')
    await flushPromises()
    expect(switchWorkspace).toHaveBeenCalledWith('work', { transition: true })

    wrapper.unmount()
  })
})
