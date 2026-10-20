// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('../../lib/loopback', () => ({ isLoopbackPage: vi.fn() }))

import { api } from '../../lib/api'
import { isLoopbackPage } from '../../lib/loopback'
import { resetHostTerminalForTests } from '../../composables/useHostTerminal'
import HostTerminalButton from '../HostTerminalButton.vue'

const caps = {
  available: true,
  platform: 'macos',
  reason: '',
  terminal: 'Terminal',
  actions: ['claude-install', 'skills-update'],
}

async function mountButton(props: Record<string, unknown>) {
  const wrapper = mount(HostTerminalButton, { props })
  await flushPromises()
  return wrapper
}

describe('HostTerminalButton', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    resetHostTerminalForTests()
    vi.mocked(isLoopbackPage).mockReturnValue(true)
    vi.mocked(api.get).mockResolvedValue(caps)
  })

  it('renders nothing when the engine says unavailable', async () => {
    vi.mocked(api.get).mockResolvedValue({ ...caps, available: false, actions: [] })
    const wrapper = await mountButton({ action: 'claude-install' })
    expect(wrapper.find('button').exists()).toBe(false)
    const folder = await mountButton({ chatId: 'c1' })
    expect(folder.find('button').exists()).toBe(false)
  })

  it('shows Run in Terminal for an action and posts it on click', async () => {
    vi.mocked(api.post).mockResolvedValue({ ok: true, platform: 'macos', launched: 'Terminal' })
    const wrapper = await mountButton({ action: 'claude-install' })
    expect(wrapper.get('button').text()).toBe('Run in Terminal')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/host/terminal', { action: 'claude-install' })
    expect(wrapper.get('[role=status]').text()).toBe('Opened in Terminal')
  })

  it('shows Open in Terminal for a folder and posts the chat id', async () => {
    vi.mocked(api.post).mockResolvedValue({ ok: true, platform: 'macos', launched: 'Terminal' })
    const wrapper = await mountButton({ chatId: 'c1' })
    expect(wrapper.get('button').text()).toBe('Open in Terminal')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/host/terminal', { open: 'folder', chat_id: 'c1' })
  })

  it('shows the engine error inline with role=alert and re-enables the button', async () => {
    vi.mocked(api.post).mockRejectedValue({ payload: { error: 'No supported terminal app was found' } })
    const wrapper = await mountButton({ action: 'skills-update' })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role=alert]').text()).toContain('No supported terminal app was found')
    expect(wrapper.get('button').attributes('disabled')).toBeUndefined()
  })

  it('tells its parent when the engine says there is nothing to update', async () => {
    vi.mocked(api.post).mockRejectedValue({ status: 409, payload: { ok: false, reason: 'not_applicable', error: 'opencode-update: nothing to update' } })
    const wrapper = await mountButton({ action: 'skills-update' })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('not-applicable')).toHaveLength(1)
  })

  it('does not emit not-applicable for other failures', async () => {
    vi.mocked(api.post).mockRejectedValue({ status: 409, payload: { ok: false, reason: 'no_terminal', error: 'x' } })
    const wrapper = await mountButton({ action: 'skills-update' })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('not-applicable')).toBeUndefined()
  })

  it('disables itself while the launch is in flight', async () => {
    let resolve: (v: unknown) => void = () => {}
    vi.mocked(api.post).mockReturnValue(new Promise((r) => { resolve = r }))
    const wrapper = await mountButton({ action: 'claude-install' })
    await wrapper.get('button').trigger('click')
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    expect(wrapper.get('button').attributes('aria-busy')).toBe('true')
    expect(wrapper.get('button').text()).toBe('Opening…')
    resolve({ ok: true, platform: 'macos', launched: 'Terminal' })
    await flushPromises()
    expect(wrapper.get('button').attributes('disabled')).toBeUndefined()
  })

  it('uses the link style by default', async () => {
    const wrapper = await mountButton({ action: 'claude-install', variant: 'link' })
    expect(wrapper.get('button').classes()).toContain('host-terminal-link')
  })

  it('uses the small button style for the small variant', async () => {
    const wrapper = await mountButton({ action: 'claude-install', variant: 'small' })
    expect(wrapper.get('button').classes()).toContain('btn-small')
  })

  it('uses a native type=button control', async () => {
    const wrapper = await mountButton({ action: 'claude-install', variant: 'small' })
    const button = wrapper.get('button')
    expect(button.element.tagName).toBe('BUTTON')
    expect(button.attributes('type')).toBe('button')
    expect(button.classes()).toContain('btn-small')
  })
})
