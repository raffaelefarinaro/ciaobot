import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import MemoryCarePanel from '../components/MemoryCarePanel.vue'
import type { CareReport } from '../lib/careReport'
import type { FixtureRoute } from './fixtureBoundary'
import { careReports, careRoute, careRunRoutes, CARE_WORKSPACE } from './careFixture'
import { assertFenceQuiet, renderHome } from './homeWindowsFixture'

type Canvas = Parameters<NonNullable<StoryObj['play']>>[0]['canvas']

/**
 * Memory → Workspace care over the fail-closed fixture: the production panel
 * reading one synthetic report per state. Fix with a chat opens a chat, which
 * the fixture does not declare, so no play presses it.
 */
function story(report: CareReport | CareReport[] | 'held' | 'failed', options: { narrow?: boolean, extra?: FixtureRoute[] } = {}) {
  const owner = renderHome(MemoryCarePanel, {
    label: 'Workspace care workshop: one synthetic workspace and a synthetic report.',
    workspace: CARE_WORKSPACE,
    narrow: options.narrow,
  })
  return {
    parameters: { fixture: { routes: [careRoute(report), ...(options.extra ?? [])] } },
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
    await summary(canvas, 'Done with 2 things for you')
    expect(canvas.getByRole('heading', { name: 'What needs you' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Open Unsure: New line in Unsure' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Review guide: Memory guide at 91%' })).toBeTruthy()
    expect(canvas.getAllByText('done')).toHaveLength(5)
  },
}

export const Complete: Story = {
  ...story(careReports.complete),
  play: async ({ canvas }) => {
    await summary(canvas, 'Done')
    expect(canvas.queryByRole('heading', { name: 'What needs you' })).toBeNull()
    expect(canvas.getByText('Weekly checks last done Nov 27')).toBeTruthy()
  },
}

export const Blocked: Story = {
  ...story(careReports.blocked),
  play: async ({ canvas }) => {
    await summary(canvas, 'Did not finish: 4 of 6 steps')
    expect(canvas.getAllByText('blocked')).toHaveLength(2)
    expect(canvas.getByRole('button', { name: 'Fix Hygiene with a chat' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Open report file: Hygiene is blocked' })).toBeTruthy()
  },
}

export const Interrupted: Story = {
  ...story(careReports.interrupted),
  play: async ({ canvas }) => {
    await summary(canvas, 'Cut off after 2 of 6 steps')
    expect(canvas.getAllByText('not done')).toHaveLength(4)
  },
}

export const Running: Story = {
  ...story(careReports.running),
  play: async ({ canvas }) => {
    await summary(canvas, 'Running: step 3 of 6')
    expect(canvas.getByText('running')).toBeTruthy()
    expect((canvas.getByRole('button', { name: 'Running…' }) as HTMLButtonElement).disabled).toBe(true)
    expect(canvas.getAllByText('waiting')).toHaveLength(3)
  },
}

export const NeverRun: Story = {
  ...story(careReports.never),
  play: async ({ canvas }) => {
    await summary(canvas, 'Not run yet')
    expect(canvas.getByText('It runs every day. Its report will show here.')).toBeTruthy()
    expect((canvas.getByRole('button', { name: 'Open Care-Report.md' }) as HTMLButtonElement).disabled).toBe(true)
  },
}

export const RunAgain: Story = {
  // Two reads before the press (mount and refresh), one inside it, then the run.
  ...story([careReports.blocked, careReports.blocked, careReports.blocked, careReports.running], { extra: careRunRoutes() }),
  play: async ({ canvas, userEvent }) => {
    await summary(canvas, 'Did not finish: 4 of 6 steps')
    await userEvent.click(canvas.getByRole('button', { name: 'Run care again' }))
    await waitFor(() => expect(canvas.getByText('Care started.')).toBeTruthy())
    await assertFenceQuiet(canvas)
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
