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

describe('SettingsNotifications in the desktop client', () => {
  it('says the Mac app handles notifications in the desktop client', async () => {
    ;(globalThis as Record<string, unknown>).__CIAOBOT_DESKTOP__ = true
    const view = await mountCard()

    expect(view.text()).toContain('Handled by the Mac app')
    expect(view.text()).toContain('System Settings')
    expect(view.text()).not.toContain('Not available in this browser')
    const toggles = view.findAll('button').filter(b => {
      const text = b.text()
      return text === 'Enable on this device' || text === 'Disable on this device'
    })
    expect(toggles).toHaveLength(0)
    expect(api.get).not.toHaveBeenCalled()
  })
})
