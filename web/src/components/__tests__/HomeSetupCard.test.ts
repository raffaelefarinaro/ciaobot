// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub, type VueWrapper } from '@vue/test-utils'
import HomeSetupCard from '../HomeSetupCard.vue'
import { enablePush } from '../../lib/push'
import { SETUP_CARD_DISMISSED_KEY } from '../../lib/pwaPlatform'
import { resetHomeNoticeWindows } from '../../composables/useHomeNoticeWindows'
import {
  _resetInstallPromptForTests,
  canPromptInstall,
  installedOnDevice,
  listenForInstallPrompt,
} from '../../lib/installPrompt'

// Hoisted so the mocked factories can read them: a mock factory runs while the
// test file's imports resolve, before the module body has run.
const state = vi.hoisted(() => ({ pushEnabled: false, pushSupported: true, loopback: true }))

vi.mock('../../lib/push', () => ({
  pushSupported: () => state.pushSupported,
  isPushEnabled: async () => state.pushEnabled,
  enablePush: vi.fn(),
}))

vi.mock('../../lib/loopback', () => ({ isLoopbackPage: () => state.loopback }))

const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'

let wrapper: VueWrapper | null = null

async function mountCard() {
  wrapper = mount(HomeSetupCard, { global: { stubs: { RouterLink: RouterLinkStub } } })
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

/** The one navigation link that points at `to`, wherever the card put it. */
function linkTo(view: VueWrapper, to: string) {
  const match = view.findAllComponents(RouterLinkStub).find((l) => l.props('to') === to)
  if (!match) throw new Error(`no link to ${to}`)
  return match
}

beforeEach(() => {
  vi.clearAllMocks()
  state.pushEnabled = false
  state.pushSupported = true
  state.loopback = true
  localStorage.clear()
  resetHomeNoticeWindows()
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
    expect(view.getComponent(RouterLinkStub).props('to')).toBe('/settings#install-app')
    expect(hasButton(view, 'Enable')).toBe(true)
  })

  it('uses quiet row labels for the three setup tasks', async () => {
    const view = await mountCard()

    // A task name, not a heading: one plain label per row.
    expect(view.findAll('.home-setup-label').map(l => l.text())).toEqual([
      'Install the app',
      'Turn on notifications',
      'Use on your phone',
    ])
    expect(view.findAll('.home-setup-steps strong')).toHaveLength(0)
  })

  it('places install help in the action column as a real link', async () => {
    const view = await mountCard()

    const help = linkTo(view, '/settings#install-app')
    const row = view.find('.home-setup-step')
    const hint = row.find('.hint')
    expect(hint.exists()).toBe(true)
    // The help sits in the row's action column, not inside the hint text.
    expect(hint.element.contains(help.element)).toBe(false)
    expect(help.element.parentElement).toBe(row.element)
    // A navigation link, dressed as a quiet bordered action: never a primary.
    expect(help.classes()).toContain('home-setup-action')
    expect(help.classes()).not.toContain('btn-primary')
  })

  it('keeps notification enable primary and help neutral', async () => {
    const view = await mountCard()

    expect(button(view, 'Enable').classes()).toContain('btn-primary')
    for (const to of ['/settings#install-app', '/settings#other-devices']) {
      const help = linkTo(view, to)
      expect(help.classes(), to).toContain('home-setup-action')
      expect(help.classes(), to).not.toContain('btn-primary')
    }
  })

  it('says the app is already installed when Chromium reports it to a tab', async () => {
    installedOnDevice.value = true
    state.pushEnabled = true
    const view = await mountCard()

    // Install done and push on: nothing left to set up on this device.
    expect(view.text()).not.toContain('Set up this device')

    state.pushEnabled = false
    view.unmount()
    const fresh = await mountCard()
    expect(fresh.text()).toContain('Installed on this device')
    expect(fresh.findAllComponents(RouterLinkStub).some(l => l.props('to') === '/settings#install-app')).toBe(false)
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
    expect(view.findAllComponents(RouterLinkStub).some(l => l.props('to') === '/settings#install-app')).toBe(false)

    await button(view, 'Install').trigger('click')
    await flushPromises()

    expect(event.prompt).toHaveBeenCalledTimes(1)
    expect(view.text()).toContain('Installed')
  })

  it('enables notifications', async () => {
    const view = await mountCard()

    await button(view, 'Enable').trigger('click')
    await flushPromises()
    expect(enablePush).toHaveBeenCalled()
    expect(view.text()).toContain('On for this device')
  })

  it('stays visible after enabling in the installed app so the confirmation shows', async () => {
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

    // Enabling is what completes the card, so unmounting here would pull it
    // out from under the click in the installed app and on the iOS Home Screen.
    expect(view.find('.home-setup').exists()).toBe(true)
    expect(view.text()).toContain('On for this device')
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

  it('closing the setup window persists per browser without a second Hide button', async () => {
    const view = await mountCard()

    expect(hasButton(view, 'Hide on this device')).toBe(false)
    await view.get('[aria-label="Dismiss setup reminder on this device"]').trigger('click')
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
    wrapper?.unmount()
    expect((await mountCard()).find('.home-setup').exists()).toBe(false)
  })

  it('does not reopen a dismissed setup window along with transient Home notices', async () => {
    const view = await mountCard()
    await view.get('[aria-label="Dismiss setup reminder on this device"]').trigger('click')
    await flushPromises()
    expect(view.find('.home-setup').exists()).toBe(false)
    expect(localStorage.getItem(SETUP_CARD_DISMISSED_KEY)).toBe('1')
    resetHomeNoticeWindows()
    await flushPromises()
    expect(view.find('.home-setup').exists()).toBe(false)
  })

  it('does not flash for a set-up device before the push probe resolves', async () => {
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

    wrapper = mount(HomeSetupCard, { global: { stubs: { RouterLink: RouterLinkStub } } })
    // `pushOn` is only known after the probe, and Home remounts on every
    // return from a chat, so a first frame that guesses wrong would make the
    // recent-chats list jump under the user's finger.
    expect(wrapper.find('.home-setup').exists()).toBe(false)

    await flushPromises()
    expect(wrapper.find('.home-setup').exists()).toBe(false)
  })

  it('hides once installed when this browser has no push', async () => {
    // Without a PushManager there is no second step, so "installed and on" is
    // the only way to be done, and this device can never get there.
    state.pushSupported = false
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

  it('stays hidden in an installed app whose notifications are blocked', async () => {
    // Nothing on this card can lift a browser-level block, so coming back on
    // every Home visit is nagging, not guidance; the Settings notifications
    // card is where the block gets explained.
    vi.stubGlobal('Notification', { permission: 'denied' })
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

  it('asks iOS users to install first', async () => {
    Object.defineProperty(navigator, 'userAgent', { value: IPHONE_UA, configurable: true })
    const view = await mountCard()

    expect(view.text()).toContain('Install the app first')
    // Enabling from a Safari tab on iOS silently never delivers anything.
    expect(hasButton(view, 'Enable')).toBe(false)
  })

  it('shows the phone setup row on the computer running Ciaobot', async () => {
    const view = await mountCard()

    expect(view.text()).toContain('Use on your phone')
    expect(view.text()).toContain('Install Tailscale')
    expect(view.text()).toContain('Add to Home Screen')
    expect(
      view.findAllComponents(RouterLinkStub).some(l => l.props('to') === '/settings#other-devices'),
    ).toBe(true)
  })

  it('keeps phone instructions behind a closed disclosure', async () => {
    const view = await mountCard()

    const disclosure = view.find('details.home-setup-phone')
    expect(disclosure.exists()).toBe(true)
    expect(disclosure.attributes('open')).toBeUndefined()
    // The row still says what it is; the instructions wait behind the summary.
    expect(disclosure.find('summary').text()).toBe('Use on your phone')
    expect(view.text()).toContain('Install Tailscale')
    expect(view.text()).toContain('Make Ciaobot reachable from the phone')
    expect(view.text()).toContain('Add to Home Screen')
    const phone = linkTo(view, '/settings#other-devices')
    expect(phone.element.closest('details')?.classList.contains('home-setup-phone')).toBe(true)
  })

  it('hides the phone setup row on a remote browser', async () => {
    state.loopback = false
    const view = await mountCard()

    expect(view.text()).toContain('Set up this device')
    expect(view.text()).not.toContain('Use on your phone')
  })

  it('hides the phone setup row on a phone', async () => {
    const original = navigator.userAgent
    Object.defineProperty(navigator, 'userAgent', { value: IPHONE_UA, configurable: true })
    try {
      const view = await mountCard()
      expect(view.text()).not.toContain('Use on your phone')
    } finally {
      Object.defineProperty(navigator, 'userAgent', { value: original, configurable: true })
    }
  })

  it('the phone row does not keep a finished card up', async () => {
    installedOnDevice.value = true
    state.pushEnabled = true
    const view = await mountCard()

    expect(view.find('.home-setup').exists()).toBe(false)
  })
})
