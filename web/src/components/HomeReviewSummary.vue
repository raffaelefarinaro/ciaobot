<template>
  <section v-if="items.length" class="home-review-summary" aria-labelledby="home-review-title">
    <div class="home-review-intro">
      <p class="home-review-eyebrow">memory pulse</p>
      <h2 id="home-review-title">What changed</h2>
      <p>The useful outcome of your work, not a second inbox.</p>
    </div>

    <div class="home-review-list">
      <button
        v-for="item in items"
        :key="item.key"
        type="button"
        class="home-review-item"
        :class="{
          'home-review-item--attention': item.state === 'error',
          'home-review-item--stale': item.state === 'stale',
          'home-review-item--loading': item.state === 'loading',
        }"
        @click="openItem(item.key)"
      >
        <span class="home-review-icon" aria-hidden="true">
          <svg v-if="item.key === 'memory'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M5 4h14v16H5z" /><path d="M8 8h8M8 12h8M8 16h5" />
          </svg>
          <svg v-else-if="item.key === 'retirement'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M5 4h10a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z" /><path d="M8 20V7a3 3 0 0 1 3-3M11 8h4M11 12h4" />
          </svg>
          <svg v-else width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="8" /><path d="M12 7v5l3 2" />
          </svg>
        </span>
        <span class="home-review-copy">
          <span class="home-review-title">{{ item.title }}</span>
          <span class="home-review-detail">{{ item.detail }}</span>
        </span>
        <span class="home-review-action">{{ item.action }}</span>
      </button>
    </div>

    <button type="button" class="home-review-link" @click="openMemory">
      Open Memory
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M5 12h13M13 6l6 6-6 6" />
      </svg>
    </button>
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
  title: string
  detail: string
  action: string
  state: ReviewState
}

const router = useRouter()
const projects = useProjectStore()
const proposals = useProposalsStore()
const retirement = useVaultReviewStore()
const tasks = useTaskStore()
const memory = useMemoryMapStore()

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`
}

// The row's trailing word: what clicking does when the count is trustworthy,
// otherwise the state that makes it untrustworthy.
function actionFor(state: ReviewState, verb: string): string {
  if (state === 'loading') return 'checking'
  if (state === 'stale') return 'stale'
  if (state === 'error') return 'error'
  return verb
}

const items = computed<ReviewItem[]>(() => {
  const workspace = projects.activeWorkspace
  const proposalCount = proposals.scopedRows(workspace).length
  let proposalState: ReviewState = 'ready'
  let proposalDetail = proposalCount
    ? `${proposalCount === 1 ? 'A durable fact is' : 'Durable facts are'} ready to accept, edit, or reject.`
    : 'Up to date. No suggested memories are waiting in this workspace.'
  if (!proposals.loaded && !proposals.loadError) {
    proposalState = 'loading'
    proposalDetail = `Checking ${workspace} for suggested memories.`
  } else if (proposals.loadError) {
    proposalState = proposalCount ? 'stale' : 'error'
    proposalDetail = proposalCount
      ? `Showing the last successful load. ${proposals.loadError}`
      : proposals.loadError
  }

  const hasCurrentRetirement = retirement.loadedWorkspace === workspace
  const retirementCount = hasCurrentRetirement ? retirement.candidates.length : 0
  let retirementState: ReviewState = 'ready'
  let retirementDetail = retirementCount
    ? `${retirementCount === 1 ? 'A note needs' : 'Notes need'} a freshness or duplication decision.`
    : 'Up to date. No notes need a memory decision in this workspace.'
  if (!hasCurrentRetirement && !retirement.loadError) {
    retirementState = 'loading'
    retirementDetail = `Checking ${workspace} for notes that need review.`
  } else if (retirement.loadError) {
    retirementState = hasCurrentRetirement && retirementCount ? 'stale' : 'error'
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
    .filter(schedule => Boolean(schedule.next_run))
    .sort((a, b) => String(a.next_run).localeCompare(String(b.next_run)))[0]
  let automationState: ReviewState = 'ready'
  let automationDetail = active.length
    ? next
      ? next.title
        ? `${next.title} · next ${formatRun(String(next.next_run))}`
        : `Next run ${formatRun(String(next.next_run))}`
      : 'Active · next run pending'
    : 'Up to date. No active automations belong to this workspace.'
  if (!tasks.schedulesLoaded && !tasks.scheduleLoadError) {
    automationState = 'loading'
    automationDetail = `Checking ${workspace} for active automations.`
  } else if (tasks.scheduleLoadError) {
    automationState = tasks.schedules.length ? 'stale' : 'error'
    automationDetail = tasks.schedules.length
      ? `Showing the last successful load. ${tasks.scheduleLoadError}`
      : tasks.scheduleLoadError
  }

  return [
    {
      key: 'memory',
      title: proposalCount && proposalState !== 'loading'
        ? plural(proposalCount, 'memory proposal')
        : 'Memory proposals',
      detail: proposalDetail,
      action: actionFor(proposalState, 'review'),
      state: proposalState,
    },
    {
      key: 'retirement',
      title: retirementCount && retirementState !== 'loading'
        ? plural(retirementCount, 'note to revisit', 'notes to revisit')
        : 'Notes to revisit',
      detail: retirementDetail,
      action: actionFor(retirementState, 'open'),
      state: retirementState,
    },
    {
      key: 'automations',
      title: active.length && automationState !== 'loading'
        ? plural(active.length, 'active automation')
        : 'Automations',
      detail: automationDetail,
      action: actionFor(automationState, 'view'),
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

function openMemory() {
  void router.push('/memory')
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
  width: 100%;
  min-width: 0;
  padding-top: 10px;
}

.home-review-intro {
  margin-bottom: 18px;
}

.home-review-eyebrow {
  margin: 0;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.2;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.home-review-intro h2 {
  margin: 6px 0 4px;
  color: var(--fg);
  font-size: calc(19px * var(--font-scale));
  letter-spacing: -0.03em;
}

.home-review-intro p:last-child {
  margin: 0;
  color: var(--fg2);
  font-size: calc(13px * var(--font-scale));
  line-height: 1.45;
}

/* Pulse rows, not cards: a hairline list reads as "what changed", where a
   stack of bordered tiles read as a second inbox to clear. */
.home-review-list {
  border-top: 1px solid var(--border);
}

.home-review-item {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  width: 100%;
  min-height: var(--touch);
  padding: 15px 0;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.home-review-item:hover .home-review-title {
  color: var(--accent);
}

.home-review-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}

.home-review-icon {
  display: grid;
  place-items: center;
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--bg2);
  color: var(--accent);
}

.home-review-copy {
  flex: 1;
  min-width: 0;
}

.home-review-title {
  display: block;
  margin-bottom: 3px;
  font-size: calc(13px * var(--font-scale));
  font-weight: 650;
  transition: color 120ms var(--ease);
}

.home-review-detail {
  display: block;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.home-review-action {
  flex: none;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 28px;
}

.home-review-item--stale .home-review-action {
  color: var(--accent);
}

.home-review-item--attention .home-review-icon,
.home-review-item--attention .home-review-action {
  color: var(--warning);
}

.home-review-item--loading .home-review-title {
  color: var(--fg2);
}

.home-review-link {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: var(--touch);
  margin-top: 4px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
}

.home-review-link:hover {
  text-decoration: underline;
  text-underline-offset: 3px;
}

/* Under the workbench's single-column break the rail sits below the request
   column with room to spare, so the rows lay out as three short tiles. */
@container chat-pane (max-width: 940px) and (min-width: 701px) {
  .home-review-summary {
    padding-top: 0;
  }

  .home-review-list {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
    border-top: 0;
  }

  .home-review-item {
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: 9px;
  }
}

@container chat-pane (max-width: 700px) {
  .home-review-summary {
    padding-top: 0;
  }

  .home-review-item {
    padding: 13px 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-review-title {
    transition: none;
  }
}
</style>
