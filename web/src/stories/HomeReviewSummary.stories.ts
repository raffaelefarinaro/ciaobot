import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import type { Pinia } from 'pinia'
import HomeReviewSummary from '../components/HomeReviewSummary.vue'
import type { ProposalRow, Schedule } from '../lib/types'
import { useProposalsStore } from '../stores/proposals'
import { useTaskStore } from '../stores/tasks'
import { useVaultReviewStore } from '../stores/vaultReview'
import { statsRoutes } from './homeTodayStatsFixture'
import { assertFenceQuiet } from './homeWindowsFixture'
import { renderSeeded } from './homeIntakeFixture'

/** Synthetic workspace only: no real names, paths or tokens. */
const WORKSPACE = 'storybook-alpha'

function proposal(id: string): ProposalRow {
  return {
    id,
    kind: 'memory',
    text: `Synthetic fact ${id}`,
    source: 'storybook',
    workspace: WORKSPACE,
    path: 'MEMORY.md',
    line: 1,
    region: 'memory',
    leak_warning: false,
  } as ProposalRow
}

function failingSchedule(): Schedule {
  return {
    schedule_id: 'storybook-schedule-alpha',
    title: 'Synthetic briefing',
    prompt: 'Write the synthetic briefing',
    frequency: 'daily',
    enabled: true,
    workspace: WORKSPACE,
    last_status: 'error',
    next_run: '2026-11-19T08:00:00Z',
  } as Schedule
}

/** Each source's own loaded flags, seeded directly: the owner only reads them. */
type ReviewSeed = {
  proposals?: { rows?: ProposalRow[], loaded?: boolean, loadError?: string }
  retirementLoaded?: boolean
  automations?: { loaded?: boolean, schedules?: Schedule[], loadError?: string }
}

function seedReview(state: ReviewSeed) {
  return (pinia: Pinia) => {
    const proposals = useProposalsStore(pinia)
    proposals.rows = state.proposals?.rows ?? []
    proposals.loaded = state.proposals?.loaded ?? false
    proposals.loadError = state.proposals?.loadError ?? ''
    const retirement = useVaultReviewStore(pinia)
    retirement.candidates = []
    retirement.loadedWorkspace = state.retirementLoaded ? WORKSPACE : null
    const tasks = useTaskStore(pinia)
    tasks.schedules = state.automations?.schedules ?? []
    tasks.schedulesLoaded = state.automations?.loaded ?? false
    tasks.scheduleLoadError = state.automations?.loadError ?? ''
  }
}

const meta = {
  id: 'home-review-summary',
  title: 'Home intake and review/HomeReviewSummary',
  component: HomeReviewSummary,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HomeReviewSummary>
export default meta
type Story = StoryObj<typeof meta>

// The owner's connected child (HomeTodayStats) reads the declared stats route
// for the synthetic workspace. Every play asserts the fence stays quiet.

export const Default: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary with memory proposals',
    seed: seedReview({
      proposals: { rows: [proposal('storybook-proposal-alpha'), proposal('storybook-proposal-beta')], loaded: true },
      retirementLoaded: true,
      automations: { loaded: true },
    }),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.getByRole('button', { name: /2 memory proposals/ })).toBeTruthy()
  },
}

export const Checking: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary while checking',
    seed: seedReview({}),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.getByText('Checking for suggested memories.')).toBeTruthy()
    expect(canvas.getByText('Checking automations.')).toBeTruthy()
  },
}

export const Current: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary with nothing to review',
    seed: seedReview({
      proposals: { rows: [], loaded: true },
      retirementLoaded: true,
      automations: { loaded: true },
    }),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.getByText('Nothing to review.')).toBeTruthy()
    expect(canvas.queryByRole('button', { name: /memory/i })).toBeNull()
  },
}

// A refresh failed after a successful load: the last rows stay, marked stale.
export const Stale: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary with a failed refresh',
    seed: seedReview({
      proposals: { rows: [proposal('storybook-proposal-alpha')], loaded: true, loadError: 'Synthetic proposal refresh failed.' },
      retirementLoaded: true,
      automations: { loaded: true },
    }),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    const item = canvas.getByRole('button', { name: /1 memory proposal/ })
    expect(item.className).toContain('home-review-item--stale')
    expect(canvas.getByText(/Showing the last successful load\./)).toBeTruthy()
  },
}

// The first load failed with nothing to show: the row carries the error and the attention style.
export const Failed: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary with a failed first load',
    seed: seedReview({
      proposals: { rows: [], loaded: false, loadError: 'Synthetic proposals unavailable.' },
      retirementLoaded: true,
      automations: { loaded: true },
    }),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    const item = canvas.getByRole('button', { name: /Memory proposals/ })
    expect(item.className).toContain('home-review-item--attention')
    expect(canvas.getByText('Synthetic proposals unavailable.')).toBeTruthy()
  },
}

export const AutomationAttention: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary with a failed automation',
    seed: seedReview({
      proposals: { rows: [], loaded: true },
      retirementLoaded: true,
      automations: { loaded: true, schedules: [failingSchedule()] },
    }),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    const item = canvas.getByRole('button', { name: /1 automation needs a look/ })
    await waitFor(() => expect(item.className).toContain('home-review-item--attention'))
    expect(canvas.getByText(/the last run failed\./)).toBeTruthy()
  },
}
