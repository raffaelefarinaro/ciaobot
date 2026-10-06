// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { api } from '../../lib/api'
import SettingsView from '../SettingsView.vue'

afterEach(() => vi.restoreAllMocks())

it('tells the user to move the main workspace with setup, not a .env key', async () => {
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: stub }] })
  await router.push('/settings')
  await router.isReady()
  vi.spyOn(api, 'get').mockImplementation(async (path) => {
    if (path === '/api/settings/routines') {
      return { workspace_context: { workspace_root: '/Users/me/ciaobot', vault_root: '/Users/me/ciaobot/memory-vault' } } as never
    }
    throw new Error('Unrelated settings data unavailable in this test')
  })
  const wrapper = mount(SettingsView, { global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: stub } } })
  try {
    await flushPromises()
    const card = wrapper.find('.workspace-root-path').element.closest('.card') as HTMLElement
    // The engine finds `.env` inside this folder, so a CIAO_WORKSPACE line in
    // that file can never move it; the service definition has to be repointed.
    expect(card.textContent).not.toContain('CIAO_WORKSPACE')
    expect(card.textContent).toContain('ciao service stop')
    expect(card.textContent).toContain('ciao setup --workspace <new folder> --yes --load-launchd')
    expect(card.textContent).toContain('/Users/me/ciaobot')
  } finally {
    wrapper.unmount()
  }
})
