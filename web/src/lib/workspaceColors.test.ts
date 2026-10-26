import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { contrastRatio } from './contrast'
import {
  DEFAULT_WORKSPACE_COLOR,
  activeWorkspaceAccent,
  workspaceAccent,
  rememberWorkspaceColor,
  normalizeWorkspaceColor,
  colorForWorkspace,
  WORKSPACE_COLOR_PRESETS,
} from './workspaceColors'

describe('normalizeWorkspaceColor', () => {
  it('defaults missing or blank values to amber', () => {
    expect(normalizeWorkspaceColor(undefined)).toBe(DEFAULT_WORKSPACE_COLOR)
    expect(normalizeWorkspaceColor(null)).toBe('amber')
    expect(normalizeWorkspaceColor('')).toBe('amber')
    expect(normalizeWorkspaceColor('  ')).toBe('amber')
  })

  it('accepts known preset ids case-insensitively', () => {
    for (const preset of WORKSPACE_COLOR_PRESETS) {
      expect(normalizeWorkspaceColor(preset.id)).toBe(preset.id)
      expect(normalizeWorkspaceColor(preset.id.toUpperCase())).toBe(preset.id)
    }
  })

  it('falls back to amber for unknown ids', () => {
    expect(normalizeWorkspaceColor('neon')).toBe('amber')
    expect(normalizeWorkspaceColor('red')).toBe('amber')
  })
})

describe('colorForWorkspace', () => {
  it('reads color from a workspace record', () => {
    expect(colorForWorkspace({ name: 'work', color: 'cyan' })).toBe('cyan')
    expect(colorForWorkspace({ name: 'personal' })).toBe('amber')
    expect(colorForWorkspace(null)).toBe('amber')
  })
})

describe('activeWorkspaceAccent', () => {
  it('is neutral before the list names the workspace, on a first visit', () => {
    localStorage.clear()
    expect(activeWorkspaceAccent([], 'personal')).toBe('neutral')
  })

  it('paints the last colour this browser showed until the list arrives', () => {
    localStorage.clear()
    rememberWorkspaceColor('cyan')
    expect(activeWorkspaceAccent([], 'personal')).toBe('cyan')
    expect(activeWorkspaceAccent([{ name: 'personal', color: 'violet' }], 'personal')).toBe('violet')
  })

  it('ignores a remembered value that is not a preset', () => {
    localStorage.clear()
    localStorage.setItem('ciao-workspace-color', 'neon')
    expect(activeWorkspaceAccent([], 'personal')).toBe('neutral')
  })

  it('keeps another workspace neutral until the list arrives', () => {
    localStorage.clear()
    rememberWorkspaceColor('cyan')
    expect(workspaceAccent([], 'work', 'personal')).toBe('neutral')
    expect(workspaceAccent([], 'personal', 'personal')).toBe('cyan')
  })

  it('gives a listed workspace without a colour the default', () => {
    expect(activeWorkspaceAccent([{ name: 'personal' }], 'personal')).toBe(DEFAULT_WORKSPACE_COLOR)
  })
})

describe('accent presets', () => {
  const base = ['amber', 'pink', 'cyan', 'emerald', 'violet']
  const extra = ['blue', 'teal', 'lime', 'fuchsia']

  it('lists nine presets: the original five, then four extra', () => {
    expect(WORKSPACE_COLOR_PRESETS).toHaveLength(9)
    expect(WORKSPACE_COLOR_PRESETS.map((p) => p.id)).toEqual([...base, ...extra])
    for (const preset of WORKSPACE_COLOR_PRESETS) {
      expect(preset.extra === true).toBe(extra.includes(preset.id))
    }
  })

  it('accepts the new ids', () => {
    for (const id of extra) expect(normalizeWorkspaceColor(id)).toBe(id)
  })

  const css = readFileSync(resolve(__dirname, '../App.vue'), 'utf8')
  function accentPair(id: string, light: boolean): { accent: string; strong: string } {
    const selector = `${light ? ':root.theme-light ' : ''}[data-workspace-color="${id}"]`
    const start = css.indexOf(`${selector} {`)
    const hit = [...css.matchAll(/(:root\.theme-light )?\[data-workspace-color="([a-z]+)"\]\s*\{([^}]*)\}/g)]
      .find((m) => m[2] === id && Boolean(m[1]) === light)
    expect(hit, `${selector} block (${start})`).toBeTruthy()
    const body = hit![3]
    return {
      accent: /--accent:\s*(#[0-9a-fA-F]{6})/.exec(body)![1],
      strong: /--accent-strong:\s*(#[0-9a-fA-F]{6})/.exec(body)![1],
    }
  }

  it('every preset swatch and CSS accent pair keeps --on-accent at AA', () => {
    for (const preset of WORKSPACE_COLOR_PRESETS) {
      const dark = accentPair(preset.id, false)
      expect(dark.accent.toLowerCase(), `${preset.id} swatch`).toBe(preset.swatch)
      expect(contrastRatio('#1a1a2e', dark.accent), `${preset.id} dark on-accent`).toBeGreaterThanOrEqual(4.5)
      expect(contrastRatio('#1a1a2e', dark.strong), `${preset.id} dark strong`).toBeGreaterThanOrEqual(4.5)
      expect(contrastRatio(dark.accent, '#1a1a2e'), `${preset.id} dark text`).toBeGreaterThanOrEqual(4.5)

      const light = accentPair(preset.id, true)
      expect(contrastRatio('#ffffff', light.accent), `${preset.id} light on-accent`).toBeGreaterThanOrEqual(4.5)
      expect(contrastRatio('#ffffff', light.strong), `${preset.id} light strong`).toBeGreaterThanOrEqual(4.5)
    }
  })
})
