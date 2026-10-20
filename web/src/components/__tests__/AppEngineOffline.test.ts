// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import App from '../../App.vue'
import { useProjectStore } from '../../stores/projects'
import { PROBE_TIMEOUT_MS } from '../../lib/engineStatus'

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/' }),
  useRouter: () => ({ push: vi.fn() }),
}))

// The monitor is a pure module with its own tests; here the only thing that
// matters is the wiring: which states reach the screen, and that the screen
// lands over a route that stays mounted.
const engine = vi.hoisted(() => ({
  onChange: null as null | ((state: string) => void),
  isAlive: null as null | (() => boolean),
  start: vi.fn(),
  stop: vi.fn(),
  retry: vi.fn(async () => {}),
}))

vi.mock('../../lib/engineStatus', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/engineStatus')>()
  return {
    ...actual,
    createEngineMonitor: (opts: {
      onChange: (state: string) => void
      isAlive?: () => boolean
    }) => {
      engine.onChange = opts.onChange
      engine.isAlive = opts.isAlive ?? null
      return {
        start: engine.start,
        stop: engine.stop,
        retry: engine.retry,
        get state() {
          return 'ready'
        },
      }
    },
  }
})

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
  engine.onChange = null
  engine.start.mockClear()
  engine.stop.mockClear()
  engine.retry.mockReset().mockResolvedValue(undefined)
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

  it('does not flash startup UI when a ready response arrives before the display delay', async () => {
    vi.useFakeTimers()
    const wrapper = await mountApp({ StartupView: startupStub })

    expect(wrapper.find('.startup-stub').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(200)
    expect(wrapper.find('.startup-stub').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows boot phases after the short delay and does not poll twice when the delay expires', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async () => ({
      status: 200,
      ok: true,
      json: async () => ({ overall_ready: false, phases: [{ name: 'engine', status: 'in_progress' }] }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = await mountApp({ StartupView: startupStub })

    expect(wrapper.find('.startup-stub').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(200)
    expect(wrapper.find('.startup-stub').exists()).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('ignores a late startup result after unmount', async () => {
    vi.useFakeTimers()
    let resolveFetch: ((value: { status: number; ok: boolean; json: () => Promise<unknown> }) => void) | undefined
    const fetchMock = vi.fn(() => new Promise<{ status: number; ok: boolean; json: () => Promise<unknown> }>((resolve) => {
      resolveFetch = resolve
    }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = await mountApp({ StartupView: startupStub })

    await vi.advanceTimersByTimeAsync(200)
    expect(wrapper.find('.startup-stub').exists()).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    wrapper.unmount()
    resolveFetch?.({ status: 200, ok: true, json: async () => ({ overall_ready: true, phases: [] }) })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each([401, 403])('keeps a %i auth challenge clear of the startup curtain and stops startup polling', async (status) => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async () => ({ status, ok: false, json: async () => ({}) }))
    vi.stubGlobal('fetch', fetchMock)
    engine.retry.mockImplementationOnce(async () => { engine.onChange?.('auth_required') })
    const wrapper = await mountApp({ StartupView: startupStub })

    await vi.advanceTimersByTimeAsync(200)
    expect(wrapper.find('.startup-stub').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('resumes startup status after the engine monitor clears an auth challenge', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ status: 401, ok: false, json: async () => ({}) })
      .mockResolvedValueOnce({ status: 200, ok: true, json: async () => ({ overall_ready: true, phases: [] }) })
    vi.stubGlobal('fetch', fetchMock)
    engine.retry.mockImplementationOnce(async () => { engine.onChange?.('auth_required') })
    const wrapper = await mountApp({ StartupView: startupStub })
    await vi.advanceTimersByTimeAsync(200)
    expect(wrapper.find('.startup-stub').exists()).toBe(false)

    engine.onChange?.('ready')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.startup-stub').exists()).toBe(false)
    wrapper.unmount()
  })

  it('retries a startup request after its real abort deadline', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new Error('aborted')))
    }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = await mountApp({ StartupView: startupStub })

    await vi.advanceTimersByTimeAsync(200)
    expect(wrapper.find('.startup-stub').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(PROBE_TIMEOUT_MS)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1500)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(4000)
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

  it('tells the monitor that the socket can vouch for the engine', async () => {
    // The awareness socket carries a keepalive every few seconds, so a recent
    // frame is proof the engine is up. The monitor consults it so a slow
    // remote path never raises the "computer is off" curtain.
    const wrapper = await mountApp()
    // The monitor is handed the store's own answer, and the store has not
    // seen a frame yet, so the engine is not vouched for.
    expect(engine.isAlive).toBeTypeOf('function')
    expect(engine.isAlive?.()).toBe(false)
    wrapper.unmount()
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

  it('shows the curtain when the engine dies again while booting', async () => {
    // This is reachable in an already-loaded tab: after an outage the engine
    // comes back `booting`, the boot view returns, and a second crash lands
    // there. A stale boot view has no Retry and no recovery hint, so an
    // unreachable engine always gets the curtain instead.
    const wrapper = await mountApp({ StartupView: startupStub })

    engine.onChange?.('unreachable')
    await nextTick()
    // While the engine boots its startup-status never reports ready: keep the
    // boot view's restarted poll pending, or it would finish startup on its
    // own and hide the state this test is about.
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    engine.onChange?.('booting')
    await nextTick()
    expect(wrapper.find('.startup-stub').exists()).toBe(true)
    engine.onChange?.('unreachable')
    await nextTick()

    expect(wrapper.find('.engine-offline-stub').exists()).toBe(true)
    expect(wrapper.find('.startup-stub').exists()).toBe(false)
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
