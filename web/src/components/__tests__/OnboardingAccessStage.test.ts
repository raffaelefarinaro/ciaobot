// @vitest-environment jsdom
import { mount, flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import OnboardingAccessStage from '../onboarding/OnboardingAccessStage.vue'

const mocks = vi.hoisted(() => ({ patch: vi.fn(), get: vi.fn() }))
vi.mock('../../lib/api', () => ({ api: { patch: mocks.patch, get: mocks.get } }))

const props = { context: { target: { kind: 'workspace' as const, name: 'personal' }, workspace: 'personal', revision: 1, provisioned: true }, busy: false }

describe('OnboardingAccessStage', () => {
  it('does not infer a scope or save on mount, and requires an explicit scope before Continue', async () => {
    mocks.get.mockResolvedValue({ workspaces: [{ name: 'personal', agent_fs_scope: null, default_provider: 'claude' }] })
    const wrapper = mount(OnboardingAccessStage, { props })
    await flushPromises()
    expect(mocks.patch).not.toHaveBeenCalled()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false, dirty: false })
    expect(wrapper.text()).toContain('not a safety guarantee')
    await wrapper.get('input[type="radio"][value="workspace"]').setValue()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false, dirty: true })
  })

  it('blocks Continue and announces a failed explicit save', async () => {
    mocks.get.mockResolvedValue({ workspaces: [{ name: 'personal', agent_fs_scope: 'workspace', default_provider: 'claude' }] })
    mocks.patch.mockRejectedValue(new Error('offline'))
    const wrapper = mount(OnboardingAccessStage, { props })
    await flushPromises()
    await wrapper.get('input[type="radio"][value="machine"]').setValue()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('could not be saved')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false, error: expect.any(String) })
  })
})
