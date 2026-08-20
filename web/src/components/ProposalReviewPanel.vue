<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useProposalsStore } from '../stores/proposals'
import type { ProposalRow } from '../lib/types'

const store = useProposalsStore()

const kindFilter = ref('all')
const workspaceFilter = ref('all')
const selected = ref<Set<string>>(new Set())
const confirmLeakId = ref('')
const olderThanDays = ref(30)

const kinds = computed(() => ['all', ...new Set(store.rows.map(r => r.kind))])
const workspaces = computed(() => ['all', ...new Set(store.rows.map(r => r.workspace))])

const filtered = computed(() => store.rows.filter(r =>
  (kindFilter.value === 'all' || r.kind === kindFilter.value) &&
  (workspaceFilter.value === 'all' || r.workspace === workspaceFilter.value),
))

const allSelected = computed(() =>
  filtered.value.length > 0 && filtered.value.every(r => selected.value.has(r.id)),
)

function toggleAll() {
  const next = new Set(selected.value)
  if (allSelected.value) {
    filtered.value.forEach(r => next.delete(r.id))
  } else {
    filtered.value.forEach(r => next.add(r.id))
  }
  selected.value = next
}

function toggleRow(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

// Batch accept must never include skill rows: they are files, not bullets, and
// the server has no accept descriptor for them (accept_for('skill') raises).
const selectedNonSkill = computed(() => [...selected.value].filter(id => {
  const row = store.rows.find(r => r.id === id)
  return row && row.kind !== 'skill'
}))

function isRegionKind(row: ProposalRow): boolean {
  return row.kind === 'memory' || row.kind === 'profile' || row.kind === 'user'
}

function isRehome(row: ProposalRow): boolean {
  return row.kind === 'rehome'
}

function isSkill(row: ProposalRow): boolean {
  return row.kind === 'skill'
}

// How a rehome row is presented. Never a pre-filled one-click accept for a
// destination no tag backs: a single clean signal is a plain accept, multiple
// candidates are a picker, and no signal is a question.
function rehomeMode(row: ProposalRow): 'accept' | 'picker' | 'question' {
  const sig = row.rehome
  if (!sig) return 'question'
  if (sig.candidates.length > 1) return 'picker'
  if (sig.justified) return 'accept'
  return 'question'
}

function confirmAccept(row: ProposalRow) {
  // A region-kind row with a leak warning must be confirmed before the accept
  // is sent: accepting writes a region visible in every workspace.
  if (row.leak_warning) {
    confirmLeakId.value = row.id
    return
  }
  void store.act(row.id, 'accept')
}

function doAccept(row: ProposalRow) {
  confirmLeakId.value = ''
  void store.act(row.id, 'accept')
}

function cancelLeakConfirm() {
  confirmLeakId.value = ''
}

function doDismiss(row: ProposalRow) {
  void store.act(row.id, 'dismiss')
}

function batchAccept() {
  void store.batch(selectedNonSkill.value, 'accept')
}

function batchDismiss() {
  void store.batch([...selected.value], 'dismiss')
}

function dismissOlder() {
  const date = new Date()
  date.setDate(date.getDate() - olderThanDays.value)
  const iso = date.toISOString().slice(0, 10)
  void store.dismissOlderThan(iso)
}

onMounted(() => { void store.fetch() })
</script>

<template>
  <div class="proposal-review">
    <div class="proposal-review-header">
      <h2 class="proposal-review-title">Proposal review</h2>
      <p class="proposal-review-subtitle">
        {{ store.rows.length }} queued. Accepting a region row writes to a bounded
        guide region; rehome rows move a file; skill rows are files, dismiss only.
      </p>
    </div>

    <div v-if="store.error" class="proposal-review-error">{{ store.error }}</div>

    <div class="proposal-review-toolbar">
      <label class="proposal-filter">
        <span class="proposal-filter-label">kind</span>
        <select v-model="kindFilter" class="proposal-select">
          <option v-for="k in kinds" :key="k" :value="k">{{ k }}</option>
        </select>
      </label>
      <label class="proposal-filter">
        <span class="proposal-filter-label">workspace</span>
        <select v-model="workspaceFilter" class="proposal-select">
          <option v-for="w in workspaces" :key="w" :value="w">{{ w }}</option>
        </select>
      </label>
      <div class="proposal-batch">
        <label class="proposal-select-all">
          <input
            type="checkbox"
            :checked="allSelected"
            :disabled="!filtered.length"
            @change="toggleAll"
          />
          <span>select all</span>
        </label>
        <button
          type="button"
          class="btn-small btn-primary"
          :disabled="!selectedNonSkill.length || store.busy"
          @click="batchAccept"
        >
          accept selected ({{ selectedNonSkill.length }})
        </button>
        <button
          type="button"
          class="btn-small btn-chip"
          :disabled="!selected.size || store.busy"
          @click="batchDismiss"
        >
          dismiss selected ({{ selected.size }})
        </button>
      </div>
      <div class="proposal-older">
        <label class="proposal-filter">
          <span class="proposal-filter-label">dismiss older than</span>
          <input
            v-model.number="olderThanDays"
            type="number"
            min="1"
            max="3650"
            class="proposal-select proposal-days"
          />
          <span class="proposal-days-unit">days</span>
        </label>
        <button
          type="button"
          class="btn-small btn-chip"
          :disabled="store.busy"
          @click="dismissOlder"
        >
          dismiss
        </button>
      </div>
    </div>

    <div v-if="store.loading" class="proposal-loading">Loading proposals…</div>

    <div v-else-if="!filtered.length" class="proposal-empty">
      No proposals match this filter.
    </div>

    <ul v-else class="proposal-list">
      <li
        v-for="row in filtered"
        :key="row.id"
        class="proposal-row"
        :class="{
          'proposal-row--skill': isSkill(row),
          'proposal-row--leak': row.leak_warning,
        }"
      >
        <input
          type="checkbox"
          class="proposal-check"
          :checked="selected.has(row.id)"
          @change="toggleRow(row.id)"
        />
        <div class="proposal-body">
          <div class="proposal-meta">
            <span class="badge" :class="isSkill(row) ? '--accent2' : '--accent'">{{ row.kind }}</span>
            <span class="proposal-workspace">{{ row.workspace }}</span>
            <span v-if="isSkill(row)" class="badge --muted">file</span>
            <span v-if="row.leak_warning" class="badge --warn">leaks to every workspace</span>
          </div>
          <p class="proposal-text">{{ row.text }}</p>
          <p class="proposal-path">{{ row.path }}</p>

          <!-- Rehome: picker over multiple candidates -->
          <div v-if="isRehome(row) && rehomeMode(row) === 'picker'" class="proposal-rehome">
            <label class="proposal-filter">
              <span class="proposal-filter-label">move to</span>
              <select class="proposal-select">
                <option v-for="c in row.rehome!.candidates" :key="c" :value="c">{{ c }}</option>
              </select>
            </label>
            <button
              type="button"
              class="btn-small btn-primary"
              :disabled="store.busy"
              @click="doAccept(row)"
            >
              confirm
            </button>
            <button
              type="button"
              class="btn-small btn-chip"
              :disabled="store.busy"
              @click="doDismiss(row)"
            >
              dismiss
            </button>
          </div>

          <!-- Rehome: no signal, destination is a question -->
          <div v-else-if="isRehome(row) && rehomeMode(row) === 'question'" class="proposal-rehome">
            <span class="proposal-question">
              Move to {{ row.rehome?.destination || 'a destination' }}?
            </span>
            <button
              type="button"
              class="btn-small btn-primary"
              :disabled="store.busy"
              @click="doAccept(row)"
            >
              confirm
            </button>
            <button
              type="button"
              class="btn-small btn-chip"
              :disabled="store.busy"
              @click="doDismiss(row)"
            >
              dismiss
            </button>
          </div>

          <!-- Leak warning: confirm before accept -->
          <div v-else-if="row.leak_warning && confirmLeakId === row.id" class="proposal-leak-confirm">
            <p class="proposal-leak-warning">
              Accepting writes a region into the guide that is injected into every
              workspace session, not just {{ row.workspace }}. Confirm?
            </p>
            <button
              type="button"
              class="btn-small btn-primary"
              :disabled="store.busy"
              @click="doAccept(row)"
            >
              confirm accept
            </button>
            <button
              type="button"
              class="btn-small btn-chip"
              @click="cancelLeakConfirm"
            >
              cancel
            </button>
          </div>

          <div v-else class="proposal-actions">
            <button
              v-if="!isSkill(row)"
              type="button"
              class="btn-small btn-primary"
              :disabled="store.busy"
              @click="confirmAccept(row)"
            >
              accept
            </button>
            <button
              type="button"
              class="btn-small btn-chip"
              :disabled="store.busy"
              @click="doDismiss(row)"
            >
              dismiss
            </button>
          </div>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.proposal-review {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.proposal-review-header {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.proposal-review-title {
  margin: 0;
  font-size: var(--text-lg);
  color: var(--fg);
}

.proposal-review-subtitle {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}

.proposal-review-error {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--error);
  border-radius: var(--radius);
  color: var(--error);
  font-size: var(--text-sm);
}

.proposal-review-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.proposal-filter {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
}

.proposal-filter-label {
  font-size: var(--text-sm);
  color: var(--fg2);
}

.proposal-select {
  background: var(--bg2);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 4px 8px;
  font-size: var(--text-sm);
  min-height: 32px;
}

.proposal-days {
  width: 64px;
}

.proposal-days-unit {
  font-size: var(--text-sm);
  color: var(--fg2);
}

.proposal-batch,
.proposal-older {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
}

.proposal-select-all {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-sm);
  color: var(--fg2);
}

.proposal-loading,
.proposal-empty {
  padding: var(--space-4);
  text-align: center;
  color: var(--fg2);
  font-size: var(--text-sm);
}

.proposal-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.proposal-row {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  min-width: 0;
}

.proposal-row--skill {
  border-left: 3px solid var(--accent2);
}

.proposal-row--leak {
  border-left: 3px solid var(--warn);
}

.proposal-check {
  flex-shrink: 0;
  margin-top: 4px;
}

.proposal-body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.proposal-meta {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex-wrap: wrap;
}

.proposal-workspace {
  font-size: var(--text-sm);
  color: var(--fg2);
}

.proposal-text {
  margin: 0;
  font-size: var(--text-base);
  color: var(--fg);
}

.proposal-path {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--fg3);
  overflow-wrap: anywhere;
}

.proposal-rehome,
.proposal-leak-confirm,
.proposal-actions {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex-wrap: wrap;
  margin-top: var(--space-1);
}

.proposal-question {
  font-size: var(--text-sm);
  color: var(--warn);
}

.proposal-leak-warning {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--warn);
}
</style>
