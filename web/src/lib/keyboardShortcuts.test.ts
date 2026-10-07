// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { bindingFromEvent, DEFAULT_SHORTCUTS, displayBinding, effectiveBinding, keyboardMatches } from './keyboardShortcuts'

describe('keyboard shortcuts', () => {
  it('uses defaults and supports disabled/custom bindings', () => {
    expect(effectiveBinding({ keyboard_shortcuts: {}, keyboard_send_mode: 'modifier' }, 'archiveChat')).toBe(DEFAULT_SHORTCUTS.archiveChat)
    expect(effectiveBinding({ keyboard_shortcuts: { archiveChat: 'disabled' }, keyboard_send_mode: 'modifier' }, 'archiveChat')).toBe('disabled')
  })

  it('matches physical codes and exact modifiers', () => {
    const event = new KeyboardEvent('keydown', { code: 'KeyK', metaKey: true })
    expect(keyboardMatches(event, 'Mod+KeyK')).toBe(true)
    expect(keyboardMatches(event, 'Alt+KeyK')).toBe(false)
  })

  it('captures modified chords and workspace digit slots', () => {
    expect(bindingFromEvent(new KeyboardEvent('keydown', { code: 'KeyK', ctrlKey: true }))).toBe('Mod+KeyK')
    expect(bindingFromEvent(new KeyboardEvent('keydown', { code: 'Digit3' }), true)).toBe('Digit3')
    expect(bindingFromEvent(new KeyboardEvent('keydown', { code: 'KeyA' }))).toBeNull()
  })

  it('labels shared bindings for the local keyboard', () => {
    expect(displayBinding('Alt+KeyN', true)).toBe('⌥N')
    expect(displayBinding('Alt+KeyN', false)).toBe('Alt+N')
  })
})
