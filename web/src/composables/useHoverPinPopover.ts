import { computed, ref, type ComputedRef, type Ref } from 'vue'

// Shared hover-preview / click-to-pin read popover used by the chat transcript
// (ChatPanel) and the pinned file viewer (PinnedFilePanel). Both surfaces show
// the same thing on a commented element: preview it on hover, keep it open when
// the pointer moves into the popover itself, and pin it on click until dismissed.
// Only the element lookup and the clamp math differ, so those are injected.

type PopoverAnchor = { top: number; left: number }

export type HoverPinPopover = {
  id: string
  top: number
  left: number
  pinned: boolean
}

export type HoverPinPopoverOptions<T> = {
  /** Resolve the commented element under a pointer event, or null if there is none. */
  resolveTarget: (event: MouseEvent) => HTMLElement | null
  /** Position the popover for an anchor element. Return null to abort the open. */
  anchorFor: (el: HTMLElement) => PopoverAnchor | null
  /** Resolve the comment a popover id points at, for the `comment` computed. */
  findComment: (id: string) => T | null
  /**
   * Cheap guard run before any DOM walk. Return false when the surface has no
   * comments at all, so mouse movement over a plain transcript or file costs
   * nothing. Defaults to always-true.
   */
  hasTargets?: () => boolean
  /** Side effect when a popover gets pinned, e.g. closing an open editor. */
  onPin?: () => void
  /** Grace period before a hover-preview closes, in ms. */
  closeDelayMs?: number
}

// One MediaQueryList for the app instead of one per pointer event.
let hoverMedia: MediaQueryList | null | undefined
function hoverCapable(): boolean {
  if (typeof window === 'undefined') return false
  if (hoverMedia === undefined) hoverMedia = window.matchMedia('(hover: hover)')
  return hoverMedia?.matches ?? false
}

export function useHoverPinPopover<T>(options: HoverPinPopoverOptions<T>): {
  popover: Ref<HoverPinPopover | null>
  comment: ComputedRef<T | null>
  show: (id: string, el: HTMLElement, pinned: boolean) => void
  close: () => void
  clearPendingClose: () => void
  onTargetOver: (event: MouseEvent) => void
  onTargetOut: (event: MouseEvent) => void
  onPopoverEnter: () => void
  onPopoverLeave: () => void
} {
  const closeDelayMs = options.closeDelayMs ?? 160
  const popover = ref<HoverPinPopover | null>(null)
  const comment = computed(() => (popover.value ? options.findComment(popover.value.id) : null))

  let closeTimer: ReturnType<typeof setTimeout> | null = null

  function clearPendingClose(): void {
    if (closeTimer) {
      clearTimeout(closeTimer)
      closeTimer = null
    }
  }

  function close(): void {
    clearPendingClose()
    popover.value = null
  }

  function scheduleClose(): void {
    clearPendingClose()
    closeTimer = setTimeout(() => {
      closeTimer = null
      if (popover.value && !popover.value.pinned) popover.value = null
    }, closeDelayMs)
  }

  function show(id: string, el: HTMLElement, pinned: boolean): void {
    // Don't demote an already-pinned popover to a hover preview.
    if (!pinned && popover.value?.pinned && popover.value.id === id) {
      clearPendingClose()
      return
    }
    const anchor = options.anchorFor(el)
    if (!anchor) return
    clearPendingClose()
    if (pinned) options.onPin?.()
    popover.value = { id, top: anchor.top, left: anchor.left, pinned }
  }

  // Resolve the element for an event, ignoring moves that stay inside it.
  function targetFor(event: MouseEvent): HTMLElement | null {
    if (options.hasTargets && !options.hasTargets()) return null
    const el = options.resolveTarget(event)
    if (!el) return null
    const related = event.relatedTarget as Node | null
    if (related && el.contains(related)) return null
    return el
  }

  function onTargetOver(event: MouseEvent): void {
    if (!hoverCapable()) return
    const el = targetFor(event)
    const id = el?.dataset.commentId
    if (!el || !id) return
    show(id, el, false)
  }

  function onTargetOut(event: MouseEvent): void {
    if (!popover.value || popover.value.pinned) return
    if (!targetFor(event)) return
    scheduleClose()
  }

  function onPopoverEnter(): void {
    clearPendingClose()
  }

  function onPopoverLeave(): void {
    if (popover.value && !popover.value.pinned) scheduleClose()
  }

  return {
    popover,
    comment,
    show,
    close,
    clearPendingClose,
    onTargetOver,
    onTargetOut,
    onPopoverEnter,
    onPopoverLeave,
  }
}
