import { describe, expect, it } from 'vitest'
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
