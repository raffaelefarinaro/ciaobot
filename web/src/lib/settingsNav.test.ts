import { describe, expect, it } from 'vitest'
import { SETTINGS_NAV, settingsTabLabel } from './settingsNav'

describe('settingsTabLabel', () => {
  it('names every settings tab by its sidebar label', () => {
    for (const item of SETTINGS_NAV) {
      expect(settingsTabLabel(item.to)).toBe(item.label)
    }
    expect(settingsTabLabel('/settings')).toBe('General')
    expect(settingsTabLabel('/settings/models')).toBe('Models & providers')
  })

  it('falls back to Settings for an unknown tab', () => {
    expect(settingsTabLabel('/settings/nope')).toBe('Settings')
  })
})
