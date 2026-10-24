<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api'
import type { OnboardingReceipt } from '../lib/onboarding'

// Presentation only: it reads the current receipt and links into the journey.
// It is not a dismissal — the card returns while a target is still pending.
const router = useRouter()
const pending = ref(false)
const label = ref('')
const loaded = ref(false)
let disposed = false

function describe(receipt: OnboardingReceipt): string {
  if (receipt.installation.status === 'in_progress') return 'Finish setting up this installation.'
  const workspace = Object.entries(receipt.workspaces).find(([, progress]) => progress.status === 'in_progress')
  return workspace ? `Finish setting up the workspace “${workspace[0]}”.` : ''
}

async function load(): Promise<void> {
  try {
    const receipt = await api.get<OnboardingReceipt>('/api/onboarding')
    if (disposed) return
    label.value = describe(receipt)
    pending.value = !!label.value
  } catch {
    // No receipt (or an unreadable one) means the journey cannot be resumed
    // from Home; Settings still reports the real state.
    pending.value = false
  } finally {
    if (!disposed) loaded.value = true
  }
}

function resume(): void {
  const workspace = /workspace “([^”]+)”/.exec(label.value)?.[1]
  void router.push(workspace ? { path: '/onboarding', query: { workspace } } : { path: '/onboarding' })
}

function onFocus(): void { void load() }
onMounted(() => { void load(); window.addEventListener('focus', onFocus) })
onBeforeUnmount(() => { disposed = true; window.removeEventListener('focus', onFocus) })

const visible = computed(() => loaded.value && pending.value)
</script>

<template>
  <section v-if="visible" class="home-onboarding-resume" aria-label="Setup not finished">
    <p>{{ label }}</p>
    <button type="button" @click="resume">Resume setup</button>
  </section>
</template>

<style scoped>
.home-onboarding-resume { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 0 16px; padding: 12px 16px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg2); }
.home-onboarding-resume p { margin: 0; color: var(--fg2); }
.home-onboarding-resume button { min-height: 44px; padding: 8px 14px; border: 1px solid var(--accent); border-radius: var(--radius-sm); background: var(--accent); color: var(--on-accent); font: inherit; cursor: pointer; }
.home-onboarding-resume button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (max-width: 560px) { .home-onboarding-resume { flex-direction: column; align-items: stretch; } }
</style>
