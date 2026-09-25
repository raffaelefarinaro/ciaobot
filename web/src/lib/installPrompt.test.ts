// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  _resetInstallPromptForTests,
  canPromptInstall,
  installed,
  listenForInstallPrompt,
  promptInstall,
} from './installPrompt'

function fakeInstallEvent() {
  return Object.assign(
    new Event('beforeinstallprompt', { cancelable: true }),
    {
      prompt: vi.fn(async () => {}),
      userChoice: Promise.resolve({ outcome: 'accepted' as const }),
    },
  )
}

beforeEach(() => {
  _resetInstallPromptForTests()
  listenForInstallPrompt(window)
})

describe('install prompt listener', () => {
  // The event only fires once per browser session, so it has to be captured
  // before any card is mounted; the listener calls preventDefault to suppress
  // Chromium's own mini-infobar and keep the prompt in the app's hands.
  it('captures the deferred event and suppresses the default', () => {
    const event = fakeInstallEvent()
    window.dispatchEvent(event)

    expect(canPromptInstall.value).toBe(true)
    expect(event.defaultPrevented).toBe(true)
  })

  it('shows the native prompt and reports acceptance', async () => {
    const event = fakeInstallEvent()
    window.dispatchEvent(event)

    await expect(promptInstall()).resolves.toBe(true)
    expect(event.prompt).toHaveBeenCalledTimes(1)
    expect(installed.value).toBe(true)
    expect(canPromptInstall.value).toBe(false)
  })

  it('notices an install that happened without our prompt', () => {
    window.dispatchEvent(new Event('appinstalled'))
    expect(installed.value).toBe(true)
  })

  it('resolves false when the browser never offered a prompt', async () => {
    await expect(promptInstall()).resolves.toBe(false)
  })
})
