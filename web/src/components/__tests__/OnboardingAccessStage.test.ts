// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import OnboardingAccessStage from '../onboarding/OnboardingAccessStage.vue'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), patch: vi.fn() } }))

const context = { target: { kind: 'installation' as const }, workspace: 'personal', revision: 1, provisioned: true }

function responses({ overrides = {}, engineMode = 'auto', provider = 'claude' } = {}) {
  vi.mocked(api.get).mockImplementation((url: string) => {
    if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal', default_provider: provider, provider_overrides: overrides }] })
    if (url === '/api/settings/routines') return Promise.resolve({ provider_default_modes: { claude: engineMode } })
    return Promise.resolve({})
  })
}

async function mounted(options = {}) {
  responses(options)
  const wrapper = mount(OnboardingAccessStage, { props: { context, busy: false }, attachTo: document.body })
  await vi.waitFor(() => expect(wrapper.text()).toContain('Approval mode'))
  await vi.waitFor(() => expect((wrapper.emitted('state')?.at(-1)?.[0] as { pending?: boolean })?.pending).toBe(false))
  return wrapper
}

beforeEach(() => { vi.resetAllMocks() })

describe('OnboardingAccessStage', () => {
  it('has no filesystem scope choice', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('input[name="scope"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Filesystem scope')
    expect(vi.mocked(api.get).mock.calls.map(call => call[0])).not.toContain('/api/host/terminal')
  })

  it('allows Continue with the engine default approval', async () => {
    const wrapper = await mounted()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, dirty: false })
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('has no save step, refresh or acknowledgement control', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
  })

  it('shows the bypass note with the provider docs link', async () => {
    const hrefs = { claude: 'https://code.claude.com/docs/en/permissions', opencode: 'https://opencode.ai/docs/permissions/' }
    for (const provider of ['claude', 'opencode'] as const) {
      const wrapper = await mounted({ provider })
      const note = wrapper.get('[data-testid="bypass-note"]')
      expect(note.text()).toContain('full user access')
      const link = note.get('a')
      expect(link.attributes('href')).toBe(hrefs[provider])
      expect(link.attributes('target')).toBe('_blank')
      expect(link.attributes('rel')).toBe('noopener')
      wrapper.unmount()
    }
  })

  it('describes the Claude classifier as not a safety guarantee', async () => {
    const wrapper = await mounted()
    expect(wrapper.text()).toContain('not a safety guarantee')
    expect(wrapper.text()).toContain('classifier decides')
  })

  it('describes OpenCode bypass as unenforced instructions', async () => {
    const wrapper = await mounted({ provider: 'opencode' })
    expect(wrapper.text()).toContain('not enforced safeguards')
    expect(wrapper.text()).toContain('configurable rules')
  })

  it('shows an explicit stored approval mode and the engine default separately', async () => {
    const wrapper = await mounted({ overrides: { claude: { permission_mode: 'manual', model: 'opus' } } })
    expect((wrapper.get('input[name="approval"][value="manual"]').element as HTMLInputElement).checked).toBe(true)
    expect(wrapper.text()).toContain('Engine default (auto)')
    expect(api.patch).not.toHaveBeenCalled()
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
    const manual = wrapper.get('input[name="approval"][value="manual"]')
    ;(manual.element as HTMLInputElement).focus()
    await manual.setValue()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false, pending: true })
    // Not disabled, so focus is not dropped mid-save.
    expect((manual.element as HTMLInputElement).disabled).toBe(false)
    expect(document.activeElement).toBe(manual.element)
    await wrapper.get('input[name="approval"][value="bypass"]').setValue()
    expect(api.patch).toHaveBeenCalledTimes(1)

    resolvePatch({})
    await vi.waitFor(() => expect((wrapper.emitted('state')?.at(-1)?.[0] as { pending?: boolean })?.pending).toBe(false))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
    expect((wrapper.get('input[name="approval"][value="manual"]').element as HTMLInputElement).checked).toBe(true)
  })

  it('blocks Continue after a failed save, puts the choice back, and recovers by tapping the saved value', async () => {
    const wrapper = await mounted()
    vi.mocked(api.patch).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({})
    await wrapper.get('input[name="approval"][value="manual"]').setValue()
    await vi.waitFor(() => expect(wrapper.text()).toContain('offline'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    expect((wrapper.get('input[name="approval"][value=""]').element as HTMLInputElement).checked).toBe(true)

    await wrapper.get('input[name="approval"][value=""]').trigger('click')
    expect(wrapper.text()).not.toContain('offline')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, error: null })
    expect(api.patch).toHaveBeenCalledTimes(1)

    await wrapper.get('input[name="approval"][value="manual"]').setValue()
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, pending: false }))
  })
})
