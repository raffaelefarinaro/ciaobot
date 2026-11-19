import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import ProposalHistoryList from '../components/ProposalHistoryList.vue'
import { useProposalsStore } from '../stores/proposals'
import { historyRows, historyRoutes, mountReview, reviewStage, refreshHistorySnapshot, reviewWorkspace } from './memoryReviewFixture'

/**
 * The decision history of the production ProposalHistoryList, over the
 * fail-closed fixture boundary. Each story mounts the real owner with synthetic
 * `storybook-*` decisions only.
 */

const meta = {
  id: 'proposal-history-list',
  title: 'Memory review/ProposalHistoryList',
  component: ProposalHistoryList,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof ProposalHistoryList>
export default meta
type Story = StoryObj<typeof meta>

async function assertSettled(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  await new Promise(resolve => setTimeout(resolve, 500))
  expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}

export const Default: Story = {
  parameters: { fixture: { routes: historyRoutes('rows') } },
  render: () => reviewStage(
    'Synthetic default: production ProposalHistoryList with one accepted and one dismissed decision.',
    host => mountReview(host, ProposalHistoryList),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Storybook alpha kept a synthetic fact')).toBeTruthy())
    expect(canvas.getByText('Storybook beta dismissed a synthetic suggestion')).toBeTruthy()
    expect(canvas.queryByText(/No decisions yet/)).toBeNull()
    expect(canvas.queryByRole('alert')).toBeNull()
    await assertSettled(canvas)
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: historyRoutes('rows') } },
  render: () => reviewStage(
    'Synthetic narrow: the decision list inside a 390px containing block.',
    host => mountReview(host, ProposalHistoryList),
    true,
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Storybook beta dismissed a synthetic suggestion')).toBeTruthy())
    await assertSettled(canvas)
  },
}

export const Loading: Story = {
  parameters: { fixture: { routes: historyRoutes('held') } },
  render: () => reviewStage(
    'Synthetic loading: the first history read is held, so the list shows its skeleton.',
    host => mountReview(host, ProposalHistoryList),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('status', { name: 'Loading history' })).toBeTruthy())
    // A held read stays a skeleton; an answered one would show its alert here.
    await new Promise(resolve => setTimeout(resolve, 300))
    expect(canvas.getByRole('status', { name: 'Loading history' })).toBeTruthy()
    expect(canvas.queryByRole('alert')).toBeNull()
    await assertSettled(canvas)
  },
}

export const InitialError: Story = {
  parameters: { fixture: { routes: historyRoutes('error') } },
  render: () => reviewStage(
    'Synthetic initial error: the first history read fails before any page exists.',
    host => mountReview(host, ProposalHistoryList),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('alert')).toBeTruthy())
    expect(canvas.getByRole('alert').textContent).toContain('Synthetic review list unavailable')
    expect(canvas.queryByText(/No decisions yet/)).toBeNull()
    await assertSettled(canvas)
  },
}

export const Stale: Story = {
  parameters: { fixture: { routes: historyRoutes('error') } },
  render: () => reviewStage(
    'Synthetic stale: a loaded page stays on screen while its refresh fails.',
    host => mountReview(host, ProposalHistoryList, {
      seed: (pinia) => {
        const store = useProposalsStore(pinia)
        store.historyRows = historyRows
        store.historyLoaded = true
        store.historyWorkspace = reviewWorkspace
      },
      afterMount: refreshHistorySnapshot,
    }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('alert').textContent).toContain('Synthetic review list unavailable'))
    expect(canvas.getByText('Storybook alpha kept a synthetic fact')).toBeTruthy()
    await assertSettled(canvas)
  },
}

export const FilteredEmpty: Story = {
  parameters: { fixture: { routes: historyRoutes('rows') } },
  render: () => reviewStage(
    'Synthetic filtered empty: the action filter is set to a decision kind the page does not hold.',
    host => mountReview(host, ProposalHistoryList, {
      seed: (pinia) => {
        const store = useProposalsStore(pinia)
        // A loaded page of accepted decisions only; the mount read is skipped once loaded.
        store.historyRows = historyRows.filter(row => row.action === 'accepted')
        store.historyLoaded = true
        store.historyWorkspace = reviewWorkspace
        store.historyActionFilter = 'dismissed'
      },
    }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/No decisions match the current filters/)).toBeTruthy())
    expect(canvas.queryByText(/No decisions yet/)).toBeNull()
    await assertSettled(canvas)
  },
}

export const Empty: Story = {
  parameters: { fixture: { routes: historyRoutes('empty') } },
  render: () => reviewStage(
    'Synthetic empty: a successful read with no decisions in this workspace.',
    host => mountReview(host, ProposalHistoryList),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('No decisions yet. Suggestions you accept or dismiss show up here.')).toBeTruthy())
    expect(canvas.queryByRole('button', { name: 'show more' })).toBeNull()
    await assertSettled(canvas)
  },
}

export const LoadMore: Story = {
  parameters: { fixture: { routes: historyRoutes('truncated') } },
  render: () => reviewStage(
    'Synthetic load more: the first page is full and the server holds more decisions behind it.',
    host => mountReview(host, ProposalHistoryList),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('button', { name: 'show more' })).toBeTruthy())
    expect((canvas.getByRole('button', { name: 'show more' }) as HTMLButtonElement).disabled).toBe(false)
    await assertSettled(canvas)
  },
}
