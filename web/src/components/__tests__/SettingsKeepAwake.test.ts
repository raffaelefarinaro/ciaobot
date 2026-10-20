// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsKeepAwake from '../settings/SettingsKeepAwake.vue'
import type { RoutineSettings } from '../../lib/types'

function mountCard(
  routines: Partial<RoutineSettings> | null,
  saveRoutines = vi.fn(() => Promise.resolve()),
  routinesSaving = false,
) {
  const wrapper = mount(SettingsKeepAwake, {
    props: { routines: routines as RoutineSettings | null, routinesSaving, saveRoutines },
  })
  return { wrapper, saveRoutines }
}

describe('SettingsKeepAwake', () => {
  it('labels the Mac host and explains the remote-device and sleep limits', () => {
    const { wrapper } = mountCard({
      keep_awake_platform: 'macos',
      keep_awake_while_agents_working: false,
    })
    const toggle = wrapper.get('[role="switch"]')

    expect(toggle.attributes('aria-label')).toBe('Keep Mac awake while agents are working')
    expect(toggle.attributes('aria-checked')).toBe('false')
    expect(toggle.attributes('aria-describedby')).toBe('keep-awake-description')
    expect(wrapper.text()).toContain('not on the device viewing this page')
    expect(wrapper.text()).toContain('closing the lid')
  })

  it('uses the Windows host label and persists the switch value', async () => {
    const { wrapper, saveRoutines } = mountCard({
      keep_awake_platform: 'windows',
      keep_awake_while_agents_working: true,
    })
    const toggle = wrapper.get('[role="switch"]')

    expect(toggle.attributes('aria-label')).toBe('Keep Windows PC awake while agents are working')
    expect(toggle.attributes('aria-checked')).toBe('true')
    expect(toggle.text()).toContain('On')

    await toggle.trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ keep_awake_while_agents_working: false })
  })

  it('does not render on hosts without a supported power backend', () => {
    const { wrapper } = mountCard({ keep_awake_platform: '' })
    expect(wrapper.find('[role="switch"]').exists()).toBe(false)
  })

  it('disables the switch while settings are loading or saving', () => {
    const loading = mountCard(null)
    expect(loading.wrapper.find('[role="switch"]').exists()).toBe(false)

    const saving = mountCard({ keep_awake_platform: 'macos' }, undefined, true)
    expect(saving.wrapper.get('[role="switch"]').attributes('disabled')).toBeDefined()
  })
})
