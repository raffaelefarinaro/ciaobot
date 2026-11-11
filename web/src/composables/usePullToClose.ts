import { computed, ref, type ComputedRef, type Ref } from 'vue'

/** Past this many px of downward pull, letting go closes the pane. */
export const PULL_CLOSE_PX = 120
/** A downward flick this fast (px per ms) past FLICK_MIN_PX also closes. */
export const PULL_FLICK_VELOCITY = 0.6
export const PULL_FLICK_MIN_PX = 40
const SETTLE_MS = 280

function reducedMotion(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** Whether a pull released at `dy` px moving at `velocity` px/ms closes the pane. */
export function pullCloses(dy: number, velocity: number): boolean {
  return dy >= PULL_CLOSE_PX || (velocity > PULL_FLICK_VELOCITY && dy > PULL_FLICK_MIN_PX)
}

/**
 * Pull to close: dragging an open item's title down (the `pull` / `pull-end`
 * events of `TitleRail`) moves the whole pane with the finger, shrinking it and
 * rounding its corners over a hint naming where letting go leads, like
 * dismissing a story. Past PULL_CLOSE_PX (or a downward flick) the pane drops
 * away and `close` runs; otherwise it springs back. Under reduced motion the
 * close is immediate.
 *
 * `invalidate` cancels a pull whose settle animation is still running, so a
 * delayed close belongs to the item and view where the pull started.
 */
export function usePullToClose(close: () => void, disabled: () => boolean = () => false): {
  pullY: Ref<number>
  pullStyle: ComputedRef<Record<string, string | number | undefined> | undefined>
  onPull: (dy: number) => void
  onPullEnd: (dy: number, velocity: number) => void
  invalidate: () => void
} {
  const pullY = ref(0)
  const settling = ref(false)
  let context = 0

  function invalidate(): void {
    context++
    pullY.value = 0
    settling.value = false
  }

  const pullStyle = computed(() => {
    if (pullY.value <= 0 && !settling.value) return undefined
    const p = Math.min(1, pullY.value / 400)
    return {
      transform: `translateY(${pullY.value}px) scale(${1 - p * 0.12})`,
      transformOrigin: '50% 0',
      borderRadius: `${Math.min(28, pullY.value / 4)}px`,
      overflow: 'hidden',
      boxShadow: pullY.value > 0 ? '0 -10px 40px -10px rgb(0 0 0 / 45%)' : undefined,
      transition: settling.value ? `transform ${SETTLE_MS}ms var(--ease), border-radius ${SETTLE_MS}ms var(--ease)` : 'none',
      position: 'relative',
      zIndex: 1,
    }
  })

  function onPull(dy: number): void {
    if (disabled()) return
    if (settling.value) context++
    settling.value = false
    pullY.value = dy
  }

  function onPullEnd(dy: number, velocity: number): void {
    if (disabled()) return
    const started = context
    const closes = pullCloses(dy, velocity)
    if (closes && reducedMotion()) { pullY.value = 0; close(); return }
    settling.value = true
    if (closes) {
      pullY.value = Math.max(window.innerHeight, dy + 1)
      setTimeout(() => {
        if (started !== context) return
        settling.value = false
        pullY.value = 0
        close()
      }, SETTLE_MS)
    } else {
      pullY.value = 0
      setTimeout(() => { if (started === context) settling.value = false }, SETTLE_MS)
    }
  }

  return { pullY, pullStyle, onPull, onPullEnd, invalidate }
}
