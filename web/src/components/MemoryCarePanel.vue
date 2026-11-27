<template>
  <section class="mc" aria-labelledby="mc-title">
    <button v-if="proposalPath" type="button" class="mc-back" @click="closeProposal">
      <span aria-hidden="true">‹</span> Workspace care
    </button>
    <RouterLink v-else class="mc-back" :to="memorySectionPath('map')">
      <span aria-hidden="true">‹</span> Memory
    </RouterLink>
    <h2 id="mc-title" class="mc-title">{{ proposalPath ? 'Review proposal' : 'Workspace care' }}</h2>
    <p v-if="!proposalPath" class="mc-lede">What the daily run left for you, and how it went. Read from Workspace/Care-Report.md.</p>

    <p v-if="!report && !care.error" class="mc-note">Reading the report…</p>
    <p v-else-if="!report" class="mc-note mc-note--error" role="alert">Could not read the care report: {{ care.error }}</p>

    <CareProposalReview v-else-if="proposalPath" :report="report" :path="proposalPath" />

    <div v-else class="mc-card">
      <div class="mc-summary">
        <h3 class="mc-summary-title">
          <CareGlyph :kind="glyph" :size="18" />
          <span>{{ summaryTitle }}</span>
        </h3>
        <p class="mc-summary-meta">{{ summaryMeta }}</p>
        <!-- One segment per step, in the report's order. The words above say the
             same thing, so the bar is decoration for assistive tech. -->
        <div v-if="report.steps.length" class="mc-progress" aria-hidden="true">
          <i
            v-for="(step, i) in report.steps"
            :key="i"
            class="mc-seg"
            :class="`mc-seg--${stepGlyph(step, report.running)}`"
          />
        </div>
      </div>

      <p class="mc-status" role="status">{{ status }}</p>
      <p v-if="care.error" class="mc-note">Could not refresh the report. Showing the last one read.</p>

      <section v-if="needs.length" class="mc-section" aria-labelledby="mc-needs-title">
        <h3 id="mc-needs-title" class="rail-title">What needs you</h3>
        <ol class="mc-needs">
          <li v-for="(item, i) in needs" :key="`${i}:${item.path}`" class="mc-need">
            <span class="mc-need-n" aria-hidden="true">{{ i + 1 }}</span>
            <div class="mc-need-body">
              <p class="mc-need-title">{{ item.title }}</p>
              <p v-if="item.detail" class="mc-need-detail">{{ item.detail }}</p>
              <div class="mc-need-actions">
                <button
                  v-if="needActionLabel(needAction(item))"
                  type="button"
                  class="btn-small mc-action"
                  :class="{ 'btn-primary': needAction(item) === 'proposal' }"
                  :aria-label="`${needActionLabel(needAction(item))}: ${item.title}`"
                  @click="act(item)"
                >{{ needActionLabel(needAction(item)) }}</button>
                <button
                  type="button"
                  class="mc-link mc-fix"
                  :disabled="fixing !== ''"
                  :aria-label="`Fix with a chat: ${item.title}`"
                  @click="fixNeed(item, i)"
                >{{ fixing === `need:${i}` ? 'Opening…' : 'Fix with a chat' }}</button>
              </div>
            </div>
          </li>
        </ol>
      </section>
      <p v-else-if="calm" class="mc-calm">Nothing needs you.</p>

      <details
        v-if="report.steps.length"
        class="mc-disclosure"
        :open="stepsOpen"
        @toggle="onToggle($event, 'steps')"
      >
        <summary class="mc-disclosure-summary">
          <AppIcon name="chevron" class="mc-chevron" :size="14" />
          <span class="mc-disclosure-label">Steps</span>
          <span class="mc-disclosure-count">{{ stepsCount }}</span>
        </summary>
        <ul class="mc-steps">
          <li v-for="(step, i) in report.steps" :key="i" class="mc-step">
            <CareGlyph class="mc-step-glyph" :kind="stepGlyph(step, report.running)" />
            <div class="mc-step-body">
              <p class="mc-step-name">{{ stepName(step.id) }}</p>
              <p class="mc-step-line">{{ stepLine(step) }}</p>
              <p v-if="step.verify" class="mc-step-evidence">verify: {{ step.verify }}</p>
              <button
                v-if="stepCanFix(step)"
                type="button"
                class="btn-small mc-action"
                :disabled="fixing !== ''"
                :aria-label="`Fix ${stepName(step.id)} with a chat`"
                @click="fix(step)"
              >{{ fixing === step.id ? 'Opening…' : 'Fix with a chat' }}</button>
            </div>
            <span class="mc-step-state">{{ stepWord(step, report.running) }}</span>
          </li>
        </ul>
        <p v-if="report.interrupted_ids.length" class="mc-note">
          The previous run stopped before finishing {{ report.interrupted_ids.map(stepName).join(', ') }}.
        </p>
        <p v-if="report.weekly_retry.length" class="mc-note">
          Retried on each run until it works, for up to 3 runs: {{ retryWords }}.
        </p>
      </details>

      <details
        v-if="report.notes.length"
        class="mc-disclosure"
        :open="notesOpen"
        @toggle="onToggle($event, 'notes')"
      >
        <summary class="mc-disclosure-summary">
          <AppIcon name="chevron" class="mc-chevron" :size="14" />
          <span class="mc-disclosure-label">Notes</span>
          <span class="mc-disclosure-count">{{ report.notes.length }}</span>
        </summary>
        <ul class="mc-notes">
          <li v-for="(note, i) in report.notes" :key="i">{{ note }}</li>
        </ul>
      </details>

      <div class="mc-foot">
        <span class="mc-foot-note">{{ weeklyWords }}</span>
        <span class="mc-foot-actions">
          <button
            v-if="report.schedule"
            type="button"
            class="mc-link"
            :disabled="care.starting || !!report.running"
            @click="runAgain"
          >{{ care.starting ? 'Starting…' : report.running ? 'Running…' : report.status === 'missing' ? 'Run care now' : 'Run care again' }}</button>
          <button
            type="button"
            class="mc-link"
            :disabled="report.status === 'missing'"
            :title="`Open ${report.report_path}`"
            @click="openReport"
          >Open Care-Report.md</button>
        </span>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import AppIcon from './AppIcon.vue'
import CareGlyph from './CareGlyph.vue'
import CareProposalReview from './CareProposalReview.vue'
import {
  careFixSeed, careGlyph, careNeedFixSeed, careState, careSummaryMeta, careSummaryTitle, dayWords, needAction,
  needActionLabel, openNeeds, stepCanFix, stepGlyph, stepLine, stepName, stepsCountWords, stepsOpenByDefault,
  stepWord, type CareNeed, type CareStep,
} from '../lib/careReport'
import { startFileDiscussion } from '../lib/fileDiscussion'
import { fetchWorkspaceGuide } from '../lib/workspaceGuide'
import { useCareReportStore } from '../stores/careReport'
import { useFileViewerStore } from '../stores/fileViewer'
import { memorySectionPath } from '../stores/memoryMap'
import { useProjectStore } from '../stores/projects'

/**
 * Memory → Workspace care: the last care run as the report file states it.
 * What needs the person leads; the steps and the run's notes fold away below.
 * Every button goes somewhere that already exists — the file viewer, a new chat
 * with a draft that is never sent, the care schedule's own run-now — or to the
 * proposal review (`?proposal=<vault path>`), whose only write is Reject.
 */
const store = useProjectStore()
const care = useCareReportStore()
const fileViewer = useFileViewerStore()
const route = useRoute()
const router = useRouter()

watch(() => store.activeWorkspace, ws => care.show(ws), { immediate: true })
let release: () => void = () => {}
onMounted(() => {
  release = care.attach()
  // Opening the page re-reads it: the run may have moved since Home read it.
  void care.refresh()
})
onBeforeUnmount(() => release())

const report = computed(() => (care.report && care.report.workspace === store.activeWorkspace ? care.report : null))
const glyph = computed(() => (report.value ? careGlyph(careState(report.value)) : 'open'))
const summaryTitle = computed(() => (report.value ? careSummaryTitle(report.value) : ''))
const summaryMeta = computed(() => (report.value ? careSummaryMeta(report.value) : ''))
const needs = computed(() => (report.value ? openNeeds(report.value) : []))
/** A finished or stopped run with nothing left for the person says so in one line. */
const calm = computed(() => {
  const state = report.value ? careState(report.value) : 'never'
  return state === 'complete' || state === 'blocked' || state === 'interrupted'
})
const stepsCount = computed(() => (report.value ? stepsCountWords(report.value) : ''))
const weeklyWords = computed(() => {
  const day = report.value ? dayWords(report.value.weekly_done) : ''
  return day ? `Weekly checks last done ${day}` : 'Weekly checks not done yet'
})

const retryWords = computed(() => (report.value?.weekly_retry ?? [])
  .map(r => `${stepName(r.id)} (run ${r.runs} of 3)`).join(', '))

// The disclosures start from the run's state; once the person opens or closes
// one, that choice holds for the rest of the browser session.
const STORAGE = { steps: 'ciao.care.stepsOpen', notes: 'ciao.care.notesOpen' } as const
type Disclosure = keyof typeof STORAGE

function remembered(which: Disclosure): boolean | null {
  try {
    const value = sessionStorage.getItem(STORAGE[which])
    return value === 'open' ? true : value === 'closed' ? false : null
  } catch {
    return null
  }
}

const choice = ref<Record<Disclosure, boolean | null>>({ steps: remembered('steps'), notes: remembered('notes') })
const stepsOpen = computed(() => choice.value.steps ?? (report.value ? stepsOpenByDefault(report.value) : false))
const notesOpen = computed(() => choice.value.notes ?? false)

function onToggle(event: Event, which: Disclosure): void {
  const open = (event.target as HTMLDetailsElement).open
  const shown = which === 'steps' ? stepsOpen.value : notesOpen.value
  // The toggle event also fires when the page itself sets `open`: only a
  // change the bound state did not make is the person's choice.
  if (open === shown) return
  choice.value = { ...choice.value, [which]: open }
  try {
    sessionStorage.setItem(STORAGE[which], open ? 'open' : 'closed')
  } catch {
    // Storage refused (private mode): the choice still holds for this page.
  }
}

const status = ref('')
const fixing = ref('')

/** The proposal the review is showing: only a proposal path, never any file. */
const proposalPath = computed(() => {
  const raw = route.query.proposal
  return typeof raw === 'string' && /^Workspace\/Proposals\/[a-z0-9]+(?:-[a-z0-9]+)*\.md$/.test(raw) ? raw : ''
})

// The review is this page with `?proposal=`: opening and closing it keeps the path.
function closeProposal(): void {
  void router.push({ query: {} })
}

async function runAgain(): Promise<void> {
  status.value = ''
  const { message } = await care.runAgain()
  status.value = message
}

function openReport(): void {
  if (report.value) void fileViewer.open(report.value.report_path)
}

async function act(item: CareNeed): Promise<void> {
  const current = report.value
  if (!current) return
  const action = needAction(item)
  if (action === 'proposal') {
    void router.push({ query: { proposal: item.path } })
    return
  }
  if (action === 'guide') {
    const guide = await fetchWorkspaceGuide(current.workspace)
    if (guide.path) void fileViewer.open(guide.path)
    else status.value = guide.error || 'This workspace has no guide yet.'
    return
  }
  if (item.open_path) void fileViewer.open(item.open_path)
}

async function fixNeed(item: CareNeed, index: number): Promise<void> {
  const current = report.value
  if (!current || fixing.value) return
  fixing.value = `need:${index}`
  try {
    await startFileDiscussion(store, {
      path: item.open_path || current.report_path,
      seed: careNeedFixSeed(current, item),
      title: `Care: ${item.title.replace(/[.:]+$/, '').slice(0, 60)}`,
      workspace: current.workspace,
    })
  } finally {
    fixing.value = ''
  }
}

async function fix(step: CareStep): Promise<void> {
  const current = report.value
  if (!current || fixing.value) return
  fixing.value = step.id
  try {
    await startFileDiscussion(store, {
      path: current.report_path,
      seed: careFixSeed(current, step),
      title: `Fix care step: ${step.id}`,
      workspace: current.workspace,
    })
  } finally {
    fixing.value = ''
  }
}
</script>

<style scoped>
.mc {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4) 0 var(--space-6);
  max-width: 760px;
  container-type: inline-size;
}
.mc-back {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  align-self: flex-start;
  min-height: var(--touch);
  color: var(--fg2);
  font-size: var(--text-sm);
  text-decoration: none;
}
.mc-back:hover { color: var(--fg); }
button.mc-back { padding: 0; border: 0; background: none; cursor: pointer; font: inherit; font-size: var(--text-sm); }
.mc-back:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
.mc-title { margin: 0; font-size: var(--text-lg); }
.mc-lede, .mc-note { margin: 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.5; }
.mc-note--error { color: var(--error-text); }

/* The report reads as one window, like the Needs you list on Home. */
.mc-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-5) var(--space-4);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}
@container (max-width: 520px) { .mc-card { padding: var(--space-1) var(--space-4) var(--space-3); } }
.mc-summary {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-4) 0;
  border-bottom: 1px solid var(--border);
}
.mc-summary-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--text-lg);
  font-weight: 650;
}
.mc-summary-meta { margin: 0; color: var(--fg2); font-size: var(--text-sm); line-height: 1.5; }
.mc-progress { display: flex; gap: 4px; margin-top: var(--space-1); }
.mc-seg { flex: 1 1 0; height: 4px; border-radius: var(--radius-pill); background: var(--border); }
.mc-seg--done { background: var(--success); }
.mc-seg--blocked { background: var(--warning); }
.mc-seg--running { background: var(--accent); }

.mc-status { margin: 0; color: var(--fg2); font-size: var(--text-sm); }
.mc-status:empty { display: none; }

.mc-section { display: flex; flex-direction: column; gap: var(--space-2); padding-top: var(--space-3); }
.mc-section .rail-title { margin: 0; }
.mc-calm { margin: 0; padding-top: var(--space-3); color: var(--fg2); font-size: var(--text-base); }

/* Steps and Notes fold under what needs the person: hairline sections with a
   44px summary, the chevron turning when open, as Always loaded does. */
.mc-disclosure { border-top: 1px solid var(--border); }
.mc-disclosure:first-of-type { margin-top: var(--space-2); }
.mc-disclosure-summary {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  cursor: pointer;
  list-style: none;
  color: var(--fg2);
  font-size: var(--text-sm);
  font-weight: 600;
}
.mc-disclosure-summary::-webkit-details-marker { display: none; }
.mc-disclosure-summary:hover { color: var(--fg); }
.mc-disclosure-summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
.mc-chevron { color: var(--fg3); transition: transform 0.15s ease-out; }
.mc-disclosure[open] .mc-chevron { transform: rotate(90deg); }
@media (prefers-reduced-motion: reduce) { .mc-chevron { transition: none; } }
.mc-disclosure-count { color: var(--fg3); font-size: var(--text-xs); font-variant-numeric: tabular-nums; font-weight: 400; }
.mc-disclosure > .mc-note { margin-top: var(--space-2); }
.mc-notes {
  margin: 0 0 var(--space-3);
  padding-left: var(--space-5);
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}
.mc-notes li + li { margin-top: var(--space-1); }

.mc-needs, .mc-steps { margin: 0; padding: 0; list-style: none; }
.mc-need, .mc-step {
  display: grid;
  column-gap: var(--space-3);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
}
.mc-need { grid-template-columns: 1.5rem minmax(0, 1fr); }
.mc-need-n { color: var(--fg2); font-family: var(--font-mono); font-size: var(--text-sm); line-height: 1.6; }
.mc-need-body, .mc-step-body { min-width: 0; display: flex; flex-direction: column; gap: var(--space-1); align-items: flex-start; }
.mc-need-title, .mc-step-name { margin: 0; font-size: var(--text-base); font-weight: 650; line-height: 1.4; }
.mc-need-detail, .mc-step-line { margin: 0; color: var(--fg2); font-size: var(--text-sm); line-height: 1.5; overflow-wrap: anywhere; }
.mc-action { margin-top: var(--space-1); min-height: var(--touch); }
.mc-need-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1) var(--space-4); }
.mc-fix { margin-top: var(--space-1); }
/* Straight after the summary there is already a rule: no second one. */
.mc-status:empty + .mc-disclosure { border-top: 0; margin-top: 0; }
.mc-steps { border-top: 1px solid var(--border); }
.mc-steps .mc-step:last-child { border-bottom: 0; }

.mc-step { grid-template-columns: auto minmax(0, 1fr) auto; align-items: start; }
.mc-step-glyph { margin-top: 2px; }
.mc-step-evidence {
  margin: 0;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.mc-step-state { color: var(--fg3); font-family: var(--font-mono); font-size: var(--text-xs); line-height: 1.9; }

.mc-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2) var(--space-4);
  padding-top: var(--space-2);
}
.mc-foot-note { color: var(--fg3); font-size: var(--text-sm); }
.mc-foot-actions { display: flex; flex-wrap: wrap; gap: var(--space-4); }
.mc-link {
  min-height: var(--touch);
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
}
@media (pointer: coarse) { .mc-link { min-width: var(--touch); } }
.mc-link:hover:not(:disabled) { text-decoration: underline; text-underline-offset: 3px; }
.mc-link:disabled { color: var(--fg3); cursor: default; }
.mc-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
</style>
