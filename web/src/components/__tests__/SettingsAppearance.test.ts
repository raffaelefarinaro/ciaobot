// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import SettingsAppearance from '../settings/SettingsAppearance.vue'
import { api } from '../../lib/api'
import { applyAppearanceSettings } from '../../composables/useAppearanceSettings'

const engine = { text_contrast: 'normal' as const, body_font: 'system' as const, theme: 'dark' as const, font_scale: 1.2, revision: 'r0' }

beforeEach(() => {
  applyAppearanceSettings(engine)
  vi.spyOn(api, 'get').mockResolvedValue(engine)
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  document.documentElement.removeAttribute('data-text-contrast')
  document.documentElement.removeAttribute('data-body-font')
})

function radios(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('[role="radiogroup"][aria-label="Text contrast"] [role="radio"]')
}

describe('SettingsAppearance', () => {
  it('renders Text contrast radios with the engine value checked', async () => {
    const wrapper = mount(SettingsAppearance)
    await flushPromises()
    const buttons = radios(wrapper)
    expect(buttons.map((b) => b.text())).toEqual(['Normal', 'Soft'])
    expect(buttons.map((b) => b.attributes('aria-checked'))).toEqual(['true', 'false'])
  })

  it('patches text_contrast with the current revision when Soft is clicked', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ ...engine, text_contrast: 'soft', revision: 'r1' })
    const wrapper = mount(SettingsAppearance)
    await flushPromises()
    await radios(wrapper)[1].trigger('click')
    await flushPromises()
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', { text_contrast: 'soft', revision: 'r0' })
    expect(radios(wrapper).map((b) => b.attributes('aria-checked'))).toEqual(['false', 'true'])
  })

  it('saves the last choice made while a save is pending and keeps focus on it', async () => {
    let resolveFirst!: (value: unknown) => void
    const patch = vi.spyOn(api, 'patch')
      .mockReturnValueOnce(new Promise((r) => { resolveFirst = r }))
      .mockResolvedValue({ ...engine, text_contrast: 'normal', revision: 'r2' })
    const wrapper = mount(SettingsAppearance, { attachTo: document.body })
    await flushPromises()
    await radios(wrapper)[1].trigger('click')
    await radios(wrapper)[0].trigger('keydown', { key: 'ArrowLeft' })
    expect(patch).toHaveBeenCalledTimes(1)
    resolveFirst({ ...engine, text_contrast: 'soft', revision: 'r1' })
    await flushPromises()
    expect(patch).toHaveBeenCalledTimes(2)
    expect(patch).toHaveBeenLastCalledWith('/api/settings/appearance', { text_contrast: 'normal', revision: 'r1' })
    expect(radios(wrapper)[0].attributes('aria-checked')).toBe('true')
    expect(document.activeElement).toBe(radios(wrapper)[0].element)
    wrapper.unmount()
  })

  it('follows the radio keyboard pattern: one tab stop, arrows select, Home/End jump', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ ...engine, text_contrast: 'soft', revision: 'r1' })
    const wrapper = mount(SettingsAppearance, { attachTo: document.body })
    await flushPromises()
    expect(radios(wrapper).map((b) => b.attributes('tabindex'))).toEqual(['0', '-1'])
    await radios(wrapper)[0].trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', { text_contrast: 'soft', revision: 'r0' })
    expect(document.activeElement).toBe(radios(wrapper)[1].element)
    expect(radios(wrapper).map((b) => b.attributes('tabindex'))).toEqual(['-1', '0'])
    await radios(wrapper)[1].trigger('keydown', { key: 'Home' })
    expect(document.activeElement).toBe(radios(wrapper)[0].element)
    await radios(wrapper)[0].trigger('keydown', { key: 'End' })
    expect(document.activeElement).toBe(radios(wrapper)[1].element)
    wrapper.unmount()
  })

  it('shows a failed save in an alert', async () => {
    vi.spyOn(api, 'patch').mockRejectedValue(new Error('Appearance settings changed on another device. Reload and try again.'))
    const wrapper = mount(SettingsAppearance)
    await flushPromises()
    await radios(wrapper)[1].trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('another device')
  })
})

describe('SettingsAppearance body font', () => {
  const fontRadios = (wrapper: ReturnType<typeof mount>) =>
    wrapper.findAll('[role="radiogroup"][aria-label="Body font"] [role="radio"]')

  it('renders System, Serif and Mono with the engine value checked, each in its own face', async () => {
    const wrapper = mount(SettingsAppearance)
    await flushPromises()
    const buttons = fontRadios(wrapper)
    expect(buttons.map((b) => b.text())).toEqual(['System', 'Serif', 'Mono'])
    expect(buttons.map((b) => b.attributes('aria-checked'))).toEqual(['true', 'false', 'false'])
    expect(buttons.map((b) => b.attributes('style') ?? '')).toEqual([
      expect.stringContaining('--font-sans'),
      expect.stringContaining('--font-serif'),
      expect.stringContaining('--font-mono'),
    ])
  })

  it('patches body_font with the current revision when Serif is clicked', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ ...engine, body_font: 'serif', revision: 'r1' })
    const wrapper = mount(SettingsAppearance)
    await flushPromises()
    await fontRadios(wrapper)[1].trigger('click')
    await flushPromises()
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', { body_font: 'serif', revision: 'r0' })
    expect(fontRadios(wrapper).map((b) => b.attributes('aria-checked'))).toEqual(['false', 'true', 'false'])
    expect(document.documentElement.dataset.bodyFont).toBe('serif')
  })

  it('shows a failed save in an alert', async () => {
    vi.spyOn(api, 'patch').mockRejectedValue(new Error('Appearance settings changed on another device. Reload and try again.'))
    const wrapper = mount(SettingsAppearance)
    await flushPromises()
    await fontRadios(wrapper)[2].trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('another device')
  })
})
