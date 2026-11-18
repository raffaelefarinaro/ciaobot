// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsNotifications from '../settings/SettingsNotifications.vue'
import { api } from '../../lib/api'

vi.mock('../../lib/push', () => ({
  pushSupported: () => true,
  isPushEnabled: async () => true,
  currentSubscription: async () => ({ endpoint: 'https://push.example/1' }),
  enablePush: vi.fn(),
  disablePush: vi.fn(),
}))

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn(async (path: string) => {
      if (path.startsWith('/api/push/subscription')) return { registered: true, count: 1 }
      return {}
    }),
    post: vi.fn(async () => ({ ok: true })),
    patch: vi.fn(),
  },
}))

let wrapper: VueWrapper | null = null

async function mountCard() {
  wrapper = mount(SettingsNotifications)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('Notification', { permission: 'granted', requestPermission: vi.fn() })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  delete (globalThis as Record<string, unknown>).__CIAOBOT_DESKTOP__
})

describe('SettingsNotifications push controls', () => {
  it('has no delivery row', async () => {
    const view = await mountCard()
    expect(view.text()).not.toContain('Delivery')
    expect(view.text()).not.toContain('Push to every device')
    expect(api.patch).not.toHaveBeenCalled()
  })
})

const g = globalThis as Record<string, unknown>

function snapshot(over: Record<string, unknown> = {}) {
  return {
    v: 1,
    notifications: {
      state: 'on', ready: true, action: 'toggle',
      title: 'Chat Notifications: On', detail: 'Delivering to this Mac.', ...over,
    },
  }
}

function installBridge(replies: unknown[] | ((req: unknown) => unknown)) {
  const queue = Array.isArray(replies) ? [...replies] : null
  const post = vi.fn(async (req: unknown) => {
    if (queue) return queue.length > 1 ? queue.shift() : queue[0]
    return (replies as (r: unknown) => unknown)(req)
  })
  g.webkit = { messageHandlers: { ciaobotAppSettings: { postMessage: post } } }
  return post
}

describe('SettingsNotifications in the desktop client', () => {
  beforeEach(() => {
    g.__CIAOBOT_DESKTOP__ = true
  })
  afterEach(() => {
    delete g.webkit
  })

  it('keeps the static Mac app text when the app has no bridge', async () => {
    const view = await mountCard()

    expect(view.text()).toContain('Handled by the Mac app')
    expect(view.text()).toContain('System Settings')
    expect(view.find('[role="switch"]').exists()).toBe(false)
    expect(view.findAll('button')).toHaveLength(0)
    expect(api.get).not.toHaveBeenCalled()
  })

  it('shows the tray title and an on switch', async () => {
    installBridge([snapshot()])
    const view = await mountCard()

    expect(view.text()).toContain('Chat Notifications: On')
    expect(view.text()).not.toContain('Handled by the Mac app')
    const sw = view.find('[role="switch"]')
    expect(sw.attributes('aria-checked')).toBe('true')
    expect(sw.attributes('aria-label')).toBe('Chat notifications')
  })

  it('posts one notifications request on click and renders the reply', async () => {
    const post = installBridge((req: unknown) =>
      (req as { op: string }).op === 'get'
        ? snapshot()
        : snapshot({ state: 'off', title: 'Chat Notifications: Off' }))
    const view = await mountCard()
    await view.find('[role="switch"]').trigger('click')
    await flushPromises()

    const actions = post.mock.calls.filter(c => (c[0] as { op: string }).op === 'notifications')
    expect(actions).toHaveLength(1)
    expect(actions[0]![0]).toEqual({ op: 'notifications' })
    expect(view.text()).toContain('Chat Notifications: Off')
    expect(view.find('[role="switch"]').attributes('aria-checked')).toBe('false')
  })

  it('re-reads when the app signals a change', async () => {
    const post = installBridge([snapshot(), snapshot({ state: 'off', title: 'Chat Notifications: Off' })])
    const view = await mountCard()
    expect(post).toHaveBeenCalledTimes(1)

    window.dispatchEvent(new Event('ciaobot-app-settings-changed'))
    await flushPromises()

    expect(post).toHaveBeenCalledTimes(2)
    expect(view.find('[role="switch"]').attributes('aria-checked')).toBe('false')
  })

  it('stops listening after unmount', async () => {
    const post = installBridge([snapshot()])
    const view = await mountCard()
    view.unmount()
    wrapper = null
    window.dispatchEvent(new Event('ciaobot-app-settings-changed'))
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('offers the System Settings pane when denied, and no switch', async () => {
    installBridge([snapshot({ state: 'denied', action: 'openSystemSettings', title: 'Chat Notifications: Blocked' })])
    const view = await mountCard()

    expect(view.find('[role="switch"]').exists()).toBe(false)
    expect(view.findAll('button').map(b => b.text())).toEqual(['Open System Settings'])
  })

  it('offers no control when unavailable', async () => {
    installBridge([snapshot({ state: 'unavailable', action: 'none', title: 'Chat Notifications: Unavailable' })])
    const view = await mountCard()

    expect(view.text()).toContain('Chat Notifications: Unavailable')
    expect(view.find('[role="switch"]').exists()).toBe(false)
    expect(view.findAll('button')).toHaveLength(0)
  })

  it('shows an alert when the app refuses the request', async () => {
    installBridge((req: unknown) => {
      if ((req as { op: string }).op === 'get') return snapshot()
      throw 'refused'
    })
    const view = await mountCard()
    await view.find('[role="switch"]').trigger('click')
    await flushPromises()

    const alert = view.find('[role="alert"]')
    expect(alert.text()).toContain('Ciaobot for Mac did not change notifications.')
    expect(view.find('[role="switch"]').attributes('aria-checked')).toBe('true')
  })
})
