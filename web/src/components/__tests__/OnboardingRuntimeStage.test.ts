// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import OnboardingRuntimeStage from '../onboarding/OnboardingRuntimeStage.vue'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), patch: vi.fn(), post: vi.fn() } }))

const context = {
  target: { kind: 'installation' as const },
  workspace: 'personal',
  revision: 1,
  provisioned: true,
}
const ready = { connections: { claude: { ok: true, auth: 'signed in as owner', label: 'Claude Code', terminal_action: 'claude-login' } } }
const missing = { connections: { claude: { ok: false, auth: 'not installed', label: 'Claude Code', command: 'brew install claude', terminal_action: 'claude-install' } } }
const observed = {
  provider_models: { claude: ['configured-alias'] },
  provider_catalog_evidence: {
    claude: { status: 'observed', source: 'claude_initialize', models: [{ id: 'claude-x', label: 'Claude X' }], reason: null },
  },
}
const unknown = {
  provider_models: { claude: ['alias'] },
  provider_catalog_evidence: {
    claude: { status: 'unknown', source: null, models: [], reason: 'not_observed' },
  },
}

async function mountWithStatus(payload: unknown) {
  vi.mocked(api.get).mockResolvedValueOnce(payload)
  const wrapper = mount(OnboardingRuntimeStage, { props: { context, busy: false } })
  await wrapper.get('.models-empty button').trigger('click')
  await vi.waitFor(() => expect(api.get).toHaveBeenCalled())
  expect(String(vi.mocked(api.get).mock.calls[0]?.[0])).toBe('/api/settings/providers')
  return wrapper
}

beforeEach(() => { vi.resetAllMocks() })

describe('OnboardingRuntimeStage', () => {
  it('saves the selected runtime and model without replacing other overrides', async () => {
    const wrapper = await mountWithStatus(ready)
    vi.mocked(api.get).mockResolvedValueOnce(observed)
    await wrapper.get('.models-empty button').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('input[name="model"]').exists()).toBe(true))
    await wrapper.findAll('input[name="model"]')[0].setValue()
    vi.mocked(api.get).mockResolvedValueOnce({ workspaces: [{ name: 'personal', provider_overrides: {
      claude: { permission_mode: 'manual', thinking: 'high' }, opencode: { model: 'other-model' },
    } }] })
    vi.mocked(api.patch).mockResolvedValue({})
    const stage = wrapper.vm as unknown as { save: () => Promise<boolean> }
    expect(await stage.save()).toBe(true)
    expect(api.patch).toHaveBeenCalledWith('/api/workspaces/personal', {
      default_provider: 'claude', provider_overrides: {
        claude: { model: 'claude-x', permission_mode: 'manual', thinking: 'high' },
        opencode: { model: 'other-model' },
      },
    })
  })

  it('does not write settings when the choice is deliberately deferred', async () => {
    const wrapper = await mountWithStatus(missing)
    await wrapper.get('button.defer').trigger('click')
    const stage = wrapper.vm as unknown as { save: () => Promise<boolean> }
    expect(await stage.save()).toBe(true)
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('keeps the selection and reports a failed save for retry', async () => {
    const wrapper = await mountWithStatus(ready)
    vi.mocked(api.get).mockResolvedValueOnce(observed)
    await wrapper.get('.models-empty button').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('input[name="model"]').exists()).toBe(true))
    await wrapper.findAll('input[name="model"]')[0].setValue()
    vi.mocked(api.get).mockResolvedValue({ workspaces: [{ name: 'personal' }] })
    vi.mocked(api.patch).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({})
    const stage = wrapper.vm as unknown as { save: () => Promise<boolean> }
    expect(await stage.save()).toBe(false)
    expect(wrapper.text()).toContain('offline')
    expect((wrapper.get('input[name="model"]').element as HTMLInputElement).checked).toBe(true)
    await wrapper.get('button.retry').trigger('click')
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true }))
  })
  it('makes no request and opens no terminal on mount', async () => {
    vi.mocked(api.get).mockResolvedValue(ready)
    const wrapper = mount(OnboardingRuntimeStage, { props: { context, busy: false } })
    await wrapper.vm.$nextTick()
    expect(api.get).not.toHaveBeenCalled()
    expect(wrapper.findComponent({ name: 'HostTerminalButton' }).exists()).toBe(false)
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })

  it('requires an explicit model choice or a deliberate deferral', async () => {
    const wrapper = await mountWithStatus(ready)
    vi.mocked(api.get).mockResolvedValueOnce(observed)
    await wrapper.get('.models-empty button').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('Models reported by this runtime'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })

    await wrapper.findAll('input[name="model"]')[0].setValue()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
  })

  it('labels configured aliases as unverified and keeps reported models separate', async () => {
    const wrapper = await mountWithStatus(ready)
    vi.mocked(api.get).mockResolvedValueOnce(unknown)
    await wrapper.get('.models-empty button').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('Configured choices; availability not checked'))
    expect(wrapper.text()).toContain('alias')
    expect(wrapper.text()).not.toContain('Models reported by this runtime')
  })

  it('blocks Continue after a failed status read and offers a retry', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('offline'))
    const wrapper = mount(OnboardingRuntimeStage, { props: { context, busy: false } })
    await wrapper.get('.models-empty button').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('Could not read the provider status'))
    expect(wrapper.find('button.retry').exists()).toBe(true)
    expect(wrapper.find('button.defer').exists()).toBe(false)
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })

  it('offers install or sign-in with the provider command when the runtime is missing', async () => {
    const wrapper = await mountWithStatus(missing)
    await vi.waitFor(() => expect(wrapper.text()).toContain('not ready on this computer'))
    expect(wrapper.text()).toContain('brew install claude')
    const terminal = wrapper.findComponent({ name: 'HostTerminalButton' })
    expect(terminal.exists()).toBe(true)
    expect(terminal.props('action')).toBe('claude-install')
    expect(wrapper.find('button.defer').exists()).toBe(true)
  })
})

describe('OnboardingRuntimeStage missing status', () => {
  it('announces a runtime with no reported status and offers a re-check', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ connections: {} })
    const wrapper = mount(OnboardingRuntimeStage, { props: { context, busy: false } })
    await wrapper.get('.models-empty button').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('No status was reported for this runtime'))
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(wrapper.find('button.defer').exists()).toBe(false)
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    vi.mocked(api.get).mockResolvedValueOnce(ready)
    await wrapper.get('.models-empty button').trigger('click')
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false }))
    expect(vi.mocked(api.get).mock.calls.length).toBeGreaterThan(1)
  })
})
