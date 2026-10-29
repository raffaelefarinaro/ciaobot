// @vitest-environment jsdom

/**
 * `components/BrowserTerminalButton.vue` — the chat's and the project's way
 * into the browser terminal.
 *
 * The button replaces the native host terminal on these two surfaces: it
 * opens the pane under the chat, in this browser, and it asks the engine for
 * the sessions of that one target. The native terminal keeps its own button
 * for the actions that need a real window on the engine's machine.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import BrowserTerminalButton from '../BrowserTerminalButton.vue'
import { useTerminalStore } from '../../stores/terminal'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: apiDel },
}))

function statusPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    available: true,
    reason: '',
    platform: 'macos',
    host: 'engine.local',
    unlock_expires_in: 900,
    detached_ttl_seconds: 600,
    ...overrides,
  }
}

describe('BrowserTerminalButton', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
    apiDel.mockReset()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
      return Promise.resolve({})
    })
  })

  afterEach(() => {
    useTerminalStore().dispose()
    document.body.innerHTML = ''
  })

  it('opens the terminal for a chat', async () => {
    const store = useTerminalStore()
    const wrapper = mount(BrowserTerminalButton, { props: { chatId: 'c1' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(store.visible).toBe(true)
    expect(store.target).toEqual({ chatId: 'c1' })
    expect(apiGet).toHaveBeenCalledWith('/api/terminal/status')
    wrapper.unmount()
  })

  it('opens the terminal for a project', async () => {
    const store = useTerminalStore()
    const wrapper = mount(BrowserTerminalButton, { props: { projectId: 'p1' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(store.visible).toBe(true)
    expect(store.target).toEqual({ projectId: 'p1' })
    wrapper.unmount()
  })

  it('starts a shell when the chat has none', async () => {
    apiPost.mockResolvedValue({
      session: {
        session_id: 's1', folder: '/w', cols: 80, rows: 24, seq: 0,
        attached: false, exited: false, exit_code: null,
      },
    })
    const wrapper = mount(BrowserTerminalButton, { props: { chatId: 'c1' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/terminal/sessions', { chat_id: 'c1' })
    wrapper.unmount()
  })

  it('does not open a native terminal window', async () => {
    const wrapper = mount(BrowserTerminalButton, { props: { chatId: 'c1' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()

    // The native host terminal is a different route, and this button does not
    // touch it: the shell runs in this browser, under the chat.
    expect(apiPost).not.toHaveBeenCalledWith('/api/host/terminal', expect.anything())
    wrapper.unmount()
  })
})
