// @vitest-environment jsdom
import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import OnboardingRuntimeStage from '../onboarding/OnboardingRuntimeStage.vue'

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../../lib/api', () => ({ api: { get, post } }))
vi.mock('../../composables/useHostTerminal', () => ({ useHostTerminal: () => ({ available: { value: false }, canRun: () => false, ensureCapabilities: vi.fn(), runAction: vi.fn() }) }))

const context = { target: { kind: 'installation' as const }, workspace: 'personal', revision: 0, provisioned: false }
const evidence = (status: 'observed' | 'unknown', models: {id:string;label:string}[] = []) => ({ status, source: null, models, reason: null })

describe('OnboardingRuntimeStage', () => {
  beforeEach(() => { get.mockReset(); post.mockReset() })

  it('keeps configured aliases separate from observed runtime models', async () => {
    get.mockResolvedValue({ provider_models: { claude: ['alias'] }, provider_catalog_evidence: { claude: evidence('observed', [{ id: 'claude-sonnet', label: 'Sonnet' }]) } })
    const wrapper = mount(OnboardingRuntimeStage, { props: { context, busy: false } })
    await flushPromises()
    await wrapper.get('input[value="claude"]').setValue()
    expect(wrapper.text()).toContain('reported by this runtime')
    expect(wrapper.text()).not.toContain('alias')
    expect(wrapper.text()).toContain('A successful reply has not been verified')
  })

  it('requires explicit choice or deliberate deferral and labels unverified aliases', async () => {
    get.mockResolvedValue({ provider_models: { claude: ['alias'] }, provider_catalog_evidence: { claude: evidence('unknown') } })
    const wrapper = mount(OnboardingRuntimeStage, { props: { context, busy: false } })
    await flushPromises()
    expect(wrapper.text()).toContain('Configured choices; availability not checked')
    expect(wrapper.text()).toContain('alias')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
    await wrapper.get('button[aria-pressed]').trigger('click')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
  })
})
