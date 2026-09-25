import { afterEach, describe, expect, it } from 'vitest'
import { installInstructions, isSafari } from './pwaPlatform'

const MAC_SAFARI_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
const MAC_CHROME_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'

const original = navigator.userAgent

function setUserAgent(ua: string) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true })
}

afterEach(() => {
  Object.defineProperty(navigator, 'userAgent', { value: original, configurable: true })
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
