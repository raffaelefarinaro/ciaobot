import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsKeyboardShortcuts from '../components/settings/SettingsKeyboardShortcuts.vue'
import { json, route } from './settingsAccessFixture'

const keyboard = { keyboard_shortcuts: {}, keyboard_send_mode: 'modifier', revision: 'synthetic' }
const read = route('GET', '/api/settings/keyboard', () => json(keyboard))
const meta = {
  id: 'settings-keyboard-shortcuts', title: 'Settings/SettingsKeyboardShortcuts', component: SettingsKeyboardShortcuts,
  tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } },
} satisfies Meta<typeof SettingsKeyboardShortcuts>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  parameters: { fixture: { routes: [read] } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByText('Shortcut bindings'))
    expect(canvas.getAllByRole('button', { name: 'Change' }).length).toBeGreaterThan(0)
    expect(canvas.getByRole('combobox')).toHaveValue('modifier')
  },
}
export const Capture: Story = {
  parameters: { fixture: { routes: [read] } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByText('Shortcut bindings'))
    await userEvent.click(canvas.getAllByRole('button', { name: 'Change' })[0])
    expect(canvas.getByText('Press a shortcut…')).toBeTruthy()
    await userEvent.keyboard('{Escape}')
    expect(canvas.queryByText('Press a shortcut…')).toBeNull()
  },
}
export const Conflict: Story = {
  parameters: { fixture: { routes: [read] } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByText('Shortcut bindings'))
    await userEvent.click(canvas.getAllByRole('button', { name: 'Change' })[0])
    await userEvent.keyboard('{Alt>}{s}{/Alt}')
    expect(await canvas.findByRole('alert')).toHaveTextContent('That shortcut is already assigned to Show or hide the sidebar.')
    expect(canvas.getByText('Press a shortcut…')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Cancel' })).toBeEnabled()
  },
}
export const LoadFailure: Story = {
  parameters: { fixture: { routes: [route('GET', '/api/settings/keyboard', () => json({ error: 'Synthetic failure' }, 500))] } },
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('alert')).toHaveTextContent('Could not load keyboard settings.'))
  },
}
export const SaveFailure: Story = {
  parameters: { fixture: { routes: [read, route('PATCH', '/api/settings/keyboard', () => json({ error: 'Synthetic save refusal' }, 500))] } },
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('combobox')).toHaveValue('modifier'))
    await userEvent.selectOptions(canvas.getByRole('combobox'), 'enter')
    expect(await canvas.findByRole('alert')).toHaveTextContent('Synthetic save refusal')
    expect(canvas.getByRole('combobox')).toHaveValue('modifier')
  },
}
export const Saving: Story = {
  parameters: { fixture: { routes: [read, route('PATCH', '/api/settings/keyboard', () => new Promise<never>(() => {}))] } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.selectOptions(canvas.getByRole('combobox'), 'enter')
    expect(canvas.getByRole('combobox')).toBeDisabled()
    expect(canvas.getByRole('button', { name: 'Restore all defaults' })).toBeDisabled()
  },
}
