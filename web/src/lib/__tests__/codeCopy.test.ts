// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { writeClipboard } from '../codeCopy'

describe('writeClipboard', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    Reflect.deleteProperty(document, 'execCommand')
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
  })

  it('does not start a legacy copy after async rejection when its freshness guard is false', async () => {
    let rejectWrite!: (error: Error) => void
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn(() => new Promise<void>((_resolve, reject) => { rejectWrite = reject })) },
    })
    const legacyCopy = vi.fn(() => true)
    Object.defineProperty(document, 'execCommand', { configurable: true, value: legacyCopy })
    let current = true
    const result = writeClipboard('command', () => current)
    current = false
    rejectWrite(new Error('permission denied'))

    await expect(result).resolves.toBe(false)
    expect(legacyCopy).not.toHaveBeenCalled()
  })

  it('keeps legacy fallback when an async write rejects in the current context', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error('permission denied')) },
    })
    const legacyCopy = vi.fn(() => true)
    Object.defineProperty(document, 'execCommand', { configurable: true, value: legacyCopy })

    await expect(writeClipboard('command', () => true)).resolves.toBe(true)
    expect(legacyCopy).toHaveBeenCalledWith('copy')
  })
})
