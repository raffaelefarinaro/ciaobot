// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { api } from '../../lib/api'
import SettingsView from '../SettingsView.vue'
import SettingsKeyboardShortcuts from '../settings/SettingsKeyboardShortcuts.vue'

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
    return wrapper.findAll('.shortcut-setting-row kbd').map(k => k.text())
  } finally {
    wrapper.unmount()
  }
}

it('shows the Option glyph for browser shortcuts on Apple platforms', async () => {
  const labels = await shortcutLabels('MacIntel')
  expect(labels).toEqual(expect.arrayContaining(['⌥N', '⌥⌫', '⌥S', '⌥M', '⌥=', '⌥-', '⌥←', '⌥→', '⌘\\', '⌘⌥[', '⌘⌥]']))
  expect(labels.some(l => l.startsWith('Alt+'))).toBe(false)
})

it.each(['Win32', 'Linux x86_64'])('shows Alt labels for browser shortcuts on %s', async (platform) => {
  const labels = await shortcutLabels(platform)
  expect(labels).toEqual(expect.arrayContaining(['Alt+N', 'Alt+Backspace', 'Alt+S', 'Alt+M', 'Alt+=', 'Alt+-', 'Alt+←', 'Alt+→', 'Ctrl+\\', 'Ctrl+Alt+[', 'Ctrl+Alt+]']))
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
    expect(wrapper.findAll('.shortcut-setting-row')).toHaveLength(21)
    expect(wrapper.get('.shortcut-send-setting').text()).toContain('Send a message with')
    expect(wrapper.get('.settings-disclosure').text()).toBe('Restore all defaults')
  } finally {
    wrapper.unmount()
  }
})

it('does not capture or cancel an IME composition, then captures an Option arrow', async () => {
  const settings = { keyboard_shortcuts: {}, keyboard_send_mode: 'modifier', mid_turn_delivery: 'queue', revision: '' }
  vi.spyOn(api, 'get').mockResolvedValue(settings)
  const save = vi.spyOn(api, 'patch').mockResolvedValue(settings)
  const wrapper = mount(SettingsKeyboardShortcuts)
  try {
    await flushPromises()
    const previous = wrapper.findAll('.shortcut-setting-row').find(row => row.text().includes('Previous chat'))!
    await previous.find('button').trigger('click')
    const composing = new KeyboardEvent('keydown', { key: 'x', code: 'KeyX', altKey: true, isComposing: true, bubbles: true, cancelable: true })
    window.dispatchEvent(composing)
    expect(composing.defaultPrevented).toBe(false)
    expect(save).not.toHaveBeenCalled()
    const legacyIME = new KeyboardEvent('keydown', { key: 'x', code: 'KeyX', keyCode: 229, altKey: true, bubbles: true, cancelable: true })
    window.dispatchEvent(legacyIME)
    expect(legacyIME.defaultPrevented).toBe(false)
    expect(save).not.toHaveBeenCalled()
    const arrow = new KeyboardEvent('keydown', { key: 'ArrowLeft', code: 'ArrowLeft', altKey: true, bubbles: true, cancelable: true })
    window.dispatchEvent(arrow)
    await flushPromises()
    expect(arrow.defaultPrevented).toBe(true)
    expect(save).toHaveBeenCalledWith('/api/settings/keyboard', expect.objectContaining({ keyboard_shortcuts: { previousChat: 'Alt+ArrowLeft' } }))
  } finally { wrapper.unmount() }
})
