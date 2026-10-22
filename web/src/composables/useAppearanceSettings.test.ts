// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { api } from '../lib/api'
import { applyAppearanceSettings, useAppearanceSettings } from './useAppearanceSettings'
import { useFontScale } from './useFontScale'

const base = { text_contrast: 'normal' as const, body_font: 'system' as const, theme: 'dark' as const, font_scale: 1.2, revision: 'r0' }

beforeEach(() => {
  applyAppearanceSettings(base)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  localStorage.clear()
  document.documentElement.removeAttribute('data-text-contrast')
  document.documentElement.removeAttribute('data-body-font')
  document.documentElement.classList.remove('theme-light')
})

describe('applyAppearanceSettings', () => {
  it('applies to <html> and writes the paint cache', () => {
    applyAppearanceSettings({ ...base, text_contrast: 'soft', revision: 'r1' })
    expect(document.documentElement.dataset.textContrast).toBe('soft')
    expect(JSON.parse(localStorage.getItem('ciao-appearance') || '{}').text_contrast).toBe('soft')
    expect(useAppearanceSettings().settings.value.revision).toBe('r1')
  })

  it('applies the body font to <html> and the cache, and ignores a bogus one', () => {
    applyAppearanceSettings({ ...base, body_font: 'serif', revision: 'r1' })
    expect(document.documentElement.dataset.bodyFont).toBe('serif')
    expect(JSON.parse(localStorage.getItem('ciao-appearance') || '{}').body_font).toBe('serif')
    applyAppearanceSettings({ body_font: 'comic' as never })
    expect(useAppearanceSettings().settings.value.body_font).toBe('serif')
    applyAppearanceSettings({ body_font: 'system' })
    expect(document.documentElement.dataset.bodyFont).toBeUndefined()
  })

  it('ignores bogus values', () => {
    applyAppearanceSettings({ ...base, text_contrast: 'soft', revision: 'r1' })
    applyAppearanceSettings({ text_contrast: 'loud' as never, theme: 'blue' as never, font_scale: 9 })
    const { settings } = useAppearanceSettings()
    expect(settings.value.text_contrast).toBe('soft')
    expect(settings.value.theme).toBe('dark')
    expect(settings.value.font_scale).toBe(1.2)
  })

  it('applies the engine theme and font scale to this browser', () => {
    applyAppearanceSettings({ ...base, theme: 'light', font_scale: 1.35, revision: 'r2' })
    expect(document.documentElement.classList.contains('theme-light')).toBe(true)
    expect(localStorage.getItem('ciao-theme')).toBe('light')
    expect(useFontScale().fontScale.value).toBe(1.35)
    expect(document.documentElement.style.getPropertyValue('--font-scale')).toBe('1.35')
  })

  it('seeds an engine that never stored a theme or font scale, once', async () => {
    localStorage.setItem('ciao-theme', 'light')
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ ...base, theme: 'light', revision: 'r3' })
    applyAppearanceSettings({ text_contrast: 'normal', theme: null, font_scale: null, revision: 'r2' })
    await vi.waitFor(() => expect(patch).toHaveBeenCalledTimes(1))
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', {
      theme: 'light',
      font_scale: useFontScale().fontScale.value,
      revision: 'r2',
    })
    applyAppearanceSettings({ text_contrast: 'normal', theme: null, font_scale: null, revision: 'r2' })
    await nextTick()
    expect(patch).toHaveBeenCalledTimes(1)
  })
})

describe('save', () => {
  it('PATCHes with the current revision and applies the result', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ ...base, text_contrast: 'soft', revision: 'r9' })
    await useAppearanceSettings().save({ text_contrast: 'soft' })
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', { text_contrast: 'soft', revision: 'r0' })
    expect(useAppearanceSettings().settings.value.revision).toBe('r9')
  })

  it('reloads and rethrows on a 409', async () => {
    const conflict = Object.assign(new Error('changed on another device'), { status: 409 })
    vi.spyOn(api, 'patch').mockRejectedValue(conflict)
    const get = vi.spyOn(api, 'get').mockResolvedValue({ ...base, text_contrast: 'soft', revision: 'r7' })
    await expect(useAppearanceSettings().save({ text_contrast: 'normal' })).rejects.toBe(conflict)
    expect(get).toHaveBeenCalledWith('/api/settings/appearance')
    expect(useAppearanceSettings().settings.value.text_contrast).toBe('soft')
  })
})

describe('custom colours', () => {
  const colors = { dark: { bg: '#101018', fg: '#e8e8f0' }, light: { bg: '#fdf6e3', fg: '#2b2b2b' } }

  it('applies the active mode pair and the cache keeps it', () => {
    applyAppearanceSettings({ ...base, custom_colors: colors, revision: 'r1' })
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('#101018')
    expect(JSON.parse(localStorage.getItem('ciao-appearance') || '{}').custom_colors.dark.bg).toBe('#101018')
    applyAppearanceSettings({ custom_colors: 'red' as never })
    expect(useAppearanceSettings().settings.value.custom_colors).toEqual(colors)
    applyAppearanceSettings({ custom_colors: {}, revision: 'r2' })
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('')
  })

  it('re-applies the other mode pair when the theme class flips', async () => {
    applyAppearanceSettings({ ...base, custom_colors: colors, revision: 'r1' })
    document.documentElement.classList.add('theme-light')
    await new Promise((resolve) => setTimeout(resolve, 0))
    await nextTick()
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('#fdf6e3')
    expect(document.documentElement.dataset.customColors).toBe('light')
    document.documentElement.classList.remove('theme-light')
    await new Promise((resolve) => setTimeout(resolve, 0))
    await nextTick()
    expect(document.documentElement.style.getPropertyValue('--bg')).toBe('#101018')
    applyAppearanceSettings({ custom_colors: {}, revision: 'r2' })
  })
})

describe('font scale persistence', () => {
  it('saves a locally changed font scale to the engine, debounced', async () => {
    vi.useFakeTimers()
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ ...base, font_scale: 1.3, revision: 'r4' })
    useFontScale().set(1.25)
    useFontScale().set(1.3)
    await nextTick()
    expect(patch).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(400)
    expect(patch).toHaveBeenCalledTimes(1)
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', { font_scale: 1.3, revision: 'r0' })
  })

  it('keeps a local change that an engine value overtakes before the save', async () => {
    vi.useFakeTimers()
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ ...base, font_scale: 1.4, revision: 'r5' })
    useFontScale().set(1.4)
    await nextTick()
    // An engine event (another device, or the first load) arrives inside the debounce.
    applyAppearanceSettings({ ...base, font_scale: 1.2, revision: 'r0b' })
    expect(useFontScale().fontScale.value).toBe(1.4)
    await vi.advanceTimersByTimeAsync(400)
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', { font_scale: 1.4, revision: 'r0b' })
  })

  it('keeps a local change made while a save is in flight and saves it next', async () => {
    vi.useFakeTimers()
    let resolveFirst!: (value: unknown) => void
    const patch = vi.spyOn(api, 'patch')
      .mockReturnValueOnce(new Promise((r) => { resolveFirst = r }))
      .mockResolvedValue({ ...base, font_scale: 1.45, revision: 'r7' })
    useFontScale().set(1.3)
    await vi.advanceTimersByTimeAsync(400)
    expect(patch).toHaveBeenCalledTimes(1)
    useFontScale().set(1.45)
    resolveFirst({ ...base, font_scale: 1.3, revision: 'r6' })
    await vi.advanceTimersByTimeAsync(1000)
    expect(useFontScale().fontScale.value).toBe(1.45)
    expect(patch).toHaveBeenLastCalledWith('/api/settings/appearance', { font_scale: 1.45, revision: 'r6' })
  })

  it('lets the engine win when the font scale save fails', async () => {
    vi.useFakeTimers()
    vi.spyOn(api, 'patch').mockRejectedValue(new Error('boom'))
    useFontScale().set(1.5)
    await vi.advanceTimersByTimeAsync(400)
    expect(useFontScale().fontScale.value).toBe(1.2)
  })

  it('seeds a font scale snapped to the 0.05 grid', async () => {
    // seeding is once per page life, so this needs a fresh module
    vi.resetModules()
    const freshApi = (await import('../lib/api')).api
    const fresh = await import('./useAppearanceSettings')
    const freshScale = (await import('./useFontScale')).useFontScale()
    const patch = vi.spyOn(freshApi, 'patch').mockResolvedValue({ ...base, revision: 'r8' })
    localStorage.setItem('ciao-theme', 'dark')
    freshScale.set(1.23)
    fresh.applyAppearanceSettings({ text_contrast: 'normal', theme: 'dark', font_scale: null, revision: 'r0' })
    await vi.waitFor(() => expect(patch).toHaveBeenCalled())
    expect(patch.mock.calls[0][1]).toMatchObject({ font_scale: 1.25 })
  })
})
