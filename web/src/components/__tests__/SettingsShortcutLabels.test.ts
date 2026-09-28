// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { api } from '../../lib/api'
import SettingsView from '../SettingsView.vue'

afterEach(() => {
  vi.restoreAllMocks()
  delete (navigator as unknown as Record<string, unknown>).platform
})

async function shortcutLabels(platform: string): Promise<string[]> {
  Object.defineProperty(navigator, 'platform', { value: platform, configurable: true })
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: stub }] })
  await router.push('/settings')
  await router.isReady()
  vi.spyOn(api, 'get').mockRejectedValue(new Error('Unrelated settings data unavailable in this test'))
  const wrapper = mount(SettingsView, { global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: stub } } })
  try {
    await flushPromises()
    // The less common shortcuts sit behind "Show all"; open it to read them.
    await wrapper.get('.settings-disclosure').trigger('click')
    return wrapper.findAll('.shortcut-list kbd').map(k => k.text())
  } finally {
    wrapper.unmount()
  }
}

it('shows the Option glyph for browser shortcuts on Apple platforms', async () => {
  const labels = await shortcutLabels('MacIntel')
  expect(labels).toEqual(expect.arrayContaining(['⌥N', '⌥⌫', '⌥S', '⌥M', '⌥=', '⌥-']))
  expect(labels.some(l => l.startsWith('Alt+'))).toBe(false)
})

it.each(['Win32', 'Linux x86_64'])('shows Alt labels for browser shortcuts on %s', async (platform) => {
  const labels = await shortcutLabels(platform)
  expect(labels).toEqual(expect.arrayContaining(['Alt+N', 'Alt+Backspace', 'Alt+S', 'Alt+M', 'Alt+=', 'Alt+-']))
  expect(labels.some(l => l.includes('⌥'))).toBe(false)
})

it('shows the common shortcuts and discloses the rest', async () => {
  Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true })
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: stub }] })
  await router.push('/settings')
  await router.isReady()
  vi.spyOn(api, 'get').mockRejectedValue(new Error('Unrelated settings data unavailable in this test'))
  const wrapper = mount(SettingsView, { global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: stub } } })
  try {
    await flushPromises()
    const toggle = wrapper.get('.settings-disclosure')
    expect(wrapper.findAll('.shortcut-list li')).toHaveLength(3)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-controls')).toBe('settings-shortcut-list')
    expect(toggle.text()).toBe('Show all 10')

    await toggle.trigger('click')
    expect(wrapper.findAll('.shortcut-list li')).toHaveLength(10)
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.text()).toBe('Show fewer')
  } finally {
    wrapper.unmount()
  }
})
