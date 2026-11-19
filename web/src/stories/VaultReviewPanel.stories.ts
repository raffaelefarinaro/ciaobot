import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import VaultReviewPanel from '../components/VaultReviewPanel.vue'
import { mountReview, refreshVaultSnapshot, reviewStage, seedVaultSnapshot, vaultRoutes } from './memoryReviewFixture'

/**
 * The retirement queues of the production VaultReviewPanel (To revisit and
 * Trash), over the fail-closed fixture boundary. Each story mounts the real
 * owner with synthetic `storybook-*` notes only.
 */

const meta = {
  id: 'vault-review-panel',
  title: 'Memory review/VaultReviewPanel',
  component: VaultReviewPanel,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof VaultReviewPanel>
export default meta
type Story = StoryObj<typeof meta>

async function assertSettled(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  await new Promise(resolve => setTimeout(resolve, 500))
  expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}

export const Default: Story = {
  parameters: { fixture: { routes: vaultRoutes('loaded') } },
  render: () => reviewStage(
    'Synthetic default: production VaultReviewPanel To revisit section with two candidate notes.',
    host => mountReview(host, VaultReviewPanel),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Storybook alpha')).toBeTruthy())
    expect(canvas.getByText('Storybook beta')).toBeTruthy()
    expect(canvas.queryByText(/Nothing to revisit/)).toBeNull()
    await assertSettled(canvas)
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: vaultRoutes('loaded') } },
  render: () => reviewStage(
    'Synthetic narrow: the To revisit list inside a 390px containing block.',
    host => mountReview(host, VaultReviewPanel),
    true,
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Storybook beta')).toBeTruthy())
    await assertSettled(canvas)
  },
}

export const Loading: Story = {
  parameters: { fixture: { routes: vaultRoutes('held') } },
  render: () => reviewStage(
    'Synthetic loading: the retirement read is held, so the panel shows its candidate skeleton.',
    host => mountReview(host, VaultReviewPanel),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('status', { name: 'Loading candidates' })).toBeTruthy())
    // A held read stays a skeleton; an answered one would show its alert or list here.
    await new Promise(resolve => setTimeout(resolve, 300))
    expect(canvas.getByRole('status', { name: 'Loading candidates' })).toBeTruthy()
    expect(canvas.queryByRole('alert')).toBeNull()
    expect(canvas.queryByText(/Nothing to revisit/)).toBeNull()
    await assertSettled(canvas)
  },
}

export const InitialError: Story = {
  parameters: { fixture: { routes: vaultRoutes('error') } },
  render: () => reviewStage(
    'Synthetic initial error: the first retirement read fails before any snapshot exists.',
    host => mountReview(host, VaultReviewPanel),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('alert').textContent).toContain('Could not load notes to revisit.'))
    expect(canvas.getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(canvas.queryByText(/Nothing to revisit/)).toBeNull()
    await assertSettled(canvas)
  },
}

export const Stale: Story = {
  parameters: { fixture: { routes: vaultRoutes('error') } },
  render: () => reviewStage(
    'Synthetic stale: a loaded list stays on screen while its refresh fails.',
    host => mountReview(host, VaultReviewPanel, {
      seed: seedVaultSnapshot,
      afterMount: refreshVaultSnapshot,
    }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Could not refresh this list. Showing the last successful load.')).toBeTruthy())
    expect(canvas.getByText('Storybook alpha')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Retry' })).toBeTruthy()
    await assertSettled(canvas)
  },
}

export const Empty: Story = {
  parameters: { fixture: { routes: vaultRoutes('empty') } },
  render: () => reviewStage(
    'Synthetic empty: a successful read with nothing to revisit in this workspace.',
    host => mountReview(host, VaultReviewPanel),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Nothing to revisit/)).toBeTruthy())
    expect(canvas.queryByText('Storybook alpha')).toBeNull()
    await assertSettled(canvas)
  },
}

export const Trash: Story = {
  parameters: { fixture: { routes: vaultRoutes('loaded') } },
  render: () => reviewStage(
    'Synthetic trash: the Trash section lists one retired note.',
    host => mountReview(host, VaultReviewPanel, { props: { section: 'trash' } }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Storybook retired')).toBeTruthy())
    expect(canvas.getByText('storybook-vault/Storybook retired.md', { exact: false })).toBeTruthy()
    expect(canvas.queryByText(/Nothing retired yet/)).toBeNull()
    await assertSettled(canvas)
  },
}

export const TrashEmpty: Story = {
  parameters: { fixture: { routes: vaultRoutes('empty') } },
  render: () => reviewStage(
    'Synthetic trash empty: a successful read with nothing retired.',
    host => mountReview(host, VaultReviewPanel, { props: { section: 'trash' } }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Nothing retired yet/)).toBeTruthy())
    await assertSettled(canvas)
  },
}

export const TrashLoading: Story = {
  parameters: { fixture: { routes: vaultRoutes('held') } },
  render: () => reviewStage(
    'Synthetic trash loading: the retirement read is held, so Trash shows its retired-notes skeleton.',
    host => mountReview(host, VaultReviewPanel, { props: { section: 'trash' } }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('status', { name: 'Loading retired notes' })).toBeTruthy())
    await assertSettled(canvas)
  },
}

export const TrashError: Story = {
  parameters: { fixture: { routes: vaultRoutes('error') } },
  render: () => reviewStage(
    'Synthetic trash error: the retirement read fails before Trash has a snapshot.',
    host => mountReview(host, VaultReviewPanel, { props: { section: 'trash' } }),
  ),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('alert').textContent).toContain('Could not load retired notes.'))
    expect(canvas.queryByText(/Nothing retired yet/)).toBeNull()
    await assertSettled(canvas)
  },
}
