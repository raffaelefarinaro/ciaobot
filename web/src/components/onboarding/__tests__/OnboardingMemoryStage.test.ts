// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { computed, reactive, ref } from 'vue'
import OnboardingMemoryStage from '../OnboardingMemoryStage.vue'

const preferences = {
  draft: reactive({ memoryHandover: 'all' }),
  savedHandover: ref('off'),
  actingAvailable: ref(true),
  dirty: computed(() => true),
  loaded: ref(true),
  pending: ref(false),
  error: ref<string | null>(null),
  load: vi.fn(),
  saveMemoryHandover: vi.fn(),
}
vi.mock('../../../composables/useOnboardingPreferences', () => ({
  useOnboardingPreferences: () => preferences,
}))

const props = { context: { target: { kind: 'workspace' as const, name: 'studio' }, workspace: 'studio', revision: 1, provisioned: true }, busy: false }

beforeEach(() => {
  preferences.draft.memoryHandover = 'all'
  preferences.actingAvailable.value = true
  preferences.error.value = null
  preferences.saveMemoryHandover.mockReset().mockResolvedValue(true)
})

describe('OnboardingMemoryStage', () => {
  it('offers three plain-word options with no acknowledgement or save step', () => {
    const view = mount(OnboardingMemoryStage, { props })
    expect(view.text()).toContain('Ask me first')
    expect(view.text()).toContain('Skip likely duplicates')
    expect(view.text()).toContain("File what's clear, ask about the rest")
    expect(view.text()).toContain('queued for review')
    expect(view.text()).toContain('History')
    expect(view.find('input[type="checkbox"]').exists()).toBe(false)
    expect(view.find('button').exists()).toBe(false)
    expect(view.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
  })

  it('saves a choice the moment it is made', async () => {
    const view = mount(OnboardingMemoryStage, { props })
    await view.get('input[value="dismiss"]').setValue()
    expect(preferences.draft.memoryHandover).toBe('dismiss')
    expect(preferences.saveMemoryHandover).toHaveBeenCalledTimes(1)
  })

  it('lets the shell commit the proposed choice through save', async () => {
    const view = mount(OnboardingMemoryStage, { props })
    expect(preferences.saveMemoryHandover).not.toHaveBeenCalled()
    expect(await (view.vm as unknown as { save: () => Promise<boolean> }).save()).toBe(true)
    expect(preferences.saveMemoryHandover).toHaveBeenCalledTimes(1)
  })

  it('puts the choice back on the saved value when the save fails', async () => {
    preferences.savedHandover.value = 'off'
    preferences.saveMemoryHandover.mockResolvedValue(false)
    const view = mount(OnboardingMemoryStage, { props })
    await view.get('input[value="dismiss"]').setValue()
    expect(preferences.draft.memoryHandover).toBe('off')
    preferences.savedHandover.value = 'off'
  })

  it('marks the unsaved proposal as suggested', () => {
    const view = mount(OnboardingMemoryStage, { props })
    expect(view.text()).toContain('Suggested. Saved when you continue.')
  })

  it('blocks Continue while a save has failed', () => {
    preferences.error.value = 'offline'
    const view = mount(OnboardingMemoryStage, { props })
    expect(view.get('[role="alert"]').text()).toContain('offline')
    expect(view.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: false, error: 'offline' })
  })

  it('says acceptance is not acting while the trusted service is unavailable', () => {
    preferences.actingAvailable.value = false
    const view = mount(OnboardingMemoryStage, { props })
    expect(view.text()).toContain('acceptance is not acting now')
    expect(view.text()).toContain('always-loaded memory')
  })
})
