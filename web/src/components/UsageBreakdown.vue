<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  barPercent,
  busiestHour,
  formatCount,
  heatCellLabel,
  heatmapCells,
  heatmapMonths,
  modelShares,
  periodText,
  topProjects,
} from '../lib/usageStats'
import type { UsageStats } from '../lib/usageStats'

// Presentational only: UsageView owns the request and passes the normalized stats down.
const props = defineProps<{ stats: UsageStats | null; rangeDays: number }>()

const cells = computed(() => heatmapCells(props.stats?.daily_messages ?? []))
const weeks = computed(() => Math.ceil(cells.value.length / 7))
const months = computed(() => heatmapMonths(cells.value))
// Where the strip is wider than the card (a phone), open it on the latest weeks.
const heatScroll = ref<HTMLElement | null>(null)
watch(cells, async () => {
  await nextTick()
  const el = heatScroll.value
  if (el) el.scrollLeft = el.scrollWidth
}, { immediate: true })
const projects = computed(() => topProjects(props.stats?.projects ?? []))
const projectMax = computed(() => Math.max(0, ...projects.value.map(p => p.messages ?? 0)))
const pipeline = computed(() => [
  { key: 'proposed', label: 'Proposed', value: props.stats?.memory_pipeline.proposed ?? null },
  { key: 'accepted', label: 'Accepted', value: props.stats?.memory_pipeline.accepted ?? null },
  { key: 'notes_updated', label: 'Vault notes updated', value: props.stats?.memory_pipeline.notes_updated ?? null },
])
const hours = computed<Array<number | null>>(() => props.stats?.hours ?? Array.from({ length: 24 }, () => null))
const hourMax = computed(() => Math.max(0, ...hours.value.map(v => v ?? 0)))
const hoursLabel = computed(() => {
  const busiest = busiestHour(hours.value)
  return busiest === null
    ? 'Messages by hour of day, none in this period'
    : `Messages by hour of day, busiest at ${String(busiest).padStart(2, '0')}:00`
})
const models = computed(() => modelShares(props.stats?.models ?? []))
const period = computed(() => periodText(props.rangeDays))

function hourTitle(h: number, v: number | null): string {
  return `${String(h).padStart(2, '0')}:00 · ${formatCount(v)} messages`
}
</script>

<template>
  <div class="usage-breakdown">
    <section class="usage-card" aria-labelledby="usage-activity-title">
      <h3 id="usage-activity-title" class="usage-card-title">Activity</h3>
      <div v-if="cells.length" ref="heatScroll" class="usage-heat-scroll" tabindex="0" :style="{ '--usage-weeks': weeks }">
        <div class="usage-heat-months" aria-hidden="true">
          <span v-for="m in months" :key="m.column" :style="{ gridColumnStart: m.column }">{{ m.label }}</span>
        </div>
        <ol class="usage-heat" role="list" aria-label="Messages per day, last 52 weeks">
          <li
            v-for="(cell, i) in cells"
            :key="cell.date ?? `pad-${i}`"
            class="usage-heat-cell"
            :class="{ 'usage-heat-cell--pad': !cell.date }"
            :data-level="cell.level"
            :aria-hidden="cell.date ? undefined : 'true'"
            :aria-label="cell.date ? heatCellLabel(cell) : undefined"
            :title="cell.date ? heatCellLabel(cell) : undefined"
          />
        </ol>
      </div>
      <p v-else class="usage-caption">No activity recorded yet.</p>
      <p class="usage-legend" aria-hidden="true"><span class="usage-legend-scale">Less <span v-for="l in 5" :key="l" class="usage-heat-cell usage-legend-swatch" :data-level="l - 1" /> More</span> <span>· messages per day, last 52 weeks</span></p>
    </section>
    <div class="usage-pair">
      <section class="usage-card" aria-labelledby="usage-projects-title">
        <h3 id="usage-projects-title" class="usage-card-title">Projects</h3>
        <ul v-if="projects.length" class="usage-bars" role="list">
          <li v-for="p in projects" :key="p.project_id || p.name" class="usage-bar">
            <span class="usage-bar-name" :title="p.name">{{ p.name }}</span>
            <span class="usage-bar-track" aria-hidden="true"><span class="usage-bar-fill" :style="{ width: barPercent(p.messages, projectMax) }" /></span>
            <span class="usage-bar-value">{{ formatCount(p.messages) }}</span>
          </li>
        </ul>
        <p v-else class="usage-caption">No project activity in this period.</p>
        <p class="usage-caption">Messages sent, last {{ period }}.</p>
      </section>
      <section class="usage-card" aria-labelledby="usage-memory-title">
        <h3 id="usage-memory-title" class="usage-card-title">Memory pipeline</h3>
        <ol class="usage-pipeline" role="list">
          <li v-for="(step, i) in pipeline" :key="step.key" class="usage-pipeline-step" :style="{ marginRight: `${i * 14}%` }">
            <span>{{ step.label }}</span><strong>{{ formatCount(step.value) }}</strong>
          </li>
        </ol>
        <p class="usage-caption">Proposed by chats → accepted by you → written to the vault.</p>
      </section>
    </div>
    <div class="usage-pair">
      <section class="usage-card usage-card--fill" aria-labelledby="usage-hours-title">
        <h3 id="usage-hours-title" class="usage-card-title">When you work</h3>
        <div class="usage-hours" role="img" :aria-label="hoursLabel">
          <span v-for="(v, h) in hours" :key="h" class="usage-hour" :style="{ height: barPercent(v, hourMax) }" :title="hourTitle(h, v)" />
        </div>
        <div class="usage-hours-axis" aria-hidden="true"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div>
        <p class="usage-caption">Messages sent, last {{ period }}.</p>
      </section>
      <section class="usage-card" aria-labelledby="usage-models-title">
        <h3 id="usage-models-title" class="usage-card-title">Models used</h3>
        <ul v-if="models.length" class="usage-bars" role="list">
          <li v-for="m in models" :key="m.model" class="usage-bar">
            <span class="usage-bar-name" :title="m.model">{{ m.label }}</span>
            <span class="usage-bar-track" aria-hidden="true"><span class="usage-bar-fill" :style="{ width: `${m.percent}%` }" /></span>
            <span class="usage-bar-value">{{ m.percent }}%</span>
          </li>
        </ul>
        <p v-else class="usage-caption">No model turns in this period.</p>
        <p class="usage-caption">Share of turns, last {{ period }}.</p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.usage-breakdown {
  --usage-heat-0: var(--bg3);
  --usage-heat-1: color-mix(in srgb, var(--accent) 30%, var(--bg3));
  --usage-heat-2: color-mix(in srgb, var(--accent) 55%, var(--bg3));
  --usage-heat-3: color-mix(in srgb, var(--accent) 80%, var(--bg3));
  --usage-heat-4: var(--accent);
  display: flex; flex-direction: column; gap: 18px; margin-top: var(--space-5);
}
.usage-card { min-width: 0; padding: 14px 16px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg2); }
.usage-card-title { margin: 0 0 10px; color: var(--fg2); font-size: calc(13px * var(--font-scale)); font-weight: 600; }
.usage-pair { display: grid; grid-template-columns: minmax(0, 1fr); gap: 18px; }
@container chat-pane (min-width: 820px) { .usage-pair { grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); } }
/* The year strip spans the card: week columns share the width, each cell stays
   square, and below 10px a column the strip scrolls inside the card instead. */
.usage-heat-scroll { --usage-heat-gap: 3px; overflow-x: auto; padding-bottom: 4px; }
.usage-heat-scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
.usage-heat-months, .usage-heat { display: grid; grid-template-columns: repeat(var(--usage-weeks), minmax(10px, 1fr)); column-gap: var(--usage-heat-gap); }
.usage-heat-months { margin-bottom: 4px; color: var(--fg3); font-size: var(--text-xs); line-height: 1.2; }
.usage-heat-months span { grid-row: 1; white-space: nowrap; }
.usage-heat { grid-auto-flow: column; grid-template-rows: repeat(7, auto); row-gap: var(--usage-heat-gap); margin: 0; padding: 0; list-style: none; }
.usage-heat .usage-heat-cell { width: auto; height: auto; aspect-ratio: 1; }
.usage-heat-cell { display: inline-block; width: 12px; height: 12px; border-radius: 2px; background: var(--usage-heat-0); }
.usage-heat-cell[data-level='1'] { background: var(--usage-heat-1); }
.usage-heat-cell[data-level='2'] { background: var(--usage-heat-2); }
.usage-heat-cell[data-level='3'] { background: var(--usage-heat-3); }
.usage-heat-cell[data-level='4'] { background: var(--usage-heat-4); }
.usage-heat-cell--pad { visibility: hidden; }
.usage-legend { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin: 8px 0 0; color: var(--fg2); font-size: var(--text-sm); }
.usage-legend-swatch { width: 11px; height: 11px; }
.usage-legend-scale { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.usage-bars { display: flex; flex-direction: column; gap: 8px; margin: 0; padding: 0; list-style: none; }
.usage-bar { display: grid; grid-template-columns: minmax(0, 130px) minmax(0, 1fr) 48px; gap: 8px; align-items: center; font-size: var(--text-base); }
.usage-bar-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.usage-bar-track { height: 8px; border-radius: 4px; background: var(--usage-heat-0); overflow: hidden; }
.usage-bar-fill { display: block; height: 100%; border-radius: 4px; background: var(--accent); }
.usage-bar-value { text-align: right; color: var(--fg2); font-variant-numeric: tabular-nums; }
.usage-pipeline { display: flex; flex-direction: column; gap: 6px; margin: 0; padding: 0; list-style: none; }
.usage-pipeline-step { display: flex; justify-content: space-between; gap: var(--space-3); padding: 8px 10px; border-radius: var(--radius-sm); background: color-mix(in srgb, var(--accent) 12%, var(--bg2)); font-size: var(--text-base); }
.usage-pipeline-step strong { font-weight: 650; font-variant-numeric: tabular-nums; }
/* Beside a taller Models card the chart takes the spare height, not empty space under the caption. */
.usage-card--fill { display: flex; flex-direction: column; }
.usage-hours { display: flex; flex: 1 1 auto; align-items: flex-end; gap: 2px; min-height: 90px; }
.usage-hour { flex: 1; min-width: 0; border-radius: 2px 2px 0 0; background: var(--accent); opacity: 0.85; }
.usage-hours-axis { display: flex; justify-content: space-between; margin-top: 4px; color: var(--fg2); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
.usage-caption { margin: 10px 0 0; color: var(--fg3); font-size: var(--text-sm); }
</style>
