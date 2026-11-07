// @vitest-environment jsdom

/**
 * `components/needs-you/McpAuthBody.vue` for a Claude chat: a waiting sign-in
 * shows the sign-in page as a link (the engine sends the URL a poll later, so
 * there is no click to open a pop-up from), and the helper terminal can be
 * shown beside the chat. OpenCode cards keep only B2's controls.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount, type VueWrapper } from '@vue/test-utils'
import McpAuthBody from '../McpAuthBody.vue'
import type { ChatAction } from '../../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const terminal = vi.hoisted(() => ({ open: vi.fn(), selectSession: vi.fn() }))

vi.mock('../../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() },
}))
vi.mock('../../../stores/terminal', () => ({ useTerminalStore: () => terminal }))

const CHAT_ID = 'chat-1'

function action(over: Partial<ChatAction> = {}): ChatAction {
  return {
    id: 'a1',
    kind: 'mcp_auth',
    origin: 'engine',
    state: 'waiting',
    params: { server: 'notion', provider: 'claude', url: 'https://mcp.notion.com/authorize?a=1', session_id: 's1' },
    error: '',
    created_at: 1,
    ...over,
  }
}

let wrapper: VueWrapper | null = null

function mountBody(a: ChatAction) {
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(McpAuthBody, {
    props: { chatId: CHAT_ID, action: a },
    attachTo: document.body,
    global: { plugins: [pinia] },
  })
  return wrapper
}

function terminalButton(w: VueWrapper) {
  return w.findAll('button').find(b => b.text() === 'Show sign-in terminal')
}

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  terminal.open.mockReset().mockResolvedValue(undefined)
  terminal.selectSession.mockReset().mockReturnValue(true)
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.restoreAllMocks()
})

describe('McpAuthBody for a Claude chat', () => {
  it('renders the sign-in link for a waiting Claude action', () => {
    const w = mountBody(action())
    const link = w.get('a.needs-you-btn')
    expect(link.attributes('href')).toBe('https://mcp.notion.com/authorize?a=1')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    expect(link.text()).toBe('Open the sign-in page')
    expect(terminalButton(w)).toBeDefined()
  })

  it('does not render a link for a javascript url, and keeps the terminal button', () => {
    const w = mountBody(action({ params: { server: 'notion', provider: 'claude', url: 'javascript:alert(1)', session_id: 's1' } }))
    expect(w.find('a[href]').exists()).toBe(false)
    expect(terminalButton(w)).toBeDefined()
  })

  it('shows the terminal button while a Claude sign-in is working', () => {
    const w = mountBody(action({ state: 'working', params: { server: 'notion', provider: 'claude', session_id: 's1' } }))
    expect(terminalButton(w)).toBeDefined()
    expect(w.find('a').exists()).toBe(false)
  })

  it('shows the terminal button for Claude only', () => {
    const w = mountBody(action({ params: { server: 'notion', provider: 'opencode', url: 'https://x.example/a', session_id: 's1' } }))
    expect(terminalButton(w)).toBeUndefined()
  })
})
