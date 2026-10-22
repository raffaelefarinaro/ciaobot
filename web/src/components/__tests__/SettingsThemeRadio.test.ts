// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { api } from '../../lib/api'
import SettingsView from '../SettingsView.vue'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
  document.documentElement.classList.remove('theme-light')
})

it('Theme follows the radio keyboard pattern and saves the choice to the engine', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true }))
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings/:tab?', component: stub }] })
  await router.push('/settings/appearance')
  await router.isReady()
  vi.spyOn(api, 'get').mockRejectedValue(new Error('Unrelated settings data unavailable in this test'))
  const patch = vi.spyOn(api, 'patch').mockResolvedValue({ text_contrast: 'normal', theme: 'light', font_scale: 1.2, revision: 'r1' })
  localStorage.setItem('ciao-theme', 'dark')
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: stub } },
  })
  try {
    await flushPromises()
    const group = wrapper.get('[role="radiogroup"][aria-label="Theme"]')
    const radios = () => group.findAll('[role="radio"]')
    expect(radios().map((r) => r.text())).toEqual(['Dark', 'Light', 'System'])
    expect(radios().map((r) => r.attributes('aria-checked'))).toEqual(['true', 'false', 'false'])
    expect(radios().map((r) => r.attributes('tabindex'))).toEqual(['0', '-1', '-1'])

    await radios()[0].trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(document.activeElement).toBe(radios()[1].element)
    expect(radios()[1].attributes('aria-checked')).toBe('true')
    expect(document.documentElement.classList.contains('theme-light')).toBe(true)
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', expect.objectContaining({ theme: 'light' }))

    await radios()[1].trigger('keydown', { key: 'End' })
    expect(document.activeElement).toBe(radios()[2].element)
    await radios()[2].trigger('keydown', { key: 'ArrowRight' })
    expect(document.activeElement).toBe(radios()[0].element)
  } finally {
    wrapper.unmount()
  }
})
