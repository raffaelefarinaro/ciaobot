// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import OnboardingShell from '../onboarding/OnboardingShell.vue'

const context = { target: { kind: 'installation' as const }, workspace: 'studio', revision: 2, provisioned: true }

describe('OnboardingShell', () => {
  it('renders stage, summary and navigation without domain side effects', async () => {
    const wrapper = mount(OnboardingShell, { props: { context, step: 'location', busy: false, error: null, conflict: false, canContinue: true }, slots: { stage: '<button>Stage action</button>', summary: '<p>Ready</p>' } })
    expect(wrapper.text()).toContain('Stage action')
    expect(wrapper.text()).toContain('Ready')
    await wrapper.get('button.continue').trigger('click')
    await wrapper.get('button.back').trigger('click')
    await wrapper.get('button.leave').trigger('click')
    expect(wrapper.emitted('continue')).toHaveLength(1)
    expect(wrapper.emitted('back')).toHaveLength(1)
    expect(wrapper.emitted('leave')).toHaveLength(1)
    expect(wrapper.emitted('complete')).toBeUndefined()
  })

  it('announces errors and disables navigation while pending', () => {
    const wrapper = mount(OnboardingShell, { props: { context, step: 'access_memory', busy: true, error: 'Save failed', conflict: true, canContinue: false } })
    expect(wrapper.get('[role="alert"]').text()).toContain('Save failed')
    expect(wrapper.text()).toContain('changed elsewhere')
    expect(wrapper.get('button.continue').attributes('disabled')).toBeDefined()
    expect(wrapper.get('button.back').attributes('disabled')).toBeDefined()
  })
})
