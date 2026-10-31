// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick } from 'vue'
import { createRouter, createWebHistory, type Router } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import FileViewerModal from '../FileViewerModal.vue'
import * as confirmLib from '../../lib/confirm'
import { pendingConfirm } from '../../lib/confirm'
import { OVERLAY_STATE_KEY } from '../../composables/useOverlayBackEntry'
import { useFileViewerStore } from '../../stores/fileViewer'
import { useProjectStore } from '../../stores/projects'

let wrapper: ReturnType<typeof mount> | null = null
let router: Router
let opener: HTMLButtonElement

const Empty = defineComponent({ render: () => h('div') })

async function settle() {
  await nextTick()
  await nextTick()
  await new Promise<void>(resolve => setTimeout(resolve, 0))
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
  await settle()
  await flushPromises()
}

async function openViewer(options: { dirty?: boolean } = {}) {
  window.history.replaceState(null, '', '/')
  router = createRouter({
    history: createWebHistory(),
    routes: [{ path: '/', component: Empty }, { path: '/chat/:chatId', component: Empty }],
  })
  await router.push('/chat/c1')
  await router.isReady()
  const store = useFileViewerStore()
  store.kind = 'text'
  store.path = 'notes/today.md'
  store.line = null
  store.content = 'Hello'
  store.loading = false
  store.error = ''
  store.chatId = 'chat-1'
  if (options.dirty) {
    store.editing = true
    store.editBuffer = 'Unsaved'
  }
  opener = document.createElement('button')
  document.body.append(opener)
  opener.focus()
  wrapper = mount(FileViewerModal, {
    attachTo: document.body,
    global: { plugins: [router] },
  })
  store.isOpen = true
  store.loadToken++
  await settle()
  await flushPromises()
  return store
}

describe('FileViewerModal and the Back button', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.stubGlobal('innerWidth', 600)
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    })
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
  })

  afterEach(async () => {
    pendingConfirm.value?.resolve(false)
    wrapper?.unmount()
    wrapper = null
    opener?.remove()
    await flushPromises()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('Back closes the viewer, keeps the address and returns focus to the opener', async () => {
    const store = await openViewer()
    expect(store.isOpen).toBe(true)
    expect(typeof marker()).toBe('string')
    await back()
    expect(store.isOpen).toBe(false)
    expect(wrapper!.find('.fv-modal').exists()).toBe(false)
    expect(window.location.pathname).toBe('/chat/c1')
    expect(router.currentRoute.value.path).toBe('/chat/c1')
    expect(document.activeElement).toBe(opener)
  })

  it('Back over unsaved edits asks first and keeps the viewer and its entry when declined', async () => {
    const ask = vi.spyOn(confirmLib, 'askConfirm').mockResolvedValue(false)
    const store = await openViewer({ dirty: true })
    const id = marker()
    expect(id).not.toBeNull()
    await back()
    if (marker() === null) {
      await nextPopstate()
      await settle()
    }
    expect(ask).toHaveBeenCalled()
    expect(store.isOpen).toBe(true)
    expect(marker()).toBe(id)
  })

  it("Back on the chat's shared pin closes it without unpinning", async () => {
    const projects = useProjectStore()
    const unpin = vi.spyOn(projects, 'unpinFile').mockResolvedValue()
    const store = await openViewer()
    store.sharedPinChatId = 'chat-1'
    await settle()
    await back()
    expect(store.isOpen).toBe(false)
    expect(unpin).not.toHaveBeenCalled()
  })

  it('x removes the entry it added', async () => {
    const store = await openViewer()
    expect(marker()).not.toBeNull()
    const popped = nextPopstate()
    store.close()
    await popped
    await settle()
    await flushPromises()
    expect(marker()).toBeNull()
    expect(window.location.pathname).toBe('/chat/c1')
  })
})
