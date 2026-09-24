// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, nextTick } from 'vue'
import { api } from '../../lib/api'
import SettingsView from '../SettingsView.vue'

afterEach(() => {
  vi.restoreAllMocks()
})

async function mountSettings() {
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/settings', component: stub },
      { path: '/settings/:tab', component: stub },
    ],
  })
  await router.push('/settings')
  await router.isReady()
  vi.spyOn(api, 'get').mockRejectedValue(new Error('Unrelated settings data unavailable in this test'))
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: stub } },
  })
  await flushPromises()
  await nextTick()
  await nextTick()
  return { wrapper, router }
}

it('lists the rendered sections of the tab in the On this page rail', async () => {
  const { wrapper } = await mountSettings()
  try {
    const labels = wrapper.findAll('.settings-toc-item').map(item => item.text())
    // Built from the page itself, in page order, headings in sentence case.
    expect(labels.slice(0, 2)).toEqual(['Appearance', 'This host'])
    expect(labels).toContain('Keyboard shortcuts')
    expect(labels).toContain('Open source')
    // The client/role callout is a notice, not a section to jump to.
    expect(labels).not.toContain('Connection role unavailable')
    // Every entry points at a real element.
    for (const item of wrapper.findAll('.settings-toc-item')) {
      expect(item.attributes('type')).toBe('button')
    }
  } finally {
    wrapper.unmount()
  }
})

it('scrolls to and focuses the section a rail entry names', async () => {
  const { wrapper } = await mountSettings()
  try {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const entry = wrapper.findAll('.settings-toc-item').find(item => item.text() === 'Keyboard shortcuts')
    expect(entry).toBeTruthy()
    await entry!.trigger('click')
    const section = document.getElementById('settings-keyboard-shortcuts')
    expect(section).not.toBeNull()
    expect(scroll).toHaveBeenCalled()
    expect(document.activeElement).toBe(section)
    expect(entry!.attributes('aria-current')).toBe('location')
  } finally {
    wrapper.unmount()
  }
})
