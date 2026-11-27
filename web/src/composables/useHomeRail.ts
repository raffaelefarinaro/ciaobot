import { readonly, ref } from 'vue'

// Whether Home's At a glance rail sits beside the request column.
//
// The layout decides, not a number kept here: ChatLayout's `.home-workbench`
// grid is one column when the pane (a `chat-pane` container query, DESIGN.md
// › Layout) is too narrow and two when the rail docks. This reads the grid's
// resolved track count, so the care signal follows the same query the layout
// uses, follows pane resizes live (the observer re-reads on every size change,
// before paint) and never looks at the window width.
const beside = ref(false)
let tracked: HTMLElement | null = null
let observer: ResizeObserver | null = null

/** Top-level tracks of a grid-template-columns value; `minmax(0px, 1fr)` is one track. */
export function countTracks(columns: string): number {
  let depth = 0
  let count = 0
  let inTrack = false
  for (const ch of columns) {
    if (ch === '(') depth++
    else if (ch === ')') depth = Math.max(0, depth - 1)
    if (depth === 0 && /\s/.test(ch)) {
      inTrack = false
    } else if (!inTrack) {
      inTrack = true
      count++
    }
  }
  return count
}

function measure(): void {
  if (!tracked) {
    beside.value = false
    return
  }
  // A `display: none` ancestor resolves no tracks: keep the last answer
  // rather than reading the specified value of a layout nobody sees.
  if (tracked.getClientRects().length === 0) return
  beside.value = countTracks(getComputedStyle(tracked).gridTemplateColumns.trim()) > 1
}

/** A function ref for the `.home-workbench` element; called again on every patch, so it is idempotent. */
export function trackHomeWorkbench(el: unknown): void {
  const next = el instanceof HTMLElement ? el : null
  if (next === tracked) return
  observer?.disconnect()
  observer = null
  tracked = next
  if (next && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(measure)
    observer.observe(next)
  }
  measure()
}

export function useHomeRail() {
  return { beside: readonly(beside) }
}

/** Test and story seam: say where the rail is without a layout engine. */
export function setHomeRailBeside(value: boolean): void {
  beside.value = value
}
