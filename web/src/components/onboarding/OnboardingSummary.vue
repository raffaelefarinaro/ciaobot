<script setup lang="ts">
import type { OnboardingSummaryItem } from '../../lib/onboarding'
import OnboardingStack from './OnboardingStack.vue'
defineProps<{ items: readonly OnboardingSummaryItem[]; loading: boolean }>()
const stateLabel = { ready: 'ready', deferred: 'deferred', attention: 'attention', unknown: 'unknown' } as const
function internalHref(href?: string): string | undefined { return href?.startsWith('/') && !href.startsWith('//') ? href : undefined }
</script>

<template>
  <OnboardingStack as="section" aria-label="Setup summary">
    <p v-if="loading" role="status">Checking setup…</p>
    <ul v-else class="summary-list">
      <li v-for="item in items" :key="item.id" class="summary-row">
        <div class="summary-text">
          <span class="summary-label">{{ item.label }}</span>
          <span class="summary-value">{{ item.value }}</span>
        </div>
        <div class="summary-side">
          <span class="summary-state" :data-state="item.state">{{ stateLabel[item.state] }}</span>
          <a v-if="internalHref(item.href)" :href="internalHref(item.href)">Review</a>
        </div>
      </li>
    </ul>
  </OnboardingStack>
</template>

<style scoped>
.summary-list { display: grid; gap: var(--space-3, 12px); margin: 0; padding: 0; list-style: none; }
.summary-row { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-3, 12px); padding: var(--space-3, 12px) var(--space-4, 16px); border: 1px solid var(--border); border-radius: var(--radius-sm, 6px); background: var(--bg); }
.summary-text { display: grid; gap: var(--space-1, 4px); min-width: 0; flex: 1 1 220px; }
.summary-label { color: var(--fg2); }
.summary-value { overflow-wrap: anywhere; }
.summary-side { display: flex; align-items: center; gap: var(--space-3, 12px); }
.summary-state { color: var(--fg2); font-size: var(--text-sm, 12px); }
.summary-state[data-state="attention"] { color: var(--warning); }
a { display: inline-flex; align-items: center; min-height: var(--touch, 44px); color: var(--accent); }
</style>
