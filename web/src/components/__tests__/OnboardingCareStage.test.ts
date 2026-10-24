// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OnboardingCareStage from '../onboarding/OnboardingCareStage.vue'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), patch: vi.fn() } }))

const context = { target: { kind: 'installation' as const }, workspace: 'personal', revision: 1, provisioned: true }
const routine = {
  schedule_id: 'system-memory-curation@personal', title: 'Workspace care', prompt: 'unchanged prompt', frequency: 'weekly',
  enabled: true, timezone_name: 'Europe/Zurich', daily_time_utc: '08:00', scope: 'system',
}

beforeEach(() => { setActivePinia(createPinia()); vi.resetAllMocks() })

describe('OnboardingCareStage', () => {
  it('offers the packaged default without writing and enables Continue after explicit choice', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('Workspace care'))
    expect(wrapper.find('button[aria-label="Keep packaged default"]').exists()).toBe(true)
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    await wrapper.get('button[aria-label="Keep packaged default"]').trigger('click')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('updates only time and timezone on the existing workspace care row', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    vi.mocked(api.patch).mockResolvedValue({ ...routine, daily_time_utc: '09:15', timezone_name: 'Europe/Paris' })
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('Workspace care'))
    await wrapper.get('input[type="time"]').setValue('09:15')
    await wrapper.get('input[name="timezone"]').setValue('Europe/Paris')
    await wrapper.get('button[aria-label="Save care timing"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, dirty: false }))
    expect(api.patch).toHaveBeenCalledWith('/api/schedules/system-memory-curation@personal', { time: '09:15', timezone: 'Europe/Paris' })
    expect(api.patch).toHaveBeenCalledOnce()
  })

  it('does not offer Continue when the installation has no care routine', async () => {
    vi.mocked(api.get).mockResolvedValue([])
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('No care routine is installed'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    expect(api.patch).not.toHaveBeenCalled()
  })
})

describe('OnboardingCareStage safety', () => {
  it('falls back to a disabled Continue after a failed save', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    vi.mocked(api.patch).mockRejectedValue(new Error('offline'))
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('Workspace care'))
    await wrapper.get('input[type="time"]').setValue('09:15')
    await wrapper.get('button[aria-label="Save care timing"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('offline'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })

  it('does not enable Continue when the fields are edited after keeping the default', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('Workspace care'))
    await wrapper.get('button[aria-label="Keep packaged default"]').trigger('click')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
    await wrapper.get('input[type="time"]').setValue('10:00')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('keeps the browser timezone as the editable suggestion', async () => {
    vi.mocked(api.get).mockResolvedValue([{ ...routine, timezone_name: 'Europe/Zurich' }])
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('Workspace care'))
    const zone = (wrapper.get('input[name="timezone"]').element as HTMLInputElement).value
    expect(zone).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone)
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })

  it('uses the availability answer to propose a slot until the time is edited', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('Workspace care'))
    await wrapper.findAll('input[name="availability"]')[1].setValue()
    expect((wrapper.get('input[type="time"]').element as HTMLInputElement).value).toBe('13:00')
    await wrapper.get('input[type="time"]').setValue('15:00')
    await wrapper.findAll('input[name="availability"]')[2].setValue()
    expect((wrapper.get('input[type="time"]').element as HTMLInputElement).value).toBe('15:00')
  })
})
