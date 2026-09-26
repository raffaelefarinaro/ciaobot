// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import App from '../../App.vue'
import { useProjectStore } from '../../stores/projects'

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

// A named stub, for the tests that have to tell the boot view and the curtain
// apart: the default `StartupView: true` renders `<startup-view-stub>`.
const startupStub = { template: '<div class="startup-stub" />' }

async function mountApp(extraStubs: Record<string, unknown> = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(App, {
    global: { plugins: [pinia], stubs: { ...stubs, ...extraStubs } },
  })
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

  it('shows the curtain instead of an empty StartupView on a cold launch with the engine down', async () => {
    // With the engine down, /api/startup-status never answers, so the boot view
    // has nothing to render: phases, progress and a Skip button around an
    // engine that is simply not there.
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }))
    const wrapper = await mountApp({ StartupView: startupStub })

    engine.onChange?.('unreachable')
    await nextTick()

    expect(wrapper.find('.engine-offline-stub').exists()).toBe(true)
    expect(wrapper.find('.startup-stub').exists()).toBe(false)
  })

  it('shows boot progress again when the engine comes back booting after an outage', async () => {
    // The curtain lifting on the first non-failure probe would drop the user
    // into a half-started engine; the boot view has to stay up until
    // overall_ready.
    const wrapper = await mountApp({ StartupView: startupStub })
    expect(wrapper.find('.engine-offline-stub').exists()).toBe(false)

    engine.onChange?.('unreachable')
    await nextTick()
    expect(wrapper.find('.engine-offline-stub').exists()).toBe(true)

    engine.onChange?.('booting')
    await nextTick()

    expect(wrapper.find('.startup-stub').exists()).toBe(true)
    expect(wrapper.find('.engine-offline-stub').exists()).toBe(false)
  })

  it('nudges sockets when the engine recovers', async () => {
    await mountApp()
    const nudge = vi.spyOn(useProjectStore(), 'reconnectNow')

    engine.onChange?.('unreachable')
    await nextTick()
    expect(nudge).not.toHaveBeenCalled()

    // Both sockets may be in a 2s-64s backoff, or the chat one gone for good
    // after five failed handshakes, so recovery has to reset that.
    engine.onChange?.('ready')
    await nextTick()
    expect(nudge).toHaveBeenCalledTimes(1)

    // A login prompt is not a recovery: nothing was listening to reconnect.
    engine.onChange?.('auth_required')
    await nextTick()
    expect(nudge).toHaveBeenCalledTimes(1)
    nudge.mockRestore()
  })
})
