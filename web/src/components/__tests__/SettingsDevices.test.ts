// @vitest-environment jsdom
//
// Settings → General → "Other devices", mounted on its own. No SettingsView, no
// router, no Pinia: the card's only inputs are `api` and the clipboard, both
// stubbed here.
//
// The labels are the point of the card. A browser treats non-loopback plain
// HTTP as an insecure context, so a LAN URL can never install the app or
// deliver push notifications; only the configured HTTPS origin can. Claiming
// otherwise would send someone to a URL that silently cannot do what they were
// told.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import SettingsDevices from '../settings/SettingsDevices.vue'
import { api } from '../../lib/api'

const ADDRESSES = {
  port: 9443,
  trusted_url: 'https://mini.ts.net/',
  addresses: [
    { url: 'https://mini.ts.net/', kind: 'trusted' as const, secure: true, loopback: false },
    { url: 'http://192.168.1.20:9443/', kind: 'lan' as const, secure: false, loopback: false },
    { url: 'http://localhost:9443/', kind: 'loopback' as const, secure: false, loopback: true },
  ],
}

vi.mock('../../lib/api', () => ({
  api: {
    get: vi.fn(),
    patch: vi.fn(),
  },
}))

vi.mock('../../lib/codeCopy', () => ({
  writeClipboard: vi.fn(async () => true),
}))

beforeEach(() => {
  vi.mocked(api.get).mockReset().mockResolvedValue(ADDRESSES as never)
  vi.mocked(api.patch).mockReset().mockResolvedValue({ trusted_url: 'https://x.ts.net/' } as never)
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

  it('saves the trusted URL', async () => {
    const wrapper = await mountCard()
    await wrapper.find('#trusted-url').setValue('https://x.ts.net')
    await wrapper.find('form.device-trusted').trigger('submit')
    await flushPromises()

    expect(api.patch).toHaveBeenCalledWith('/api/settings/routines', {
      trusted_url: 'https://x.ts.net',
    })
  })

  it('shows the save error', async () => {
    vi.mocked(api.patch).mockRejectedValue(
      new Error('trusted_url must start with https://') as never,
    )
    const wrapper = await mountCard()
    await wrapper.find('#trusted-url').setValue('http://x')
    await wrapper.find('form.device-trusted').trigger('submit')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain(
      'trusted_url must start with https://',
    )
  })
})
