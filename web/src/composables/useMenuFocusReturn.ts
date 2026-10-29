import { getCurrentScope, nextTick, onScopeDispose } from 'vue'

/**
 * One menu action's claim on focus. `restore` is bound to the `arm` that made it,
 *
 * so an older write that settles after a newer action has started cannot move
 * focus, end the newer watch, or scroll the page: it no longer owns the watch.
 */
export interface MenuFocusAction {
  /**
   * Focus `target()` after the next DOM flush, unless this action was superseded
   * or the user has deliberately focused or pressed elsewhere since it started.
   */
  restore: (target: () => HTMLElement | null) => Promise<void>
  /** Whether this action no longer owns the watch, or the user has moved on. */
  departed: () => boolean
  /** End this action's watch if it still owns it; a newer watch is untouched. */
  cancel: () => void
}

export interface MenuFocusReturn {
  /**
   * Start watching for a departure from `trigger` and return this action's
   * restore. A newer `arm` supersedes the previous watch.
   */
  arm: (trigger: HTMLElement | null) => MenuFocusAction
}

interface Watch {
  token: number
  trigger: HTMLElement | null
  left: boolean
}

/** Whether a focus or press target belongs to the trigger (including itself). */
function belongsTo(target: EventTarget | null, trigger: HTMLElement | null): boolean {
  return target instanceof Node && trigger !== null && trigger.contains(target)
}

export function useMenuFocusReturn(): MenuFocusReturn {
  let watch: Watch | null = null
  let tokenSeq = 0

  function onFocusIn(event: FocusEvent): void {
    if (!watch) return
    if (belongsTo(event.target, watch.trigger)) return
    watch.left = true
  }

  function onPointerDown(event: PointerEvent): void {
    if (!watch) return
    if (belongsTo(event.target, watch.trigger)) return
    watch.left = true
  }

  function listen(on: boolean): void {
    if (on) {
      document.addEventListener('focusin', onFocusIn, true)
      document.addEventListener('pointerdown', onPointerDown, true)
    } else {
      document.removeEventListener('focusin', onFocusIn, true)
      document.removeEventListener('pointerdown', onPointerDown, true)
    }
  }

  /** End the current watch, or only the watch with `token` if it is still current. */
  function disarm(token?: number): void {
    if (!watch) return
    if (token !== undefined && watch.token !== token) return
    listen(false)
    watch = null
  }

  function arm(trigger: HTMLElement | null): MenuFocusAction {
    disarm()
    const token = ++tokenSeq
    watch = { token, trigger, left: false }
    listen(true)

    return {
      departed: (): boolean => !watch || watch.token !== token || watch.left,
      cancel: (): void => disarm(token),
      restore: async (target: () => HTMLElement | null): Promise<void> => {
        // This action was superseded before it settled; the newer watch owns
        // focus and must not be ended or moved by this one.
        if (!watch || watch.token !== token) return
        if (watch.left) {
          disarm(token)
          return
        }
        await nextTick()
        if (!watch || watch.token !== token) return
        if (watch.left) {
          disarm(token)
          return
        }
        const active = document.activeElement
        if (
          active &&
          active !== document.body &&
          active !== document.documentElement &&
          !belongsTo(active, watch.trigger)
        ) {
          disarm(token)
          return
        }
        const destination = target()
        if (!destination) {
          disarm(token)
          return
        }
        destination.scrollIntoView({ block: 'nearest', inline: 'nearest' })
        destination.focus()
        disarm(token)
      },
    }
  }

  if (getCurrentScope()) onScopeDispose(() => disarm())

  return { arm }
}
