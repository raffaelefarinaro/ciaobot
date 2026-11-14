// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsVaultCheck from '../settings/SettingsVaultCheck.vue'
import type { RoutineSettings } from '../../lib/types'

function mountCard(routines: Partial<RoutineSettings> | null, saveRoutines = vi.fn(() => Promise.resolve())) {
  const wrapper = mount(SettingsVaultCheck, {
    props: { routines: routines as RoutineSettings | null, routinesSaving: false, saveRoutines },
  })
  return { wrapper, saveRoutines }
}

describe('SettingsVaultCheck', () => {
  it('keeps checked controlled by routines across a deferred save', async () => {
    let resolveSave!: () => void
    const save = vi.fn(() => new Promise<void>((resolve) => { resolveSave = resolve }))
    const { wrapper } = mountCard({ verify_vault_answers: true }, save)
    const button = wrapper.get('button.settings-switch')
    await button.trigger('click')
    expect(save).toHaveBeenCalledExactlyOnceWith({ verify_vault_answers: false })
    await wrapper.setProps({ routinesSaving: true })
    expect(button.attributes('disabled')).toBeDefined()
    expect(button.attributes('aria-checked')).toBe('true')
    resolveSave()
    await Promise.resolve()
    expect(button.attributes('aria-checked')).toBe('true')
    await wrapper.setProps({ routinesSaving: false, routines: { verify_vault_answers: false } as RoutineSettings })
    expect(button.attributes('aria-checked')).toBe('false')
    expect(button.text()).toBe('Off')
  })

  it('is on by default', () => {
    const { wrapper } = mountCard({ model_options: { anthropic: [] } } as Partial<RoutineSettings>)
    expect(wrapper.get('[role="switch"]').attributes('aria-checked')).toBe('true')
  })

  it('turns the vault check off', async () => {
    const { wrapper, saveRoutines } = mountCard({ model_options: { anthropic: [] } } as Partial<RoutineSettings>)
    await wrapper.get('[role="switch"]').trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ verify_vault_answers: false })
  })

  it('turns the vault check back on', async () => {
    const { wrapper, saveRoutines } = mountCard({ verify_vault_answers: false })
    expect(wrapper.get('[role="switch"]').attributes('aria-checked')).toBe('false')
    await wrapper.get('[role="switch"]').trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ verify_vault_answers: true })
  })

  it('is disabled until the settings have loaded', () => {
    const { wrapper } = mountCard(null)
    expect(wrapper.get('[role="switch"]').attributes('disabled')).toBeDefined()
  })
})
