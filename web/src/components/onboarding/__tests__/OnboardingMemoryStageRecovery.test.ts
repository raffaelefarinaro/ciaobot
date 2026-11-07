// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OnboardingMemoryStage from '../OnboardingMemoryStage.vue'
import { useProjectStore } from '../../../stores/projects'
import { api } from '../../../lib/api'

vi.mock('../../../lib/api', () => ({ api: { get: vi.fn(), patch: vi.fn(), post: vi.fn() } }))

const props = { context: { target: { kind: 'installation' as const }, workspace: 'personal', revision: 1, provisioned: true }, busy: false }
const checked = (wrapper: ReturnType<typeof mount>) =>
  wrapper.findAll('input[type="radio"]').find(radio => (radio.element as HTMLInputElement).checked)?.attributes('value')
const lastState = (wrapper: ReturnType<typeof mount>) => wrapper.emitted('state')?.at(-1)?.[0]

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve(
    url.startsWith('/api/proposals/handover')
      ? { handover: 'off', acting_available: true }
      : { schema: 1, revision: 1, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} },
  ) as never)
})

describe('OnboardingMemoryStage failed-save recovery (real preferences)', () => {
  it('rolls back, shows the error, clears it on the saved value and retries on a new tap', async () => {
    const update = vi.spyOn(useProjectStore(), 'updateWorkspace')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(undefined as never)
    const wrapper = mount(OnboardingMemoryStage, { props, attachTo: document.body })
    await flushPromises()
    // The suggested default is shown but not written.
    expect(checked(wrapper)).toBe('all')
    expect(update).not.toHaveBeenCalled()

    await wrapper.get('input[value="dismiss"]').setValue()
    await flushPromises()
    expect(update).toHaveBeenCalledTimes(1)
    expect(checked(wrapper)).toBe('off')
    expect(wrapper.get('[role="alert"]').text()).toContain('offline')
    expect(lastState(wrapper)).toMatchObject({ canContinue: false })

    // Tapping the saved value clears the stale error and Continue recovers.
    await wrapper.get('input[value="off"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(lastState(wrapper)).toMatchObject({ canContinue: true, error: null })
    expect(update).toHaveBeenCalledTimes(1)

    await wrapper.get('input[value="dismiss"]').setValue()
    await flushPromises()
    expect(update).toHaveBeenCalledTimes(2)
    expect(update).toHaveBeenLastCalledWith('personal', { memory_handover: 'dismiss' })
    expect(checked(wrapper)).toBe('dismiss')
    wrapper.unmount()
  })

  it('rolls back after a failed Continue-time save so the suggested option can be tapped again', async () => {
    const update = vi.spyOn(useProjectStore(), 'updateWorkspace')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(undefined as never)
    const wrapper = mount(OnboardingMemoryStage, { props, attachTo: document.body })
    await flushPromises()
    const stage = wrapper.vm as unknown as { save: () => Promise<boolean> }
    expect(await stage.save()).toBe(false)
    expect(checked(wrapper)).toBe('off')
    await wrapper.get('input[value="all"]').setValue()
    await flushPromises()
    expect(update).toHaveBeenCalledTimes(2)
    expect(checked(wrapper)).toBe('all')
    expect(lastState(wrapper)).toMatchObject({ canContinue: true })
    wrapper.unmount()
  })
})
