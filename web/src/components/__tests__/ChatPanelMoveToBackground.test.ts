// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, RouterLinkStub, shallowMount } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'

const PaneHeaderStub = defineComponent({ name: 'PaneHeaderStub', setup: (_, { slots }) => () => h('header', [slots.title?.(), slots.actions?.()]) })
const ChildStub = defineComponent({ name: 'ChildStub', setup: () => () => h('div') })
const ChatCommentPopoverStub = defineComponent({
  name: 'ChatCommentPopoverStub',
  setup(_, { expose }) {
    expose({ openId: null, close: vi.fn(), clearPendingClose: vi.fn(), onTargetOver: vi.fn(), onTargetOut: vi.fn(), pinFromEvent: vi.fn(), show: vi.fn() })
    return () => h('div')
  },
})
const MODELS_RESPONSE = {
  models: ['sonnet', 'opus'], default: 'sonnet',
  provider_models: { claude: ['sonnet', 'opus'], opencode: ['anthropic/claude-sonnet-4-6'] },
  provider_defaults: { claude: 'sonnet', opencode: 'anthropic/claude-sonnet-4-6' },
  thinking_levels: {}, model_reasoning_levels: {}, model_options: {}, backends: { anthropic: true },
}
function makeProject(): ProjectInfo {
  return { project_id: 'project-1', name: 'Upwordo', workspace: 'personal', context: '', created_at: '', order: 0, vault_folder: '' } as unknown as ProjectInfo
}
function makeChat(chatId: string, extra: Partial<ChatInfo> = {}): ChatInfo {
  return { chat_id: chatId, project_id: 'project-1', title: chatId, model: 'sonnet', provider: 'opencode', mode: 'normal', session_id: '', created_at: '', archived: false, local: true, ...extra } as ChatInfo
}

async function mountPanel(setup: (store: ReturnType<typeof useProjectStore>) => void = () => {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat('chat-1')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [] }
  store.workspaces = [{ name: 'personal', color: 'emerald' }] as unknown as typeof store.workspaces
  store.bootstrapped = true
  setup(store)
  vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path === '/api/commands') return Promise.resolve({ commands: [] }) as never
    return Promise.resolve([]) as never
  })
  const wrapper = shallowMount(ChatPanel, { attachTo: document.body, global: { plugins: [pinia], stubs: {
    PaneHeader: PaneHeaderStub, ModelSelector: ChildStub, SubagentPanel: ChildStub,
    ChatCommentPopover: ChatCommentPopoverStub, CommentComposePopover: ChildStub, RouterLink: RouterLinkStub,
  } } })
  await flushPromises()
  return { wrapper, store }
}

describe('ChatPanel move to background', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(10000) })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); document.body.innerHTML = '' })

  it('shows the chip beside Stop once a backgroundable call has run 3 s', async () => {
    const { wrapper } = await mountPanel(store => {
      store.streaming['chat-1'] = true
      store.backgroundableCalls['chat-1'] = { c1: Date.now() }
    })
    expect(wrapper.find('.composer-move-bg').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.find('.composer-move-bg').exists()).toBe(true)
    const chipElement = wrapper.find('.composer-move-bg').element as HTMLElement
    const sendElement = wrapper.find('.send-btn').element as HTMLElement
    expect(Boolean(chipElement.compareDocumentPosition(sendElement) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true)
    expect(wrapper.find('.composer-move-bg').attributes('aria-label')).toBe('Move to background')
    wrapper.unmount()
  })

  it('hides the chip when no call is backgroundable or the turn is idle', async () => {
    const { wrapper, store } = await mountPanel(s => { s.streaming['chat-1'] = true; s.backgroundableCalls['chat-1'] = {} })
    expect(wrapper.find('.composer-move-bg').exists()).toBe(false)
    store.backgroundableCalls['chat-1'] = { c1: Date.now() - 4000 }
    store.streaming['chat-1'] = false
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.composer-move-bg').exists()).toBe(false)
    wrapper.unmount()
  })

  it('clicking calls moveChatToBackground and returns focus to the composer', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 })
    const { wrapper, store } = await mountPanel(s => {
      s.streaming['chat-1'] = true
      s.backgroundableCalls['chat-1'] = { c1: Date.now() - 4000 }
      vi.spyOn(s, 'moveChatToBackground').mockResolvedValue(true)
    })
    await wrapper.vm.$nextTick()
    const chip = wrapper.find('.composer-move-bg')
    const button = chip.element as HTMLButtonElement
    button.focus()
    await chip.trigger('click')
    await flushPromises()
    expect(store.moveChatToBackground).toHaveBeenCalledWith('chat-1')
    expect(document.activeElement).toBe(wrapper.find('textarea.chat-input').element)
    wrapper.unmount()
  })

  it('keeps focus in the composer when the turn ends before the request resolves', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 375 })
    let resolveMove!: (result: boolean) => void
    const moveRequest = new Promise<boolean>(resolve => { resolveMove = resolve })
    const { wrapper, store } = await mountPanel(s => {
      s.streaming['chat-1'] = true
      s.backgroundableCalls['chat-1'] = { c1: Date.now() - 4000 }
      vi.spyOn(s, 'moveChatToBackground').mockReturnValue(moveRequest)
    })
    await wrapper.vm.$nextTick()
    const chip = wrapper.find('.composer-move-bg')
    ;(chip.element as HTMLButtonElement).focus()
    await chip.trigger('click')
    store.streaming['chat-1'] = false
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.composer-move-bg').exists()).toBe(false)

    resolveMove(true)
    await flushPromises()
    const composerBar = wrapper.find('.composer-bar').element
    expect(composerBar.contains(document.activeElement)).toBe(true)
    wrapper.unmount()
  })

  it('is a native button reachable by keyboard', async () => {
    const { wrapper } = await mountPanel(s => { s.streaming['chat-1'] = true; s.backgroundableCalls['chat-1'] = { c1: Date.now() - 4000 } })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.composer-move-bg').element.tagName).toBe('BUTTON')
    expect(wrapper.find('.composer-move-bg').attributes('type')).toBe('button')
    expect(wrapper.find('.composer-move-bg').attributes('tabindex')).not.toBe('-1')
    wrapper.unmount()
  })
})
