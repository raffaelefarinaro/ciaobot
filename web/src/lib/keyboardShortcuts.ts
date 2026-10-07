export type ShortcutId = 'newChat' | 'archiveChat' | 'toggleSidebar' | 'modelPicker' | 'fontIncrease' | 'fontDecrease' | 'closeChat' | `workspace${1|2|3|4|5|6|7|8|9}`
export type SendMode = 'modifier' | 'enter'

export interface KeyboardSettings {
  keyboard_shortcuts: Record<string, string>
  keyboard_send_mode: SendMode
  revision: string
}

export const DEFAULT_SHORTCUTS: Record<ShortcutId, string> = {
  newChat: 'Alt+KeyN',
  archiveChat: 'Alt+Backspace',
  toggleSidebar: 'Alt+KeyS',
  modelPicker: 'Alt+KeyM',
  fontIncrease: 'Alt+Equal',
  fontDecrease: 'Alt+Minus',
  closeChat: 'Escape',
  workspace1: 'Digit1', workspace2: 'Digit2', workspace3: 'Digit3',
  workspace4: 'Digit4', workspace5: 'Digit5', workspace6: 'Digit6',
  workspace7: 'Digit7', workspace8: 'Digit8', workspace9: 'Digit9',
}

export const SHORTCUT_LABELS: Record<ShortcutId, string> = {
  newChat: 'New chat',
  archiveChat: 'Archive chat',
  toggleSidebar: 'Show or hide the sidebar',
  modelPicker: 'Open the model picker',
  fontIncrease: 'Increase font size',
  fontDecrease: 'Decrease font size',
  closeChat: 'Close the open chat',
  workspace1: 'Switch to workspace 1', workspace2: 'Switch to workspace 2',
  workspace3: 'Switch to workspace 3', workspace4: 'Switch to workspace 4',
  workspace5: 'Switch to workspace 5', workspace6: 'Switch to workspace 6',
  workspace7: 'Switch to workspace 7', workspace8: 'Switch to workspace 8',
  workspace9: 'Switch to workspace 9',
}

export function effectiveBinding(settings: KeyboardSettings, id: ShortcutId): string {
  return settings.keyboard_shortcuts[id] ?? DEFAULT_SHORTCUTS[id]
}

export function keyboardMatches(event: KeyboardEvent, binding: string): boolean {
  if (binding === 'disabled' || event.isComposing) return false
  const parts = binding.split('+')
  const code = parts.pop()
  const actualCode = event.code || (event.key.length === 1 && /^[1-9]$/.test(event.key)
    ? `Digit${event.key}`
    : event.key === 'Escape' ? 'Escape'
      : event.key === 'Backspace' ? 'Backspace'
        : event.key === '=' || event.key === '+' ? 'Equal'
          : event.key === '-' || event.key === '_' ? 'Minus'
            : /^[a-z]$/i.test(event.key) ? `Key${event.key.toUpperCase()}` : '')
  if (!code || actualCode !== code) return false
  const wantsMod = parts.includes('Mod')
  const wantsAlt = parts.includes('Alt')
  const wantsShift = parts.includes('Shift')
  const mod = event.metaKey || event.ctrlKey
  return mod === wantsMod && event.altKey === wantsAlt && event.shiftKey === wantsShift
}

export function bindingFromEvent(event: KeyboardEvent, allowBareDigit = false): string | null {
  if (event.isComposing || ['Alt', 'Control', 'Meta', 'Shift'].includes(event.key)) return null
  if (['Tab', 'Enter', ' ', 'Backspace', 'Delete', 'Escape'].includes(event.key)
    && !(event.metaKey || event.ctrlKey || event.altKey || event.shiftKey)) return null
  const code = event.code || (allowBareDigit && /^[1-9]$/.test(event.key) ? `Digit${event.key}` : '')
  if (!code || code === 'Unidentified') return null
  const modifiers = [
    ...(event.metaKey || event.ctrlKey ? ['Mod'] : []),
    ...(event.altKey ? ['Alt'] : []),
    ...(event.shiftKey ? ['Shift'] : []),
  ]
  const browserOwned = new Set(['Mod+KeyT', 'Mod+KeyW', 'Mod+KeyR', 'Mod+KeyL', 'Mod+KeyS', 'Mod+BracketLeft', 'Mod+BracketRight', 'Mod+Equal', 'Mod+NumpadAdd'])
  const canonical = [...modifiers, code].join('+')
  if (browserOwned.has(canonical) || (event.altKey && ['ArrowLeft', 'ArrowRight'].includes(code))) return null
  if (!modifiers.length) return allowBareDigit && /^Digit[1-9]$/.test(code) ? code : null
  return canonical
}

export function displayBinding(binding: string, apple: boolean): string {
  if (binding === 'disabled') return 'Off'
  const symbols: Record<string, string> = apple
    ? { Mod: '⌘', Alt: '⌥', Shift: '⇧', Backspace: '⌫', Equal: '=', Minus: '-' }
    : { Mod: 'Ctrl+', Alt: 'Alt+', Shift: 'Shift+', Backspace: 'Backspace', Equal: '=', Minus: '-' }
  if (binding === 'Escape') return 'Esc'
  if (/^Digit[1-9]$/.test(binding)) return binding.slice(-1)
  return binding.split('+').map(part => symbols[part] ?? part.replace(/^Key/, '')).join(apple ? '' : '')
}
