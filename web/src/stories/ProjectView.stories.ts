import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent } from 'storybook/test'
import { defineComponent, h } from 'vue'
import ProjectView from '../components/ProjectView.vue'
import { intakeProjectId, renderSeeded } from './homeIntakeFixture'
import { held, json } from './homeWindowsFixture'
import type { FixtureRoute } from './fixtureBoundary'
import type { Schedule } from '../lib/types'
import { useTaskStore } from '../stores/tasks'

type State = 'empty' | 'loading' | 'failed'
function routes(state: State): FixtureRoute[] {
  return [
    { method: 'GET', path: '/api/schedules', respond: () => state === 'loading' ? held() : state === 'failed' ? json({ error: 'Synthetic schedules unavailable' }, 503) : json([]) },
    { method: 'GET', path: `/api/projects/${intakeProjectId}/chats?archived=1&limit=10&offset=0`, respond: () => json({ items: [], total: 0, offset: 0, limit: 10 }) },
  ]
}
function fixture(projectId = intakeProjectId) { return defineComponent({ render: () => h(ProjectView, { projectId }) }) }
const meta = { id: 'connected-project-view', title: 'Connected/Project view', component: ProjectView, args: { projectId: intakeProjectId }, tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } } } satisfies Meta<typeof ProjectView>
export default meta
type Story = StoryObj<typeof meta>
export const Empty: Story = { parameters: { fixture: { routes: routes('empty') } }, render: () => renderSeeded(fixture(), { label: 'Empty project' }), play: async ({ canvas }) => {
  await canvas.findByText('No automations deliver prompts to this project.')
  expect(canvas.getByText(/No active chats in this project/)).toBeTruthy()
} }
export const Loading: Story = { parameters: { fixture: { routes: routes('loading') } }, render: () => renderSeeded(fixture(), { label: 'Loading automations' }), play: async ({ canvas }) => { await canvas.findByLabelText('Loading automations') } }
export const Failed: Story = { parameters: { fixture: { routes: routes('failed') } }, render: () => renderSeeded(fixture(), { label: 'Failed automation read' }), play: async ({ canvas }) => { await canvas.findByRole('alert'); expect(canvas.getByRole('button', { name: 'Retry' })).toBeTruthy() } }
export const Stale: Story = { parameters: { fixture: { routes: routes('failed') } }, render: () => renderSeeded(fixture(), { label: 'Retained automation', seed: pinia => {
  const store = useTaskStore(pinia)
  store.schedules = [{ schedule_id: 'synthetic-schedule', title: 'Synthetic routine', prompt: 'Review', web_project_id: intakeProjectId, web_chat_id: null, enabled: false, frequency: 'daily', daily_time_utc: '09:00', timezone_name: 'UTC', days_of_week: null } as Schedule]
  store.schedulesLoaded = true
} }), play: async ({ canvas }) => { await canvas.findByText('Synthetic routine'); await canvas.findByText(/Showing the last successful load/) } }
export const Missing: Story = { args: { projectId: 'synthetic-missing' }, parameters: { fixture: { routes: routes('empty') } }, render: ({ projectId }) => renderSeeded(fixture(projectId), { label: 'Missing project' }), play: async ({ canvas }) => { await canvas.findByText('Project not found.') } }
export const Archives: Story = { parameters: { fixture: { routes: routes('empty') } }, render: () => renderSeeded(fixture(), { label: 'Archived chats' }), play: async ({ canvas }) => { await userEvent.click(await canvas.findByRole('button', { name: 'Show archived chats' })); await canvas.findByText('No archived chats.') } }
