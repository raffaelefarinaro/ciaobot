// Pure geometry and wording for the shared drag-sort engine
// (composables/useDragSort.ts). No Vue imports, no DOM access.

export interface DragSlot {
  id: string
  top: number
  height: number
}

export type PreviewEntry = { kind: 'item'; id: string } | { kind: 'placeholder' }

export type MoveAnnouncementKind = 'pickup' | 'move' | 'drop' | 'cancel' | 'fail'

/** How many slots have their vertical midpoint above `pointerY`, so the result
 * is in `[0, slots.length]`. `slots` are the non-dragged items in layout order,
 * laid out as if the placeholder were absent. Midpoints keep it stable for
 * items of any height. */
export function insertionIndex(slots: readonly DragSlot[], pointerY: number): number {
  let count = 0
  for (const slot of slots) {
    if (slot.top + slot.height / 2 < pointerY) count += 1
  }
  return count
}

/** `ids` with a placeholder entry inserted after the `index`-th non-dragged
 * item. The dragged item stays in the list (the view collapses it). `index` is
 * clamped to the number of non-dragged items. */
export function previewEntries(
  ids: readonly string[],
  draggedId: string,
  index: number,
): PreviewEntry[] {
  const others = ids.reduce((n, id) => (id === draggedId ? n : n + 1), 0)
  const target = Math.min(Math.max(Math.trunc(index) || 0, 0), others)
  const entries: PreviewEntry[] = []
  let seen = 0
  if (target === 0) entries.push({ kind: 'placeholder' })
  for (const id of ids) {
    entries.push({ kind: 'item', id })
    if (id === draggedId) continue
    seen += 1
    if (seen === target) entries.push({ kind: 'placeholder' })
  }
  return entries
}

/** Signed px/frame for one axis: zero away from the edges of `box` (and
 * outside it), a quadratic ramp inside the `edge` band up to `max`. Negative
 * near `start`, positive near `end`. A band wider than half the box shrinks to
 * half of it; an empty or inverted box never scrolls. */
export function autoScrollStep(
  box: { start: number; end: number },
  pointer: number,
  edge = 48,
  max = 16,
): number {
  const size = box.end - box.start
  if (!(size > 0) || pointer < box.start || pointer > box.end) return 0
  const band = Math.min(edge, size / 2)
  if (!(band > 0)) return 0
  const fromStart = box.start + band - pointer
  if (fromStart > 0) return -max * Math.min(fromStart / band, 1) ** 2
  const fromEnd = pointer - (box.end - band)
  if (fromEnd > 0) return max * Math.min(fromEnd / band, 1) ** 2
  return 0
}

/** True once the pointer has travelled farther than `px` from where it went down. */
export function exceedsThreshold(dx: number, dy: number, px = 5): boolean {
  return Math.hypot(dx, dy) > px
}

const ANNOUNCEMENTS: Record<
  MoveAnnouncementKind,
  (label: string, scope: string, position: number, total: number) => string
> = {
  pickup: (label, scope, p, t) => `Picked up ${label}, position ${p} of ${t} in ${scope}.`,
  move: (label, scope, p, t) => `${label}, position ${p} of ${t} in ${scope}.`,
  drop: (label, scope, p, t) => `Dropped ${label} at position ${p} of ${t} in ${scope}.`,
  cancel: (label, scope, p, t) =>
    `Move cancelled. ${label} is back at position ${p} of ${t} in ${scope}.`,
  fail: () => 'Could not save the new order.',
}

/** The polite live-region text for a step of a move; `position` is one-based. */
export function moveAnnouncement(
  kind: MoveAnnouncementKind,
  label: string,
  scopeLabel: string,
  position: number,
  total: number,
): string {
  return ANNOUNCEMENTS[kind](label, scopeLabel, position, total)
}
