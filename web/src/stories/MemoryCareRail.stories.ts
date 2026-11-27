import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { defineComponent, h } from 'vue'
import { expect, waitFor } from 'storybook/test'
import MemoryCarePanel from '../components/MemoryCarePanel.vue'
import MemoryCareRail from '../components/MemoryCareRail.vue'
import type { CareReport } from '../lib/careReport'
import type { FixtureRoute } from './fixtureBoundary'
import { careReports, careRoute, careRunPath, CARE_WORKSPACE } from './careFixture'
import { assertFenceQuiet, json, renderHome } from './homeWindowsFixture'

type Canvas = Parameters<NonNullable<StoryObj['play']>>[0]['canvas']

/**
 * Memory → Workspace care's Daily care window beside the report card, over the
 * fail-closed fixture: one synthetic report per state, and the care schedule's
 * own row for the next run. The page loads the report; the window reads it.
 */
const nextRun = new Date(Date.now() + 12 * 3_600_000 + 60_000).toISOString()

function schedulesRoute(enabled = true): FixtureRoute {
  return {
    method: 'GET',
    path: '/api/schedules',
    respond: () => json([{ schedule_id: `system-memory-curation@${CARE_WORKSPACE}`, enabled, next_run: nextRun }]),
  }
}

const Page = defineComponent({
  setup: () => () => h('div', { style: 'display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 16px; align-items: start' }, [
    h(MemoryCarePanel),
    h(MemoryCareRail),
  ]),
})

function story(report: CareReport | CareReport[], options: { narrow?: boolean, paused?: boolean, extra?: FixtureRoute[] } = {}) {
  const owner = renderHome(options.narrow ? defineComponent({
    setup: () => () => h('div', { style: 'display: flex; flex-direction: column; gap: 16px' }, [h(MemoryCarePanel), h(MemoryCareRail)]),
  }) : Page, {
    label: 'Daily care workshop: one synthetic workspace and a synthetic report.',
    workspace: CARE_WORKSPACE,
    narrow: options.narrow,
    beforeMount: () => {
      sessionStorage.removeItem('ciao.care.stepsOpen')
      sessionStorage.removeItem('ciao.care.notesOpen')
    },
  })
  return {
    parameters: { fixture: { routes: [careRoute(report), schedulesRoute(!options.paused), ...(options.extra ?? [])] } },
    render: () => ({ components: { owner }, template: '<owner />' }),
  }
}

async function windowOf(canvas: Canvas): Promise<HTMLElement> {
  const title = await canvas.findByRole('heading', { name: 'Daily care' })
  return title.closest('section') as HTMLElement
}

const meta = {
  id: 'memory-care-rail',
  title: 'Memory/MemoryCareRail',
  component: MemoryCareRail,
  tags: ['!autodocs'],
  parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } },
} satisfies Meta<typeof MemoryCareRail>
export default meta
type Story = StoryObj<typeof meta>

export const Done: Story = {
  ...story(careReports.complete),
  play: async ({ canvas }) => {
    const win = await windowOf(canvas)
    await waitFor(() => expect(win.textContent).toContain('in 12h'))
    expect(win.textContent).toContain('Done')
    expect(win.textContent).toContain('Weekly checks')
    expect(win.querySelectorAll('.docked-row')).toHaveLength(2)
    expect(canvas.getByRole('button', { name: 'Run care again' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Open Care-Report.md' })).toBeTruthy()
    await assertFenceQuiet(canvas, 2)
  },
}

export const DidNotFinish: Story = {
  ...story(careReports.blocked),
  play: async ({ canvas }) => {
    const win = await windowOf(canvas)
    await waitFor(() => expect(win.textContent).toContain('Did not finish'))
    await assertFenceQuiet(canvas, 2)
  },
}

export const Paused: Story = {
  ...story({ ...careReports.complete, schedule: { ...careReports.complete.schedule!, enabled: false } }, { paused: true }),
  play: async ({ canvas }) => {
    const win = await windowOf(canvas)
    await waitFor(() => expect(win.textContent).toContain('Not scheduled'))
  },
}

export const Running: Story = {
  ...story(careReports.running),
  play: async ({ canvas }) => {
    await windowOf(canvas)
    const run = await canvas.findByRole('button', { name: 'Running…' })
    expect((run as HTMLButtonElement).disabled).toBe(true)
  },
}

export const NeverRun: Story = {
  ...story(careReports.never),
  play: async ({ canvas }) => {
    const win = await windowOf(canvas)
    await waitFor(() => expect(win.textContent).toContain('Never run'))
    expect((canvas.getByRole('button', { name: 'Open Care-Report.md' }) as HTMLButtonElement).disabled).toBe(true)
  },
}

export const RunAgain: Story = {
  // Two reads before the press (the page's mount and refresh), one inside it, then the run.
  ...story([careReports.blocked, careReports.blocked, careReports.blocked, careReports.running], {
    extra: [{ method: 'POST', path: careRunPath, respond: () => json({ schedule_id: `system-memory-curation@${CARE_WORKSPACE}`, status: 'ok' }, 201) }],
  }),
  play: async ({ canvas, userEvent }) => {
    await windowOf(canvas)
    await userEvent.click(await canvas.findByRole('button', { name: 'Run care again' }))
    await waitFor(() => expect(canvas.getByText('Care started.')).toBeTruthy())
    await assertFenceQuiet(canvas)
  },
}

export const Narrow: Story = {
  ...story(careReports.complete, { narrow: true }),
  play: async ({ canvas }) => {
    await windowOf(canvas)
    for (const name of ['Run care again', 'Open Care-Report.md']) {
      expect((await canvas.findByRole('button', { name })).getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    }
  },
}
