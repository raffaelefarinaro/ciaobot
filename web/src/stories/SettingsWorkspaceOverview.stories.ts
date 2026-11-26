import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsWorkspaceOverview from '../components/settings/SettingsWorkspaceOverview.vue'
import { held, json, route } from './settingsAccessFixture'
import { workspacePanel } from './workspaceAdminFixture'

const meta = { id: 'settings-workspace-overview', title: 'Settings/SettingsWorkspaceOverview', component: SettingsWorkspaceOverview,
  tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } } } satisfies Meta<typeof SettingsWorkspaceOverview>
export default meta
type Story = StoryObj<typeof meta>
export const Loading: Story = {
  parameters: { fixture: { routes: [route('GET', '/api/workspaces', held)] } },
  render: () => workspacePanel(SettingsWorkspaceOverview),
  play: async ({ canvas }) => { expect(await canvas.findByLabelText('Loading workspace')).toBeTruthy() },
}
export const Failure: Story = {
  parameters: { fixture: { routes: [route('GET', '/api/workspaces', () => json({ error: 'Synthetic failure' }, 500))] } },
  render: () => workspacePanel(SettingsWorkspaceOverview),
  play: async ({ canvas }) => { expect(await canvas.findByText(/Failed to load workspaces/)).toBeTruthy() },
}
export const Editor: Story = {
  parameters: { fixture: { routes: [
    route('GET', '/api/workspaces', () => json({ workspaces: [{ name: 'storybook-alpha', vault_root: '/storybook/alpha', default_provider: 'claude' }], primary: 'storybook-alpha' })),
    route('GET', '/api/settings/routines', held),
    route('GET', '/api/models?workspace=storybook-alpha', held),
    route('GET', '/api/proposals/handover?workspace=storybook-alpha', held),
    route('GET', '/api/workspaces/storybook-alpha/shared-brains', held),
    route('GET', '/api/care/cycle?workspace=storybook-alpha', held),
    route('GET', '/api/update-tasks?workspace=storybook-alpha', held),
  ] } },
  render: () => workspacePanel(SettingsWorkspaceOverview),
  play: async ({ canvas, userEvent }) => {
    const name = await canvas.findByLabelText('Name')
    expect(name).toHaveValue('storybook-alpha')
    await userEvent.clear(name)
    await userEvent.type(name, 'storybook-renamed')
    expect(canvas.getByRole('button', { name: 'Save changes' })).toBeEnabled()
    await userEvent.click(canvas.getByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(name).toHaveValue('storybook-alpha'))
    expect(canvas.queryByRole('button', { name: 'Save changes' })).toBeNull()
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 7\. Violations: 0/)).toBeTruthy())
  },
}
