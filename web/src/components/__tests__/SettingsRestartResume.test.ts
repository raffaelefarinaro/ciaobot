// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsRestartResume from '../settings/SettingsRestartResume.vue'
import type { RoutineSettings } from '../../lib/types'

function mountCard(routines: Partial<RoutineSettings> | null, saveRoutines = vi.fn(() => Promise.resolve())) {
  const wrapper = mount(SettingsRestartResume, {
    props: { routines: routines as RoutineSettings | null, routinesSaving: false, saveRoutines },
  })
  return { wrapper, saveRoutines }
}

describe('SettingsRestartResume', () => {
  it('is on by default', () => {
    const { wrapper } = mountCard({ model_options: { anthropic: [] } } as Partial<RoutineSettings>)
    expect(wrapper.get('[role="switch"]').attributes('aria-checked')).toBe('true')
  })

  it('turns resume off', async () => {
    const { wrapper, saveRoutines } = mountCard({ model_options: { anthropic: [] } } as Partial<RoutineSettings>)
    await wrapper.get('[role="switch"]').trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ resume_after_restart: false })
  })

  it('turns resume back on', async () => {
    const { wrapper, saveRoutines } = mountCard({ resume_after_restart: false })
    expect(wrapper.get('[role="switch"]').attributes('aria-checked')).toBe('false')
    await wrapper.get('[role="switch"]').trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ resume_after_restart: true })
  })

  it('is disabled until the settings have loaded', () => {
    const { wrapper } = mountCard(null)
    expect(wrapper.get('[role="switch"]').attributes('disabled')).toBeDefined()
  })
})
