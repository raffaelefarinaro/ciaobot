// Pure helpers for reordering the workspace list (Manage workspaces page).

/** A copy of `names` with the entry at `from` moved to index `to`. Indices
 * outside the list, or `from === to`, leave the contents unchanged. */
export function moveName(names: string[], from: number, to: number): string[] {
  const next = names.slice()
  const inRange = (i: number) => Number.isInteger(i) && i >= 0 && i < names.length
  if (from === to || !inRange(from) || !inRange(to)) return next
  const [moved] = next.splice(from, 1)
  next.splice(to, 0, moved)
  return next
}

/** The polite live-region text after a move; `position` is one-based. */
export function moveAnnouncement(label: string, position: number, total: number): string {
  return `Moved ${label} to position ${position} of ${total}`
}
