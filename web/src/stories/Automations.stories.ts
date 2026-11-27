import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef, type Component } from 'vue'
import type { Pinia } from 'pinia'
import NewScheduleForm from '../components/NewScheduleForm.vue'
import SchedulePanel from '../components/SchedulePanel.vue'
import WebhookTriggers from '../components/WebhookTriggers.vue'
import ProjectSidebar from '../components/ProjectSidebar.vue'
import { useTaskStore } from '../stores/tasks'
import { useProjectStore } from '../stores/projects'
import { useWebhookStore } from '../stores/webhooks'
import type { Schedule, WebhookTrigger } from '../lib/types'
import { mountConnected, type ConnectedFixture } from './mountConnected'
import { connectedRender, json, neverSettles } from './settingsGeneralFixture'

const meta = {
  id: 'automations',
  title: 'Tasks and automations/Automations',
  component: SchedulePanel,
  tags: ['!autodocs'],
  parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } },
} satisfies Meta<typeof SchedulePanel>
export default meta
type Story = StoryObj<typeof meta>

const schedules = (respond: () => Response | Promise<never>) => ({ method: 'GET' as const, path: '/api/schedules', respond })
const webhooks = (respond: () => Response | Promise<never>) => ({ method: 'GET' as const, path: '/api/webhooks?workspace=personal', respond })
const emptyHooks = webhooks(() => json({ triggers: [] }))
const panel = () => connectedRender({ component: SchedulePanel })
const form = () => connectedRender({ component: NewScheduleForm })
const hooks = () => connectedRender({ component: WebhookTriggers })
const schedule = {
  schedule_id: 'story-schedule', title: 'Morning briefing', prompt: 'Summarise the project.',
  daily_time_utc: '08:30', timezone_name: 'Europe/Zurich', frequency: 'daily',
  workspace: 'personal', enabled: true, next_run: '2099-01-01T08:30:00Z',
  web_project_id: null, web_chat_id: null, days_of_week: [], archive_policy: 'manual',
  chat_id: 0, created_at: '2026-01-01T00:00:00Z', last_triggered_on: '', thread_id: null,
  context_label: 'General', day_of_month: null, run_at_date: null, model: 'story-model',
  provider: 'claude', last_expected_run: null, missed: false, interval_minutes: 0,
} as Schedule
const trigger: WebhookTrigger = {
  trigger_id: 'story-trigger', name: 'Issue triage', workspace: 'personal', project_id: null,
  instructions: 'Triage the issue.', enabled: true, input_policy: 'event_text', revision: 3,
  created_at: '2026-03-01T09:00:00Z', updated_at: '2026-03-01T09:00:00Z',
}
const receiptRoute = { method: 'GET' as const, path: '/api/webhooks/story-trigger/receipts?workspace=personal', respond: () => json({ limit: 50, receipts: [{ receipt_id: 'story-receipt', trigger_id: 'story-trigger', trigger_name: 'Issue triage', status: 'launched', chat_id: 'story-chat', event_text: 'Build failed', detail: 'chat story-chat', created_at: '2026-03-04T09:00:00Z', updated_at: '2026-03-04T09:00:05Z' }] }) }
const loadedHooks = webhooks(() => json({ triggers: [trigger] }))

// This host keeps the real panel mounted when its overview link changes route,
// and can open on one of the three Automations pages.
const automationRoutes = (component: Component) => [
  { path: '/schedules', component },
  { path: '/schedules/:scheduleId', component },
  { path: '/automations/loops', component },
  { path: '/automations/webhooks', component },
]
function routedPanel(seed: (pinia: Pinia) => void, path = '/', width?: number) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      onMounted(() => {
        fixture.value = mountConnected(host.value!, { component: SchedulePanel, seed, routerRoutes: automationRoutes(SchedulePanel) })
        if (path !== '/') void fixture.value.router.replace(path)
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      // The pane's container, so the rows' narrow-pane rules apply as in the app.
      return () => h('div', { style: `container: chat-pane / inline-size;${width ? ` max-width: ${width}px` : ''}` }, [h('div', { ref: host })])
    },
  })
}
// The Automations sidebar section, in the drawer's width.
function routedSidebar(seed: (pinia: Pinia) => void, path = '/schedules') {
  const Sidebar = defineComponent({ render: () => h(ProjectSidebar, { collapsed: false, mode: 'schedules' }) })
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      onMounted(() => {
        fixture.value = mountConnected(host.value!, { component: Sidebar, seed, routerRoutes: automationRoutes(Sidebar) })
        void fixture.value.router.replace(path)
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', { style: 'width: 300px; height: 760px; display: flex' }, [h('div', { ref: host, style: 'flex: 1; display: flex' })])
    },
  })
}
const at = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString()
const seedFullWorkspace = (pinia: Pinia) => {
  const store = useTaskStore(pinia)
  store.schedules = [
    { ...schedule, schedule_id: 'care', title: 'Workspace care', scope: 'system', next_run: at(22 * 60), last_status: 'ok', last_dispatched_at: at(-120), last_run_chat_id: 'chat-care' },
    { ...schedule, schedule_id: 'brief', title: 'Morning briefing', next_run: at(9 * 60), last_status: 'ok', last_dispatched_at: at(-14 * 60), last_run_chat_id: 'chat-brief' },
    { ...schedule, schedule_id: 'running', title: 'Check whether the OpenAI plugin submission is accepted, and summarise what changed', next_run: at(19 * 60), last_status: 'running', last_dispatched_at: at(-5), last_run_chat_id: 'chat-run' },
    { ...schedule, schedule_id: 'missed', title: 'Weekly customer intel sweep', missed: true, last_expected_run: at(-7 * 60), next_run: at(16 * 60) },
    { ...schedule, schedule_id: 'once', title: 'Renew the domain', frequency: 'once', run_at_date: '2099-01-01', next_run: at(2 * 24 * 60) },
    { ...schedule, schedule_id: 'paused', title: 'Docs PR review', enabled: false, next_run: null },
    { ...schedule, schedule_id: 'loop', title: 'Check the nightly build', frequency: 'interval', interval_minutes: 5, web_chat_id: 'chat-loop', owner_chat_id: 'chat-loop', expires_at: at(90), max_runs: 12, run_count: 3, next_run: at(2), context_label: 'Fix flaky export test' },
  ] as Schedule[]
  store.schedulesLoaded = true
  useProjectStore(pinia).chats = [{ chat_id: 'chat-loop', title: 'Fix flaky export test' }] as never
}
const seedSchedules = (pinia: Pinia) => {
  const store = useTaskStore(pinia)
  store.schedules = [schedule, { ...schedule, schedule_id: 'paused', title: 'Weekly review', enabled: false }]
  store.schedulesLoaded = true
}

export const ScheduleLoading: Story = {
  parameters: { fixture: { routes: [schedules(neverSettles)] } }, render: panel,
  play: async ({ canvas }) => { expect(await canvas.findByRole('status', { name: 'Loading automations' })).toBeTruthy() },
}
export const ScheduleError: Story = {
  parameters: { fixture: { routes: [schedules(() => json({ error: 'Synthetic schedule failure' }, 503))] } }, render: panel,
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('alert')).toHaveTextContent('Synthetic schedule failure')
    expect(canvas.getByRole('button', { name: 'Retry' })).toBeTruthy()
  },
}
export const ScheduleEmpty: Story = {
  parameters: { fixture: { routes: [schedules(() => json([])), emptyHooks] } }, render: panel,
  play: async ({ canvas }) => {
    expect(await canvas.findByText('No routines yet')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'New routine' })).toBeTruthy()
  },
}
export const ScheduleStale: Story = {
  parameters: { fixture: { routes: [emptyHooks] } },
  render: () => connectedRender({ component: SchedulePanel, seed: (pinia) => {
    const store = useTaskStore(pinia)
    store.schedulesLoaded = true
    store.scheduleLoadError = 'Synthetic refresh failure'
  } }),
  play: async ({ canvas }) => {
    expect(await canvas.findByText(/Showing the last successful load/)).toBeTruthy()
    expect(canvas.getByText('No routines yet')).toBeTruthy()
  },
}
export const ScheduleOverviewAndDetail: Story = {
  parameters: { fixture: { routes: [emptyHooks] } },
  render: () => routedPanel(seedSchedules),
  play: async ({ canvas, userEvent }) => {
    const link = await canvas.findByRole('link', { name: /Morning briefing/ })
    expect(canvas.getByText('Weekly review')).toBeTruthy()
    await userEvent.click(link)
    expect(await canvas.findByRole('button', { name: 'Run now' })).toBeTruthy()
    expect(canvas.getByText('Summarise the project.')).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { name: 'Edit schedule' }))
    expect(canvas.getByRole('button', { name: 'Cancel' })).toBeTruthy()
  },
}
export const ScheduleStaleRows: Story = {
  parameters: { fixture: { routes: [emptyHooks] } },
  render: () => routedPanel((pinia) => { seedSchedules(pinia); useTaskStore(pinia).scheduleLoadError = 'Synthetic refresh failure' }),
  play: async ({ canvas }) => {
    expect(await canvas.findByText(/Showing the last successful load/)).toBeTruthy()
    expect(canvas.getByRole('link', { name: /Morning briefing/ })).toBeTruthy()
  },
}

export const SchedulesPage: Story = {
  parameters: { fixture: { routes: [emptyHooks] } },
  render: () => routedPanel(seedFullWorkspace),
  play: async ({ canvas }) => {
    const system = await canvas.findByRole('region', { name: 'System routines' })
    expect(system).toHaveTextContent('Workspace care')
    const yours = canvas.getByRole('region', { name: 'Your routines' })
    expect(yours).toHaveTextContent('paused')
    expect(yours).toHaveTextContent('once')
    expect(yours).not.toHaveTextContent('Check the nightly build')
    expect(canvas.getByRole('region', { name: 'Missed' })).toHaveTextContent('Weekly customer intel sweep did not run when it was due.')
    expect(canvas.getByRole('region', { name: 'Recent runs' })).toHaveTextContent('Morning briefing')
  },
}
export const SchedulesPageSystemOnly: Story = {
  parameters: { fixture: { routes: [emptyHooks] } },
  render: () => routedPanel((pinia) => {
    const store = useTaskStore(pinia)
    store.schedules = [{ ...schedule, schedule_id: 'care', title: 'Workspace care', scope: 'system' }]
    store.schedulesLoaded = true
  }),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('No routines yet')).toBeTruthy()
    expect(canvas.getByRole('region', { name: 'System routines' })).toHaveTextContent('Workspace care')
  },
}
export const SchedulesPagePhone: Story = {
  parameters: { fixture: { routes: [emptyHooks] } },
  render: () => routedPanel(seedFullWorkspace, '/', 390),
  play: async ({ canvas }) => {
    const nav = await canvas.findByRole('navigation', { name: 'Automations sections' })
    expect(nav.querySelector('[aria-current="page"]')).toHaveTextContent('Schedules')
  },
}
export const LoopsPage: Story = {
  parameters: { fixture: { routes: [emptyHooks] } },
  render: () => routedPanel(seedFullWorkspace, '/automations/loops'),
  play: async ({ canvas }) => {
    const loops = await canvas.findByRole('region', { name: 'Loops' })
    expect(loops).toHaveTextContent('In “Fix flaky export test” · every 5 min · ends')
    expect(loops).not.toHaveTextContent('Morning briefing')
  },
}
export const LoopsEmpty: Story = {
  parameters: { fixture: { routes: [emptyHooks] } },
  render: () => routedPanel(seedSchedules, '/automations/loops'),
  play: async ({ canvas }) => { expect(await canvas.findByText('No loops running')).toBeTruthy() },
}
export const WebhooksPage: Story = {
  parameters: { fixture: { routes: [loadedHooks, schedules(neverSettles)] } },
  render: () => routedPanel(() => {}, '/automations/webhooks'),
  play: async ({ canvas }) => {
    // The triggers do not wait on the automation list.
    expect(await canvas.findByText('Issue triage')).toBeTruthy()
    expect(canvas.queryByRole('status', { name: 'Loading automations' })).toBeNull()
  },
}
export const WebhooksPageEmpty: Story = {
  parameters: { fixture: { routes: [emptyHooks, schedules(neverSettles)] } },
  render: () => routedPanel(() => {}, '/automations/webhooks'),
  play: async ({ canvas }) => { expect(await canvas.findByText(/No webhook triggers yet/)).toBeTruthy() },
}
export const SidebarSection: Story = {
  // The sidebar's own reads that this section does not draw stay pending.
  parameters: { fixture: { routes: [loadedHooks, { method: 'GET', path: '/api/housekeeping', respond: neverSettles }, { method: 'GET', path: '/api/workspace-health', respond: neverSettles }, { method: 'GET', path: '/api/memory/unsure?workspace=personal', respond: neverSettles }] } },
  render: () => routedSidebar(seedFullWorkspace),
  play: async ({ canvas }) => {
    const nav = await canvas.findByRole('navigation', { name: 'Automations' })
    await waitFor(() => expect(nav.querySelector('a[href="/automations/webhooks"]')).toHaveAttribute('data-count', '1'))
    expect(nav.querySelector('a[aria-current="page"]')).toHaveAttribute('href', '/schedules')
    const nextUp = canvas.getByRole('group', { name: 'Next up' })
    expect(nextUp.querySelectorAll('.sidebar-row')).toHaveLength(5)
    expect(nextUp).not.toHaveTextContent('Docs PR review')
  },
}

export const NewSchedule: Story = {
  render: form,
  play: async ({ canvas, userEvent }) => {
    expect(await canvas.findByRole('form', { name: 'New automation' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Create automation' })).toBeDisabled()
    const frequency = canvas.getByLabelText('Repeats')
    await userEvent.selectOptions(frequency, 'interval')
    expect(canvas.getByLabelText('Every')).toBeTruthy()
    await userEvent.selectOptions(frequency, 'once')
    expect(canvas.getByLabelText('On')).toBeTruthy()
    await userEvent.selectOptions(frequency, 'monthly')
    expect(canvas.getByLabelText('Day of month')).toBeTruthy()
    await userEvent.selectOptions(frequency, 'weekly')
    await userEvent.click(canvas.getByRole('button', { name: 'Monday' }))
    expect(canvas.getByRole('button', { name: 'Monday' })).toHaveAttribute('aria-pressed', 'true')
  },
}
export const NewScheduleModelAndArchive: Story = {
  parameters: { fixture: { routes: [{ method: 'GET', path: '/api/models?workspace=personal', respond: () => json({ models: ['story-model'], default: '', providers: [{ id: 'claude', label: 'Claude', short_label: 'Claude Code', capabilities: {} }], provider_models: { claude: ['story-model'] } }) }] } },
  render: () => connectedRender({ component: NewScheduleForm, seed: (pinia) => {
    const projects = useProjectStore(pinia)
    projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
    projects.workspaceRegistryStatus = 'ready'
    projects.projects = [{ project_id: 'story-project', name: 'General', workspace: 'personal', is_auto: true }] as typeof projects.projects
  } }),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await waitFor(() => expect(canvas.getByText(/Automation models loaded/)).toBeTruthy())
    const picker = canvasElement.querySelector<HTMLButtonElement>('.model-selector__trigger')!
    await userEvent.click(picker)
    await waitFor(() => expect(document.querySelector('[role="option"][data-model="story-model"]')).toBeTruthy())
    await userEvent.click(document.querySelector<HTMLElement>('[role="option"][data-model="story-model"]')!)
    expect(picker.textContent?.toLowerCase()).toContain('story')
    await userEvent.click(canvas.getByText('Archive if nothing to judge'))
    expect(canvas.getByRole('radio', { name: 'Archive if nothing to judge' })).toBeChecked()
  },
}

export const WebhookLoading: Story = {
  parameters: { fixture: { routes: [webhooks(neverSettles)] } }, render: hooks,
  play: async ({ canvas }) => { expect(await canvas.findByRole('status', { name: 'Loading webhook triggers' })).toBeTruthy() },
}
export const WebhookError: Story = {
  parameters: { fixture: { routes: [webhooks(() => json({ error: 'Synthetic webhook failure' }, 503))] } }, render: hooks,
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('alert')).toHaveTextContent('Synthetic webhook failure')
    expect(canvas.getByRole('button', { name: 'Retry' })).toBeTruthy()
  },
}
export const WebhookEmptyAndForm: Story = {
  parameters: { fixture: { routes: [emptyHooks] } }, render: hooks,
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText(/No webhook triggers yet/)).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: 'New trigger' }))
    expect(canvas.getByRole('button', { name: 'Create trigger' })).toBeDisabled()
    await userEvent.type(canvas.getByLabelText('Name'), 'Issue triage')
    expect(canvas.getByRole('button', { name: 'Create trigger' })).toBeEnabled()
    await userEvent.click(canvas.getAllByRole('button', { name: 'Cancel' })[0])
    expect(canvas.queryByLabelText('Name')).toBeNull()
  },
}
export const WebhookRowsAndHistory: Story = {
  parameters: { fixture: { routes: [loadedHooks, receiptRoute] } }, render: hooks,
  play: async ({ canvas, userEvent }) => {
    expect(await canvas.findByText('Issue triage')).toBeTruthy()
    expect(canvas.getByText('Enabled')).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { name: 'History' }))
    expect(await canvas.findByText('Build failed')).toBeTruthy()
    expect(canvas.getByText('Launched')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Open the chat this event became' })).toBeTruthy()
  },
}
export const WebhookStaleRows: Story = {
  parameters: { fixture: { routes: [webhooks(() => json({ error: 'Synthetic refresh failure' }, 503))] } },
  render: () => connectedRender({ component: WebhookTriggers, seed: (pinia) => {
    const store = useWebhookStore(pinia)
    store.triggers = [trigger]
    store.loadedWorkspace = 'personal'
    store.loadError = 'Synthetic refresh failure'
  } }),
  play: async ({ canvas }) => {
    expect(await canvas.findByText(/Showing the last successful load/)).toBeTruthy()
    expect(canvas.getByText('Issue triage')).toBeTruthy()
  },
}
export const WebhookWriteError: Story = {
  parameters: { fixture: { routes: [loadedHooks, { method: 'PATCH', path: '/api/webhooks/story-trigger', respond: () => json({ error: 'Synthetic revision conflict' }, 409) }] } }, render: hooks,
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Disable' }))
    expect(await canvas.findByRole('alert')).toHaveTextContent('Synthetic revision conflict')
    expect(canvas.getByText('Issue triage')).toBeTruthy()
    expect(canvas.getByText('Enabled')).toBeTruthy()
  },
}
