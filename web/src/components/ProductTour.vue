<template>
  <Teleport to="body">
    <div
      v-if="tour.active && tour.currentStep"
      class="product-tour"
      role="dialog"
      aria-modal="true"
      :aria-label="tour.currentStep.title"
    >
      <div class="product-tour-backdrop" @click.stop />

      <div
        v-if="spotlightStyle"
        class="product-tour-spotlight"
        :style="spotlightStyle"
        aria-hidden="true"
      />

      <div
        class="product-tour-card"
        :class="{ 'product-tour-card--center': isCentered }"
        :style="cardStyle"
        @click.stop
      >
        <div class="product-tour-progress">{{ tour.progressLabel }}</div>
        <h2 class="product-tour-title">{{ tour.currentStep.title }}</h2>
        <p class="product-tour-body">{{ tour.currentStep.body }}</p>
        <p v-if="showMissingHint" class="product-tour-missing">{{ tour.currentStep.missingHint }}</p>
        <div class="product-tour-actions">
          <button type="button" class="product-tour-skip" @click="tour.skip()">Skip tour</button>
          <div class="product-tour-nav">
            <button
              type="button"
              class="btn-small"
              :disabled="tour.isFirst || tour.preparing"
              @click="tour.prev()"
            >Back</button>
            <button
              type="button"
              class="btn-primary"
              :disabled="tour.preparing"
              @click="tour.next()"
            >{{ tour.isLast ? 'Done' : 'Next' }}</button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useProductTourStore } from '../stores/productTour'
import { useProjectStore } from '../stores/projects'
import { shouldShowMissingHint, tourTargetSelector } from '../lib/productTour'

const tour = useProductTourStore()
const projectStore = useProjectStore()

const targetRect = ref<DOMRect | null>(null)

const targetFound = computed(() => {
  const step = tour.currentStep
  if (!step?.target || step.placement === 'center') return true
  return targetRect.value !== null
})

const showMissingHint = computed(() => {
  const step = tour.currentStep
  if (!step) return false
  return shouldShowMissingHint(
    step,
    {
      hasActiveChat: !!projectStore.activeChat,
      projectCount: projectStore.workspaceProjects.length,
    },
    targetFound.value,
  )
})

const isCentered = computed(() => {
  const step = tour.currentStep
  return !step?.target || step.placement === 'center'
})

const spotlightStyle = computed(() => {
  if (isCentered.value || !targetRect.value) return null
  const r = targetRect.value
  const pad = 6
  return {
    top: `${Math.max(0, r.top - pad)}px`,
    left: `${Math.max(0, r.left - pad)}px`,
    width: `${r.width + pad * 2}px`,
    height: `${r.height + pad * 2}px`,
  }
})

const cardStyle = computed(() => {
  if (isCentered.value || !targetRect.value) {
    return {
      top: '50%',
      left: '50%',
      transform: 'translate(-50%, -50%)',
      maxWidth: 'min(420px, calc(100vw - 32px))',
    }
  }

  const r = targetRect.value
  const placement = tour.currentStep?.placement || 'bottom'
  const margin = 14
  const cardWidth = 360
  const vw = window.innerWidth
  const vh = window.innerHeight

  let top = r.bottom + margin
  let left = r.left + r.width / 2 - cardWidth / 2

  if (placement === 'top') {
    top = r.top - margin
    return {
      top: `${top}px`,
      left: `${Math.max(12, Math.min(left, vw - cardWidth - 12))}px`,
      transform: 'translateY(-100%)',
      maxWidth: `${cardWidth}px`,
    }
  }
  if (placement === 'left') {
    return {
      top: `${Math.max(12, r.top)}px`,
      left: `${Math.max(12, r.left - cardWidth - margin)}px`,
      maxWidth: `${cardWidth}px`,
    }
  }
  if (placement === 'right') {
    return {
      top: `${Math.max(12, r.top)}px`,
      left: `${Math.min(vw - cardWidth - 12, r.right + margin)}px`,
      maxWidth: `${cardWidth}px`,
    }
  }

  // bottom (default)
  top = Math.min(vh - 180, r.bottom + margin)
  return {
    top: `${top}px`,
    left: `${Math.max(12, Math.min(left, vw - cardWidth - 12))}px`,
    maxWidth: `${cardWidth}px`,
  }
})

function measureTarget() {
  const step = tour.currentStep
  if (!step?.target || step.placement === 'center') {
    targetRect.value = null
    return
  }
  const el = document.querySelector(tourTargetSelector(step.target))
  if (!el) {
    targetRect.value = null
    return
  }
  targetRect.value = el.getBoundingClientRect()
}

function scheduleMeasure() {
  requestAnimationFrame(() => {
    measureTarget()
    requestAnimationFrame(measureTarget)
  })
}

watch(
  () => [tour.active, tour.stepIndex, tour.preparing] as const,
  ([active]) => {
    if (active) scheduleMeasure()
    else targetRect.value = null
  },
  { immediate: true },
)

function onLayoutChange() {
  if (tour.active) scheduleMeasure()
}

onMounted(() => {
  window.addEventListener('resize', onLayoutChange)
  window.addEventListener('scroll', onLayoutChange, true)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onLayoutChange)
  window.removeEventListener('scroll', onLayoutChange, true)
})
</script>

<style scoped>
.product-tour {
  position: fixed;
  inset: 0;
  z-index: 10050;
  pointer-events: auto;
}

.product-tour-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(10, 10, 22, 0.55);
}

.product-tour-spotlight {
  position: fixed;
  border-radius: var(--radius);
  box-shadow: 0 0 0 9999px rgba(10, 10, 22, 0.55);
  pointer-events: none;
  z-index: 1;
  transition: top 160ms var(--ease), left 160ms var(--ease), width 160ms var(--ease), height 160ms var(--ease);
}

.product-tour-card {
  position: fixed;
  z-index: 2;
  background: var(--bg-elev);
  color: var(--fg);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  padding: 16px 16px 14px;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.45);
  pointer-events: auto;
}

.product-tour-card--center {
  width: min(420px, calc(100vw - 32px));
}

.product-tour-progress {
  font-size: var(--text-xs);
  color: var(--fg3);
  margin-bottom: 6px;
}

.product-tour-title {
  margin: 0 0 8px;
  font-size: var(--text-lg);
  font-weight: 600;
  line-height: 1.25;
}

.product-tour-body {
  margin: 0 0 14px;
  font-size: var(--text-sm);
  color: var(--fg2);
  line-height: 1.45;
}

.product-tour-missing {
  margin: -6px 0 14px;
  padding: 8px 10px;
  font-size: var(--text-sm);
  color: var(--fg2);
  line-height: 1.45;
  background: color-mix(in srgb, var(--accent2) 8%, var(--bg-elev));
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent2);
  border-radius: var(--radius-sm);
}

.product-tour-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.product-tour-skip {
  background: none;
  border: none;
  color: var(--fg3);
  font-family: var(--font);
  font-size: var(--text-sm);
  cursor: pointer;
  padding: 4px 0;
}
.product-tour-skip:hover {
  color: var(--fg2);
}

.product-tour-nav {
  display: flex;
  gap: 8px;
}
</style>
