// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
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
    await vi.waitFor(() => expect(wrapper.text()).toContain('Access & memory'))
    // The shell's Continue is off on this screen, but Memory's own continue must
    // be able to advance once Access is ready too.
    expect(wrapper.text()).toContain('Memory handover')
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
    await vi.waitFor(() => expect(wrapper.text()).toContain('Main Ciaobot folder'))
    // The provisioned path is the engine's, not an empty query value.
    expect((wrapper.get('input[name="folder"]').element as HTMLInputElement).value).toBe('/srv/ciaobot')
    expect((wrapper.get('input[name="folder"]').element as HTMLInputElement).readOnly).toBe(true)
  })
})

describe('OnboardingView advancement', () => {
  it('advances from the shared screen only after memory saves and access is ready', async () => {
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/onboarding') return Promise.resolve({ schema: 1, revision: 2, installation: { status: 'in_progress', step: 'access_memory' }, workspaces: {} }) as never
      if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal' }] }) as never
      if (url === '/api/setup-status') return Promise.resolve({ workspace_root: '/srv/ciaobot' }) as never
      return Promise.resolve({}) as never
    })
    vi.mocked(api.patch).mockResolvedValue({ schema: 1, revision: 3, installation: { status: 'in_progress', step: 'care' }, workspaces: {} } as never)

    const MemoryStub = {
      name: 'OnboardingMemoryStage',
      emits: ['state', 'continue', 'back', 'refresh'],
      template: '<button class="memory-continue" @click="$emit(\'state\', { dirty: false, pending: false, canContinue: true, error: null }); $emit(\'continue\')">save</button>',
    }
    const AccessStub = {
      name: 'OnboardingAccessStage',
      emits: ['state', 'continue', 'back', 'refresh'],
      template: '<div />',
      mounted(this: { $emit: (event: string, payload: unknown) => void }) {
        this.$emit('state', { dirty: false, pending: false, canContinue: true, error: null })
      },
    }
    const wrapper = mount(OnboardingView, {
      global: { stubs: { OnboardingAccessStage: AccessStub, OnboardingMemoryStage: MemoryStub } },
    })
    await vi.waitFor(() => expect(wrapper.find('.memory-continue').exists()).toBe(true))
    await wrapper.get('.memory-continue').trigger('click')
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledWith('/api/onboarding', expect.objectContaining({ step: 'care' })))
  })
})
