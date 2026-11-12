import { ref, watch } from 'vue'
import { api } from '../lib/api'
import { isLightTheme } from '../lib/theme'
import type { ColorPair } from '../lib/contrast'
import {
  DEFAULT_APPEARANCE,
  applyAppearanceToRoot,
  applyTheme,
  isBodyFont,
  isTextContrast,
  isTheme,
  readLocalTheme,
  restoreAppearanceFromCache,
  writeAppearanceCache,
  type AppearancePayload,
  type Theme,
} from '../lib/appearance'
import { DEFAULT_FONT_SCALE, MAX_FONT_SCALE, MIN_FONT_SCALE, useFontScale } from './useFontScale'

const settings = ref<AppearancePayload>({ ...DEFAULT_APPEARANCE })
const loaded = ref(false)
let loading: Promise<void> | null = null
let seeded = false
/** Theme saves queued or in flight. */
const themeSaves = ref(0)
// Writes go one at a time: each carries the revision the previous one returned.
let writes: Promise<unknown> = Promise.resolve()

const { fontScale, set: setFontScale } = useFontScale()

function round2(value: number): number {
  return Math.round(value * 100) / 100
}

function validFontScale(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
    && value >= MIN_FONT_SCALE - 1e-9 && value <= MAX_FONT_SCALE + 1e-9
}

/** What the engine says the scale is: an unset value is the shipped default. */
function engineFontScale(): number {
  return settings.value.font_scale ?? DEFAULT_FONT_SCALE
}

function validColors(value: unknown): value is AppearancePayload['custom_colors'] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  return Object.entries(value).every(([mode, pair]) => {
    if (mode !== 'dark' && mode !== 'light') return false
    const candidate = pair as Partial<ColorPair> | null
    return !!candidate && typeof candidate.bg === 'string' && typeof candidate.fg === 'string'
  })
}

/** Applies an engine value (GET, PATCH result, snapshot or change event). */
export function applyAppearanceSettings(value: Partial<AppearancePayload>): void {
  if (isTextContrast(value.text_contrast)) settings.value.text_contrast = value.text_contrast
  if (isBodyFont(value.body_font)) settings.value.body_font = value.body_font
  if (value.theme === null) settings.value.theme = null
  else if (isTheme(value.theme)) {
    settings.value.theme = value.theme
    // A choice still being saved is newer than the engine's answer to an
    // earlier one; saveTheme applies the engine value once the last settles.
    if (themeSaves.value === 0) applyTheme(value.theme)
  }
  if (value.font_scale === null || validFontScale(value.font_scale)) {
    settings.value.font_scale = value.font_scale === null ? null : round2(value.font_scale)
    // A local change that is waiting to be saved, or is being saved, is newer
    // than whatever the engine just said: keep it and let the save settle it.
    if (!fontChangePending()) setFontScale(engineFontScale())
  }
  if (validColors(value.custom_colors)) settings.value.custom_colors = value.custom_colors
  if (typeof value.revision === 'string') settings.value.revision = value.revision
  loaded.value = true
  applyAppearanceToRoot(settings.value, document.documentElement)
  writeAppearanceCache(settings.value)
  seedMissing()
}

/**
 * An engine that has never stored a theme takes this browser's theme once;
 * after that the engine wins. An unset font scale is the shipped default and
 * is never seeded, so a later default change still reaches installs nobody
 * resized (#2715). One attempt per page life: a lost race (409) is settled by
 * the winner's change event.
 */
function seedMissing(): void {
  if (seeded) return
  const patch: Partial<AppearancePayload> = {}
  if (settings.value.theme === null) patch.theme = readLocalTheme()
  if (Object.keys(patch).length === 0) return
  seeded = true
  void save(patch).catch(() => {})
}

async function load(): Promise<void> {
  if (loading) return loading
  loading = api.get<AppearancePayload>('/api/settings/appearance')
    .then(applyAppearanceSettings)
    .finally(() => { loading = null })
  return loading
}

function save(patch: Partial<AppearancePayload>): Promise<void> {
  const run = async (): Promise<void> => {
    try {
      const value = await api.patch<AppearancePayload>('/api/settings/appearance', {
        ...patch,
        revision: settings.value.revision,
      })
      applyAppearanceSettings(value)
    } catch (error) {
      if ((error as { status?: number }).status === 409) await load().catch(() => {})
      throw error
    }
  }
  const result = writes.then(run, run)
  writes = result.catch(() => {})
  return result
}

// Flipping Theme (or the OS scheme) switches which mode's custom pair shows.
watch(isLightTheme, () => {
  const root = document.documentElement
  // Before the first engine value, `settings` is still the defaults; applying
  // them would wipe the paint cache main.ts just restored.
  if (loaded.value) applyAppearanceToRoot(settings.value, root)
  else restoreAppearanceFromCache(root)
})

// Every writer of the font scale (Settings buttons, the zoom shortcuts) goes
// through useFontScale, so one debounced watcher persists them all to the engine.
let fontTimer: ReturnType<typeof setTimeout> | null = null
let fontSaves = 0

function fontChangePending(): boolean {
  return fontTimer !== null || fontSaves > 0
}

async function flushFontScale(): Promise<void> {
  const sent = round2(fontScale.value)
  fontSaves++
  try {
    await save({ font_scale: sent })
  } catch {
    // The engine wins on failure (a 409 already reloaded its value).
    setFontScale(engineFontScale())
    return
  } finally {
    fontSaves--
  }
  if (fontChangePending()) return
  if (round2(fontScale.value) !== sent) {
    // The user moved again while the save was in flight: save that too.
    void flushFontScale()
  } else {
    // Nothing newer locally: whatever the engine stored is what we show.
    setFontScale(engineFontScale())
  }
}

watch(fontScale, (next) => {
  if (!loaded.value || round2(next) === engineFontScale()) return
  if (fontTimer) clearTimeout(fontTimer)
  fontTimer = setTimeout(() => {
    fontTimer = null
    if (round2(fontScale.value) !== engineFontScale()) void flushFontScale()
  }, 300)
})

/** Applies a theme choice at once, then stores it on the engine. */
async function saveTheme(theme: Theme): Promise<void> {
  applyTheme(theme)
  themeSaves.value++
  try {
    await save({ theme })
  } finally {
    themeSaves.value--
    // After the last save the engine value is the truth (the user's last
    // choice on success, the previous one on failure).
    if (themeSaves.value === 0 && settings.value.theme) applyTheme(settings.value.theme)
  }
}

export function useAppearanceSettings(): {
  settings: typeof settings
  loaded: typeof loaded
  load: () => Promise<void>
  save: (patch: Partial<AppearancePayload>) => Promise<void>
  saveTheme: (theme: Theme) => Promise<void>
  themeSaves: typeof themeSaves
} {
  return { settings, loaded, load, save, saveTheme, themeSaves }
}
