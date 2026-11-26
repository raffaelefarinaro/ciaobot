import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import ImportSources from '../components/ImportSources.vue'
import { useProjectStore } from '../stores/projects'
import { mountConnected } from './mountConnected'
import type { FixtureRoute } from './fixtureBoundary'

const workspace = 'storybook-alpha'
const source = { provider: 'claude_code', source_id: 'synthetic-session', project_hint: 'synthetic-project' }
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
type State = 'loaded' | 'empty' | 'loading' | 'failed' | 'stale' | 'excluded' | 'preview-error' | 'file-error'
function routes(state: State): FixtureRoute[] {
  let scans = 0
  return [
    { method: 'GET', path: `/api/import/batches?workspace=${workspace}`, respond: () => json({ batches: [], retention_days: 30 }) },
    { method: 'GET', path: `/api/import/sources?workspace=${workspace}`, respond: () => {
      scans++
      if (state === 'loading') return new Promise<never>(() => {})
      if (state === 'failed' || (state === 'stale' && scans > 1)) return json({ error: 'Synthetic scan failed' }, 503)
      return json({ sources: { available: state === 'empty' ? [] : [source], excluded: state === 'excluded' ? [{ ref: { provider: 'claude_code', source_id: 'excluded-session' }, reason: 'own', message: 'Ciaobot conversation' }] : [], unsupported: state === 'excluded' ? [{ provider: 'opencode', reason: 'unavailable', message: 'Source unavailable' }] : [], truncated: {} } })
    } },
    { method: 'POST', path: '/api/import/preview', respond: () => state === 'preview-error' ? json({ error: 'Synthetic preview failed' }, 503) : json({ preview: { conversations: [{ source, state: 'ready', message_count: 4, first_date: '', omitted: {}, already_imported: false }], provider: 'claude', model: 'synthetic-model', estimated_chars: 100, estimated_messages: 4, batch_cap: 10, destination: 'memory' } }) },
    { method: 'POST', path: '/api/import/batches', respond: () => json({ error: 'Synthetic filing failed' }, 503) },
  ]
}
function renderFixture() {
  return defineComponent({ setup() {
    const host = ref<HTMLElement>()
    let mounted: ReturnType<typeof mountConnected> | undefined
    onMounted(() => { mounted = mountConnected(host.value!, { component: ImportSources, seed: pinia => { useProjectStore(pinia).activeWorkspace = workspace } }) })
    onBeforeUnmount(() => mounted?.dispose())
    return () => h('div', { ref: host, style: 'max-width: 700px' })
  } })
}
const meta = { id: 'connected-import-sources', title: 'Connected/Import sources', component: ImportSources, tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } } } satisfies Meta<typeof ImportSources>
export default meta
type Story = StoryObj<typeof meta>
const config = (state: State) => ({ fixture: { routes: routes(state) } })
export const Selection: Story = { parameters: config('loaded'), render: renderFixture, play: async ({ canvas }) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Find conversations' }))
  await userEvent.click(await canvas.findByRole('checkbox'))
  await userEvent.click(canvas.getByRole('button', { name: 'Review 1 selected' }))
  await canvas.findByText('Before anything runs')
  expect(canvas.getByText(/synthetic-model/)).toBeTruthy()
} }
export const Empty: Story = { parameters: config('empty'), render: renderFixture, play: async ({ canvas }) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Find conversations' }))
  await canvas.findByText(/No past conversations found/)
} }
export const Loading: Story = { parameters: config('loading'), render: renderFixture, play: async ({ canvas }) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Find conversations' }))
  await canvas.findByText(/Looking for conversations on this computer/)
} }
export const Failed: Story = { parameters: config('failed'), render: renderFixture, play: async ({ canvas }) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Find conversations' }))
  await canvas.findByRole('alert')
} }
export const Stale: Story = { parameters: config('stale'), render: renderFixture, play: async ({ canvas }) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Find conversations' }))
  await canvas.findByRole('checkbox')
  await userEvent.click(canvas.getByRole('button', { name: 'Look again' }))
  await waitFor(() => expect(canvas.getByText(/These rows may be out of date/)).toBeTruthy())
  expect(canvas.getByRole('checkbox')).toBeTruthy()
} }
export const Excluded: Story = { parameters: config('excluded'), render: renderFixture, play: async ({ canvas }) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Find conversations' }))
  await userEvent.click(await canvas.findByText('Not importable (1)'))
  await userEvent.click(canvas.getByText('Sources unavailable (1)'))
  expect(canvas.getByText('Ciaobot conversation')).toBeTruthy()
  expect(canvas.getByText('Source unavailable')).toBeTruthy()
  expect(canvas.getAllByRole('checkbox')).toHaveLength(1)
} }
export const PreviewError: Story = { parameters: config('preview-error'), render: renderFixture, play: async ({ canvas }) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Find conversations' }))
  await userEvent.click(await canvas.findByRole('checkbox'))
  await userEvent.click(canvas.getByRole('button', { name: 'Review 1 selected' }))
  await canvas.findByRole('alert')
  expect(canvas.queryByText('Before anything runs')).toBeNull()
} }
export const FilingError: Story = { parameters: config('file-error'), render: renderFixture, play: async ({ canvas }) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Find conversations' }))
  await userEvent.click(await canvas.findByRole('checkbox'))
  await userEvent.click(canvas.getByRole('button', { name: 'Review 1 selected' }))
  await userEvent.click(await canvas.findByRole('button', { name: 'File an import of 1 conversation' }))
  await canvas.findByRole('alert')
  expect(canvas.getByText('Before anything runs')).toBeTruthy()
} }
