// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import EngineOfflineView from '../EngineOfflineView.vue'

// The curtain registers a capture-phase keydown listener on `window` while it is
// up. A wrapper that is never unmounted leaves that listener behind, and it goes
// on swallowing shortcuts for the rest of the file -- so the test that checks the
// curtain *stops* swallowing them would be reading other tests' leftovers.
enableAutoUnmount(afterEach)

type ViewProps = {
  state: 'updating' | 'unreachable'
  loopback: boolean
  host: string
  retrying: boolean
}

const baseProps: ViewProps = {
  state: 'unreachable',
  loopback: true,
  host: 'localhost:8543',
  retrying: false,
}

function mountView(props: Partial<ViewProps> = {}) {
  return mount(EngineOfflineView, { props: { ...baseProps, ...props } })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('engine offline screen', () => {
  it('names the terminal commands when the engine is on this computer', () => {
    // On loopback the fix is a command, so it is shown verbatim rather than
    // described.
    const text = mountView().text()
    expect(text).toContain("Ciaobot isn't running")
    expect(text).toContain('ciao service start')
    expect(text).toContain('ciao service status')
    expect(text).toContain('.runtime/ciao.stderr.log')
  })

  it('names the host instead of the commands anywhere else', () => {
    const text = mountView({ loopback: false, host: 'mini.tailnet.ts.net' }).text()
    expect(text).toContain("Ciaobot on mini.tailnet.ts.net isn't reachable")
    // A remote browser has no terminal on the host, so offering `ciao service
    // start` there would be advice the reader cannot take.
    expect(text).not.toContain('ciao service start')
  })

  it('says restarting, with no commands, during an announced restart', () => {
    const text = mountView({ state: 'updating' }).text()
    expect(text).toContain('Ciaobot is restarting')
    expect(text).not.toContain('ciao service start')
  })

  it('offers Retry and reports that it is already checking', async () => {
    const wrapper = mountView()
    await wrapper.find('.engine-offline-actions .btn-primary').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
    expect(wrapper.text()).toContain('Reconnecting automatically')

    await wrapper.setProps({ retrying: true })
    const button = wrapper.find('.engine-offline-actions .btn-primary')
    expect(button.attributes('disabled')).toBeDefined()
    expect(button.text()).toContain('Checking')
  })

  it('copies a command to the clipboard', async () => {
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    const wrapper = mountView()
    const [firstCopy] = wrapper.findAll('.engine-offline-cmd button')
    await firstCopy.trigger('click')
    await wrapper.vm.$nextTick()

    expect(writeText).toHaveBeenCalledWith('ciao service start')
    expect(firstCopy.text()).toBe('Copied')
  })

  it('takes focus so the curtain is usable from the keyboard', () => {
    // Mounted on the document: focus() is a no-op on a detached tree, which is
    // exactly the case a mount() without attachTo would leave us in.
    const wrapper = mount(EngineOfflineView, {
      props: { ...baseProps },
      attachTo: document.body,
    })
    // The app behind the curtain stays mounted, so without this the first Tab
    // would land on a control the user cannot see.
    const retry = wrapper.find('.engine-offline-actions .btn-primary').element
    expect(document.activeElement).toBe(retry)
    wrapper.unmount()
  })

  it('suppresses global shortcuts while mounted', () => {
    // ChatLayout's bare 1-9, Esc and modifier shortcuts are still live behind
    // the curtain, and nothing there is inert: without this the first key press
    // switches workspace under a screen the user cannot see.
    const wrapper = mount(EngineOfflineView, {
      props: { ...baseProps },
      attachTo: document.body,
    })
    const spy = vi.fn()
    window.addEventListener('keydown', spy)
    try {
      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: '1', bubbles: true }))
      expect(spy).not.toHaveBeenCalled()

      // The listener dies with the curtain: a normal page must still hear keys.
      wrapper.unmount()
      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: '1', bubbles: true }))
      expect(spy).toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', spy)
    }
  })

  it('traps Tab focus inside the curtain', () => {
    const wrapper = mount(EngineOfflineView, {
      props: { ...baseProps },
      attachTo: document.body,
    })
    const buttons = wrapper.findAll('button')
    const first = buttons[0].element as HTMLElement
    const last = buttons[buttons.length - 1].element as HTMLElement
    expect(first).not.toBe(last)

    // Forward from the last control wraps to the first...
    last.focus()
    expect(document.activeElement).toBe(last)
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    expect(document.activeElement).toBe(first)

    // ...and back again, so neither end walks into the app behind the curtain.
    document.body.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }),
    )
    expect(document.activeElement).toBe(last)
    wrapper.unmount()
  })
})
