import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import OnboardingMemoryStage from '../OnboardingMemoryStage.vue'

const preferences = {
  draft: { memoryHandover: 'all', acknowledged: false },
  savedHandover: ref('off'),
  actingAvailable: ref(true),
  dirty: computed(() => true),
  pending: ref(false),
  error: ref<string | null>(null),
  load: vi.fn(),
  saveMemoryHandover: vi.fn(),
}
vi.mock('../../../composables/useOnboardingPreferences', () => ({
  useOnboardingPreferences: () => preferences,
}))

describe('OnboardingMemoryStage', () => {
  it('requires acknowledgement and explains the outcomes and history', () => {
    const view = mount(OnboardingMemoryStage, { props: { context: { target: { kind: 'workspace', name: 'studio' }, workspace: 'studio', revision: 1, provisioned: true }, busy: false } })
    expect(view.text()).toContain('queued for review')
    expect(view.text()).toContain('History')
    expect(view.find('input[type="checkbox"]').exists()).toBe(true)
    expect(view.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })

  it('says acceptance is not acting while the trusted service is unavailable', () => {
    preferences.actingAvailable.value = false
    const view = mount(OnboardingMemoryStage, { props: { context: { target: { kind: 'workspace', name: 'studio' }, workspace: 'studio', revision: 1, provisioned: true }, busy: false } })
    expect(view.text()).toContain('acceptance is not acting now')
    expect(view.text()).toContain('always-loaded memory')
    preferences.actingAvailable.value = true
  })
})
// @vitest-environment jsdom
