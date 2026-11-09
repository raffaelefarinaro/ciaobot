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
          <span v-if="row.avg !== null" class="home-today-avg">avg {{ formatCount(row.avg) }}</span>
        </dd>
      </div>
    </dl>
    <p class="rail-note">Averages over the last 30 days.</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { fetchUsageStats, formatCount, type UsageToday } from '../lib/usageStats'
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

const rows = computed(() => {
  const t = today.value
  return [
    { key: 'chats_opened', label: 'Chats opened', value: t?.chats_opened ?? null, avg: t?.avg_chats_opened_30d ?? null },
    { key: 'messages_sent', label: 'Messages sent', value: t?.messages_sent ?? null, avg: t?.avg_messages_sent_30d ?? null },
    { key: 'chats_archived', label: 'Chats archived', value: t?.chats_archived ?? null, avg: null },
    { key: 'memories_saved', label: 'Memories saved', value: t?.memories_saved ?? null, avg: null },
    { key: 'projects_worked_on', label: 'Projects worked on', value: t?.projects_worked_on ?? null, avg: null },
    { key: 'tasks_completed', label: 'Tasks completed', value: t?.tasks_completed ?? null, avg: null },
  ]
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
