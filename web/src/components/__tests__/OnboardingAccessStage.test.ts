// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import OnboardingAccessStage from '../onboarding/OnboardingAccessStage.vue'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), patch: vi.fn() } }))

const context = { target: { kind: 'installation' as const }, workspace: 'personal', revision: 1, provisioned: true }

function responses({ scope = 'workspace', platform = 'darwin', overrides = {}, engineMode = 'auto' } = {}) {
  vi.mocked(api.get).mockImplementation((url: string) => {
    if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal', agent_fs_scope: scope, default_provider: 'claude', provider_overrides: overrides }] })
    if (url === '/api/host/terminal') return Promise.resolve({ available: true, platform })
    if (url === '/api/settings/routines') return Promise.resolve({ provider_default_modes: { claude: engineMode } })
    return Promise.resolve({})
  })
}

async function mounted(options = {}) {
  responses(options)
  const wrapper = mount(OnboardingAccessStage, { props: { context, busy: false } })
  await vi.waitFor(() => expect(wrapper.text()).toContain('Filesystem scope'))
  await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]?.pending).toBe(false))
  return wrapper
}

beforeEach(() => { vi.resetAllMocks() })

describe('OnboardingAccessStage', () => {
  it('loads the saved scope without saving anything', async () => {
    const wrapper = await mounted()
    expect(api.patch).not.toHaveBeenCalled()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, dirty: false })
  })

  it('shows an explicit stored approval mode and the engine default separately', async () => {
    const wrapper = await mounted({ overrides: { claude: { permission_mode: 'manual', model: 'opus' } } })
    const select = wrapper.get('select').element as HTMLSelectElement
    expect(select.value).toBe('manual')
    expect(wrapper.text()).toContain('Engine default (auto)')
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

  it('disables every control while a save is in flight', async () => {
    const wrapper = await mounted()
    let resolvePatch: (value: unknown) => void = () => {}
    vi.mocked(api.patch).mockReturnValue(new Promise(resolve => { resolvePatch = resolve }) as never)
    await wrapper.findAll('input[type="radio"]')[1].setValue()
    await wrapper.get('button').trigger('click')

    expect((wrapper.get('select').element as HTMLSelectElement).disabled).toBe(true)
    expect((wrapper.find('input[value="machine"]').element as HTMLInputElement).disabled).toBe(true)

    resolvePatch({})
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]?.pending).toBe(false))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, dirty: false })
  })

  it('blocks Continue after a failed save', async () => {
    const wrapper = await mounted()
    vi.mocked(api.patch).mockRejectedValue(new Error('offline'))
    await wrapper.findAll('input[type="radio"]')[1].setValue()
    await wrapper.get('button').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('offline'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })
})
