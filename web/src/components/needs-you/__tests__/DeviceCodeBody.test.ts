// @vitest-environment jsdom

/**
 * `components/needs-you/DeviceCodeBody.vue`: a device-code sign-in (gh or az)
 * shows its one-time code large with Copy, a link to the tool's sign-in page,
 * and the helper terminal. The card owns Not now and Continue; this body owns
 * the primary control only.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import DeviceCodeBody from '../DeviceCodeBody.vue'
import { useProjectStore } from '../../../stores/projects'
import type { ChatAction } from '../../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const terminal = vi.hoisted(() => ({ open: vi.fn(), selectSession: vi.fn() }))
const writeClipboardMock = vi.hoisted(() => vi.fn())

vi.mock('../../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() },
}))
vi.mock('../../../stores/terminal', () => ({ useTerminalStore: () => terminal }))
vi.mock('../../../lib/codeCopy', () => ({ writeClipboard: writeClipboardMock }))

const CHAT_ID = 'c1'

function action(over: Partial<ChatAction> = {}): ChatAction {
  return {
    id: 'a1',
    kind: 'device_code',
    origin: 'agent',
    state: 'waiting',
    params: {
      tool: 'gh',
      session_id: 's1',
      code: 'C891-07AA',
      url: 'https://github.com/login/device',
    },
    error: '',
    created_at: 1,
    ...over,
  }
}

let wrapper: VueWrapper | null = null

function mountBody(a: ChatAction) {
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(DeviceCodeBody, {
    props: { chatId: CHAT_ID, action: a },
    attachTo: document.body,
    global: { plugins: [pinia] },
  })
  return wrapper
}

function button(w: VueWrapper, label: string) {
  return w.findAll('button').find(b => b.text() === label)
}

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  terminal.open.mockReset().mockResolvedValue(undefined)
  terminal.selectSession.mockReset().mockReturnValue(true)
  writeClipboardMock.mockReset().mockResolvedValue(true)
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.restoreAllMocks()
})

describe('DeviceCodeBody while waiting for the code', () => {
  it('shows the code and an Open github.com link', () => {
    const w = mountBody(action())
    const group = w.get('[aria-label="One-time code"]')
    expect(group.text()).toContain('C891-07AA')
    const link = w.findAll('a').find(a => a.text() === 'Open github.com')
    expect(link).toBeDefined()
    expect(link!.attributes('href')).toBe('https://github.com/login/device')
    expect(link!.attributes('target')).toBe('_blank')
    expect(link!.attributes('rel')).toContain('noopener')
    expect(button(w, 'Show sign-in terminal')).toBeDefined()
  })

  it('links az to its own host', () => {
    const w = mountBody(action({
      params: { tool: 'az', session_id: 's1', code: 'ABCD2345', url: 'https://microsoft.com/devicelogin' },
    }))
    const link = w.findAll('a').find(a => a.text() === 'Open microsoft.com')
    expect(link?.attributes('href')).toBe('https://microsoft.com/devicelogin')
  })

  it('copy writes the code', async () => {
    const w = mountBody(action())
    await button(w, 'Copy')!.trigger('click')
    await flushPromises()
    expect(writeClipboardMock).toHaveBeenCalledWith('C891-07AA')
    expect(button(w, 'Copied')).toBeDefined()
  })

  it('reports a copy that failed', async () => {
    writeClipboardMock.mockResolvedValue(false)
    const w = mountBody(action())
    await button(w, 'Copy')!.trigger('click')
    await flushPromises()
    expect(w.emitted('error')!.at(-1)).toEqual(['The code could not be copied. Select it and copy it by hand.'])
  })

  it('shows no link for a javascript url, and keeps the code and terminal button', () => {
    const w = mountBody(action({
      params: { tool: 'gh', session_id: 's1', code: 'C891-07AA', url: 'javascript:alert(1)' },
    }))
    expect(w.find('a[href]').exists()).toBe(false)
    expect(w.get('[aria-label="One-time code"]').text()).toContain('C891-07AA')
    expect(button(w, 'Show sign-in terminal')).toBeDefined()
  })

  it('shows no code block for a malformed code', () => {
    const w = mountBody(action({
      params: { tool: 'gh', session_id: 's1', code: '<b>', url: 'https://github.com/login/device' },
    }))
    expect(w.find('[aria-label="One-time code"]').exists()).toBe(false)
    expect(w.find('b').exists()).toBe(false)
    expect(button(w, 'Copy')).toBeUndefined()
  })
})

describe('DeviceCodeBody before and during the start', () => {
  it('open offers Start sign-in, which posts an empty body', async () => {
    const w = mountBody(action({ state: 'open', params: { tool: 'gh' } }))
    const store = useProjectStore()
    const spy = vi.spyOn(store, 'startChatAction').mockResolvedValue({ ...action(), state: 'working' })
    await button(w, 'Start sign-in')!.trigger('click')
    await flushPromises()
    expect(spy).toHaveBeenCalledWith('c1', 'a1', {})
    expect(w.find('a[href]').exists()).toBe(false)
    expect(button(w, 'Copy')).toBeUndefined()
    expect(button(w, 'Show sign-in terminal')).toBeUndefined()
  })

  it('failed offers Try again', () => {
    const w = mountBody(action({ state: 'failed', error: 'x', params: { tool: 'gh' } }))
    expect(button(w, 'Try again')).toBeDefined()
  })

  it('reports a start that failed', async () => {
    const w = mountBody(action({ state: 'open', params: { tool: 'gh' } }))
    vi.spyOn(useProjectStore(), 'startChatAction').mockRejectedValue(new Error('boom'))
    await button(w, 'Start sign-in')!.trigger('click')
    await flushPromises()
    expect(w.emitted('error')!.at(-1)).toEqual(['The sign-in could not be started. Try again.'])
  })

  it('shows only the terminal button while working without a code', () => {
    const w = mountBody(action({
      state: 'working',
      params: { tool: 'gh', session_id: 's1', code: '', url: '' },
    }))
    expect(button(w, 'Show sign-in terminal')).toBeDefined()
    expect(button(w, 'Start sign-in')).toBeUndefined()
    expect(button(w, 'Copy')).toBeUndefined()
    expect(w.find('a').exists()).toBe(false)
    expect(w.find('[aria-label="One-time code"]').exists()).toBe(false)
  })

  it('renders nothing when done', () => {
    const w = mountBody(action({ state: 'done' }))
    expect(w.findAll('button, a').length).toBe(0)
  })
})
