// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref, type Ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createRouter, createWebHistory, type Router } from 'vue-router'
import { OVERLAY_STATE_KEY, useOverlayBackEntry } from '../useOverlayBackEntry'
import { pendingConfirm } from '../../lib/confirm'

const Empty = defineComponent({ render: () => h('div') })

function makeRouter(): Router {
  return createRouter({
    history: createWebHistory(),
    routes: [
      { path: '/', component: Empty },
      { path: '/tasks', component: Empty },
    ],
  })
}

function marker(): unknown {
  return window.history.state?.[OVERLAY_STATE_KEY] ?? null
}

function nextPopstate(): Promise<void> {
  return new Promise(resolve => window.addEventListener('popstate', () => resolve(), { once: true }))
}

async function back(): Promise<void> {
  const popped = nextPopstate()
  window.history.back()
  await popped
  await flushPromises()
}

interface Harness {
  router: Router
  wrapper: VueWrapper
  open: Ref<boolean>
  close: ReturnType<typeof vi.fn>
  afterEach: ReturnType<typeof vi.fn>
}

// A fresh router per test: the composable installs its guard once per router.
async function mountHost(closeResult = true): Promise<Harness> {
  const router = makeRouter()
  await router.push('/')
  await router.isReady()
  const open = ref(false)
  const close = vi.fn(async () => {
    if (!closeResult) return false
    open.value = false
    return true
  })
  const wrapper = mount(defineComponent({
    setup() {
      useOverlayBackEntry(() => open.value, close)
      return () => h('div')
    },
  }), { global: { plugins: [router] } })
  const after = vi.fn()
  router.afterEach(after)
  return { router, wrapper, open, close, afterEach: after }
}

let current: Harness | null = null

describe('useOverlayBackEntry', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/')
    pendingConfirm.value = null
  })

  afterEach(async () => {
    current?.wrapper.unmount()
    current = null
    await flushPromises()
  })

  it('adds one entry at the same address while an overlay is open', async () => {
    const h = current = await mountHost()
    const before = window.history.length
    h.open.value = true
    await flushPromises()
    expect(window.history.length).toBe(before + 1)
    expect(window.location.pathname).toBe('/')
    expect(typeof window.history.state[OVERLAY_STATE_KEY]).toBe('string')
    expect(h.afterEach).not.toHaveBeenCalled()
  })

  it('Back closes the top overlay and keeps the page', async () => {
    const h = current = await mountHost()
    h.open.value = true
    await flushPromises()
    await back()
    expect(h.close).toHaveBeenCalledTimes(1)
    expect(h.open.value).toBe(false)
    expect(window.location.pathname).toBe('/')
    expect(h.router.currentRoute.value.path).toBe('/')
  })

  it('a refused close keeps the overlay and puts its entry back', async () => {
    const h = current = await mountHost(false)
    h.open.value = true
    await flushPromises()
    const id = marker()
    expect(id).not.toBeNull()
    await back()
    // vue-router restores the entry with its own go(-delta): a second popstate.
    if (marker() === null) {
      await nextPopstate()
      await flushPromises()
    }
    expect(h.close).toHaveBeenCalledTimes(1)
    expect(marker()).toBe(id)
    expect(h.open.value).toBe(true)
  })

  it('closing any other way steps back without a navigation', async () => {
    const h = current = await mountHost()
    h.open.value = true
    await flushPromises()
    const popped = nextPopstate()
    h.open.value = false
    await popped
    await flushPromises()
    expect(marker()).toBeNull()
    expect(h.afterEach).not.toHaveBeenCalled()
    expect(window.history.state.forward).toBe(window.history.state.current)
    expect(h.close).not.toHaveBeenCalled()
  })

  it('a navigation right after a close starts from the page', async () => {
    const h = current = await mountHost()
    h.open.value = true
    await flushPromises()
    h.open.value = false
    await h.router.push('/tasks')
    expect(window.location.pathname).toBe('/tasks')
    await back()
    expect(window.location.pathname).toBe('/')
    expect(marker()).toBeNull()
  })

  it('a close inside a navigation leaves history to it', async () => {
    const h = current = await mountHost()
    h.router.beforeEach(() => { h.open.value = false })
    h.open.value = true
    await flushPromises()
    const go = vi.spyOn(h.router.options.history, 'go')
    await h.router.push('/tasks')
    await flushPromises()
    expect(go).not.toHaveBeenCalled()
    expect(window.location.pathname).toBe('/tasks')
    expect(h.router.currentRoute.value.path).toBe('/tasks')
  })

  it("Forward onto a closed overlay's entry bounces back", async () => {
    const h = current = await mountHost()
    h.open.value = true
    await flushPromises()
    const popped = nextPopstate()
    h.open.value = false
    await popped
    await flushPromises()
    const forwarded = nextPopstate()
    window.history.forward()
    await forwarded
    await flushPromises()
    // The refused navigation puts the position back on the page entry.
    await new Promise(resolve => setTimeout(resolve, 50))
    await flushPromises()
    expect(marker()).toBeNull()
    expect(h.router.currentRoute.value.path).toBe('/')
  })

  it('Back is refused while a confirm is pending', async () => {
    const h = current = await mountHost()
    h.open.value = true
    await flushPromises()
    pendingConfirm.value = { resolve: vi.fn() } as unknown as typeof pendingConfirm.value
    const id = marker()
    await back()
    if (marker() === null) {
      await nextPopstate()
      await flushPromises()
    }
    expect(h.close).not.toHaveBeenCalled()
    expect(h.open.value).toBe(true)
    expect(marker()).toBe(id)
  })

  it('unmounting with an open overlay releases its entry', async () => {
    const h = current = await mountHost()
    h.open.value = true
    await flushPromises()
    const popped = nextPopstate()
    h.wrapper.unmount()
    await popped
    await flushPromises()
    expect(marker()).toBeNull()
    expect(h.close).not.toHaveBeenCalled()
  })

  it('a marker left by a reload is cleared on install', async () => {
    window.history.replaceState({ [OVERLAY_STATE_KEY]: 'stale-1' }, '', '/')
    current = await mountHost()
    expect(marker()).toBeNull()
  })

  it('does nothing without a router', async () => {
    const open = ref(true)
    const wrapper = mount(defineComponent({
      setup() { useOverlayBackEntry(() => open.value, async () => true); return () => h('div') },
    }))
    const before = window.history.length
    await flushPromises()
    expect(window.history.length).toBe(before)
    wrapper.unmount()
  })
})
