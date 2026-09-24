<template>
  <section v-if="items.length" class="home-review-summary" aria-labelledby="home-review-title">
    <div class="home-review-heading">
      <div>
        <span>What changed</span>
        <h2 id="home-review-title">Review before you start again</h2>
      </div>
      <p>Durable knowledge and background work stay visible here instead of hiding behind their destination pages.</p>
    </div>

    <div class="home-review-grid">
      <button
        v-for="item in items"
        :key="item.key"
        type="button"
        class="home-review-item"
        :class="{
          'home-review-item--attention': item.state === 'error',
          'home-review-item--stale': item.state === 'stale',
        }"
        @click="openItem(item.key)"
      >
        <span class="home-review-label">{{ item.label }}</span>
        <strong>{{ item.value }}</strong>
        <span class="home-review-detail">{{ item.detail }}</span>
        <span class="home-review-action" aria-hidden="true">Review →</span>
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useProposalsStore } from '../stores/proposals'
import { useVaultReviewStore } from '../stores/vaultReview'
import { useTaskStore } from '../stores/tasks'
import { useMemoryMapStore } from '../stores/memoryMap'
import { scheduleInWorkspace } from '../lib/automationWorkspace'

type ReviewState = 'ready' | 'loading' | 'stale' | 'error'
type ReviewItem = {
  key: 'memory' | 'retirement' | 'automations'
  label: string
  value: string
  detail: string
  state: ReviewState
}

const router = useRouter()
const projects = useProjectStore()
const proposals = useProposalsStore()
const retirement = useVaultReviewStore()
const tasks = useTaskStore()
const memory = useMemoryMapStore()

const items = computed<ReviewItem[]>(() => {
  const workspace = projects.activeWorkspace
  const proposalCount = proposals.scopedRows(workspace).length
  let proposalState: ReviewState = 'ready'
  let proposalValue = proposalCount ? String(proposalCount) : 'Up to date'
  let proposalDetail = proposalCount
    ? 'Suggested durable facts are ready to accept, edit, or reject.'
    : 'No suggested memories are waiting in this workspace.'
  if (!proposals.loaded && !proposals.loadError) {
    proposalState = 'loading'
    proposalValue = 'Checking…'
    proposalDetail = `Checking ${workspace} for suggested memories.`
  } else if (proposals.loadError) {
    proposalState = proposalCount ? 'stale' : 'error'
    proposalValue = proposalCount ? String(proposalCount) : 'Stale'
    proposalDetail = proposalCount
      ? `Showing the last successful load. ${proposals.loadError}`
      : proposals.loadError
  }

  const hasCurrentRetirement = retirement.loadedWorkspace === workspace
  const retirementCount = hasCurrentRetirement ? retirement.candidates.length : 0
  let retirementState: ReviewState = 'ready'
  let retirementValue = retirementCount ? String(retirementCount) : 'Up to date'
  let retirementDetail = retirementCount
    ? 'Notes need a freshness or duplication decision.'
    : 'No notes need a memory decision in this workspace.'
  if (!hasCurrentRetirement && !retirement.loadError) {
    retirementState = 'loading'
    retirementValue = 'Checking…'
    retirementDetail = `Checking ${workspace} for notes that need review.`
  } else if (retirement.loadError) {
    retirementState = hasCurrentRetirement && retirementCount ? 'stale' : 'error'
    retirementValue = retirementCount ? String(retirementCount) : 'Stale'
    retirementDetail = hasCurrentRetirement && retirementCount
      ? `Showing the last successful load. ${retirement.loadError}`
      : retirement.loadError
  }

  const active = tasks.schedules.filter(schedule => (
    schedule.enabled && scheduleInWorkspace(
      schedule,
      workspace,
      projects.chats,
      projects.projects,
    )
  ))
  const next = active
    .map(schedule => schedule.next_run)
    .filter((value): value is string => Boolean(value))
    .sort()[0]
  let automationState: ReviewState = 'ready'
  let automationValue = active.length ? String(active.length) : 'Up to date'
  let automationDetail = active.length
    ? `Active · next ${next ? formatRun(next) : 'run pending'}`
    : 'No active automations belong to this workspace.'
  if (!tasks.schedulesLoaded && !tasks.scheduleLoadError) {
    automationState = 'loading'
    automationValue = 'Checking…'
    automationDetail = `Checking ${workspace} for active automations.`
  } else if (tasks.scheduleLoadError) {
    automationState = tasks.schedules.length ? 'stale' : 'error'
    automationValue = active.length ? String(active.length) : 'Stale'
    automationDetail = tasks.schedules.length
      ? `Showing the last successful load. ${tasks.scheduleLoadError}`
      : tasks.scheduleLoadError
  }

  return [
    {
      key: 'memory',
      label: 'Memory proposals',
      value: proposalValue,
      detail: proposalDetail,
      state: proposalState,
    },
    {
      key: 'retirement',
      label: 'Knowledge upkeep',
      value: retirementValue,
      detail: retirementDetail,
      state: retirementState,
    },
    {
      key: 'automations',
      label: 'Automations',
      value: automationValue,
      detail: automationDetail,
      state: automationState,
    },
  ]
})

watch(
  () => projects.activeWorkspace,
  (workspace) => {
    if (!workspace) return
    void proposals.ensureLoaded()
    void retirement.ensureLoaded(workspace)
  },
  { immediate: true },
)

function formatRun(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, {
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function openItem(key: ReviewItem['key']) {
  if (key === 'automations') {
    void router.push('/schedules')
    return
  }
  memory.view = 'review'
  memory.reviewTab = key === 'retirement' ? 'retirement' : 'proposals'
  if (key === 'retirement') memory.retirementTab = 'candidates'
  void router.push('/proposals')
}
</script>

<style scoped>
.home-review-summary {
  width: min(100%, 920px);
  margin: 0 auto var(--space-5);
}

.home-review-heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-3);
}

.home-review-heading span {
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 650;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.home-review-heading h2 {
  margin: var(--space-1) 0 0;
  color: var(--fg);
  font-size: var(--text-lg);
  letter-spacing: -0.02em;
}

.home-review-heading p {
  max-width: 48ch;
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
  text-align: right;
}

.home-review-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-2);
}

.home-review-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  grid-template-areas:
    "label action"
    "value action"
    "detail detail";
  align-items: start;
  gap: var(--space-1) var(--space-3);
  min-height: 116px;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 150ms var(--ease), background 150ms var(--ease), transform 150ms var(--ease);
}

.home-review-item:hover {
  border-color: var(--border-strong);
  background: var(--bg3);
  transform: translateY(-1px);
}

.home-review-item--attention {
  border-color: color-mix(in srgb, var(--warning) 44%, var(--border));
}

.home-review-item--stale {
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
}

.home-review-label {
  grid-area: label;
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 650;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.home-review-item strong {
  grid-area: value;
  color: var(--fg);
  font-size: var(--text-lg);
  line-height: 1.1;
  letter-spacing: -0.02em;
}

.home-review-item--attention strong {
  color: var(--warning);
}

.home-review-detail {
  grid-area: detail;
  align-self: end;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.4;
}

.home-review-action {
  grid-area: action;
  align-self: center;
  color: var(--accent);
  font-size: var(--text-sm);
  font-weight: 650;
}

@media (max-width: 720px) {
  .home-review-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: var(--space-2);
  }

  .home-review-heading p {
    text-align: left;
  }

  .home-review-grid {
    grid-template-columns: 1fr;
  }

  .home-review-item {
    min-height: 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-review-item {
    transition: none;
  }
}
</style>
