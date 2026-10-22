import { ref, watch } from 'vue'
import { api } from '../lib/api'
import {
  DEFAULT_APPEARANCE,
  applyAppearanceToRoot,
  applyTheme,
  isTextContrast,
  isTheme,
  readLocalTheme,
  writeAppearanceCache,
  type AppearancePayload,
} from '../lib/appearance'
import { FONT_SCALE_STEP, MAX_FONT_SCALE, MIN_FONT_SCALE, useFontScale } from './useFontScale'

const settings = ref<AppearancePayload>({ ...DEFAULT_APPEARANCE })
const loaded = ref(false)
let loading: Promise<void> | null = null
let seeded = false
// Writes go one at a time: each carries the revision the previous one returned.
let writes: Promise<unknown> = Promise.resolve()

const { fontScale, set: setFontScale } = useFontScale()

function round2(value: number): number {
  return Math.round(value * 100) / 100
}

/** Nearest step of the engine's 0.05 grid, clamped to the allowed range. */
function snapFontScale(value: number): number {
  const steps = Math.round((value - MIN_FONT_SCALE) / FONT_SCALE_STEP)
  return round2(Math.min(MAX_FONT_SCALE, Math.max(MIN_FONT_SCALE, MIN_FONT_SCALE + steps * FONT_SCALE_STEP)))
}

function validFontScale(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
    && value >= MIN_FONT_SCALE - 1e-9 && value <= MAX_FONT_SCALE + 1e-9
}

/** Applies an engine value (GET, PATCH result, snapshot or change event). */
export function applyAppearanceSettings(value: Partial<AppearancePayload>): void {
  if (isTextContrast(value.text_contrast)) settings.value.text_contrast = value.text_contrast
  if (value.theme === null) settings.value.theme = null
  else if (isTheme(value.theme)) {
    settings.value.theme = value.theme
    applyTheme(value.theme)
  }
  if (value.font_scale === null) settings.value.font_scale = null
  else if (validFontScale(value.font_scale)) {
    settings.value.font_scale = round2(value.font_scale)
    // A local change that is waiting to be saved, or is being saved, is newer
    // than whatever the engine just said: keep it and let the save settle it.
    if (!fontChangePending()) setFontScale(settings.value.font_scale)
  }
  if (typeof value.revision === 'string') settings.value.revision = value.revision
  loaded.value = true
  applyAppearanceToRoot(settings.value, document.documentElement)
  writeAppearanceCache(settings.value)
  seedMissing()
}

/**
 * An engine that has never stored a theme or font scale takes this browser's
 * current values once; after that the engine wins. One attempt per page life:
 * a lost race (409) is settled by the winner's change event.
 */
function seedMissing(): void {
  if (seeded) return
  const patch: Partial<AppearancePayload> = {}
  if (settings.value.theme === null) patch.theme = readLocalTheme()
  if (settings.value.font_scale === null) patch.font_scale = snapFontScale(fontScale.value)
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
    if (settings.value.font_scale !== null) setFontScale(settings.value.font_scale)
    return
  } finally {
    fontSaves--
  }
  if (fontChangePending()) return
  if (round2(fontScale.value) !== sent) {
    // The user moved again while the save was in flight: save that too.
    void flushFontScale()
  } else if (settings.value.font_scale !== null) {
    // Nothing newer locally: whatever the engine stored is what we show.
    setFontScale(settings.value.font_scale)
  }
}

watch(fontScale, (next) => {
  if (!loaded.value || round2(next) === settings.value.font_scale) return
  if (fontTimer) clearTimeout(fontTimer)
  fontTimer = setTimeout(() => {
    fontTimer = null
    if (round2(fontScale.value) !== settings.value.font_scale) void flushFontScale()
  }, 300)
})

export function useAppearanceSettings(): {
  settings: typeof settings
  loaded: typeof loaded
  load: () => Promise<void>
  save: (patch: Partial<AppearancePayload>) => Promise<void>
} {
  return { settings, loaded, load, save }
}
