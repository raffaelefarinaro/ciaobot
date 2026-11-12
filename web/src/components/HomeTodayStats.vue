<template>
  <section class="home-today" aria-labelledby="home-today-title">
    <div class="home-today-head">
      <h3 id="home-today-title" class="rail-title">Today</h3>
      <router-link to="/stats" class="home-today-link">Usage</router-link>
    </div>
    <dl class="rail-kvs home-today-list">
      <div v-for="row in rows" :key="row.key" class="rail-kv">
        <dt>{{ row.label }}</dt>
        <dd>
          <strong>{{ formatCount(row.value) }}</strong>
          <span v-if="row.avg !== null" class="home-today-avg">avg {{ formatAverage(row.avg) }}</span>
        </dd>
      </div>
    </dl>
    <p class="rail-note">Averages over the last 30 days.</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { fetchUsageStats, formatAverage, formatCount, type UsageToday } from '../lib/usageStats'
import { useProjectStore } from '../stores/projects'

const projects = useProjectStore()
const today = ref<UsageToday | null>(null)
let requestId = 0
async function load() {
  const id = ++requestId
  today.value = null
  try {
    const s = await fetchUsageStats(projects.activeWorkspace, 30)
    if (id === requestId) today.value = s.today
  } catch {
    if (id === requestId) today.value = null
  }
}
onMounted(load)
watch(() => projects.activeWorkspace, load)

const ROWS = [
  ['chats_opened', 'Chats opened'],
  ['messages_sent', 'Messages sent'],
  ['chats_archived', 'Chats archived'],
  ['memories_saved', 'Memories saved'],
  ['projects_worked_on', 'Projects worked on'],
  ['tasks_completed', 'Tasks completed'],
] as const

const rows = computed(() => {
  const t = today.value
  return ROWS.map(([key, label]) => ({ key, label, value: t?.[key] ?? null, avg: t?.[`avg_${key}_30d`] ?? null }))
})
</script>

<style scoped>
.home-today { margin-top: var(--space-5); }
.home-today-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin-bottom: 10px;
}
.home-today-head .rail-title { margin: 0; }
.home-today-link {
  color: var(--fg2);
  font-size: var(--text-sm);
  font-weight: 500;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
.home-today-link:hover { color: var(--accent); text-decoration-color: currentColor; }
.home-today-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}
.home-today-list { margin: 0; }
.home-today-list dt { font-weight: 400; }
.home-today-list dd {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin: 0;
}
.home-today-list strong { font-variant-numeric: tabular-nums; }
.home-today-avg {
  order: -1;
  color: var(--fg3);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
@media (pointer: coarse) {
  .home-today-link {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    min-width: var(--touch);
    min-height: var(--touch);
  }
}
</style>
