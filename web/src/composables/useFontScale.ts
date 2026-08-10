import { ref, type Ref } from 'vue'

/**
 * Reactive app-wide font scale.
 *
 * The app exposes a `--font-scale` CSS variable that every text size multiplies
 * against (see App.vue). This composable owns that variable on the JS side: it
 * reads the persisted value from localStorage at startup, mirrors changes
 * back to both the CSS variable and localStorage, and clamps to a sensible
 * range so the UI stays usable at the extremes.
 *
 * Settings has +/- buttons that adjust by ±0.05; the global keyboard shortcuts
 * (Cmd/Ctrl+Shift+= and Cmd/Ctrl+Shift+-) do the same so both surfaces stay in
 * lockstep. The percentage that Settings displays (100% at DEFAULT) is a UI
 * detail of Settings, not of the underlying scale — callers that need it
 * should divide by `DEFAULT_FONT_SCALE` themselves.
 *
 * The composable is intentionally cheap and stateless across callers: each
 * `useFontScale()` call returns a ref seeded from `localStorage`, and every
 * write goes through the same CSS variable + storage key, so any caller in
 * the running tab stays in sync via the CSS variable (which Vue cannot miss).
 * The ref a caller holds is only as fresh as its last `set`/`adjust`/`reset`;
 * that is enough for the Settings +/-/Reset disable checks, which is the only
 * place we render the value to the user.
 */

export const DEFAULT_FONT_SCALE = 1.2
export const MIN_FONT_SCALE = 0.8
export const MAX_FONT_SCALE = 1.5
/** Step used by Settings +/- buttons and the global zoom shortcuts. */
export const FONT_SCALE_STEP = 0.05
/** localStorage key shared with main.ts (early restore) and SettingsView. */
export const FONT_SCALE_STORAGE_KEY = 'ciao-font-scale'

function clampScale(value: number): number {
  if (value < MIN_FONT_SCALE) return MIN_FONT_SCALE
  if (value > MAX_FONT_SCALE) return MAX_FONT_SCALE
  return value
}

function applyFontScale(value: number): void {
  if (typeof document === 'undefined') return
  document.documentElement.style.setProperty('--font-scale', value.toString())
}

function readPersistedScale(): number {
  try {
    const raw = localStorage.getItem(FONT_SCALE_STORAGE_KEY)
    if (!raw) return DEFAULT_FONT_SCALE
    const parsed = parseFloat(raw)
    if (!Number.isFinite(parsed)) return DEFAULT_FONT_SCALE
    return clampScale(parsed)
  } catch {
    return DEFAULT_FONT_SCALE
  }
}

function writePersistedScale(value: number): void {
  try {
    localStorage.setItem(FONT_SCALE_STORAGE_KEY, value.toString())
  } catch { /* localStorage blocked (private mode, quota): keep the in-memory value */ }
}

/**
 * Returns a reactive `fontScale` ref seeded from the persisted value. The ref
 * mirrors the CSS variable for the lifetime of the caller. `set(next)`
 * mutates both the variable and localStorage; `adjust(delta)` adds `delta`
 * clamped to `[MIN_FONT_SCALE, MAX_FONT_SCALE]`; `reset()` returns to
 * `DEFAULT_FONT_SCALE`.
 */
export function useFontScale(): {
  fontScale: Ref<number>
  set: (next: number) => void
  adjust: (delta: number) => void
  reset: () => void
} {
  const fontScale = ref(readPersistedScale())

  function set(next: number) {
    const clamped = clampScale(next)
    fontScale.value = clamped
    applyFontScale(clamped)
    writePersistedScale(clamped)
  }

  function adjust(delta: number) {
    // Mirror SettingsView: round to 2dp so the 0.05 step does not drift.
    const next = parseFloat((fontScale.value + delta).toFixed(2))
    set(next)
  }

  function reset() {
    set(DEFAULT_FONT_SCALE)
  }

  return { fontScale, set, adjust, reset }
}
