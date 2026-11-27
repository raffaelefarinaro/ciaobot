<template>
  <DockedWindow class="home-review-summary" title="At a glance" title-id="home-review-title">

    <!-- Only what needs a look: an up-to-date queue is not news, so it drops
         out instead of holding a row that says "nothing here". -->
    <div v-if="visibleItems.length || careLine" class="home-review-list">
      <!-- Workspace care: always one quiet line once its report is read. The
           Needs you list carries the signal; this line only states the fact. -->
      <button
        v-if="careLine"
        type="button"
        class="home-review-item home-review-item--care"
        :class="{ 'home-review-item--care-running': careLine.running }"
        @click="openCare"
      >
        <span class="home-review-title home-review-care-title"><CareGlyph :kind="careLine.glyph" />Workspace care</span>
        <span class="home-review-detail">{{ careLine.detail }}</span>
      </button>
      <button
        v-for="item in visibleItems"
        :key="item.key"
        type="button"
        class="home-review-item"
        :class="{
          'home-review-item--attention': item.state === 'error' || item.attention,
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
    <HomeTodayStats />
  </DockedWindow>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import { useTaskSignalsStore } from '../stores/taskSignals'
import type { Schedule } from '../lib/types'
import { scheduleInWorkspace, scheduleNeedsAttention } from '../lib/automationWorkspace'
import HomeTodayStats from './HomeTodayStats.vue'
import CareGlyph from './CareGlyph.vue'
import DockedWindow from './DockedWindow.vue'
import { careGlyph, careRailDetail, careState } from '../lib/careReport'
import { useCareReportStore } from '../stores/careReport'
import { memorySectionPath } from '../stores/memoryMap'

type ReviewState = 'ready' | 'loading' | 'stale' | 'error'
type ReviewItem = {
  key: 'tasks' | 'automations'
  title: string
  detail: string
  count: number
  state: ReviewState
  attention: boolean
}

const router = useRouter()
const projects = useProjectStore()
const tasks = useTaskStore()
const taskSignals = useTaskSignalsStore()

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`
}

// Tasks the agent reported done, waiting for the user's Approve on the board.
const tasksInReview = computed(() => taskSignals.inReview(projects.activeWorkspace))

// Schedules that failed, did not run, or lost their chat: the sidebar's
// Automations warning, scoped to the active workspace.
const failingAutomations = computed(() => tasks.schedules.filter(s => (
  scheduleNeedsAttention(s)
  // An enabled missed one-time reminder is already the install-wide
  // `missed-schedules` notice (with Fire now); the backend only covers enabled
  // ones, so a disabled one that lost its chat still shows here.
  && !(s.frequency === 'once' && s.enabled && s.missed
    && s.last_status !== 'error' && s.last_status !== 'missing-chat')
  && scheduleInWorkspace(s, projects.activeWorkspace, projects.chats, projects.projects)
)))

// Mirrors the backend's `_schedule_display_name`.
function automationName(s: Schedule): string {
  const title = (s.title || '').trim()
  if (title) return title
  const line = (s.prompt || '').trim().split('\n')[0].trim()
  if (line) return line.length > 60 ? `${line.slice(0, 57)}…` : line
  return s.schedule_id
}

function automationProblem(s: Schedule): string {
  if (s.last_status === 'missing-chat') return 'its chat was deleted, so it is turned off.'
  if (s.last_status === 'error') return 'the last run failed.'
  return 'it did not run when expected.'
}

const items = computed<ReviewItem[]>(() => {
  const inReview = tasksInReview.value
  const failing = failingAutomations.value
  const n = failing.length
  let automationState: ReviewState = 'ready'
  let automationDetail = 'Up to date. No automation in this workspace needs a look.'
  if (n === 1) {
    automationDetail = `${automationName(failing[0])} · ${automationProblem(failing[0])}`
  } else if (n > 1) {
    automationDetail = 'Some automations failed or did not run. Open Automations to check them.'
  }
  if (!tasks.schedulesLoaded && !tasks.scheduleLoadError) {
    automationState = 'loading'
    automationDetail = 'Checking automations.'
  } else if (tasks.scheduleLoadError) {
    automationState = tasks.schedules.length ? 'stale' : 'error'
    automationDetail = tasks.schedules.length
      ? `Showing the last successful load. ${tasks.scheduleLoadError}`
      : tasks.scheduleLoadError
  }

  return [
    {
      key: 'tasks',
      title: plural(inReview.length, 'task in review', 'tasks in review'),
      detail: inReview.length === 1
        ? `${inReview[0].title} · the agent says it is done.`
        : 'The agent says these are done. Approve or send them back.',
      count: inReview.length,
      state: 'ready',
      attention: false,
    },
    {
      key: 'automations',
      title: n && automationState !== 'loading'
        ? plural(n, 'automation needs a look', 'automations need a look')
        : 'Automations',
      detail: automationDetail,
      count: n,
      state: automationState,
      attention: automationState === 'ready' && n > 0,
    },
  ]
})

const care = useCareReportStore()
watch(() => projects.activeWorkspace, ws => care.show(ws), { immediate: true })
let releaseCare: () => void = () => {}
onMounted(() => { releaseCare = care.attach() })
onBeforeUnmount(() => releaseCare())

const careLine = computed(() => {
  const report = care.report
  if (!report || report.workspace !== projects.activeWorkspace) return null
  const state = careState(report)
  return { glyph: careGlyph(state), detail: careRailDetail(report), running: state === 'running' }
})

function openCare(): void {
  void router.push(memorySectionPath('care'))
}

const visibleItems = computed(() => items.value.filter(
  item => item.state !== 'ready' || item.count > 0,
))

// The pass itself is no longer linked from here: it is a chat, and Home lists
// every open pass as one row under *memory insights* (HomeRecentChats), which
// is the entry that opens it. A single "open the newest pass" shortcut on top
// of that was a second way into one row out of many.

function openItem(key: ReviewItem['key']) {
  if (key === 'tasks') {
    // One task opens its card; several open the board on the In review column.
    const only = tasksInReview.value.length === 1 ? tasksInReview.value[0] : null
    void router.push(only ? { name: 'task-detail', params: { taskId: only.id } } : '/tasks')
    return
  }
  const failing = failingAutomations.value
  void router.push(failing.length === 1 ? `/schedules/${failing[0].schedule_id}` : '/schedules')
}
</script>

<style scoped>
/* Hairline rows, not cards: "what changed" reads as a short list, where a
   stack of bordered tiles read as a second inbox to clear. */
.home-review-list {
  border-top: 0;
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

/* The care line is quiet in every state: the glyph's shape and the detail's
   words carry the status, and Needs you carries the call to act. */
.home-review-care-title {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg2);
}
.home-review-item--care-running .home-review-care-title { color: var(--fg); }

.home-review-item--loading .home-review-title {
  color: var(--fg2);
}

.home-review-clear {
  margin: 0;
  padding: 13px 0;
  border-top: 0;
  border-bottom: 1px solid var(--border);
  color: var(--fg3);
  font-size: var(--text-sm);
}

@media (prefers-reduced-motion: reduce) {
  .home-review-title {
    transition: none;
  }
}
</style>
