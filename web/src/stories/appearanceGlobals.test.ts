// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest'
import { APPEARANCE_CACHE_KEY, THEME_STORAGE_KEY } from '../lib/appearance'
import { applyStoryAppearance, type StoryAppearanceGlobals } from './appearanceGlobals'

// The root starts in a state the toolbar changes in both directions: a class it
// would remove, a `data-*` it would overwrite and a `data-*` it would delete.
let root: HTMLElement

beforeEach(() => {
  localStorage.clear()
  root = document.createElement('div')
  root.setAttribute('class', 'workshop theme-light story-host')
  root.dataset.workspaceColor = 'pink'
  root.dataset.textContrast = 'soft'
})

describe('story appearance globals', () => {
  it('sets the light theme class and clears it for dark, even when light repeats', () => {
    root.classList.remove('theme-light')
    applyStoryAppearance({ theme: 'light' }, root)
    expect(root.classList.contains('theme-light')).toBe(true)
    applyStoryAppearance({ theme: 'dark' }, root)
    expect(root.classList.contains('theme-light')).toBe(false)
    applyStoryAppearance({ theme: 'light' }, root)
    expect(root.classList.contains('theme-light')).toBe(true)
    // A toggle-only implementation flips this back off.
    applyStoryAppearance({ theme: 'light' }, root)
    expect(root.classList.contains('theme-light')).toBe(true)
  })

  it('maps contrast and body font to the attributes the stylesheet reads', () => {
    applyStoryAppearance({ contrast: 'soft', font: 'serif' }, root)
    expect(root.dataset.textContrast).toBe('soft')
    expect(root.dataset.bodyFont).toBe('serif')
    applyStoryAppearance({ contrast: 'normal', font: 'mono' }, root)
    expect('textContrast' in root.dataset).toBe(false)
    expect(root.dataset.bodyFont).toBe('mono')
    applyStoryAppearance({ font: 'system' }, root)
    expect('bodyFont' in root.dataset).toBe(false)
  })

  it('reads missing or unknown globals as the app defaults', () => {
    const unknown = { theme: 'sepia', contrast: 'high', font: 'cursive' } as unknown as StoryAppearanceGlobals
    applyStoryAppearance(unknown, root)
    expect(root.classList.contains('theme-light')).toBe(false)
    expect('textContrast' in root.dataset).toBe(false)
    expect('bodyFont' in root.dataset).toBe(false)
    applyStoryAppearance({}, root)
    expect(root.classList.contains('theme-light')).toBe(false)
    expect('textContrast' in root.dataset).toBe(false)
  })

  it('restores the exact prior class list and dataset', () => {
    const priorClass = root.getAttribute('class')
    const priorDataset = { ...root.dataset }
    const reset = applyStoryAppearance({ theme: 'dark', contrast: 'normal', font: 'serif' }, root)
    expect(root.getAttribute('class')).toBe('workshop story-host')
    reset()
    expect(root.getAttribute('class')).toBe(priorClass)
    expect({ ...root.dataset }).toEqual(priorDataset)
  })

  it('leaves no appearance on a clean root after reset', () => {
    const clean = document.createElement('div')
    const reset = applyStoryAppearance({ theme: 'light', contrast: 'soft', font: 'serif' }, clean)
    expect(clean.getAttribute('class')).toBe('theme-light')
    reset()
    expect(clean.getAttribute('class')).toBeNull()
    expect(Object.keys(clean.dataset)).toEqual([])
  })

  it('restores the pre-toolbar state across a reset and re-apply cycle', () => {
    const priorClass = root.getAttribute('class')
    const priorDataset = { ...root.dataset }
    const first = applyStoryAppearance({ theme: 'light', font: 'mono' }, root)
    first()
    const second = applyStoryAppearance({ theme: 'dark', contrast: 'normal', font: 'system' }, root)
    second()
    expect(root.getAttribute('class')).toBe(priorClass)
    expect({ ...root.dataset }).toEqual(priorDataset)
  })

  it('never writes the engine or paint-cache storage keys', () => {
    applyStoryAppearance({ theme: 'light', contrast: 'soft', font: 'mono' }, root)
    applyStoryAppearance({ theme: 'dark' }, root)
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
    expect(localStorage.getItem(APPEARANCE_CACHE_KEY)).toBeNull()
    expect(localStorage.length).toBe(0)
  })
})
