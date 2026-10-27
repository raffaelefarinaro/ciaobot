// Shared pointer + keyboard drag-sort engine. One engine serves the task lanes
// (several scopes side by side) and single-scope lists such as the project
// sidebar. It persists nothing: `onCommit` is a callback.
//
// Rendering contract for the views that use it:
// - Render `entriesFor(scopeId)` instead of the raw list. An `item` entry is the
//   real row; in a pointer drag a `placeholder` entry is rendered as a keyed
//   `<li class="drag-placeholder" aria-hidden="true">`.
// - In a pointer drag the source row stays mounted. While `isSource(id)` is
//   true it carries the class `drag-source`, which collapses it (`height: 0;
//   margin: 0; padding: 0; border: 0; overflow: hidden; opacity: 0`) but keeps
//   it in the DOM; the overlay stands in for it.
// - In a keyboard drag there is no overlay and no placeholder: the row itself
//   stays visible and `entriesFor` moves it to its preview position (and
//   between scopes). `isSource` is false then; `isLifted(id)` is true instead.
//   Views style the lifted row as picked up: the `--border-strong` outline plus
//   the open-menu elevation, keeping its focus ring visible.
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
// - Every grip carries `data-drag-handle="<id>"` (DragGrip does) and
//   `touch-action: none`; nothing else on the row does. Spread
//   `gripHandlers(id, scopeId)` onto it with `v-bind`.
// - Menu "Move up/down" items call `move(id, scopeId, direction)`, which runs
//   the same commit and announcement path as a drop.
// - Bind `announcement` to an sr-only `role="status" aria-live="polite"` region.
// - Call `cancel()` on navigation, workspace change or a remote update of the
//   list being dragged. It ends the drag silently.

import {
  getCurrentScope,
  nextTick,
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
  mode: 'pointer' | 'keyboard'
}

export type MoveDirection = 'up' | 'down' | 'left' | 'right'

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
  /** Lanes side by side: left/right change scope. */
  horizontalScopes?: boolean
  threshold?: number
}

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

/**
 * Whether focus still belongs to the dragged item with `id`: on its grip, or
 * nowhere.
 *
 * A missing/body focus is a remount or a blur, not a decision to leave the drag,
 * so keyboard restoration is still wanted then. Any other active element is the
 * user's own navigation, and a deferred restore must not take it back.
 */
function focusStaysOnDragHandle(id: string): boolean {
  const active = document.activeElement
  if (!active || active === document.body || active === document.documentElement) return true
  return active instanceof HTMLElement && active.dataset.dragHandle === id
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

  let pending: Pending | null = null
  let session: Session | null = null
  let origin: { id: string; scope: string; index: number } | null = null
  let listenersOn = false
  /**
   * Identity of the current drag interaction. A new pickup or pointer press
   * bumps it, as does a silent end (cancel, disposal): a reveal or write that
   * belongs to an older interaction must do nothing once that happens.
   */
  let interactionToken = 0
  /**
   * The interaction whose write is in flight. A silent cancel or a new drag can
   * leave an older write pending: it must not clear the live drag, announce over
   * it, or restore its card's focus when it settles.
   */
  let committingToken: number | null = null
  /**
   * The grip whose focus a keyboard drag owns, and whether the user deliberately
   * left it. Body focus alone cannot tell a remount from a press on something
   * unfocusable, so the events that cause the latter are recorded here.
   */
  let focusWatchId: string | null = null
  let focusWatchSeq = 0
  let focusLeft = false

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

  function findGrip(id: string): HTMLElement | null {
    const grips = options.root.value?.querySelectorAll<HTMLElement>('[data-drag-handle]') ?? []
    for (const grip of grips) if (grip.dataset.dragHandle === id) return grip
    return null
  }

  function onFocusIn(event: FocusEvent): void {
    const id = focusWatchId
    if (!id) return
    const target = event.target
    // The reveal itself focuses the same grip, which is not a departure.
    if (target instanceof HTMLElement && target.dataset.dragHandle === id) return
    focusLeft = true
  }

  function onFocusWatchPointerdown(event: PointerEvent): void {
    const id = focusWatchId
    if (!id) return
    const target = event.target
    if (target instanceof Element && target.closest(`[data-drag-handle="${CSS.escape(id)}"]`)) return
    // Pressing anything else, including a lane's empty area, is the user
    // choosing another target. A body focus after that is not a remount.
    focusLeft = true
  }

  function startFocusWatch(id: string): number {
    focusWatchId = id
    focusWatchSeq += 1
    focusLeft = false
    document.addEventListener('focusin', onFocusIn, true)
    document.addEventListener('pointerdown', onFocusWatchPointerdown, true)
    return focusWatchSeq
  }

  /**
   * `seq` identifies the watch a deferred reveal started. A newer pickup may
   * already own the same grip; the old reveal must not end its watch.
   */
  function endFocusWatch(seq?: number): void {
    if (seq !== undefined && seq !== focusWatchSeq) return
    if (!focusWatchId) return
    focusWatchId = null
    focusLeft = false
    document.removeEventListener('focusin', onFocusIn, true)
    document.removeEventListener('pointerdown', onFocusWatchPointerdown, true)
  }

  /** The no-steal rule, including evidence a body focus is deliberate. */
  function canRestoreFocus(id: string): boolean {
    // A press or focus elsewhere wins even if the grip happens to still hold
    // focus (a touch scroll, for example): the user chose another target.
    if (focusWatchId === id && focusLeft) return false
    const active = document.activeElement
    if (active instanceof HTMLElement && active.dataset.dragHandle === id) return true
    return focusStaysOnDragHandle(id)
  }

  async function reveal(id: string, token: number, watch?: number): Promise<void> {
    // The user may already have moved on while the write was in flight.
    if (!canRestoreFocus(id)) {
      if (watch !== undefined) endFocusWatch(watch)
      return
    }
    await nextTick()
    if (token !== interactionToken || !canRestoreFocus(id)) {
      if (watch !== undefined) endFocusWatch(watch)
      return
    }
    const root = options.root.value
    const target = root?.querySelector<HTMLElement>('.drag-placeholder') ?? options.itemEl(id)
    target?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    findGrip(id)?.focus()
    if (watch !== undefined) endFocusWatch(watch)
  }

  /**
   * A pointer failure restores the grip only when that grip held focus and the
   * user has not navigated or chosen another target since. The preview can
   * remount the focused row and drop focus to the document; without this the
   * keyboard path back to the row is lost. Menu failures never come here: their
   * menu owns trigger restoration.
   */
  function restorePointerFocusAfterFailure(id: string, token: number, watch: number): void {
    void nextTick(() => {
      if (token !== interactionToken || !canRestoreFocus(id)) {
        endFocusWatch(watch)
        return
      }
      findGrip(id)?.focus()
      endFocusWatch(watch)
    })
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
    root.addEventListener('lostpointercapture', onPointercancel)
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
    root?.removeEventListener('lostpointercapture', onPointercancel)
    document.removeEventListener('keydown', onDocumentKeydown)
    window.removeEventListener('blur', onWindowBlur)
    document.removeEventListener('visibilitychange', onVisibilityChange)
  }

  function onFocusout(): void {
    // A remount across lanes blurs the grip for a moment; only a focus that
    // really left the picked-up grip cancels.
    setTimeout(() => {
      const d = drag.value
      if (!d || d.mode !== 'keyboard' || isCommitting()) return
      const active = document.activeElement
      if (active instanceof HTMLElement && active.dataset.dragHandle === d.id) return
      abort(true)
    }, 0)
  }

  function releaseCapture(pointerId: number): void {
    const root = options.root.value
    if (root && root.hasPointerCapture(pointerId)) root.releasePointerCapture(pointerId)
  }

  function teardownPointer(): void {
    const root = options.root.value
    if (session) {
      cancelAnimationFrame(session.raf)
      session.overlay.remove()
      releaseCapture(session.pointerId)
      session = null
      root?.style.removeProperty(HEIGHT_VAR)
    } else if (pending) {
      releaseCapture(pending.pointerId)
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

  function finish(restoreFocus = true): void {
    const wasKeyboard = drag.value?.mode === 'keyboard'
    const id = drag.value?.id
    if (wasKeyboard) options.root.value?.removeEventListener('focusout', onFocusout)
    drag.value = null
    origin = null
    if (wasKeyboard && id) {
      if (restoreFocus) void reveal(id, interactionToken, focusWatchId ? focusWatchSeq : undefined)
      else endFocusWatch()
    }
  }

  /** End without a write. */
  function abort(speak: boolean): void {
    const d = drag.value
    const o = origin
    const wasPointer = session !== null
    const restoreFocus = speak && d?.mode === 'keyboard'
    // The reveal and any in-flight write from this interaction are now stale.
    interactionToken += 1
    teardownPointer()
    if (!d || !o) {
      finish(false)
      if (wasPointer || d?.mode === 'pointer') endFocusWatch()
      return
    }
    if (wasPointer) swallowNextClick()
    if (speak) {
      const total = options.itemIds(o.scope).length
      announce('cancel', o.id, o.scope, o.index + 1, total)
    }
    finish(restoreFocus)
    if (wasPointer || d.mode === 'pointer') endFocusWatch()
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
    const pointerWatch = wasPointer && focusWatchId === d.id ? focusWatchSeq : undefined
    const unchanged = d.toScope === o.scope && d.index === o.index
    const total = visibleIds(d.toScope, d.id).length + 1
    teardownPointer()
    if (wasPointer) swallowNextClick()
    if (unchanged) {
      announce('drop', d.id, d.toScope, d.index + 1, total)
      finish()
      if (pointerWatch !== undefined) endFocusWatch(pointerWatch)
      return
    }
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
      if (pointerWatch !== undefined) endFocusWatch(pointerWatch)
      return
    }
    announcement.value = moveAnnouncement('fail', '', '', 0, 0)
    // Keyboard failures restore focus through `finish`/`reveal`, the one
    // deferred path that owns the focus watch. A pointer failure restores only
    // the grip that held focus before the preview remounted it; menu failures
    // never reach either path.
    finish()
    if (pointerWatch !== undefined) restorePointerFocusAfterFailure(d.id, token, pointerWatch)
  }

  // ---- pointer flow ----------------------------------------------------

  function targetAt(x: number, y: number): { scope: string; index: number } | null {
    const d = drag.value
    if (!d) return null
    for (const scope of options.scopes()) {
      const el = scope.el()
      if (!el) continue
      const rect = el.getBoundingClientRect()
      if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) continue
      if (scope.positional === false) {
        if (!document.elementsFromPoint(x, y).includes(el)) continue
      }
      if (!allowed(d.id, d.fromScope, scope.id)) return null
      const ids = visibleIds(scope.id, d.id)
      if (scope.positional === false) return { scope: scope.id, index: ids.length }
      const hasPlaceholder = d.toScope === scope.id
      const shift = hasPlaceholder ? placeholderShift(el) : 0
      const slots: DragSlot[] = []
      ids.forEach((id, i) => {
        const item = options.itemEl(id)
        if (!item) return
        const rect2 = layoutRect(item)
        slots.push({
          id,
          top: rect2.top - (hasPlaceholder && i >= d.index ? shift : 0),
          height: rect2.height,
        })
      })
      return { scope: scope.id, index: insertionIndex(slots, y) }
    }
    return null
  }

  function placeholderShift(scopeEl: HTMLElement): number {
    const height = session?.height ?? 0
    const gap = parseFloat(getComputedStyle(scopeEl).rowGap) || 0
    return height + gap
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
    overlay.querySelectorAll('[data-drag-handle]').forEach((n) => n.removeAttribute('data-drag-handle'))
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
    drag.value = { id: p.id, fromScope: p.scopeId, toScope: p.scopeId, index, mode: 'pointer' }
    // A pointer press focuses its grip in the browser. Keep that ownership so a
    // failed write can put focus back after the preview remounts the row; touch
    // and presses that never focused the grip start no watch.
    const active = document.activeElement
    if (active instanceof HTMLElement && active.dataset.dragHandle === p.id) startFocusWatch(p.id)
    announce('pickup', p.id, p.scopeId, index + 1, options.itemIds(p.scopeId).length)
    positionOverlay()
    updateTarget(e.clientX, e.clientY)
    session.raf = requestAnimationFrame(tick)
  }

  function onPointerdown(id: string, scopeId: string, e: PointerEvent): void {
    if (drag.value || pending || isCommitting()) return
    if (e.pointerType === 'mouse' && e.button !== 0) return
    if (!e.isPrimary || !options.canDrag(id, scopeId)) return
    const root = options.root.value
    if (!root) return
    pending = { id, scopeId, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY }
    root.setPointerCapture(e.pointerId)
    // A pointer gesture is a new interaction: any keyboard reveal still waiting
    // on its tick belongs to the gesture the user just left.
    interactionToken += 1
    endFocusWatch()
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
    if (drag.value && !isCommitting()) abort(true)
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

  // ---- keyboard flow ---------------------------------------------------

  function pickUp(id: string, scopeId: string): void {
    const ids = options.itemIds(scopeId)
    const index = ids.indexOf(id)
    // A pickup is a new interaction, superseding any deferred restore.
    interactionToken += 1
    origin = { id, scope: scopeId, index }
    drag.value = { id, fromScope: scopeId, toScope: scopeId, index, mode: 'keyboard' }
    options.root.value?.addEventListener('focusout', onFocusout)
    startFocusWatch(id)
    announce('pickup', id, scopeId, index + 1, ids.length)
    void reveal(id, interactionToken)
  }

  function neighbourScope(from: string, step: -1 | 1): string | null {
    const scopes = options.scopes()
    let i = scopes.findIndex((s) => s.id === from) + step
    for (; i >= 0 && i < scopes.length; i += step) {
      const s = scopes[i]
      if (s.positional === false) continue
      const d = drag.value
      if (d && !allowed(d.id, d.fromScope, s.id)) continue
      return s.id
    }
    return null
  }

  function keyboardMove(key: string): boolean {
    const d = drag.value
    if (!d) return false
    let toScope = d.toScope
    let index = d.index
    if (key === 'ArrowUp') {
      if (index === 0) return true
      index -= 1
    } else if (key === 'ArrowDown') {
      if (index >= visibleIds(toScope, d.id).length) return true
      index += 1
    } else if (options.horizontalScopes && (key === 'ArrowLeft' || key === 'ArrowRight')) {
      const next = neighbourScope(toScope, key === 'ArrowLeft' ? -1 : 1)
      if (next === null) return true
      toScope = next
      index = Math.min(index, visibleIds(next, d.id).length)
    } else {
      return false
    }
    drag.value = { ...d, toScope, index }
    announce('move', d.id, toScope, index + 1, visibleIds(toScope, d.id).length + 1)
    void reveal(d.id, interactionToken)
    return true
  }

  function onKeydown(id: string, scopeId: string, e: KeyboardEvent): void {
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return
    const d = drag.value
    const pickKey = e.key === ' ' || e.key === 'Enter'
    if (!d) {
      if (pending || isCommitting() || !pickKey || !options.canDrag(id, scopeId)) return
      e.preventDefault()
      e.stopPropagation()
      pickUp(id, scopeId)
      return
    }
    if (d.mode !== 'keyboard' || d.id !== id || isCommitting()) return
    if (pickKey) {
      e.preventDefault()
      e.stopPropagation()
      void commit()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      abort(true)
    } else if (keyboardMove(e.key)) {
      e.preventDefault()
      e.stopPropagation()
    }
  }

  // ---- public ----------------------------------------------------------

  function gripHandlers(id: string, scopeId: string) {
    return {
      onPointerdown: (e: PointerEvent) => onPointerdown(id, scopeId, e),
      onKeydown: (e: KeyboardEvent) => onKeydown(id, scopeId, e),
      onClick: (e: MouseEvent) => {
        e.stopPropagation()
        e.preventDefault()
      },
    }
  }

  function entriesFor(scopeId: string): PreviewEntry[] {
    const ids = options.itemIds(scopeId)
    const d = drag.value
    if (d?.mode === 'keyboard') {
      const rest = ids.filter((id) => id !== d.id)
      if (d.toScope === scopeId) rest.splice(d.index, 0, d.id)
      return rest.map((id): PreviewEntry => ({ kind: 'item', id }))
    }
    if (d && d.toScope === scopeId) return previewEntries(ids, d.id, d.index)
    return ids.map((id): PreviewEntry => ({ kind: 'item', id }))
  }

  function isSource(id: string): boolean {
    return drag.value?.mode === 'pointer' && drag.value.id === id
  }

  function isLifted(id: string): boolean {
    return drag.value?.mode === 'keyboard' && drag.value.id === id
  }

  function cancel(_reason?: string): void {
    if (drag.value) {
      abort(false)
      return
    }
    // Navigation or an unmount can land between a drop's write and its reveal,
    // or while the write itself is still pending.
    interactionToken += 1
    if (pending) teardownPointer()
    endFocusWatch()
  }

  async function move(id: string, scopeId: string, direction: MoveDirection): Promise<boolean> {
    if (drag.value || isCommitting() || !options.canDrag(id, scopeId)) return false
    const ids = options.itemIds(scopeId)
    const pos = ids.indexOf(id)
    if (pos < 0) return false
    let toScope = scopeId
    let index = pos
    if (direction === 'up') index = pos - 1
    else if (direction === 'down') index = pos + 1
    else if (options.horizontalScopes) {
      const scopes = options.scopes()
      const at = scopes.findIndex((s) => s.id === scopeId)
      const next = scopes[at + (direction === 'left' ? -1 : 1)]
      if (!next || next.positional === false) return false
      toScope = next.id
      index = Math.min(pos, visibleIds(next.id, id).length)
    } else {
      return false
    }
    if (toScope === scopeId && (index < 0 || index >= ids.length)) return false
    if (!allowed(id, scopeId, toScope)) return false
    const total = visibleIds(toScope, id).length + 1
    const token = interactionToken
    committingToken = token
    let ok = false
    try {
      ok = await options.onCommit({ id, fromScope: scopeId, toScope, index })
    } catch {
      ok = false
    }
    if (committingToken === token) committingToken = null
    if (token !== interactionToken) return ok
    if (ok) announce('drop', id, toScope, index + 1, total)
    else announcement.value = moveAnnouncement('fail', '', '', 0, 0)
    return ok
  }

  if (getCurrentScope()) onScopeDispose(() => cancel())

  return {
    drag: drag as Readonly<ShallowRef<DragState | null>>,
    announcement: announcement as Readonly<Ref<string>>,
    gripHandlers,
    entriesFor,
    isSource,
    isLifted,
    cancel,
    move,
  }
}
