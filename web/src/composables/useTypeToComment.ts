// Type-to-comment: with a selection live, the next keystroke opens the comment
// composer instead of doing nothing.
//
// The floating "Comment" pill stays — it's the discoverable path — but once you
// know the gesture, selecting and typing is one step instead of two. Modelled on
// document editors: the selection captures the keyboard until you dismiss it.
//
// Three entry points, all gated on `isActive()` (a selection is anchored and no
// draft is open yet):
//   - a printable character opens the composer seeded with that character
//   - a paste opens it seeded with the clipboard text, and attaches any images
//   - Cmd/Ctrl+D opens it and starts dictating
//
// Keystrokes aimed at a field (the chat composer, the comment textarea itself,
// a search box) are left alone, so this never steals typing from a focused
// input.
import { onBeforeUnmount, onMounted } from 'vue'

export type TypeToCommentOptions = {
  /** True when a selection is anchored and no comment draft is open yet. */
  isActive: () => boolean
  /** Open the composer, seeded with `initialText` (may be empty). */
  open: (initialText: string) => void
  /** Start voice dictation in the freshly opened composer. */
  dictate?: () => void
  /** Attach pasted image files to the freshly opened draft. */
  addImages?: (files: File[]) => void | Promise<void>
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return !!target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]')
}

/** True for a keystroke that should start a comment: one printable character,
 *  no modifiers, not a space (which stays a scroll key) and not mid-IME. */
function isTypeThrough(e: KeyboardEvent): boolean {
  if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return false
  if (e.key === ' ') return false
  return Array.from(e.key).length === 1
}

export function useTypeToComment(options: TypeToCommentOptions): void {
  function onKeydown(e: KeyboardEvent): void {
    if (!options.isActive() || isEditableTarget(e.target)) return
    if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 'd' || e.key === 'D')) {
      if (!options.dictate) return
      // Beats the browser's own bookmark shortcut.
      e.preventDefault()
      e.stopPropagation()
      options.open('')
      options.dictate()
      return
    }
    // Cmd/Ctrl+V arrives as a paste event instead, with the clipboard attached.
    if (!isTypeThrough(e)) return
    e.preventDefault()
    options.open(e.key)
  }

  function onPaste(e: ClipboardEvent): void {
    if (!options.isActive() || isEditableTarget(e.target)) return
    const data = e.clipboardData
    if (!data) return
    const text = data.getData('text/plain')
    const images = Array.from(data.files ?? []).filter(f => f.type.startsWith('image/'))
    if (!text.trim() && !images.length) return
    e.preventDefault()
    options.open(text)
    if (images.length && options.addImages) void options.addImages(images)
  }

  onMounted(() => {
    document.addEventListener('keydown', onKeydown)
    document.addEventListener('paste', onPaste)
  })
  onBeforeUnmount(() => {
    document.removeEventListener('keydown', onKeydown)
    document.removeEventListener('paste', onPaste)
  })
}
