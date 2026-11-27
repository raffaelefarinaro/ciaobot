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

function measure(): void {
  const columns = tracked ? getComputedStyle(tracked).gridTemplateColumns.trim() : ''
  beside.value = columns.split(/\s+/).filter(Boolean).length > 1
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
