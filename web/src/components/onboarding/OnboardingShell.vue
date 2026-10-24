<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
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
    <header class="onboarding-header">
      <p class="onboarding-kicker">Ciaobot setup</p>
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
      <button type="button" class="onboarding-button" :disabled="busy" @click="emit('reload')">Reload</button>
    </div>

    <footer class="onboarding-actions">
      <button type="button" class="onboarding-button back" :disabled="busy" @click="emit('back')">Back</button>
      <button type="button" class="onboarding-button onboarding-primary continue" :disabled="busy || !canContinue" @click="emit('continue')">Continue</button>
      <button type="button" class="onboarding-button onboarding-leave leave" :disabled="busy" @click="emit('leave')">Leave setup</button>
    </footer>
  </main>
</template>

<style scoped>
.onboarding-shell { max-width: 760px; margin: 0 auto; padding: var(--space-6, 24px); color: var(--fg); }
.onboarding-header { margin-bottom: 24px; }
.onboarding-kicker { margin: 0 0 8px; color: var(--accent); font: 600 11px var(--font-mono); letter-spacing: .5px; text-transform: uppercase; }
h1 { margin: 0; font: 700 24px/1.3 var(--font); }
h1:focus { outline: 2px solid var(--accent); outline-offset: 4px; }
.onboarding-context { color: var(--fg2); margin: 8px 0 0; }
.onboarding-steps ol { display: flex; gap: 8px; list-style: none; padding: 0; margin: 0 0 24px; }
.onboarding-steps li { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; color: var(--fg3); font-size: var(--text-sm, 12px); }
.onboarding-steps li[aria-current="step"] { color: var(--fg); }
.step-number { display: grid; place-items: center; flex: 0 0 28px; height: 28px; border: 1px solid var(--border); border-radius: 50%; font: 600 12px var(--font-mono); }
[aria-current="step"] .step-number { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.onboarding-content { display: grid; gap: 16px; }
.onboarding-stage, .onboarding-summary { min-width: 0; padding: 16px; border: 1px solid var(--border); border-radius: var(--radius, 10px); background: var(--bg2); }
.onboarding-alert { margin-top: 16px; padding: 12px; border: 1px solid var(--warning); border-radius: var(--radius, 10px); }
.onboarding-actions { display: flex; align-items: center; gap: 8px; margin-top: 24px; }
.onboarding-button { min-height: 44px; padding: 8px 14px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm, 6px); background: transparent; color: var(--fg); font: inherit; cursor: pointer; }
.onboarding-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.onboarding-button:disabled { opacity: .55; cursor: not-allowed; }
.onboarding-primary { margin-left: auto; color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.onboarding-leave { margin-left: 8px; }
@media (max-width: 560px) { .onboarding-shell { padding: 16px; } .onboarding-steps ol { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); } .onboarding-actions { flex-wrap: wrap; } .onboarding-leave { margin-left: 0; } }
</style>
