import { describe, expect, it } from 'vitest'
import { SETTINGS_NAV, SETTINGS_NAV_GROUPS, settingsTabLabel } from './settingsNav'

describe('settingsTabLabel', () => {
  it('names every settings tab by its sidebar label', () => {
    for (const item of SETTINGS_NAV) {
      expect(settingsTabLabel(item.to)).toBe(item.label)
    }
    expect(settingsTabLabel('/settings')).toBe('General')
    expect(settingsTabLabel('/settings/providers')).toBe('Providers')
  })

  it('falls back to Settings for an unknown tab', () => {
    expect(settingsTabLabel('/settings/nope')).toBe('Settings')
  })
})

describe('SETTINGS_NAV_GROUPS', () => {
  it('groups the rail into Ciaobot and the workspace', () => {
    expect(SETTINGS_NAV_GROUPS.map(g => g.id)).toEqual(['engine', 'workspace'])
    const [engine, workspace] = SETTINGS_NAV_GROUPS
    expect(engine.label).toBe('Ciaobot')
    expect(engine.items.map(i => i.label)).toEqual(['General', 'Providers', 'Workspaces'])
    expect(workspace.label).toBeNull()
    expect(workspace.items.map(i => i.label)).toEqual(['Skills', 'Subagents', 'Commands', 'MCP servers'])
    expect(SETTINGS_NAV).toEqual([...engine.items, ...workspace.items])
    const targets = SETTINGS_NAV.map(i => i.to)
    expect(new Set(targets).size).toBe(targets.length)
  })
})
