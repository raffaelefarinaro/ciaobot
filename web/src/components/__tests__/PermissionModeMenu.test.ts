// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import PermissionModeMenu from '../PermissionModeMenu.vue'
import type { BridgeMode } from '../../lib/permissionModes'

const MODES: BridgeMode[] = ['normal', 'auto', 'bypass', 'plan']
let wrapper: VueWrapper | null = null

function mountMenu(props: Record<string, unknown> = {}) {
  document.body.innerHTML = '<span id="mode-label">Permission mode</span><span id="mode-note">Full rule</span>'
  wrapper = mount(PermissionModeMenu, {
    attachTo: document.body,
    props: { modelValue: 'normal', modes: MODES, labelledBy: 'mode-label', describedBy: 'mode-note', ...props },
  })
  return wrapper
}

function trigger(): HTMLButtonElement {
  return document.querySelector<HTMLButtonElement>('.permission-mode-trigger')!
}

function items(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[role="menuitemradio"]'))
}

async function press(target: Element, key: string) {
  target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }))
  await flushPromises()
}

// Reka restores focus to the button once the closing layer has unmounted,
// a tick or two after the key.
async function expectFocusBack() {
  for (let i = 0; i < 20 && document.activeElement !== trigger(); i += 1) {
    await new Promise(resolve => setTimeout(resolve, 5))
  }
  expect(document.activeElement).toBe(trigger())
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('PermissionModeMenu', () => {
  it('names the current mode and what it allows on a labelled, described menu button', () => {
    mountMenu({ modelValue: 'auto' })
    const button = trigger()
    expect(button.textContent).toContain('Auto')
    expect(button.textContent).toContain('allow safe work, ask on risky')
    expect(button.getAttribute('aria-haspopup')).toBe('menu')
    expect(button.getAttribute('aria-expanded')).toBe('false')
    const [labelId, valueId] = button.getAttribute('aria-labelledby')!.split(' ')
    expect(labelId).toBe('mode-label')
    expect(document.getElementById(valueId)!.textContent).toContain('Auto')
    expect(button.getAttribute('aria-describedby')).toBe('mode-note')
  })

  it('reads an unknown saved mode as Unknown and checks no option', async () => {
    mountMenu({ modelValue: 'future-mode' })
    expect(trigger().textContent).toContain('Unknown')
    await press(trigger(), 'Enter')
    expect(items()).toHaveLength(4)
    expect(items().some(item => item.getAttribute('aria-checked') === 'true')).toBe(false)
  })

  it('opens from the keyboard and lists the four modes with their meanings, the current one checked', async () => {
    mountMenu()
    await press(trigger(), 'Enter')
    expect(trigger().getAttribute('aria-expanded')).toBe('true')
    expect(items().map(item => item.textContent!.replace(/\s+/g, ' ').trim())).toEqual([
      'Manualask for every action',
      'Autoallow safe work, ask on risky',
      'Bypassallow everything',
      'Planread and propose only, change nothing',
    ])
    expect(items().map(item => item.getAttribute('aria-checked'))).toEqual(['true', 'false', 'false', 'false'])
  })

  it('moves with the arrow keys and picks with Enter', async () => {
    const w = mountMenu()
    await press(trigger(), 'Enter')
    const first = items()[0]
    first.focus()
    await press(first, 'ArrowDown')
    expect(document.activeElement).toBe(items()[1])
    await press(items()[1], 'ArrowDown')
    expect(document.activeElement).toBe(items()[2])
    await press(items()[2], 'Enter')
    expect(w.emitted('select')).toEqual([['bypass']])
    // The menu closes and focus goes back to its button.
    expect(items()).toHaveLength(0)
    await expectFocusBack()
  })

  it('emits Bypass on a click exactly like any other mode, with no extra step', async () => {
    const w = mountMenu()
    await press(trigger(), 'Enter')
    items().find(item => item.textContent!.includes('Bypass'))!.click()
    await flushPromises()
    expect(w.emitted('select')).toEqual([['bypass']])
  })

  it('closes on Escape without picking and returns focus to the button', async () => {
    const w = mountMenu()
    await press(trigger(), 'Enter')
    items()[0].focus()
    await press(items()[0], 'Escape')
    expect(items()).toHaveLength(0)
    await expectFocusBack()
    expect(w.emitted('select')).toBeUndefined()
  })

  it('does not open while disabled', async () => {
    const w = mountMenu({ disabled: true })
    // aria-disabled, not disabled: focus can still come back to the button.
    expect(trigger().getAttribute('aria-disabled')).toBe('true')
    await press(trigger(), 'Enter')
    trigger().dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
    await flushPromises()
    expect(items()).toHaveLength(0)
    expect(w.emitted('select')).toBeUndefined()
  })
})
