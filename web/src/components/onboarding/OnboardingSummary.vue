<script setup lang="ts">
import type { OnboardingSummaryItem } from '../../lib/onboarding'
defineProps<{ items: readonly OnboardingSummaryItem[]; loading: boolean }>()
const stateLabel = { ready: 'ready', deferred: 'deferred', attention: 'attention', unknown: 'unknown' } as const
function internalHref(href?: string): string | undefined { return href?.startsWith('/') && !href.startsWith('//') ? href : undefined }
</script>
<template>
  <section aria-label="Setup summary">
    <h2>What’s ready</h2><p v-if="loading" role="status">Checking setup…</p>
    <dl v-else><div v-for="item in items" :key="item.id" class="summary-row"><dt>{{ item.label }}</dt><dd><span>{{ item.value }}</span><span class="summary-state" :data-state="item.state">{{ stateLabel[item.state] }}</span><a v-if="internalHref(item.href)" :href="internalHref(item.href)">Review</a></dd></div></dl>
  </section>
</template>
<style scoped>
h2{font:600 16px var(--font);margin:0 0 12px}.summary-row{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-top:1px solid var(--border)}dt{color:var(--fg2)}dd{display:flex;gap:8px;flex-wrap:wrap;margin:0}.summary-state{font-size:12px;color:var(--fg2)}.summary-state[data-state="attention"]{color:var(--warning)}a{color:var(--accent)}
</style>
