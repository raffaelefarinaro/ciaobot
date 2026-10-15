import { describe, expect, it } from 'vitest'
import { applyKeyboardSettings, useKeyboardSettings } from './useKeyboardSettings'

describe('useKeyboardSettings mid_turn_delivery', () => {
  it('defaults to queue and applies a steer preference', () => {
    const { settings } = useKeyboardSettings()
    applyKeyboardSettings({ mid_turn_delivery: 'queue' })
    expect(settings.value.mid_turn_delivery).toBe('queue')
    applyKeyboardSettings({ mid_turn_delivery: 'steer' })
    expect(settings.value.mid_turn_delivery).toBe('steer')
  })

  it('ignores a value that is neither queue nor steer', () => {
    const { settings } = useKeyboardSettings()
    applyKeyboardSettings({ mid_turn_delivery: 'steer' })
    applyKeyboardSettings({ mid_turn_delivery: 'bogus' as never })
    expect(settings.value.mid_turn_delivery).toBe('steer')
    applyKeyboardSettings({ mid_turn_delivery: 'queue' })
  })
})
