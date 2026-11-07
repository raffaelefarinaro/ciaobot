<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import OnboardingButton from './OnboardingButton.vue'
import OnboardingStepFooter from './OnboardingStepFooter.vue'
import { ONBOARDING_STEPS, type OnboardingStageContext, type OnboardingStep } from '../../lib/onboarding'

const props = defineProps<{
  context: OnboardingStageContext
  step: OnboardingStep
  busy: boolean
  error: string | null
  conflict: boolean
  canContinue: boolean
}>()
const emit = defineEmits<{ continue: []; back: []; leave: []; reload: [] }>()
const heading = ref<HTMLHeadingElement | null>(null)
watch(() => props.step, async () => { await nextTick(); heading.value?.focus() })
</script>

<template>
  <main class="onboarding-shell" aria-labelledby="onboarding-title">
    <div class="onboarding-page">
      <header class="onboarding-header">
        <h1 id="onboarding-title" ref="heading" tabindex="-1">{{ ONBOARDING_STEPS.find(item => item.id === step)?.label }}</h1>
        <p class="onboarding-context">{{ context.target.kind === 'installation' ? 'This installation' : `Workspace · ${context.target.name}` }}</p>
      </header>

      <nav aria-label="Setup steps" class="onboarding-steps">
        <ol>
          <li v-for="(item, index) in ONBOARDING_STEPS" :key="item.id" :aria-current="step === item.id ? 'step' : undefined">
            <span class="step-number">{{ index + 1 }}</span><span>{{ item.label }}</span>
          </li>
        </ol>
      </nav>

      <section class="onboarding-content">
        <div class="onboarding-stage"><slot name="stage" /></div>
        <aside v-if="$slots.summary" class="onboarding-summary" aria-label="Setup summary"><slot name="summary" /></aside>
      </section>

      <div v-if="error || conflict" class="onboarding-alert" role="alert" aria-live="assertive">
        <p v-if="conflict">Progress changed elsewhere. Reload to continue with the latest version.</p>
        <p v-if="error">{{ error }}</p>
        <OnboardingButton :disabled="busy" @click="emit('reload')">Reload</OnboardingButton>
      </div>

      <OnboardingStepFooter>
        <template #start>
          <OnboardingButton class="back" :disabled="busy" @click="emit('back')">Back</OnboardingButton>
        </template>
        <OnboardingButton variant="link" class="leave" :disabled="busy" @click="emit('leave')">Leave setup</OnboardingButton>
        <OnboardingButton variant="primary" class="continue" :disabled="busy || !canContinue" @click="emit('continue')">{{ step === 'finish' ? 'Finish setup' : 'Continue' }}</OnboardingButton>
      </OnboardingStepFooter>
    </div>
  </main>
</template>

<style scoped>
/* The page and body do not scroll, so the shell is the scroll container and the
   footer sticks to its bottom edge. */
.onboarding-shell { flex: 1; min-height: 0; overflow-y: auto; color: var(--fg); }
.onboarding-page { max-width: 760px; margin: 0 auto; padding: var(--space-5, 24px) var(--space-5, 24px) 0; }
.onboarding-header { margin-bottom: var(--space-5, 24px); }
h1 { margin: 0; font: 700 24px/1.3 var(--font); }
h1:focus { outline: none; }
.onboarding-context { color: var(--fg2); margin: var(--space-2, 8px) 0 0; }
.onboarding-steps ol { display: flex; gap: var(--space-2, 8px); list-style: none; padding: 0; margin: 0 0 var(--space-5, 24px); }
.onboarding-steps li { display: flex; align-items: center; gap: var(--space-2, 8px); flex: 1; min-width: 0; color: var(--fg3); font-size: var(--text-sm, 12px); }
.onboarding-steps li[aria-current="step"] { color: var(--fg); }
.step-number { display: grid; place-items: center; flex: 0 0 28px; height: 28px; border: 1px solid var(--border); border-radius: 50%; font: 600 12px var(--font-mono); }
[aria-current="step"] .step-number { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.onboarding-content { display: grid; gap: var(--space-4, 16px); }
.onboarding-stage, .onboarding-summary { min-width: 0; padding: var(--space-5, 24px); border: 1px solid var(--border); border-radius: var(--radius, 10px); background: var(--bg2); }
.onboarding-alert { display: grid; justify-items: start; gap: var(--space-3, 12px); margin-top: var(--space-4, 16px); padding: var(--space-4, 16px); border: 1px solid var(--warning); border-radius: var(--radius, 10px); }
.onboarding-alert p { margin: 0; }
@media (max-width: 560px) { .ob-button.leave { padding-inline: var(--space-2, 8px); } .onboarding-page { padding: var(--space-4, 16px) var(--space-4, 16px) 0; } .onboarding-stage, .onboarding-summary { padding: var(--space-4, 16px); } .onboarding-steps ol { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
