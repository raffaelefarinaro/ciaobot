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
 * (Cmd/Ctrl+Shift+= and Cmd/Ctrl+Shift+-) do the same. The percentage that
 * Settings displays (100% at DEFAULT) is a UI detail of Settings, not of the
 * underlying scale — callers that need it should divide by
 * `DEFAULT_FONT_SCALE` themselves.
 *
 * The ref is module-scoped, so every caller shares one value. A per-caller ref
 * seeded from localStorage looks equivalent and is not: ChatLayout reads the
 * scale once at app mount and lives for the whole session, so after Settings
 * moved the scale, the shortcut's `adjust(+0.05)` added its delta to
 * ChatLayout's stale copy and the font jumped *backwards*. Sharing the ref is
 * what actually makes the two surfaces agree.
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

// One ref for the whole tab — see the note above on why this is not per-caller.
const fontScale = ref(readPersistedScale())

function set(next: number) {
  const clamped = clampScale(next)
  fontScale.value = clamped
  applyFontScale(clamped)
  writePersistedScale(clamped)
}

function adjust(delta: number) {
  // Round to 2dp so the 0.05 step does not drift.
  set(parseFloat((fontScale.value + delta).toFixed(2)))
}

function reset() {
  set(DEFAULT_FONT_SCALE)
}

/**
 * Returns the shared reactive `fontScale` plus its mutators. `set(next)`
 * updates the CSS variable and localStorage; `adjust(delta)` adds `delta`
 * clamped to `[MIN_FONT_SCALE, MAX_FONT_SCALE]`; `reset()` returns to
 * `DEFAULT_FONT_SCALE`.
 */
export function useFontScale(): {
  fontScale: Ref<number>
  set: (next: number) => void
  adjust: (delta: number) => void
  reset: () => void
} {
  return { fontScale, set, adjust, reset }
}
