<template>
  <section class="home-review-summary" aria-labelledby="home-review-title">
    <h2 id="home-review-title">What changed</h2>

    <!-- Only what needs a look: an up-to-date queue is not news, so it drops
         out instead of holding a row that says "nothing here". -->
    <div v-if="visibleItems.length" class="home-review-list">
      <button
        v-for="item in visibleItems"
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
        <span class="home-review-title">{{ item.title }}</span>
        <span class="home-review-detail">{{ item.detail }}</span>
      </button>
    </div>
    <p v-else class="home-review-clear">Nothing to review.</p>

    <button type="button" class="home-review-link" @click="openMemory">
      Open Memory
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M5 12h13M13 6l6 6-6 6" />
      </svg>
    </button>
    <!-- The pass itself, which is a chat — not the /memory view above. Only
         shown while one is open: a pass that ended cleanly auto-archives, and
         an affordance with nothing to open is a dead row. -->
    <button v-if="latestPass" type="button" class="home-review-link" @click="openMemoryPass">
      Open memory pass
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
import { memorySectionPath } from '../stores/memoryMap'
import { scheduleInWorkspace } from '../lib/automationWorkspace'

type ReviewState = 'ready' | 'loading' | 'stale' | 'error'
type ReviewItem = {
  key: 'memory' | 'retirement' | 'automations'
  title: string
  detail: string
  count: number
  state: ReviewState
}

const router = useRouter()
const projects = useProjectStore()
const proposals = useProposalsStore()
const retirement = useVaultReviewStore()
const tasks = useTaskStore()

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`
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
    proposalDetail = 'Checking for suggested memories.'
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
    retirementDetail = 'Checking for notes that need review.'
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
    automationDetail = 'Checking for active automations.'
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
      count: proposalCount,
      state: proposalState,
    },
    {
      key: 'retirement',
      title: retirementCount && retirementState !== 'loading'
        ? plural(retirementCount, 'note to revisit', 'notes to revisit')
        : 'Notes to revisit',
      detail: retirementDetail,
      count: retirementCount,
      state: retirementState,
    },
    {
      key: 'automations',
      title: active.length && automationState !== 'loading'
        ? plural(active.length, 'active automation')
        : 'Automations',
      detail: automationDetail,
      count: active.length,
      state: automationState,
    },
  ]
})

const visibleItems = computed(() => items.value.filter(
  item => item.state !== 'ready' || item.count > 0,
))

// The newest pass still open in this workspace. The Memory project is hidden
// from the sidebar, so this rail is the way in when a pass is queued, running,
// or waiting on the owner.
const latestPass = computed(() => projects.latestMemoryPassChat(projects.activeWorkspace))

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

function openMemoryPass() {
  if (latestPass.value) void projects.switchChat(latestPass.value.chat_id)
}

function openItem(key: ReviewItem['key']) {
  if (key === 'automations') {
    void router.push('/schedules')
    return
  }
  void router.push(memorySectionPath(key === 'retirement' ? 'revisit' : 'suggested'))
}
</script>

<style scoped>
.home-review-summary {
  width: 100%;
  min-width: 0;
}

.home-review-summary h2 {
  margin: 0 0 12px;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
}

/* Hairline rows, not cards: "what changed" reads as a short list, where a
   stack of bordered tiles read as a second inbox to clear. */
.home-review-list {
  border-top: 1px solid var(--border);
}

.home-review-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  min-height: var(--touch);
  padding: 13px 0;
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

.home-review-title {
  font-size: calc(13px * var(--font-scale));
  font-weight: 650;
  transition: color 120ms var(--ease);
}

.home-review-detail {
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.home-review-item--stale .home-review-detail {
  color: var(--fg);
}

.home-review-item--attention .home-review-title {
  color: var(--warning);
}

.home-review-item--loading .home-review-title {
  color: var(--fg2);
}

.home-review-clear {
  margin: 0;
  padding: 13px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  color: var(--fg3);
  font-size: var(--text-sm);
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

@media (prefers-reduced-motion: reduce) {
  .home-review-title {
    transition: none;
  }
}
</style>
