/**
 * Engine-wide appearance preferences, applied to <html>.
 *
 * The engine owns the values (`/api/settings/appearance`); localStorage holds
 * paint caches only, so a device repaints its last known look before the first
 * fetch finishes. No read path prefers a cache over the engine once loaded.
 */

import { checkCustomPair, type ColorPair, type CustomTokens, type ThemeMode } from './contrast'

export type TextContrast = 'normal' | 'soft'
export type BodyFont = 'system' | 'serif' | 'mono'
export type Theme = 'dark' | 'light' | 'system'

export interface AppearancePayload {
  text_contrast: TextContrast
  body_font: BodyFont
  /** `null` until a browser first stores one on the engine. */
  theme: Theme | null
  /** `null` until a browser first stores one on the engine. */
  font_scale: number | null
  /** A background and text colour per theme; a mode without a pair is unset. */
  custom_colors: Partial<Record<ThemeMode, ColorPair>>
  revision: string
}

export const DEFAULT_APPEARANCE: AppearancePayload = {
  text_contrast: 'normal',
  body_font: 'system',
  theme: null,
  font_scale: null,
  custom_colors: {},
  revision: '',
}

/**
 * The `--bg`/`--fg` of each built-in theme, mirrored from App.vue so the
 * Custom colours dialog can seed the mode that is not showing. A test locks
 * them to App.vue.
 */
export const DEFAULT_PAIRS: Record<ThemeMode, ColorPair> = {
  dark: { bg: '#1a1a2e', fg: '#e8e8f0' },
  light: { bg: '#f4f4fa', fg: '#1a1a2e' },
}

export const CUSTOM_TOKEN_NAMES: (keyof CustomTokens)[] = [
  '--bg', '--bg2', '--bg3', '--bg-elev', '--fg', '--fg2', '--fg3', '--border', '--border-strong',
]

export const APPEARANCE_CACHE_KEY = 'ciao-appearance'
/** Paint cache of the engine theme; main.ts restores it before mount. */
export const THEME_STORAGE_KEY = 'ciao-theme'

export function isTheme(value: unknown): value is Theme {
  return value === 'dark' || value === 'light' || value === 'system'
}

export function isTextContrast(value: unknown): value is TextContrast {
  return value === 'normal' || value === 'soft'
}

export function isBodyFont(value: unknown): value is BodyFont {
  return value === 'system' || value === 'serif' || value === 'mono'
}

/** Sets the data attributes and inline properties the engine value drives. */
export function applyAppearanceToRoot(value: Partial<AppearancePayload>, root: HTMLElement): void {
  if (value.text_contrast === 'soft') root.dataset.textContrast = 'soft'
  else if (value.text_contrast === 'normal') delete root.dataset.textContrast
  if (value.body_font === 'serif' || value.body_font === 'mono') root.dataset.bodyFont = value.body_font
  else if (value.body_font === 'system') delete root.dataset.bodyFont
  if (value.custom_colors !== undefined) applyCustomColors(value.custom_colors, root)
}

/**
 * Writes the active theme's custom pair as inline tokens on `root`. A pair
 * that does not pass the readability check (a hand-edited file or request) is
 * ignored rather than applied.
 */
function applyCustomColors(colors: AppearancePayload['custom_colors'], root: HTMLElement): void {
  for (const name of CUSTOM_TOKEN_NAMES) root.style.removeProperty(name)
  delete root.dataset.customColors
  const mode: ThemeMode = root.classList.contains('theme-light') ? 'light' : 'dark'
  const pair = colors?.[mode]
  if (!pair) return
  const result = checkCustomPair(pair)
  if (!result.ok) return
  for (const name of CUSTOM_TOKEN_NAMES) root.style.setProperty(name, result.tokens[name])
  root.dataset.customColors = mode
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
