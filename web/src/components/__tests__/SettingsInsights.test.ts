// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsInsights from '../settings/SettingsInsights.vue'
import type { RoutineSettings } from '../../lib/types'

function mountCard(routines: RoutineSettings | null, saveRoutines = vi.fn(() => Promise.resolve())) {
  const wrapper = mount(SettingsInsights, {
    props: { routines, routinesSaving: false, saveRoutines },
  })
  return { wrapper, saveRoutines, toggle: wrapper.get('[role="switch"]') }
}

describe('SettingsInsights', () => {
  it('reads On and turns insights off', async () => {
    const { toggle, saveRoutines } = mountCard({ insights_enabled: true } as RoutineSettings)
    expect(toggle.attributes('aria-checked')).toBe('true')
    expect(toggle.text()).toContain('On')

    await toggle.trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ insights_enabled: false })
  })

  it('reads Off and turns insights back on', async () => {
    const { toggle, saveRoutines } = mountCard({ insights_enabled: false } as RoutineSettings)
    expect(toggle.attributes('aria-checked')).toBe('false')
    expect(toggle.text()).toContain('Off')

    await toggle.trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ insights_enabled: true })
  })

  it('is disabled until the settings have loaded', () => {
    const { toggle } = mountCard(null)
    expect(toggle.attributes('disabled')).toBeDefined()
  })
})
