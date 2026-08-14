// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import { useProjectStore } from '../../stores/projects'
import type { ChatInfo } from '../../lib/types'

// The store's onMounted fetches (models, commands, loops, schedules) all
// swallow errors, so a rejecting api keeps the mount path quiet while we
// drive the capability card purely from store state.
const apiGet = vi.hoisted(() => vi.fn(() => Promise.reject(new Error('no server'))))
const apiPost = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiPatch = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiDel = vi.hoisted(() => vi.fn(() => Promise.resolve({})))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel },
}))

vi.mock('../../router', () => ({
  router: {
    push: vi.fn(),
    currentRoute: { value: { params: {} } },
  },
}))

// Stub heavy/leaf children that aren't relevant to the capability card. We
// mock the module path because Vue SFCs import siblings directly via ESM,
// which bypasses `config.global.stubs`. `vi.hoisted` keeps the stub above the
// hoisted `vi.mock` registrations (and the static ChatPanel import below).
const NoopStub = vi.hoisted(() => ({ name: 'NoopStub', render: () => null }))
// ChatPanel calls methods on the comment popover from scroll handlers
// (closeChatCommentPopover etc.), so the stub needs them as instance methods.
const CommentPopoverStub = vi.hoisted(() => ({
  name: 'CommentPopoverStub',
  render: () => null,
  methods: {
    close: () => {},
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

import ChatPanel from '../ChatPanel.vue'
import { mount } from '@vue/test-utils'

const CHAT_ID = 'chat-cap'
const REQ_ID = 'req-1'

function makeChat(overrides: Partial<ChatInfo> = {}): ChatInfo {
  return {
    chat_id: CHAT_ID,
    project_id: 'proj-1',
    title: 'cap test',
    model: 'deepseek-v4-flash:cloud',
    provider: 'claude',
    model_bucket: '',
    mode: 'bypass',
    session_id: 'sess-1',
    created_at: '2026-08-11T00:00:00Z',
    archived: false,
    ...overrides,
  }
}

function makeQuestion() {
  return {
    request_id: REQ_ID,
    missing: 'image_input',
    current_model: 'deepseek-v4-flash:cloud',
    candidates: [
      { id: 'deepseek-v4-flash:cloud', label: 'deepseek-v4-flash:cloud', disabled: true },
      { id: 'minimax-m3:cloud', label: 'minimax-m3:cloud', supports_vision: true },
    ],
    timeout_s: 30,
    opened_at: Date.now(),
  }
}

async function mountPanel() {
  const wrapper = mount(ChatPanel, {
    global: {
      stubs: { Teleport: true },
    },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('image-capability question card', () => {
  test('renders the card with header, countdown, candidates and actions', async () => {
    const store = useProjectStore()
    store.chats = [makeChat()]
    store.activeChatId = CHAT_ID
    store.activeCapabilityQuestions = { [CHAT_ID]: [makeQuestion()] }

    const wrapper = await mountPanel()

    const card = wrapper.find('.capability-card')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain("This model can't see images")
    // Countdown renders as "<remaining>s" (30s window, just opened).
    expect(card.text()).toMatch(/\d+s/)
    // Current model is listed and marked disabled.
    const current = card.find('button[disabled]')
    expect(current.text()).toContain('deepseek-v4-flash:cloud')
    expect(card.text()).toContain('current model')
    // Vision candidate is clickable.
    const candidate = card.findAll('button.question-option').find(b => b.text().includes('minimax-m3:cloud'))
    expect(candidate).toBeDefined()
    expect(candidate!.attributes('disabled')).toBeUndefined()
    // Actions.
    expect(card.text()).toContain('Open picker')
    expect(card.text()).toContain('Cancel')
  })

  test('clicking a candidate switches the model', async () => {
    const store = useProjectStore()
    store.chats = [makeChat()]
    store.activeChatId = CHAT_ID
    store.activeCapabilityQuestions = { [CHAT_ID]: [makeQuestion()] }
    const respond = vi.spyOn(store, 'respondCapability')

    const wrapper = await mountPanel()
    const candidate = wrapper
      .findAll('button.question-option')
      .find(b => b.text().includes('minimax-m3:cloud'))!
    await candidate.trigger('click')

    expect(respond).toHaveBeenCalledWith(CHAT_ID, REQ_ID, 'switch', 'minimax-m3:cloud')
  })

  test('Open picker sends picker and opens the model selector', async () => {
    const store = useProjectStore()
    store.chats = [makeChat()]
    store.activeChatId = CHAT_ID
    store.activeCapabilityQuestions = { [CHAT_ID]: [makeQuestion()] }
    const respond = vi.spyOn(store, 'respondCapability')

    const wrapper = await mountPanel()
    const picker = wrapper.findAll('button.btn-sm').find(b => b.text() === 'Open picker')!
    await picker.trigger('click')

    expect(respond).toHaveBeenCalledWith(CHAT_ID, REQ_ID, 'picker')
    // The picker opens (ModelSelector is stubbed, but the v-if flips).
    expect(wrapper.findComponent({ name: 'NoopStub' }).exists()).toBe(true)
  })

  test('Cancel sends cancel', async () => {
    const store = useProjectStore()
    store.chats = [makeChat()]
    store.activeChatId = CHAT_ID
    store.activeCapabilityQuestions = { [CHAT_ID]: [makeQuestion()] }
    const respond = vi.spyOn(store, 'respondCapability')

    const wrapper = await mountPanel()
    const cancel = wrapper.findAll('button.btn-sm').find(b => b.text() === 'Cancel')!
    await cancel.trigger('click')

    expect(respond).toHaveBeenCalledWith(CHAT_ID, REQ_ID, 'cancel')
  })

  test('expired question disables the buttons', async () => {
    const store = useProjectStore()
    store.chats = [makeChat()]
    store.activeChatId = CHAT_ID
    store.activeCapabilityQuestions = {
      [CHAT_ID]: [{ ...makeQuestion(), opened_at: Date.now() - 31_000 }],
    }

    const wrapper = await mountPanel()
    const card = wrapper.find('.capability-card')
    const buttons = card.findAll('button')
    expect(buttons.length).toBeGreaterThan(0)
    for (const b of buttons) {
      expect(b.attributes('disabled')).toBeDefined()
    }
    // Countdown is clamped at 0.
    expect(card.text()).toContain('0s')
  })
})
