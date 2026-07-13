<template>
  <section
    v-if="visible"
    class="getting-started card"
    :class="{ 'getting-started--home': variant === 'home' }"
    aria-label="Getting started checklist"
  >
    <div class="gs-header">
      <div>
        <p class="section-title">Getting started</p>
        <p class="hint">Learn the app by doing — each step opens the page where it happens.</p>
      </div>
      <span class="gs-progress">{{ progress.doneCount }} / {{ progress.total }}</span>
    </div>

    <ul class="gs-list">
      <li v-for="item in progress.items" :key="item.id" class="gs-row" :class="{ 'is-done': item.done }">
        <span class="gs-mark" aria-hidden="true">{{ item.done ? '✓' : '○' }}</span>
        <div class="gs-main">
          <span class="gs-title">{{ item.title }}</span>
          <p class="gs-body">{{ item.body }}</p>
        </div>
        <button
          v-if="!item.done"
          type="button"
          class="btn-small gs-cta"
          @click="go(item)"
        >{{ item.cta }}</button>
      </li>
    </ul>

    <div class="gs-footer">
      <template v-if="variant === 'home'">
        <button type="button" class="gs-dismiss" @click="store.dismiss()">Hide checklist</button>
        <span class="hint">You can bring it back from Settings → Home.</span>
      </template>
      <template v-else>
        <button
          v-if="store.dismissed"
          type="button"
          class="btn-small"
          @click="store.restore()"
        >Show on home screen</button>
        <button
          v-else
          type="button"
          class="gs-dismiss"
          @click="store.dismiss()"
        >Hide from home screen</button>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useGettingStartedStore } from '../stores/gettingStarted'
import type { GettingStartedItemStatus } from '../lib/gettingStarted'

const props = withDefaults(defineProps<{
  /** 'home' hides itself when dismissed or complete; 'settings' always shows. */
  variant?: 'home' | 'settings'
}>(), { variant: 'home' })

const emit = defineEmits<{ 'open-sidebar': [] }>()

const store = useGettingStartedStore()
const router = useRouter()

const progress = computed(() => store.progress)

const visible = computed(() => {
  if (props.variant === 'settings') return true
  return !store.dismissed && !progress.value.allDone
})

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
.getting-started {
  text-align: left;
}

/* On the home empty state there is no surrounding .card styling context;
   give the checklist its own bounded panel. */
.getting-started--home {
  width: min(560px, 100%);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
}

.gs-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

.gs-progress {
  flex-shrink: 0;
  font-size: var(--text-xs);
  color: var(--fg3);
  font-variant-numeric: tabular-nums;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 2px 8px;
}

.gs-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}

.gs-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: 10px 0;
  border-top: 1px dashed var(--border);
}
.gs-row:first-child {
  border-top: none;
}

.gs-mark {
  flex-shrink: 0;
  width: 1.4em;
  text-align: center;
  color: var(--fg3);
  line-height: 1.4;
}
.gs-row.is-done .gs-mark {
  color: var(--success);
}

.gs-main {
  flex: 1;
  min-width: 0;
}

.gs-title {
  display: block;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
  line-height: 1.4;
}
.gs-row.is-done .gs-title {
  color: var(--fg3);
  text-decoration: line-through;
  text-decoration-color: var(--border-strong);
}

.gs-body {
  margin: 2px 0 0;
  font-size: var(--text-xs);
  color: var(--fg3);
  line-height: 1.45;
}
.gs-row.is-done .gs-body {
  display: none;
}

.gs-cta {
  flex-shrink: 0;
  align-self: center;
}

.gs-footer {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--border);
}

.gs-dismiss {
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
.gs-dismiss:hover {
  color: var(--fg);
  border-color: var(--fg2);
}
</style>
