import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import HousekeepingStrip from '../components/HousekeepingStrip.vue'
import type {
  HousekeepingResponse,
  HostTerminalCapabilities,
  OperatorAction,
  WorkspaceHealthResponse,
} from '../lib/types'
import type { FixtureRoute } from './fixtureBoundary'
import { assertFenceQuiet, held, homeWorkspace, json, renderHome } from './homeWindowsFixture'
import type { CareReport } from '../lib/careReport'
import { careReports, careRoute, careRunRoutes } from './careFixture'

type Canvas = Parameters<NonNullable<StoryObj['play']>>[0]['canvas']

const terminal: HostTerminalCapabilities = {
  available: true,
  platform: 'mac',
  reason: '',
  terminal: 'Terminal',
  actions: ['skills-update'],
}

function action(id: string, overrides: Partial<OperatorAction> = {}): OperatorAction {
  return {
    id,
    kind: 'skills-update',
    severity: 2,
    title: `Synthetic ${id} title`,
    detail: `Synthetic ${id} detail`,
    glyph: 'star',
    blocking: false,
    workspace: homeWorkspace,
    run_label: `Run ${id}`,
    chat_label: '',
    chat_prompt: '',
    view_label: '',
    view_route: '',
    ...overrides,
  }
}

function housekeeping(actions: OperatorAction[] = []): HousekeepingResponse {
  return { actions }
}

function health(status: WorkspaceHealthResponse['status'], detail: string): WorkspaceHealthResponse {
  return {
    status,
    checks: [{
      id: 'storybook-check',
      title: 'Synthetic workspace check',
      status,
      detail,
      path: '/workspace/storybook-alpha',
      action: '',
    }],
  }
}

const okHealth = health('ok', 'Synthetic workspace checks passed')

type FixtureOptions = {
  housekeeping?: HousekeepingResponse
  health?: WorkspaceHealthResponse
  holdHousekeeping?: boolean
  holdFix?: boolean
  holdRun?: boolean
  runId?: string
  dismissId?: string
  /** The care read; a clean run (no row) unless a story names another. */
  care?: CareReport | CareReport[]
  careRun?: boolean
}

function routes(options: FixtureOptions = {}): FixtureRoute[] {
  const list: FixtureRoute[] = [
    { method: 'GET', path: '/api/housekeeping', respond: () => options.holdHousekeeping ? held() : json(options.housekeeping ?? housekeeping()) },
    { method: 'GET', path: '/api/workspace-health', respond: () => json(options.health ?? okHealth) },
    { method: 'GET', path: '/api/host/terminal', respond: () => json(terminal) },
    {
      method: 'POST',
      path: '/api/workspace-health/fix',
      respond: () => options.holdFix ? held() : json(okHealth),
    },
    { method: 'POST', path: '/api/host/terminal', respond: () => json({ ok: true, platform: 'mac', launched: 'Terminal' }) },
    careRoute(options.care ?? careReports.complete),
  ]
  if (options.careRun) list.push(...careRunRoutes())
  if (options.runId) {
    list.push({
      method: 'POST',
      path: `/api/housekeeping/${options.runId}/run`,
      respond: () => options.holdRun ? held() : json({ ok: true, actions: [], summary: 'Synthetic run completed' }),
    })
  }
  if (options.dismissId) {
    list.push({ method: 'POST', path: `/api/housekeeping/${options.dismissId}/dismiss`, respond: () => json({ ok: true, actions: [], summary: 'Synthetic dismiss completed' }) })
  }
  return list
}

function component(narrow = false) {
  return renderHome(HousekeepingStrip, {
    label: 'Housekeeping workshop: one synthetic workspace and synthetic rows.',
    workspace: homeWorkspace,
    narrow,
  })
}

function story(routeList: FixtureRoute[], narrow = false) {
  const owner = component(narrow)
  return {
    parameters: { fixture: { routes: routeList } },
    render: () => ({ components: { owner }, template: '<owner />' }),
  }
}

async function settled(canvas: Canvas): Promise<void> {
  await waitFor(() => expect(canvas.queryByRole('heading', { name: 'Needs you' })).not.toBeNull())
  await assertFenceQuiet(canvas, 3)
}

async function settledEmpty(canvas: Canvas): Promise<void> {
  await waitFor(() => {
    expect(canvas.getByLabelText('Fixture ledger').textContent).toMatch(/Fixture requests: [3-9]\d*\./)
  })
  expect(canvas.queryByRole('heading', { name: 'Needs you' })).toBeNull()
  await assertFenceQuiet(canvas, 3)
}

const meta = {
  id: 'housekeeping-strip',
  title: 'Home windows/HousekeepingStrip',
  component: HousekeepingStrip,
  tags: ['!autodocs'],
  parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } },
} satisfies Meta<typeof HousekeepingStrip>

export default meta

type Story = StoryObj<typeof meta>

export const Empty: Story = {
  ...story(routes()),
  play: async ({ canvas }) => { await settledEmpty(canvas) },
}

export const InitialLoading: Story = {
  ...story(routes({ holdHousekeeping: true })),
  play: async ({ canvas }) => {
    await waitFor(() => {
      expect(canvas.getByLabelText('Fixture ledger').textContent).toMatch(/Fixture requests: [1-9]\d*\./)
    })
    expect(canvas.queryByRole('heading', { name: 'Needs you' })).toBeNull()
    await assertFenceQuiet(canvas, 3)
  },
}

export const HealthError: Story = {
  ...story(routes({ health: health('error', 'Synthetic vault is not writable') })),
  play: async ({ canvas }) => {
    await settled(canvas)
    expect(canvas.getByText('Synthetic workspace check')).toBeTruthy()
    expect(canvas.getByText('Synthetic vault is not writable')).toBeTruthy()
    await assertFenceQuiet(canvas)
  },
}

export const HealthFixBusy: Story = {
  ...story(routes({ health: health('error', 'Synthetic vault is not writable'), holdFix: true })),
  play: async ({ canvas, userEvent }) => {
    await settled(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'Fix' }))
    const fixing = canvas.getByRole('button', { name: 'Fixing…' })
    expect(fixing).toHaveProperty('disabled', true)
    await assertFenceQuiet(canvas)
  },
}

export const ActionRunBusy: Story = {
  ...story(routes({
    housekeeping: housekeeping([action('storybook-run', { run_label: 'Run synthetic action' })]),
    runId: 'storybook-run',
    holdRun: true,
  })),
  play: async ({ canvas, userEvent }) => {
    await settled(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'Run synthetic action' }))
    const running = await waitFor(() => canvas.getByRole('button', { name: 'Running…' }))
    expect(running).toHaveProperty('disabled', true)
    await assertFenceQuiet(canvas)
  },
}

export const ActionDismiss: Story = {
  ...story(routes({
    housekeeping: housekeeping([action('storybook-dismiss', { run_label: '', dismiss_label: 'Not now' })]),
    dismissId: 'storybook-dismiss',
  })),
  play: async ({ canvas, userEvent }) => {
    await settled(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'Not now' }))
    await waitFor(() => expect(canvas.queryByText('Synthetic storybook-dismiss title')).toBeNull())
    await assertFenceQuiet(canvas)
  },
}

export const ActionTerminal: Story = {
  ...story(routes({
    housekeeping: housekeeping([action('storybook-terminal', { run_label: '', terminal_action: 'skills-update' })]),
  })),
  play: async ({ canvas, userEvent }) => {
    await settled(canvas)
    const lead = canvas.getByRole('button', { name: 'Synthetic storybook-terminal title in Terminal' })
    await userEvent.click(lead)
    await waitFor(() => expect(canvas.getByText('Opened in Terminal')).toBeTruthy())
    await assertFenceQuiet(canvas)
  },
}

export const Narrow: Story = {
  ...story(routes({ housekeeping: housekeeping([action('storybook-narrow', { run_label: 'Run narrow' })]) }), true),
  play: async ({ canvas }) => {
    await settled(canvas)
    const wrapper = canvas.getByLabelText('Fixture ledger').parentElement
    expect(wrapper?.style.maxWidth).toBe('390px')
    expect(canvas.getByRole('button', { name: 'Run narrow' })).toBeTruthy()
    await assertFenceQuiet(canvas)
  },
}

// ── Workspace care ─────────────────────────────────────────────────────────

export const CareNeedsYou: Story = {
  ...story(routes({ care: careReports.needsYou })),
  play: async ({ canvas }) => {
    await settled(canvas)
    expect(canvas.getByText('Care needs you')).toBeTruthy()
    expect(canvas.getByText('Needs attention:')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Open report' })).toBeTruthy()
    await assertFenceQuiet(canvas)
  },
}

export const CareDidNotFinish: Story = {
  ...story(routes({ care: careReports.blocked })),
  play: async ({ canvas }) => {
    await settled(canvas)
    expect(canvas.getByText('Care did not finish')).toBeTruthy()
    expect(canvas.getByText('4 of 6 steps done. Blocked: Hygiene, Guide review.')).toBeTruthy()
    expect(canvas.getByText('Error:')).toBeTruthy()
    await assertFenceQuiet(canvas)
  },
}

export const CareCutOff: Story = {
  ...story(routes({ care: careReports.interrupted })),
  play: async ({ canvas }) => {
    await settled(canvas)
    expect(canvas.getByText(/^Care was cut off/)).toBeTruthy()
    await assertFenceQuiet(canvas)
  },
}

export const CareRunningRendersNothing: Story = {
  ...story(routes({ care: careReports.running })),
  play: async ({ canvas }) => { await settledEmpty(canvas) },
}

export const CareNeverRunRendersNothing: Story = {
  ...story(routes({ care: careReports.never })),
  play: async ({ canvas }) => { await settledEmpty(canvas) },
}

export const CareRunAgain: Story = {
  // The first read and the one inside the press, then the run in progress.
  ...story(routes({ care: [careReports.blocked, careReports.blocked, careReports.running], careRun: true })),
  play: async ({ canvas, userEvent }) => {
    await settled(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'More actions for Care did not finish' }))
    await userEvent.click(canvas.getByRole('menuitem', { name: 'Run care again' }))
    await waitFor(() => expect(canvas.getByText('Care started.')).toBeTruthy())
    await assertFenceQuiet(canvas)
  },
}

export const CareNarrow: Story = {
  ...story(routes({ care: careReports.needsYou }), true),
  play: async ({ canvas }) => {
    await settled(canvas)
    const lead = canvas.getByRole('button', { name: 'Open report' })
    expect(lead.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    await assertFenceQuiet(canvas)
  },
}
