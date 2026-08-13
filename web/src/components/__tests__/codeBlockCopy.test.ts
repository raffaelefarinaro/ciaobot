// @vitest-environment jsdom

// Fenced code blocks in chat markdown carry their own copy button. The button
// is emitted by the markdown renderer (chat markdown is injected with v-html,
// so it cannot be a Vue component) and driven by one delegated listener on the
// ChatPanel root — which is also what makes it survive the innerHTML swap that
// happens on every streamed token. Both halves are pinned here, plus the
// insecure-origin path where navigator.clipboard does not exist at all.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import { COPY_FEEDBACK_MS } from '../../lib/codeCopy'
import { renderMarkdown } from '../../lib/safeMarkdown'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatPanel from '../ChatPanel.vue'

const FENCED = ['Here you go:', '', '```python', 'print("hi")', 'x = 1', '```'].join('\n')

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
    provider: 'claude',
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

function installClipboard(): { writeText: ReturnType<typeof vi.fn> } {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText },
  })
  return { writeText }
}

function removeClipboard(): void {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
}

// jsdom has no matchMedia; ChatPanel consults it to decide whether a tap on a
// bubble should toggle the action rail. `matches: true` is the touch case.
function stubMatchMedia(matches: boolean): void {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn(() => ({ matches } as MediaQueryList)),
  })
}

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
  vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  vi.spyOn(api, 'get').mockImplementation((path: string) => {
    if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
    return Promise.resolve([]) as never
  })

  const wrapper = shallowMount(ChatPanel, {
    attachTo: document.body,
    global: {
      plugins: [pinia],
      stubs: {
        PaneHeader: PaneHeaderStub,
        ModelSelector: ChildStub,
        VoiceRecorder: ChildStub,
        SubagentPanel: ChildStub,
        ChatCommentPopover: ChatCommentPopoverStub,
        CommentComposePopover: ChildStub,
        RouterLink: ChildStub,
      },
    },
  })
  await flushPromises()
  return { wrapper, store }
}

describe('fenced code block markup', () => {
  it('gives every fenced block a real, labelled copy button', () => {
    const html = renderMarkdown(FENCED)
    const host = document.createElement('div')
    host.innerHTML = html

    const block = host.querySelector('.code-block')
    expect(block).toBeTruthy()
    const button = block!.querySelector('button.code-copy-btn') as HTMLButtonElement
    // A real button (keyboard reachable, focusable) that survives sanitizing.
    expect(button).toBeInstanceOf(HTMLButtonElement)
    expect(button.getAttribute('type')).toBe('button')
    expect(button.getAttribute('aria-label')).toBe('Copy code')
    expect(button.textContent).toBe('Copy')
    // The state hook the styling keys off must survive DOMPurify too.
    expect(button.dataset.copyState).toBe('idle')
    // The language stays a class on <code>; it must never leak into the text.
    expect(block!.querySelector('pre code')?.className).toContain('language-python')
  })

  it('leaves inline code and prose alone', () => {
    const html = renderMarkdown('Use `ls -l` first.')
    expect(html).not.toContain('code-copy-btn')
    expect(html).not.toContain('code-block')
  })
})

describe('copy button behaviour', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
    stubMatchMedia(false)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    removeClipboard()
    document.body.innerHTML = ''
    localStorage.clear()
  })

  it('copies the raw block text and confirms, then reverts', async () => {
    const { writeText } = installClipboard()
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'assistant', content: FENCED, timestamp: '2026-08-12T10:00:00Z' },
    ]
    await flushPromises()

    const button = wrapper.find('.message-content .code-copy-btn')
    expect(button.exists()).toBe(true)

    vi.useFakeTimers()
    await button.trigger('click')
    await Promise.resolve()
    await Promise.resolve()

    // Raw source, not highlighted markup, no language label, no trailing
    // newline that marked adds to every fence.
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText.mock.calls[0][0]).toBe('print("hi")\nx = 1')

    const el = button.element as HTMLButtonElement
    expect(el.dataset.copyState).toBe('copied')
    expect(el.textContent).toBe('Copied!')
    expect(el.getAttribute('aria-label')).toBe('Code copied')

    vi.advanceTimersByTime(COPY_FEEDBACK_MS)
    expect(el.dataset.copyState).toBe('idle')
    expect(el.textContent).toBe('Copy')
    expect(el.getAttribute('aria-label')).toBe('Copy code')

    vi.useRealTimers()
    wrapper.unmount()
  })

  it('still copies after the bubble re-renders, as it does while streaming', async () => {
    const { writeText } = installClipboard()
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'assistant', content: FENCED, timestamp: '2026-08-12T10:00:00Z' },
    ]
    await flushPromises()
    const firstNode = wrapper.find('.message-content .code-copy-btn').element

    // A streamed token replaces the bubble's innerHTML wholesale: the button
    // node is a different element afterwards, so only delegation keeps working.
    store.messages['chat-1'] = [
      {
        role: 'assistant',
        content: `${FENCED}\n\nAnd a second one:\n\n\`\`\`sh\nls -l\n\`\`\``,
        timestamp: '2026-08-12T10:00:00Z',
      },
    ]
    await flushPromises()

    const buttons = wrapper.findAll('.message-content .code-copy-btn')
    expect(buttons).toHaveLength(2)
    expect(buttons[0].element).not.toBe(firstNode)

    await buttons[1].trigger('click')
    await Promise.resolve()
    await Promise.resolve()

    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText.mock.calls[0][0]).toBe('ls -l')
    wrapper.unmount()
  })

  it('degrades without throwing when the clipboard API is unavailable', async () => {
    // Insecure origin (plain http on the LAN): navigator.clipboard is absent
    // and jsdom has no execCommand either, so both paths fail.
    removeClipboard()
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'assistant', content: FENCED, timestamp: '2026-08-12T10:00:00Z' },
    ]
    await flushPromises()

    const button = wrapper.find('.message-content .code-copy-btn')
    await expect(button.trigger('click')).resolves.not.toThrow()
    await Promise.resolve()
    await Promise.resolve()

    const el = button.element as HTMLButtonElement
    expect(el.dataset.copyState).toBe('failed')
    expect(el.getAttribute('aria-label')).toBe('Copy failed')
    wrapper.unmount()
  })

  it('does not toggle the message action rail when the button is tapped', async () => {
    installClipboard()
    stubMatchMedia(true)
    const { wrapper, store } = await mountPanel()
    store.messages['chat-1'] = [
      { role: 'assistant', content: FENCED, timestamp: '2026-08-12T10:00:00Z' },
    ]
    await flushPromises()

    await wrapper.find('.message-content .code-copy-btn').trigger('click')
    await flushPromises()

    expect(wrapper.find('.message-wrap.assistant').classes()).not.toContain('actions-tapped')
    wrapper.unmount()
  })
})
