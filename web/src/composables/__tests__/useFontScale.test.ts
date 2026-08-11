// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_FONT_SCALE,
  FONT_SCALE_STEP,
  FONT_SCALE_STORAGE_KEY,
  MAX_FONT_SCALE,
  MIN_FONT_SCALE,
  useFontScale,
} from '../useFontScale'

// Vitest 4's jsdom env does not provide localStorage, and the composable
// round-trips through it.
class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
  key(): string | null { return null }
  get length(): number { return this.values.size }
}

describe('useFontScale', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    // The module ref is seeded once at import; put it back to a known value
    // through the public API rather than re-importing the module.
    useFontScale().reset()
  })

  it('shares one value across callers', () => {
    // The regression this pins: ChatLayout calls useFontScale() once at app
    // mount and lives for the session, while Settings calls it on every open.
    // With a per-caller ref, Settings moving the scale left ChatLayout's copy
    // stale, so the next keyboard zoom added its delta to the old value and
    // the font jumped the wrong way.
    const longLived = useFontScale()
    const settings = useFontScale()

    settings.set(MAX_FONT_SCALE)
    expect(longLived.fontScale.value).toBe(MAX_FONT_SCALE)

    longLived.adjust(-FONT_SCALE_STEP)
    expect(settings.fontScale.value).toBe(
      parseFloat((MAX_FONT_SCALE - FONT_SCALE_STEP).toFixed(2)),
    )
  })

  it('clamps to the shared bounds and persists', () => {
    const { fontScale, set } = useFontScale()

    set(MAX_FONT_SCALE + 1)
    expect(fontScale.value).toBe(MAX_FONT_SCALE)
    set(MIN_FONT_SCALE - 1)
    expect(fontScale.value).toBe(MIN_FONT_SCALE)

    expect(localStorage.getItem(FONT_SCALE_STORAGE_KEY)).toBe(String(MIN_FONT_SCALE))
    expect(document.documentElement.style.getPropertyValue('--font-scale'))
      .toBe(String(MIN_FONT_SCALE))
  })

  it('steps without floating-point drift and resets to the default', () => {
    const { fontScale, set, adjust, reset } = useFontScale()

    set(1.1)
    adjust(FONT_SCALE_STEP)
    expect(fontScale.value).toBe(1.15)

    reset()
    expect(fontScale.value).toBe(DEFAULT_FONT_SCALE)
  })
})
