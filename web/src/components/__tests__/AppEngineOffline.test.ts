// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import App from '../../App.vue'

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/' }),
  useRouter: () => ({ push: vi.fn() }),
}))

// The monitor is a pure module with its own tests; here the only thing that
// matters is the wiring: which states reach the screen, and that the screen
// lands over a route that stays mounted.
const engine = vi.hoisted(() => ({
  onChange: null as null | ((state: string) => void),
  start: vi.fn(),
  stop: vi.fn(),
  retry: vi.fn(async () => {}),
}))

vi.mock('../../lib/engineStatus', () => ({
  createEngineMonitor: (opts: { onChange: (state: string) => void }) => {
    engine.onChange = opts.onChange
    return {
      start: engine.start,
      stop: engine.stop,
      retry: engine.retry,
      get state() {
        return 'ready'
      },
    }
  },
}))

const stubs = {
  StartupView: true,
  RestartNotice: true,
  InAppToast: true,
  ConfirmDialog: true,
  PromptDialog: true,
  NewChatPicker: true,
  EngineOfflineView: { template: '<div class="engine-offline-stub" />' },
  'router-view': { template: '<div class="route-stub" />' },
  'router-link': { template: '<a><slot /></a>' },
}

async function mountApp() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(App, { global: { plugins: [pinia], stubs } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ overall_ready: true, phases: [] }),
  })))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('engine offline screen wiring', () => {
  it('hides while the engine answers and the startup view still owns the screen', async () => {
    const wrapper = await mountApp()
    expect(wrapper.find('.engine-offline-stub').exists()).toBe(false)
    expect(engine.start).toHaveBeenCalled()
  })

  it('covers the route without unmounting it, and clears on recovery', async () => {
    // A crash must not cost the open chat, its scroll position or its drafts,
    // so the screen is an overlay rather than a route change.
    const wrapper = await mountApp()
    engine.onChange?.('unreachable')
    await nextTick()

    expect(wrapper.find('.engine-offline-stub').exists()).toBe(true)
    expect(wrapper.find('.route-stub').exists()).toBe(true)

    engine.onChange?.('ready')
    await nextTick()
    expect(wrapper.find('.engine-offline-stub').exists()).toBe(false)
    expect(wrapper.find('.route-stub').exists()).toBe(true)
  })

  it('shows the screen for an announced restart, and stops polling on unmount', async () => {
    const wrapper = await mountApp()
    engine.onChange?.('updating')
    await nextTick()
    expect(wrapper.find('.engine-offline-stub').exists()).toBe(true)

    wrapper.unmount()
    expect(engine.stop).toHaveBeenCalled()
  })

  it('never shows the screen for an auth challenge', async () => {
    // A 401 is a login problem; the login flow owns it, and stacking the outage
    // screen on top of it would hide the only way back in.
    const wrapper = await mountApp()
    engine.onChange?.('auth_required')
    await nextTick()
    expect(wrapper.find('.engine-offline-stub').exists()).toBe(false)
  })
})
