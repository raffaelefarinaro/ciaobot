/**
 * What Memory's review queues (Unsure, Skills) share: the single-key
 * shortcuts, where an item came from, and the path rule that keeps a note
 * link inside the vault. Pure helpers, no Vue.
 */

/** One place an item came from: a chat (linked), a vault note (opened), a web link, or plain words such as "care run". */
export type ReviewSource =
  | { kind: 'chat'; id: string }
  | { kind: 'note'; path: string }
  | { kind: 'link'; url: string }
  | { kind: 'text'; label: string }

/** A source's identity, for de-duplication and list keys. */
export function sourceKey(source: ReviewSource): string {
  switch (source.kind) {
    case 'chat': return source.id
    case 'note': return source.path
    case 'link': return source.url
    case 'text': return source.label
  }
}

/**
 * A path relative to `base` (a folder the viewer can open), resolved; '' when
 * it is absolute, has a scheme, or climbs out of `root`. `root` is the vault
 * as the file viewer names it: workspace-relative (`memory-vault/work`), ''
 * for a vault at the workspace root, or absolute (`/Users/me/vault`, whose
 * leading `/` is kept).
 */
export function resolveUnder(root: string, base: string, relative: string): string {
  if (!relative || /^[a-z][a-z0-9+.-]*:/i.test(relative) || relative.startsWith('/') || relative.startsWith('~')) return ''
  const lead = base.startsWith('/') ? '/' : ''
  const parts = base.split('/').filter(Boolean)
  const floor = root.split('/').filter(Boolean).length
  for (const piece of relative.split('#')[0].split('/')) {
    if (!piece || piece === '.') continue
    if (piece === '..') {
      if (parts.length <= floor) return ''
      parts.pop()
    } else parts.push(piece)
  }
  const path = lead + parts.join('/')
  if (!root) return path
  return path === root || path.startsWith(`${root}/`) ? path : ''
}

// ── Queue keys ───────────────────────────────────────────────────────────────

export type QueueKey = 'approve' | 'dismiss' | 'skip' | 'next' | 'previous'

/**
 * The queue's single-key shortcuts: A approves, D dismisses, S skips, the
 * arrows move. Unmodified keys only (Shift, Alt, Ctrl, Cmd and AltGr chords are
 * someone else's), never mid-IME (Safari reports a composing key as 229), and
 * never a key another handler already took. The caller also refuses a key aimed
 * at a text field or an open dialog or menu.
 */
export function queueKey(event: KeyboardEvent): QueueKey | null {
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return null
  if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return null
  if (event.getModifierState?.('AltGraph')) return null
  switch (event.key) {
    case 'a': case 'A': return 'approve'
    case 'd': case 'D': return 'dismiss'
    case 's': case 'S': return 'skip'
    case 'ArrowDown': case 'ArrowRight': return 'next'
    case 'ArrowUp': case 'ArrowLeft': return 'previous'
    default: return null
  }
}

/** A key aimed at something that types or owns its own keys: a field, a menu, a dialog. */
export function keyBelongsElsewhere(target: EventTarget | null, queue: Element | null): boolean {
  if (!(target instanceof Element)) return false
  if (target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"]')) return true
  if (target instanceof HTMLElement && target.isContentEditable) return true
  // A chat or terminal in the slot beside the page keeps its own keys.
  if (target.closest('.chat-split-side')) return true
  const owner = target.closest('[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], [role="tablist"], [role="slider"], .title-rail')
  return !!owner && !(queue && queue.contains(owner))
}
