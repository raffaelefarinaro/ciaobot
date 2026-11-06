// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import OnboardingAccessStage from '../onboarding/OnboardingAccessStage.vue'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), patch: vi.fn() } }))

const context = { target: { kind: 'installation' as const }, workspace: 'personal', revision: 1, provisioned: true }

function responses({ scope = 'workspace' as string | null, platform = 'darwin', overrides = {}, engineMode = 'auto', provider = 'claude' } = {}) {
  vi.mocked(api.get).mockImplementation((url: string) => {
    if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal', agent_fs_scope: scope, default_provider: provider, provider_overrides: overrides }] })
    if (url === '/api/host/terminal') return Promise.resolve({ available: true, platform })
    if (url === '/api/settings/routines') return Promise.resolve({ provider_default_modes: { claude: engineMode } })
    return Promise.resolve({})
  })
}

async function mounted(options = {}) {
  responses(options)
  const wrapper = mount(OnboardingAccessStage, { props: { context, busy: false }, attachTo: document.body })
  await vi.waitFor(() => expect(wrapper.text()).toContain('Filesystem scope'))
  await vi.waitFor(() => expect((wrapper.emitted('state')?.at(-1)?.[0] as { pending?: boolean })?.pending).toBe(false))
  return wrapper
}

beforeEach(() => { vi.resetAllMocks() })

describe('OnboardingAccessStage', () => {
  it('keeps Continue disabled until a scope is chosen, then saves it on the tap', async () => {
    const wrapper = await mounted({ scope: null })
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    expect(api.patch).not.toHaveBeenCalled()
    vi.mocked(api.patch).mockResolvedValue({})
    await wrapper.find('input[value="workspace"]').setValue()
    expect(api.patch).toHaveBeenCalledWith('/api/workspaces/personal', { agent_fs_scope: 'workspace' })
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, pending: false }))
  })

  it('has no save step, refresh or acknowledgement control', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
  })

  it('states the consequence of Whole machine inline and can undo it', async () => {
    const wrapper = await mounted()
    vi.mocked(api.patch).mockResolvedValue({})
    expect(wrapper.find('.consequence').exists()).toBe(false)
    await wrapper.find('input[value="machine"]').setValue()
    await vi.waitFor(() => expect(wrapper.find('.consequence').exists()).toBe(true))
    expect(wrapper.get('.consequence').text()).toContain('any file this user can read')
    await vi.waitFor(() => expect(wrapper.get('button.undo').attributes('disabled')).toBeUndefined())
    await wrapper.get('button.undo').trigger('click')
    expect(api.patch).toHaveBeenLastCalledWith('/api/workspaces/personal', { agent_fs_scope: 'workspace' })
  })

  it('states that scope is not a safety guarantee', async () => {
    const wrapper = await mounted()
    expect(wrapper.text()).toContain('not a safety guarantee')
    expect(wrapper.text()).toContain('classifier decides')
  })

  it('describes OpenCode bypass as unenforced instructions', async () => {
    const wrapper = await mounted({ provider: 'opencode' })
    expect(wrapper.text()).toContain('not enforced safeguards')
    expect(wrapper.text()).toContain('configurable rules')
  })

  it('treats an unsupported host platform as unknown', async () => {
    const wrapper = await mounted({ platform: 'other' })
    expect(wrapper.text()).toContain('platform was not reported')
  })

  it('loads the saved scope without saving anything', async () => {
    const wrapper = await mounted()
    expect(api.patch).not.toHaveBeenCalled()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, dirty: false })
  })

  it('shows an explicit stored approval mode and the engine default separately', async () => {
    const wrapper = await mounted({ overrides: { claude: { permission_mode: 'manual', model: 'opus' } } })
    expect((wrapper.get('input[name="approval"][value="manual"]').element as HTMLInputElement).checked).toBe(true)
    expect(wrapper.text()).toContain('Engine default (auto)')
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('disables workspace-only scope on Windows and explains why', async () => {
    const wrapper = await mounted({ platform: 'win32' })
    expect((wrapper.find('input[value="workspace"]').element as HTMLInputElement).disabled).toBe(true)
    expect(wrapper.text()).toContain('Windows has no filesystem sandbox')
  })

  it('says the platform is unknown instead of implying a sandbox', async () => {
    const wrapper = await mounted({ platform: '' })
    expect(wrapper.text()).toContain('platform was not reported')
  })

  it('saves an approval mode on the tap, keeping other provider overrides', async () => {
    const wrapper = await mounted({ overrides: { claude: { model: 'opus' } } })
    vi.mocked(api.patch).mockResolvedValue({})
    await wrapper.get('input[name="approval"][value="manual"]').setValue()
    expect(api.patch).toHaveBeenCalledWith('/api/workspaces/personal', { provider_overrides: { claude: { model: 'opus', permission_mode: 'manual' } } })
  })

  it('ignores a second tap during a save and keeps focus on the checked radio', async () => {
    const wrapper = await mounted()
    let resolvePatch: (value: unknown) => void = () => {}
    vi.mocked(api.patch).mockReturnValue(new Promise(resolve => { resolvePatch = resolve }) as never)
    const machine = wrapper.find('input[value="machine"]')
    ;(machine.element as HTMLInputElement).focus()
    await machine.setValue()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false, pending: true })
    // Not disabled, so focus is not dropped mid-save.
    expect((machine.element as HTMLInputElement).disabled).toBe(false)
    expect(document.activeElement).toBe(machine.element)
    await wrapper.get('input[name="approval"][value="manual"]').setValue()
    await wrapper.find('input[value="workspace"]').setValue()
    expect(api.patch).toHaveBeenCalledTimes(1)

    resolvePatch({})
    await vi.waitFor(() => expect((wrapper.emitted('state')?.at(-1)?.[0] as { pending?: boolean })?.pending).toBe(false))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
    expect((wrapper.find('input[value="machine"]').element as HTMLInputElement).checked).toBe(true)
  })

  it('blocks Continue after a failed save, puts the choice back, and recovers by tapping the saved value', async () => {
    const wrapper = await mounted()
    vi.mocked(api.patch).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({})
    await wrapper.find('input[value="machine"]').setValue()
    await vi.waitFor(() => expect(wrapper.text()).toContain('offline'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    expect((wrapper.find('input[value="workspace"]').element as HTMLInputElement).checked).toBe(true)

    await wrapper.find('input[value="workspace"]').trigger('click')
    expect(wrapper.text()).not.toContain('offline')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, error: null })
    expect(api.patch).toHaveBeenCalledTimes(1)

    await wrapper.find('input[value="machine"]').setValue()
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, pending: false }))
  })
})
