import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsAllWorkspaces from '../components/settings/SettingsAllWorkspaces.vue'
import { held, json, route } from './settingsAccessFixture'
import { workspacePanel } from './workspaceAdminFixture'

const meta = { id: 'settings-all-workspaces', title: 'Settings/SettingsAllWorkspaces', component: SettingsAllWorkspaces,
  tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } } } satisfies Meta<typeof SettingsAllWorkspaces>
export default meta
type Story = StoryObj<typeof meta>
const registry = route('GET', '/api/workspaces', () => json({ workspaces: [{ name: 'storybook-alpha', vault_root: '/storybook/alpha', default_provider: 'claude' }], primary: 'storybook-alpha' }))
export const ArchivedEmpty: Story = {
  parameters: { fixture: { routes: [registry, route('GET', '/api/workspaces/archived', () => json({ archived: [] }))] } },
  render: () => workspacePanel(SettingsAllWorkspaces, true),
  play: async ({ canvas }) => { await waitFor(() => expect(canvas.getByText('No archived workspaces.')).toBeTruthy()) },
}
export const ArchivedLoading: Story = {
  parameters: { fixture: { routes: [registry, route('GET', '/api/workspaces/archived', held)] } },
  render: () => workspacePanel(SettingsAllWorkspaces, true),
  play: async ({ canvas }) => { await waitFor(() => expect(canvas.getByText('Loading…')).toBeTruthy()) },
}
export const ArchivedFailure: Story = {
  parameters: { fixture: { routes: [registry, route('GET', '/api/workspaces/archived', () => json({ error: 'Synthetic failure' }, 500))] } },
  render: () => workspacePanel(SettingsAllWorkspaces, true),
  play: async ({ canvas }) => { expect(await canvas.findByRole('alert')).toHaveTextContent('Could not load archived workspaces') },
}
export const ArchivedPopulated: Story = {
  parameters: { fixture: { routes: [registry, route('GET', '/api/workspaces/archived', () => json({ archived: [
    { id: 'archive-alpha', name: 'storybook-old', archived_at: '2026-11-20T10:00:00Z', path: '.archived-workspaces/storybook-old', layout: 'per-root', color: 'amber', default_provider: 'claude', schedules: 2, restorable: true, blocked_reason: '' },
    { id: 'archive-beta', name: 'storybook-blocked', archived_at: '2026-11-21T10:00:00Z', path: '.archived-workspaces/storybook-blocked', layout: 'per-root', color: 'amber', default_provider: 'claude', schedules: 0, restorable: false, blocked_reason: 'A workspace with this name already exists.' },
  ] }))] } },
  render: () => workspacePanel(SettingsAllWorkspaces, true),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('button', { name: 'Restore workspace storybook-old' })).toBeEnabled()
    expect(canvas.getByRole('button', { name: 'Restore workspace storybook-blocked' })).toBeDisabled()
    expect(canvas.getByText('A workspace with this name already exists.')).toBeTruthy()
  },
}
const three = ['storybook-alpha', 'storybook-beta', 'storybook-gamma'].map(name => ({ name, vault_root: `/storybook/${name}`, default_provider: 'claude' }))
const multiple = route('GET', '/api/workspaces', () => json({ workspaces: three, primary: 'storybook-alpha' }))
const archives = route('GET', '/api/workspaces/archived', () => json({ archived: [] }))
let reordered = false
export const Reordered: Story = {
  parameters: { fixture: { routes: [route('GET', '/api/workspaces', () => json({ workspaces: reordered ? [three[0], three[2], three[1]] : three, primary: 'storybook-alpha' })), archives,
    route('POST', '/api/workspaces/reorder', () => { reordered = true; return json({ workspaces: [three[0], three[2], three[1]], primary: 'storybook-alpha' }) })] } },
  render: () => { reordered = false; return workspacePanel(SettingsAllWorkspaces, true) },
  play: async ({ canvas, canvasElement, userEvent }) => {
    const handle = await canvas.findByRole('button', { name: /Reorder Storybook Gamma/ })
    handle.focus()
    await userEvent.keyboard('{Alt>}{ArrowUp}{/Alt}')
    await waitFor(() => expect([...canvasElement.querySelectorAll('.workspace-row')].map(row => row.id)).toEqual(['workspace-row-storybook-alpha', 'workspace-row-storybook-gamma', 'workspace-row-storybook-beta']))
    expect(document.activeElement).toBe(handle)
  },
}
export const ReorderFailure: Story = {
  parameters: { fixture: { routes: [multiple, archives, route('POST', '/api/workspaces/reorder', () => json({ error: 'Synthetic refusal' }, 500))] } },
  render: () => workspacePanel(SettingsAllWorkspaces, true),
  play: async ({ canvas, canvasElement, userEvent }) => {
    const handle = await canvas.findByRole('button', { name: /Reorder Storybook Gamma/ })
    handle.focus()
    await userEvent.keyboard('{Alt>}{ArrowUp}{/Alt}')
    expect(await canvas.findByRole('alert')).toHaveTextContent('Could not save the workspace order')
    expect([...canvasElement.querySelectorAll('.workspace-row')].map(row => row.id)).toEqual(['workspace-row-storybook-alpha', 'workspace-row-storybook-beta', 'workspace-row-storybook-gamma'])
  },
}
