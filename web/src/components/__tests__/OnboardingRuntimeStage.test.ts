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

interface Replies { status?: unknown; models?: unknown; workspaces?: unknown }

// The stage reads on its own: current choice, provider status, then models.
function mountStage(replies: Replies) {
  vi.mocked(api.get).mockImplementation((url: string) => {
    if (url === '/api/workspaces') return Promise.resolve(replies.workspaces ?? { workspaces: [{ name: 'personal' }] }) as never
    if (url === '/api/settings/providers') return replies.status instanceof Error ? Promise.reject(replies.status) as never : Promise.resolve(replies.status ?? ready) as never
    if (url.startsWith('/api/models')) return Promise.resolve(replies.models ?? observed) as never
    return Promise.reject(new Error(`unexpected ${url}`)) as never
  })
  return mount(OnboardingRuntimeStage, { props: { context, busy: false } })
}
const stageOf = (wrapper: ReturnType<typeof mount>) => wrapper.vm as unknown as { save: () => Promise<boolean> }

beforeEach(() => { vi.resetAllMocks() })

describe('OnboardingRuntimeStage', () => {
  it('lists the selected runtime\'s models without being asked', async () => {
    const wrapper = mountStage({})
    await vi.waitFor(() => expect(wrapper.find('input[name="model"]').exists()).toBe(true))
    expect(wrapper.text()).toContain('Models reported by this runtime')
    expect(wrapper.text()).toContain('Claude X')
    expect(wrapper.text()).not.toContain('Check this computer')
    expect(wrapper.text()).not.toContain('Load available models')
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('saves the model when it is chosen, without replacing other overrides', async () => {
    vi.mocked(api.patch).mockResolvedValue({})
    const wrapper = mountStage({})
    await vi.waitFor(() => expect(wrapper.find('input[name="model"]').exists()).toBe(true))
    vi.mocked(api.get).mockImplementation((url: string) => url === '/api/workspaces'
      ? Promise.resolve({ workspaces: [{ name: 'personal', provider_overrides: {
        claude: { permission_mode: 'manual', thinking: 'high' }, opencode: { model: 'other-model' },
      } }] }) as never
      : Promise.reject(new Error('unexpected')) as never)
    await wrapper.findAll('input[name="model"]')[0].setValue()
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledWith('/api/workspaces/personal', {
      default_provider: 'claude', provider_overrides: {
        claude: { model: 'claude-x', permission_mode: 'manual', thinking: 'high' },
        opencode: { model: 'other-model' },
      },
    }))
    // Continue finds the choice already written and does not write it again.
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, pending: false }))
    expect(await stageOf(wrapper).save()).toBe(true)
    expect(api.patch).toHaveBeenCalledTimes(1)
  })

  it('shows what the installer already set and asks nothing until Change', async () => {
    const wrapper = mountStage({ workspaces: { workspaces: [{ name: 'personal', default_provider: 'claude', provider_overrides: { claude: { model: 'opus' } } }] } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('opus'))
    expect(wrapper.text()).toContain('Claude Code')
    expect(wrapper.find('input[type="radio"]').exists()).toBe(false)
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true }))
    expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining('/api/models'))

    await wrapper.get('button.change').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('input[name="model"]').exists()).toBe(true))
    expect(wrapper.find('input[name="runtime"]').exists()).toBe(true)
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('keeps the model choice and requests nothing when the checked runtime is clicked again', async () => {
    vi.mocked(api.patch).mockResolvedValue({})
    const wrapper = mountStage({})
    await vi.waitFor(() => expect(wrapper.find('input[name="model"]').exists()).toBe(true))
    await wrapper.findAll('input[name="model"]')[0].setValue()
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true, pending: false }))
    const modelReads = () => vi.mocked(api.get).mock.calls.filter(([url]) => String(url).startsWith('/api/models')).length
    const before = modelReads()
    await wrapper.get('input[name="runtime"][value="claude"]').trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
    expect((wrapper.get('input[name="model"]').element as HTMLInputElement).checked).toBe(true)
    expect(modelReads()).toBe(before)
  })

  it('does not write settings when the choice is deliberately deferred', async () => {
    const wrapper = mountStage({ status: missing })
    await vi.waitFor(() => expect(wrapper.find('button.defer').exists()).toBe(true))
    await wrapper.get('button.defer').trigger('click')
    expect(await stageOf(wrapper).save()).toBe(true)
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('keeps the selection and reports a failed save for retry', async () => {
    const wrapper = mountStage({})
    await vi.waitFor(() => expect(wrapper.find('input[name="model"]').exists()).toBe(true))
    vi.mocked(api.patch).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({})
    await wrapper.findAll('input[name="model"]')[0].setValue()
    await vi.waitFor(() => expect(wrapper.text()).toContain('offline'))
    expect((wrapper.get('input[name="model"]').element as HTMLInputElement).checked).toBe(true)
    await wrapper.get('button.retry').trigger('click')
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true }))
  })

  it('opens no terminal and writes nothing on mount', async () => {
    const wrapper = mountStage({})
    await vi.waitFor(() => expect(wrapper.find('input[name="model"]').exists()).toBe(true))
    expect(wrapper.findComponent({ name: 'HostTerminalButton' }).exists()).toBe(false)
    expect(api.patch).not.toHaveBeenCalled()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })

  it('requires an explicit model choice or a deliberate deferral', async () => {
    const wrapper = mountStage({})
    await vi.waitFor(() => expect(wrapper.text()).toContain('Models reported by this runtime'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    vi.mocked(api.patch).mockResolvedValue({})
    await wrapper.findAll('input[name="model"]')[0].setValue()
    await vi.waitFor(() => expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true }))
  })

  it('loads the other runtime\'s choices when it is selected', async () => {
    const wrapper = mountStage({ status: { connections: { ...ready.connections, opencode: { ok: true, auth: 'signed in', label: 'OpenCode' } } },
      models: { provider_models: { claude: ['alias'], opencode: ['openai/gpt'] }, provider_catalog_evidence: {} } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('Configured choices'))
    await wrapper.get('input[name="runtime"][value="opencode"]').setValue()
    await vi.waitFor(() => expect(wrapper.text()).toContain('openai/gpt'))
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })

  it('labels configured aliases as unverified and keeps reported models separate', async () => {
    const wrapper = mountStage({ models: unknown })
    await vi.waitFor(() => expect(wrapper.text()).toContain('Configured choices; availability not checked'))
    expect(wrapper.text()).toContain('alias')
    expect(wrapper.text()).not.toContain('Models reported by this runtime')
  })

  it('blocks Continue after a failed status read and offers a retry', async () => {
    const wrapper = mountStage({ status: new Error('offline') })
    await vi.waitFor(() => expect(wrapper.text()).toContain('Could not read the provider status'))
    expect(wrapper.find('button.retry').exists()).toBe(true)
    expect(wrapper.find('button.defer').exists()).toBe(false)
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })

  it('offers install or sign-in with the provider command when the runtime is missing', async () => {
    const wrapper = mountStage({ status: missing })
    await vi.waitFor(() => expect(wrapper.text()).toContain('not ready on this computer'))
    expect(wrapper.text()).toContain('brew install claude')
    const terminal = wrapper.findComponent({ name: 'HostTerminalButton' })
    expect(terminal.exists()).toBe(true)
    expect(terminal.props('action')).toBe('claude-install')
    expect(wrapper.find('button.defer').exists()).toBe(true)
    expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining('/api/models'))
  })
})

describe('OnboardingRuntimeStage missing status', () => {
  it('announces a runtime with no reported status and offers a re-check', async () => {
    const wrapper = mountStage({ status: { connections: {} } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('No status was reported for this runtime'))
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(wrapper.find('button.defer').exists()).toBe(false)
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    const before = vi.mocked(api.get).mock.calls.length
    await wrapper.get('button').trigger('click')
    await vi.waitFor(() => expect(vi.mocked(api.get).mock.calls.length).toBeGreaterThan(before))
  })
})
