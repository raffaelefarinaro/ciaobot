/**
 * Appearance fixture: the settings appearance singleton and its paint.
 *
 * `useAppearanceSettings` is a module singleton: one copy of the settings, the
 * save queue and the paint caches per page, shared by the app and by every
 * story that renders an appearance owner. A story seeds it through the
 * production path (`applyAppearanceSettings`) and its teardown restores exactly
 * the prior state, so a custom-colours story cannot leak its paint or its
 * storage into another story.
 */
import { applyAppearanceSettings } from '../composables/useAppearanceSettings'
import {
  APPEARANCE_CACHE_KEY,
  CUSTOM_TOKEN_NAMES,
  DEFAULT_APPEARANCE,
  THEME_STORAGE_KEY,
  type AppearancePayload,
} from '../lib/appearance'

/** The engine-shaped payload the fixture GET answers and the singleton is reset to. */
export function appearanceFixturePayload(
  overrides: Partial<AppearancePayload> = {},
): AppearancePayload {
  return { ...DEFAULT_APPEARANCE, theme: 'dark', revision: 'storybook-1', ...overrides }
}

/**
 * Seeds the appearance singleton through the production apply path (no fetch)
 * and returns the teardown. The teardown resets the singleton, then restores
 * the prior `<html>` class, dataset, inline custom tokens and the two storage
 * keys the production path may have written.
 */
export function seedAppearance(payload: Partial<AppearancePayload> = {}): () => void {
  const root = document.documentElement
  const priorClass = root.getAttribute('class')
  const priorStyle = root.getAttribute('style')
  const priorDataset = {
    textContrast: root.dataset.textContrast,
    bodyFont: root.dataset.bodyFont,
    customColors: root.dataset.customColors,
  }
  const priorTokens = CUSTOM_TOKEN_NAMES.map((name) => root.style.getPropertyValue(name))
  const priorCache = {
    appearance: localStorage.getItem(APPEARANCE_CACHE_KEY),
    theme: localStorage.getItem(THEME_STORAGE_KEY),
  }
  applyAppearanceSettings(appearanceFixturePayload(payload))
  return () => {
    applyAppearanceSettings(appearanceFixturePayload())
    if (priorClass === null) root.removeAttribute('class')
    else root.setAttribute('class', priorClass)
    if (priorStyle === null) root.removeAttribute('style')
    else root.setAttribute('style', priorStyle)
    delete root.dataset.textContrast
    delete root.dataset.bodyFont
    delete root.dataset.customColors
    if (priorDataset.textContrast !== undefined) root.dataset.textContrast = priorDataset.textContrast
    if (priorDataset.bodyFont !== undefined) root.dataset.bodyFont = priorDataset.bodyFont
    if (priorDataset.customColors !== undefined) root.dataset.customColors = priorDataset.customColors
    CUSTOM_TOKEN_NAMES.forEach((name, index) => {
      const prior = priorTokens[index]
      if (prior === '') root.style.removeProperty(name)
      else root.style.setProperty(name, prior)
    })
    if (priorCache.appearance === null) localStorage.removeItem(APPEARANCE_CACHE_KEY)
    else localStorage.setItem(APPEARANCE_CACHE_KEY, priorCache.appearance)
    if (priorCache.theme === null) localStorage.removeItem(THEME_STORAGE_KEY)
    else localStorage.setItem(THEME_STORAGE_KEY, priorCache.theme)
  }
}
