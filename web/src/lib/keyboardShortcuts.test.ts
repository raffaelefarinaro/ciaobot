// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { bindingFromEvent, DEFAULT_SHORTCUTS, displayBinding, effectiveBinding, keyboardMatches } from './keyboardShortcuts'

describe('keyboard shortcuts', () => {
  it('uses defaults and supports disabled/custom bindings', () => {
    expect(effectiveBinding({ keyboard_shortcuts: {}, keyboard_send_mode: 'modifier', mid_turn_delivery: 'queue', revision: '' }, 'archiveChat')).toBe(DEFAULT_SHORTCUTS.archiveChat)
    expect(effectiveBinding({ keyboard_shortcuts: { archiveChat: 'disabled' }, keyboard_send_mode: 'modifier', mid_turn_delivery: 'queue', revision: '' }, 'archiveChat')).toBe('disabled')
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

  it('defaults previous/next chat to Alt+brackets', () => {
    expect(DEFAULT_SHORTCUTS.previousChat).toBe('Alt+BracketLeft')
    expect(DEFAULT_SHORTCUTS.nextChat).toBe('Alt+BracketRight')
  })

  it('labels brackets', () => {
    expect(displayBinding('Alt+BracketLeft', true)).toBe('⌥[')
    expect(displayBinding('Alt+BracketRight', false)).toBe('Alt+]')
  })

  it('matches the bracket chord', () => {
    expect(keyboardMatches(new KeyboardEvent('keydown', { code: 'BracketRight', altKey: true }), 'Alt+BracketRight')).toBe(true)
  })

  it('captures it in Settings', () => {
    expect(bindingFromEvent(new KeyboardEvent('keydown', { code: 'BracketLeft', altKey: true }))).toBe('Alt+BracketLeft')
  })

  it('labels the side-by-side chords for each keyboard', () => {
    expect(displayBinding('Mod+Backslash', true)).toBe('⌘\\')
    expect(displayBinding('Mod+Alt+BracketRight', true)).toBe('⌘⌥]')
    expect(displayBinding('Mod+Alt+BracketLeft', false)).toBe('Ctrl+Alt+[')
  })

  it('captures and matches the side-by-side chords', () => {
    expect(bindingFromEvent(new KeyboardEvent('keydown', { code: 'Backslash', metaKey: true }))).toBe('Mod+Backslash')
    expect(keyboardMatches(new KeyboardEvent('keydown', { code: 'BracketLeft', ctrlKey: true, altKey: true }), DEFAULT_SHORTCUTS.focusPaneLeft)).toBe(true)
    expect(keyboardMatches(new KeyboardEvent('keydown', { code: 'BracketLeft', ctrlKey: true }), DEFAULT_SHORTCUTS.focusPaneLeft)).toBe(false)
  })
})
