import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import ProjectDecisionEntry from '../components/ProjectDecisionEntry.vue'
import { held, json, route } from './settingsAccessFixture'

const endpoint = '/api/workspaces/storybook-alpha/share-evidence'
const meta = {
  id: 'project-decision-entry', title: 'Projects/ProjectDecisionEntry', component: ProjectDecisionEntry,
  args: { workspace: 'storybook-alpha', projectId: 'alpha' },
  tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } },
} satisfies Meta<typeof ProjectDecisionEntry>
export default meta
type Story = StoryObj<typeof meta>

export const Closed: Story = { play: async ({ canvas }) => {
  expect(canvas.getByRole('button', { name: 'Record decision' })).toBeEnabled()
} }
export const Loading: Story = {
  parameters: { fixture: { routes: [route('GET', endpoint, held)] } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { name: 'Record decision' }))
    expect(await canvas.findByRole('status')).toHaveTextContent('Loading sharing permissions')
  },
}
export const Failure: Story = {
  parameters: { fixture: { routes: [route('GET', endpoint, () => json({ error: 'Synthetic failure' }, 500))] } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { name: 'Record decision' }))
    expect(await canvas.findByRole('alert')).toHaveTextContent('Synthetic failure')
    expect(canvas.getByRole('button', { name: 'Retry' })).toBeEnabled()
  },
}
export const Ready: Story = {
  parameters: { fixture: { routes: [route('GET', endpoint, () => json({ grants: [], disclosure_tokens: { alpha: 'synthetic-token' } }))] } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { name: 'Record decision' }))
    await waitFor(() => expect(canvas.getByText('No standing permission is recorded for this project.')).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Save decision' })).toBeDisabled()
  },
}

const disclosure = route('GET', endpoint, () => json({ grants: [], disclosure_tokens: { alpha: 'synthetic-token' } }))
async function fillDecision(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas'], userEvent: Parameters<NonNullable<Story['play']>>[0]['userEvent']) {
  await userEvent.click(canvas.getByRole('button', { name: 'Record decision' }))
  await userEvent.type(await canvas.findByLabelText('Decision'), 'Use the shared release process')
  await userEvent.type(canvas.getByLabelText('Effective date'), '2026-11-26')
}
export const Saved: Story = {
  parameters: { fixture: { routes: [disclosure, route('POST', `${endpoint}/decisions`, () => json({}))] } },
  play: async ({ canvas, userEvent }) => {
    await fillDecision(canvas, userEvent)
    await userEvent.click(canvas.getByRole('button', { name: 'Save decision' }))
    expect(await canvas.findByText('Decision recorded.')).toBeTruthy()
    expect(canvas.getByLabelText('Decision')).toHaveValue('')
  },
}
export const SaveFailure: Story = {
  parameters: { fixture: { routes: [disclosure, route('POST', `${endpoint}/decisions`, () => json({ error: 'Synthetic write refused' }, 500))] } },
  play: async ({ canvas, userEvent }) => {
    await fillDecision(canvas, userEvent)
    await userEvent.click(canvas.getByRole('button', { name: 'Save decision' }))
    expect(await canvas.findByRole('alert')).toHaveTextContent('Synthetic write refused')
    expect(canvas.getByLabelText('Decision')).toHaveValue('Use the shared release process')
  },
}
export const Saving: Story = {
  parameters: { fixture: { routes: [disclosure, route('POST', `${endpoint}/decisions`, held)] } },
  play: async ({ canvas, userEvent }) => {
    await fillDecision(canvas, userEvent)
    await userEvent.click(canvas.getByRole('button', { name: 'Save decision' }))
    expect(canvas.getByRole('button', { name: 'Saving…' })).toBeDisabled()
    expect(canvas.getByLabelText('Decision')).toBeDisabled()
  },
}
