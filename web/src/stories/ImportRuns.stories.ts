import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import ImportRuns from '../components/ImportRuns.vue'
import { useProjectStore } from '../stores/projects'
import { mountConnected } from './mountConnected'
import type { FixtureRoute } from './fixtureBoundary'

type State = 'loaded' | 'empty' | 'loading' | 'failed' | 'stale' | 'queued' | 'running'
const workspace = 'storybook-alpha'
const batch = { batch_id: 'synthetic-batch', workspace, destination: 'memory', status: 'done', sources: [{ provider: 'claude_code', source_id: 'synthetic-session', status: 'done' }], progress: { total_sources: 1, completed_sources: 1, proposals_filed: 2, skipped: 0 }, provenance: [], created_at: '', updated_at: '', error: '' }
function routes(state: State): FixtureRoute[] {
  let reads = 0
  const running = { ...batch, status: 'running', progress: { ...batch.progress, total_sources: 2, completed_sources: 1, current_source_id: 'synthetic-session' } }
  return [{ method: 'GET', path: `/api/import/batches?workspace=${workspace}`, respond: () => {
    reads++
    if (state === 'loading') return new Promise<never>(() => {})
    const failed = state === 'failed' || (state === 'stale' && reads > 1)
    return new Response(JSON.stringify(failed ? { error: 'Synthetic read failure' } : { batches: state === 'empty' ? [] : [state === 'queued' ? { ...batch, status: 'queued' } : state === 'running' ? running : batch], retention_days: 30 }), { status: failed ? 503 : 200, headers: { 'Content-Type': 'application/json' } })
  } },
  { method: 'POST', path: '/api/import/batches/synthetic-batch/run', respond: () => new Response(JSON.stringify({ batch: running }), { headers: { 'Content-Type': 'application/json' } }) },
  { method: 'POST', path: '/api/import/batches/synthetic-batch/cancel', respond: () => new Response(JSON.stringify({ batch: { ...running, status: 'cancelled' } }), { headers: { 'Content-Type': 'application/json' } }) }]
}
function renderFixture() {
  return defineComponent({ setup() {
    const host = ref<HTMLElement>()
    let mounted: ReturnType<typeof mountConnected> | undefined
    onMounted(() => { mounted = mountConnected(host.value!, { component: ImportRuns, seed: pinia => { useProjectStore(pinia).activeWorkspace = workspace } }) })
    onBeforeUnmount(() => mounted?.dispose())
    return () => h('div', { ref: host, style: 'max-width: 700px' })
  } })
}
const meta = { id: 'connected-import-runs', title: 'Connected/Import runs', component: ImportRuns, tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } } } satisfies Meta<typeof ImportRuns>
export default meta
type Story = StoryObj<typeof meta>
const config = (state: State) => ({ fixture: { routes: routes(state) } })
export const Loaded: Story = { parameters: config('loaded'), render: renderFixture, play: async ({ canvas }) => {
  await canvas.findByRole('group', { name: 'Show imports' })
  await userEvent.click(canvas.getByRole('button', { name: 'Still open' }))
  await canvas.findByText(/No import matches/)
  await userEvent.click(canvas.getByRole('button', { name: 'Show all imports' }))
  expect(canvas.queryByText(/No import matches/)).toBeNull()
} }
export const Empty: Story = { parameters: config('empty'), render: renderFixture, play: async ({ canvas }) => { await canvas.findByText(/No imports filed/) } }
export const Loading: Story = { parameters: config('loading'), render: renderFixture, play: async ({ canvas }) => { await canvas.findByText(/Reading your import runs/) } }
export const Failed: Story = { parameters: config('failed'), render: renderFixture, play: async ({ canvas }) => { await canvas.findByRole('alert') } }
export const Stale: Story = { parameters: config('stale'), render: renderFixture, play: async ({ canvas }) => {
  await canvas.findByRole('group', { name: 'Show imports' })
  await userEvent.click(canvas.getByRole('button', { name: 'Check for progress' }))
  await waitFor(() => expect(canvas.getByText(/These runs may be out of date/)).toBeTruthy())
  expect(canvas.getByRole('group', { name: 'Show imports' })).toBeTruthy()
} }
export const Queued: Story = { parameters: config('queued'), render: renderFixture, play: async ({ canvas }) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Start extraction' }))
  expect(await canvas.findByRole('progressbar', { name: 'Extraction progress: 1 of 2 conversations' })).toBeTruthy()
  expect(canvas.getByRole('button', { name: 'Cancel' })).toBeTruthy()
} }
export const Running: Story = { parameters: config('running'), render: renderFixture, play: async ({ canvas }) => {
  expect(await canvas.findByRole('progressbar', { name: 'Extraction progress: 1 of 2 conversations' })).toBeTruthy()
  await userEvent.click(canvas.getByRole('button', { name: 'Cancel' }))
  await canvas.findByRole('button', { name: 'Remove this record' })
} }
