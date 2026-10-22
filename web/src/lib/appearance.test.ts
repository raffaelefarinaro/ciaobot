// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import appVue from '../App.vue?raw'
import {
  APPEARANCE_CACHE_KEY,
  CUSTOM_TOKEN_NAMES,
  DEFAULT_PAIRS,
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

describe('applyAppearanceToRoot body font', () => {
  it('sets data-body-font for serif and mono and removes it for system', () => {
    const root = document.createElement('html')
    applyAppearanceToRoot({ body_font: 'serif' }, root)
    expect(root.dataset.bodyFont).toBe('serif')
    applyAppearanceToRoot({ body_font: 'mono' }, root)
    expect(root.dataset.bodyFont).toBe('mono')
    applyAppearanceToRoot({ body_font: 'system' }, root)
    expect(root.dataset.bodyFont).toBeUndefined()
  })

  it('ignores an unknown value', () => {
    const root = document.createElement('html')
    applyAppearanceToRoot({ body_font: 'serif' }, root)
    applyAppearanceToRoot({ body_font: 'comic' as never }, root)
    expect(root.dataset.bodyFont).toBe('serif')
  })

  it('restores the body font from the paint cache', () => {
    writeAppearanceCache({ ...DEFAULT_APPEARANCE, body_font: 'mono', revision: 'r' })
    const root = document.createElement('html')
    restoreAppearanceFromCache(root)
    expect(root.dataset.bodyFont).toBe('mono')
  })
})

describe('font tokens', () => {
  const sources = import.meta.glob('../**/*.{vue,css}', { query: '?raw', eager: true, import: 'default' }) as Record<string, string>

  it('no component uses var(--font-sans) outside App.vue and the Body font picker', () => {
    // SettingsAppearance.vue is the one exception: its System button must draw
    // itself in the system face even while Serif or Mono is active.
    const offenders = Object.entries(sources)
      .filter(([path, text]) => !path.endsWith('/App.vue') && !path.endsWith('/SettingsAppearance.vue')
        && text.includes('var(--font-sans)'))
      .map(([path]) => path)
    expect(offenders).toEqual([])
    const picker = Object.entries(sources).find(([path]) => path.endsWith('/SettingsAppearance.vue'))
    expect(picker?.[1].split('var(--font-sans)').length).toBe(2)
    expect(Object.keys(sources).some((path) => path.endsWith('/App.vue'))).toBe(true)
    expect(Object.keys(sources).some((path) => path.endsWith('settingsPanels.css'))).toBe(true)
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

describe('applyAppearanceToRoot custom colours', () => {
  const dark = { bg: '#101018', fg: '#e8e8f0' }
  const light = { bg: '#fdf6e3', fg: '#2b2b2b' }

  it('applies the active mode pair as inline tokens and marks the root', () => {
    const root = document.createElement('html')
    applyAppearanceToRoot({ custom_colors: { dark } }, root)
    expect(root.style.getPropertyValue('--bg')).toBe('#101018')
    expect(root.style.getPropertyValue('--fg')).toBe('#e8e8f0')
    expect(root.dataset.customColors).toBe('dark')
  })

  it('removes the tokens when the other mode has no pair, and applies it when it has', () => {
    const root = document.createElement('html')
    applyAppearanceToRoot({ custom_colors: { dark } }, root)
    root.classList.add('theme-light')
    applyAppearanceToRoot({ custom_colors: { dark } }, root)
    for (const name of CUSTOM_TOKEN_NAMES) expect(root.style.getPropertyValue(name)).toBe('')
    expect(root.dataset.customColors).toBeUndefined()
    applyAppearanceToRoot({ custom_colors: { dark, light } }, root)
    expect(root.style.getPropertyValue('--bg')).toBe('#fdf6e3')
    expect(root.dataset.customColors).toBe('light')
  })

  it('ignores a stored pair that fails the readability check', () => {
    const root = document.createElement('html')
    applyAppearanceToRoot({ custom_colors: { dark: { bg: '#1a1a2e', fg: '#5a5a70' } } }, root)
    expect(root.style.getPropertyValue('--bg')).toBe('')
    expect(root.dataset.customColors).toBeUndefined()
  })

  it('clears the tokens when custom colours are reset, and restores them from the cache', () => {
    const root = document.createElement('html')
    applyAppearanceToRoot({ custom_colors: { dark } }, root)
    applyAppearanceToRoot({ custom_colors: {} }, root)
    expect(root.style.getPropertyValue('--bg')).toBe('')
    writeAppearanceCache({ ...DEFAULT_APPEARANCE, custom_colors: { dark }, revision: 'r' })
    restoreAppearanceFromCache(root)
    expect(root.style.getPropertyValue('--bg')).toBe('#101018')
  })
})

describe('DEFAULT_PAIRS', () => {
  it('equals the --bg and --fg of the dark and light blocks in App.vue', () => {
    const block = (selector: RegExp): string => {
      const match = selector.exec(appVue)
      expect(match).not.toBeNull()
      return match![1]
    }
    const read = (css: string, name: string): string => {
      const match = new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`).exec(css)
      expect(match).not.toBeNull()
      return match![1].toLowerCase()
    }
    const dark = block(/^:root \{([^}]*)\}/m)
    const light = block(/^:root\.theme-light \{([^}]*)\}/m)
    expect({ bg: read(dark, '--bg'), fg: read(dark, '--fg') }).toEqual(DEFAULT_PAIRS.dark)
    expect({ bg: read(light, '--bg'), fg: read(light, '--fg') }).toEqual(DEFAULT_PAIRS.light)
  })
})
