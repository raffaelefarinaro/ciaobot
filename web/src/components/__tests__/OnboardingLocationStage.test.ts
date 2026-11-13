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

beforeEach(() => { vi.resetAllMocks() })

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

    // The stage has no button of its own; the shell footer owns Continue.
    expect(wrapper.find('button').exists()).toBe(false)
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
    expect(wrapper.find('#onboarding-name-error').text()).toContain('letters, numbers')

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

  it('allows_retry_with_the_nested_vault_folder_after_inspection_refusal', async () => {
    // Answered by path, not by call order: a folder change may inspect twice.
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url.includes(encodeURIComponent('/home/clone/work/memory-vault'))) return { mode: 'existing', ok: true }
      throw new Error('400: more than one existing vault')
    })
    const wrapper = mount(OnboardingLocationStage, { props: { context, busy: false } })
    await wrapper.find('input[name="name"]').setValue('Personal')

    await chooseFolder(wrapper, '/home/clone')
    await vi.waitFor(() => expect(wrapper.text()).toContain('Could not inspect this folder'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })

    // The refused path stays editable; the chosen nested vault is inspected next.
    const folderInput = wrapper.find('input[name="folder"]')
    expect((folderInput.element as HTMLInputElement).value).toBe('/home/clone')
    expect(folderInput.attributes('disabled')).toBeUndefined()
    await chooseFolder(wrapper, '/home/clone/work/memory-vault')

    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true }))
    expect(api.get).toHaveBeenLastCalledWith(
      expect.stringContaining(encodeURIComponent('/home/clone/work/memory-vault')),
    )
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

  it('shows the installer\'s folder and workspace as set, with a way to change them', async () => {
    vi.mocked(api.get).mockResolvedValue({ workspace_root: '/srv/ciaobot', ready: true })
    const wrapper = mount(OnboardingLocationStage, { props: {
      context: { ...context, provisioned: true, workspace: 'personal' }, busy: false,
    } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('/srv/ciaobot'))
    expect(wrapper.find('input[name="folder"]').exists()).toBe(false)
    expect(wrapper.find('input[name="name"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('personal')
    expect(wrapper.get('a.change').attributes('href')).toBe('/settings/advanced#workspace-move')
    // Nothing is asked, so the shell may continue straight away.
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
  })

  it('asks for the workspace name only after Change', async () => {
    vi.mocked(api.get).mockResolvedValue({ workspace_root: '/srv/ciaobot', ready: true })
    const wrapper = mount(OnboardingLocationStage, { props: {
      context: { ...context, provisioned: true, workspace: 'personal' }, busy: false,
    } })
    await wrapper.get('button.change-name').trigger('click')
    expect((wrapper.get('input[name="name"]').element as HTMLInputElement).value).toBe('personal')
    await wrapper.get('input[name="name"]').setValue('my workspace')
    await wrapper.get('input[name="name"]').trigger('blur')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })
})

describe('OnboardingLocationStage races', () => {
  it('uses the authoritative path when it differs from the context', async () => {
    vi.mocked(api.get).mockResolvedValue({ workspace_root: '/srv/ciaobot' })
    const wrapper = mount(OnboardingLocationStage, { props: {
      context: { ...context, provisioned: true, workspace: 'personal' }, busy: false,
    } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('/srv/ciaobot'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
  })

  it('ignores an inspection that resolves after the path changed', async () => {
    let resolveFirst: (value: unknown) => void = () => {}
    vi.mocked(api.get).mockReturnValueOnce(new Promise(resolve => { resolveFirst = resolve }) as never)
    const wrapper = mount(OnboardingLocationStage, { props: { context, busy: false } })
    await wrapper.find('input[name="name"]').setValue('Personal')
    await wrapper.find('input[name="folder"]').setValue('/valid')
    await wrapper.find('input[name="folder"]').trigger('change')
    await wrapper.find('input[name="folder"]').setValue('/uninspected')

    resolveFirst({ mode: 'empty', ok: true })
    await vi.waitFor(() => expect(wrapper.text()).not.toContain('Existing notes'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })
})
