// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import OnboardingLocationStage from '../onboarding/OnboardingLocationStage.vue'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), patch: vi.fn(), post: vi.fn() } }))

const context = { target: { kind: 'installation' as const }, workspace: '', revision: 1, provisioned: false }

async function chooseFolder(wrapper: ReturnType<typeof mount>, path: string) {
  await wrapper.find('input[name="folder"]').setValue(path)
  await wrapper.find('input[name="folder"]').trigger('change')
}

beforeEach(() => { vi.clearAllMocks() })

describe('OnboardingLocationStage', () => {
  it('puts the folder first and does not continue on a folder alone', async () => {
    vi.mocked(api.get).mockResolvedValue({ mode: 'empty', ok: true })
    const wrapper = mount(OnboardingLocationStage, { props: { context, busy: false } })
    expect(wrapper.findAll('label')[0].text()).toContain('Main Ciaobot folder')
    expect(api.get).not.toHaveBeenCalled()

    await chooseFolder(wrapper, '/home/ciaobot')
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith(expect.stringContaining('/api/setup/inspect-folder')))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })

    await wrapper.find('input[name="name"]').setValue('Personal')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })

    await wrapper.find('form').trigger('submit')
    expect(wrapper.emitted('continue')).toHaveLength(1)
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('rejects a name the engine would refuse, with an associated message', async () => {
    vi.mocked(api.get).mockResolvedValue({ mode: 'empty', ok: true })
    const wrapper = mount(OnboardingLocationStage, { props: { context, busy: false } })
    await chooseFolder(wrapper, '/home/ciaobot')
    await vi.waitFor(() => expect(api.get).toHaveBeenCalled())

    await wrapper.find('input[name="name"]').setValue('my workspace')
    await wrapper.find('input[name="name"]').trigger('blur')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    expect(wrapper.find('#name-error').text()).toContain('letters, numbers')

    await wrapper.find('input[name="name"]').setValue('my-workspace_2')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
  })

  it('blocks Continue when the folder cannot be inspected and reports it to the shell', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('nope'))
    const wrapper = mount(OnboardingLocationStage, { props: { context, busy: false } })
    await wrapper.find('input[name="name"]').setValue('Personal')
    await chooseFolder(wrapper, '/home/missing')
    await vi.waitFor(() => expect(wrapper.text()).toContain('Could not inspect this folder'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false, error: expect.stringContaining('Could not inspect') })
  })

  it('accepts an existing notes folder and says files stay where they are', async () => {
    vi.mocked(api.get).mockResolvedValue({ mode: 'existing', ok: true })
    const wrapper = mount(OnboardingLocationStage, { props: { context, busy: false } })
    await chooseFolder(wrapper, '/home/notes')
    await vi.waitFor(() => expect(wrapper.text()).toContain('Existing notes found'))
    await wrapper.find('input[name="name"]').setValue('Personal')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
    expect(wrapper.text()).toContain('will not reorganise or upload anything')
  })

  it('shows a provisioned path as read-only', () => {
    vi.mocked(api.get).mockResolvedValue({ workspace_root: '/srv/ciaobot', ready: true })
    const wrapper = mount(OnboardingLocationStage, { props: {
      context: { ...context, provisioned: true, workspace: '/srv/ciaobot' }, busy: false,
    } })
    expect((wrapper.find('input[name="folder"]').element as HTMLInputElement).readOnly).toBe(true)
    expect((wrapper.find('input[name="folder"]').element as HTMLInputElement).value).toBe('/srv/ciaobot')
  })
})
