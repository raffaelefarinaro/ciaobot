// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import SettingsAppearance from '../settings/SettingsAppearance.vue'
import { api } from '../../lib/api'
import { applyAppearanceSettings } from '../../composables/useAppearanceSettings'

const engine = { text_contrast: 'normal' as const, theme: 'dark' as const, font_scale: 1.2, revision: 'r0' }

beforeEach(() => {
  applyAppearanceSettings(engine)
  vi.spyOn(api, 'get').mockResolvedValue(engine)
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  document.documentElement.removeAttribute('data-text-contrast')
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

  it('ignores another choice while a save is pending', async () => {
    let resolve!: (value: typeof engine) => void
    const patch = vi.spyOn(api, 'patch').mockReturnValue(new Promise((r) => { resolve = r }))
    const wrapper = mount(SettingsAppearance)
    await flushPromises()
    await radios(wrapper)[1].trigger('click')
    await radios(wrapper)[0].trigger('click')
    expect(patch).toHaveBeenCalledTimes(1)
    resolve({ ...engine, text_contrast: 'soft' as never, revision: 'r1' })
    await flushPromises()
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
