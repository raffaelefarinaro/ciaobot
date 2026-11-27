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
  enabled: true, timezone_name: 'UTC', daily_time_utc: '06:00', scope: 'system',
}
const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone

beforeEach(() => { setActivePinia(createPinia()); vi.resetAllMocks() })

const save = (wrapper: ReturnType<typeof mount>) => (wrapper.vm as unknown as { save: () => Promise<boolean> }).save()
const loaded = (wrapper: ReturnType<typeof mount>) => vi.waitFor(() => expect(wrapper.text()).toContain('Workspace care is'))

describe('OnboardingCareStage', () => {
  it('proposes 07:00 in the browser zone and saves it on Continue, with no Save button', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    vi.mocked(api.patch).mockResolvedValue({ ...routine, daily_time_utc: '07:00', timezone_name: browserZone })
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await loaded(wrapper)
    expect(wrapper.find('button').exists()).toBe(false)
    expect((wrapper.get('input[type="time"]').element as HTMLInputElement).value).toBe('07:00')
    expect(wrapper.text()).toContain(`Care will run at 07:00 ${browserZone}. Saved when you continue.`)
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, dirty: true })
    expect(await save(wrapper)).toBe(true)
    expect(api.patch).toHaveBeenCalledWith('/api/schedules/system-memory-curation@personal', { time: '07:00', timezone: browserZone })
    expect(wrapper.text()).toContain(`Care runs at 07:00 ${browserZone}.`)
  })

  it('keeps a time someone already set in another zone and writes nothing on Continue', async () => {
    vi.mocked(api.get).mockResolvedValue([{ ...routine, daily_time_utc: '05:30', timezone_name: 'America/New_York' }])
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await loaded(wrapper)
    expect((wrapper.get('input[type="time"]').element as HTMLInputElement).value).toBe('05:30')
    expect(wrapper.text()).toContain('Care runs at 05:30 America/New_York.')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, dirty: false })
    expect(await save(wrapper)).toBe(true)
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('commits an edited time and timezone, on the existing care row, when Continue saves', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    vi.mocked(api.patch).mockResolvedValue({ ...routine, daily_time_utc: '09:15', timezone_name: 'Europe/Paris' })
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await loaded(wrapper)
    await wrapper.get('input[type="time"]').setValue('09:15')
    await wrapper.get('input[name="timezone"]').setValue('Europe/Paris')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ dirty: true, canContinue: true })
    expect(wrapper.text()).toContain('Care will run at 09:15 Europe/Paris. Saved when you continue.')
    expect(api.patch).not.toHaveBeenCalled()
    expect(await save(wrapper)).toBe(true)
    expect(api.patch).toHaveBeenCalledWith('/api/schedules/system-memory-curation@personal', { time: '09:15', timezone: 'Europe/Paris' })
    expect(api.patch).toHaveBeenCalledOnce()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ dirty: false })
    // Saving again with nothing new writes nothing more.
    expect(await save(wrapper)).toBe(true)
    expect(api.patch).toHaveBeenCalledOnce()
  })

  it('does not dead-end when the installation has no care routine', async () => {
    vi.mocked(api.get).mockResolvedValue([])
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('No care routine is installed'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
    expect(await save(wrapper)).toBe(true)
    expect(api.patch).not.toHaveBeenCalled()
  })
})

describe('OnboardingCareStage safety', () => {
  it('shows a failed save inline, keeps Continue available, and clears the error on the next edit', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    vi.mocked(api.patch).mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await loaded(wrapper)
    await wrapper.get('input[type="time"]').setValue('09:15')
    expect(await save(wrapper)).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toContain('offline')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, error: 'offline' })
    await wrapper.get('input[type="time"]').setValue('09:30')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('fills the browser timezone as the editable proposal', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await loaded(wrapper)
    const zone = (wrapper.get('input[name="timezone"]').element as HTMLInputElement).value
    expect(zone).toBe(browserZone)
  })

  it('uses the availability answer to propose a slot until the time is edited', async () => {
    vi.mocked(api.get).mockResolvedValue([routine])
    const wrapper = mount(OnboardingCareStage, { props: { context, busy: false } })
    await loaded(wrapper)
    await wrapper.findAll('input[name="availability"]')[1].setValue()
    expect((wrapper.get('input[type="time"]').element as HTMLInputElement).value).toBe('13:00')
    await wrapper.get('input[type="time"]').setValue('15:00')
    await wrapper.findAll('input[name="availability"]')[2].setValue()
    expect((wrapper.get('input[type="time"]').element as HTMLInputElement).value).toBe('15:00')
  })
})
