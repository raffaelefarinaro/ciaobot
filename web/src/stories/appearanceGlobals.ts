/**
 * The workshop's appearance toolbar, applied through the same root hooks the
 * engine uses. Pure: the preview decorator owns when this runs, so nothing
 * here imports Vue.
 *
 * It never calls `applyTheme` (`lib/appearance.ts`): that writes `ciao-theme`
 * to localStorage, and a toolbar click in the workshop must not leave a
 * preference behind for the app on the same origin.
 */

import { applyAppearanceToRoot, type BodyFont, type TextContrast } from '../lib/appearance'

export type StoryTheme = 'dark' | 'light'

export interface StoryAppearanceGlobals {
  /** Toolbar global; anything but `light` reads as the app's dark default. */
  theme?: StoryTheme
  contrast?: TextContrast
  font?: BodyFont
}

const LIGHT_CLASS = 'theme-light'

/** Applies the globals to `root` and returns a restore of its prior state. */
export function applyStoryAppearance(globals: StoryAppearanceGlobals, root: HTMLElement): () => void {
  const priorClass = root.getAttribute('class')
  const priorDataset = Object.entries(root.dataset) as [string, string][]
  apply(globals, root)
  return () => {
    if (priorClass === null) root.removeAttribute('class')
    else root.setAttribute('class', priorClass)
    for (const name of Object.keys(root.dataset)) delete root.dataset[name]
    for (const [name, value] of priorDataset) root.dataset[name] = value
  }
}

/**
 * The engine's own mapping: the light class is a class, contrast and body font
 * are `data-*` attributes that the stylesheet reads. `applyAppearanceToRoot`
 * removes the attributes for the defaults, so re-applying is idempotent.
 */
function apply(globals: StoryAppearanceGlobals, root: HTMLElement): void {
  root.classList.toggle(LIGHT_CLASS, globals.theme === 'light')
  const contrast: TextContrast = globals.contrast === 'soft' ? 'soft' : 'normal'
  const font: BodyFont = globals.font === 'serif' || globals.font === 'mono' ? globals.font : 'system'
  applyAppearanceToRoot({ text_contrast: contrast, body_font: font }, root)
}
