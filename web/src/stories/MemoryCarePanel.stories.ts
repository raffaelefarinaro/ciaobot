import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import MemoryCarePanel from '../components/MemoryCarePanel.vue'
import type { CareReport } from '../lib/careReport'
import type { FixtureRoute } from './fixtureBoundary'
import { careProposalRoutes, careReports, careRoute, changesRoute, CARE_PROPOSAL_PATH, CARE_WORKSPACE } from './careFixture'
import { assertFenceQuiet, renderHome } from './homeWindowsFixture'

type Canvas = Parameters<NonNullable<StoryObj['play']>>[0]['canvas']

/**
 * Memory → Workspace care over the fail-closed fixture: the production panel
 * reading one synthetic report per state. Decide in a chat and Fix with a chat
 * open a chat, and Open note and See details open the file viewer, which the
 * fixture does not declare, so no play presses them.
 */
function story(report: CareReport | CareReport[] | 'held' | 'failed', options: { narrow?: boolean, extra?: FixtureRoute[], route?: string } = {}) {
  const owner = renderHome(MemoryCarePanel, {
    label: 'Workspace care workshop: one synthetic workspace and a synthetic report.',
    workspace: CARE_WORKSPACE,
    narrow: options.narrow,
    route: options.route,
    // The page keeps a person's open/closed choice for the session: start each story from none.
    beforeMount: () => {
      sessionStorage.removeItem('ciao.care.stepsOpen')
      sessionStorage.removeItem('ciao.care.notesOpen')
    },
  })
  return {
    parameters: { fixture: { routes: [careRoute(report), changesRoute(), ...(options.extra ?? [])] } },
    render: () => ({ components: { owner }, template: '<owner />' }),
  }
}

async function summary(canvas: Canvas, title: string): Promise<void> {
  await waitFor(() => expect(canvas.getByRole('heading', { name: title })).toBeTruthy())
  await assertFenceQuiet(canvas, 2)
}

const meta = {
  id: 'memory-care-panel',
  title: 'Memory/MemoryCarePanel',
  component: MemoryCarePanel,
  tags: ['!autodocs'],
  parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } },
} satisfies Meta<typeof MemoryCarePanel>
export default meta
type Story = StoryObj<typeof meta>

export const NeedsYou: Story = {
  ...story(careReports.needsYou),
  play: async ({ canvas }) => {
    await summary(canvas, 'Done with 4 things for you')
    expect(canvas.getByRole('heading', { name: 'What needs you' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: /^Review proposal: A proposal would trim your guide/ })).toBeTruthy()
    expect(canvas.getByRole('button', { name: /^Optimize: Your always-loaded memory/ })).toBeTruthy()
    expect(canvas.getByRole('button', { name: /^Open Unsure: 13 suggestions/ })).toBeTruthy()
    // A proposal, the guide and Unsure each have one destination; the item about a real note is decided in a chat,
    // with Open note and its report section beside it, each at the 44px floor.
    expect(canvas.queryAllByRole('button', { name: /^Fix with a chat: / })).toHaveLength(0)
    for (const name of [/^Decide in a chat: Links in People\/User\.md/, /^Open note: Links/, /^See details: Links/]) {
      expect(canvas.getByRole('button', { name }).getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    }
    // Steps and Notes are folded: the step words are not shown until opened.
    const steps = canvas.getByText('Steps').closest('details') as HTMLDetailsElement
    expect(steps.open).toBe(false)
    expect((canvas.getByText('Notes').closest('details') as HTMLDetailsElement).open).toBe(false)
  },
}

export const StepsOpened: Story = {
  ...story(careReports.needsYou),
  play: async ({ canvas, userEvent }) => {
    await summary(canvas, 'Done with 4 things for you')
    const toggle = canvas.getByText('Steps').closest('summary') as HTMLElement
    await userEvent.click(toggle)
    await waitFor(() => expect((toggle.parentElement as HTMLDetailsElement).open).toBe(true))
    expect(canvas.getAllByText('done')).toHaveLength(5)
    expect(toggle.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  },
}

export const ProposalReview: Story = {
  ...story(careReports.needsYou, { route: `/?proposal=${CARE_PROPOSAL_PATH}`, extra: careProposalRoutes() }),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('heading', { name: 'Trim the guide' })).toBeTruthy())
    expect(canvas.getByText('Guide change')).toBeTruthy()
    expect(canvas.getByRole('heading', { name: 'What it changes' })).toBeTruthy()
    expect(canvas.getByText('The change is made in a chat, with you.')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Approve' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Reject' })).toBeTruthy()
    await assertFenceQuiet(canvas)
  },
}

export const ProposalRejected: Story = {
  ...story(careReports.needsYou, { route: `/?proposal=${CARE_PROPOSAL_PATH}`, extra: careProposalRoutes() }),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('heading', { name: 'Trim the guide' })).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: 'Reject' }))
    await waitFor(() => expect(canvas.getByText('This proposal was rejected. Nothing else was changed.')).toBeTruthy())
    expect(canvas.queryByRole('button', { name: 'Approve' })).toBeNull()
    await assertFenceQuiet(canvas)
  },
}

export const ProposalReviewNarrow: Story = {
  ...story(careReports.needsYou, { narrow: true, route: `/?proposal=${CARE_PROPOSAL_PATH}`, extra: careProposalRoutes() }),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('heading', { name: 'Trim the guide' })).toBeTruthy())
    for (const name of ['Approve', 'Reject']) {
      expect(canvas.getByRole('button', { name }).getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    }
  },
}

export const Complete: Story = {
  ...story(careReports.complete),
  play: async ({ canvas }) => {
    await summary(canvas, 'Done')
    expect(canvas.queryByRole('heading', { name: 'What needs you' })).toBeNull()
    expect(canvas.getByText('Nothing needs you.')).toBeTruthy()
    expect((canvas.getByText('Steps').closest('details') as HTMLDetailsElement).open).toBe(false)
    expect(canvas.queryByText(/Weekly checks/)).toBeNull()
    expect(canvas.queryByText('‹ Memory')).toBeNull()
  },
}

export const Blocked: Story = {
  ...story(careReports.blocked),
  play: async ({ canvas }) => {
    await summary(canvas, 'Did not finish: 4 of 6 steps')
    expect(canvas.getAllByText('blocked')).toHaveLength(2)
    expect(canvas.getByRole('button', { name: 'Fix Hygiene with a chat' })).toBeTruthy()
    // Nothing needs the person, so the steps are open to show what stopped.
    expect(canvas.getByText('Nothing needs your decision. Some steps did not finish; they are below.')).toBeTruthy()
    expect(canvas.getByText('Hygiene did not finish; it runs again on the next run.')).toBeTruthy()
  },
}

export const Interrupted: Story = {
  ...story(careReports.interrupted),
  play: async ({ canvas }) => {
    await summary(canvas, 'Cut off after 2 of 6 steps')
    expect(canvas.getAllByText('not done')).toHaveLength(4)
  },
}

export const RunningStale: Story = {
  ...story(careReports.runningStale),
  play: async ({ canvas }) => {
    await summary(canvas, 'Running now')
    expect(canvas.getByText('Care is starting. Results appear here as steps finish.')).toBeTruthy()
    expect(canvas.getByText(/^Previous run: .* · 1 thing needed you$/)).toBeTruthy()
    // Nothing from the previous report is shown as current.
    expect(canvas.queryByRole('heading', { name: 'What needs you' })).toBeNull()
    expect(canvas.queryByText('Steps')).toBeNull()
    expect(canvas.queryByText('Notes')).toBeNull()
    expect(canvas.queryByText('blocked')).toBeNull()
  },
}

export const Running: Story = {
  ...story(careReports.running),
  play: async ({ canvas }) => {
    await summary(canvas, 'Running: step 3 of 6')
    expect(canvas.getByText('running')).toBeTruthy()
    expect(canvas.getAllByText('waiting')).toHaveLength(3)
  },
}

export const NeverRun: Story = {
  ...story(careReports.never),
  play: async ({ canvas }) => {
    await summary(canvas, 'Not run yet')
    expect(canvas.getByText('It runs every day. Its report will show here.')).toBeTruthy()
  },
}

export const Loading: Story = {
  ...story('held'),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Reading the report…')).toBeTruthy()
    await assertFenceQuiet(canvas)
  },
}

export const ReadFailed: Story = {
  ...story('failed'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Could not read the care report/)).toBeTruthy())
  },
}

export const Narrow: Story = {
  ...story(careReports.blocked, { narrow: true }),
  play: async ({ canvas }) => {
    await summary(canvas, 'Did not finish: 4 of 6 steps')
    const fix = canvas.getByRole('button', { name: 'Fix Hygiene with a chat' })
    expect(fix.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  },
}
