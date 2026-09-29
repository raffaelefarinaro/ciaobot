// @vitest-environment jsdom
//
// Settings → General → "Other devices", mounted on its own. No SettingsView, no
// router, no Pinia: the card's only inputs are `api` and the clipboard, both
// stubbed here.
//
// The labels are the point of the card. A browser treats non-loopback plain
// HTTP as an insecure context, so a LAN URL can never install the app or
// deliver push notifications; only an HTTPS origin can. Claiming
// otherwise would send someone to a URL that silently cannot do what they were
// told.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import SettingsDevices from '../settings/SettingsDevices.vue'
import { api } from '../../lib/api'

const ADDRESSES = {
  port: 9443,
  addresses: [
    { url: 'https://mini.ts.net/', kind: 'trusted' as const, secure: true, loopback: false },
    { url: 'http://192.168.1.20:9443/', kind: 'lan' as const, secure: false, loopback: false },
    { url: 'http://localhost:9443/', kind: 'loopback' as const, secure: false, loopback: true },
  ],
}

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn(),
  },
}))

vi.mock('../../lib/codeCopy', () => ({
  writeClipboard: vi.fn(async () => true),
}))

beforeEach(() => {
  vi.mocked(api.get).mockReset().mockResolvedValue(ADDRESSES as never)
})

async function mountCard() {
  const wrapper = mount(SettingsDevices)
  await flushPromises()
  return wrapper
}

describe('SettingsDevices', () => {
  it('lists the trusted URL first with full-app label and LAN as browser-only', async () => {
    const wrapper = await mountCard()
    const rows = wrapper.findAll('.device-row')

    expect(rows).toHaveLength(3)
    expect(rows[0].text()).toContain('https://mini.ts.net/')
    expect(rows[0].text()).toContain('Full app')
    expect(rows[1].text()).toContain('Browser only')
    expect(rows[2].text()).toContain('This computer only')
    // A phone scanning the loopback QR lands on its own device, so it is not
    // offered as a QR.
    expect(rows[2].findAll('.device-actions button')).toHaveLength(1)
  })

  it('shows a QR code for a shareable address', async () => {
    const wrapper = await mountCard()
    expect(wrapper.find('.device-qr').exists()).toBe(false)

    await wrapper.findAll('.device-actions button')[1].trigger('click')
    expect(wrapper.find('.device-qr-code svg').exists()).toBe(true)
  })

  it('marks a Tailscale Serve address and stops explaining how to set it up', async () => {
    vi.mocked(api.get).mockResolvedValue({
      port: 9443,
      addresses: [
        {
          url: 'https://mini.tail1.ts.net/',
          kind: 'trusted',
          source: 'tailscale',
          secure: true,
          loopback: false,
        },
        ADDRESSES.addresses[1],
      ],
    } as never)
    const wrapper = await mountCard()

    const first = wrapper.findAll('.device-row')[0]
    expect(first.text()).toContain('Full app')
    expect(first.text()).toContain('via Tailscale Serve')
    expect(wrapper.text()).not.toContain('tailscale serve --bg')
    expect(wrapper.find('#trusted-url').exists()).toBe(false)
  })

  it('tells how to get an automatic address when Tailscale Serve is not set up', async () => {
    const wrapper = await mountCard()
    expect(wrapper.text()).toContain('tailscale serve --bg 9443')
    expect(wrapper.text()).toContain('finds the address on its own')
    expect(wrapper.text()).not.toContain('via Tailscale Serve')
  })
})
