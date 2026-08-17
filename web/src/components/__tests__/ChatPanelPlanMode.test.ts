// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
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

// The Mode row lives in the model picker's header slot, so the stub has to
// render slots for the mode chips to be reachable from these tests.
const ModelSelectorStub = defineComponent({
  name: 'ModelSelectorStub',
  setup(_, { slots }) {
    return () => h('div', { class: 'model-selector' }, [
      slots.header?.(),
      slots.footer?.(),
    ])
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

function makeChat(chatId: string, mode: string = 'normal'): ChatInfo {
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title: chatId,
    model: 'sonnet',
    provider: 'claude',
    mode,
    session_id: '',
    created_at: '',
    archived: false,
  }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

type Harness = {
  wrapper: VueWrapper
  store: ReturnType<typeof useProjectStore>
  updateChat: ReturnType<typeof vi.spyOn>
  sendMessage: ReturnType<typeof vi.spyOn>
}

class MemoryStorage {
  private values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }

  removeItem(key: string): void {
    this.values.delete(key)
  }

  clear(): void {
    this.values.clear()
  }
}

function planReturnModeStorageKey(chatId: string): string {
  return `ciao-plan-return-mode:${chatId}`
}

async function mountPanel(options: {
  mode?: string
  commandsFail?: boolean
  skills?: Array<{ name: string; description: string; argument_hint: string; source: 'skill'; path: string }>
} = {}): Promise<Harness> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.projects = [makeProject()]
  store.chats = [makeChat('chat-1', options.mode), makeChat('chat-2')]
  store.activeChatId = 'chat-1'
  store.messages = { 'chat-1': [], 'chat-2': [] }
  store.bootstrapped = true

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path.startsWith('/api/commands')) {
      if (options.commandsFail) return Promise.reject(new Error('commands unavailable')) as never
      return Promise.resolve({ commands: [], skills: options.skills || [] }) as never
    }
    return Promise.resolve([]) as never
  })

  const updateChat = vi.spyOn(store, 'updateChat')
  const sendMessage = vi.spyOn(store, 'sendMessage')
  const wrapper = shallowMount(ChatPanel, {
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ModelSelectorStub,
        VoiceRecorder: ChildStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
      },
    },
  })
  await flushPromises()
  return { wrapper, store, updateChat, sendMessage }
}

function textareaValue(wrapper: VueWrapper): string {
  return (wrapper.get('textarea.chat-input').element as HTMLTextAreaElement).value
}

/** Open the model picker and click a Mode row chip by its visible label. */
async function pickMode(wrapper: VueWrapper, label: string): Promise<void> {
  await wrapper.get('button.model-picker-btn').trigger('click')
  const chip = wrapper.findAll('button.mode-row-chip').find(button => button.text() === label)
  if (!chip) throw new Error(`no mode chip labelled ${label}`)
  await chip.trigger('click')
}

describe('ChatPanel plan mode', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('enters plan mode from the Mode row in the model picker', async () => {
    const { wrapper, updateChat, sendMessage } = await mountPanel()
    updateChat.mockResolvedValue()

    await pickMode(wrapper, 'Plan')
    await flushPromises()

    expect(updateChat).toHaveBeenCalledWith('chat-1', { mode: 'plan' })
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('sends /plan as an ordinary message now that the command is gone', async () => {
    const { wrapper, updateChat, sendMessage } = await mountPanel()
    sendMessage.mockResolvedValue()

    await wrapper.get('textarea.chat-input').setValue('/plan')
    await wrapper.get('button.send-btn').trigger('click')
    await flushPromises()

    expect(updateChat).not.toHaveBeenCalled()
    expect(sendMessage).toHaveBeenCalled()
    expect(String(sendMessage.mock.calls[0]?.[1] ?? '')).toBe('/plan')
    wrapper.unmount()
  })

  it('falls back to auto when the plan chip leaves externally-entered plan mode', async () => {
    const { wrapper, updateChat } = await mountPanel({ mode: 'plan' })
    const pending = deferred<void>()
    updateChat.mockReturnValue(pending.promise)
    const chip = wrapper.get('button.plan-mode-chip')

    expect(chip.attributes('aria-label')).toBe('Leave plan mode')
    expect(chip.attributes('aria-pressed')).toBe('true')
    expect(chip.classes()).toContain('touch-hit')
    expect(chip.attributes('disabled')).toBeUndefined()

    await chip.trigger('click')
    await nextTick()

    expect(updateChat).toHaveBeenCalledWith('chat-1', { mode: 'auto' })
    expect(wrapper.get('button.plan-mode-chip').attributes('disabled')).toBeDefined()

    pending.resolve()
    await flushPromises()

    expect(wrapper.get('button.plan-mode-chip').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('shows an error and stays out of plan mode when the PATCH fails', async () => {
    const { wrapper, store, updateChat } = await mountPanel()
    updateChat.mockRejectedValueOnce(new Error('PATCH failed')).mockResolvedValueOnce()

    await pickMode(wrapper, 'Plan')
    await flushPromises()

    expect(store.toasts.at(-1)).toMatchObject({
      title: 'Could not change plan mode',
      variant: 'error',
    })

    await pickMode(wrapper, 'Plan')
    await flushPromises()

    expect(updateChat).toHaveBeenNthCalledWith(2, 'chat-1', { mode: 'plan' })
    wrapper.unmount()
  })

  it('leaves the composer untouched when the mode changes', async () => {
    const { wrapper, updateChat } = await mountPanel()
    updateChat.mockResolvedValue()

    await wrapper.get('textarea.chat-input').setValue('half-written prompt')
    await pickMode(wrapper, 'Plan')
    await flushPromises()

    expect(textareaValue(wrapper)).toBe('half-written prompt')
    wrapper.unmount()
  })

  it('leaves the slash picker empty when the command API fails', async () => {
    const { wrapper } = await mountPanel({ commandsFail: true })

    await wrapper.get('textarea.chat-input').setValue('/')

    expect(wrapper.find('.commands-picker-name').exists()).toBe(false)
    wrapper.unmount()
  })

  it('lists provider skills in the slash picker', async () => {
    const { wrapper } = await mountPanel({
      skills: [{
        name: 'research',
        description: 'Research with the loaded skill',
        argument_hint: '',
        source: 'skill',
        path: 'skills/',
      }],
    })

    await wrapper.get('textarea.chat-input').setValue('/res')
    const row = wrapper.get('.commands-picker-row')
    expect(row.text()).toContain('skill')
    expect(row.text()).toContain('/research')
    await row.trigger('mousedown')
    expect(textareaValue(wrapper)).toBe('/research')
    wrapper.unmount()
  })

  it('lists and inserts skills from a slash token anywhere in the draft', async () => {
    const { wrapper } = await mountPanel({
      skills: [{
        name: 'research',
        description: 'Research with the loaded skill',
        argument_hint: '',
        source: 'skill',
        path: 'skills/',
      }],
    })

    await wrapper.get('textarea.chat-input').setValue('Please use /res')
    const row = wrapper.get('.commands-picker-row')
    expect(row.text()).toContain('/research')
    await row.trigger('mousedown')
    await nextTick()

    expect(textareaValue(wrapper)).toBe('Please use /research')
    wrapper.unmount()
  })

  it('restores auto after the picker enters and the chip exits plan mode', async () => {
    const first = await mountPanel({ mode: 'auto' })
    first.updateChat.mockResolvedValue()

    await pickMode(first.wrapper, 'Plan')
    await flushPromises()

    expect(first.updateChat).toHaveBeenCalledWith('chat-1', { mode: 'plan' })
    first.wrapper.unmount()

    const second = await mountPanel({ mode: 'plan' })
    second.updateChat.mockResolvedValue()
    await second.wrapper.get('button.plan-mode-chip').trigger('click')
    await flushPromises()

    expect(second.updateChat).toHaveBeenCalledWith('chat-1', { mode: 'auto' })
    second.wrapper.unmount()
  })

  it('restores normal after entering plan, reloading, and exiting', async () => {
    const first = await mountPanel({ mode: 'normal' })
    first.updateChat.mockResolvedValue()

    await pickMode(first.wrapper, 'Plan')
    await flushPromises()
    first.wrapper.unmount()

    const second = await mountPanel({ mode: 'plan' })
    second.updateChat.mockResolvedValue()
    await second.wrapper.get('button.plan-mode-chip').trigger('click')
    await flushPromises()

    expect(second.updateChat).toHaveBeenCalledWith('chat-1', { mode: 'normal' })
    second.wrapper.unmount()
  })

  it('falls back to auto when the persisted return mode is corrupt', async () => {
    localStorage.setItem(planReturnModeStorageKey('chat-1'), 'turbo')
    const { wrapper, updateChat } = await mountPanel({ mode: 'plan' })
    updateChat.mockResolvedValue()

    await wrapper.get('button.plan-mode-chip').trigger('click')
    await flushPromises()

    expect(updateChat).toHaveBeenCalledWith('chat-1', { mode: 'auto' })
    wrapper.unmount()
  })

  it('restores bypass after the panel is remounted in plan mode', async () => {
    const first = await mountPanel({ mode: 'bypass' })
    first.updateChat.mockResolvedValue()

    await pickMode(first.wrapper, 'Plan')
    await flushPromises()
    first.wrapper.unmount()

    const second = await mountPanel({ mode: 'plan' })
    second.updateChat.mockResolvedValue()
    await second.wrapper.get('button.plan-mode-chip').trigger('click')
    await flushPromises()

    expect(second.updateChat).toHaveBeenCalledWith('chat-1', { mode: 'bypass' })
    second.wrapper.unmount()
  })

  it('restores the remembered mode when the plan chip exits plan mode', async () => {
    localStorage.setItem(planReturnModeStorageKey('chat-1'), 'normal')
    const { wrapper, updateChat } = await mountPanel({ mode: 'plan' })
    updateChat.mockResolvedValue()

    await wrapper.get('button.plan-mode-chip').trigger('click')
    await flushPromises()

    expect(updateChat).toHaveBeenCalledWith('chat-1', { mode: 'normal' })
    wrapper.unmount()
  })

  it('drops the return marker when the picker leaves plan for another mode', async () => {
    localStorage.setItem(planReturnModeStorageKey('chat-1'), 'bypass')
    const { wrapper, updateChat } = await mountPanel({ mode: 'plan' })
    updateChat.mockResolvedValue()

    await pickMode(wrapper, 'Manual')
    await flushPromises()

    expect(updateChat).toHaveBeenCalledWith('chat-1', { mode: 'normal' })
    expect(localStorage.getItem(planReturnModeStorageKey('chat-1'))).toBeNull()
    wrapper.unmount()
  })

  it('removes any return marker when entering plan mode fails', async () => {
    localStorage.setItem(planReturnModeStorageKey('chat-1'), 'bypass')
    const { wrapper, updateChat } = await mountPanel({ mode: 'auto' })
    updateChat.mockRejectedValue(new Error('PATCH failed'))

    await pickMode(wrapper, 'Plan')
    await flushPromises()

    expect(localStorage.getItem(planReturnModeStorageKey('chat-1'))).toBeNull()
    wrapper.unmount()
  })

  it('retains the return marker after a failed exit so a retry restores the same mode', async () => {
    localStorage.setItem(planReturnModeStorageKey('chat-1'), 'bypass')
    const { wrapper, updateChat } = await mountPanel({ mode: 'plan' })
    updateChat.mockRejectedValueOnce(new Error('PATCH failed')).mockResolvedValueOnce()

    await wrapper.get('button.plan-mode-chip').trigger('click')
    await flushPromises()

    expect(localStorage.getItem(planReturnModeStorageKey('chat-1'))).toBe('bypass')

    await wrapper.get('button.plan-mode-chip').trigger('click')
    await flushPromises()

    expect(updateChat).toHaveBeenNthCalledWith(2, 'chat-1', { mode: 'bypass' })
    expect(localStorage.getItem(planReturnModeStorageKey('chat-1'))).toBeNull()
    wrapper.unmount()
  })
})
