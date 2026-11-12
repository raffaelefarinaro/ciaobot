// @vitest-environment jsdom

/**
 * `components/needs-you/McpAuthBody.vue` for a Claude chat: a waiting sign-in
 * shows the sign-in page as a link (the engine sends the URL a poll later, so
 * there is no click to open a pop-up from), and the helper terminal can be
 * shown beside the chat. OpenCode cards keep only B2's controls.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import McpAuthBody from '../McpAuthBody.vue'
import { mcpTurnOffErrorText, mcpTurnOffScopeText } from '../../../lib/chatActions'
import type { ChatAction } from '../../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const terminal = vi.hoisted(() => ({ open: vi.fn(), selectSession: vi.fn() }))
const loopback = vi.hoisted(() => ({ page: false }))

vi.mock('../../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() },
}))
vi.mock('../../../stores/terminal', () => ({ useTerminalStore: () => terminal }))
vi.mock('../../../lib/loopback', () => ({ isLoopbackPage: () => loopback.page, isLoopbackHostname: () => false }))

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
  loopback.page = false
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

describe('McpAuthBody paste-back field', () => {
  function pasteInput(w: VueWrapper) {
    return w.find('input[type=url]')
  }

  it('shows the paste field on a non-loopback page', () => {
    const w = mountBody(action())
    expect(pasteInput(w).exists()).toBe(true)
    expect(w.findAll('button').some(b => b.text() === 'Finish sign-in')).toBe(true)
  })

  it('shows it for an OpenCode sign-in too', () => {
    const w = mountBody(action({ params: { server: 'linear', provider: 'opencode', url: 'https://x.example/a' } }))
    expect(pasteInput(w).exists()).toBe(true)
  })

  it('hides the paste field on the host', () => {
    loopback.page = true
    const w = mountBody(action())
    expect(pasteInput(w).exists()).toBe(false)
  })

  it('hides it once used', () => {
    const w = mountBody(action({ params: { server: 'notion', provider: 'claude', url: 'https://mcp.notion.com/authorize?a=1', session_id: 's1', callback_used: '1' } }))
    expect(pasteInput(w).exists()).toBe(false)
  })

  it('hides it until the sign-in page is known', () => {
    const w = mountBody(action({ params: { server: 'notion', provider: 'claude', session_id: 's1' } }))
    expect(pasteInput(w).exists()).toBe(false)
  })

  it('keeps the focus on the card when the field goes away after the send', async () => {
    // A focusable card in the document, so the focus has somewhere to rest.
    const card = document.createElement('div')
    card.className = 'needs-you-card'
    card.tabIndex = -1
    document.body.appendChild(card)
    const pinia = createPinia()
    setActivePinia(pinia)
    wrapper = mount(McpAuthBody, {
      props: { chatId: CHAT_ID, action: action() },
      attachTo: card,
      global: { plugins: [pinia] },
    })
    const input = pasteInput(wrapper).element as HTMLInputElement
    input.focus()
    expect(document.activeElement).toBe(input)

    await wrapper.setProps({
      action: action({ params: { server: 'notion', provider: 'claude', url: 'https://mcp.notion.com/authorize?a=1', session_id: 's1', callback_used: '1' } }),
    })
    await nextTick()
    await flushPromises()

    expect(pasteInput(wrapper).exists()).toBe(false)
    expect(document.activeElement).not.toBe(document.body)
    wrapper.unmount()
    wrapper = null
    card.remove()
  })

  it('says the address was sent', async () => {
    apiPost.mockResolvedValueOnce({
      action: action({ params: { server: 'notion', provider: 'claude', url: 'https://mcp.notion.com/authorize?a=1', session_id: 's1', callback_used: '1' } }),
    })
    const w = mountBody(action())
    w.get('input[type=url]').setValue('http://localhost:1234/callback?code=a')
    w.get('form').trigger('submit')
    await flushPromises()
    expect(w.emitted('notice')?.at(-1)).toEqual(['Address sent. This card updates when the sign-in finishes.'])
  })
})

describe('McpAuthBody Turn off', () => {
  function turnOffButton(w: VueWrapper) {
    return w.get('button[aria-label="Turn off notion"]')
  }

  it('offers Turn off with its scope and posts it', async () => {
    apiPost.mockResolvedValueOnce({ actions: [], reload: { reloaded: [], pending: [] } })
    const w = mountBody(action({ state: 'open', params: { server: 'notion', provider: 'opencode' } }))
    const button = turnOffButton(w)
    const scope = w.get('.needs-you-turn-off-scope')
    expect(button.classes()).toContain('needs-you-btn--quiet')
    expect(button.attributes('aria-describedby')).toBe(scope.attributes('id'))
    expect(scope.text()).toBe(mcpTurnOffScopeText('opencode'))

    await button.trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/chats/chat-1/actions/a1/turn-off', {})
  })

  it('shows the Claude scope line', () => {
    const w = mountBody(action({ state: 'open', params: { server: 'notion', provider: 'claude' } }))
    expect(w.get('.needs-you-turn-off-scope').text()).toContain('git repository')
  })

  it('shows the refusal in words and keeps the card', async () => {
    apiPost.mockRejectedValueOnce(Object.assign(new Error('config_has_comments'), {
      status: 400,
      payload: { error: 'config_has_comments', action: action({ state: 'open', params: { server: 'notion', provider: 'opencode' } }) },
    }))
    const w = mountBody(action({ state: 'open', params: { server: 'notion', provider: 'opencode' } }))

    await turnOffButton(w).trigger('click')
    await flushPromises()

    expect(w.emitted('error')?.at(-1)).toEqual([mcpTurnOffErrorText('config_has_comments', 'notion')])
  })

  it('does not start the sign-in while a Turn off is pending', async () => {
    apiPost.mockReturnValueOnce(new Promise(() => {}))
    const w = mountBody(action({ state: 'open', params: { server: 'notion', provider: 'opencode' } }))

    await turnOffButton(w).trigger('click')
    await nextTick()
    expect(turnOffButton(w).attributes('aria-busy')).toBe('true')
    await w.get('button.needs-you-btn--primary').trigger('click')
    await flushPromises()

    expect(apiPost).not.toHaveBeenCalledWith('/api/chats/chat-1/actions/a1/start', expect.anything())
  })

  it('hides Turn off on a done card', () => {
    const w = mountBody(action({ state: 'done', params: { server: 'notion', provider: 'opencode' } }))
    expect(w.findAll('button').some(b => b.text() === 'Turn off')).toBe(false)
    expect(w.find('.needs-you-turn-off-scope').exists()).toBe(false)
  })
})
