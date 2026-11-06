// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, onMounted } from 'vue'
import OnboardingView from '../OnboardingView.vue'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../../../lib/api'

const replace = vi.fn()
vi.mock('vue-router', async importOriginal => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ replace }),
}))
vi.mock('../../../lib/api', () => ({ api: { get: vi.fn(), patch: vi.fn(), post: vi.fn() } }))

// A Memory stage that reports ready and exposes the commit the shell calls on Continue.
function memoryStub(save: () => Promise<boolean>) {
  return defineComponent({
    name: 'OnboardingMemoryStage',
    emits: ['state'],
    setup(_, { emit, expose }) {
      expose({ save })
      onMounted(() => emit('state', { dirty: false, pending: false, canContinue: true, error: null }))
      return () => h('div')
    },
  })
}

beforeEach(() => { setActivePinia(createPinia()); vi.resetAllMocks() })

describe('onboarding route', () => {
  it('is authenticated', async () => {
    const { routes } = await import('../../../router')
    expect(routes.find(route => route.path === '/onboarding')?.meta?.requiresAuth).toBe(true)
  })
})

describe('OnboardingView resumes', () => {
  it('can advance from a resumed access/memory step once both halves are ready', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/onboarding') return Promise.resolve({ schema: 1, revision: 2, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} }) as never
      if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal', agent_fs_scope: 'workspace' }] }) as never
      if (url === '/api/setup-status') return Promise.resolve({ workspace_root: '/srv/ciaobot' }) as never
      if (url === '/api/host/terminal') return Promise.resolve({ platform: 'darwin' }) as never
      if (url === '/api/settings/routines') return Promise.resolve({ provider_default_modes: { claude: 'auto' } }) as never
      if (url === '/api/proposals/handover') return Promise.resolve({ handover: 'off', acting_available: true }) as never
      if (url.startsWith('/api/proposals/handover')) return Promise.resolve({ handover: 'off', acting_available: true }) as never
      return Promise.resolve({ provider_catalog_evidence: {}, provider_models: {} }) as never
    })
    const wrapper = mount(OnboardingView)
    await vi.waitFor(() => expect(wrapper.text()).toContain('Memory & access'))
    // One scrolling screen: both halves are present, and Memory's options are
    // reachable above the single footer.
    expect(wrapper.text()).toContain('Memory handover')
    expect(wrapper.text()).toContain('Ask me first')
    expect(wrapper.findAll('footer')).toHaveLength(1)
    expect(wrapper.findAll('button.continue')).toHaveLength(1)
    expect(wrapper.findAll('button.back')).toHaveLength(1)
  })
})

describe('OnboardingView', () => {
  it('waits for the context before mounting a stage', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/onboarding') return Promise.resolve({ schema: 1, revision: 2, installation: { status: 'in_progress', step: 'location' }, workspaces: {} }) as never
      if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal' }] }) as never
      if (url === '/api/setup-status') return Promise.resolve({ workspace_root: '/srv/ciaobot' }) as never
      return Promise.reject(new Error('unexpected')) as never
    })
    const wrapper = mount(OnboardingView)
    await vi.waitFor(() => expect(wrapper.text()).toContain('Ciaobot folder'))
    // The provisioned path is the engine's, shown as set rather than asked again.
    await vi.waitFor(() => expect(wrapper.text()).toContain('/srv/ciaobot'))
    expect(wrapper.text()).toContain('personal')
    expect(wrapper.find('input[name="folder"]').exists()).toBe(false)
    expect(wrapper.find('input[name="name"]').exists()).toBe(false)
  })
})

describe('OnboardingView advancement', () => {
  it('completes through the visible Finish setup action', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/onboarding') return Promise.resolve({ schema: 1, revision: 2, installation: { status: 'in_progress', step: 'finish' }, workspaces: {} }) as never
      if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal' }] }) as never
      if (url === '/api/setup-status') return Promise.resolve({ workspace_root: '/srv/ciaobot' }) as never
      if (url === '/api/schedules') return Promise.resolve([]) as never
      return Promise.resolve({}) as never
    })
    vi.mocked(api.post).mockResolvedValue({ schema: 1, revision: 3, installation: { status: 'completed', step: 'finish' }, workspaces: {} } as never)
    const wrapper = mount(OnboardingView)
    await vi.waitFor(() => expect((wrapper.get('footer button.continue').element as HTMLButtonElement).disabled).toBe(false))
    expect(wrapper.get('footer button.continue').text()).toBe('Finish setup')
    // The Finish stage carries no button row of its own.
    expect(wrapper.find('.finish-stage button.primary').exists()).toBe(false)
    await wrapper.get('footer button.continue').trigger('click')
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/onboarding/complete', expect.objectContaining({ target: { kind: 'installation' } })))
    expect(replace).toHaveBeenCalledWith('/')
  })
  it('waits for the actual runtime/model save before advancing the receipt', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/onboarding') return Promise.resolve({ schema: 1, revision: 2, installation: { status: 'in_progress', step: 'runtime' }, workspaces: {} }) as never
      if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal', default_provider: 'claude' }] }) as never
      if (url === '/api/setup-status') return Promise.resolve({ workspace_root: '/srv/ciaobot' }) as never
      if (url === '/api/settings/providers') return Promise.resolve({ connections: { opencode: { ok: true, auth: 'signed in' } } }) as never
      if (url.startsWith('/api/models')) return Promise.resolve({ provider_catalog_evidence: { opencode: { status: 'observed', models: [{ id: 'openai/chosen', label: 'Chosen' }] } } }) as never
      return Promise.resolve({}) as never
    })
    let finishSave!: () => void
    const saved = new Promise<void>(resolve => { finishSave = resolve })
    vi.mocked(api.patch).mockImplementation((url: string) => url === '/api/workspaces/personal'
      ? saved.then(() => ({ workspaces: [{ name: 'personal' }] })) as never
      : Promise.resolve({ schema: 1, revision: 3, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} }) as never)
    const wrapper = mount(OnboardingView)
    await vi.waitFor(() => expect(wrapper.find('input[value="opencode"]').exists()).toBe(true))
    await wrapper.get('input[value="opencode"]').setValue()
    // The models list itself, with no Load or Check button in between.
    await vi.waitFor(() => expect(wrapper.find('input[name="model"]').exists()).toBe(true))
    await wrapper.get('input[name="model"]').setValue()
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledWith('/api/workspaces/personal', {
      default_provider: 'opencode', provider_overrides: { opencode: { model: 'openai/chosen' } },
    }))
    // Continue waits for that write; the receipt does not move yet.
    expect(wrapper.get('footer button.continue').attributes('disabled')).toBeDefined()
    expect(api.patch).not.toHaveBeenCalledWith('/api/onboarding', expect.anything())
    finishSave()
    await vi.waitFor(() => expect(wrapper.get('footer button.continue').attributes('disabled')).toBeUndefined())
    await wrapper.get('footer button.continue').trigger('click')
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledWith('/api/onboarding', expect.objectContaining({ step: 'access_memory' })))
    expect(vi.mocked(api.patch).mock.calls.filter(([url]) => url === '/api/workspaces/personal')).toHaveLength(1)
    await vi.waitFor(() => expect(wrapper.get('h1').text()).toBe('Memory & access'))
  })
  it('advances from the shared screen only after memory saves and access is ready', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/onboarding') return Promise.resolve({ schema: 1, revision: 2, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} }) as never
      if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal' }] }) as never
      if (url === '/api/setup-status') return Promise.resolve({ workspace_root: '/srv/ciaobot' }) as never
      return Promise.resolve({}) as never
    })
    vi.mocked(api.patch).mockResolvedValue({ schema: 1, revision: 3, installation: { status: 'in_progress', step: 'care' }, workspaces: {} } as never)

    const memorySave = vi.fn().mockResolvedValue(true)
    const MemoryStub = memoryStub(memorySave)
    const AccessStub = {
      name: 'OnboardingAccessStage',
      emits: ['state'],
      template: '<div />',
      mounted(this: { $emit: (event: string, payload: unknown) => void }) {
        this.$emit('state', { dirty: false, pending: false, canContinue: true, error: null })
      },
    }
    const wrapper = mount(OnboardingView, {
      global: { stubs: { OnboardingAccessStage: AccessStub, OnboardingMemoryStage: MemoryStub } },
    })
    await vi.waitFor(() => expect((wrapper.get('footer button.continue').element as HTMLButtonElement).disabled).toBe(false))
    await wrapper.get('footer button.continue').trigger('click')
    expect(memorySave).toHaveBeenCalledTimes(1)
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledWith('/api/onboarding', expect.objectContaining({ step: 'care' })))
    // The journey moved on, so the care screen is what the operator sees.
    await vi.waitFor(() => expect(wrapper.text()).toContain('When should workspace care run?'))
  })
})

describe('OnboardingView failed save', () => {
  it('keeps the step and writes nothing when the save fails', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/onboarding') return Promise.resolve({ schema: 1, revision: 2, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} }) as never
      if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal' }] }) as never
      if (url === '/api/setup-status') return Promise.resolve({ workspace_root: '/srv/ciaobot' }) as never
      return Promise.resolve({}) as never
    })
    vi.mocked(api.patch).mockRejectedValue(new Error('offline'))

    const memorySave = vi.fn().mockResolvedValue(false)
    const MemoryStub = memoryStub(memorySave)
    const AccessStub = {
      name: 'OnboardingAccessStage',
      emits: ['state'],
      template: '<div />',
      mounted(this: { $emit: (event: string, payload: unknown) => void }) {
        this.$emit('state', { dirty: false, pending: false, canContinue: true, error: null })
      },
    }
    const wrapper = mount(OnboardingView, {
      global: { stubs: { OnboardingAccessStage: AccessStub, OnboardingMemoryStage: MemoryStub } },
    })
    await vi.waitFor(() => expect((wrapper.get('footer button.continue').element as HTMLButtonElement).disabled).toBe(false))
    await wrapper.get('footer button.continue').trigger('click')
    await vi.waitFor(() => expect(memorySave).toHaveBeenCalledTimes(1))
    // The commit failed: the journey did not move and nothing was written.
    expect(vi.mocked(api.patch)).not.toHaveBeenCalled()
    expect(wrapper.get('h1').text()).toBe('Memory & access')
    expect(wrapper.text()).not.toContain('When should workspace care run?')
  })
})
