import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import OnboardingMemoryStage from '../OnboardingMemoryStage.vue'

vi.mock('../../../composables/useOnboardingPreferences', () => ({
  useOnboardingPreferences: () => ({ draft: { memoryHandover: 'all', acknowledged: false }, savedHandover: ref('off'), actingAvailable: ref(true), dirty: computed(() => true), pending: ref(false), error: ref(null), load: vi.fn(), saveMemoryHandover: vi.fn() }),
}))

describe('OnboardingMemoryStage', () => {
  it('requires acknowledgement and explains the outcomes and history', () => {
    const view = mount(OnboardingMemoryStage, { props: { context: { target: { kind: 'workspace', name: 'studio' }, workspace: 'studio', revision: 1, provisioned: true }, busy: false } })
    expect(view.text()).toContain('queued for review')
    expect(view.text()).toContain('History')
    expect(view.find('input[type="checkbox"]').exists()).toBe(true)
    expect(view.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false })
  })
})
// @vitest-environment jsdom
