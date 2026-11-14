<script setup lang="ts">
import { computed, ref } from 'vue'
import { filterInventory } from './inventory'
import type { Coverage, InventoryEntry, Migration } from './inventory'

const props = defineProps<{ entries: InventoryEntry[] }>()
const query = ref('')
const family = ref('all')
const coverage = ref<'all' | Coverage>('all')
const migration = ref<'all' | Migration>('all')
const families = computed(() => [...new Set(props.entries.map((entry) => entry.family))].sort())
const results = computed(() => filterInventory(props.entries, { query: query.value, family: family.value, coverage: coverage.value, migration: migration.value }))
const counts = computed(() => Object.fromEntries(['covered', 'uncovered', 'unknown'].map((status) => [status, props.entries.filter((entry) => entry.states.some((state) => state.coverage === status)).length])))
const coverageLabels: Record<Coverage, string> = { covered: 'Covered', uncovered: 'Uncovered', unknown: 'Unknown', 'not-applicable': 'Not applicable' }
function clear() { query.value = ''; family.value = 'all'; coverage.value = 'all'; migration.value = 'all' }
</script>

<template>
  <main class="sb-inventory-root">
    <h1>UI ownership inventory</h1>
    <p>Find production owners and their rendered-state stories. An inventory entry is not a migration approval.</p>
    <p role="status">{{ entries.length }} entries · {{ results.length }} shown</p>
    <p>{{ counts.covered }} with covered states · {{ counts.uncovered }} with uncovered states · {{ counts.unknown }} with unknown states. Categories overlap.</p>
    <div class="sb-inventory-filters">
      <label>Search <input v-model="query" type="search" placeholder="Name, owner, state or path"></label>
      <label>Family <select v-model="family"><option value="all">All</option><option v-for="item in families" :key="item" :value="item">{{ item }}</option></select></label>
      <label>Coverage <select v-model="coverage"><option value="all">All</option><option value="covered">Covered</option><option value="uncovered">Uncovered</option><option value="unknown">Unknown</option><option value="not-applicable">Not applicable</option></select></label>
      <label>Migration <select v-model="migration"><option value="all">All</option><option value="unassessed">Unassessed</option><option value="retained">Retained</option><option value="planned">Planned</option><option value="migrated">Migrated</option></select></label>
      <button class="btn-small" type="button" @click="clear">Clear filters</button>
    </div>
    <p v-if="!entries.length">The catalogue is empty.</p>
    <p v-else-if="!results.length" role="status">No matching entries. Clear filters to see the catalogue.</p>
    <div class="sb-inventory-list">
      <article v-for="entry in results" :key="entry.id" class="sb-inventory-entry">
        <h2>{{ entry.name }}</h2>
        <p>{{ entry.kind }} · {{ entry.family }} · <code>{{ entry.id }}</code></p>
        <p><strong>States:</strong> <span v-for="(state, index) in entry.states" :key="state.name">{{ index ? ' · ' : '' }}{{ state.name }}: {{ coverageLabels[state.coverage] }}</span></p>
        <details>
          <summary>Ownership and state details</summary>
          <dl>
            <dt>Owner / anchor</dt><dd><code>{{ entry.owner.path }} · {{ entry.owner.anchor }}</code></dd>
            <dt>Direct consumers</dt><dd><span v-if="!entry.consumers.length">None listed</span><ul v-else><li v-for="path in entry.consumers" :key="path"><code>{{ path }}</code></li></ul></dd>
            <dt>Support sources</dt><dd><span v-if="!entry.sources.length">None listed</span><ul v-else><li v-for="path in entry.sources" :key="path"><code>{{ path }}</code></li></ul></dd>
            <dt>Variants</dt><dd>{{ entry.variants.length ? entry.variants.join(', ') : 'Not assessed' }}. {{ entry.variantNotes }}</dd>
            <dt>Loading: {{ entry.loading.status }}</dt><dd>{{ entry.loading.detail }}</dd>
            <dt>Migration: {{ entry.migration.status }}</dt><dd>{{ entry.migration.note }}</dd>
            <template v-if="entry.rationale"><dt>Composition rationale</dt><dd>{{ entry.rationale }}</dd></template>
          </dl>
          <h3>State coverage</h3>
          <ul><li v-for="state in entry.states" :key="state.name"><strong>{{ state.name }} — {{ coverageLabels[state.coverage] }}</strong>: {{ state.note }} <a v-for="id in state.storyIds" :key="id" :href="`./?path=/story/${id}`" target="_top">{{ id }}</a></li></ul>
        </details>
      </article>
    </div>
  </main>
</template>

<style scoped>
.sb-inventory-root { color: var(--fg); font-family: var(--font); max-width: var(--page-max); margin: auto; display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space-3); min-width: 0; }
.sb-inventory-root h1 { font-size: var(--text-lg); }
.sb-inventory-filters { display: flex; flex-wrap: wrap; gap: var(--space-3); align-items: end; }
.sb-inventory-filters label { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space-1); flex: 1 1 12rem; min-width: 0; }
.sb-inventory-filters input, .sb-inventory-filters select, .sb-inventory-filters button, summary { min-height: var(--touch); max-width: 100%; }
.sb-inventory-filters input, .sb-inventory-filters select { min-width: 0; width: 100%; }
.sb-inventory-list { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space-3); min-width: 0; }
.sb-inventory-entry { min-width: 0; padding: var(--space-4); background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); overflow-wrap: anywhere; }
.sb-inventory-entry h2 { font-size: var(--text-lg); }
.sb-inventory-entry h3 { font-size: var(--text-base); margin-top: var(--space-3); }
.sb-inventory-entry p { margin-top: var(--space-2); }
.sb-inventory-entry details { margin-top: var(--space-2); }
.sb-inventory-entry summary { cursor: pointer; display: list-item; padding: var(--space-2) 0; }
.sb-inventory-entry dt { font-weight: 600; margin-top: var(--space-3); }
.sb-inventory-entry ul { padding-left: var(--space-5); }
.sb-inventory-entry li { margin-top: var(--space-1); }
.sb-inventory-entry a { margin-left: var(--space-2); }
@media (pointer: coarse) {
  .sb-inventory-entry a { display: inline-flex; align-items: center; min-height: var(--touch); }
}
</style>
