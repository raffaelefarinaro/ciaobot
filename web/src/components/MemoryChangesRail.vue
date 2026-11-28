<template>
  <DockedWindow class="changes-rail" title="This week" id="changes-rail-title">
    <p v-if="!feed" class="changes-rail-text">{{ changes.error ? 'Could not read the changes.' : 'Reading…' }}</p>
    <template v-else>
      <dl class="changes-rail-counts">
        <div v-for="row in rows" :key="row.label" class="changes-rail-kv">
          <dt>{{ row.label }}</dt>
          <dd class="changes-rail-value">{{ row.value.toLocaleString() }}</dd>
        </div>
      </dl>
      <p class="changes-rail-text">
        Counted from the undo log, skill suggestions and the chats read for memory since {{ since }}. Lines Ciaobot adds are not
        logged, so they are not counted.
      </p>
    </template>
  </DockedWindow>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { dayLabel, weekRows } from '../lib/changesFeed'
import { useChangesStore } from '../stores/changes'
import { useProjectStore } from '../stores/projects'
import DockedWindow from './DockedWindow.vue'

/** Memory · Changes' docked window: the last seven days' counts, from the same feed the page shows. */
const store = useProjectStore()
const changes = useChangesStore()
const feed = computed(() => (changes.feed && changes.feed.workspace === store.activeWorkspace ? changes.feed : null))
const rows = computed(() => (feed.value ? weekRows(feed.value.week) : []))
const since = computed(() => (feed.value ? dayLabel(feed.value.week.since) : ''))
</script>

<style scoped>
.changes-rail-counts { display: flex; flex-direction: column; margin: 0; }
.changes-rail-kv {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-height: 40px;
  border-bottom: 1px solid var(--border);
}
.changes-rail-kv dt { color: var(--fg); }
.changes-rail-kv dd { margin: 0; }
.changes-rail-value {
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.changes-rail-text { margin: 10px 0 0; color: var(--fg2); font-size: var(--text-sm); line-height: 1.5; }
</style>
