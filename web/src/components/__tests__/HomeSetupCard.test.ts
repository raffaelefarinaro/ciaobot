// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import HomeSetupCard from '../HomeSetupCard.vue'
import { enablePush, sendTestNotification } from '../../lib/push'
import { SETUP_CARD_DISMISSED_KEY } from '../../lib/pwaPlatform'
import {
  _resetInstallPromptForTests,
  canPromptInstall,
  listenForInstallPrompt,
} from '../../lib/installPrompt'

// Hoisted so the mocked factories can read them: a mock factory runs while the
// test file's imports resolve, before the module body has run.
const state = vi.hoisted(() => ({ pushEnabled: false, desktopApp: false }))

vi.mock('../../lib/push', () => ({
  pushSupported: () => true,
  isPushEnabled: async () => state.pushEnabled,
  enablePush: vi.fn(),
  sendTestNotification: vi.fn(async () => true),
}))

vi.mock('../../lib/desktop', () => ({
  isDesktopApp: () => state.desktopApp,
}))

const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'

let wrapper: VueWrapper | null = null

async function mountCard() {
  wrapper = mount(HomeSetupCard)
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
  state.pushEnabled = false
  state.desktopApp = false
  localStorage.clear()
  _resetInstallPromptForTests()
  listenForInstallPrompt(window)
  // jsdom has no matchMedia at all, so a stub left by an earlier test would
  // otherwise leak a "standalone" answer into the next one.
  delete (window as unknown as Record<string, unknown>).matchMedia
  Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
  vi.stubGlobal('Notification', { permission: 'default' })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  _resetInstallPromptForTests()
  delete (navigator as unknown as Record<string, unknown>).userAgent
})

describe('HomeSetupCard', () => {
  it('shows install instructions and an Enable button on a fresh browser', async () => {
    const view = await mountCard()

    expect(view.text()).toContain('Set up this device')
    expect(view.text()).toContain('Install the app')
    // No programmatic prompt in this browser, so it has to say where the
    // control lives instead of showing a button that cannot work.
    expect(view.text()).toContain('Install option in the address bar or menu')
    expect(hasButton(view, 'Enable')).toBe(true)
  })

  it('shows the native Install button when the browser offered a prompt', async () => {
    const event = Object.assign(
      new Event('beforeinstallprompt', { cancelable: true }),
      { prompt: vi.fn(async () => {}), userChoice: Promise.resolve({ outcome: 'accepted' }) },
    )
    window.dispatchEvent(event)
    expect(canPromptInstall.value).toBe(true)

    const view = await mountCard()
    expect(hasButton(view, 'Install')).toBe(true)
    // Instructions are for the browsers without a prompt; this one has one.
    expect(view.text()).not.toContain('Install option in the address bar or menu')

    await button(view, 'Install').trigger('click')
    await flushPromises()

    expect(event.prompt).toHaveBeenCalledTimes(1)
    expect(view.text()).toContain('Installed')
  })

  it('enables notifications and then sends a test', async () => {
    const view = await mountCard()

    await button(view, 'Enable').trigger('click')
    await flushPromises()
    expect(enablePush).toHaveBeenCalled()

    const send = button(view, 'Send test')
    expect(send.attributes('disabled')).toBeUndefined()
    await send.trigger('click')
    await flushPromises()

    expect(sendTestNotification).toHaveBeenCalled()
    expect(view.text()).toContain('Sent.')
  })

  it('stays visible after enabling in the installed app so the test can be sent', async () => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (query: string) => ({
        matches: query.includes('standalone'),
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      }),
    })
    const view = await mountCard()

    await button(view, 'Enable').trigger('click')
    await flushPromises()

    // Enabling is what completes the card, so unmounting here would take the
    // test step away in the installed app and on the iOS Home Screen.
    expect(view.find('.home-setup').exists()).toBe(true)
    const send = button(view, 'Send test')
    expect(send.attributes('disabled')).toBeUndefined()
    await send.trigger('click')
    await flushPromises()

    expect(view.text()).toContain('Sent.')
  })

  it('keeps Send test disabled until notifications are on', async () => {
    const view = await mountCard()
    expect(button(view, 'Send test').attributes('disabled')).toBeDefined()
  })

  it('explains a blocked permission instead of offering Enable', async () => {
    vi.stubGlobal('Notification', { permission: 'denied' })
    const view = await mountCard()

    expect(view.text()).toContain('Blocked. Allow notifications for this site')
    expect(hasButton(view, 'Enable')).toBe(false)
  })

  it('explains the block after the user denies the prompt', async () => {
    vi.mocked(enablePush).mockImplementationOnce(async () => {
      vi.stubGlobal('Notification', { permission: 'denied' })
      throw new Error('Notification permission denied')
    })
    const view = await mountCard()
    expect(hasButton(view, 'Enable')).toBe(true)

    await button(view, 'Enable').trigger('click')
    await flushPromises()

    // Only `permission` moved; nothing re-read it, so Enable used to linger.
    expect(view.text()).toContain('Blocked. Allow notifications for this site')
    expect(hasButton(view, 'Enable')).toBe(false)
  })

  it('Hide persists per browser', async () => {
    const view = await mountCard()

    await button(view, 'Hide').trigger('click')
    await flushPromises()

    expect(view.find('.home-setup').exists()).toBe(false)
    expect(localStorage.getItem(SETUP_CARD_DISMISSED_KEY)).toBe('1')

    wrapper?.unmount()
    expect((await mountCard()).find('.home-setup').exists()).toBe(false)
  })

  it('renders nothing when installed and notifications are on', async () => {
    state.pushEnabled = true
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (query: string) => ({
        matches: query.includes('standalone'),
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      }),
    })

    const view = await mountCard()
    expect(view.find('.home-setup').exists()).toBe(false)
  })

  it('renders nothing on an insecure origin', async () => {
    Object.defineProperty(window, 'isSecureContext', { value: false, configurable: true })
    const view = await mountCard()
    expect(view.find('.home-setup').exists()).toBe(false)
  })

  it('renders nothing inside Ciaobot.app', async () => {
    state.desktopApp = true
    const view = await mountCard()
    expect(view.find('.home-setup').exists()).toBe(false)
  })

  it('asks iOS users to install first', async () => {
    Object.defineProperty(navigator, 'userAgent', { value: IPHONE_UA, configurable: true })
    const view = await mountCard()

    expect(view.text()).toContain('Install the app first')
    // Enabling from a Safari tab on iOS silently never delivers anything.
    expect(hasButton(view, 'Enable')).toBe(false)
  })
})
