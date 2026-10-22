// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  APPEARANCE_CACHE_KEY,
  DEFAULT_APPEARANCE,
  applyAppearanceToRoot,
  applyTheme,
  readLocalTheme,
  restoreAppearanceFromCache,
  writeAppearanceCache,
} from './appearance'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  document.documentElement.classList.remove('theme-light')
})

describe('applyAppearanceToRoot', () => {
  it('sets data-text-contrast for soft and removes it for normal', () => {
    const root = document.createElement('html')
    applyAppearanceToRoot({ text_contrast: 'soft' }, root)
    expect(root.dataset.textContrast).toBe('soft')
    applyAppearanceToRoot({ text_contrast: 'normal' }, root)
    expect(root.dataset.textContrast).toBeUndefined()
  })

  it('ignores an unknown value', () => {
    const root = document.createElement('html')
    applyAppearanceToRoot({ text_contrast: 'soft' }, root)
    applyAppearanceToRoot({ text_contrast: 'loud' as never }, root)
    expect(root.dataset.textContrast).toBe('soft')
  })
})

describe('theme', () => {
  it('applies explicit choices and caches them', () => {
    applyTheme('light')
    expect(document.documentElement.classList.contains('theme-light')).toBe(true)
    expect(readLocalTheme()).toBe('light')
    applyTheme('dark')
    expect(document.documentElement.classList.contains('theme-light')).toBe(false)
    expect(readLocalTheme()).toBe('dark')
  })

  it('resolves system from the OS scheme per device', () => {
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
    applyTheme('system')
    expect(document.documentElement.classList.contains('theme-light')).toBe(true)
    expect(readLocalTheme()).toBe('system')
  })

  it('reads system when nothing valid is stored', () => {
    expect(readLocalTheme()).toBe('system')
    localStorage.setItem('ciao-theme', 'bogus')
    expect(readLocalTheme()).toBe('system')
  })
})

describe('appearance paint cache', () => {
  it('round-trips through localStorage', () => {
    writeAppearanceCache({ ...DEFAULT_APPEARANCE, text_contrast: 'soft', revision: 'r' })
    const root = document.createElement('html')
    restoreAppearanceFromCache(root)
    expect(root.dataset.textContrast).toBe('soft')
  })

  it('ignores malformed JSON', () => {
    localStorage.setItem(APPEARANCE_CACHE_KEY, '{nope')
    const root = document.createElement('html')
    expect(() => restoreAppearanceFromCache(root)).not.toThrow()
    expect(root.dataset.textContrast).toBeUndefined()
  })

  it('does not throw when localStorage throws', () => {
    const blocked = () => { throw new Error('blocked') }
    vi.stubGlobal('localStorage', { getItem: blocked, setItem: blocked })
    const root = document.createElement('html')
    expect(() => writeAppearanceCache(DEFAULT_APPEARANCE)).not.toThrow()
    expect(() => restoreAppearanceFromCache(root)).not.toThrow()
    expect(() => applyTheme('dark')).not.toThrow()
    expect(readLocalTheme()).toBe('system')
  })
})
