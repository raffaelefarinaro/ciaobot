// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsNotifications from '../settings/SettingsNotifications.vue'
import { api } from '../../lib/api'

vi.mock('../../lib/push', async () => {
  // The card now calls the shared helper instead of doing the POST itself, so
  // the mock has to keep making it: these tests watch the request, not the call.
  const { api } = await import('../../lib/api')
  const currentSubscription = async () => ({ endpoint: 'https://push.example/1' })
  return {
    pushSupported: () => true,
    isPushEnabled: async () => true,
    currentSubscription,
    enablePush: vi.fn(),
    disablePush: vi.fn(),
    sendTestNotification: async () => {
      const sub = await currentSubscription()
      if (!sub) return false
      await api.post('/api/push/test', { endpoint: sub.endpoint })
      return true
    },
  }
})

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn(async (path: string) => {
      if (path === '/api/settings/routines') return { push_all_devices: false }
      if (path.startsWith('/api/push/subscription')) return { registered: true, count: 1 }
      return {}
    }),
    post: vi.fn(async () => ({ ok: true })),
    patch: vi.fn(async () => ({ push_all_devices: true })),
  },
}))

let wrapper: VueWrapper | null = null

const MAC_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

async function mountCard() {
  wrapper = mount(SettingsNotifications)
  await flushPromises()
  return wrapper
}

function button(view: VueWrapper, label: string) {
  const match = view.findAll('button').find((b) => b.text() === label)
  if (!match) throw new Error(`no button labelled ${label}`)
  return match
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
  delete (navigator as unknown as Record<string, unknown>).userAgent
})

describe('SettingsNotifications push controls', () => {
  it('sends a test notification to this device', async () => {
    const view = await mountCard()

    await button(view, 'Send test notification').trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('/api/push/test', { endpoint: 'https://push.example/1' })
    // Accepted by the push service is not proof of display; say so.
    expect(view.text()).toContain('Sent.')
  })

  it('toggles delivery to every device', async () => {
    const view = await mountCard()
    expect(view.text()).toContain('Other devices only')

    await button(view, 'Push to every device').trigger('click')
    await flushPromises()

    expect(api.patch).toHaveBeenCalledWith('/api/settings/routines', { push_all_devices: true })
    expect(view.text()).toContain('Every device, including this computer')
  })

  it('says the menu bar covers a Mac while Delivery is off', async () => {
    Object.defineProperty(navigator, 'userAgent', { value: MAC_UA, configurable: true })
    const view = await mountCard()

    await button(view, 'Send test notification').trigger('click')
    await flushPromises()

    // The test reaches this browser even with Delivery off, but its chat
    // banners come from the menu bar, so the text must not overclaim.
    expect(view.text()).toContain(
      "Sent. This Mac's chat notifications come from the menu bar unless Delivery is set to every device.",
    )
  })
})
