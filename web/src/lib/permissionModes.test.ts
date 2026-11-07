import { describe, expect, it } from 'vitest'
import { isBridgeMode, modeLabel, PERMISSION_MODE_COPY } from './permissionModes'

describe('permissionModes', () => {
  it('labels each mode with the words Settings → Providers uses', () => {
    expect(modeLabel('normal')).toBe('Manual — ask for every action')
    expect(modeLabel('auto')).toBe('Auto — allow safe work, ask on risky')
    expect(modeLabel('bypass')).toBe('Bypass — allow everything')
    expect(modeLabel('plan')).toBe('Plan — read and propose only, change nothing')
  })

  it('accepts only the engine mode names', () => {
    for (const mode of ['plan', 'normal', 'auto', 'bypass']) expect(isBridgeMode(mode)).toBe(true)
    expect(isBridgeMode('manual')).toBe(false)
    expect(isBridgeMode('constructor')).toBe(false)
    expect(isBridgeMode(undefined)).toBe(false)
    expect(isBridgeMode(1)).toBe(false)
  })

  it('names every mode', () => {
    expect(Object.keys(PERMISSION_MODE_COPY).sort()).toEqual(['auto', 'bypass', 'normal', 'plan'])
  })
})
