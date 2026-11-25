import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor, within } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import CareRunOutcome from '../components/CareRunOutcome.vue'
import { mountConnected } from './mountConnected'
import type { FixtureRoute } from './fixtureBoundary'

/**
 * The Automations care card (#3648): the production CareRunOutcome.vue mounted
 * in a fresh Pinia and memory router, fed one exact-dispatch answer by the
 * declared route. Synthetic keys, counts and workspace names only.
 */

const SCHEDULE = 'system-memory-curation@storybook-alpha'
const DISPATCH = 'dispatch-storybook'
const WORKSPACE = 'storybook-alpha'
const PROPOSAL = `prv_${'d'.repeat(64)}`
const PATH = `/api/schedules/system-memory-curation%40storybook-alpha/care-run?dispatch_id=${DISPATCH}`

const COUNTS = { planned: 6, completed: 2, deferred: 1, failed: 1 }
const item = (status: string, detail: string, item_key: string) => ({ item_key, pass_id: 'pass-1', status, detail, receipt_ids: [] })

const OK = {
  state: 'ok',
  care_run: { run_id: 'run-storybook', ...COUNTS, counts_complete: true, reasons: [] },
  report_available: true,
  job_status: 'ok',
  run_id: 'run-storybook',
  counts: COUNTS,
  outcomes: [
    item('applied', 'filed', 'Notes/applied-one'),
    item('applied', 'filed', 'Notes/applied-two'),
    item('failed', 'The write was refused', 'Notes/refused'),
    item('blocked', 'The note is locked by another run', 'Notes/locked'),
    item('deferred_budget', 'Left for the next run', 'Notes/later'),
    item('queued', 'Waiting for your decision', PROPOSAL),
  ],
  weekly: { marker: 'not_advanced' },
}
const NONE = { care_run: null, report_available: false }

function route(respond: FixtureRoute['respond']): { fixture: { routes: FixtureRoute[] } } {
  return { fixture: { routes: [{ method: 'GET', path: PATH, respond }] } }
}
const answer = (body: unknown, status = 200) => route(() =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))

const Inert = defineComponent({ render: () => h('span') })

function render(narrow = false) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountConnected>>()
      const failure = ref('')
      onMounted(() => {
        if (!host.value) return
        try {
          fixture.value = mountConnected(host.value, {
            component: defineComponent({
              render: () => h(CareRunOutcome, { scheduleId: SCHEDULE, chatId: 'chat-storybook', dispatchId: DISPATCH, workspace: WORKSPACE }),
            }),
            routerRoutes: [
              { path: '/chat/:chatId', component: Inert },
              { path: '/memory/:section', component: Inert },
            ],
          })
        } catch (cause) {
          failure.value = String(cause)
        }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', narrow ? { style: 'max-width: 390px' } : {}, [
        h('div', { ref: host }),
        failure.value ? h('p', { role: 'alert' }, failure.value) : null,
      ])
    },
  })
}

const meta = {
  id: 'care-run-outcome',
  title: 'Tasks, projects and automations/CareRunOutcome',
  component: CareRunOutcome,
  args: { scheduleId: SCHEDULE, chatId: 'chat-storybook', dispatchId: DISPATCH, workspace: WORKSPACE },
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
  render: () => render(),
} satisfies Meta<typeof CareRunOutcome>
export default meta
type Story = StoryObj<typeof meta>

const body = (el: HTMLElement) => within(el)

export const Loading: Story = {
  parameters: route(() => new Promise<never>(() => {})),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(body(canvasElement).getByRole('status').textContent).toContain('Checking the outcome'))
  },
}

export const Ok: Story = {
  parameters: answer(OK),
  play: async ({ canvasElement }) => {
    const c = body(canvasElement)
    await waitFor(() => expect(c.getByText('Planned 6 · completed 2 · deferred 1 · failed 1')).toBeTruthy())
    expect(c.getByText('Failed (1)')).toBeTruthy()
    expect(c.getByText('Queued for review (1)')).toBeTruthy()
    expect(c.getByText(/did not advance/)).toBeTruthy()
    expect(c.getByRole('link', { name: 'Open the run chat' }).getAttribute('href')).toBe('/chat/chat-storybook')
    expect(c.getByRole('link', { name: 'Open in Memory Review' }).getAttribute('href'))
      .toBe(`/memory/review?show=suggested&workspace=${WORKSPACE}&proposal=${PROPOSAL}`)
  },
}

export const NoOp: Story = {
  parameters: answer({
    ...OK,
    counts: { planned: 0, completed: 0, deferred: 0, failed: 0 },
    outcomes: [item('no_change', '', 'Notes/steady')],
    weekly: { marker: 'advanced' },
  }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(body(canvasElement).getByText('No change needed.')).toBeTruthy())
  },
}

export const JobFailed: Story = {
  parameters: answer({ ...OK, job_status: 'error' }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(body(canvasElement).getByText('This run failed.')).toBeTruthy())
  },
}

export const IncompleteWithNotes: Story = {
  parameters: answer({
    ...OK,
    care_run: { run_id: 'run-storybook', ...COUNTS, counts_complete: false, reasons: ['1 accept(s) failed; the run\'s receipts say which'] },
  }),
  play: async ({ canvasElement }) => {
    const c = body(canvasElement)
    await waitFor(() => expect(c.getByText(/Incomplete: some work/)).toBeTruthy())
    expect(c.getByText(/accept\(s\) failed/)).toBeTruthy()
  },
}

export const NoItemDetail: Story = {
  parameters: answer({ ...OK, care_run: null, report_available: false, outcomes: undefined, weekly: undefined }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(body(canvasElement).getByText('Per-item detail was not recorded for this run.')).toBeTruthy())
  },
}

export const Pending: Story = {
  parameters: answer({ state: 'pending', ...NONE }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(body(canvasElement).getByText(/still running, or its outcome is not recorded yet/)).toBeTruthy())
  },
}

export const Superseded: Story = {
  parameters: answer({ state: 'superseded', ...NONE }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(body(canvasElement).getByText(/A newer run replaced this one/)).toBeTruthy())
  },
}

export const Ambiguous: Story = {
  parameters: answer({ state: 'ambiguous', ...NONE }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(body(canvasElement).getByText(/records for this run disagree/)).toBeTruthy())
  },
}

export const Unavailable: Story = {
  parameters: answer({ state: 'unavailable', ...NONE }),
  play: async ({ canvasElement }) => {
    const c = body(canvasElement)
    await waitFor(() => expect(c.getByText(/cannot be read right now/)).toBeTruthy())
    expect(c.getByRole('button', { name: 'Retry' })).toBeTruthy()
  },
}

export const ErrorWithRetry: Story = {
  parameters: answer({ error: 'Synthetic failure' }, 500),
  play: async ({ canvasElement }) => {
    const c = body(canvasElement)
    await waitFor(() => expect(c.getByRole('alert').textContent).toContain('Could not load the outcome'))
    expect(c.getByRole('button', { name: 'Retry' })).toBeTruthy()
  },
}

export const Narrow: Story = {
  parameters: answer(OK),
  render: () => render(true),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(body(canvasElement).getByText('Failed (1)')).toBeTruthy())
  },
}

export const KeyboardFocus: Story = {
  parameters: answer(OK),
  play: async ({ canvasElement }) => {
    const c = body(canvasElement)
    const link = await waitFor(() => c.getByRole('link', { name: 'Open in Memory Review' }))
    await userEvent.tab()
    expect(canvasElement.contains(document.activeElement)).toBe(true)
    link.focus()
    expect(document.activeElement).toBe(link)
    expect(getComputedStyle(link).outlineStyle).not.toBe('none')
  },
}
