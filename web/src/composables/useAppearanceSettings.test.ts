// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { api } from '../lib/api'
import { applyAppearanceSettings, useAppearanceSettings } from './useAppearanceSettings'
import { useFontScale } from './useFontScale'

const base = { text_contrast: 'normal' as const, theme: 'dark' as const, font_scale: 1.2, revision: 'r0' }

beforeEach(() => {
  applyAppearanceSettings(base)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  localStorage.clear()
  document.documentElement.removeAttribute('data-text-contrast')
  document.documentElement.classList.remove('theme-light')
})

describe('applyAppearanceSettings', () => {
  it('applies to <html> and writes the paint cache', () => {
    applyAppearanceSettings({ ...base, text_contrast: 'soft', revision: 'r1' })
    expect(document.documentElement.dataset.textContrast).toBe('soft')
    expect(JSON.parse(localStorage.getItem('ciao-appearance') || '{}').text_contrast).toBe('soft')
    expect(useAppearanceSettings().settings.value.revision).toBe('r1')
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
})
