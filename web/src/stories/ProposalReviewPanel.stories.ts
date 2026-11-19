import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import ProposalReviewPanel from '../components/ProposalReviewPanel.vue'
import { useProposalsStore } from '../stores/proposals'
import { mountReview, proposalRoutes, reviewStage, seedProposalSnapshot } from './memoryReviewFixture'

/**
 * The suggested queue and the history section of the production
 * ProposalReviewPanel, over the fail-closed fixture boundary. Each story mounts
 * the real owner (its SkeletonLoader, ProposalHistoryList and DecisionReason
 * children included) with synthetic `storybook-*` rows only.
 */

const meta = {
  id: 'proposal-review-panel',
  title: 'Memory review/ProposalReviewPanel',
  component: ProposalReviewPanel,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof ProposalReviewPanel>
export default meta
type Story = StoryObj<typeof meta>

// Under the browser runner the fence sees the runner's own control socket; let
// late reporting arrive and then assert the ledger is empty.
async function assertSettled(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  await new Promise(resolve => setTimeout(resolve, 500))
  expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}

const queueRows = proposalRoutes({ queue: 'rows' })

export const Default: Story = {
  parameters: { fixture: { routes: queueRows } },
  render: () => reviewStage(
    'Synthetic default: production ProposalReviewPanel with two suggested memory rows.',
    host => mountReview(host, ProposalReviewPanel),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('2 suggested')).toBeTruthy())
    expect(canvas.getByText('Storybook alpha keeps its synthetic fixtures tidy')).toBeTruthy()
    expect(canvas.getByText('Storybook beta prefers short synthetic reviews')).toBeTruthy()
    expect(canvas.queryByText('All reviewed.')).toBeNull()
    await assertSettled(canvas)
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: queueRows } },
  render: () => reviewStage(
    'Synthetic narrow: the default queue inside a 390px containing block.',
    host => mountReview(host, ProposalReviewPanel),
    true,
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('2 suggested')).toBeTruthy())
    expect(canvas.getByText('Storybook beta prefers short synthetic reviews')).toBeTruthy()
    await assertSettled(canvas)
  },
}

export const Loading: Story = {
  parameters: { fixture: { routes: proposalRoutes({ queue: 'held' }) } },
  render: () => reviewStage(
    'Synthetic loading: the first proposal read is held, so the queue shows its skeleton.',
    host => mountReview(host, ProposalReviewPanel),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('status', { name: 'Loading proposals' })).toBeTruthy())
    // A held read stays a skeleton; an answered one would show its retry or rows here.
    await new Promise(resolve => setTimeout(resolve, 300))
    expect(canvas.getByRole('status', { name: 'Loading proposals' })).toBeTruthy()
    expect(canvas.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(canvas.queryByText('All reviewed.')).toBeNull()
    await assertSettled(canvas)
  },
}

export const InitialError: Story = {
  parameters: { fixture: { routes: proposalRoutes({ queue: 'error' }) } },
  render: () => reviewStage(
    'Synthetic initial error: the first proposal read fails before any snapshot exists.',
    host => mountReview(host, ProposalReviewPanel),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Retry' })).toBeTruthy())
    expect(canvas.getByText('Synthetic review list unavailable')).toBeTruthy()
    expect(canvas.queryByText('All reviewed.')).toBeNull()
    expect(canvas.queryByRole('status', { name: 'Loading proposals' })).toBeNull()
    await assertSettled(canvas)
  },
}

export const Stale: Story = {
  parameters: { fixture: { routes: proposalRoutes({ queue: 'error' }) } },
  render: () => reviewStage(
    'Synthetic stale: a snapshot is on screen and the refresh fails, so the rows stay under a stale notice.',
    host => mountReview(host, ProposalReviewPanel, { seed: seedProposalSnapshot }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Could not refresh — showing the last loaded queue.')).toBeTruthy())
    expect(canvas.getByText('Storybook alpha keeps its synthetic fixtures tidy')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Retry' })).toBeTruthy()
    await assertSettled(canvas)
  },
}

export const FilteredEmpty: Story = {
  parameters: { fixture: { routes: queueRows } },
  render: () => reviewStage(
    'Synthetic filtered empty: a search that matches none of the two suggested rows.',
    host => mountReview(host, ProposalReviewPanel, {
      seed: (pinia) => { useProposalsStore(pinia).search = 'storybook-no-such-suggestion' },
    }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('No proposals match the current filters.')).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Clear filters' })).toBeTruthy()
    expect(canvas.queryByText('All reviewed.')).toBeNull()
    await assertSettled(canvas)
  },
}

export const Empty: Story = {
  parameters: { fixture: { routes: proposalRoutes({ queue: 'empty' }) } },
  render: () => reviewStage(
    'Synthetic empty: a successful read with nothing suggested in this workspace.',
    host => mountReview(host, ProposalReviewPanel),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('All reviewed.')).toBeTruthy())
    expect(canvas.getByText('0 suggested')).toBeTruthy()
    expect(canvas.queryByRole('button', { name: 'Clear filters' })).toBeNull()
    await assertSettled(canvas)
  },
}

export const Selection: Story = {
  parameters: { fixture: { routes: queueRows } },
  render: () => reviewStage(
    'Synthetic selection: the row checkboxes are on and one row is ticked, so the batch bar counts it.',
    host => mountReview(host, ProposalReviewPanel),
  ),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('2 suggested')).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: 'Select' }))
    const first = await waitFor(() => canvas.getByRole('checkbox', { name: /Storybook alpha keeps its synthetic fixtures tidy/ }))
    await userEvent.click(first)
    await waitFor(() => expect(canvas.getByText('1 selected')).toBeTruthy())
    expect((first as HTMLInputElement).checked).toBe(true)
    await assertSettled(canvas)
  },
}

export const PreviewOpen: Story = {
  parameters: { fixture: { routes: queueRows } },
  render: () => reviewStage(
    'Synthetic preview open: the first row opens its decision card with the server preview.',
    host => mountReview(host, ProposalReviewPanel),
  ),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('2 suggested')).toBeTruthy())
    const accept = await waitFor(() => canvas.getAllByRole('button', { name: 'Add entry' })[0])
    await userEvent.click(accept)
    // The card repeats the row's text as its preview; the row hides its own change tag while open.
    await waitFor(() => expect(canvas.getAllByText('Storybook alpha keeps its synthetic fixtures tidy').length).toBeGreaterThan(1))
    // The second row keeps its own accept action; the first row's has moved into the card.
    expect(canvas.getAllByRole('button', { name: 'Add entry' })).toHaveLength(1)
    await assertSettled(canvas)
  },
}

export const PreviewError: Story = {
  parameters: { fixture: { routes: proposalRoutes({ queue: 'rows', previews: 'error' }) } },
  render: () => reviewStage(
    'Synthetic preview error: the destination read for each row fails, so the rows say they could not read it.',
    host => mountReview(host, ProposalReviewPanel),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getAllByText('· could not read the destination.', { exact: false })).toHaveLength(2))
    expect(canvas.getAllByRole('button', { name: 'Retry' }).length).toBeGreaterThan(0)
    expect(canvas.queryByText('2 suggested')).not.toBeNull()
    await assertSettled(canvas)
  },
}

export const History: Story = {
  parameters: { fixture: { routes: proposalRoutes({ queue: 'rows', history: 'rows' }) } },
  render: () => reviewStage(
    'Synthetic history section: the panel renders its decision history instead of the queue.',
    host => mountReview(host, ProposalReviewPanel, { props: { section: 'history' } }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Storybook alpha kept a synthetic fact')).toBeTruthy())
    expect(canvas.getByText('Storybook beta dismissed a synthetic suggestion')).toBeTruthy()
    expect(canvas.queryByText(/suggested$/)).toBeNull()
    await assertSettled(canvas)
  },
}
