import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import type { Pinia } from 'pinia'
import HomeReviewSummary from '../components/HomeReviewSummary.vue'
import type { Schedule } from '../lib/types'
import { useTaskStore } from '../stores/tasks'
import { statsRoutes } from './homeTodayStatsFixture'
import { assertFenceQuiet } from './homeWindowsFixture'
import { renderSeeded } from './homeIntakeFixture'

/** Synthetic workspace only: no real names, paths or tokens. */
const WORKSPACE = 'storybook-alpha'

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
  automations?: { loaded?: boolean, schedules?: Schedule[], loadError?: string }
}

function seedReview(state: ReviewSeed) {
  return (pinia: Pinia) => {
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

export const Checking: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary while checking',
    seed: seedReview({}),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.getByText('Checking automations.')).toBeTruthy()
  },
}

export const Current: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary with nothing to review',
    seed: seedReview({
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
      automations: { loaded: true, schedules: [failingSchedule()], loadError: 'Synthetic schedule refresh failed.' },
    }),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    const item = canvas.getByRole('button', { name: /1 automation needs a look/ })
    expect(item.className).toContain('home-review-item--stale')
    expect(canvas.getByText(/Showing the last successful load\./)).toBeTruthy()
  },
}

// The first load failed with nothing to show: the row carries the error.
export const Failed: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary with a failed first load',
    seed: seedReview({
      automations: { loaded: false, loadError: 'Synthetic schedules unavailable.' },
    }),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.getByText('Synthetic schedules unavailable.')).toBeTruthy()
  },
}

export const AutomationAttention: Story = {
  parameters: { fixture: { routes: statsRoutes('default') } },
  render: () => renderSeeded(HomeReviewSummary, {
    label: 'Synthetic review summary with a failed automation',
    seed: seedReview({
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
