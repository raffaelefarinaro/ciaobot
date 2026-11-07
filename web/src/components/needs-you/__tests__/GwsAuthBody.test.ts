// @vitest-environment jsdom

/**
 * `components/needs-you/GwsAuthBody.vue`: the Google sign-in card. On the engine's
 * computer Start opens the consent page in a new tab; elsewhere the consent link
 * and the paste-back field are shown, and a pasted address goes to the callback route.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import GwsAuthBody from '../GwsAuthBody.vue'
import type { ChatAction } from '../../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const loopback = vi.hoisted(() => ({ page: false }))

vi.mock('../../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() },
}))
vi.mock('../../../lib/loopback', () => ({ isLoopbackPage: () => loopback.page, isLoopbackHostname: () => false }))

const CHAT_ID = 'chat-1'
const GOOGLE = 'https://accounts.google.com/o/oauth2/auth?x=1'

function action(over: Partial<ChatAction> = {}): ChatAction {
  return {
    id: 'a1',
    kind: 'gws_auth',
    origin: 'engine',
    state: 'open',
    params: { profile: 'work' },
    error: '',
    created_at: 1,
    ...over,
  }
}

let wrapper: VueWrapper | null = null

function mountBody(a: ChatAction) {
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(GwsAuthBody, {
    props: { chatId: CHAT_ID, action: a },
    attachTo: document.body,
    global: { plugins: [pinia], stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
  return wrapper
}

function primaryButton(w: VueWrapper) {
  return w.findAll('button').find(b => b.text() === 'Sign in with Google' || b.text() === 'Starting…')
}

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  loopback.page = false
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.restoreAllMocks()
})

describe('GwsAuthBody', () => {
  it('opens a placeholder tab before the start resolves on the host', async () => {
    loopback.page = true
    const tab = { opener: {} as unknown, location: { href: '' }, close: vi.fn() }
    const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    let resolve!: (value: unknown) => void
    apiPost.mockReturnValueOnce(new Promise(r => { resolve = r }))
    const w = mountBody(action())

    await primaryButton(w)!.trigger('click')

    expect(open).toHaveBeenCalledWith('', '_blank')
    resolve({ action: action({ state: 'waiting', params: { profile: 'work', url: GOOGLE } }) })
    await flushPromises()
    expect(tab.location.href).toBe(GOOGLE)
    expect(tab.opener).toBeNull()
  })

  it('closes the placeholder when the start fails', async () => {
    loopback.page = true
    const tab = { opener: {} as unknown, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    apiPost.mockResolvedValueOnce({ action: action({ state: 'failed', error: 'no client' }) })
    const w = mountBody(action())

    await primaryButton(w)!.trigger('click')
    await flushPromises()

    expect(tab.close).toHaveBeenCalledTimes(1)
  })

  it('never opens a tab off the host', async () => {
    loopback.page = false
    const open = vi.spyOn(window, 'open')
    apiPost.mockResolvedValueOnce({ action: action({ state: 'waiting', params: { profile: 'work', url: GOOGLE } }) })
    const w = mountBody(action())

    await primaryButton(w)!.trigger('click')
    await flushPromises()

    expect(open).not.toHaveBeenCalled()
  })

  it('shows the paste field off the host while waiting', () => {
    loopback.page = false
    const w = mountBody(action({ state: 'waiting', params: { profile: 'work', url: GOOGLE } }))

    expect(w.find('input[type="url"]').exists()).toBe(true)
    expect(w.findAll('button').some(b => b.text() === 'Finish sign-in')).toBe(true)
    expect(w.text()).toContain('Copy its full address')
    const link = w.find(`a[href="${GOOGLE}"]`)
    expect(link.exists()).toBe(true)
    expect(link.text()).toBe('Open Google sign-in')
  })

  it('posts a pasted address to the callback route', async () => {
    loopback.page = false
    apiPost.mockResolvedValueOnce({ action: action({ state: 'done', params: { profile: 'work', url: GOOGLE } }) })
    const w = mountBody(action({ state: 'waiting', params: { profile: 'work', url: GOOGLE } }))
    const pasted = 'http://127.0.0.1:5555/?code=a&state=b'

    await w.find('input[type="url"]').setValue(pasted)
    await w.find('form').trigger('submit')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/chats/chat-1/actions/a1/callback', { url: pasted })
  })

  it('hides the paste field on the host', () => {
    loopback.page = true
    const w = mountBody(action({ state: 'waiting', params: { profile: 'work', url: GOOGLE } }))

    expect(w.findAll('button').some(b => b.text() === 'Finish sign-in')).toBe(false)
    expect(w.find('input[type="url"]').exists()).toBe(false)
  })

  it('renders no link for a non-Google url', () => {
    loopback.page = false
    const w = mountBody(action({ state: 'waiting', params: { profile: 'work', url: 'https://evil.io' } }))

    expect(w.find('a[href="https://evil.io"]').exists()).toBe(false)
  })

  it('links to manual connect for a web-app client', () => {
    const webApp = mountBody(action({
      state: 'failed',
      error: 'This OAuth client is a web app and does not support one-click sign-in.',
    }))
    expect(webApp.text()).toContain('Use manual connect in Settings')
    wrapper?.unmount()

    const other = mountBody(action({ state: 'failed', error: 'Google returned error: access_denied' }))
    expect(other.text()).not.toContain('Use manual connect in Settings')
  })
})
