// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import OnboardingLocationStage from '../onboarding/OnboardingLocationStage.vue'

const context = { target: { kind: 'installation' as const }, workspace: '', revision: 1, provisioned: false }

describe('OnboardingLocationStage', () => {
  it('requires a chosen folder and a valid workspace name, then emits continue without writing progress', async () => {
    const wrapper = mount(OnboardingLocationStage, { props: { context, busy: false } })

    expect(wrapper.findAll('label')[0].text()).toContain('Main Ciaobot folder')
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.find('input[name="folder"]').setValue('/home/ciaobot')
    await wrapper.find('input[name="name"]').setValue('Personal')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
    await wrapper.find('form').trigger('submit')
    expect(wrapper.emitted('continue')).toHaveLength(1)
  })

  it('shows a provisioned path as read-only and explains remote folder selection', () => {
    const wrapper = mount(OnboardingLocationStage, { props: {
      context: { ...context, provisioned: true, workspace: '/srv/ciaobot' }, busy: false,
    } })
    expect((wrapper.find('input[name="folder"]').element as HTMLInputElement).readOnly).toBe(true)
    expect((wrapper.find('input[name="folder"]').element as HTMLInputElement).value).toBe('/srv/ciaobot')
  })
})
