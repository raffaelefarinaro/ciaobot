import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  PRODUCT_TOUR_STEPS,
  clearTourCompleted,
  isTourCompleted,
  markTourCompleted,
  type TourBeforeEnter,
} from '../lib/productTour'

export type ProductTourHooks = {
  openSidebar?: () => void
  navigateToChat?: () => Promise<void> | void
  ensureWelcomeChat?: () => Promise<void>
}

export const useProductTourStore = defineStore('productTour', () => {
  const active = ref(false)
  const stepIndex = ref(0)
  const preparing = ref(false)

  let hooks: ProductTourHooks = {}

  const steps = PRODUCT_TOUR_STEPS
  const currentStep = computed(() => steps[stepIndex.value] ?? null)
  const isFirst = computed(() => stepIndex.value <= 0)
  const isLast = computed(() => stepIndex.value >= steps.length - 1)
  const progressLabel = computed(() => `${stepIndex.value + 1} / ${steps.length}`)

  function registerHooks(next: ProductTourHooks) {
    hooks = { ...hooks, ...next }
  }

  async function runBeforeEnter(actions: TourBeforeEnter[] | undefined) {
    if (!actions?.length) return
    for (const action of actions) {
      if (action === 'openSidebar') {
        hooks.openSidebar?.()
      } else if (action === 'chatRoute') {
        await hooks.navigateToChat?.()
      } else if (action === 'welcomeChat') {
        await hooks.ensureWelcomeChat?.()
      }
    }
    // Let the DOM settle after navigation / sidebar expand.
    await new Promise<void>(resolve => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
    })
  }

  async function prepareStep(index: number) {
    const step = steps[index]
    if (!step) return
    preparing.value = true
    try {
      await runBeforeEnter(step.beforeEnter)
    } finally {
      preparing.value = false
    }
  }

  async function start(force = false) {
    if (!force && isTourCompleted()) return
    if (force) clearTourCompleted()
    stepIndex.value = 0
    active.value = true
    await prepareStep(0)
  }

  async function maybeAutoStart() {
    if (active.value || isTourCompleted()) return
    await start(false)
  }

  async function next() {
    if (isLast.value) {
      finish()
      return
    }
    stepIndex.value += 1
    await prepareStep(stepIndex.value)
  }

  async function prev() {
    if (isFirst.value) return
    stepIndex.value -= 1
    await prepareStep(stepIndex.value)
  }

  function skip() {
    finish()
  }

  function finish() {
    active.value = false
    markTourCompleted()
  }

  function replay() {
    return start(true)
  }

  return {
    active,
    stepIndex,
    preparing,
    steps,
    currentStep,
    isFirst,
    isLast,
    progressLabel,
    registerHooks,
    start,
    maybeAutoStart,
    next,
    prev,
    skip,
    finish,
    replay,
    isTourCompleted,
    clearTourCompleted,
  }
})
