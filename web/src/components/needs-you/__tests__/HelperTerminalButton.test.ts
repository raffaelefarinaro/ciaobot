// @vitest-environment jsdom

/**
 * `components/needs-you/HelperTerminalButton.vue`: shows an engine helper
 * terminal in the chat's terminal pane. It opens the pane without starting a
 * shell, then selects the helper session.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import HelperTerminalButton from '../HelperTerminalButton.vue'

const terminal = vi.hoisted(() => ({
  open: vi.fn(),
  selectSession: vi.fn(),
}))
vi.mock('../../../stores/terminal', () => ({ useTerminalStore: () => terminal }))

beforeEach(() => {
  terminal.open.mockReset().mockResolvedValue(undefined)
  terminal.selectSession.mockReset().mockReturnValue(true)
})

describe('HelperTerminalButton', () => {
  it('opens the chat terminal on the helper session', async () => {
    const wrapper = mount(HelperTerminalButton, { props: { chatId: 'c1', sessionId: 's1' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(terminal.open).toHaveBeenCalledTimes(1)
    expect(terminal.open).toHaveBeenCalledWith({ chatId: 'c1' }, { ensureSession: false })
    expect(terminal.selectSession).toHaveBeenCalledWith('s1')
    expect(terminal.open.mock.invocationCallOrder[0]).toBeLessThan(terminal.selectSession.mock.invocationCallOrder[0])
  })

  it('renders nothing without a session', () => {
    const wrapper = mount(HelperTerminalButton, { props: { chatId: 'c1', sessionId: '' } })
    expect(wrapper.find('button').exists()).toBe(false)
  })
})
