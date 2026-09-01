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
const ModelSelectorStub = vi.hoisted(() => ({
  name: 'ModelSelector',
  props: ['modelValue', 'sections', 'disabled', 'activeModels', 'searchable', 'placeholder', 'filterSection'],
  emits: ['select', 'update:modelValue', 'close'],
  template: `<div class="model-selector-stub" :data-disabled="String(disabled)"><div v-for="s in sections" :key="s.key" :data-section="s.key" :data-disabled="String(s.disabled)"><span class="section-label">{{ s.label }}</span><button v-for="m in s.models" :key="m" class="model-option" :data-model="m" :disabled="s.disabled" @click="$emit('select', m, s.key)">{{ m }}</button></div></div>`,
}))
vi.mock('../ModelSelector.vue', () => ({ default: ModelSelectorStub }))
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
  test('renders the card with header, countdown, inline picker and Cancel', async () => {
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
    // Inline ModelSelector is rendered (stubbed), showing current disabled and vision models.
    const selector = wrapper.find('.model-selector-stub')
    expect(selector.exists()).toBe(true)
    // Current model is in a disabled section.
    const disabledSection = selector.find('[data-section="current"]')
    expect(disabledSection.exists()).toBe(true)
    expect(disabledSection.attributes('data-disabled')).toBe('true')
    expect(disabledSection.text()).toContain('deepseek-v4-flash:cloud')
    // Vision candidate is clickable.
    const candidate = selector.find('[data-model="minimax-m3:cloud"]')
    expect(candidate.exists()).toBe(true)
    expect(candidate.attributes('disabled')).toBeUndefined()
    // Only Cancel action remains; Open picker is gone (its purpose is subsumed by the inline picker).
    expect(card.text()).toContain('Cancel')
    expect(card.text()).not.toContain('Open picker')
  })

  test('picking a vision model from the inline picker switches the model', async () => {
    const store = useProjectStore()
    store.chats = [makeChat()]
    store.activeChatId = CHAT_ID
    store.activeCapabilityQuestions = { [CHAT_ID]: [makeQuestion()] }
    const respond = vi.spyOn(store, 'respondCapability')

    const wrapper = await mountPanel()
    const candidate = wrapper.find('[data-model="minimax-m3:cloud"]')
    await candidate.trigger('click')

    expect(respond).toHaveBeenCalledWith(CHAT_ID, REQ_ID, 'switch', 'minimax-m3:cloud')
  })

  test('current model entry is disabled and not switchable', async () => {
    const store = useProjectStore()
    store.chats = [makeChat()]
    store.activeChatId = CHAT_ID
    store.activeCapabilityQuestions = { [CHAT_ID]: [makeQuestion()] }
    const respond = vi.spyOn(store, 'respondCapability')

    const wrapper = await mountPanel()
    const current = wrapper.find('[data-section="current"] [data-model="deepseek-v4-flash:cloud"]')
    expect(current.attributes('disabled')).toBeDefined()
    await current.trigger('click')
    // Disabled button does not emit select, so no switch.
    expect(respond).not.toHaveBeenCalled()
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

  test('expired question disables the picker and Cancel', async () => {
    const store = useProjectStore()
    store.chats = [makeChat()]
    store.activeChatId = CHAT_ID
    store.activeCapabilityQuestions = {
      [CHAT_ID]: [{ ...makeQuestion(), opened_at: Date.now() - 31_000 }],
    }

    const wrapper = await mountPanel()
    const card = wrapper.find('.capability-card')
    // Inline picker is disabled via ModelSelector prop.
    const selector = wrapper.find('.model-selector-stub')
    expect(selector.attributes('data-disabled')).toBe('true')
    // Cancel is also disabled.
    const cancel = wrapper.findAll('button.btn-sm').find(b => b.text() === 'Cancel')!
    expect(cancel.attributes('disabled')).toBeDefined()
    // Countdown is clamped at 0.
    expect(card.text()).toContain('0s')
  })
})
