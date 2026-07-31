// @vitest-environment jsdom

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { installViewportPlumbing } from './viewport'

const FULL_HEIGHT = 932
const KEYBOARD_HEIGHT = 596

type FakeViewport = EventTarget & { height: number }

// One shared fake for the whole file. The plumbing attaches its listeners once,
// so swapping in a fresh EventTarget per test would orphan them and the fake
// keyboard would stop reaching the code under test.
const vv = new EventTarget() as FakeViewport
vv.height = FULL_HEIGHT
Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true })
Object.defineProperty(window, 'innerHeight', { value: FULL_HEIGHT, configurable: true, writable: true })

function appHeight(): string {
  return document.documentElement.style.getPropertyValue('--app-h')
}

function keyboardOpen(): boolean {
  return document.documentElement.classList.contains('keyboard-open')
}

describe('installViewportPlumbing', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vv.height = FULL_HEIGHT
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true, writable: true })
    document.documentElement.classList.remove('keyboard-open')
    document.documentElement.style.removeProperty('--app-h')
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  function settle() {
    vi.runAllTimers()
  }

  /** Keyboard opens: iOS shrinks the visual viewport and fires a vv resize. */
  function openKeyboard() {
    vv.height = KEYBOARD_HEIGHT
    vv.dispatchEvent(new Event('resize'))
  }

  it('tracks the visual viewport height on the --app-h variable', () => {
    installViewportPlumbing()
    settle()
    expect(appHeight()).toBe(`${FULL_HEIGHT}px`)
    expect(keyboardOpen()).toBe(false)

    openKeyboard()
    expect(appHeight()).toBe(`${KEYBOARD_HEIGHT}px`)
    expect(keyboardOpen()).toBe(true)
  })

  it('re-measures on resume when the keyboard was dismissed while suspended', () => {
    installViewportPlumbing()
    settle()
    openKeyboard()
    expect(keyboardOpen()).toBe(true)

    // iOS suspends JS while backgrounded and dismisses the keyboard itself, so
    // the viewport grows back with no resize event ever reaching the page.
    vv.height = FULL_HEIGHT
    document.dispatchEvent(new Event('visibilitychange'))
    settle()

    expect(appHeight()).toBe(`${FULL_HEIGHT}px`)
    expect(keyboardOpen()).toBe(false)
  })

  it('re-measures on a bfcache restore', () => {
    installViewportPlumbing()
    settle()
    openKeyboard()

    vv.height = FULL_HEIGHT
    window.dispatchEvent(new Event('pageshow'))
    settle()

    expect(appHeight()).toBe(`${FULL_HEIGHT}px`)
    expect(keyboardOpen()).toBe(false)
  })

  it('ignores a stale height read at the instant of resume', () => {
    installViewportPlumbing()
    settle()
    openKeyboard()

    // Height still reports the keyboard-open value when visibilitychange
    // fires; the dismissal animation lands a moment later.
    document.dispatchEvent(new Event('visibilitychange'))
    expect(appHeight()).toBe(`${KEYBOARD_HEIGHT}px`)
    vv.height = FULL_HEIGHT
    settle()

    expect(appHeight()).toBe(`${FULL_HEIGHT}px`)
    expect(keyboardOpen()).toBe(false)
  })

  it('does not re-measure while the app is hidden', () => {
    installViewportPlumbing()
    settle()
    openKeyboard()

    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true, writable: true })
    vv.height = FULL_HEIGHT
    document.dispatchEvent(new Event('visibilitychange'))
    settle()

    expect(appHeight()).toBe(`${KEYBOARD_HEIGHT}px`)
  })

  it('keeps detecting the keyboard after a resume that happens with it open', () => {
    installViewportPlumbing()
    settle()
    openKeyboard()

    // Resuming with the keyboard still up must not record the shrunken height
    // as the new maximum, or keyboard-open would never be detected again.
    document.dispatchEvent(new Event('visibilitychange'))
    settle()
    expect(keyboardOpen()).toBe(true)

    vv.height = FULL_HEIGHT
    vv.dispatchEvent(new Event('resize'))
    expect(keyboardOpen()).toBe(false)

    openKeyboard()
    expect(keyboardOpen()).toBe(true)
  })
})
