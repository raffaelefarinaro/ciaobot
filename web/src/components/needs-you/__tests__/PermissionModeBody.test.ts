// @vitest-environment jsdom

/**
 * `components/needs-you/PermissionModeBody.vue`: the body of a permission_mode
 * card. It shows the mode now and the mode asked for, the agent's reason as
 * plain text, and a warning for Bypass. Start is the only control; it sends an
 * empty body, because the engine applies the mode the request carried.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PermissionModeBody from '../PermissionModeBody.vue'
import { useProjectStore } from '../../../stores/projects'
import type { ChatAction, ChatInfo } from '../../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() },
}))

const CHAT_ID = 'chat-1'

function action(over: Partial<ChatAction> = {}): ChatAction {
  return {
    id: 'a1',
    kind: 'permission_mode',
    origin: 'agent',
    state: 'open',
    params: { mode: 'plan' },
    error: '',
    created_at: 1,
    ...over,
  }
}

function chat(mode: string): ChatInfo {
  return {
    chat_id: CHAT_ID,
    project_id: 'p1',
    title: 'T',
    model: 'sonnet',
    provider: 'claude',
    mode,
    session_id: '',
    created_at: '',
    archived: false,
  } as ChatInfo
}

let wrapper: VueWrapper | null = null

function mountBody(props: { action?: ChatAction; mode?: string } = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.chats = [chat(props.mode ?? 'auto')]
  wrapper = mount(PermissionModeBody, {
    props: { chatId: CHAT_ID, action: props.action ?? action() },
    attachTo: document.body,
    global: { plugins: [pinia] },
  })
  return { wrapper, store }
}

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  apiGet.mockResolvedValue({})
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.restoreAllMocks()
})

describe('PermissionModeBody', () => {
  it('names the mode now and the mode asked for', () => {
    const { wrapper } = mountBody({ mode: 'auto' })
    const text = wrapper.text()
    expect(text).toContain('Now')
    expect(text).toContain('Auto — allow safe work, ask on risky')
    expect(text).toContain('Requested')
    expect(text).toContain('Plan — read and propose only, change nothing')
  })

  it('shows the agent reason as text, never as markup', () => {
    const { wrapper } = mountBody({ action: action({ params: { mode: 'plan', note: '<b>x</b>' } }) })
    expect(wrapper.html()).toContain('&lt;b&gt;x&lt;/b&gt;')
    expect(wrapper.find('b').exists()).toBe(false)
  })

  it('Start calls startChatAction with the chat, the action and an empty body', async () => {
    const { wrapper, store } = mountBody()
    const spy = vi.spyOn(store, 'startChatAction').mockResolvedValue({ ...action(), state: 'done' })
    const button = wrapper.findAll('button').find(b => b.text() === 'Switch to Plan')
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()
    expect(spy).toHaveBeenCalledWith(CHAT_ID, 'a1', {})
  })

  it('warns before Bypass runs without asking', () => {
    const { wrapper } = mountBody({ action: action({ params: { mode: 'bypass' } }) })
    const warning = wrapper.get('.needs-you-perm-warn')
    expect(warning.text()).toBe('Every command and file edit runs without asking you first.')
  })

  it('shows no warning for a mode that is not Bypass', () => {
    const { wrapper } = mountBody({ action: action({ params: { mode: 'normal' } }) })
    expect(wrapper.find('.needs-you-perm-warn').exists()).toBe(false)
  })
})
