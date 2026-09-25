// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const confirmMock = vi.fn()
const getMock = vi.fn()
const postMock = vi.fn()

vi.mock('../../lib/confirm', () => ({
  askConfirm: (message: string, options: unknown) => confirmMock(message, options),
}))
vi.mock('../../lib/api', () => ({
  api: {
    get: (path: string) => getMock(path),
    post: (path: string, body?: unknown) => postMock(path, body),
  },
}))

import DevicePanel from '../DevicePanel.vue'

const stubs = { NodeAddresses: true, ConnectedClients: true }

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ version: '1.2.3', overall_ready: true }) })))
  getMock.mockImplementation(async (path: string) => {
    if (path === '/api/node/status') throw new Error('unverifiable')
    if (path === '/api/device/package-status') return { update_available: false, current_version: '1.2.3', latest_version: '1.2.3' }
    if (path === '/api/native/sessions') return { sessions: [] }
    return {}
  })
  postMock.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.unstubAllGlobals()
  confirmMock.mockReset()
  getMock.mockReset()
  postMock.mockReset()
})

describe('DevicePanel', () => {
  it('shows an unverifiable role as text with sentence-case sections', async () => {
    const wrapper = mount(DevicePanel, { global: { stubs } })
    await flushPromises()
    const headings = wrapper.findAll('h3').map(h => h.text())
    expect(headings).toEqual(['Role', 'Local app'])
    expect(wrapper.text()).toContain('Current role')
    expect(wrapper.text()).toContain('Unknown')
    expect(wrapper.find('.badge').exists()).toBe(false)
    expect(wrapper.text()).toContain('Up to date')
  })

  it('asks before making this device the host from an unknown role', async () => {
    confirmMock.mockResolvedValue(false)
    const wrapper = mount(DevicePanel, { global: { stubs } })
    await flushPromises()
    const button = wrapper.findAll('button').find(b => b.text() === 'Make this the host…')!
    expect(button.classes()).toContain('btn-danger')
    await button.trigger('click')
    await flushPromises()
    expect(confirmMock).toHaveBeenCalledOnce()
    expect(postMock).not.toHaveBeenCalled()

    confirmMock.mockResolvedValue(true)
    await button.trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/node/handover', expect.objectContaining({ force: true }))
  })
})
