// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsMacUpdates from '../settings/SettingsMacUpdates.vue'
import { MAC_APP_SETTINGS_CHANGED } from '../../lib/macAppSettings'

const g = globalThis as Record<string, unknown>

type Updates = { available: boolean; automatic: boolean; canCheck: boolean; reason: string }
const UP: Updates = { available: true, automatic: true, canCheck: true, reason: '' }

/** Installs a bridge whose `get` answers with `updates` and whose actions answer
 *  with the same block, updated by the request: a started check reads as
 *  running (`canCheck` false), and an automatic change lands in `automatic`. */
function install(initial: Updates | null, opts: { refuseActions?: boolean } = {}) {
  let updates = initial
  const postMessage = vi.fn(async (request: { op: string; on?: boolean }) => {
    if (request.op === 'get') return { v: 1, notifications: null, updates }
    if (opts.refuseActions) throw 'refused'
    if (request.op === 'checkForUpdates' && updates) updates = { ...updates, canCheck: false }
    if (request.op === 'setAutomaticUpdates' && updates) updates = { ...updates, automatic: request.on === true }
    return { v: 1, notifications: null, updates }
  })
  g.webkit = { messageHandlers: { ciaobotAppSettings: { postMessage } } }
  return {
    postMessage,
    setUpdates(next: Updates | null) { updates = next },
  }
}

let wrapper: VueWrapper | null = null

async function mountRow() {
  wrapper = mount(SettingsMacUpdates, { attachTo: document.body })
  await flushPromises()
  return wrapper
}

function switchEl() {
  return document.querySelector<HTMLButtonElement>('button[role="switch"]')!
}
function checkButton() {
  return [...document.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent?.includes('Check for Updates'))!
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  delete g.webkit
})

beforeEach(() => {
  vi.clearAllMocks()
})

describe('SettingsMacUpdates', () => {
  it('renders nothing without the app bridge', async () => {
    const view = await mountRow()
    expect(view.find('.mac-updates').exists()).toBe(false)
    expect(view.text()).toBe('')
  })

  it('renders nothing when the app sends no updates block', async () => {
    install(null)
    const view = await mountRow()
    expect(view.text()).toBe('')
  })

  it('shows an automatic install as the status with the switch on', async () => {
    install(UP)
    const view = await mountRow()
    expect(view.text()).toContain('Ciaobot for Mac')
    expect(view.text()).toContain('Installs updates automatically')
    expect(switchEl().getAttribute('aria-checked')).toBe('true')
    expect(checkButton().disabled).toBe(false)
  })

  it('shows the manual state with the switch off', async () => {
    install({ ...UP, automatic: false })
    const view = await mountRow()
    expect(view.text()).toContain('Asks before installing updates')
    expect(switchEl().getAttribute('aria-checked')).toBe('false')
  })

  it('disables both controls and shows the native reason as text when unavailable', async () => {
    const reason = 'Updates are unavailable in a debug build.<b>x</b>'
    install({ available: false, automatic: false, canCheck: false, reason })
    const view = await mountRow()
    expect(view.text()).toContain('Updates are unavailable')
    expect(view.text()).toContain(reason)
    expect(view.find('b').exists()).toBe(false)
    expect(switchEl().disabled).toBe(true)
    expect(checkButton().disabled).toBe(true)
    expect(switchEl().getAttribute('aria-describedby')).toBe('mac-updates-reason')
  })

  it('disables the check while a check is running and keeps the switch usable', async () => {
    install({ ...UP, canCheck: false })
    const view = await mountRow()
    expect(view.text()).toContain('Checking for updates')
    expect(checkButton().disabled).toBe(true)
    expect(switchEl().disabled).toBe(false)
  })

  it('sends checkForUpdates on a click and renders the reply', async () => {
    const bridge = install(UP)
    const view = await mountRow()
    checkButton().click()
    await flushPromises()
    expect(bridge.postMessage).toHaveBeenLastCalledWith({ op: 'checkForUpdates' })
    // The reply says the check is running: the status and the button must follow it.
    expect(view.text()).toContain('Checking for updates')
    expect(checkButton().disabled).toBe(true)
  })

  it('sends setAutomaticUpdates with the flipped value and shows the new state', async () => {
    const bridge = install(UP)
    const view = await mountRow()
    switchEl().click()
    await flushPromises()
    expect(bridge.postMessage).toHaveBeenLastCalledWith({ op: 'setAutomaticUpdates', on: false })
    expect(view.text()).toContain('Asks before installing updates')
    expect(switchEl().getAttribute('aria-checked')).toBe('false')
  })

  it('shows a refused action as an alert and keeps the last state', async () => {
    install({ ...UP, automatic: false }, { refuseActions: true })
    const view = await mountRow()
    switchEl().click()
    await flushPromises()
    const alert = view.find('[role="alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('did not change this setting')
    expect(switchEl().getAttribute('aria-checked')).toBe('false')
  })

  it('names the tray row in the refused check copy', async () => {
    install(UP, { refuseActions: true })
    const view = await mountRow()
    checkButton().click()
    await flushPromises()
    expect(view.find('[role="alert"]').text()).toContain('Ciaobot > Settings > Check for Updates…')
  })

  it('still reports a refused action when a read superseded it in flight', async () => {
    let refuse!: () => void
    const refusal = new Promise<never>((_, reject) => { refuse = () => reject('refused') })
    const postMessage = vi.fn(async (request: { op: string }) => {
      if (request.op === 'get') return { v: 1, notifications: null, updates: UP }
      return refusal
    })
    g.webkit = { messageHandlers: { ciaobotAppSettings: { postMessage } } }
    const view = await mountRow()
    switchEl().click()
    await flushPromises()
    // A change event re-reads the state while the action is still pending.
    window.dispatchEvent(new Event(MAC_APP_SETTINGS_CHANGED))
    await flushPromises()
    refuse()
    await flushPromises()
    expect(view.find('[role="alert"]').text()).toContain('did not change this setting')
  })

  it('keeps the last good state through a refused read', async () => {
    const bridge = install(UP)
    const view = await mountRow()
    bridge.postMessage.mockRejectedValueOnce('refused')
    window.dispatchEvent(new Event(MAC_APP_SETTINGS_CHANGED))
    await flushPromises()
    expect(view.text()).toContain('Installs updates automatically')
    expect(switchEl().getAttribute('aria-checked')).toBe('true')
  })

  it('keeps the last good state through a malformed read', async () => {
    const bridge = install(UP)
    const view = await mountRow()
    bridge.postMessage.mockResolvedValueOnce({ v: 2, notifications: null, updates: UP })
    window.dispatchEvent(new Event(MAC_APP_SETTINGS_CHANGED))
    await flushPromises()
    expect(view.text()).toContain('Installs updates automatically')
  })

  it('hides the row when the bridge is gone', async () => {
    install(UP)
    const view = await mountRow()
    delete g.webkit
    window.dispatchEvent(new Event(MAC_APP_SETTINGS_CHANGED))
    await flushPromises()
    expect(view.text()).toBe('')
  })

  it('re-reads the snapshot on the change event', async () => {
    const bridge = install(UP)
    const view = await mountRow()
    bridge.setUpdates({ ...UP, automatic: false })
    window.dispatchEvent(new Event(MAC_APP_SETTINGS_CHANGED))
    await flushPromises()
    expect(view.text()).toContain('Asks before installing updates')
  })

  it('hides the row when a later read has no updates block', async () => {
    const bridge = install(UP)
    const view = await mountRow()
    bridge.setUpdates(null)
    window.dispatchEvent(new Event(MAC_APP_SETTINGS_CHANGED))
    await flushPromises()
    expect(view.text()).toBe('')
  })

  it('stops listening once unmounted', async () => {
    const bridge = install(UP)
    const view = await mountRow()
    view.unmount()
    wrapper = null
    const reads = bridge.postMessage.mock.calls.length
    window.dispatchEvent(new Event(MAC_APP_SETTINGS_CHANGED))
    await flushPromises()
    expect(bridge.postMessage.mock.calls.length).toBe(reads)
  })
})

describe('SettingsMacUpdates disabled check button', () => {
  // jsdom does not apply the component's scoped stylesheet, so this guard reads
  // the disabled rule from the component's own <style> block and checks the
  // properties that make a disabled button read as disabled.
  const source = readFileSync(join(__dirname, '..', 'settings', 'SettingsMacUpdates.vue'), 'utf8')
  const styleBody = source.slice(source.indexOf('<style'))
  const rule = (selector: string) => {
    const match = styleBody.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`))
    return match ? match[1] : null
  }

  it('is rendered as a small button that carries the native disabled attribute', async () => {
    install({ ...UP, canCheck: false })
    await mountRow()
    const button = checkButton()
    expect(button.classList.contains('btn-small')).toBe(true)
    expect(button.disabled).toBe(true)
  })

  it('dims the disabled button and shows a not-allowed cursor', () => {
    const body = rule('.mac-updates-actions .btn-small:disabled')
    expect(body, 'missing .mac-updates-actions .btn-small:disabled rule').not.toBeNull()
    const opacity = Number(body!.match(/opacity:\s*([\d.]+)/)?.[1])
    expect(opacity).toBeGreaterThan(0)
    expect(opacity).toBeLessThan(1)
    expect(body).toMatch(/cursor:\s*not-allowed/)
  })

  it('does not highlight on hover while disabled', () => {
    const body = rule('.mac-updates-actions .btn-small:disabled:hover')
    expect(body, 'missing disabled hover rule').not.toBeNull()
    expect(body).toMatch(/background:\s*var\(--bg3\)/)
  })
})
