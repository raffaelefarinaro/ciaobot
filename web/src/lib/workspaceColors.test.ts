import { describe, expect, it } from 'vitest'
import {
  DEFAULT_WORKSPACE_COLOR,
  normalizeWorkspaceColor,
  colorForWorkspace,
  WORKSPACE_COLOR_PRESETS,
} from './workspaceColors'

describe('normalizeWorkspaceColor', () => {
  it('defaults missing or blank values to pink', () => {
    expect(normalizeWorkspaceColor(undefined)).toBe(DEFAULT_WORKSPACE_COLOR)
    expect(normalizeWorkspaceColor(null)).toBe('pink')
    expect(normalizeWorkspaceColor('')).toBe('pink')
    expect(normalizeWorkspaceColor('  ')).toBe('pink')
  })

  it('accepts known preset ids case-insensitively', () => {
    for (const preset of WORKSPACE_COLOR_PRESETS) {
      expect(normalizeWorkspaceColor(preset.id)).toBe(preset.id)
      expect(normalizeWorkspaceColor(preset.id.toUpperCase())).toBe(preset.id)
    }
  })

  it('falls back to pink for unknown ids', () => {
    expect(normalizeWorkspaceColor('neon')).toBe('pink')
    expect(normalizeWorkspaceColor('red')).toBe('pink')
  })
})

describe('colorForWorkspace', () => {
  it('reads color from a workspace record', () => {
    expect(colorForWorkspace({ name: 'work', color: 'cyan' })).toBe('cyan')
    expect(colorForWorkspace({ name: 'personal' })).toBe('pink')
    expect(colorForWorkspace(null)).toBe('pink')
  })
})
