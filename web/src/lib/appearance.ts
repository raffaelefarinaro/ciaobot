/**
 * Engine-wide appearance preferences, applied to <html>.
 *
 * The engine owns the values (`/api/settings/appearance`); localStorage holds
 * paint caches only, so a device repaints its last known look before the first
 * fetch finishes. No read path prefers a cache over the engine once loaded.
 */

export type TextContrast = 'normal' | 'soft'
export type Theme = 'dark' | 'light' | 'system'

export interface AppearancePayload {
  text_contrast: TextContrast
  /** `null` until a browser first stores one on the engine. */
  theme: Theme | null
  /** `null` until a browser first stores one on the engine. */
  font_scale: number | null
  revision: string
}

export const DEFAULT_APPEARANCE: AppearancePayload = {
  text_contrast: 'normal',
  theme: null,
  font_scale: null,
  revision: '',
}

export const APPEARANCE_CACHE_KEY = 'ciao-appearance'
/** Paint cache of the engine theme; main.ts restores it before mount. */
export const THEME_STORAGE_KEY = 'ciao-theme'

export function isTheme(value: unknown): value is Theme {
  return value === 'dark' || value === 'light' || value === 'system'
}

export function isTextContrast(value: unknown): value is TextContrast {
  return value === 'normal' || value === 'soft'
}

/** Sets the data attributes and inline properties the engine value drives. */
export function applyAppearanceToRoot(value: Partial<AppearancePayload>, root: HTMLElement): void {
  if (value.text_contrast === 'soft') root.dataset.textContrast = 'soft'
  else if (value.text_contrast === 'normal') delete root.dataset.textContrast
}

/** This browser's stored theme choice, `system` when it has none. */
export function readLocalTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY)
    return isTheme(stored) ? stored : 'system'
  } catch {
    return 'system'
  }
}

/** Applies a theme choice: toggles `theme-light` and keeps the local cache. */
export function applyTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch { /* localStorage blocked */ }
  const classes = document.documentElement.classList
  if (theme === 'light') {
    classes.add('theme-light')
  } else if (theme === 'dark') {
    classes.remove('theme-light')
  } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
    classes.remove('theme-light')
  } else {
    classes.add('theme-light')
  }
}

export function writeAppearanceCache(value: AppearancePayload): void {
  try {
    localStorage.setItem(APPEARANCE_CACHE_KEY, JSON.stringify(value))
  } catch { /* localStorage blocked */ }
}

export function restoreAppearanceFromCache(root: HTMLElement): void {
  try {
    const raw = localStorage.getItem(APPEARANCE_CACHE_KEY)
    if (!raw) return
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object') {
      applyAppearanceToRoot(parsed as Partial<AppearancePayload>, root)
    }
  } catch { /* blocked storage or malformed JSON: paint the defaults */ }
}
