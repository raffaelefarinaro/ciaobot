<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import PaneHeader from './PaneHeader.vue'
import UsageBreakdown from './UsageBreakdown.vue'
import SkeletonLoader from './SkeletonLoader.vue'
import { errorMessage } from '../lib/errorMessage'
import { useProjectStore } from '../stores/projects'
import {
  STATS_RANGES,
  fetchUsageStats,
  formatCompact,
  formatCount,
  formatDelta,
  periodLabel,
  rangeLabel,
} from '../lib/usageStats'
import type { StatsRange, UsageCounts, UsageStats } from '../lib/usageStats'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const projects = useProjectStore()
const range = ref<StatsRange>(30)
const stats = ref<UsageStats | null>(null)
const loading = ref(false)
const error = ref('')
const answered = ref(false)
// Only the latest request may write, so a slow earlier answer cannot overwrite a newer range.
let requestId = 0

async function load() {
  const id = ++requestId
  loading.value = true
  error.value = ''
  try {
    const next = await fetchUsageStats(projects.activeWorkspace, range.value)
    if (id === requestId) stats.value = next
  } catch (e) {
    if (id === requestId) {
      stats.value = null
      error.value = errorMessage(e, 'The engine did not answer.')
    }
  } finally {
    if (id === requestId) { loading.value = false; answered.value = true }
  }
}

function selectRange(r: StatsRange) {
  if (r === range.value) return
  range.value = r
  stats.value = null
  void load()
}

onMounted(() => { void load() })
watch(() => projects.activeWorkspace, () => { stats.value = null; void load() })

// Until the first answer the page draws its shape instead of a grid of dashes.
// After that a new range or workspace keeps the grid and shows dashes until its
// numbers arrive, so the tiles do not flicker between shapes.
const firstLoad = computed(() => !answered.value)

const KPIS: Array<{ key: keyof UsageCounts; label: string; compact?: boolean }> = [
  { key: 'chats_opened', label: 'Chats opened' },
  { key: 'messages_sent', label: 'Messages sent' },
  { key: 'chats_archived', label: 'Chats archived' },
  { key: 'memories_saved', label: 'Memories saved' },
  { key: 'tasks_completed', label: 'Tasks completed' },
  { key: 'projects_worked_on', label: 'Projects worked on' },
  { key: 'automation_runs', label: 'Automation runs' },
  { key: 'tokens', label: 'Tokens processed', compact: true },
]

const kpis = computed(() => KPIS.map(k => {
  const current = stats.value?.totals[k.key] ?? null
  const previous = stats.value?.previous[k.key] ?? null
  const delta = formatDelta(current, previous)
  return {
    key: k.key,
    label: k.label,
    value: k.compact ? formatCompact(current) : formatCount(current),
    delta: delta ? `${delta} vs ${periodLabel(stats.value?.range_days ?? range.value)}` : '',
  }
}))
</script>

<template>
  <div class="usage-view">
    <PaneHeader page-tag="Usage" @open-sidebar="emit('open-sidebar')" />
    <div class="page-grid page-grid--single usage-grid">
      <div class="page-main" :aria-busy="loading ? 'true' : undefined">
        <p class="usage-lede">How you and your agents spent the time in this workspace.</p>
        <div class="usage-range" role="group" aria-label="Range">
          <button
            v-for="r in STATS_RANGES"
            :key="r"
            type="button"
            class="usage-range-btn"
            :aria-pressed="range === r ? 'true' : 'false'"
            @click="selectRange(r)"
          >{{ rangeLabel(r) }}</button>
        </div>
        <div v-if="error" class="usage-error" role="alert">
          <p>Could not load usage. {{ error }}</p>
          <button type="button" class="usage-retry" @click="load">Try again</button>
        </div>
        <SkeletonLoader v-if="firstLoad" label="Loading usage" variant="cards" :count="3" />
        <template v-else>
        <section class="usage-totals" aria-labelledby="usage-totals-title">
          <h3 id="usage-totals-title" class="sr-only">Totals</h3>
          <dl class="usage-kpis">
            <div v-for="kpi in kpis" :key="kpi.key" class="usage-kpi">
              <dt class="usage-kpi-label">{{ kpi.label }}</dt>
              <dd class="usage-kpi-value">{{ kpi.value }}</dd>
              <dd v-if="kpi.delta" class="usage-kpi-delta">{{ kpi.delta }}</dd>
            </div>
          </dl>
        </section>
        <UsageBreakdown :stats="stats" :range-days="stats?.range_days ?? range" />
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.usage-view { flex: 1; display: flex; flex-direction: column; overflow-y: auto; min-width: 0; }
.usage-grid { padding-top: var(--space-5); padding-bottom: var(--space-6); }
.usage-lede { margin: 0 0 var(--space-3); color: var(--fg2); font-size: var(--text-base); }
.usage-range { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 var(--space-5); }
.usage-range-btn { min-height: 30px; padding: 0 12px; border: 1px solid var(--border); border-radius: var(--radius-pill); background: var(--bg2); color: var(--fg2); font: inherit; font-size: var(--text-sm); cursor: pointer; transition: border-color 120ms var(--ease), background 120ms var(--ease), color 120ms var(--ease); }
.usage-range-btn:hover { color: var(--fg); border-color: var(--border-strong); }
.usage-range-btn[aria-pressed='true'] { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 16%, var(--bg2)); color: var(--fg); }
.usage-range-btn:focus-visible, .usage-retry:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.usage-error { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); margin: 0 0 var(--space-4); color: var(--fg); font-size: var(--text-sm); }
.usage-error p { margin: 0; }
.usage-retry { min-height: 30px; padding: 0 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: transparent; color: var(--fg); font: inherit; font-size: var(--text-sm); cursor: pointer; }
.usage-kpis { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg2); overflow: hidden; }
.usage-kpi { display: flex; flex-direction: column; gap: 2px; min-width: 0; margin: 0 -1px -1px 0; padding: 14px 16px; border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.usage-kpi-value { order: -1; margin: 0; color: var(--fg); font-size: calc(24px * var(--font-scale)); font-weight: 650; line-height: 1.15; font-variant-numeric: tabular-nums; }
.usage-kpi-label { color: var(--fg2); font-size: var(--text-sm); }
.usage-kpi-delta { margin: 0; color: var(--fg3); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
@container chat-pane (min-width: 720px) { .usage-kpis { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (pointer: coarse) { .usage-range-btn, .usage-retry { min-width: var(--touch); min-height: var(--touch); } }
@media (prefers-reduced-motion: reduce) { .usage-range-btn { transition: none; } }
</style>
