<template>
  <Teleport to="body">
    <div
      v-if="tour.active && tour.currentStep"
      class="product-tour"
      role="dialog"
      aria-modal="true"
      :aria-label="tour.currentStep.title"
    >
      <div v-if="!spotlightStyle" class="product-tour-backdrop" @click.stop />

      <div
        v-if="spotlightStyle"
        class="product-tour-spotlight"
        :style="spotlightStyle"
        aria-hidden="true"
      />

      <div
        ref="cardEl"
        class="product-tour-card"
        :class="{ 'product-tour-card--center': isCentered }"
        :style="cardStyle"
        @click.stop
      >
        <div class="product-tour-progress">{{ tour.progressLabel }}</div>
        <h2 class="product-tour-title">{{ tour.currentStep.title }}</h2>
        <img
          v-if="tour.currentStep.image"
          class="product-tour-image"
          :src="tour.currentStep.image"
          :alt="tour.currentStep.imageAlt || ''"
          @load="measureCard"
        />
        <p class="product-tour-body">{{ tour.currentStep.body }}</p>
        <p v-if="showMissingHint" class="product-tour-missing">{{ tour.currentStep.missingHint }}</p>
        <button
          v-if="tour.currentStep.action"
          type="button"
          class="product-tour-tryit"
          :disabled="tour.preparing"
          @click="tryIt(tour.currentStep.action)"
        >{{ tour.currentStep.action.label }} →</button>
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
import { useRouter } from 'vue-router'
import { useProductTourStore } from '../stores/productTour'
import { useProjectStore } from '../stores/projects'
import { shouldShowMissingHint, tourTargetSelector } from '../lib/productTour'

const tour = useProductTourStore()
const projectStore = useProjectStore()
const router = useRouter()

// "Try it" hands control to the user on the real page; the tour has done its
// job and can be replayed from Settings → Home.
async function tryIt(action: { label: string; route: string }) {
  tour.finish()
  if (router.currentRoute.value.path !== action.route) {
    await router.push(action.route)
  }
}

const targetRect = ref<DOMRect | null>(null)
const cardEl = ref<HTMLElement | null>(null)
const cardSize = ref({ width: 360, height: 200 })

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
  const edge = 12
  const vw = window.innerWidth
  const vh = window.innerHeight

  const cardWidth = Math.min(360, vw - edge * 2)
  // Never let the assumed height exceed the viewport, so clamping stays valid.
  const cardHeight = Math.min(cardSize.value.height, vh - edge * 2)

  // Clamp helpers keep the whole card inside the viewport on small screens.
  const clampLeft = (value: number) =>
    Math.max(edge, Math.min(value, vw - cardWidth - edge))
  const clampTop = (value: number) =>
    Math.max(edge, Math.min(value, vh - cardHeight - edge))

  // Room available on each side of the target.
  const spaceAbove = r.top - margin - edge
  const spaceBelow = vh - r.bottom - margin - edge
  const spaceLeft = r.left - margin - edge
  const spaceRight = vw - r.right - margin - edge

  let top: number
  let left: number

  if (placement === 'left' || placement === 'right') {
    // Flip horizontally to whichever side actually fits the card.
    const wantLeft = placement === 'left'
    const useLeft = wantLeft
      ? spaceLeft >= cardWidth || spaceLeft >= spaceRight
      : !(spaceRight >= cardWidth || spaceRight >= spaceLeft)
    left = useLeft ? r.left - cardWidth - margin : r.right + margin
    top = r.top
  } else {
    // Vertical placement: flip to the side with more room when the
    // preferred side can't fit the whole card (e.g. a full-height target).
    const wantTop = placement === 'top'
    const useTop = wantTop
      ? spaceAbove >= cardHeight || spaceAbove >= spaceBelow
      : !(spaceBelow >= cardHeight || spaceBelow >= spaceAbove)
    top = useTop ? r.top - margin - cardHeight : r.bottom + margin
    left = r.left + r.width / 2 - cardWidth / 2
  }

  return {
    top: `${clampTop(top)}px`,
    left: `${clampLeft(left)}px`,
    width: `${cardWidth}px`,
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
  measureCard()
}

function measureCard() {
  const el = cardEl.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  if (rect.height > 0) cardSize.value = { width: rect.width, height: rect.height }
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
  max-height: calc(100vh - 24px);
  overflow-y: auto;
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

.product-tour-image {
  display: block;
  width: 100%;
  height: auto;
  margin: 0 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg);
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

.product-tour-tryit {
  display: block;
  width: 100%;
  min-height: var(--touch);
  margin: 0 0 12px;
  padding: 8px 12px;
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-elev));
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  color: var(--fg);
  font-family: var(--font);
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
  transition: background 120ms var(--ease);
}
.product-tour-tryit:hover {
  background: color-mix(in srgb, var(--accent) 18%, var(--bg-elev));
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
