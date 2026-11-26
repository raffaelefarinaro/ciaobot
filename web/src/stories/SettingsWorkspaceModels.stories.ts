import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsWorkspaceModels from '../components/settings/SettingsWorkspaceModels.vue'
import { held, json, route } from './settingsAccessFixture'
import { workspacePanel } from './workspaceAdminFixture'

const meta = { id: 'settings-workspace-models', title: 'Settings/SettingsWorkspaceModels', component: SettingsWorkspaceModels,
  tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } } } satisfies Meta<typeof SettingsWorkspaceModels>
export default meta
type Story = StoryObj<typeof meta>
const registry = route('GET', '/api/workspaces', () => json({ workspaces: [{ name: 'storybook-alpha', vault_root: '/storybook/alpha', default_provider: 'claude' }], primary: 'storybook-alpha' }))
const routines = route('GET', '/api/settings/routines', held)
export const Loading: Story = {
  parameters: { fixture: { routes: [registry, routines, route('GET', '/api/models?workspace=storybook-alpha', held)] } },
  render: () => workspacePanel(SettingsWorkspaceModels),
  play: async ({ canvas }) => { expect(await canvas.findByText('Loading model options…')).toBeTruthy() },
}
export const Failure: Story = {
  parameters: { fixture: { routes: [registry, routines, route('GET', '/api/models?workspace=storybook-alpha', () => json({ error: 'Synthetic failure' }, 500))] } },
  render: () => workspacePanel(SettingsWorkspaceModels),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('alert')).toHaveTextContent('Could not load the model list.')
    expect(canvas.getByRole('button', { name: 'Retry' })).toBeEnabled()
  },
}

const loadedRoutines = route('GET', '/api/settings/routines', () => json({
  provider_default_models: { claude: 'sonnet' }, provider_default_modes: { claude: 'auto' },
  provider_default_thinking: { claude: 'high' }, provider_insights_models: {},
  model_options: { anthropic: ['sonnet', 'opus'] },
}))
const loadedModels = route('GET', '/api/models?workspace=storybook-alpha', () => json({
  models: ['sonnet', 'opus'], default: 'sonnet', provider_models: { claude: ['sonnet', 'opus'] },
  provider_defaults: { claude: 'sonnet' }, thinking_levels: { claude: ['low', 'medium', 'high'] },
}))
const editRoutes = [registry, loadedRoutines, loadedModels]
async function editThinking(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas'], userEvent: Parameters<NonNullable<Story['play']>>[0]['userEvent']) {
  const select = await canvas.findByLabelText('Thinking')
  await waitFor(() => expect(select).toBeEnabled())
  await userEvent.selectOptions(select, 'low')
  expect(canvas.getByRole('button', { name: 'Save changes' })).toBeEnabled()
}
export const DirtyAndDiscard: Story = {
  parameters: { fixture: { routes: editRoutes } },
  render: () => workspacePanel(SettingsWorkspaceModels),
  play: async ({ canvas, userEvent }) => {
    await editThinking(canvas, userEvent)
    await userEvent.click(canvas.getByRole('button', { name: 'Discard' }))
    expect(canvas.queryByRole('button', { name: 'Save changes' })).toBeNull()
    expect(canvas.getByLabelText('Thinking')).toHaveValue('')
  },
}
export const Saving: Story = {
  parameters: { fixture: { routes: [...editRoutes, route('PATCH', '/api/workspaces/storybook-alpha', held)] } },
  render: () => workspacePanel(SettingsWorkspaceModels),
  play: async ({ canvas, userEvent }) => {
    await editThinking(canvas, userEvent)
    await userEvent.click(canvas.getByRole('button', { name: 'Save changes' }))
    expect(canvas.getByRole('button', { name: 'Saving...' })).toBeDisabled()
    expect(canvas.getByLabelText('Thinking')).toBeDisabled()
  },
}
export const SaveFailure: Story = {
  parameters: { fixture: { routes: [...editRoutes, route('PATCH', '/api/workspaces/storybook-alpha', () => json({ error: 'Synthetic save refusal' }, 500))] } },
  render: () => workspacePanel(SettingsWorkspaceModels),
  play: async ({ canvas, userEvent }) => {
    await editThinking(canvas, userEvent)
    await userEvent.click(canvas.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Save changes' })).toBeEnabled())
    expect(canvas.getByLabelText('Thinking')).toHaveValue('low')
  },
}
let saved = false
export const Saved: Story = {
  parameters: { fixture: { routes: [
    route('GET', '/api/workspaces', () => json({ workspaces: [{ name: 'storybook-alpha', vault_root: '/storybook/alpha', default_provider: 'claude',
      ...(saved ? { provider_overrides: { claude: { thinking: 'low' } } } : {}) }], primary: 'storybook-alpha' })),
    loadedRoutines, loadedModels,
    route('PATCH', '/api/workspaces/storybook-alpha', () => { saved = true; return json({ workspaces: [{ name: 'storybook-alpha', vault_root: '/storybook/alpha', default_provider: 'claude', provider_overrides: { claude: { thinking: 'low' } } }] }) }),
  ] } },
  render: () => { saved = false; return workspacePanel(SettingsWorkspaceModels) },
  play: async ({ canvas, userEvent }) => {
    await editThinking(canvas, userEvent)
    await userEvent.click(canvas.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(canvas.queryByRole('button', { name: 'Save changes' })).toBeNull())
    expect(canvas.getByLabelText('Thinking')).toHaveValue('low')
  },
}
