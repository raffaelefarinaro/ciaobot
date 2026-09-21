// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { useProjectStore } from '../../stores/projects'
import App from '../../App.vue'

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/' }),
  useRouter: () => ({ push: vi.fn() }),
}))

// The banner is the only chrome shared by every screen, so App is mounted with
// its children stubbed: the point of these tests is the banner, not startup.
const stubs = {
  StartupView: true,
  RestartNotice: true,
  InAppToast: true,
  ConfirmDialog: true,
  PromptDialog: true,
  NewChatPicker: true,
  'router-view': true,
  'router-link': { template: '<a><slot /></a>' },
}

async function mountApp() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  const wrapper = mount(App, { global: { plugins: [pinia], stubs } })
  // Let the startup-status poll resolve so the client-mode banner renders.
  await vi.waitFor(() => {
    expect(wrapper.find('.client-mode-banner').exists()).toBe(true)
  })
  return { wrapper, store }
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({
      overall_ready: true,
      phases: [],
      node_role: 'client',
      host_url: 'https://100.101.252.27:8443',
      has_host_session: true,
    }),
  })))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('client-mode banner connectivity state', () => {
  it('names the host while the connection is healthy', async () => {
    const { wrapper } = await mountApp()
    const banner = wrapper.find('.client-mode-banner')
    expect(banner.text()).toContain('Client mode')
    expect(banner.classes()).not.toContain('is-offline')
    expect(banner.attributes('role')).toBe('status')
  })

  it('reports an unreachable host on every screen, not only inside a chat', async () => {
    // The "Can't reach the host" card lives in ChatPanel, so with no chat open
    // the home screen looked perfectly healthy while the host was gone.
    const { wrapper, store } = await mountApp()
    store.hostConnectionUnavailable = true
    await wrapper.vm.$nextTick()

    const banner = wrapper.find('.client-mode-banner')
    expect(banner.classes()).toContain('is-offline')
    expect(banner.attributes('role')).toBe('alert')
    expect(banner.text()).toContain('Can’t reach')
    expect(banner.text()).toContain('100.101.252.27:8443')
    // The way out stays reachable from the banner itself.
    expect(banner.text()).toContain('Switch to host')

    store.hostConnectionUnavailable = false
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.client-mode-banner').classes()).not.toContain('is-offline')
  })

  it('stands down while a ChatPanel carries the outage card itself', async () => {
    // Two alerts for one outage is a duplicate announcement for a screen
    // reader as much as a visual one. Keyed on the panel being mounted, not on
    // the URL: /chat with no id and the subagent sub-route are chat paths that
    // mount no panel, so they still need the banner.
    const { wrapper, store } = await mountApp()
    store.hostConnectionUnavailable = true
    store.chatPanelsMounted = 1
    await wrapper.vm.$nextTick()

    let banner = wrapper.find('.client-mode-banner')
    expect(banner.classes()).not.toContain('is-offline')
    expect(banner.text()).toContain('Client mode')

    // Leaving the chat hands the notice back to the banner.
    store.chatPanelsMounted = 0
    await wrapper.vm.$nextTick()
    banner = wrapper.find('.client-mode-banner')
    expect(banner.classes()).toContain('is-offline')
  })
})
