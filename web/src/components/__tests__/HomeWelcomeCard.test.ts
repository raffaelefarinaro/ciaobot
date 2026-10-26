// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub, type VueWrapper } from '@vue/test-utils'
import HomeWelcomeCard from '../HomeWelcomeCard.vue'
import { api } from '../../lib/api'
import { writeClipboard } from '../../lib/codeCopy'
import { resetWelcomeForTests } from '../../composables/useWelcome'
import { resetHomeNoticeWindows, useHomeNoticeWindows } from '../../composables/useHomeNoticeWindows'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('../../lib/codeCopy', async (orig) => ({
  ...(await orig<typeof import('../../lib/codeCopy')>()),
  writeClipboard: vi.fn(async () => true),
}))

/** Same shape as lib/api's ApiError (not exported): a message and a status. */
function apiError(message: string, status: number): Error {
  return Object.assign(new Error(message), { status })
}

const get = vi.mocked(api.get)
const post = vi.mocked(api.post)

function pending(over: Record<string, unknown> = {}) {
  return {
    pending: true,
    workspace_root: '/Users/a/Ciaobot',
    provider: 'claude',
    provider_label: 'Claude Code',
    on_host: true,
    platform: 'darwin',
    ...over,
  }
}

function answer(status: unknown) {
  get.mockImplementation(async (path: string) => {
    if (path === '/api/welcome') return status
    if (path === '/api/welcome/password') return { password: 'p4ss-w0rd' }
    return {}
  })
}

let wrapper: VueWrapper | null = null

async function mountCard() {
  wrapper = mount(HomeWelcomeCard, { global: { stubs: { RouterLink: RouterLinkStub } } })
  await flushPromises()
  return wrapper
}

function button(view: VueWrapper, label: string) {
  const match = view.findAll('button').find((b) => b.text() === label)
  if (!match) throw new Error(`no button labelled ${label}`)
  return match
}

function hasButton(view: VueWrapper, label: string): boolean {
  return view.findAll('button').some((b) => b.text() === label)
}

beforeEach(() => {
  vi.clearAllMocks()
  post.mockResolvedValue({ ok: true })
  resetWelcomeForTests()
  resetHomeNoticeWindows()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('HomeWelcomeCard', () => {
  it('renders nothing when nothing is pending', async () => {
    answer({ pending: false })
    const view = await mountCard()
    expect(view.text()).not.toContain('Your Ciaobot')
  })

  it('renders nothing for a fixture-style empty object', async () => {
    answer({})
    const view = await mountCard()
    expect(view.text()).not.toContain('Your Ciaobot')
  })

  it('shows folder, masked password and AI tool on the host', async () => {
    answer(pending())
    const view = await mountCard()
    const text = view.text()
    expect(text).toContain('/Users/a/Ciaobot')
    expect(text).toContain('Show in Finder')
    expect(text).toContain('••••••••••••')
    expect(text).toContain('Claude Code')
    expect(text).toContain('Save it in your password manager')
    expect(get.mock.calls.map((c) => c[0])).toEqual(['/api/welcome'])
  })

  it('Show fetches and reveals the password, Hide masks it', async () => {
    answer(pending())
    const view = await mountCard()
    await button(view, 'Show').trigger('click')
    await flushPromises()
    expect(view.text()).toContain('p4ss-w0rd')
    expect(button(view, 'Hide').attributes('aria-pressed')).toBe('true')
    await button(view, 'Hide').trigger('click')
    await flushPromises()
    expect(view.text()).not.toContain('p4ss-w0rd')
  })

  it('Copy writes the password to the clipboard', async () => {
    answer(pending())
    const view = await mountCard()
    await button(view, 'Copy').trigger('click')
    await flushPromises()
    expect(writeClipboard).toHaveBeenCalledWith('p4ss-w0rd')
    expect(hasButton(view, 'Copied')).toBe(true)
  })

  it('a remote browser gets no password controls', async () => {
    answer(pending({ on_host: false }))
    const view = await mountCard()
    expect(hasButton(view, 'Show')).toBe(false)
    expect(hasButton(view, 'Copy')).toBe(false)
    expect(hasButton(view, 'Show in Finder')).toBe(false)
    expect(view.text()).toContain('The password is shown on the computer running Ciaobot.')
    expect(get.mock.calls.map((c) => c[0])).not.toContain('/api/welcome/password')
  })

  it('folder label follows the platform', async () => {
    answer(pending({ platform: 'win32' }))
    let view = await mountCard()
    expect(hasButton(view, 'Show in File Explorer')).toBe(true)
    view.unmount()
    wrapper = null
    resetWelcomeForTests()
    answer(pending({ platform: 'linux' }))
    view = await mountCard()
    await button(view, 'Open folder').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/welcome/open-folder', {})
  })

  it('closing dismisses for every device and does not come back with Show closed notices', async () => {
    answer(pending())
    const view = await mountCard()
    await view.find('button[aria-label="Dismiss this card on every device"]').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/welcome/dismiss', {})
    expect(view.text()).not.toContain('Your Ciaobot')
    useHomeNoticeWindows().reopenAll()
    await flushPromises()
    expect(view.text()).not.toContain('Your Ciaobot')
    view.unmount()
    wrapper = null
    get.mockClear()
    const again = await mountCard()
    expect(get).not.toHaveBeenCalled()
    expect(again.text()).not.toContain('Your Ciaobot')
  })

  it('links Providers to the providers tab', async () => {
    answer(pending())
    const view = await mountCard()
    const link = view.findAllComponents(RouterLinkStub).find((l) => l.text() === 'Providers')
    expect(link?.props('to')).toBe('/settings/providers#chat-providers')
  })

  it('hides the card without an error when the password route answers 404', async () => {
    answer(pending())
    const view = await mountCard()
    get.mockImplementation(async (path: string) => {
      if (path === '/api/welcome/password') throw apiError('nothing to show', 404)
      return {}
    })
    await button(view, 'Show').trigger('click')
    await flushPromises()
    expect(view.text()).not.toContain('Your Ciaobot')
    expect(view.text()).not.toContain('nothing to show')
  })

  it('shows a generic error when the password cannot be fetched, the server text for not_on_host', async () => {
    answer(pending())
    const view = await mountCard()
    get.mockImplementation(async () => { throw new TypeError('Failed to fetch') })
    await button(view, 'Show').trigger('click')
    await flushPromises()
    expect(view.find('[role="alert"]').text()).toBe('Could not get the password. Check that Ciaobot is running and try again.')
    get.mockImplementation(async () => {
      throw apiError('The password is shown on the computer running Ciaobot.', 403)
    })
    await button(view, 'Show').trigger('click')
    await flushPromises()
    expect(view.find('[role="alert"]').text()).toBe('The password is shown on the computer running Ciaobot.')
  })

  it('discards a password that arrives after Close', async () => {
    answer(pending())
    const view = await mountCard()
    let release: (v: { password: string }) => void = () => {}
    get.mockImplementation(() => new Promise((r) => { release = r as typeof release }))
    await button(view, 'Copy').trigger('click')
    await view.find('button[aria-label="Dismiss this card on every device"]').trigger('click')
    await flushPromises()
    release({ password: 'p4ss-w0rd' })
    await flushPromises()
    expect(writeClipboard).not.toHaveBeenCalled()
    expect(view.text()).not.toContain('p4ss-w0rd')
    expect(view.html()).not.toContain('p4ss-w0rd')
  })

  it('discards a password that arrives after unmount', async () => {
    answer(pending())
    const view = await mountCard()
    let release: (v: { password: string }) => void = () => {}
    get.mockImplementation(() => new Promise((r) => { release = r as typeof release }))
    await button(view, 'Copy').trigger('click')
    view.unmount()
    wrapper = null
    release({ password: 'p4ss-w0rd' })
    await flushPromises()
    expect(writeClipboard).not.toHaveBeenCalled()
  })

  it('a re-shown card needs a new fetch after the password was hidden by a remount', async () => {
    answer(pending())
    let view = await mountCard()
    await button(view, 'Show').trigger('click')
    await flushPromises()
    expect(view.text()).toContain('p4ss-w0rd')
    view.unmount()
    wrapper = null
    view = await mountCard()
    expect(view.text()).not.toContain('p4ss-w0rd')
    get.mockClear()
    await button(view, 'Show').trigger('click')
    await flushPromises()
    expect(get.mock.calls.map((c) => c[0])).toEqual(['/api/welcome/password'])
  })

  it('shows the copy failure alert when the clipboard refuses', async () => {
    answer(pending())
    vi.mocked(writeClipboard).mockResolvedValueOnce(false)
    const view = await mountCard()
    await button(view, 'Copy').trigger('click')
    await flushPromises()
    expect(view.find('[role="alert"]').text()).toBe('Could not copy. Select the password and copy it instead.')
    expect(hasButton(view, 'Copied')).toBe(false)
  })

  it('masks with hidden bullets and a screen-reader label', async () => {
    answer(pending())
    const view = await mountCard()
    expect(view.find('.home-welcome-secret [aria-hidden="true"]').text()).toBe('••••••••••••')
    expect(view.find('.home-welcome-secret .sr-only').text()).toBe('Password hidden')
    expect(view.find('.home-welcome-secret').attributes('aria-label')).toBeUndefined()
  })
})
