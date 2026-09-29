// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { api } from '../../lib/api'
import SettingsView from '../SettingsView.vue'

afterEach(() => vi.restoreAllMocks())

it('explains how to invoke critique and where the automatic panel comes from', async () => {
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings/:tab', component: stub }],
  })
  await router.push('/settings/models')
  await router.isReady()
  vi.spyOn(api, 'get').mockImplementation(async (path) => {
    if (path === '/api/settings/routines') return {
      critique_models: '',
      critique_models_effective: 'sonnet,opencode:openai/gpt-5',
      model_options: { anthropic: [] },
    }
    throw new Error('Other settings are not needed')
  })
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: stub } },
  })
  try {
    await flushPromises()
    const row = wrapper.findAll('.routine-row').find((element) => element.text().includes('Critique panel'))
    expect(row).toBeTruthy()
    expect(row!.text()).toContain('/critique')
    expect(row!.text()).toContain('critique skill')
    expect(row!.text()).toContain('distinct model defaults of your configured workspaces')
    expect(row!.text()).toContain('Unavailable providers are skipped')
    const link = row!.get('a')
    expect(link.attributes('href')).toBe('https://www.raffaelefarinaro.com/ciaobot/models.html#critique')
    expect(link.attributes('rel')).toContain('noopener')
  } finally {
    wrapper.unmount()
  }
})
