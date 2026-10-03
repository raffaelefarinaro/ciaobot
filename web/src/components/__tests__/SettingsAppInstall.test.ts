// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import SettingsAppInstall from '../settings/SettingsAppInstall.vue'
import { SETUP_CARD_DISMISSED_KEY } from '../../lib/pwaPlatform'

const MAC_SAFARI_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
const MAC_CHROME_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
const WINDOWS_EDGE_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'
const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
const ANDROID_CHROME_UA =
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
const LINUX_FIREFOX_UA =
  'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0'
const LINUX_CHROME_UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
const MAC_FIREFOX_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0'
const ANDROID_FIREFOX_UA =
  'Mozilla/5.0 (Android 14; Mobile; rv:121.0) Gecko/121.0 Firefox/121.0'
const MAC_EDGE_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'
const WINDOWS_BRAVE_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Brave/1.61.104'

const VENDOR_LINKS = [
  'https://support.apple.com/en-us/104996',
  'https://support.google.com/chrome/answer/9658361?hl=en&co=GENIE.Platform%3DDesktop',
  'https://support.microsoft.com/en-us/edge/install-manage-or-uninstall-apps-in-microsoft-edge',
  'https://support.apple.com/guide/iphone/iphea86e5236/ios',
  'https://support.google.com/chrome/answer/9658361?hl=en&co=GENIE.Platform%3DAndroid',
]

let wrapper: VueWrapper | null = null

/** Every one of these globals is a real platform fact that the card reads, so
 *  each case replaces it and each case puts it back exactly. Values would do for
 *  most of them, but not for all: `location` is an accessor with no setter in
 *  jsdom, `matchMedia` does not exist at all, and both `maxTouchPoints` and the
 *  user agent live on prototypes. Saving the descriptor covers the three cases
 *  and costs the same, and it leaves a real implementation in place afterwards
 *  instead of deleting one the test suite may grow. */
const saved: Array<{ target: object; key: string; descriptor: PropertyDescriptor | undefined }> = []

function override(target: object, key: string, value: unknown) {
  saved.push({ target, key, descriptor: Object.getOwnPropertyDescriptor(target, key) })
  Object.defineProperty(target, key, { value, configurable: true })
}

function restore() {
  for (const { target, key, descriptor } of saved.splice(0)) {
    if (descriptor) Object.defineProperty(target, key, descriptor)
    else delete (target as unknown as Record<string, unknown>)[key]
  }
}

function setUserAgent(ua: string) {
  override(navigator, 'userAgent', ua)
}

/** An iPad in desktop browsing mode is only tellable apart from a Mac by its
 *  touch points; a Mac has no touchscreen. */
function setMaxTouchPoints(points: number) {
  override(navigator, 'maxTouchPoints', points)
}

/** jsdom has no matchMedia at all, so a stub left behind by one case would
 *  hand a "standalone" answer to the next one. */
function setStandalone(standalone: boolean) {
  override(window, 'matchMedia', (query: string) => ({
    matches: standalone && query.includes('standalone'),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }))
}

function setSecureContext(secure: boolean) {
  override(window, 'isSecureContext', secure)
}

/** jsdom serves the page from localhost; a LAN address is what the plain-HTTP
 *  warning is about. */
function setLocation(location: { hostname: string; protocol: string }) {
  const href = `${location.protocol}//${location.hostname}/settings`
  override(window, 'location', { ...window.location, href, ...location })
}

function mountCard() {
  wrapper = mount(SettingsAppInstall, { attachTo: document.body })
  return wrapper
}

/** The rows carrying the "Your browser" badge. Zero is the answer a browser the
 *  card cannot place should get; one row is the answer it should get when it
 *  can, and it must never be two. */
function currentRouteNames(view: VueWrapper) {
  return view
    .findAll('.install-route')
    .filter(r => r.attributes('data-current') === 'true')
    .map(r => r.get('h3').text())
}

beforeEach(() => {
  localStorage.clear()
  setUserAgent(MAC_CHROME_UA)
  setMaxTouchPoints(0)
  setStandalone(false)
  setSecureContext(true)
  setLocation({ hostname: 'localhost', protocol: 'http:' })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  localStorage.clear()
  restore()
})

describe('SettingsAppInstall', () => {
  it('states that installing is optional and that the browser already works', () => {
    const view = mountCard()

    expect(view.text()).toContain('Use Ciaobot as an app')
    expect(view.text()).toContain('installing is optional')
    expect(view.text()).toContain('The engine on your host does the work and has to stay running')
  })

  it('survives the Home setup reminder being dismissed for good', () => {
    // Home's X persists per browser, and that reminder carries general install
    // instructions too. Settings is the permanent copy, so it must not read
    // that key at all.
    localStorage.setItem(SETUP_CARD_DISMISSED_KEY, '1')

    const view = mountCard()

    expect(view.text()).toContain('Use Ciaobot as an app')
    expect(view.findAll('.install-route')).toHaveLength(5)
  })

  it('reports a browser tab and then the installed app, for this window only', () => {
    const inBrowser = mountCard()
    expect(inBrowser.text()).toContain('A browser tab')
    expect(inBrowser.text()).toContain('Installing is what adds the icon')
    wrapper?.unmount()

    setStandalone(true)
    const installed = mountCard()
    expect(installed.text()).toContain('Running as the installed app')
    // No device-wide claim: a tab cannot see what else is installed.
    expect(installed.text()).toContain('Another tab, or another device, may still be the browser')
  })

  it('carries the five vendor guides, each opening safely in a new tab', () => {
    const view = mountCard()

    const hrefs = view.findAll('a').map(a => a.attributes('href'))
    for (const url of VENDOR_LINKS) expect(hrefs).toContain(url)
    // The remote HTTPS guide the plain-HTTP case points at.
    expect(hrefs).toContain('https://www.raffaelefarinaro.com/ciaobot/remote.html')

    for (const link of view.findAll('a')) {
      expect(link.attributes('target')).toBe('_blank')
      expect(link.attributes('rel')).toBe('noopener noreferrer')
    }
  })

  it('warns on plain HTTP and names localhost and HTTPS on a secure address', () => {
    setSecureContext(false)
    setLocation({ hostname: '192.168.1.20', protocol: 'http:' })
    const lan = mountCard()

    expect(lan.text()).toContain('Plain HTTP on the network')
    expect(lan.text()).toContain('only installs over HTTPS or on the host’s own localhost')
    wrapper?.unmount()

    setSecureContext(true)
    setLocation({ hostname: 'localhost', protocol: 'http:' })
    const loopback = mountCard()
    expect(loopback.text()).toContain('Secure: the host’s own localhost')
    // Secure is what the address offers; it says nothing about this browser.
    expect(loopback.text()).toContain('The address a browser needs before it will offer installing')
    wrapper?.unmount()

    setLocation({ hostname: 'ciao.example.com', protocol: 'https:' })
    const https = mountCard()
    expect(https.text()).toContain('Secure: HTTPS')
    expect(https.text()).toContain('depends on the browser and its version')
  })

  it('never turns a secure origin into a claim about this browser', () => {
    // Firefox on a Mac over HTTPS: the origin half of the deal is satisfied and
    // the browser half is not, because Firefox on macOS has no install action to
    // offer. The card cannot read which browsers can install, so it must not
    // assert that this one can, secure address or not.
    setUserAgent(MAC_FIREFOX_UA)
    setLocation({ hostname: 'ciao.example.com', protocol: 'https:' })
    const view = mountCard()

    expect(view.text()).toContain('Secure: HTTPS')
    expect(view.text()).toContain('The address a browser needs before it will offer installing')
    // The one sentence about this browser states a condition, not a fact.
    expect(view.text()).toContain('Whether this browser can install the app here')
    expect(view.text()).toContain('depends on the browser and its version')
    expect(view.text()).not.toContain('This browser can install the app from this address')
    // And no row is told it is the reader's browser either.
    expect(currentRouteNames(view)).toEqual([])
  })

  it('marks this browser without hiding the other four routes', () => {
    setUserAgent(IPHONE_UA)
    const view = mountCard()

    const routes = view.findAll('.install-route')
    expect(routes).toHaveLength(5)
    expect(routes.filter(r => r.attributes('data-current') === 'true')).toHaveLength(1)
    expect(routes.find(r => r.attributes('data-current') === 'true')!.text()).toContain('Safari on iPhone or iPad')
    // Nothing is dropped: the other rows are all still on the page.
    for (const name of ['Safari on a Mac', 'Chrome on Mac or Windows', 'Edge on Windows', 'Chrome on Android']) {
      expect(view.text()).toContain(name)
    }
    expect(view.text()).toContain('iPhone and iPad only send notifications from the installed app')
  })

  it.each([
    [MAC_SAFARI_UA, 'Safari on a Mac (macOS Sonoma 14 or newer)'],
    [MAC_CHROME_UA, 'Chrome on Mac or Windows'],
    [WINDOWS_EDGE_UA, 'Edge on Windows'],
    [IPHONE_UA, 'Safari on iPhone or iPad'],
    [ANDROID_CHROME_UA, 'Chrome on Android'],
  ])('highlights the route this browser is on', (ua, expected) => {
    setUserAgent(ua)
    const view = mountCard()

    const current = view.findAll('.install-route').filter(r => r.attributes('data-current') === 'true')
    expect(current).toHaveLength(1)
    expect(current[0].text()).toContain(expected)
  })

  // A badge names a browser and an OS together, so a half-right answer is a
  // wrong one: each of these would send the reader to a menu or a platform the
  // row does not describe. None of them is placed, and all five rows stay.
  it.each([
    { ua: LINUX_FIREFOX_UA, why: 'Firefox on Linux' },
    { ua: LINUX_CHROME_UA, why: 'Chrome on Linux, a row that names Mac or Windows' },
    { ua: MAC_EDGE_UA, why: 'Edge on a Mac, a row that names Windows' },
    { ua: ANDROID_FIREFOX_UA, why: 'Firefox on Android, a row that names Chrome' },
    { ua: WINDOWS_BRAVE_UA, why: 'Brave on Windows, another Chromium browser rather than Chrome' },
    { ua: MAC_FIREFOX_UA, why: 'Firefox on a Mac, which has no install action there' },
  ])('marks no route for $why', ({ ua }) => {
    setUserAgent(ua)
    const view = mountCard()

    expect(view.findAll('.install-route')).toHaveLength(5)
    expect(currentRouteNames(view)).toEqual([])
    expect(view.text()).not.toContain('Your browser')
  })

  it('sends an iPad in desktop browsing mode to the Home Screen steps', () => {
    // Safari on iPad in its default desktop mode reports a Macintosh Safari
    // user agent. Nothing in the string says iPad; the five touch points do.
    // Claiming the Mac row here would send it looking for a Dock this device
    // does not have, so it has to read as an iPad and find the Share sheet.
    setUserAgent(MAC_SAFARI_UA)
    setMaxTouchPoints(5)
    const view = mountCard()

    expect(currentRouteNames(view)).toHaveLength(1)
    expect(currentRouteNames(view)[0]).toContain('Safari on iPhone or iPad')
    expect(currentRouteNames(view)[0]).not.toContain('Safari on a Mac')
    // The Mac row is still there and still unbadged, with its own steps.
    const macRow = view.findAll('.install-route').find(r => r.text().includes('Safari on a Mac'))!
    expect(macRow.attributes('data-current')).toBe('false')
    expect(macRow.text()).toContain('Add to Dock')
    // The row it was sent to is the one that works, so it carries the steps.
    expect(view.text()).toContain('Tap Share, then "Add to Home Screen"')
  })

  it('claims neither offline use nor an installed engine', () => {
    const view = mountCard()
    const text = view.text().toLowerCase()

    expect(text).not.toContain('offline')
    expect(text).not.toContain('without a connection')
    expect(text).not.toContain('without the engine')
    expect(text).not.toContain('installed engine')
  })

  it('uses a real heading and a native disclosure for the platform steps', () => {
    const view = mountCard()

    expect(view.find('h2.section-title').text()).toBe('Use Ciaobot as an app')
    expect(view.findAll('h3.install-route-name')).toHaveLength(5)

    // A div with a click handler would be unreachable by keyboard; a summary is.
    const details = view.get('details.install-platforms')
    const summary = details.get('summary')
    expect(summary.text()).toBe('Steps for every platform')
    expect(summary.attributes('tabindex')).toBeUndefined()
    expect(summary.attributes('role')).toBeUndefined()
    expect(details.findAll('li')).toHaveLength(5)
  })
})