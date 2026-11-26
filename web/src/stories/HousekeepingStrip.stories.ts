import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import HousekeepingStrip from '../components/HousekeepingStrip.vue'
import type {
  HousekeepingResponse,
  HostTerminalCapabilities,
  OperatorAction,
  UpdateTaskRow,
  UpdateTasksResponse,
  WorkspaceHealthResponse,
} from '../lib/types'
import type { FixtureRoute } from './fixtureBoundary'
import { assertFenceQuiet, held, homeWorkspace, json, renderHome } from './homeWindowsFixture'

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

function task(overrides: Partial<UpdateTaskRow> = {}): UpdateTaskRow {
  return {
    id: 'storybook-task',
    revision: 3,
    scope: 'workspace',
    title: 'Synthetic update task title',
    why: 'Synthetic reason for the update task',
    since_version: '0.0.0',
    status: 'offered',
    applicability: 'applicable',
    applicability_checked_at: '2026-01-01T00:00:00Z',
    offered: true,
    suppressed: false,
    chat_id: '',
    chat_live: false,
    prompt_digest: '',
    attempted_fingerprint: '',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function housekeeping(actions: OperatorAction[] = []): HousekeepingResponse {
  return { actions }
}

function updates(rows: UpdateTaskRow[] = []): UpdateTasksResponse {
  return { tasks: rows }
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
const query = `workspace=${homeWorkspace}`

type FixtureOptions = {
  housekeeping?: HousekeepingResponse
  health?: WorkspaceHealthResponse
  updates?: UpdateTasksResponse
  holdUpdates?: boolean
  holdFix?: boolean
  holdRun?: boolean
  holdStart?: boolean
  runId?: string
  dismissId?: string
}

function routes(options: FixtureOptions = {}): FixtureRoute[] {
  const list: FixtureRoute[] = [
    { method: 'GET', path: '/api/housekeeping', respond: () => json(options.housekeeping ?? housekeeping()) },
    { method: 'GET', path: '/api/workspace-health', respond: () => json(options.health ?? okHealth) },
    { method: 'GET', path: '/api/host/terminal', respond: () => json(terminal) },
    {
      method: 'GET',
      path: `/api/update-tasks?${query}`,
      respond: () => options.holdUpdates ? held() : json(options.updates ?? updates()),
    },
    {
      method: 'POST',
      path: '/api/workspace-health/fix',
      respond: () => options.holdFix ? held() : json(okHealth),
    },
    { method: 'POST', path: '/api/host/terminal', respond: () => json({ ok: true, platform: 'mac', launched: 'Terminal' }) },
  ]
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
  // Every eligible offered task triggers the store's preview read even though
  // the owner does not render the result; declare it for all task stories.
  if ((options.updates?.tasks ?? []).some(row =>
    row.offered && !row.suppressed && row.status === 'offered' && row.applicability === 'applicable')) {
    const preview = json({
      task_id: 'storybook-task',
      revision: 3,
      workspace: homeWorkspace,
      state: 'review',
      summary: 'Synthetic preview summary',
      plan: null,
      checked_at: '2026-01-01T00:00:00Z',
    })
    list.push({ method: 'GET', path: `/api/update-tasks/storybook-task/preview?${query}`, respond: () => preview })
    list.push({ method: 'GET', path: `/api/update-tasks/storybook-task/preview?${query}&fresh=1`, respond: () => preview })
  }
  list.push({
    method: 'POST',
    path: `/api/update-tasks/storybook-task/start?${query}`,
    respond: () => options.holdStart
      ? held()
      : json({ ok: true, task_id: 'storybook-task', chat_id: 'storybook-chat', resumed: false, tasks: [task({ status: 'in_progress', chat_live: true, chat_id: 'storybook-chat' })] }),
  })
  list.push({
    method: 'POST',
    path: `/api/update-tasks/storybook-task/dismiss?${query}`,
    respond: () => json({ ok: true, task_id: 'storybook-task', tasks: [] }),
  })
  return list
}

function component(narrow = false) {
  return renderHome(HousekeepingStrip, {
    label: 'Housekeeping workshop: one synthetic workspace and synthetic rows.',
    workspace: homeWorkspace,
    narrow,
    confirm: true,
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
  ...story(routes({ holdUpdates: true })),
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

export const TaskOffered: Story = {
  ...story(routes({ updates: updates([task()]) })),
  play: async ({ canvas }) => {
    await settled(canvas)
    expect(canvas.getByText('Synthetic update task title')).toBeTruthy()
    expect(canvas.getByText('Synthetic reason for the update task')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Start in chat' })).toBeTruthy()
    await assertFenceQuiet(canvas)
  },
}

export const TaskStartBusy: Story = {
  ...story(routes({ updates: updates([task()]), holdStart: true })),
  play: async ({ canvas, userEvent }) => {
    await settled(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'Start in chat' }))
    const working = await waitFor(() => canvas.getByRole('button', { name: 'Working…' }))
    expect(working).toHaveProperty('disabled', true)
    await assertFenceQuiet(canvas)
  },
}

export const TaskInProgress: Story = {
  ...story(routes({ updates: updates([task({ status: 'in_progress', chat_live: true, chat_id: 'storybook-chat' })]) })),
  play: async ({ canvas }) => {
    await settled(canvas)
    expect(canvas.getByRole('button', { name: 'Resume chat' })).toBeTruthy()
    expect(canvas.getByText('Started. Its chat is open.')).toBeTruthy()
    await assertFenceQuiet(canvas)
  },
}

export const TaskUnknownApplicability: Story = {
  ...story(routes({ updates: updates([task({ applicability: 'unknown' })]) })),
  play: async ({ canvas }) => {
    await settled(canvas)
    expect(canvas.getByText('Checking whether this applies here…')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Check again' })).toBeTruthy()
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
