// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import HomeOnboardingResume from '../HomeOnboardingResume.vue'
import { api } from '../../lib/api'

const push = vi.fn()
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))
vi.mock('../../lib/api', () => ({ api: { get: vi.fn() } }))

const receipt = (installation: string, workspaces: Record<string, { status: string; step: string }> = {}) => ({
  schema: 1, revision: 1, installation: { status: installation, step: 'location' }, workspaces,
})

beforeEach(() => { setActivePinia(createPinia()); vi.resetAllMocks() })

describe('HomeOnboardingResume', () => {
  it('stays hidden when the installation is complete', async () => {
    vi.mocked(api.get).mockResolvedValue(receipt('completed') as never)
    const wrapper = mount(HomeOnboardingResume)
    await vi.waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(wrapper.text()).not.toContain('Resume setup')
  })

  it('offers a pending installation and routes into the journey', async () => {
    vi.mocked(api.get).mockResolvedValue(receipt('in_progress') as never)
    const wrapper = mount(HomeOnboardingResume)
    await vi.waitFor(() => expect(wrapper.text()).toContain('Finish setting up this installation'))
    await wrapper.get('button').trigger('click')
    expect(push).toHaveBeenCalledWith({ path: '/onboarding' })
  })

  it('routes a pending workspace with its name', async () => {
    vi.mocked(api.get).mockResolvedValue(receipt('legacy_exempt', { studio: { status: 'in_progress', step: 'runtime' } }) as never)
    const wrapper = mount(HomeOnboardingResume)
    await vi.waitFor(() => expect(wrapper.text()).toContain('studio'))
    await wrapper.get('button').trigger('click')
    expect(push).toHaveBeenCalledWith({ path: '/onboarding', query: { workspace: 'studio' } })
  })

  it('never claims a pending journey when the receipt cannot be read', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('offline'))
    const wrapper = mount(HomeOnboardingResume)
    await vi.waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(wrapper.text()).not.toContain('Resume setup')
  })
})
