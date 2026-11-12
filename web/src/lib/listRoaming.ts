/**
 * Arrow-key roaming over page lists of rows (Home's chats, the Automations
 * overview, the sidebar's automation list). ChatLayout owns the global keydown
 * and asks the list's host to move; the host hands its rows here, grouped.
 *
 * `layout` says how the groups sit on screen:
 * - `list`: one flat list (pass a single group); every arrow moves along it,
 *   Down/Right to the next row, Up/Left to the previous one.
 * - `rows`: groups stacked; Up/Down move between groups, Left/Right within one.
 * - `columns`: groups side by side; Left/Right move between groups, Up/Down within one.
 *
 * When focus is on none of the rows, `anchor` names where to start: a group,
 * and the row to move from (-1 focuses that group's first row). There is no
 * wrap-around: an arrow at either end is consumed and focus stays put.
 * Returns true when the key was a roaming key the lists handled.
 */
export type RoamLayout = 'list' | 'rows' | 'columns'

export interface RoamAnchor {
  group: number
  item: number
}

export function roamFocus(
  groups: HTMLElement[][],
  key: string,
  layout: RoamLayout,
  anchor: () => RoamAnchor,
  active: Element | null = typeof document === 'undefined' ? null : document.activeElement,
): boolean {
  const delta = key === 'ArrowDown' || key === 'ArrowRight' ? 1 : key === 'ArrowUp' || key === 'ArrowLeft' ? -1 : 0
  if (!delta || !groups.some(g => g.length)) return false

  let group = groups.findIndex(g => g.includes(active as HTMLElement))
  let item = group >= 0 ? groups[group].indexOf(active as HTMLElement) : -1
  if (group < 0) ({ group, item } = anchor())
  if (!groups[group]?.length) return false
  if (item < 0) {
    focusRow(groups[group][0])
    return true
  }

  const vertical = key === 'ArrowUp' || key === 'ArrowDown'
  const acrossGroups = (layout === 'rows' && vertical) || (layout === 'columns' && !vertical)
  if (!acrossGroups) {
    const next = clamp(item + delta, 0, groups[group].length - 1)
    if (next !== item) focusRow(groups[group][next])
    return true
  }
  let next = group + delta
  while (next >= 0 && next < groups.length && !groups[next].length) next += delta
  if (next < 0 || next >= groups.length) return true
  focusRow(groups[next][clamp(item, 0, groups[next].length - 1)])
  return true
}

/** Where a flat list starts: the row for the open item (`current`), else the first row. */
export function listAnchor(rows: HTMLElement[], current: HTMLElement | null | undefined): RoamAnchor {
  return { group: 0, item: current ? rows.indexOf(current) : -1 }
}

function focusRow(row: HTMLElement): void {
  row.focus()
  row.scrollIntoView?.({ block: 'nearest' })
}

function clamp(value: number, lower: number, upper: number): number {
  return Math.min(upper, Math.max(lower, value))
}
