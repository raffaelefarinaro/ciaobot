// Shared pointer drag-sort engine. One engine serves the task lanes (several
// scopes side by side) and single-scope lists such as the project sidebar. It
// persists nothing: `onCommit` is a callback.
//
// Rendering contract for the views that use it:
// - Render `entriesFor(scopeId)` instead of the raw list. An `item` entry is the
//   real row; in a pointer drag a `placeholder` entry is rendered as a keyed
//   `<li class="drag-placeholder" aria-hidden="true">`.
// - In a pointer drag the source row stays mounted. While `isSource(id)` is
//   true it carries the class `drag-source`, which collapses it (`height: 0;
//   margin: 0; padding: 0; border: 0; overflow: hidden; opacity: 0`) but keeps
//   it in the DOM; the overlay stands in for it. Its following row gap is
//   cancelled too, so the rows after it already sit where they will land.
// - Wrap each list in `<TransitionGroup name="drag-reorder">` so neighbours
//   animate with Vue's FLIP move class: `.drag-reorder-move { transition:
//   transform 120ms var(--ease) }`, and `none` under `prefers-reduced-motion:
//   reduce`. The placeholder (dashed `--border-strong` outline) and the
//   highlighted target scope stay in that case.
// - The placeholder's height is the source's height: while a drag is active the
//   engine sets `--drag-item-height` (px) on `root`. Use it for
//   `.drag-placeholder { height: var(--drag-item-height) }`.
// - The dragged overlay is a clone of the source appended to `document.body`
//   (`aria-hidden`, `inert`, `pointer-events: none`, class `drag-overlay`) and
//   uses the open-menu elevation. The views need no styling for it.
// - There is no drag handle. A row spreads `surfaceHandlers(id, scopeId)` onto
//   itself and a fine pointer drags its own content. That path ignores presses
//   that start on an interactive descendant, so a link, button or form control
//   keeps its gesture; a row's primary control (the project name) can opt in
//   with `data-drag-primary`, where a press that does not travel is still its
//   click and a drag swallows the click it would have ended on. Touch is not a
//   surface drag, so a swipe still scrolls.
// - Keyboard and touch reorder through the row's menu: its Move up / Move down
//   items call `move(id, scopeId, direction)`, which runs the same commit,
//   rollback and announcement path as a drop. The view returns focus to the
//   menu trigger; the engine itself never moves focus.
// - Releasing a pointer drag does not tear the preview down: the card takes the
//   placeholder's slot at once (`settledOrder`), `isSource` turns false, and
//   `settling` stays true until the write resolves. Views put the
//   `drag-settling` class on their list over that period, which suspends the FLIP
//   move that would replay the drop; they also hide a leaving placeholder
//   (`.drag-placeholder.drag-reorder-leave-active { display: none }`) so its
//   one-frame leave cannot open a gap. Normal in-drag movement animates as
//   before, and a refused write restores the saved order with the animation
//   back.
// - Bind `announcement` to an sr-only `role="status" aria-live="polite"` region.
// - Call `cancel()` on navigation, workspace change or a remote update of the
//   list being dragged. It ends the drag silently.

import {
  getCurrentScope,
  onScopeDispose,
  ref,
  shallowRef,
  type Ref,
  type ShallowRef,
} from 'vue'
import {
  autoScrollStep,
  exceedsThreshold,
  insertionIndex,
  moveAnnouncement,
  previewEntries,
  settledOrder,
  type DragSlot,
  type MoveAnnouncementKind,
  type PreviewEntry,
} from '@/lib/dragSort'

export type { PreviewEntry } from '@/lib/dragSort'

export interface DragScope {
  id: string
  el: () => HTMLElement | null
  /** False for an append-only target such as the collapsed Done tab. */
  positional?: boolean
}

export interface DragCommit {
  id: string
  fromScope: string
  toScope: string
  /** Index in the destination's visible list without the dragged item. */
  index: number
}

export interface DragState {
  id: string
  fromScope: string
  /** Empty while the pointer is outside every valid scope. */
  toScope: string
  index: number
}

export type MoveDirection = 'up' | 'down'

export interface UseDragSortOptions {
  /** Stable container; pointer capture lives here. */
  root: Ref<HTMLElement | null>
  scopes: () => DragScope[]
  /** Visible ids in scope order. */
  itemIds: (scopeId: string) => string[]
  /** The element that moves (card li, project group). */
  itemEl: (id: string) => HTMLElement | null
  canDrag: (id: string, scopeId: string) => boolean
  label: (id: string) => string
  scopeLabel: (scopeId: string) => string
  canDropIn?: (id: string, fromScope: string, toScope: string) => boolean
  /** Persist once; false means it failed. */
  onCommit: (commit: DragCommit) => Promise<boolean> | boolean
  onCancel?: () => void
  threshold?: number
}

/**
 * A surface press captures the pointer only once it clears the threshold:
 * capturing on pointerdown would retarget the plain click that should open the
 * row to the root element instead.
 */
interface Pending {
  id: string
  scopeId: string
  pointerId: number
  startX: number
  startY: number
}

interface Session {
  pointerId: number
  offX: number
  offY: number
  width: number
  height: number
  x: number
  y: number
  overlay: HTMLElement
  raf: number
}

// Same shadow as the open menus (`.workspace-scope-menu` in ProjectSidebar.vue);
// there is no shadow token to reference.
const OVERLAY_ELEVATION = '0 1rem 2.5rem rgb(0 0 0 / 38%)'
const HEIGHT_VAR = '--drag-item-height'

/**
 * Controls that own the press they receive. A surface drag must not take a
 * press from a link, button or form control inside the row: their click is the
 * action the user asked for.
 */
const OWN_GESTURE =
  'a[href], button, input, select, textarea, label, [contenteditable="true"], [role="button"], [role="menuitem"]'

/**
 * A row's primary control can also start a surface drag. The press is still the
 * control's click until it clears the threshold; a drag then swallows that
 * click. A nested link or button inside the primary keeps its own gesture.
 */
const PRIMARY_GESTURE = '[data-drag-primary]'

function ownGesture(target: EventTarget | null): Element | null {
  return target instanceof Element ? target.closest(OWN_GESTURE) : null
}

function translateY(el: HTMLElement): number {
  const transform = getComputedStyle(el).transform
  if (!transform || transform === 'none') return 0
  return new DOMMatrix(transform).m42
}

/** The element's rect without any in-flight FLIP translation. */
function layoutRect(el: HTMLElement): { top: number; height: number } {
  const rect = el.getBoundingClientRect()
  return { top: rect.top - translateY(el), height: rect.height }
}

function isScrollable(style: string): boolean {
  return style === 'auto' || style === 'scroll' || style === 'overlay'
}

/** The preview placeholder drawn in this scope, if Vue has rendered one. */
function renderedPlaceholder(scopeEl: HTMLElement): HTMLElement | null {
  return scopeEl.querySelector<HTMLElement>('.drag-placeholder')
}

/**
 * The placeholder's slot index among the visible items, read from the drawn
 * order rather than from the reactive target. A release in the same task as the
 * last move can see a placeholder that Vue has not moved yet, or none at all.
 * `compareDocumentPosition` keeps this correct when the rows sit in a list
 * nested inside the scope (a task lane) as well as when they are direct children
 * (the project sidebar).
 */
function renderedSlotIndex(
  placeholder: HTMLElement,
  ids: readonly string[],
  itemEl: (id: string) => HTMLElement | null,
): number {
  let count = 0
  for (const id of ids) {
    const item = itemEl(id)
    if (item && item !== placeholder) {
      if (item.compareDocumentPosition(placeholder) & Node.DOCUMENT_POSITION_FOLLOWING) count += 1
    }
  }
  return count
}

function scrollersOf(...starts: Array<HTMLElement | null>): HTMLElement[] {
  const found = new Set<HTMLElement>()
  for (const start of starts) {
    for (let el = start; el; el = el.parentElement) {
      const style = getComputedStyle(el)
      if (isScrollable(style.overflowX) || isScrollable(style.overflowY)) found.add(el)
    }
  }
  return [...found]
}

export function useDragSort(options: UseDragSortOptions) {
  const drag = shallowRef<DragState | null>(null)
  const announcement = ref('')
  /**
   * A pointer drop is waiting for its acknowledged order. The card already holds
   * its target slot and the list must not animate the commit-time re-render.
   */
  const settling = ref(false)

  let pending: Pending | null = null
  let session: Session | null = null
  let origin: { id: string; scope: string; index: number } | null = null
  let listenersOn = false
  /**
   * Identity of the current drag interaction. A new pointer press bumps it, as
   * does a silent end (cancel, disposal): a write that belongs to an older
   * interaction must do nothing once that happens.
   */
  let interactionToken = 0
  /**
   * The interaction whose write is in flight. A silent cancel or a new drag can
   * leave an older write pending: it must not clear the live drag or announce
   * over it.
   */
  let committingToken: number | null = null

  function isCommitting(): boolean {
    return committingToken === interactionToken
  }

  function announce(
    kind: MoveAnnouncementKind,
    id: string,
    scopeId: string,
    position: number,
    total: number,
  ): void {
    announcement.value = moveAnnouncement(
      kind,
      options.label(id),
      options.scopeLabel(scopeId),
      position,
      total,
    )
  }

  function visibleIds(scopeId: string, withoutId: string): string[] {
    return options.itemIds(scopeId).filter((x) => x !== withoutId)
  }

  function allowed(id: string, from: string, to: string): boolean {
    return options.canDropIn ? options.canDropIn(id, from, to) : true
  }

  // ---- listeners -------------------------------------------------------

  function addListeners(): void {
    const root = options.root.value
    if (!root || listenersOn) return
    listenersOn = true
    root.addEventListener('pointermove', onPointermove)
    root.addEventListener('pointerup', onPointerup)
    root.addEventListener('pointercancel', onPointercancel)
    root.addEventListener('lostpointercapture', onLostPointerCapture)
    document.addEventListener('pointermove', onDocumentPointermove, true)
    document.addEventListener('pointerup', onDocumentPointerup, true)
    document.addEventListener('pointercancel', onDocumentPointercancel, true)
    document.addEventListener('keydown', onDocumentKeydown)
    window.addEventListener('blur', onWindowBlur)
    document.addEventListener('visibilitychange', onVisibilityChange)
  }

  function removeListeners(): void {
    if (!listenersOn) return
    listenersOn = false
    const root = options.root.value
    root?.removeEventListener('pointermove', onPointermove)
    root?.removeEventListener('pointerup', onPointerup)
    root?.removeEventListener('pointercancel', onPointercancel)
    root?.removeEventListener('lostpointercapture', onLostPointerCapture)
    document.removeEventListener('pointermove', onDocumentPointermove, true)
    document.removeEventListener('pointerup', onDocumentPointerup, true)
    document.removeEventListener('pointercancel', onDocumentPointercancel, true)
    document.removeEventListener('keydown', onDocumentKeydown)
    window.removeEventListener('blur', onWindowBlur)
    document.removeEventListener('visibilitychange', onVisibilityChange)
  }

  /**
   * A pen implicitly captures the pointer on the card it pressed. When the drag
   * activates the root takes that capture over, and the card reports the loss
   * here as it bubbles. Only the root losing its own capture ends the gesture.
   */
  function onLostPointerCapture(e: PointerEvent): void {
    if (e.target !== options.root.value) return
    onPointercancel()
  }

  /**
   * A surface press has no pointer capture until it clears the threshold, so
   * its first move can land anywhere in the document and never reach the root.
   * Read that move at the document so the gesture still becomes a drag; once
   * it has, the root's own handler tracks the captured pointer.
   */
  function onDocumentPointermove(e: PointerEvent): void {
    if (!pending || e.pointerId !== pending.pointerId) return
    onPointermove(e)
  }

  /**
   * A surface press captured its pointer only after it cleared the threshold,
   * so a release outside the board before then has no root handler. End that
   * press quietly; an activated drag is already captured and commits through
   * the root's own listener.
   */
  function onDocumentPointerup(e: PointerEvent): void {
    if (!pending || e.pointerId !== pending.pointerId) return
    if (!session) teardownPointer()
  }

  function onDocumentPointercancel(e: PointerEvent): void {
    if (!pending || e.pointerId !== pending.pointerId) return
    if (!session) teardownPointer()
  }

  function releaseCapture(pointerId: number): void {
    const root = options.root.value
    if (root && root.hasPointerCapture(pointerId)) root.releasePointerCapture(pointerId)
  }

  function teardownPointer(): void {
    if (session) {
      cancelAnimationFrame(session.raf)
      session.overlay.remove()
      releaseCapture(session.pointerId)
      session = null
      options.root.value?.style.removeProperty(HEIGHT_VAR)
    }
    pending = null
    removeListeners()
  }

  function swallowNextClick(): void {
    const swallow = (e: Event): void => {
      e.stopPropagation()
      e.preventDefault()
    }
    window.addEventListener('click', swallow, true)
    setTimeout(() => window.removeEventListener('click', swallow, true), 0)
  }

  function finish(): void {
    drag.value = null
    settling.value = false
    origin = null
  }

  /** End without a write. */
  function abort(speak: boolean): void {
    const d = drag.value
    const o = origin
    const wasPointer = session !== null
    // Any in-flight write from this interaction is now stale.
    interactionToken += 1
    teardownPointer()
    if (!d || !o) {
      finish()
      return
    }
    if (wasPointer) swallowNextClick()
    if (speak) {
      const total = options.itemIds(o.scope).length
      announce('cancel', o.id, o.scope, o.index + 1, total)
    }
    finish()
    options.onCancel?.()
  }

  async function commit(): Promise<void> {
    const d = drag.value
    const o = origin
    if (!d || !o || isCommitting()) return
    if (d.toScope === '') {
      abort(true)
      return
    }
    const wasPointer = session !== null
    const unchanged = d.toScope === o.scope && d.index === o.index
    const total = visibleIds(d.toScope, d.id).length + 1
    teardownPointer()
    if (wasPointer) swallowNextClick()
    if (unchanged) {
      announce('drop', d.id, d.toScope, d.index + 1, total)
      finish()
      return
    }
    // The overlay is gone, but the preview stays on screen in its target slot
    // until the acknowledged order has replaced it. Drawing a placeholder here
    // would collapse it and shift the neighbours twice, once when the card was
    // released and again when the answer landed.
    if (wasPointer) settling.value = true
    const token = interactionToken
    committingToken = token
    let ok = false
    try {
      ok = await options.onCommit({
        id: d.id,
        fromScope: d.fromScope,
        toScope: d.toScope,
        index: d.index,
      })
    } catch {
      ok = false
    }
    if (committingToken === token) committingToken = null
    // Cancelled, disposed of, or superseded by a newer interaction while the
    // write was in flight: it ends nothing and announces nothing.
    if (token !== interactionToken) return
    if (ok) {
      announce('drop', d.id, d.toScope, d.index + 1, total)
      finish()
      return
    }
    announcement.value = moveAnnouncement('fail', '', '', 0, 0)
    finish()
  }

  // ---- pointer flow ----------------------------------------------------

  function targetAt(x: number, y: number): { scope: string; index: number } | null {
    const d = drag.value
    if (!d) return null
    // What is actually on top at the pointer. A lane clipped by the board's
    // sideways scroll, or one a sheet or menu covers, is not a target even
    // though its own rect and the stack behind the cover still contain it.
    const top = document.elementsFromPoint(x, y)[0]
    for (const scope of options.scopes()) {
      const el = scope.el()
      if (!el) continue
      const rect = el.getBoundingClientRect()
      if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) continue
      if (!top || (top !== el && !el.contains(top))) continue
      if (!allowed(d.id, d.fromScope, scope.id)) return null
      const ids = visibleIds(scope.id, d.id)
      if (scope.positional === false) return { scope: scope.id, index: ids.length }
      // Hit testing follows what is drawn: a placeholder that Vue has not
      // flushed yet does not shift anything, and one drawn at an older index
      // shifts the slots that actually follow it.
      const placeholder = renderedPlaceholder(el)
      const shift = placeholder ? placeholderShift(placeholder) : 0
      const at = placeholder ? renderedSlotIndex(placeholder, ids, options.itemEl) : -1
      const slots: DragSlot[] = []
      ids.forEach((id, i) => {
        const item = options.itemEl(id)
        if (!item) return
        const rect2 = layoutRect(item)
        slots.push({
          id,
          top: rect2.top - (i >= at ? shift : 0),
          height: rect2.height,
        })
      })
      return { scope: scope.id, index: insertionIndex(slots, y) }
    }
    return null
  }

  function placeholderShift(placeholder: HTMLElement): number {
    // The drawn placeholder's own size, not the source card's remembered
    // height: borders and an in-flight move must not skew the row it occupies.
    // Gap lives on the list that holds the rows, which need not be the scope
    // element itself (a task lane wraps its `ul.task-cards`).
    const list = placeholder.parentElement
    const gap = list ? parseFloat(getComputedStyle(list).rowGap) || 0 : 0
    return layoutRect(placeholder).height + gap
  }

  function updateTarget(x: number, y: number): void {
    const d = drag.value
    if (!d) return
    const t = targetAt(x, y)
    const toScope = t?.scope ?? ''
    const index = t?.index ?? 0
    if (d.toScope === toScope && d.index === index) return
    drag.value = { ...d, toScope, index }
  }

  function positionOverlay(): void {
    if (!session) return
    session.overlay.style.transform = `translate3d(${session.x - session.offX}px, ${
      session.y - session.offY
    }px, 0)`
  }

  function tick(): void {
    if (!session) return
    const d = drag.value
    const scopeEl = d ? (options.scopes().find((s) => s.id === d.toScope)?.el() ?? null) : null
    let scrolled = false
    for (const el of scrollersOf(scopeEl, options.root.value)) {
      const rect = el.getBoundingClientRect()
      const style = getComputedStyle(el)
      if (isScrollable(style.overflowY) && el.scrollHeight > el.clientHeight) {
        const dy = autoScrollStep({ start: rect.top, end: rect.bottom }, session.y)
        if (dy) {
          el.scrollTop += dy
          scrolled = true
        }
      }
      if (isScrollable(style.overflowX) && el.scrollWidth > el.clientWidth) {
        const dx = autoScrollStep({ start: rect.left, end: rect.right }, session.x)
        if (dx) {
          el.scrollLeft += dx
          scrolled = true
        }
      }
    }
    if (scrolled) updateTarget(session.x, session.y)
    session.raf = requestAnimationFrame(tick)
  }

  function activate(e: PointerEvent): void {
    const p = pending
    const source = p ? options.itemEl(p.id) : null
    const root = options.root.value
    if (!p || !source || !root) {
      abort(false)
      return
    }
    const rect = source.getBoundingClientRect()
    const overlay = source.cloneNode(true) as HTMLElement
    // The clone is decoration: it must not duplicate ids or radio groups, or
    // re-point labels, in the live document.
    for (const n of [overlay, ...overlay.querySelectorAll('[id],[name],[for]')]) {
      for (const attr of ['id', 'name', 'for']) n.removeAttribute(attr)
    }
    overlay.classList.add('drag-overlay')
    overlay.setAttribute('aria-hidden', 'true')
    overlay.setAttribute('inert', '')
    Object.assign(overlay.style, {
      position: 'fixed',
      left: '0',
      top: '0',
      margin: '0',
      width: `${rect.width}px`,
      height: `${rect.height}px`,
      pointerEvents: 'none',
      zIndex: '1000',
      transition: 'none',
      boxShadow: OVERLAY_ELEVATION,
    })
    document.body.appendChild(overlay)
    root.style.setProperty(HEIGHT_VAR, `${rect.height}px`)
    session = {
      pointerId: p.pointerId,
      offX: p.startX - rect.left,
      offY: p.startY - rect.top,
      width: rect.width,
      height: rect.height,
      x: e.clientX,
      y: e.clientY,
      overlay,
      raf: 0,
    }
    const index = options.itemIds(p.scopeId).indexOf(p.id)
    origin = { id: p.id, scope: p.scopeId, index }
    drag.value = { id: p.id, fromScope: p.scopeId, toScope: p.scopeId, index }
    // A surface press was selecting text until it became a drag; the trail it
    // leaves would follow the pointer across the board.
    window.getSelection()?.removeAllRanges()
    announce('pickup', p.id, p.scopeId, index + 1, options.itemIds(p.scopeId).length)
    positionOverlay()
    updateTarget(e.clientX, e.clientY)
    session.raf = requestAnimationFrame(tick)
  }

  function onPointerdown(id: string, scopeId: string, e: PointerEvent): void {
    if (drag.value || pending || isCommitting()) return
    if (e.pointerType === 'mouse' && e.button !== 0) return
    if (!e.isPrimary || !options.canDrag(id, scopeId)) return
    if (!options.root.value) return
    pending = { id, scopeId, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY }
    // A pointer gesture is a new interaction: a write still landing from the
    // previous one must not end or announce over this drag.
    interactionToken += 1
    addListeners()
  }

  function onPointermove(e: PointerEvent): void {
    if (session) {
      if (e.pointerId !== session.pointerId) return
      session.x = e.clientX
      session.y = e.clientY
      positionOverlay()
      updateTarget(e.clientX, e.clientY)
      return
    }
    if (!pending || e.pointerId !== pending.pointerId) return
    if (exceedsThreshold(e.clientX - pending.startX, e.clientY - pending.startY, options.threshold)) {
      // From here the gesture is a drag, not a click: capture so the rest of it
      // keeps arriving even over another lane, and the root can commit it.
      options.root.value?.setPointerCapture(pending.pointerId)
      activate(e)
    }
  }

  function onPointerup(e: PointerEvent): void {
    if (pending && e.pointerId !== pending.pointerId) return
    if (!session) {
      teardownPointer()
      return
    }
    session.x = e.clientX
    session.y = e.clientY
    updateTarget(e.clientX, e.clientY)
    void commit()
  }

  function onPointercancel(): void {
    if (session) abort(true)
    else teardownPointer()
  }

  function onWindowBlur(): void {
    if (drag.value) {
      if (!isCommitting()) abort(true)
      return
    }
    // A captured pointer would deliver a cancel; a surface press that never
    // cleared the threshold has no capture, so the window losing focus is its
    // only end.
    if (pending) teardownPointer()
  }

  function onVisibilityChange(): void {
    if (document.hidden) onWindowBlur()
  }

  function onDocumentKeydown(e: KeyboardEvent): void {
    if (e.key !== 'Escape' || !drag.value || isCommitting()) return
    e.preventDefault()
    e.stopPropagation()
    abort(true)
  }

  // ---- public ----------------------------------------------------------

  /**
   * The row's own surface as the pointer target: the pick-up, threshold,
   * placement and write.
   *
   * Only a press on non-interactive content starts it, and touch is excluded so
   * a swipe still scrolls the board. A press that never clears the threshold is
   * an ordinary click, and the engine eats the click the browser sends after an
   * activated drag.
   */
  function surfaceHandlers(id: string, scopeId: string) {
    return {
      onPointerdown: (e: PointerEvent) => {
        if (e.pointerType === 'touch') return
        const own = ownGesture(e.target)
        if (own && !own.matches(PRIMARY_GESTURE)) return
        onPointerdown(id, scopeId, e)
      },
    }
  }

  function entriesFor(scopeId: string): PreviewEntry[] {
    const ids = options.itemIds(scopeId)
    const d = drag.value
    if (d && settling.value) {
      if (d.toScope === scopeId) {
        return settledOrder(ids, d.id, d.index).map((id): PreviewEntry => ({ kind: 'item', id }))
      }
      return ids
        .filter((id) => id !== d.id)
        .map((id): PreviewEntry => ({ kind: 'item', id }))
    }
    if (d && d.toScope === scopeId) return previewEntries(ids, d.id, d.index)
    return ids.map((id): PreviewEntry => ({ kind: 'item', id }))
  }

  function isSource(id: string): boolean {
    return Boolean(drag.value && !settling.value && drag.value.id === id)
  }

  function cancel(_reason?: string): void {
    if (drag.value) {
      abort(false)
      return
    }
    // Navigation or an unmount can land between a drop's write and its answer,
    // or while the write itself is still pending.
    interactionToken += 1
    if (pending) teardownPointer()
  }

  async function move(id: string, scopeId: string, direction: MoveDirection): Promise<boolean> {
    if (drag.value || isCommitting() || !options.canDrag(id, scopeId)) return false
    const ids = options.itemIds(scopeId)
    const pos = ids.indexOf(id)
    if (pos < 0) return false
    const index = direction === 'up' ? pos - 1 : pos + 1
    if (index < 0 || index >= ids.length) return false
    if (!allowed(id, scopeId, scopeId)) return false
    const total = visibleIds(scopeId, id).length + 1
    const token = interactionToken
    committingToken = token
    let ok = false
    try {
      ok = await options.onCommit({ id, fromScope: scopeId, toScope: scopeId, index })
    } catch {
      ok = false
    }
    if (committingToken === token) committingToken = null
    if (token !== interactionToken) return ok
    if (ok) announce('drop', id, scopeId, index + 1, total)
    else announcement.value = moveAnnouncement('fail', '', '', 0, 0)
    return ok
  }

  /**
   * Place a row chosen from a menu action or dialog. The index is the insertion
   * point among the destination's other rows, exactly what `onCommit` expects;
   * one write carries a cross-lane status and its placement. A no-op choice
   * announces the position without writing.
   */
  async function place(
    id: string,
    fromScope: string,
    toScope: string,
    index: number,
  ): Promise<boolean> {
    if (drag.value || isCommitting() || !toScope) return false
    if (!options.canDrag(id, fromScope) || !allowed(id, fromScope, toScope)) return false
    const others = visibleIds(toScope, id)
    const target = Math.min(Math.max(Math.trunc(index) || 0, 0), others.length)
    const total = others.length + 1
    const current = fromScope === toScope ? options.itemIds(toScope).indexOf(id) : -1
    if (current === target) {
      announce('drop', id, toScope, target + 1, total)
      return true
    }
    const token = interactionToken
    committingToken = token
    let ok = false
    try {
      ok = await options.onCommit({ id, fromScope, toScope, index: target })
    } catch {
      ok = false
    }
    if (committingToken === token) committingToken = null
    if (token !== interactionToken) return ok
    if (ok) announce('drop', id, toScope, target + 1, total)
    else announcement.value = moveAnnouncement('fail', '', '', 0, 0)
    return ok
  }

  if (getCurrentScope()) onScopeDispose(() => cancel())

  return {
    drag: drag as Readonly<ShallowRef<DragState | null>>,
    announcement: announcement as Readonly<Ref<string>>,
    settling: settling as Readonly<Ref<boolean>>,
    surfaceHandlers,
    entriesFor,
    isSource,
    cancel,
    move,
    place,
  }
}
