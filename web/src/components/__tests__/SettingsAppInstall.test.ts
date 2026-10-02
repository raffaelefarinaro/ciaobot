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

const VENDOR_LINKS = [
  'https://support.apple.com/en-us/104996',
  'https://support.google.com/chrome/answer/9658361?hl=en&co=GENIE.Platform%3DDesktop',
  'https://support.microsoft.com/en-us/edge/install-manage-or-uninstall-apps-in-microsoft-edge',
  'https://support.apple.com/guide/iphone/iphea86e5236/ios',
  'https://support.google.com/chrome/answer/9658361?hl=en&co=GENIE.Platform%3DAndroid',
]

const originalUserAgent = navigator.userAgent
let wrapper: VueWrapper | null = null

function setUserAgent(ua: string) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true })
}

/** jsdom has no matchMedia at all, so a stub left behind by one case would
 *  hand a "standalone" answer to the next one. */
function setStandalone(standalone: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      matches: standalone && query.includes('standalone'),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  })
}

function setSecureContext(secure: boolean) {
  Object.defineProperty(window, 'isSecureContext', { value: secure, configurable: true })
}

/** jsdom serves the page from localhost; a LAN address is what the plain-HTTP
 *  warning is about. */
function setHostname(hostname: string) {
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...window.location, hostname, protocol: 'http:', href: `http://${hostname}/settings` },
  })
}

function mountCard() {
  wrapper = mount(SettingsAppInstall, { attachTo: document.body })
  return wrapper
}

beforeEach(() => {
  localStorage.clear()
  setUserAgent(MAC_CHROME_UA)
  setStandalone(false)
  setSecureContext(true)
  setHostname('localhost')
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  localStorage.clear()
  setUserAgent(originalUserAgent)
  delete (window as unknown as Record<string, unknown>).matchMedia
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
    setHostname('192.168.1.20')
    const lan = mountCard()

    expect(lan.text()).toContain('Plain HTTP on the network')
    expect(lan.text()).toContain('only installs over HTTPS or on the host’s own localhost')
    expect(lan.text()).not.toContain('This browser can install the app from this address')
    wrapper?.unmount()

    setSecureContext(true)
    setHostname('localhost')
    const loopback = mountCard()
    expect(loopback.text()).toContain('Secure: the host’s own localhost')
    expect(loopback.text()).toContain('This browser can install the app from this address')
    wrapper?.unmount()

    setHostname('ciao.example.com')
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, hostname: 'ciao.example.com', protocol: 'https:', href: 'https://ciao.example.com/settings' },
    })
    const https = mountCard()
    expect(https.text()).toContain('Secure: HTTPS')
    expect(https.text()).toContain('This browser can install the app from this address')
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

  it('marks no route on a browser it cannot place', () => {
    setUserAgent('Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0')
    const view = mountCard()

    expect(view.findAll('.install-route')).toHaveLength(5)
    expect(view.findAll('.install-route').filter(r => r.attributes('data-current') === 'true')).toHaveLength(0)
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