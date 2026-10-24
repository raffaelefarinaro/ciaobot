import { ref } from 'vue'
import { defineStore } from 'pinia'
import { api } from '../lib/api'
import { onboardingProgress, type OnboardingReceipt, type OnboardingStep, type OnboardingTarget } from '../lib/onboarding'

export const useOnboardingStore = defineStore('onboarding', () => {
  const receipt = ref<OnboardingReceipt | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)
  const conflict = ref(false)
  let generation = 0
  let loadFlight: Promise<void> | null = null

  async function load(): Promise<void> {
    if (loadFlight) return loadFlight
    const ticket = generation
    loading.value = true
    error.value = null
    loadFlight = api.get<OnboardingReceipt>('/api/onboarding').then(value => {
      if (ticket === generation) receipt.value = value
    }).catch(reason => {
      if (ticket === generation) error.value = reason instanceof Error ? reason.message : 'Could not load onboarding progress.'
    }).finally(() => {
      if (ticket === generation) loading.value = false
      loadFlight = null
    })
    return loadFlight
  }

  function progress(target: OnboardingTarget) { return onboardingProgress(receipt.value, target) }

  async function mutate(target: OnboardingTarget, step?: OnboardingStep, done = false): Promise<boolean> {
    if (!receipt.value || saving.value) return false
    const ticket = generation
    saving.value = true
    error.value = null
    conflict.value = false
    const url = done ? '/api/onboarding/complete' : '/api/onboarding'
    const body = { expected_revision: receipt.value.revision, target, ...(step ? { step } : {}) }
    try {
      const next = done
        ? await api.post<OnboardingReceipt>(url, body)
        : await api.patch<OnboardingReceipt>(url, body)
      if (ticket === generation) receipt.value = next
      return true
    } catch (reason) {
      if (ticket !== generation) return false
      if ((reason as { status?: number })?.status === 409) {
        conflict.value = true
        await load()
        if (ticket === generation) conflict.value = true
      } else error.value = reason instanceof Error ? reason.message : 'Could not save onboarding progress.'
      return false
    } finally { if (ticket === generation) saving.value = false }
  }

  const setStep = (target: OnboardingTarget, step: OnboardingStep) => mutate(target, step)
  const complete = (target: OnboardingTarget) => mutate(target, undefined, true)
  function reset() {
    generation++
    receipt.value = null
    loading.value = false
    saving.value = false
    error.value = null
    conflict.value = false
    loadFlight = null
  }

  return { receipt, loading, saving, error, conflict, load, progress, setStep, complete, reset }
})
