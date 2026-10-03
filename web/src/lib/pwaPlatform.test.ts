import { afterEach, describe, expect, it } from 'vitest'
import {
  installInstructions,
  isAndroid,
  isEdge,
  isGoogleChrome,
  isIos,
  isIpadDesktopMode,
  isSafari,
  isWindows,
} from './pwaPlatform'

const MAC_SAFARI_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
const MAC_CHROME_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
const ANDROID_CHROME_UA =
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
const WINDOWS_CHROME_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
const WINDOWS_EDGE_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'
/** Brave keeps Chrome's and Safari's tokens and appends its own, which is what
 *  the isGoogleChrome() check keys on. */
const WINDOWS_BRAVE_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Brave/1.61.104'
const MAC_FIREFOX_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0'

/** These probes answer questions about a particular reader, so each case sets
 *  its own browser and undoes it: a user agent or a touch count left behind
 *  would decide the next case's answer. Descriptors, not values, because
 *  maxTouchPoints is an own property of Navigator.prototype. */
function override(target: object, key: string, value: unknown) {
  const descriptor = Object.getOwnPropertyDescriptor(target, key)
  Object.defineProperty(target, key, { value, configurable: true })
  return () => {
    if (descriptor) Object.defineProperty(target, key, descriptor)
    else delete (target as unknown as Record<string, unknown>)[key]
  }
}

const undo: Array<() => void> = []

function setUserAgent(ua: string) {
  undo.push(override(navigator, 'userAgent', ua))
}

function setMaxTouchPoints(points: number) {
  undo.push(override(navigator, 'maxTouchPoints', points))
}

afterEach(() => {
  while (undo.length) undo.pop()!()
})

describe('isSafari', () => {
  // Chrome's user agent also ends in "Safari/537.36", so the name alone would
  // send a Chromium user to Safari's File menu, which does not exist there.
  it('is true for Safari on a Mac', () => {
    setUserAgent(MAC_SAFARI_UA)
    expect(isSafari()).toBe(true)
  })

  it('is false for Chrome on a Mac', () => {
    setUserAgent(MAC_CHROME_UA)
    expect(isSafari()).toBe(false)
  })
})

describe('the install-route probes', () => {
  it('names the platform on its own', () => {
    setUserAgent(WINDOWS_CHROME_UA)
    expect(isWindows()).toBe(true)
    expect(isAndroid()).toBe(false)

    setUserAgent(MAC_CHROME_UA)
    expect(isWindows()).toBe(false)

    setUserAgent(ANDROID_CHROME_UA)
    expect(isAndroid()).toBe(true)
    expect(isWindows()).toBe(false)

    setUserAgent(IPHONE_UA)
    expect(isIos()).toBe(true)
    expect(isAndroid()).toBe(false)
    expect(isWindows()).toBe(false)
  })

  it('names Chrome, and only Chrome', () => {
    setUserAgent(WINDOWS_CHROME_UA)
    expect(isGoogleChrome()).toBe(true)
    setUserAgent(MAC_CHROME_UA)
    expect(isGoogleChrome()).toBe(true)
    setUserAgent(ANDROID_CHROME_UA)
    expect(isGoogleChrome()).toBe(true)

    // Edge keeps Chrome's token, so its own probe is what separates them.
    setUserAgent(WINDOWS_EDGE_UA)
    expect(isEdge()).toBe(true)
    expect(isGoogleChrome()).toBe(false)

    // Another Chromium browser appends its own name after Safari's token, which
    // is exactly what the check keys on. Its menus are not Chrome's.
    setUserAgent(WINDOWS_BRAVE_UA)
    expect(isGoogleChrome()).toBe(false)

    setUserAgent(MAC_FIREFOX_UA)
    expect(isGoogleChrome()).toBe(false)
    expect(isEdge()).toBe(false)
  })

  it('spots an iPad left in desktop browsing mode by its touch', () => {
    // A Macintosh Safari user agent with no touch is a Mac.
    setUserAgent(MAC_SAFARI_UA)
    setMaxTouchPoints(0)
    expect(isIpadDesktopMode()).toBe(false)

    // The same user agent with five touch points is an iPad asking to be
    // treated as one, which no user-agent string can say out loud.
    setMaxTouchPoints(5)
    expect(isIpadDesktopMode()).toBe(true)

    // A real iPhone already identifies itself and needs no help.
    setUserAgent(IPHONE_UA)
    expect(isIpadDesktopMode()).toBe(false)

    // A touchscreen laptop is not an iPad either, and no Mac has a touchscreen,
    // so the Macintosh token is what keeps a Windows all-in-one out.
    setUserAgent(WINDOWS_CHROME_UA)
    expect(isIpadDesktopMode()).toBe(false)
  })
})

describe('installInstructions', () => {
  // iOS has no install prompt and no "Add to Dock"; the Share sheet is the
  // only way in, and push needs it.
  it('names the Share sheet on iPhone', () => {
    setUserAgent(IPHONE_UA)
    expect(installInstructions()).toBe('Tap Share, then "Add to Home Screen".')
  })

  it('names Add to Dock in Safari on a Mac', () => {
    setUserAgent(MAC_SAFARI_UA)
    expect(installInstructions()).toBe('In Safari, choose File → "Add to Dock".')
  })

  it('falls back to the browser install control elsewhere', () => {
    setUserAgent(MAC_CHROME_UA)
    expect(installInstructions()).toBe('Use your browser\'s Install option in the address bar or menu.')
  })
})
