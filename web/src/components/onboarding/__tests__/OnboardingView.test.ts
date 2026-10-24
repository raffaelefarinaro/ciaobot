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
vi.mock('../../../lib/api', () => ({ api: { get: vi.fn() } }))

beforeEach(() => { setActivePinia(createPinia()); vi.resetAllMocks() })

describe('onboarding route', () => {
  it('is authenticated', async () => {
    const { routes } = await import('../../../router')
    expect(routes.find(route => route.path === '/onboarding')?.meta?.requiresAuth).toBe(true)
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
