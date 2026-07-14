<template>
  <section
    v-if="visible"
    class="onboarding card"
    :class="{ 'onboarding--home': variant === 'home' }"
    aria-label="Onboarding"
  >
    <div class="onboarding-header">
      <div>
        <p class="section-title">{{ variant === 'home' ? 'getting started' : 'onboarding' }}</p>
        <p class="hint">
          <template v-if="variant === 'home'">
            Learn the app by doing — each step opens the page where it happens.
          </template>
          <template v-else>
            Take the guided tour, then complete hands-on setup steps.
          </template>
        </p>
      </div>
      <span v-if="showChecklist" class="onboarding-progress">{{ progress.doneCount }} / {{ progress.total }}</span>
    </div>

    <div v-if="variant === 'settings'" class="onboarding-tour">
      <div class="onboarding-subsection">
        <p class="onboarding-subtitle">Guided tour</p>
        <p class="hint">
          Walk through workspaces, chat comments, inline file previews, pinning, and rich document viewing.
        </p>
      </div>
      <div class="onboarding-tour-action">
        <button type="button" class="btn-secondary" @click="replayProductTour">Replay tour</button>
      </div>
    </div>

    <ul v-if="showChecklist" class="onboarding-list">
      <li
        v-for="item in progress.items"
        :key="item.id"
        class="onboarding-row"
        :class="{ 'is-done': item.done }"
      >
        <span class="onboarding-mark" aria-hidden="true">{{ item.done ? '✓' : '○' }}</span>
        <div class="onboarding-main">
          <span class="onboarding-title">{{ item.title }}</span>
          <p class="onboarding-body">{{ item.body }}</p>
        </div>
        <button
          v-if="!item.done"
          type="button"
          class="btn-small onboarding-cta"
          @click="go(item)"
        >{{ item.cta }}</button>
      </li>
    </ul>

    <div v-if="showFooter" class="onboarding-footer">
      <template v-if="variant === 'home'">
        <button type="button" class="onboarding-dismiss" @click="store.dismiss()">Hide checklist</button>
        <span class="hint">You can bring it back from Settings → Home.</span>
      </template>
      <template v-else-if="showChecklist">
        <button
          v-if="store.dismissed"
          type="button"
          class="btn-small"
          @click="store.restore()"
        >Show on home screen</button>
        <button
          v-else
          type="button"
          class="onboarding-dismiss"
          @click="store.dismiss()"
        >Hide from home screen</button>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { isOnboardingFinished } from '../lib/onboarding'
import type { GettingStartedItemStatus } from '../lib/gettingStarted'
import { useGettingStartedStore } from '../stores/gettingStarted'
import { useProductTourStore } from '../stores/productTour'

const props = withDefaults(defineProps<{
  /** 'home' shows the checklist only; 'settings' adds the tour and hides when finished. */
  variant?: 'home' | 'settings'
}>(), { variant: 'home' })

const emit = defineEmits<{ 'open-sidebar': [] }>()

const store = useGettingStartedStore()
const productTour = useProductTourStore()
const router = useRouter()

const progress = computed(() => store.progress)

const showChecklist = computed(() => !progress.value.allDone)

const visible = computed(() => {
  if (props.variant === 'home') {
    return !store.dismissed && showChecklist.value
  }
  return !isOnboardingFinished(progress.value, productTour.isTourCompleted())
})

const showFooter = computed(() => {
  if (props.variant === 'home') return true
  return showChecklist.value
})

async function replayProductTour() {
  if (router.currentRoute.value.path !== '/') {
    await router.push('/')
  }
  await productTour.replay()
}

async function go(item: GettingStartedItemStatus) {
  if (item.opensSidebar) emit('open-sidebar')
  if (router.currentRoute.value.path !== item.route) {
    await router.push(item.route)
  }
}

onMounted(() => {
  if (!store.providerStatusLoaded) void store.fetchProviderStatus()
})
</script>

<style scoped>
.onboarding {
  text-align: left;
}

.onboarding--home {
  width: min(560px, 100%);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
}

.onboarding-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

.onboarding-progress {
  flex-shrink: 0;
  font-size: var(--text-xs);
  color: var(--fg3);
  font-variant-numeric: tabular-nums;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 2px 8px;
}

.onboarding-tour {
  padding-bottom: var(--space-3);
  margin-bottom: var(--space-3);
  border-bottom: 1px dashed var(--border);
}

.onboarding-subsection {
  margin-bottom: var(--space-2);
}

.onboarding-subtitle {
  margin: 0 0 4px;
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--fg2);
}

.onboarding-tour-action {
  display: flex;
  align-items: center;
}

.onboarding-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}

.onboarding-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: 10px 0;
  border-top: 1px dashed var(--border);
}
.onboarding-row:first-child {
  border-top: none;
}

.onboarding-mark {
  flex-shrink: 0;
  width: 1.4em;
  text-align: center;
  color: var(--fg3);
  line-height: 1.4;
}
.onboarding-row.is-done .onboarding-mark {
  color: var(--success);
}

.onboarding-main {
  flex: 1;
  min-width: 0;
}

.onboarding-title {
  display: block;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
  line-height: 1.4;
}
.onboarding-row.is-done .onboarding-title {
  color: var(--fg3);
  text-decoration: line-through;
  text-decoration-color: var(--border-strong);
}

.onboarding-body {
  margin: 2px 0 0;
  font-size: var(--text-xs);
  color: var(--fg3);
  line-height: 1.45;
}
.onboarding-row.is-done .onboarding-body {
  display: none;
}

.onboarding-cta {
  flex-shrink: 0;
  align-self: center;
}

.onboarding-footer {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--border);
}

.onboarding-dismiss {
  min-height: var(--touch);
  padding: 6px 10px;
  background: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-xs);
  cursor: pointer;
}
.onboarding-dismiss:hover {
  color: var(--fg);
  border-color: var(--fg2);
}
</style>
